"""Verify the pump.fun program assumptions this project is built on.

Everything downstream assumes two things that were never actually checked:

1. **Pump.fun mints are safe by construction** - mint and freeze authority
   revoked, fixed decimals, no per-token code that can tax or seize. On Solana
   there is no per-token contract to audit (all pump.fun tokens share one
   program), so the rug surface is authority and distribution rather than
   bytecode.

2. **The bonding-curve byte layout is what the IDL says.** ``curve.py`` decodes
   it; this confirms the decode against chain.

Both are properties of a closed-source program that has shipped breaking changes
twice (cashback, BOOST). So this is written as a repeatable canary with
meaningful exit codes, not a one-off script.

Two things this found on its first run, on 2026-08-03, that contradicted the
project's assumptions:

* **Pump.fun mints are Token-2022, not SPL Token** (60/60). That matters because
  Token-2022 extensions are the one genuine per-token code risk on Solana. All
  60 carried only MetadataPointer + TokenMetadata - metadata, not control - but
  the extension set is now checked on every run rather than assumed.
* **``is_mayhem_mode`` tokens do not obey the constant product** (perfect
  separation, n=37). They are excluded from the k check and flagged instead.

The decode is validated against three anchors that do not depend on the current
state of the curve:

* ``creator`` at offset 49 must match ``launches.creator`` captured from the
  stream. A 32-byte exact match confirms the entire preceding layout.
* ``token_total_supply`` must be identical across every sampled token.
* ``k_residual()`` must be ~0 for standard curves - k is invariant at every
  point, so trading cannot break it, only a bad decode can.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
from collections import Counter
from dataclasses import dataclass, field

import base58

from . import curve, db, token2022
from .config import Config, resolve_path
from .rpc import AccountInfo, RpcError, SolanaRpc

log = logging.getLogger("sniper.verify")

# SPL Mint is a fixed 82-byte layout (not Anchor, no discriminator):
#    0  mint_authority     COption<Pubkey>  4-byte tag + 32
#   36  supply             u64
#   44  decimals           u8
#   45  is_initialized     bool
#   46  freeze_authority   COption<Pubkey>  4-byte tag + 32
MINT_ACCOUNT_SIZE = token2022.MINT_ACCOUNT_SIZE

# Token-2022 mints reuse that 82-byte prefix and append a TLV extension region;
# the layout and its constants live in ``token2022``.

# The System Program owning a "curve" means the account does not exist yet.
# getMultipleAccounts can return a zero-length System-owned stub rather than
# null for an address that has never been written, and treating that as a decode
# failure would report a bad layout when the truth is a race with the recorder.
SYSTEM_PROGRAM = "11111111111111111111111111111111"


@dataclass(frozen=True)
class MintAccount:
    mint_authority: str | None
    supply: int
    decimals: int
    is_initialized: bool
    freeze_authority: str | None


def decode_mint(data: bytes) -> MintAccount:
    """Decode an SPL Mint account.

    Token-2022 mints reuse this prefix and append extensions, so a length
    greater than 82 is not an error here - the owning program is what
    distinguishes them, and that is checked separately.
    """
    if len(data) < MINT_ACCOUNT_SIZE:
        raise ValueError(f"mint account too short: {len(data)} < {MINT_ACCOUNT_SIZE}")

    def coption_pubkey(offset: int) -> str | None:
        tag = struct.unpack_from("<I", data, offset)[0]
        if tag == 0:
            return None
        return base58.b58encode(data[offset + 4 : offset + 36]).decode()

    return MintAccount(
        mint_authority=coption_pubkey(0),
        supply=struct.unpack_from("<Q", data, 36)[0],
        decimals=data[44],
        is_initialized=bool(data[45]),
        freeze_authority=coption_pubkey(46),
    )


def token_2022_extensions(data: bytes) -> list[int]:
    """Extension type ids present on a Token-2022 mint.

    Returns empty for a base 82-byte mint. Malformed or truncated TLV stops the
    walk rather than raising: a partial read is still worth reporting, and this
    is a diagnostic, not a consensus-critical parser.

    The walk itself lives in ``token2022`` so Tier 1 enrichment can reuse it to
    pull the TokenMetadata body rather than reimplementing TLV parsing.
    """
    return token2022.extension_types(data)


@dataclass
class Findings:
    """Accumulated observations across the sample."""

    sampled: int = 0
    mint_missing: int = 0
    curve_missing: int = 0

    token_programs: Counter[str] = field(default_factory=Counter)
    unknown_token_program: list[str] = field(default_factory=list)
    extensions_seen: Counter[int] = field(default_factory=Counter)
    hostile_extensions: list[tuple[str, list[str]]] = field(default_factory=list)
    mint_authority_present: list[str] = field(default_factory=list)
    freeze_authority_present: list[str] = field(default_factory=list)
    wrong_decimals: list[tuple[str, int]] = field(default_factory=list)

    discriminator_ok: int = 0
    decode_errors: list[tuple[str, str]] = field(default_factory=list)
    creator_match: int = 0
    creator_mismatch: list[tuple[str, str, str]] = field(default_factory=list)
    k_checked: int = 0
    worst_k_residual: float = 0.0
    bad_k: list[tuple[str, float]] = field(default_factory=list)
    completed_curves: int = 0

    # Mayhem tokens are expected to violate k. Counted, never failed on - but a
    # mayhem token that SATISFIES k would mean the separation broke, which is
    # just as interesting as the reverse.
    mayhem_k_broken: int = 0
    mayhem_k_intact: list[str] = field(default_factory=list)
    mayhem_flag_disagreement: list[str] = field(default_factory=list)

    account_sizes: set[int] = field(default_factory=set)
    total_supplies: set[float] = field(default_factory=set)
    mayhem_true: int = 0
    cashback_values: set[bool | None] = field(default_factory=set)


def _sample_launches(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Most recent pump launches that carry both keys we need.

    Recent is deliberate: their curves are most likely to still be open, which
    keeps the k-invariant check meaningful.
    """
    return conn.execute(
        """
        SELECT mint, creator, bonding_curve_key, v_sol_in_curve, is_mayhem_mode,
               received_at
        FROM launches
        WHERE pool = 'pump'
          AND bonding_curve_key IS NOT NULL
          AND creator IS NOT NULL
        ORDER BY received_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


async def _gather(cfg: Config, rows: list[sqlite3.Row], f: Findings) -> None:
    lamports_per_sol = int(cfg.get("pumpfun.lamports_per_sol"))
    decimals = int(cfg.get("verify.expected_decimals"))
    allowed_programs = set(cfg.get("verify.allowed_token_programs"))
    hostile = {int(k): v for k, v in cfg.section("verify.hostile_extensions").items()}
    max_residual = float(cfg.get("verify.max_k_residual"))
    exclude_mayhem = bool(cfg.get("verify.exclude_mayhem_from_k"))
    expected_disc = bytes(cfg.get("verify.bonding_curve_discriminator"))

    async with SolanaRpc(cfg) as rpc:
        mints = [r["mint"] for r in rows]
        curves = [r["bonding_curve_key"] for r in rows]
        mint_accounts = await rpc.get_multiple_accounts(mints)
        curve_accounts = await rpc.get_multiple_accounts(curves)
        log.info("fetched %d accounts in %d rpc calls", len(mints) * 2, rpc.calls)

    for row, mint_acc, curve_acc in zip(rows, mint_accounts, curve_accounts):
        f.sampled += 1
        _check_mint(row, mint_acc, f, allowed_programs, hostile, decimals)
        _check_curve(
            row, curve_acc, f, expected_disc, lamports_per_sol, decimals,
            max_residual, exclude_mayhem,
        )


def _check_mint(
    row: sqlite3.Row,
    acc: AccountInfo | None,
    f: Findings,
    allowed_programs: set[str],
    hostile: dict[int, str],
    expected_decimals: int,
) -> None:
    if acc is None or not acc.data:
        f.mint_missing += 1
        return

    f.token_programs[acc.owner] += 1
    if acc.owner not in allowed_programs:
        f.unknown_token_program.append(f"{row['mint']} owner={acc.owner}")

    # Token-2022 is not itself a risk - its extensions are. Anything that can
    # tax, seize, freeze or block a sale is a hard stop.
    exts = token_2022_extensions(acc.data)
    f.extensions_seen.update(exts)
    found = [hostile[e] for e in exts if e in hostile]
    if found:
        f.hostile_extensions.append((row["mint"], found))

    try:
        mint = decode_mint(acc.data)
    except ValueError as exc:
        f.decode_errors.append((row["mint"], f"mint: {exc}"))
        return

    if mint.mint_authority is not None:
        f.mint_authority_present.append(f"{row['mint']} -> {mint.mint_authority}")
    if mint.freeze_authority is not None:
        f.freeze_authority_present.append(f"{row['mint']} -> {mint.freeze_authority}")
    if mint.decimals != expected_decimals:
        f.wrong_decimals.append((row["mint"], mint.decimals))


def _check_curve(
    row: sqlite3.Row,
    acc: AccountInfo | None,
    f: Findings,
    expected_disc: bytes,
    lamports_per_sol: int,
    decimals: int,
    max_residual: float,
    exclude_mayhem: bool,
) -> None:
    # Migrated curves are closed, and a curve we recorded moments ago may not be
    # visible at 'confirmed' yet - it comes back as a zero-length System-owned
    # stub. Neither is a layout failure.
    if acc is None or not acc.data or acc.owner == SYSTEM_PROGRAM:
        f.curve_missing += 1
        return

    f.account_sizes.add(acc.size)
    if acc.data[:8] == expected_disc:
        f.discriminator_ok += 1

    try:
        bc = curve.decode_bonding_curve(
            acc.data, lamports_per_sol=lamports_per_sol, token_decimals=decimals
        )
    except curve.CurveError as exc:
        f.decode_errors.append((row["mint"], str(exc)))
        return

    f.total_supplies.add(bc.token_total_supply)
    if bc.is_mayhem_mode:
        f.mayhem_true += 1
    f.cashback_values.add(bc.is_cashback_coin)

    # The stream also reports is_mayhem_mode. Chain disagreeing with it would
    # mean either a bad decode or a stream we cannot trust for the one flag that
    # decides whether curve math applies at all.
    if row["is_mayhem_mode"] is not None and bool(row["is_mayhem_mode"]) != bc.is_mayhem_mode:
        f.mayhem_flag_disagreement.append(row["mint"])

    if bc.creator == row["creator"]:
        f.creator_match += 1
    else:
        f.creator_mismatch.append((row["mint"], row["creator"], bc.creator))

    if bc.complete:
        # Reserves are drained on migration, so k no longer applies.
        f.completed_curves += 1
        return

    residual = bc.to_state().k_residual()

    if bc.is_mayhem_mode and exclude_mayhem:
        # Expected to violate k - mayhem runs a different mechanism entirely.
        if residual > max_residual:
            f.mayhem_k_broken += 1
        else:
            f.mayhem_k_intact.append(row["mint"])
        return

    f.k_checked += 1
    f.worst_k_residual = max(f.worst_k_residual, residual)
    if residual > max_residual:
        f.bad_k.append((row["mint"], residual))


def _report(f: Findings, max_residual: float) -> int:
    """Print findings and return the process exit code."""
    ok = "PASS"
    bad = "FAIL"

    def line(label: str, passed: bool, detail: str) -> None:
        print(f"  [{ok if passed else bad}] {label:<28} {detail}")

    print(f"\nsampled {f.sampled} pump.fun launches")
    if f.mint_missing or f.curve_missing:
        print(
            f"  (mint accounts absent: {f.mint_missing}, "
            f"curve accounts absent: {f.curve_missing} - migrated curves are closed)"
        )

    print("\nmint safety (per-token authority and extensions):")
    programs = ", ".join(f"{p[:8]}..={n}" for p, n in f.token_programs.most_common())
    line("token program known", not f.unknown_token_program, programs or "n/a")
    line("no hostile extensions", not f.hostile_extensions,
         f"{len(f.hostile_extensions)} mints with tax/seize/freeze extensions")
    line("mint authority revoked", not f.mint_authority_present,
         f"{len(f.mint_authority_present)} with a live mint authority")
    line("freeze authority revoked", not f.freeze_authority_present,
         f"{len(f.freeze_authority_present)} with a live freeze authority")
    line("decimals", not f.wrong_decimals,
         f"{len(f.wrong_decimals)} unexpected")

    if f.extensions_seen:
        exts = ", ".join(f"{t}x{n}" for t, n in sorted(f.extensions_seen.items()))
        print(f"       token-2022 extensions present: {exts}")
    for mint, found in f.hostile_extensions[:5]:
        print(f"       HOSTILE {mint}: {', '.join(found)}")
    for detail in f.unknown_token_program[:5]:
        print(f"       UNKNOWN PROGRAM {detail}")
    for detail in f.mint_authority_present[:5] + f.freeze_authority_present[:5]:
        print(f"       {detail}")

    print("\nbonding-curve decode:")
    checked = f.discriminator_ok + len(f.decode_errors)
    line("discriminator", f.discriminator_ok > 0 and not f.decode_errors,
         f"{f.discriminator_ok}/{checked} matched")
    creator_total = f.creator_match + len(f.creator_mismatch)
    line("creator matches stream", f.creator_match > 0,
         f"{f.creator_match}/{creator_total} exact at offset 49")
    line("k invariant (standard)", not f.bad_k,
         f"worst residual {f.worst_k_residual:.3e} over {f.k_checked} open curves "
         f"(tolerance {max_residual:.0e})")
    line("token_total_supply constant", len(f.total_supplies) <= 1,
         ", ".join(f"{s:,.0f}" for s in sorted(f.total_supplies)) or "n/a")
    line("mayhem flag matches stream", not f.mayhem_flag_disagreement,
         f"{len(f.mayhem_flag_disagreement)} disagreements")

    if f.mayhem_true:
        print(f"\n  mayhem-mode tokens: {f.mayhem_true}/{f.sampled} "
              f"({f.mayhem_true / f.sampled * 100:.0f}%) - EXCLUDED from k; "
              f"{f.mayhem_k_broken} confirmed to violate k as expected")
        if f.mayhem_k_intact:
            print(f"  NOTE: {len(f.mayhem_k_intact)} mayhem token(s) SATISFIED k - "
                  "the mayhem/standard split may be breaking down, re-measure:")
            for mint in f.mayhem_k_intact[:5]:
                print(f"       {mint}")

    if f.account_sizes:
        sizes = ", ".join(str(s) for s in sorted(f.account_sizes))
        print(f"\n  observed account size(s): {sizes} bytes "
              f"(fields end at {curve.BONDING_CURVE_MIN_SIZE})")
    print(f"  is_mayhem_mode true:      {f.mayhem_true}/{f.sampled}")
    print(f"  is_cashback_coin values:  {sorted(f.cashback_values, key=str)}")
    if f.completed_curves:
        print(f"  completed curves skipped: {f.completed_curves}")

    for mint, exc in f.decode_errors[:5]:
        print(f"  decode error {mint}: {exc}")
    for mint, want, got in f.creator_mismatch[:5]:
        print(f"  creator mismatch {mint}: stream={want} chain={got}")
    for mint, residual in f.bad_k[:5]:
        print(f"  k residual {mint}: {residual:.3e}")

    # A creator match rate of zero means the layout is wrong. A partial rate is
    # a semantic finding (proxy/bundler deployers sign the tx but are not the
    # recorded creator), not a decode bug - worth surfacing, not worth failing.
    violations = (
        f.unknown_token_program
        or f.hostile_extensions
        or f.mint_authority_present
        or f.freeze_authority_present
        or f.wrong_decimals
        or f.decode_errors
        or f.bad_k
        or f.mayhem_flag_disagreement
        or (creator_total > 0 and f.creator_match == 0)
        or len(f.total_supplies) > 1
    )

    if violations:
        print("\nFAIL - an assumption this project relies on does not hold.")
        print("Do not build further tiers until this is understood.")
        return 1

    if f.sampled == 0 or (f.discriminator_ok == 0 and f.mint_missing >= f.sampled):
        print("\nINCONCLUSIVE - nothing could be checked.")
        return 2

    print("\nOK - all checked assumptions hold.")
    if f.creator_mismatch:
        print(f"NOTE: {len(f.creator_mismatch)} creator mismatches. Layout is right "
              "(others matched exactly); these are likely proxy deployers.")
    return 0


def verify_program(cfg: Config, sample: int | None = None) -> int:
    n = int(sample if sample is not None else cfg.get("verify.sample_size"))
    conn = db.connect(resolve_path(cfg, "database.path"))
    try:
        rows = _sample_launches(conn, n)
    finally:
        conn.close()

    if not rows:
        print("No pump.fun launches with a bonding_curve_key recorded yet.")
        print("Run the recorder for a few seconds first: sniper record")
        return 2

    f = Findings()
    try:
        asyncio.run(_gather(cfg, rows, f))
    except RpcError as exc:
        print(f"RPC unavailable: {exc}")
        print("Set HELIUS_API_KEY, or retry - the public endpoint rate-limits hard.")
        return 2

    return _report(f, float(cfg.get("verify.max_k_residual")))
