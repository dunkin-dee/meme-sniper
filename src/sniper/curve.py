"""Pump.fun bonding-curve math.

Every constant here was verified empirically against live launch frames on
2026-08-02 (n=53); see ``docs/STREAM_NOTES.md`` for the raw evidence. They are
not copied from documentation or blog posts, both of which are stale relative to
the cashback upgrade and BOOST.

Verified facts:

* Virtual SOL opens at exactly ``INITIAL_VIRTUAL_SOL`` (30.0) for every launch.
* Virtual tokens open at exactly ``INITIAL_VIRTUAL_TOKENS`` (1.073e9).
* The constant product ``k = vSol * vTokens = 3.219e10`` is identical across
  all observed launches (spread < 1e-6%).
* A create frame reports post-pre-buy state, so ``vSol - 30`` is the dev's
  pre-buy in SOL.

Because ``k`` is invariant, price and curve progress are computable from virtual
SOL alone - no RPC call is needed to interpret a frame, only to refresh it.

WARNING - none of this applies to ``is_mayhem_mode`` tokens. Verified against
chain on 2026-08-03 (``sniper verify-program``, n=37): every mayhem token
violates the constant product and every non-mayhem token satisfies it - a
perfect split, no exceptions. Mayhem curves are a different mechanism, not the
same curve with a different constant:

* virtual tokens can EXCEED the 1.073e9 opening reserve (observed 1.163e9),
  which the standard curve makes impossible;
* virtual SOL is observed both far below 30 (3.86) and far above the 115
  graduation level (218.0) while ``complete`` is still false.

So ``progress_pct``, ``dev_prebuy_sol`` and any k-derived price are MEANINGLESS
for mayhem tokens - not merely imprecise. Gate on ``standard_curve_applies()``
before using them. Mayhem was 38% of sampled launches (14/37) at the time of
measurement, against the 7.5% recorded in STREAM_NOTES a day earlier, so this is
not a rare edge case.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import base58

INITIAL_VIRTUAL_SOL = 30.0
INITIAL_VIRTUAL_TOKENS = 1_073_000_000.0
K = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS  # 3.219e10

# Graduation fires when total virtual SOL reaches this level.
GRADUATION_VIRTUAL_SOL = 115.0

# SOL that must actually be deposited to graduate, ignoring fees.
CURVE_SOL_SPAN = GRADUATION_VIRTUAL_SOL - INITIAL_VIRTUAL_SOL  # 85.0


class CurveError(ValueError):
    """Raised on physically impossible curve state."""


def standard_curve_applies(is_mayhem_mode: bool | None) -> bool:
    """Whether this module's constant-product math is valid for a token.

    Mayhem-mode tokens run a different curve mechanism entirely (see the module
    docstring), so every k-derived quantity must be gated on this rather than
    computed and quietly trusted. A NULL flag is treated as unknown and
    therefore unsafe - the stream omitted ``is_mayhem_mode`` on ~2% of frames.
    """
    return is_mayhem_mode is False


def tokens_for_sol(virtual_sol: float) -> float:
    """Virtual token reserve implied by a virtual SOL reserve, via k."""
    if virtual_sol <= 0:
        raise CurveError(f"virtual_sol must be positive, got {virtual_sol}")
    return K / virtual_sol


def price_sol_per_token(virtual_sol: float, virtual_tokens: float | None = None) -> float:
    """Spot price in SOL per token.

    Uses the observed token reserve when supplied so that a real on-chain
    reading is never silently overridden by the idealised k; falls back to k
    when only virtual SOL is known.
    """
    if virtual_tokens is None:
        virtual_tokens = tokens_for_sol(virtual_sol)
    if virtual_tokens <= 0:
        raise CurveError(f"virtual_tokens must be positive, got {virtual_tokens}")
    return virtual_sol / virtual_tokens


def progress_pct(virtual_sol: float) -> float:
    """Percentage of the way to graduation, 0-100.

    A dev pre-buy already advances this at t=0: an 11.85 SOL pre-buy puts the
    token ~13.9% along the curve before any outside buyer arrives.
    """
    raw = (virtual_sol - INITIAL_VIRTUAL_SOL) / CURVE_SOL_SPAN * 100.0
    return max(0.0, min(100.0, raw))


def dev_prebuy_sol(virtual_sol_at_create: float) -> float:
    """Dev pre-buy implied by the virtual SOL reported on the create frame."""
    return max(0.0, virtual_sol_at_create - INITIAL_VIRTUAL_SOL)


def sol_to_graduate(virtual_sol: float) -> float:
    """Additional SOL that must enter the curve before graduation."""
    return max(0.0, GRADUATION_VIRTUAL_SOL - virtual_sol)


def buy_cost_sol(virtual_sol: float, tokens_out: float) -> float:
    """SOL required to buy ``tokens_out`` tokens, excluding fees.

    Constant product: buying tokens raises virtual SOL along k.
    """
    virtual_tokens = tokens_for_sol(virtual_sol)
    if tokens_out >= virtual_tokens:
        raise CurveError("cannot buy the entire virtual token reserve")
    new_tokens = virtual_tokens - tokens_out
    return K / new_tokens - virtual_sol


def tokens_out_for_sol(virtual_sol: float, sol_in: float) -> float:
    """Tokens received for ``sol_in``, excluding fees."""
    if sol_in < 0:
        raise CurveError(f"sol_in must be non-negative, got {sol_in}")
    virtual_tokens = tokens_for_sol(virtual_sol)
    return virtual_tokens - K / (virtual_sol + sol_in)


@dataclass(frozen=True)
class CurveState:
    """A point-in-time reading of a bonding curve."""

    virtual_sol: float
    virtual_tokens: float
    real_sol: float | None = None
    real_tokens: float | None = None
    complete: bool = False

    @property
    def price(self) -> float:
        return price_sol_per_token(self.virtual_sol, self.virtual_tokens)

    @property
    def progress(self) -> float:
        return progress_pct(self.virtual_sol)

    @property
    def sol_remaining(self) -> float:
        return sol_to_graduate(self.virtual_sol)

    def k_residual(self) -> float:
        """Relative deviation of this reading from the ideal constant product.

        A large residual means the curve is not behaving as modelled - a program
        upgrade, a mayhem-mode variant, or a decode bug. Worth alerting on rather
        than silently trusting.
        """
        return abs(self.virtual_sol * self.virtual_tokens - K) / K


# --------------------------------------------------------------------------
# On-chain BondingCurve account decode
# --------------------------------------------------------------------------
# Layout derived from the Anchor IDL at github.com/pump-fun/pump-public-docs
# (idl/pump.json). Pump.fun does not publish program source - only docs and the
# IDL - but Anchor/Borsh serialisation is deterministic: an 8-byte account
# discriminator, then fields in declaration order, little-endian, no padding.
# So the layout is derivable rather than reverse-engineered.
#
#   off  field                    type
#     0  discriminator            [u8; 8]
#     8  virtual_token_reserves   u64      base units (token_decimals)
#    16  virtual_sol_reserves     u64      lamports
#    24  real_token_reserves      u64      base units
#    32  real_sol_reserves        u64      lamports
#    40  token_total_supply       u64      base units
#    48  complete                 bool     1 byte
#    49  creator                  Pubkey   32 bytes
#    81  is_mayhem_mode           bool     1 byte
#    82  is_cashback_coin         OptionBool  2 bytes (is_some, value)
#
# Fields end at 84, but the account is allocated larger (~150 bytes) - pump.fun
# over-allocates for forward compatibility, which is how cashback and mayhem
# mode were added without a migration. Never assume an exact account length.
BONDING_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
BONDING_CURVE_MIN_SIZE = 84

_OFF_VIRTUAL_TOKENS = 8
_OFF_VIRTUAL_SOL = 16
_OFF_REAL_TOKENS = 24
_OFF_REAL_SOL = 32
_OFF_TOTAL_SUPPLY = 40
_OFF_COMPLETE = 48
_OFF_CREATOR = 49
_OFF_MAYHEM = 81
_OFF_CASHBACK = 82


@dataclass(frozen=True)
class BondingCurveAccount:
    """Decoded on-chain bonding curve.

    Reserves are converted to human units (SOL, whole tokens) so they can be
    compared directly against the values PumpPortal reports on create frames.
    """

    virtual_tokens: float
    virtual_sol: float
    real_tokens: float
    real_sol: float
    token_total_supply: float
    complete: bool
    creator: str
    is_mayhem_mode: bool
    is_cashback_coin: bool | None
    account_size: int

    def to_state(self) -> CurveState:
        return CurveState(
            virtual_sol=self.virtual_sol,
            virtual_tokens=self.virtual_tokens,
            real_sol=self.real_sol,
            real_tokens=self.real_tokens,
            complete=self.complete,
        )


def decode_bonding_curve(
    data: bytes,
    *,
    lamports_per_sol: int,
    token_decimals: int,
) -> BondingCurveAccount:
    """Decode a raw BondingCurve account.

    Scale factors are injected rather than hardcoded so the decode stays pure and
    the values keep living in config.yaml.

    Raises CurveError on a wrong discriminator or a short account - both mean we
    are pointed at the wrong account or the program changed, and either way a
    silent misparse would produce plausible-looking garbage.
    """
    if len(data) < BONDING_CURVE_MIN_SIZE:
        raise CurveError(
            f"account too short for BondingCurve: {len(data)} < {BONDING_CURVE_MIN_SIZE}"
        )
    if data[:8] != BONDING_CURVE_DISCRIMINATOR:
        raise CurveError(
            f"discriminator mismatch: got {list(data[:8])}, "
            f"expected {list(BONDING_CURVE_DISCRIMINATOR)}"
        )

    def u64(offset: int) -> int:
        return struct.unpack_from("<Q", data, offset)[0]

    token_scale = float(10**token_decimals)
    is_some, cashback_value = data[_OFF_CASHBACK], data[_OFF_CASHBACK + 1]

    return BondingCurveAccount(
        virtual_tokens=u64(_OFF_VIRTUAL_TOKENS) / token_scale,
        virtual_sol=u64(_OFF_VIRTUAL_SOL) / float(lamports_per_sol),
        real_tokens=u64(_OFF_REAL_TOKENS) / token_scale,
        real_sol=u64(_OFF_REAL_SOL) / float(lamports_per_sol),
        token_total_supply=u64(_OFF_TOTAL_SUPPLY) / token_scale,
        complete=bool(data[_OFF_COMPLETE]),
        creator=base58.b58encode(data[_OFF_CREATOR : _OFF_CREATOR + 32]).decode(),
        is_mayhem_mode=bool(data[_OFF_MAYHEM]),
        is_cashback_coin=bool(cashback_value) if is_some else None,
        account_size=len(data),
    )
