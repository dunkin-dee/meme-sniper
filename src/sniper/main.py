"""CLI entrypoint.

Phase 1 exposes ``record`` (the Tier 0 firehose) and ``stats``. Later stages add
enrichment and tracking workers to the same asyncio loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time

from . import db
from .config import Config, resolve_path
from .ingest.pumpportal import PumpPortalRecorder
from .logging_setup import setup as setup_logging

log = logging.getLogger("sniper.main")


async def _heartbeat(recorder: PumpPortalRecorder, interval: float) -> None:
    """Periodic status line. The firehose is far too noisy to log per event."""
    while True:
        await asyncio.sleep(interval)
        state = "connected" if recorder.stats.connected_since else "DISCONNECTED"
        log.info("[%s] %s", state, recorder.stats.snapshot())


async def run_recorder(cfg: Config) -> int:
    conn = db.connect(resolve_path(cfg, "database.path"))
    recorder = PumpPortalRecorder(cfg, conn)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _request_stop() -> None:
        if not stopping.is_set():
            log.info("shutdown requested; closing cleanly")
            stopping.set()
            recorder.stop()

    # SIGINT works on Windows; SIGTERM is POSIX-only.
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    hb_interval = float(cfg.get("logging.heartbeat_seconds", 60))
    heartbeat = asyncio.create_task(_heartbeat(recorder, hb_interval))

    db.log_event(conn, "recorder_start")
    log.info("recorder starting; db=%s", resolve_path(cfg, "database.path"))

    try:
        await recorder.run()
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        db.log_event(conn, "recorder_stop", recorder.stats.snapshot())
        log.info("final: %s", recorder.stats.snapshot())
        conn.close()

    return 0


async def run_enrich(cfg: Config, loop: bool, limit: int | None) -> int:
    """Tier 1 metadata enrichment.

    Deliberately a separate process from the recorder rather than another task
    in its loop. The recorder must never die from a downstream fault, and this
    one makes outbound HTTP to arbitrary third-party hosts - the least
    trustworthy dependency in the system.
    """
    from .enrich import metadata

    conn = db.connect(resolve_path(cfg, "database.path"))
    try:
        if loop:
            db.log_event(conn, "enrich_start")
            log.info("metadata enrichment starting (continuous)")
            return await metadata.run_loop(cfg, conn)
        stats = await metadata.run_once(cfg, conn, limit)
        print(stats.snapshot())
        return 0
    finally:
        conn.close()


def show_stats(cfg: Config) -> int:
    conn = db.connect(resolve_path(cfg, "database.path"))
    try:
        c = db.counts(conn)
        print(f"database: {resolve_path(cfg, 'database.path')}")
        for table, n in c.items():
            print(f"  {table:<16} {n:>10,}")

        row = conn.execute(
            "SELECT MIN(received_at) AS lo, MAX(received_at) AS hi FROM launches"
        ).fetchone()
        if row and row["lo"]:
            span_h = (row["hi"] - row["lo"]) / 3600
            print(f"\n  collection window: {span_h:.1f}h")
            if span_h > 0:
                print(f"  launch rate:       {c['launches'] / span_h:,.0f}/hour")

        print("\n  pools (Tier 0 records all; only promote_pools are enriched):")
        for r in conn.execute(
            "SELECT pool, COUNT(*) n FROM launches GROUP BY pool ORDER BY n DESC"
        ):
            print(f"    {r['pool'] or 'unknown':<10} {r['n']:>10,}")

        # Graduation rate is THE number Phase 1 exists to measure, and it is
        # easy to compute wrongly. migrations/launches is NOT it: most
        # migrations we see belong to tokens launched before we started, so the
        # numerator and denominator describe different cohorts.
        #
        # The only honest figure is cohort-based: of tokens we recorded at
        # launch AND have watched for at least one full horizon, what fraction
        # migrated? Instant-bond bundles are excluded - they are not organic
        # graduations and we could never have bought them.
        row = conn.execute(
            """
            SELECT COUNT(*) AS cohort,
                   SUM(CASE WHEN m.mint IS NOT NULL AND COALESCE(m.instant_bond,0)=0
                            THEN 1 ELSE 0 END) AS graduated,
                   SUM(CASE WHEN COALESCE(m.instant_bond,0)=1 THEN 1 ELSE 0 END) AS instant
            FROM launches l
            LEFT JOIN migrations m ON m.mint = l.mint
            WHERE l.pool = 'pump' AND l.received_at <= ?
            """,
            (time.time() - 24 * 3600,),
        ).fetchone()

        print("\n  graduation rate (pump.fun, cohort-based):")
        if row and row["cohort"]:
            n, g = row["cohort"], row["graduated"] or 0
            print(f"    matured cohort (launched >24h ago): {n:,}")
            print(f"    organic graduations:                {g:,}  ({g / n * 100:.3f}%)")
            print(f"    instant-bond bundles (excluded):    {row['instant'] or 0:,}")
        else:
            print("    not enough history yet - needs launches older than 24h.")
            print("    (migrations/launches would be meaningless here: different cohorts)")

        inst = conn.execute(
            "SELECT COUNT(*) n FROM migrations WHERE instant_bond = 1"
        ).fetchone()["n"]
        if inst:
            print(f"\n  instant-bond migrations seen overall: {inst:,}")

        recent = conn.execute(
            "SELECT at, kind, detail FROM events ORDER BY at DESC LIMIT 10"
        ).fetchall()
        if recent:
            print("\n  recent events:")
            for r in recent:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["at"]))
                detail = f" {r['detail'][:80]}" if r["detail"] else ""
                print(f"    {ts} {r['kind']}{detail}")
    finally:
        conn.close()
    return 0


# Launch rate measured from a residential connection on 2026-08-02 across two
# runs. A datacenter IP that is being throttled should fall well below this.
RESIDENTIAL_BASELINE_PER_HOUR = (1500, 2700)


def rate_check(cfg: Config, window_hours: float) -> int:
    """Compare the recent observed launch rate against the residential baseline.

    PumpPortal bans on repeated reconnects, and datacenter IP ranges are often
    treated more aggressively than residential by API providers. This is the
    cheap, decisive test for whether the host is being silently throttled -
    silently, because a rate-limited stream looks exactly like a quiet market.
    """
    conn = db.connect(resolve_path(cfg, "database.path"))
    try:
        since = time.time() - window_hours * 3600
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(received_at) lo, MAX(received_at) hi "
            "FROM launches WHERE received_at >= ?",
            (since,),
        ).fetchone()

        if not row or not row["n"] or row["lo"] is None:
            print(f"No launches recorded in the last {window_hours:g}h.")
            print("Either the recorder is not running, or the stream is blocked.")
            return 1

        span_h = max((row["hi"] - row["lo"]) / 3600, 1e-9)
        rate = row["n"] / span_h
        lo, hi = RESIDENTIAL_BASELINE_PER_HOUR

        print(f"window observed:  {span_h:.2f}h ({row['n']:,} launches)")
        print(f"observed rate:    {rate:,.0f}/hour")
        print(f"residential base: {lo:,}-{hi:,}/hour")

        reconnects = conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind IN "
            "('ws_disconnected','ws_error') AND at >= ?",
            (since,),
        ).fetchone()["n"]
        print(f"reconnects:       {reconnects}")

        if span_h < 0.5:
            print("\nINCONCLUSIVE - needs at least ~30 minutes of continuous data.")
            return 2

        if rate < lo * 0.5:
            print("\nTHROTTLED (likely). Rate is under half the residential floor.")
            print("This IP is probably being rate-limited. Options: move to another")
            print("provider/region, or run the recorder somewhere residential.")
            return 1
        if rate < lo:
            print("\nMARGINAL. Below the residential floor, but market volume also")
            print("varies by time of day. Re-run over a longer window before acting.")
            return 2

        print("\nOK - rate is consistent with an unthrottled connection.")
        if reconnects > span_h * 4:
            print("NOTE: reconnects are frequent; check for connectivity issues.")
        return 0
    finally:
        conn.close()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sniper", description=__doc__)
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("record", help="run the Tier 0 PumpPortal recorder")
    sub.add_parser("stats", help="print collection statistics")
    rc = sub.add_parser(
        "ratecheck",
        help="check the observed launch rate against the residential baseline "
             "(detects datacenter-IP throttling)",
    )
    rc.add_argument("--hours", type=float, default=1.0, help="window to measure")
    en = sub.add_parser(
        "enrich",
        help="Tier 1: resolve each launch's metadata document and extract socials. "
             "Time-critical - metadata hosts 404 within days.",
    )
    en.add_argument("--loop", action="store_true", help="run continuously")
    en.add_argument("--limit", type=int, default=None, help="launches in this pass")
    vp = sub.add_parser(
        "verify-program",
        help="verify pump.fun program assumptions against chain (mint/freeze "
             "authority, token program, bonding-curve layout)",
    )
    vp.add_argument("--sample", type=int, default=None, help="mints to check")

    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    setup_logging(cfg.get("logging.level", "INFO"))

    if args.command == "record":
        try:
            return asyncio.run(run_recorder(cfg))
        except KeyboardInterrupt:
            return 0
    if args.command == "stats":
        return show_stats(cfg)
    if args.command == "ratecheck":
        return rate_check(cfg, args.hours)
    if args.command == "enrich":
        try:
            return asyncio.run(run_enrich(cfg, args.loop, args.limit))
        except KeyboardInterrupt:
            return 0
    if args.command == "verify-program":
        from .verify import verify_program

        return verify_program(cfg, args.sample)
    return 1


if __name__ == "__main__":
    sys.exit(cli())
