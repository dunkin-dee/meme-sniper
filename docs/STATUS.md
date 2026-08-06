# meme-sniper — Status & Roadmap

**Project:** `C:\Users\dimad\projects\meme-sniper`
**Last updated:** 2026-08-06
**Phase 1 of 2.** Phase 1 = measure the regime. Phase 2 = fit a model, only if
Phase 1's numbers justify it.

**Live since 2026-08-05.** Recorder, Tier 1 enrichment and Litestream
replication all running on EC2 (`107.20.45.160`, t3.micro). Repo at
`github.com/dunkin-dee/meme-sniper`.

> **Fresh session?** Read `CLAUDE.md` (auto-loads) and `docs/STREAM_NOTES.md`
> first. STREAM_NOTES contains verified facts that contradict public docs.

---

## Context

Identify pump.fun meme coins at launch that are not rugs and appreciate sharply
within 1 hour to 1 week.

**The fact that drives everything:** pump.fun shipped **BOOST on 2026-07-21**,
reinjecting ~17.6 SOL of dead liquidity as post-migration buybacks/burns.
Graduation rates went from ~0.2% to 4.7–6.7% — about 8x. Every published model,
threshold and heuristic is calibrated to a mechanism that no longer exists.

So Phase 1 measures the *current* regime rather than inheriting stale constants.
Its deliverable is a labelled post-BOOST dataset and a measured hit rate with
confidence intervals — not a strategy.

**Kill criterion, agreed in advance:** if three weeks of paper trading is
negative net of a realistic 2% round-trip cost model, no edge exists at this
latency tier and we stop or change approach. Written down so success isn't
graded on vibes.

**User decisions:** pump.fun only · alert + paper trading (no real funds, no hot
wallet) · free API tiers only · record-first · deploy to AWS EC2 (new account) ·
durability via Litestream + periodic local pull.

---

## Current state

**Collecting in production.** 71 tests pass. As of 2026-08-06, 25.8h into
continuous collection on EC2:

| | |
|---|---|
| launches | 36,228 (~1,400/h) |
| migrations | 1,039 |
| metadata resolved | 18,143 of 18,400 attempted (**98.6%**) |
| promoted to Tier 2 | 13,926 |

`ratecheck` returned exit 0 at 1,684/h with 0 reconnects — **PumpPortal does
not throttle this AWS IP**, which was the single risk that could have
invalidated the whole deployment tier. Litestream restore verified end to end
against Backblaze B2 (5,820 rows recovered vs 5,821 live).

```
CLAUDE.md                    agent working notes (auto-loaded)
config.yaml                  ALL thresholds/budgets/weights - no magic numbers in code
pyproject.toml               py3.11, websockets/httpx/pyyaml/base58
README.md                    project overview
docs/STREAM_NOTES.md         verified empirical findings  <- READ FIRST
docs/DEPLOY.md               EC2 runbook + 6-month cliff
src/sniper/
  config.py                  dotted-path config, ${ENV} expansion, raises on missing
  curve.py                   verified curve math + BondingCurve account decode
  db.py                      SQLite schema v2 + migrations + write helpers
  logging_setup.py           heartbeat-oriented logging
  main.py                    CLI: record | enrich | stats | ratecheck | verify-program
  rpc.py                     rate-limited Solana JSON-RPC (seed for #6)
  token2022.py               TLV extensions + on-chain TokenMetadata decode
  verify.py                  on-chain assumption canary
  ingest/pumpportal.py       Tier 0 firehose, single connection + backoff
  enrich/metadata.py         Tier 1 socials resolver (always-on worker)
tests/test_curve.py          20 tests, fixtures = real captured frames
tests/test_ingest.py         20 tests, payloads = real captured frames
tests/test_account_decode.py 14 tests, fixtures = real account bytes from chain
tests/test_metadata.py       17 tests, fixtures = real fetched documents
deploy/                      3 systemd units, litestream.yml, bootstrap.sh, push/pull scripts
data/sniper.db               local: 16,601 launches (2026-08-03 residential run)
```

### Done

- [x] **Scaffold** — config loader, SQLite schema v2 with migrations, logging
- [x] **Tier 0 recorder** — validated against the live stream over two runs
- [x] **Verified curve math** — `curve.py`, pinned by tests to real frames
- [x] **EC2 deployment — LIVE** (2026-08-05). t3.micro/x86 (`t4g` is not
      free-tier eligible on this plan). Three units, all `Restart=always`.
      Throttling ruled out by measurement; Litestream → Backblaze B2 with a
      **tested** restore.
- [x] **#4 Tier 1 metadata enrichment** (2026-08-06) — `enrich/metadata.py`,
      `token2022.py`, `sniper enrich --loop` as its own unit.
- [x] **#3 Bonding-curve account layout** — decoded and verified against chain
      (`sniper verify-program`). Layout in `curve.py`, RPC client in `rpc.py`.
- [x] **Program assumption canary** — mint/freeze authority, token program,
      Token-2022 extensions, curve decode. Exit 0/1/2, re-runnable after any
      suspected pump.fun upgrade.

### Not started
- [ ] #5 Tier 2 RugCheck + deployer history
- [ ] #6 Tier 3 curve tracker + credit budget enforcement
- [ ] #7 Features, scorer, alerts
- [ ] #8 Paper trading + labeller
- [ ] #9 Analysis notebook

---

## Verified facts (measured from the live stream, not documentation)

Full evidence in `docs/STREAM_NOTES.md`.

| Fact | Detail |
|---|---|
| Constant product | `k = vSol × vTokens = 3.219e10`, worst residual **1.2e-16** |
| Opening state | vSOL exactly **30.0**, vTokens exactly **1,073,000,000** |
| Graduation | **115 vSOL** ⇒ `progress = (vSol-30)/85`, a ~**14.7x** move |
| Dev pre-buy | `vSol - 30` — free, at t=0, **no RPC call**. Observed 0–11.85 SOL |
| Two venues | stream carries bonk.fun too; branch on `pool`, not mint suffix |
| No timestamp | 0/101 frames; `received_at` is our clock only |
| Instant bonds | create + migrate **24ms apart** — bundled, unbuyable, not organic |
| Launch rate | ~1,500–2,700/hour across both venues |
| `is_mayhem_mode` | **breaks `k` entirely** — different curve mechanism, n=37 perfect split |
| Token program | **Token-2022**, not SPL Token (60/60); metadata extensions only |
| Curve layout | fields end at byte 84; accounts over-allocated to 115 or 151 |
| On-chain `creator` | differs from tx signer on ~10–25% (proxy deployers) |
| **Metadata decay** | **36% of launches were behind a 404 host 3 days later** |
| **Fetch success** | **98.6% near-real-time vs 64% at 3 days old** |
| **Telegram is rare** | present on only **2.0%** of launches (364/18,143) |
| Twitter / website | 73% / 51% present |
| Stream `uri` vs chain | **0 mismatches over ~3,000 mints** — stream uri is reliable |

**The socials distribution is a headline Phase 1 number.** Of 18,143 resolved
documents: 23% have no socials, 29% one, 47% two, and only **1.5% all three**.
Telegram at 2.0% is the striking one — pre-BOOST literature put the Telegram
lift at 8.94x and all-three at 17.4x, and if only 2% of launches carry Telegram
then `metadata.require_telegram: true` is a very harsh but potentially very
high-precision gate. Whether that lift survives BOOST is exactly what #9 must
test; do not assume it.

**Traps already handled** (do not regress these):

- **Never apply curve math to a mayhem token.** `progress_pct`,
  `dev_prebuy_sol` and k-derived price are meaningless there, and `vSol - 30` is
  not a pre-buy when vSol can open below 30. Gate on
  `curve.standard_curve_applies()`; treat NULL as unsafe. This will silently
  corrupt features in #7 if forgotten.
- `migrations / launches` is **not** a graduation rate — different cohorts.
  Only cohort-based, excluding instant bonds.
- A throttled stream looks identical to a quiet market → `ratecheck`.
- Litestream must target a **non-AWS** bucket; the Free plan auto-closes the
  account at 6 months and takes same-account S3 with it.
- **Absent socials are empty strings, not missing keys.** Real documents carry
  `"website": "", "telegram": ""`. Testing key presence scores those as present
  and inverts the signal. Pinned by `test_metadata.py`.
- **A dead metadata host is not always an HTTP error.** `j7tracker` returns 404
  with a 27 KB HTML page; others return 404 with `application/json`. Success
  must mean "parsed to a JSON object", not "did not raise".
- **Reading the URI on-chain does not rescue a dead host.** It makes the URI
  durable, not the document it points at. Easy to overstate; the socials still
  live on somebody else's server.
- **Litestream 0.5.x ignores unrecognized config keys.** 0.3.x-style
  `retention:` under the replica parses cleanly and silently leaves you on the
  24h default. Verified by feeding it `bogus-key-xyz: 42` (exit 0).
- **`PRAGMA integrity_check` returns `ok` on a zero-byte file.** Never gate a
  backup or a pull on it alone; check row counts too.

---

## Immediate next actions

**Collection is running and is the long pole — 2–3 weeks.** Everything below
fits inside that window.

**1. AWS account hygiene — the only unmitigated risk to the data.**
The account auto-closes ~**2027-02-05**. Budgets alerts at $50/$100, and
calendar reminders for **2027-01-05** and **2027-01-26**. Credits are not the
constraint; the calendar is.

**2. Merge or back up the local database.** `data/sniper.db` holds 16,601
launches from the 2026-08-03 residential run and exists on one laptop only. The
instance started fresh. `mint` is the primary key with `INSERT OR IGNORE`, so
merging is safe.

**3. Schedule `pull-data.ps1` daily** via Task Scheduler.

**4. A second `ratecheck` at a different time of day.** The current evidence is
one daytime hour plus a 25.8h average of ~1,400/h — inside the band, but market
volume genuinely swings with the clock.

**5. Then #5 (Tier 2).**

---

## Roadmap

### #4 Tier 1 — metadata enrichment — DONE (2026-08-06)

`enrich/metadata.py` + `token2022.py`, running as `meme-sniper-enrich.service`.
Resolves each launch's document and extracts twitter/telegram/website into
`token_metadata`, promoting per `metadata.min_socials_to_promote`.

**It had to become an always-on worker, not a pre-analysis pass.** The
prediction that third-party hosts "will 404 eventually" was already true when
measured: 36% of three-day-old launches were behind a dead host. Fetch success
is 98.6% near-real-time against 64% at three days. Metadata not fetched close to
launch is not pending, it is lost.

Delivered: IPFS gateway fallback, bounded concurrency, per-run URI cache,
on-chain URI resolution via the Token-2022 TokenMetadata extension, and failed
fetches recorded rather than skipped (so "had no socials" stays distinguishable
from "never looked").

**Still to do here:** the duplicate `(creator, uri)` spam filter. 708 such
groups exist in 16,601 local launches. It is fully derivable in SQL from data
already stored, so it needs no schema change and can be a feature in #7 rather
than a column — but it is not wired into the promotion decision yet.

### #3 Bonding-curve account layout — DONE (2026-08-03)

Decoded in `curve.decode_bonding_curve()` and verified against chain. pump.fun
publishes no program source (only docs + IDL), but Anchor/Borsh is
deterministic, so the layout was derivable rather than reverse-engineered.

Validated against three anchors that do not depend on curve state: `creator` at
offset 49 matching the stream byte-for-byte, `token_total_supply` constant at
1e9, and `k_residual()` ≈ 0. Fields end at 84; accounts are over-allocated to
115 or 151 bytes, so never assert an exact length.

`src/sniper/rpc.py` came out of this and is the RPC client #6 needs.

### #5 Tier 2 — safety + deployer history

RugCheck `api.rugcheck.xyz/v1/tokens/{mint}/report`, **10 req/min
unauthenticated** (60 with key) — a real bottleneck, must sit behind the Tier 1
gate with a rate limiter, never on the firehose. Plus deployer history via
Helius `getSignaturesForAddress`. Serial spam is rampant (one creator: 5
launches / 75 seconds), so these features should be high-value.

### #6 Tier 3 — curve tracker

`getMultipleAccounts` (100 mints/call), polling decay per `tracking.schedule`.
**Budget is enforced, not assumed:** 200 tokens polled every 5s consumes the
entire 1M/month Helius free quota. Cap the watchlist, track spend in
`rpc_usage`, hard-stop at 90%.

### #7 Features + scorer + alerts

Transparent weighted scorecard, **not** an ML model — zero post-BOOST labels
exist, so a fitted model would be fabricated confidence. Weights in
`config.yaml`; persist the full feature vector with every score.

Key features: `trades_to_reach_vsol` at the configured checkpoints (**inverse
signal — the strongest published predictor**), `non_bot_share` (approximate;
record it as such), SOL velocity/acceleration, dev pre-buy (free at t=0),
concentration, bundle signature.

### #8 Paper trading + labeller

Pessimistic fills — optimistic fills are how paper strategies lie. ~1% fee each
way plus slippage and priority fees ≈ 2% round trip. Labeller backfills
1h/24h/7d: max multiple, max drawdown, graduated, rugged, alive.

Target: **≥3x within 1h–7d without first drawing down >50%.** The drawdown
clause matters — a token that dumps 80% then 5x's off the bottom is not a trade
you survive. Also record 2x/5x/10x so the target can be re-cut without
re-collecting.

### #9 Analysis notebook — the actual deliverable

1. Post-BOOST graduation base rate with Wilson CI — holding at 4.7–6.7% or
   reverting toward 0.2%?
2. Does the socials lift reproduce post-BOOST, or did BOOST change who launches?
3. Score-decile lift vs base rate, compared against its CI.
4. Paper P&L net of the 2% cost model.
5. Does `is_mayhem_mode` have its own base rate? If so, condition on it.

**Only if (3) and (4) are positive does Phase 2 — or any discussion of real
capital — make sense.**

---

## Verification

- `pytest tests/ -q` → 71 passing. Fixtures are real captured frames, real
  on-chain account bytes and real fetched metadata documents, so a pump.fun
  parameter change fails the suite rather than silently corrupting features.
- `sniper stats` → counts, pool split, cohort graduation rate (needs >24h).
- `sniper ratecheck --hours 1` → throttling detector.
- `sniper enrich --limit N` → one Tier 1 pass, prints ok/failed/promoted.
  A falling `ok` rate means metadata hosts are dying faster than we fetch.
- `systemctl is-active meme-sniper meme-sniper-enrich litestream` → all three
  must be `active`. A code push must never be the reason one stopped.
- `sniper verify-program --sample 40` → on-chain assumption canary. Run it after
  any suspected pump.fun upgrade; exit 1 means an assumption broke.
- Cross-check a tracked token's polled price against DexScreener for the same
  mint once #6 exists.
- Soak: 24h run staying under the RPC credit quota and RugCheck's 10/min.

**The real verification takes 2–3 weeks of collection.** Nothing else
substitutes for it.
