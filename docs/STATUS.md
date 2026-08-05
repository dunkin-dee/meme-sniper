# meme-sniper — Status & Roadmap

**Project:** `C:\Users\dimad\projects\meme-sniper`
**Last updated:** 2026-08-03
**Phase 1 of 2.** Phase 1 = measure the regime. Phase 2 = fit a model, only if
Phase 1's numbers justify it.

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

**Working and live-validated.** 54 tests pass. Recorder connects to the live
firehose and records ~2,100 launches/hour. `verify-program` confirms the
on-chain assumptions hold (exit 0).

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
  main.py                    CLI: record | stats | ratecheck | verify-program
  rpc.py                     rate-limited Solana JSON-RPC (seed for #6)
  verify.py                  on-chain assumption canary
  ingest/pumpportal.py       Tier 0 firehose, single connection + backoff
tests/test_curve.py          20 tests, fixtures = real captured frames
tests/test_ingest.py         20 tests, payloads = real captured frames
tests/test_account_decode.py 14 tests, fixtures = real account bytes from chain
deploy/                      systemd units, litestream.yml, bootstrap.sh, pull-data.ps1
data/sniper.db               54 launches (smoke-test data only)
```

### Done

- [x] **Scaffold** — config loader, SQLite schema v2 with migrations, logging
- [x] **Tier 0 recorder** — validated against the live stream over two runs
- [x] **Verified curve math** — `curve.py`, pinned by tests to real frames
- [x] **EC2 deployment** — systemd, Litestream, pull script, runbook
- [x] **#3 Bonding-curve account layout** — decoded and verified against chain
      (`sniper verify-program`). Layout in `curve.py`, RPC client in `rpc.py`.
- [x] **Program assumption canary** — mint/freeze authority, token program,
      Token-2022 extensions, curve decode. Exit 0/1/2, re-runnable after any
      suspected pump.fun upgrade.

### Not started
- [ ] #4 Tier 1 metadata enrichment (socials gate)
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

---

## Immediate next actions

**1. Start collecting — this gates everything.**
The data cannot be backfilled and BOOST is only weeks old. Either run locally:

```powershell
.venv\Scripts\python.exe -m sniper.main record
```

or deploy per `docs/DEPLOY.md` (t4g.micro, ~$8.50/mo, ~$51 of the $200 credits
over 6 months).

**2. If deploying, run the throttling check after 1 hour.**

```bash
sudo -u sniper /opt/meme-sniper/.venv/bin/python -m sniper.main ratecheck --hours 1
```

Unresolved risk: whether PumpPortal throttles AWS IP ranges. No evidence either
way was found — it must be measured. Exit 1 = likely throttled.

**3. Day-one AWS hygiene** (from `docs/DEPLOY.md`): calendar reminders at month
5, Budgets alerts at $50/$100, confirm the Litestream bucket is outside AWS,
and do one verified `pull-data.ps1`.

---

## Roadmap

### #4 Tier 1 — metadata enrichment (do this next; highest value per effort)

`src/sniper/enrich/metadata.py`. Resolve `launches.uri` → extract
twitter/telegram/website into `token_metadata`.

The strongest published signal, and free: across 832,941 launches, Telegram
present graduated at 1.485% vs 0.166% (**8.94x**, Cox HR 5.40); all three
socials 1.919% vs 0.110% (**17.4x**).

- **Check the mint account first.** Token-2022's TokenMetadata extension stores
  name, symbol and URI **on-chain** (verified 60/60). That is free, permanent,
  batchable at 100 mints per `getMultipleAccounts`, and immune to the IPFS/host
  decay that will eventually rot `launches.uri`. The socials still live in the
  JSON at that URI, so the HTTP fetch is still needed — but the URI itself
  should come from chain, and this is time-sensitive: third-party metadata hosts
  (`meta.uxento.io`, `metadata.j7tracker.io`) will 404 eventually.
- IPFS gateway fallbacks + `httpx` with timeout; observed URIs include
  `metadata.j7tracker.io` and `ipfs.io` — handle both plain HTTPS and IPFS.
- Bounded concurrency (`metadata.max_concurrent`), cache by URI.
- Promote to Tier 2 per `metadata.min_socials_to_promote`.
- **Free spam filter:** duplicate `(creator, uri)` pairs were observed launching
  seconds apart — cheap and effective.

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

- `pytest tests/ -q` → 54 passing. Fixtures are real captured frames and real
  on-chain account bytes, so a pump.fun parameter change fails the suite rather
  than silently corrupting features.
- `sniper stats` → counts, pool split, cohort graduation rate (needs >24h).
- `sniper ratecheck --hours 1` → throttling detector.
- `sniper verify-program --sample 40` → on-chain assumption canary. Run it after
  any suspected pump.fun upgrade; exit 1 means an assumption broke.
- Cross-check a tracked token's polled price against DexScreener for the same
  mint once #6 exists.
- Soak: 24h run staying under the RPC credit quota and RugCheck's 10/min.

**The real verification takes 2–3 weeks of collection.** Nothing else
substitutes for it.
