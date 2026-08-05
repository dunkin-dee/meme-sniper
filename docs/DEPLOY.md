# Deploying the recorder on AWS EC2

The recorder's value compounds with wall-clock time and **its data cannot be
backfilled** — a gap is permanent. Everything here is designed around that one
fact.

---

## ⚠️ Read this first: the 6-month cliff

You are on the post-July-2025 AWS **Free plan**. AWS's own wording:

> "The account closes on its own 6 months after you open it or when your credits
> run out, whichever comes first."
>
> "When a free plan expires, the account will close automatically and access to
> current resources and data will be lost."

Three consequences that shape this entire setup:

**1. A same-account S3 bucket is not a backup.** If the account closes, it takes
the EC2 instance *and* any S3 bucket in that account with it, simultaneously. A
backup that dies with the thing it backs up is not a backup. This is why
`litestream.yml` targets **Backblaze B2 or Cloudflare R2**, not S3.

**2. AWS will not upgrade you automatically.** You must convert to the Paid plan
manually before the cliff. Nothing warns you at the moment of closure.

**3. There is a 90-day grace window.** AWS retains data for 90 days after
expiry, so a missed cliff is recoverable — but only if you notice.

**Do this on day one:**

- [ ] Calendar reminder at **month 5** titled "meme-sniper: convert AWS to Paid plan or migrate"
- [ ] Second reminder at **month 5 week 3**
- [ ] AWS Budgets alert at $50 and $100 of credit spend
- [ ] Confirm Litestream's bucket is **not** in this AWS account
- [ ] Run one `pull-data.ps1` and confirm the local copy opens

Credits are not the binding constraint — see the burn estimate below. **The
calendar is.**

---

## Instance sizing

The recorder is an idle WebSocket client writing small rows to SQLite. It is
close to free in CPU terms.

| | vCPU | RAM | ~$/mo | verdict |
|---|---|---|---|---|
| t4g.nano | 2 (burst) | 0.5 GB | ~$3.07 | works, tight for the analysis notebook |
| t4g.micro | 2 (burst) | 1 GB | ~$6.13 | cheaper, but see below |
| **t3.micro** | 2 (burst) | 1 GB | **~$7.59** | **what we run** — x86 |

**We run t3.micro.** On merit `t4g` (ARM/Graviton) is the better buy — cheaper,
and every dependency here is pure Python — but on this account's Free plan the
`t4g` family is **not free-tier eligible** and `t3.micro` is (checked
2026-08-05). The free 750 h/month covers one instance running continuously, so
the nominally more expensive type is the cheaper one here.

Consequence for the AMI: **Ubuntu 24.04 LTS, 64-bit x86 — not ARM64.** Selecting
the Arm AMI silently removes `t3.micro` from the instance-type list.

Leave the credit specification at the `unlimited` default. The recorder is an
idle WebSocket client and never exhausts CPU credits, so no surcharge arises in
practice.

**Burn estimate (t3.micro):**

| item | $/mo | if free tier applies |
|---|---|---|
| t3.micro, 730 h | 7.59 | 0.00 |
| 30 GB gp3 EBS | 2.40 | 2.40 |
| egress (100 GB/mo free) | ~0 | ~0 |
| **total** | **~$9.99** | **~$2.40** |

Over 6 months that is **~$60 of your $200** worst case, ~$14 if the 750 h holds.
Either way you hit the 6-month calendar cliff with most credits unspent —
**the calendar is the binding constraint, not the money.** Confirm which applies
under Billing → Free tier after the first full day of running.

**Storage:** ~620 bytes of raw JSON per launch; with parsed columns and indexes
budget **~1–1.5 KB/launch** ⇒ **~1.5–2.3 GB/month**. A 30 GB volume holds well
over a year.

---

## Setup

### 1. Launch the instance

- Ubuntu 24.04 LTS (**64-bit x86**), **t3.micro** — set the architecture before
  the instance type, or `t3.micro` will not appear in the list
- 30 GB gp3 root volume
- Security group: **inbound SSH from your IP only**. No inbound ports are
  needed for the recorder — it makes only outbound connections.

### 2. Create the offsite bucket (NOT in AWS)

Backblaze B2 free tier (10 GB) comfortably covers this. Create a bucket and an
application key scoped to it.

### 3. Deploy

```powershell
# from your laptop, in the project root
.\deploy\push-code.ps1 -RemoteHost <host> -KeyFile ~\.ssh\sniper.pem
```

This packs a tarball, unpacks it to a staging dir on the instance, and runs
`bootstrap.sh` with `REPO_SRC` pointed at it.

**Do not copy the project directory wholesale.** `data/` holds the live SQLite
database — copied while the recorder is writing, you get a torn file with a
detached WAL, and on the remote it would shadow whatever that instance had
already collected. `.venv/` is a *Windows* virtualenv and collides with the one
`bootstrap.sh` builds. `push-code.ps1` excludes both; a bare `scp -r .` does
not.

The same command is the update path later — `bootstrap.sh` is idempotent and
its rsync leaves `data/` untouched.

### 4. Configure replication

```bash
sudo nano /etc/meme-sniper/litestream.yml    # bucket, endpoint, region
sudo nano /etc/meme-sniper/litestream.env    # key id + secret
sudo systemctl enable --now litestream
journalctl -u litestream -n 30
```

### 5. Verify

```bash
journalctl -u meme-sniper -f
sniper stats
```

---

## ⚠️ Run the throttling check after 1 hour

PumpPortal bans on repeated reconnects, and datacenter IP ranges are frequently
treated more aggressively than residential. **I found no evidence either way for
PumpPortal specifically, so this must be measured, not assumed.**

This matters because a throttled stream is invisible — it looks exactly like a
quiet market.

```bash
sniper ratecheck --hours 1
```

Compares against the **1,500–2,700 launches/hour** measured from a residential
connection on 2026-08-02.

| exit | meaning |
|---|---|
| 0 | consistent with an unthrottled connection |
| 2 | inconclusive or marginal — re-run over a longer window |
| 1 | under half the residential floor: **likely throttled** |

If throttled, try another region or provider before building anything on top of
the data. Re-run at a couple of different times of day, since genuine market
volume swings with the clock.

---

## Pulling data to your laptop

```powershell
.\deploy\pull-data.ps1 -RemoteHost <host> -KeyFile ~\.ssh\key.pem
```

Never `scp` the live database directly — a WAL-mode SQLite file copied while it
is being written gives you a torn file with a detached WAL. The script runs
`.backup` remotely first for an atomic, transactionally consistent snapshot,
verifies `PRAGMA integrity_check` locally, and only then promotes it to
`data/pulls/sniper-latest.db`. A failed check never overwrites the last good
copy.

Schedule it daily with Task Scheduler.

---

## Restoring from Litestream

```bash
litestream restore -config /etc/meme-sniper/litestream.yml \
  -o /tmp/restored.db /opt/meme-sniper/data/sniper.db
sqlite3 /tmp/restored.db "PRAGMA integrity_check; SELECT COUNT(*) FROM launches;"
```

**Test this once, early.** An untested backup is a hypothesis.

---

## Operations

```bash
systemctl status meme-sniper litestream
journalctl -u meme-sniper -f
journalctl -u meme-sniper --since "1 hour ago" | grep -Ei "error|disconnect"
sudo systemctl restart meme-sniper
```

Both units use `Restart=always` with `StartLimitIntervalSec=0`, so systemd can
never give up and leave the recorder stopped overnight.

**Health signals**

| symptom | meaning |
|---|---|
| `silent=NNNs` climbing in the heartbeat | stream stalled without closing |
| reconnects climbing steadily | possible ban or connectivity fault |
| launches flat but process alive | throttled — run `ratecheck` |
| `ws_unclassified` spike | PumpPortal changed its payload shape |

That last one is worth watching: a schema change would otherwise show up as
launches silently vanishing.

---

## Migration checklist (month 5)

Either convert to the Paid plan, or move off AWS:

1. `pull-data.ps1` — take a verified local snapshot
2. Confirm Litestream's offsite replica is current
3. Provision the new host, run `bootstrap.sh`
4. `litestream restore` onto the new host, or copy the snapshot into place
5. Start the recorder, confirm `stats` shows the expected row count
6. Run `ratecheck` on the new host
7. Only then tear down the old instance

Overlap the two for an hour; duplicate launches are harmless (`INSERT OR
IGNORE` on the mint primary key), whereas a gap is permanent.
