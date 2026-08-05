# meme-sniper

Launch recorder and signal-research platform for pump.fun.

**What this is:** a measurement instrument. It records every launch, enriches the
survivors, scores them, and paper-trades the result so the edge can be measured
in SOL before any real money is involved.

**What this is not:** a profitable bot. Phase 1 deliberately produces a *number*,
not a strategy. See "Honest framing" below.

## Why record first

Pump.fun shipped **BOOST on 2026-07-21**, reinjecting ~17.6 SOL of previously
dead liquidity as post-migration buybacks and burns. Graduation rates moved from
~0.2% to 4.7–6.7% — roughly 8x. Every published threshold and model predates
this and is calibrated to a mechanism that no longer exists.

So the first job is to measure the current regime, not to inherit someone else's
stale constants.

## Quick start

```bash
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,analysis]"

.venv\Scripts\python.exe -m sniper.main record   # start the recorder
.venv\Scripts\python.exe -m sniper.main stats    # collection summary
```

The recorder needs no API key — PumpPortal's `subscribeNewToken` and
`subscribeMigration` are free. Leave it running; **its value compounds with
wall-clock time.**

For 24/7 collection on a server, see **[docs/DEPLOY.md](docs/DEPLOY.md)** —
systemd units, Litestream replication, and the AWS Free-plan 6-month
auto-close trap.

Optional environment variables:

| var | used for |
|---|---|
| `HELIUS_API_KEY` | curve polling (free tier: 1M credits/mo, 10 RPS) |
| `RUGCHECK_API_KEY` | raises RugCheck from 10 to 60 req/min |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alert delivery |

## Design

A four-tier funnel. Free-tier quotas are the binding constraint, so budget is
spent only on survivors.

| Tier | Population | Cost | Action |
|---|---|---|---|
| 0 Firehose | every launch (~36–65k/day) | free | record raw, no filtering |
| 1 Metadata | ~5–15% | free | resolve URI → socials |
| 2 Enrichment | ~1–3% | RugCheck + RPC | deployer history, concentration |
| 3 Tracked | ~0.1% | RPC polling | curve velocity, score, alert, paper-buy |

Tier 1 carries the strongest published signal: across 832,941 launches, tokens
advertising Telegram graduated at 1.485% vs 0.166% (**8.94x**), and all three
social channels at 1.919% vs 0.110% (**17.4x**). It costs one HTTP GET.

**Where the edge has to come from.** Bundle snipers own blocks 1–2 and we will
never beat them on a free tier. The strongest predictor in the literature is
`trades_to_reach_vsol` — reaching a given SOL level in *fewer* trades, i.e.
large-ticket conviction rather than bot micro-churn. That is a selection edge on
a 30s–5min horizon, which is a race we can actually enter.

## Verified curve math

Measured against live frames, not documentation (see `docs/STREAM_NOTES.md`):

- Virtual SOL opens at exactly **30.0**, virtual tokens at **1,073,000,000**.
- `k = vSol × vTokens = 3.219e10`, invariant across all launches (worst residual
  **1.2e-16**).
- Create frames report **post-pre-buy** state, so `vSol - 30` is the dev pre-buy
  — a headline rug signal available at t=0 with **zero RPC calls**.
- Graduation at 115 vSOL ⇒ `progress = (vSol - 30) / 85`, a ~14.7x price move.

`tests/test_curve.py` pins these against real captured frames, so a pump.fun
parameter change fails the suite rather than silently corrupting features.

## Traps this codebase already accounts for

- **Two venues on one stream.** `subscribeNewToken` carries bonk.fun as well as
  pump.fun, with a different payload shape. Branch on `pool`, never mint suffix.
- **Instant-bond bundles.** Observed a token whose create and migrate frames were
  **24ms apart** — create plus full-curve buy in one Jito bundle. Unbuyable, not
  organic, and it teaches a naive model that "graduates fast" is good. Flagged
  and excluded.
- **Cohort mismatch.** `migrations / launches` is not a graduation rate; most
  migrations we see belong to tokens launched before we connected. Only
  cohort-based rates are reported.
- **No timestamp in the stream.** `received_at` is our clock. Real timing
  features must resolve on-chain block time from the signature.

## Honest framing

Phase 1 produces a labelled post-BOOST dataset, a measured hit rate with
confidence intervals, and a paper-trading ledger.

**Kill criterion:** if after three weeks of paper trading the median outcome is
negative net of a realistic 2% round-trip cost model, the honest conclusion is
that no edge exists at this latency tier, and we stop or change approach. This is
written down in advance so success is not graded on vibes.

Base rates for context: ~0.2% of pump.fun tokens graduated pre-BOOST, and one
study found 98.6% of launches show pump-and-dump signatures.

## Layout

```
config.yaml              all thresholds, budgets, weights - no magic numbers in code
src/sniper/
  curve.py               verified bonding-curve math
  db.py                  SQLite schema + migrations
  ingest/pumpportal.py   Tier 0 firehose
  main.py                CLI
docs/STREAM_NOTES.md     empirical findings from the live stream
tests/                   fixtures are real captured frames
```

## Status

- [x] Scaffold, config, schema
- [x] Tier 0 recorder (live-validated)
- [x] Verified curve math
- [x] EC2 deployment: systemd, Litestream, pull script, runbook
- [ ] Tier 1 metadata enrichment
- [ ] Tier 2 RugCheck + deployer history
- [ ] Tier 3 curve tracker with credit budgeting
- [ ] Features, scorer, alerts
- [ ] Paper trading + labeller
- [ ] Analysis notebook
