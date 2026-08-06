"""Tier 1 - resolve each launch's metadata document and extract its socials.

Why this tier exists: socials are the strongest free published signal. Across
832,941 pre-BOOST launches, Telegram present graduated at 1.485% vs 0.166%
(8.94x); all three socials 1.919% vs 0.110% (17.4x). Whether that lift survives
BOOST is one of the questions the Phase 1 notebook exists to answer, and it
cannot be answered from data we failed to collect.

**This is a race, not a batch job.** The socials live in a JSON document on a
third-party host, and those hosts die fast. Measured 2026-08-06 against
launches collected on 2026-08-03 - three days old - 5,784 of 16,197 (36%)
already pointed at a host returning 404, `metadata.j7tracker.io` alone
accounting for 5,181 of them. Metadata not fetched close to launch is not
"pending", it is lost. Hence a dedicated always-on worker rather than an
enrichment pass run before analysis.

Two consequences for the parsing, both learned from real documents:

* **Absent socials are empty strings, not missing keys.** Real payloads carry
  ``"website": "", "telegram": ""``. Testing key presence would score those as
  present and quietly invert the signal being measured.
* **A dead host is not always an HTTP error.** j7tracker returns 404 with a
  27 KB HTML page; others return 404 with ``application/json``. Success has to
  mean "parsed to a JSON object", not "did not raise".
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import db, token2022
from ..config import Config
from ..rpc import RpcError, SolanaRpc

log = logging.getLogger("sniper.enrich.metadata")

# Keys seen in real pump.fun/bonk.fun metadata documents. Matched
# case-insensitively; documents in the wild use both `createdOn` and
# `created_on`, so assuming one casing drops rows.
_TWITTER_KEYS = ("twitter", "x", "twitter_url", "twitterurl")
_TELEGRAM_KEYS = ("telegram", "tg", "telegram_url", "telegramurl")
_WEBSITE_KEYS = ("website", "web", "site", "website_url", "websiteurl")


@dataclass(frozen=True)
class Socials:
    twitter: str | None
    telegram: str | None
    website: str | None
    description: str | None
    image: str | None

    @property
    def count(self) -> int:
        return sum(1 for v in (self.twitter, self.telegram, self.website) if v)


@dataclass
class EnrichStats:
    considered: int = 0
    fetched_ok: int = 0
    fetch_failed: int = 0
    promoted: int = 0
    cache_hits: int = 0
    onchain_resolved: int = 0
    uri_mismatch: int = 0

    def snapshot(self) -> str:
        return (
            f"considered={self.considered} ok={self.fetched_ok} "
            f"failed={self.fetch_failed} promoted={self.promoted} "
            f"cached={self.cache_hits} onchain={self.onchain_resolved} "
            f"uri_mismatch={self.uri_mismatch}"
        )


def _clean(value: Any) -> str | None:
    """Normalise a metadata field to a non-empty string, or None.

    Empty and whitespace-only strings are the documented way these payloads say
    "absent", so they must collapse to None.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first(doc: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Case-insensitive lookup across candidate key spellings."""
    lowered = {k.lower(): v for k, v in doc.items() if isinstance(k, str)}
    for key in keys:
        found = _clean(lowered.get(key))
        if found:
            return found
    return None


def extract_socials(doc: dict[str, Any]) -> Socials:
    """Pull socials out of a metadata document.

    Some launchpads nest them under ``extensions`` (the Metaplex convention),
    so that sub-object is merged in - shallowly, and without letting it
    override a top-level value that is already present.
    """
    merged: dict[str, Any] = {}
    extensions = doc.get("extensions")
    if isinstance(extensions, dict):
        merged.update(extensions)
    merged.update(doc)

    return Socials(
        twitter=_first(merged, _TWITTER_KEYS),
        telegram=_first(merged, _TELEGRAM_KEYS),
        website=_first(merged, _WEBSITE_KEYS),
        description=_clean(merged.get("description")),
        image=_clean(merged.get("image")),
    )


def ipfs_candidates(uri: str, gateways: list[str]) -> list[str]:
    """Alternative URLs for an IPFS-backed document.

    A gateway being down is a transport problem, not a dead document - the CID
    is content-addressed and any gateway serves it. A plain HTTPS host that
    404s has no alternative, so this returns just the original for those.
    """
    cid: str | None = None
    if uri.startswith("ipfs://"):
        cid = uri[len("ipfs://") :].lstrip("/")
    elif "/ipfs/" in uri:
        cid = uri.split("/ipfs/", 1)[1]

    if not cid:
        return [uri]

    out = [uri] if uri.startswith("http") else []
    out.extend(gateway.rstrip("/") + "/" + cid for gateway in gateways)
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


class MetadataFetcher:
    """Bounded-concurrency fetcher with an in-run URI cache.

    The cache is not a micro-optimisation: duplicate ``(creator, uri)`` pairs
    are a known spam pattern (708 such groups in 16,601 local launches), so the
    same document is frequently requested for many mints at once.
    """

    def __init__(self, cfg: Config) -> None:
        self.timeout = float(cfg.get("metadata.timeout_seconds"))
        self.gateways: list[str] = list(cfg.get("metadata.ipfs_gateways"))
        self.max_attempts = int(cfg.get("metadata.max_attempts"))
        self.retry_backoff = float(cfg.get("metadata.retry_backoff_seconds"))
        self.user_agent: str = cfg.get("metadata.user_agent")
        self._sem = asyncio.Semaphore(int(cfg.get("metadata.max_concurrent")))
        self._cache: dict[str, tuple[dict[str, Any] | None, str | None]] = {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> MetadataFetcher:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, uri: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return (document, error). Exactly one is None."""
        if uri in self._cache:
            return self._cache[uri]
        result = await self._fetch_uncached(uri)
        self._cache[uri] = result
        return result

    def cached(self, uri: str) -> bool:
        return uri in self._cache

    async def _fetch_uncached(self, uri: str) -> tuple[dict[str, Any] | None, str | None]:
        if self._client is None:
            raise RuntimeError("MetadataFetcher must be used as an async context manager")

        last_error = "no candidates"
        for candidate in ipfs_candidates(uri, self.gateways):
            for attempt in range(self.max_attempts):
                async with self._sem:
                    try:
                        resp = await self._client.get(candidate)
                    except httpx.HTTPError as exc:
                        # Transport fault - worth one retry, unlike a 404.
                        last_error = f"{type(exc).__name__}: {exc}"
                        if attempt + 1 < self.max_attempts:
                            await asyncio.sleep(self.retry_backoff)
                        continue

                if resp.status_code != 200:
                    # Terminal for this candidate; another attempt returns the
                    # same 404. Only try the next gateway.
                    last_error = f"HTTP {resp.status_code}"
                    break

                try:
                    doc = resp.json()
                except (ValueError, json.JSONDecodeError):
                    # A dead host serving an HTML error page with status 200.
                    last_error = "response was not JSON"
                    break

                if not isinstance(doc, dict):
                    last_error = f"JSON root was {type(doc).__name__}, not object"
                    break

                return doc, None

        return None, last_error


def _pending(conn: sqlite3.Connection, cfg: Config, limit: int, retry_after: float) -> list[sqlite3.Row]:
    """Launches still needing Tier 1, newest first.

    Newest first is deliberate and is the whole point of the tier: the older a
    launch is, the more likely its metadata host has already gone. Working the
    backlog oldest-first would spend the request budget on documents that are
    disproportionately already dead.
    """
    pools = list(cfg.get("pumpportal.promote_pools"))
    placeholders = ",".join("?" for _ in pools)
    return conn.execute(
        f"""
        SELECT l.mint, l.uri, l.creator
        FROM launches l
        LEFT JOIN token_metadata t ON t.mint = l.mint
        WHERE l.pool IN ({placeholders})
          AND (
                t.mint IS NULL
                OR (t.fetch_ok = 0 AND t.fetched_at < ?)
              )
        ORDER BY l.received_at DESC
        LIMIT ?
        """,
        (*pools, time.time() - retry_after, limit),
    ).fetchall()


async def _resolve_uris_onchain(
    cfg: Config, rows: list[sqlite3.Row], stats: EnrichStats
) -> dict[str, str]:
    """Read each mint's uri from its Token-2022 TokenMetadata extension.

    Called ONLY for launches whose stream frame carried no uri. It used to run
    for every launch, which put an RPC round-trip on the critical path ahead of
    every document fetch - and measurement said that bought nothing: 0
    stream-vs-chain mismatches over ~3,000 mints. Since metadata hosts die
    within days, anything that delays the fetch is a direct cost, and a
    dependency that can rate-limit or stall is a risk to the one step that
    cannot wait.

    Coverage is why it still exists: ~0.5% of frames arrive with no uri at all
    (84 of 16,601 locally), and for those, chain is the only source.
    """
    out: dict[str, str] = {}
    mints = [r["mint"] for r in rows]
    if not mints:
        return out

    try:
        async with SolanaRpc(cfg) as rpc:
            accounts = await rpc.get_multiple_accounts(mints)
    except RpcError as exc:
        # Never fatal: the stream uri is a perfectly good fallback, and losing
        # the whole pass to an RPC blip would cost documents permanently.
        log.warning("on-chain uri resolution unavailable, using stream uri: %s", exc)
        return out

    for row, account in zip(rows, accounts):
        if account is None or not account.data:
            continue
        meta = token2022.decode_token_metadata(account.data)
        if meta is None or not meta.uri:
            continue
        out[row["mint"]] = meta.uri
        stats.onchain_resolved += 1
        if row["uri"] and row["uri"] != meta.uri:
            stats.uri_mismatch += 1
            log.debug("uri mismatch %s stream=%s chain=%s", row["mint"], row["uri"], meta.uri)
    return out


async def run_once(cfg: Config, conn: sqlite3.Connection, limit: int | None = None) -> EnrichStats:
    """One enrichment pass. Returns what it did."""
    stats = EnrichStats()
    batch = int(limit if limit is not None else cfg.get("metadata.batch_size"))
    retry_after = float(cfg.get("metadata.cache_ttl_seconds"))

    rows = _pending(conn, cfg, batch, retry_after)
    stats.considered = len(rows)
    if not rows:
        return stats

    # Only the frames with no uri need chain, and they are rare. Everything
    # else goes straight to the fetch - the RPC must not sit between a launch
    # and its document.
    onchain: dict[str, str] = {}
    if bool(cfg.get("metadata.resolve_uri_onchain")):
        missing_uri = [r for r in rows if not r["uri"]]
        if missing_uri:
            onchain = await _resolve_uris_onchain(cfg, missing_uri, stats)

    min_socials = int(cfg.get("metadata.min_socials_to_promote"))
    require_telegram = bool(cfg.get("metadata.require_telegram"))

    async with MetadataFetcher(cfg) as fetcher:

        async def handle(row: sqlite3.Row) -> tuple[str, Socials | None, str | None, str | None]:
            uri = onchain.get(row["mint"]) or row["uri"]
            if not uri:
                return row["mint"], None, "no uri on stream frame or chain", None
            if fetcher.cached(uri):
                stats.cache_hits += 1
            doc, error = await fetcher.fetch(uri)
            if doc is None:
                return row["mint"], None, error, uri
            return row["mint"], extract_socials(doc), None, uri

        results = await asyncio.gather(*(handle(r) for r in rows))

    for mint, socials, error, uri in results:
        if socials is None:
            stats.fetch_failed += 1
            db.record_token_metadata(conn, mint, None, fetch_error=error, uri=uri)
            continue

        stats.fetched_ok += 1
        db.record_token_metadata(conn, mint, socials, uri=uri)

        promote = socials.count >= min_socials and (not require_telegram or socials.telegram)
        if promote:
            stats.promoted += 1
            db.set_tier(conn, mint, 2, f"socials={socials.count}")

    return stats


async def run_loop(cfg: Config, conn: sqlite3.Connection) -> int:
    """Continuous enrichment. Sleeps only when there is nothing to do.

    Errors are logged and retried rather than raised: this worker exists to
    win a race against metadata expiry, so staying up through a transient
    failure matters more than surfacing it loudly.
    """
    idle_sleep = float(cfg.get("metadata.idle_sleep_seconds"))
    report_every = float(cfg.get("logging.heartbeat_seconds", 60))

    # Once the backlog clears, each pass handles only the handful of launches
    # arriving since the last one. Logging per pass would emit a line every few
    # seconds forever and bury anything worth reading, so totals are
    # accumulated and reported on the heartbeat interval instead.
    total = EnrichStats()
    last_report = time.monotonic()

    while True:
        try:
            stats = await run_once(cfg, conn)
        except Exception:  # noqa: BLE001 - worker must not die on one bad pass
            log.exception("enrichment pass failed; continuing")
            await asyncio.sleep(idle_sleep)
            continue

        total.considered += stats.considered
        total.fetched_ok += stats.fetched_ok
        total.fetch_failed += stats.fetch_failed
        total.promoted += stats.promoted
        total.cache_hits += stats.cache_hits
        total.onchain_resolved += stats.onchain_resolved
        total.uri_mismatch += stats.uri_mismatch

        now = time.monotonic()
        if total.considered and now - last_report >= report_every:
            log.info("enrich %s", total.snapshot())
            total = EnrichStats()
            last_report = now

        if stats.considered == 0:
            await asyncio.sleep(idle_sleep)
        else:
            # Yield briefly so a large backlog cannot monopolise the loop.
            await asyncio.sleep(0)
