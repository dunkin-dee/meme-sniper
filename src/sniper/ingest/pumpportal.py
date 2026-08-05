"""Tier 0: PumpPortal WebSocket firehose.

Only the free subscriptions are used:

* ``subscribeNewToken``  - every pump.fun launch
* ``subscribeMigration`` - graduation events

``subscribeTokenTrade`` / ``subscribeAccountTrade`` are METERED (0.01 SOL per
10k events plus a funded wallet), so they are deliberately not used; bonding-curve
polling in ``track.curve`` covers price instead.

Two rules from PumpPortal's docs are load-bearing here:

1. Use ONE connection and multiplex all subscriptions over it. Opening a socket
   per token is what gets you banned.
2. Repeated reconnect attempts trigger a ban that expires hourly - hence
   exponential backoff with jitter rather than a tight retry loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from .. import db
from ..config import Config
from collections import Counter

log = logging.getLogger("sniper.ingest")

# Payloads that are neither launches nor migrations (acks, errors) are counted
# and sampled into the events table rather than logged individually.
LaunchHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RecorderStats:
    launches: int = 0
    duplicates: int = 0
    migrations: int = 0
    instant_bonds: int = 0
    skipped_pool: int = 0
    unknown: int = 0
    reconnects: int = 0
    connected_since: float | None = None
    last_event_at: float | None = None
    started_at: float = field(default_factory=time.time)
    by_pool: Counter = field(default_factory=Counter)

    def snapshot(self) -> str:
        uptime = time.time() - self.started_at
        rate = self.launches / uptime * 3600 if uptime > 0 else 0.0
        silent = (
            f", silent={time.time() - self.last_event_at:.0f}s"
            if self.last_event_at
            else ""
        )
        pools = " ".join(f"{p}={n}" for p, n in self.by_pool.most_common(4))
        return (
            f"launches={self.launches} (~{rate:.0f}/h) dupes={self.duplicates} "
            f"migrations={self.migrations} (instant_bond={self.instant_bonds}) "
            f"unknown={self.unknown} reconnects={self.reconnects}"
            f"{' | ' + pools if pools else ''}{silent}"
        )


class PumpPortalRecorder:
    """Single-connection recorder for the free PumpPortal streams."""

    def __init__(
        self,
        cfg: Config,
        conn: sqlite3.Connection,
        on_launch: LaunchHandler | None = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.on_launch = on_launch
        self.stats = RecorderStats()

        self.url: str = cfg.get("pumpportal.url")
        self.subscriptions: list[str] = list(cfg.get("pumpportal.subscriptions"))
        self._backoff_initial: float = cfg.get("pumpportal.reconnect.initial_seconds", 1.0)
        self._backoff_max: float = cfg.get("pumpportal.reconnect.max_seconds", 300.0)
        self._backoff_mult: float = cfg.get("pumpportal.reconnect.multiplier", 2.0)
        self._backoff_jitter: float = cfg.get("pumpportal.reconnect.jitter", 0.25)

        # subscribeNewToken carries BOTH pump.fun and bonk.fun launches, which
        # have different payload shapes. Everything is recorded at Tier 0 (it is
        # free, and it makes a future LetsBonk expansion a config change), but
        # only pools in this list are promoted to the enrichment tiers.
        self.promote_pools: set[str] = set(cfg.get("pumpportal.promote_pools", ["pump"]))

        # Frame counters let analysis distinguish replayed/backfilled frames
        # right after a connect from genuinely live ones.
        self._frame_seq = 0
        self._conn_epoch = 0

        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Connect and stream until stopped, reconnecting with backoff."""
        backoff = self._backoff_initial

        while not self._stop.is_set():
            try:
                log.info("connecting to %s", _redact(self.url))
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2**22,
                ) as ws:
                    self.stats.connected_since = time.time()
                    backoff = self._backoff_initial  # reset only after a real connect
                    self._conn_epoch += 1
                    self._frame_seq = 0
                    await self._subscribe(ws)
                    db.log_event(self.conn, "ws_connected", _redact(self.url))
                    log.info("connected; subscriptions=%s", ",".join(self.subscriptions))
                    await self._consume(ws)

            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, websockets.InvalidHandshake) as exc:
                self.stats.reconnects += 1
                self.stats.connected_since = None
                detail = f"{type(exc).__name__}: {exc}"
                db.log_event(self.conn, "ws_disconnected", detail)
                log.warning("connection lost (%s); reconnecting in %.1fs", detail, backoff)
            except Exception as exc:  # noqa: BLE001 - recorder must not die
                self.stats.reconnects += 1
                self.stats.connected_since = None
                detail = f"{type(exc).__name__}: {exc}"
                db.log_event(self.conn, "ws_error", detail)
                log.exception("unexpected recorder error; reconnecting in %.1fs", backoff)

            if self._stop.is_set():
                break

            await self._sleep_with_jitter(backoff)
            backoff = min(backoff * self._backoff_mult, self._backoff_max)

    async def _sleep_with_jitter(self, base: float) -> None:
        jitter = base * self._backoff_jitter
        delay = max(0.0, base + random.uniform(-jitter, jitter))
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _subscribe(self, ws) -> None:
        # All subscriptions go over this one socket - never a socket per token.
        for method in self.subscriptions:
            await ws.send(json.dumps({"method": method}))
            log.debug("sent subscription %s", method)

    async def _consume(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            self.stats.last_event_at = time.time()
            self._frame_seq += 1
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                self.stats.unknown += 1
                continue
            if isinstance(payload, dict):
                await self._dispatch(payload)

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        kind = classify(payload)

        if kind == "launch":
            is_new = db.record_launch(
                self.conn, payload, self._frame_seq, self._conn_epoch
            )
            if is_new:
                self.stats.launches += 1
                pool = payload.get("pool") or "unknown"
                self.stats.by_pool[pool] += 1

                if pool not in self.promote_pools:
                    # Recorded, but not eligible for enrichment - out of scope.
                    self.stats.skipped_pool += 1
                    db.set_tier(self.conn, payload["mint"], 0, f"pool={pool}")
                elif self.on_launch is not None:
                    # Never let a downstream handler kill the recorder; the
                    # firehose is the one thing that must stay up.
                    try:
                        await self.on_launch(payload)
                    except Exception:  # noqa: BLE001
                        log.exception("on_launch handler failed for %s", payload.get("mint"))
            else:
                self.stats.duplicates += 1

        elif kind == "migration":
            if db.record_migration(
                self.conn, payload, self._frame_seq, self._conn_epoch
            ):
                self.stats.migrations += 1
                row = self.conn.execute(
                    "SELECT seconds_since_launch, instant_bond FROM migrations WHERE mint = ?",
                    (payload.get("mint"),),
                ).fetchone()
                if row and row["instant_bond"]:
                    # create + full-curve buy in one bundle: unbuyable, and a
                    # false positive for any "graduated fast" feature.
                    self.stats.instant_bonds += 1
                    log.info(
                        "MIGRATION %s [INSTANT-BOND %.2fs - bundled, not organic]",
                        payload.get("mint"),
                        row["seconds_since_launch"],
                    )
                else:
                    age = (
                        f" ({row['seconds_since_launch'] / 60:.1f}m after launch)"
                        if row and row["seconds_since_launch"] is not None
                        else " (launched before we connected)"
                    )
                    log.info("MIGRATION %s%s", payload.get("mint"), age)

        else:
            self.stats.unknown += 1
            # Subscription acks and error messages arrive here. Sample them so
            # a silent protocol change is visible in the events table.
            if self.stats.unknown <= 20 or self.stats.unknown % 500 == 0:
                db.log_event(
                    self.conn,
                    "ws_unclassified",
                    json.dumps(payload, separators=(",", ":"))[:500],
                )


def classify(payload: dict[str, Any]) -> str:
    """Classify a PumpPortal frame.

    Field names are matched defensively: the stream's shape has changed before
    and a schema drift should degrade to 'unknown' (logged) rather than silently
    dropping launches.
    """
    tx_type = str(payload.get("txType") or payload.get("type") or "").lower()

    if tx_type in {"create", "creation", "newtoken"}:
        return "launch"
    if tx_type in {"migrate", "migration", "complete"}:
        return "migration"

    # Fallbacks for frames that omit txType.
    if payload.get("mint") and payload.get("uri") and payload.get("symbol"):
        return "launch"
    if payload.get("mint") and payload.get("pool") and "migration" in json.dumps(payload).lower():
        return "migration"

    return "unknown"


def _redact(url: str) -> str:
    """Strip any api-key query parameter before logging a URL."""
    if "api-key=" not in url:
        return url
    head, _, tail = url.partition("api-key=")
    rest = tail.split("&", 1)
    remainder = "&" + rest[1] if len(rest) > 1 else ""
    return f"{head}api-key=***{remainder}"
