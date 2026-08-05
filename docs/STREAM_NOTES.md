# PumpPortal stream: verified observations

Everything here was measured against the live stream, not taken from
documentation. Public docs and blog posts are stale relative to the cashback
upgrade and BOOST (2026-07-21), so anything load-bearing gets verified here
first.

**Sample:** 2026-08-02, two runs totalling ~3 minutes, 101 launch frames
(53 on the clean v2 schema), 2 migration frames. Sections 7, 7a and 7b were
added 2026-08-03 from on-chain verification (`sniper verify-program`), not from
the stream.

---

## 1. The stream carries two venues, not one

`subscribeNewToken` delivers **both pump.fun and bonk.fun** launches, with
different payload shapes:

| | pump.fun | bonk.fun |
|---|---|---|
| `pool` | `"pump"` | `"bonk"` |
| mint suffix | usually `…pump` | usually `…bonk` |
| reserves | `vSolInBondingCurve`, `vTokensInBondingCurve` | `solInPool`, `tokensInPool` |
| curve address | `bondingCurveKey` | absent |
| observed share | ~98% | ~2% |

Mint suffixes are **not** reliable (`…hh7p`, `…HBES`, `…1Amz` all appeared with
`pool="pump"`). Always branch on `pool`.

Everything is recorded at Tier 0 since it is free; `pumpportal.promote_pools`
controls what reaches enrichment. Scope is currently `["pump"]`.

## 2. There is no timestamp field

`timestamp` appeared in **0 of 101** frames. `event_ts` is therefore always NULL.
This is the source not sending it, not a parse bug.

Consequence: `received_at` is our own clock and includes network and indexer
latency. Any timing feature that matters must use on-chain block time resolved
from `signature`, not `received_at`.

## 3. Create frames report post-pre-buy state

Verified on all 53 pump rows:

```
v_sol_in_curve == 30.0 + initial_buy_sol      (max residual 3.8e-10)
```

Virtual SOL opens at exactly **30.0** and virtual tokens at exactly
**1,073,000,000** every time. The create frame is emitted *after* the dev's
pre-buy lands.

Two consequences worth having:

* **Dev pre-buy is readable at t=0 with zero RPC calls** — it is
  `vSolInBondingCurve - 30`. This is a headline rug signal available on the free
  firehose.
* It gives a **free integrity check** on every frame; a residual means the
  program changed or our decode is wrong.

Observed pre-buys ranged 0 to 11.85 SOL. Only 2 of 53 launched with none. An
11.85 SOL pre-buy is already **13.9% of the entire bonding curve** bought before
any outside participant can act.

## 4. The constant product is exact and invariant

```
k = vSol × vTokens = 30 × 1_073_000_000 = 3.219e10
```

Worst relative residual across all 53 rows: **1.185e-16** — float noise.

So price and curve progress are computable from virtual SOL alone. No RPC call
is needed to *interpret* a frame, only to *refresh* it. `curve.py` implements
this and `tests/test_curve.py` pins it against these live fixtures.

Graduation is at 115 vSOL, so:

```
progress = (vSol - 30) / 85
```

Full bonding-curve traverse is a **~14.7x** price move from launch to graduation.

## 5. Instant-bond bundles exist and will poison naive features

Observed `TNOS` (`3GWsyds…pump`): create frame at `1785705522.687`, migrate frame
at `1785705522.711` — **24 milliseconds apart**.

115 vSOL is physically unreachable in 24ms of wall time. This is a **create +
full-curve buy landing in a single Jito bundle**: the deployer bonds their own
token instantly.

Why it matters:

* It is **not** an organic graduation, and it is **unbuyable** — there is no
  moment at which we could have entered.
* Counted naively it inflates the graduation rate and teaches any model that
  "graduates fast" is a positive, when it is the signature of insider
  extraction.

These are flagged via `migrations.instant_bond` (gap < 5s) and excluded from the
cohort graduation rate.

## 6. Graduation rate must be cohort-based

`COUNT(migrations) / COUNT(launches)` is **meaningless**: most migrations we
observe belong to tokens launched hours or days before we connected. Numerator
and denominator describe different cohorts. Our 75s smoke test produced a
nonsense "2.083%" this way.

The honest figure, implemented in `sniper stats`:

> Of pump.fun tokens **we recorded at launch** and have watched for at least one
> full horizon, what fraction migrated — excluding instant-bond bundles?

This needs >24h of collection before it reports anything.

## 7. `is_mayhem_mode` runs a different curve — k does not hold

**Resolved 2026-08-03 by `sniper verify-program` against chain (n=37).** The flag
is real and on-chain: it sits at offset 81 of the `BondingCurve` account, and the
stream's value agreed with chain on 37/37.

The finding that matters is far worse than "a distinct base rate":

> **Every mayhem token violates the constant product, and every non-mayhem token
> satisfies it. Perfect separation, no exceptions.**

| | k intact | k broken |
|---|---|---|
| `is_mayhem_mode = false` | **23** | 0 |
| `is_mayhem_mode = true` | 0 | **14** |

Non-mayhem residuals sit at 1e-9 to 1e-16. Mayhem residuals run 6e-2 to 6.2.

Mayhem is not the same curve with a different constant — it is a different
mechanism, and it breaches bounds the standard curve makes impossible:

* virtual tokens **exceed** the 1.073e9 opening reserve (observed 1.163e9),
  where the standard curve only ever spends tokens down;
* virtual SOL is observed at **3.86** (below the 30 floor) and at **218.0** —
  well past the 115 graduation level — while `complete` is still false. So the
  115 vSOL graduation trigger does not appear to apply either.

**Consequences.** For mayhem tokens, `progress_pct`, `dev_prebuy_sol` and any
k-derived price are *meaningless*, not merely noisy. In particular `vSol - 30` is
not a dev pre-buy when vSol can open below 30. Gate on
`curve.standard_curve_applies()` before computing any of them, and treat
`is_mayhem_mode IS NULL` as unsafe rather than false.

**The rate is volatile and much higher than first measured.** Successive samples
on 2026-08-03 gave 38% (14/37), 3% (1/30) and 8% (3/40), against 7.5% on
2026-08-02. Whatever drives it swings hour to hour, so it must be measured per
cohort rather than assumed.

## 7a. Mints are Token-2022, not SPL Token

Measured 2026-08-03: **60/60 pump.fun mints are owned by the Token-2022 program**
(`TokenzQd…`), not SPL Token (`Tokenkeg…`). Widely-repeated documentation says
otherwise. This is the one place on Solana where per-token *code* exists, so it
is worth being precise about.

All 60 carry exactly two extensions — **MetadataPointer (18)** and
**TokenMetadata (19)** — and nothing else. Zero instances of the extensions that
would let someone tax, seize, freeze or block a sale: `TransferFeeConfig`,
`MintCloseAuthority`, `DefaultAccountState`, `NonTransferable`,
`PermanentDelegate`, `TransferHook`. Mint and freeze authority are revoked on
all of them, decimals are 6.

So pump.fun uses Token-2022 for **on-chain metadata**, not for control over
holders. Two consequences:

* The safety story holds, but it now rests on an extension set that can change
  per token. `verify-program` checks it on every run rather than assuming it.
* **Name, symbol and URI live on-chain in the mint account.** That is a free,
  permanent, batchable (100/call) alternative to fetching them over HTTP, and it
  is immune to the IPFS/host decay that threatens `launches.uri`. Worth
  exploiting in #4 — though the *socials* still live in the JSON at that URI.

## 7b. On-chain `creator` is not always the transaction signer

The `creator` at offset 49 matched the stream's `traderPublicKey` on 32/35,
26/29 and 14/19 across samples — consistently ~10–25% mismatch. The layout is
correct (the rest match byte-for-byte), so the mismatches are real: a proxy or
bundler signs the create transaction on someone else's behalf.

Deployer-history features must therefore decide *which* identity they key on,
and "signer != creator" is itself a candidate signal.

## 8. Serial spam is the dominant behaviour

In a single 75-second window:

| creator | launches | distinct symbols |
|---|---|---|
| `B1g4Nyyu…` | 5 | 1 |
| `8vXbNgsS…` | 3 | 1 |
| `bwamJzzt…` | 3 | 2 |

Two mints from `BrhgsRNS…` shared an identical name, symbol and metadata URI
seconds apart. Deployer-history features should be high-value, and duplicate
`(creator, uri)` is a cheap, free spam filter.

## 9. Non-event frames

Subscription acks arrive as `{"message": "..."}` and classify as `unknown`. They
are counted and sampled into the `events` table so that a silent protocol change
shows up as a spike in unclassified frames rather than as missing launches.

---

## Observed launch rate

~1,500–2,700 launches/hour across both venues in the sampled windows
(≈36k–65k/day). Consistent with the funnel volumes assumed in the plan.
