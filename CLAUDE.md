# meme-sniper — working notes

Identify pump.fun meme coins at launch that are not rugs and appreciate sharply
within 1 hour to 1 week. **Phase 1 is a measurement instrument, not a bot.**

Read `docs/STREAM_NOTES.md` before touching ingest or feature code — it holds
empirically verified facts about the live stream that contradict the public
documentation.

## Environment

Windows. Python 3.11 via the **`py -3.11`** launcher — the `python.exe` on PATH
is the Microsoft Store stub and fails.

```powershell
.venv\Scripts\python.exe -m sniper.main record          # Tier 0 recorder
.venv\Scripts\python.exe -m sniper.main enrich --loop   # Tier 1 socials resolver
.venv\Scripts\python.exe -m sniper.main stats           # collection summary
.venv\Scripts\python.exe -m sniper.main ratecheck       # throttling detector
.venv\Scripts\python.exe -m sniper.main verify-program  # on-chain assumption canary
.venv\Scripts\python.exe -m pytest tests/ -q
```

Production is EC2 `107.20.45.160` (t3.micro). Deploy with
`.\deploy\push-code.ps1 -RemoteHost <ip> -KeyFile <pem>`; on the box the CLI is
just `sniper <cmd>`. Three units must all be active: `meme-sniper`,
`meme-sniper-enrich`, `litestream`.

Not a git repo yet. `uv` is not installed; `node` and `git` are.

## Non-negotiable rules

1. **Never hardcode thresholds.** Everything tunable lives in `config.yaml`.
   `Config.get()` raises on a missing key rather than silently defaulting.

2. **Never delete or truncate `data/sniper.db`.** It cannot be backfilled and
   the post-BOOST regime is only weeks old. Move aside, never overwrite.

3. **Always preserve `raw_json`.** Feature definitions will change; the raw
   event is the only thing that makes recomputation possible.

4. **Test fixtures must be real captured frames.** I once hand-reconstructed a
   fixture and it failed against the verified curve math. Dump real rows from
   the DB — never invent plausible-looking values and label them "verbatim".

5. **The recorder must never die from a downstream error.** `on_launch`
   handlers are wrapped; keep it that way.

6. **Don't use PumpPortal `subscribeTokenTrade`.** It is metered (0.01 SOL /
   10k events + funded wallet). Only `subscribeNewToken` and
   `subscribeMigration` are free. Price comes from bonding-curve polling.

7. **One WebSocket connection, multiplexed.** Per-token connections trigger
   hourly bans.

## Verified facts (measured, not documented)

- `k = vSol × vTokens = 3.219e10`, invariant across every launch (worst
  residual **1.2e-16**). Virtual SOL opens at exactly **30.0**, tokens at
  **1,073,000,000**. Graduation at **115 vSOL** ⇒ `progress = (vSol-30)/85`,
  a ~14.7x move.
- Create frames report **post-pre-buy** state, so `vSol - 30` **is the dev
  pre-buy** — a headline rug signal, free, at t=0, with no RPC call.
- The stream carries **bonk.fun as well as pump.fun**, with different payload
  shapes. Branch on `pool`, never on mint suffix (`…hh7p`, `…HBES` were pump).
- **No `timestamp` field exists** (0/101 frames). `received_at` is our clock.
  Real timing features need on-chain block time from `signature`.
- **Instant-bond bundles are real**: observed create and migrate frames 24ms
  apart. Create + full-curve buy in one Jito bundle. Unbuyable, not organic,
  and they teach a naive model that "graduates fast" is good. Flagged via
  `migrations.instant_bond`.
- **`is_mayhem_mode` tokens do NOT obey `k`.** Verified on chain 2026-08-03,
  n=37, *perfect* separation: every mayhem token violates the constant product,
  every non-mayhem token satisfies it. Mayhem vSol goes below 30 and above 115
  without completing, and vTokens exceeds the 1.073e9 opening reserve. So
  `progress_pct`, `dev_prebuy_sol` and k-derived price are **meaningless** there,
  not just noisy. Gate on `curve.standard_curve_applies()`; NULL means unsafe.
  Observed rate swings hard (3%–38% across same-day samples).
- **Mints are Token-2022, not SPL Token** (60/60), carrying only
  MetadataPointer + TokenMetadata — metadata, not control. No transfer fee,
  transfer hook or permanent delegate anywhere. Name/symbol/uri are therefore
  readable **on-chain**, batchable 100/call, immune to IPFS decay.
- **On-chain `creator` ≠ transaction signer on ~10–25% of launches** (proxy or
  bundler deploys). The layout is right; the mismatch is real and is itself a
  candidate signal.
- **Token metadata rots within days, so Tier 1 is a race.** Measured 2026-08-06
  on launches collected 2026-08-03: **36%** (5,784/16,197) already pointed at a
  host returning 404 — `metadata.j7tracker.io` alone was 5,181 of them. Fetch
  success is **98.6% near-real-time vs 64% at three days**. Metadata not fetched
  close to launch is not pending, it is lost — hence an always-on worker.
  Reading the URI on-chain makes the *URI* durable, **not the document**.
- **Telegram is rare: 2.0%** of launches (364/18,143); twitter 73%, website 51%.
  Only 1.5% carry all three. If the pre-BOOST 8.94x Telegram lift survives, that
  rarity makes it a harsh but high-precision gate.
- **Bonding-curve layout is derivable, not reverse-engineered.** pump.fun
  publishes no program source — only docs and IDL at
  `github.com/pump-fun/pump-public-docs` — but Anchor/Borsh is deterministic.
  Layout is pinned in `curve.py`; fields end at 84 bytes, accounts are
  over-allocated to 115 or 151. Never assert an exact account length.

## Traps

- **`migrations / launches` is NOT a graduation rate.** Most migrations we see
  belong to tokens launched before we connected — different cohorts. My first
  stats output reported a confident, meaningless "2.083%". Only cohort-based
  rates, excluding instant bonds.
- **A throttled stream looks exactly like a quiet market.** Hence `ratecheck`,
  against the 1,500–2,700 launches/hour residential baseline.
- **Litestream must target a non-AWS bucket.** The AWS Free plan auto-closes
  the account at 6 months and takes same-account S3 with it.

- **Absent socials are empty strings, not missing keys.** Real documents carry
  `"website": "", "telegram": ""`. Testing key presence scores them as present
  and inverts the signal Tier 1 exists to measure.

- **A dead metadata host is not always an HTTP error.** One returns 404 with a
  27 KB HTML page, others 404 with `application/json`. Success must mean
  "parsed to a JSON object", not "did not raise".

- **Litestream 0.5.x silently ignores unknown config keys.** 0.3.x-style
  `retention:` under the replica parses fine and leaves you on the 24h default.
  Verified with `bogus-key-xyz: 42` — exit 0. Check `litestream version` before
  trusting any example.

- **`PRAGMA integrity_check` returns `ok` on a zero-byte file.** Never gate a
  backup or a data pull on it alone; check row counts too.

## Context that drives the design

Pump.fun shipped **BOOST on 2026-07-21**, moving graduation rates from ~0.2% to
4.7–6.7%. Every published model and threshold predates it. That is why we
record first and fit later — the literature's constants describe a dead regime.

Where the edge must come from: **not speed.** Bundle snipers own blocks 1–2 and
we will not beat them on a free tier. The strongest published predictor is
`trades_to_reach_vsol` — reaching a SOL level in *fewer* trades (large-ticket
conviction over bot micro-churn). That is a selection edge on a 30s–5min
horizon.

**Kill criterion, agreed in advance:** if three weeks of paper trading is
negative net of a 2% round-trip cost model, no edge exists at this latency tier
and we stop or change approach.

## Style

Match the existing code: comments explain *why* (especially where a value was
empirically verified or a trap avoided), not *what*. Dataclasses for state,
`asyncio` throughout, type hints, `from __future__ import annotations`.
