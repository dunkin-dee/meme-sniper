"""SQLite storage and migrations.

Design notes:

* Every raw event payload is preserved verbatim in a ``raw_json`` column. Feature
  definitions will change as we learn the post-BOOST regime; keeping the raw
  event means features can be recomputed without re-collecting data we can never
  get back.
* Timestamps are unix epoch seconds (UTC, float). ``received_at`` is our clock,
  ``event_ts`` is the source's clock where one is provided - they are recorded
  separately because clock skew matters for latency features.
* WAL mode so the analysis notebook can read while the recorder writes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        # -- Tier 0: the firehose. One row per observed launch. ----------------
        """
        CREATE TABLE IF NOT EXISTS launches (
            mint              TEXT PRIMARY KEY,
            creator           TEXT,
            name              TEXT,
            symbol            TEXT,
            uri               TEXT,
            initial_buy_sol   REAL,
            market_cap_sol    REAL,
            signature         TEXT,
            pool              TEXT,
            event_ts          REAL,
            received_at       REAL NOT NULL,
            tier              INTEGER NOT NULL DEFAULT 0,
            tier_reason       TEXT,
            raw_json          TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_launches_received ON launches(received_at)",
        "CREATE INDEX IF NOT EXISTS ix_launches_creator  ON launches(creator)",
        "CREATE INDEX IF NOT EXISTS ix_launches_tier     ON launches(tier)",

        # -- Migration (graduation) events. ------------------------------------
        """
        CREATE TABLE IF NOT EXISTS migrations (
            mint         TEXT PRIMARY KEY,
            signature    TEXT,
            pool         TEXT,
            event_ts     REAL,
            received_at  REAL NOT NULL,
            raw_json     TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_migrations_received ON migrations(received_at)",

        # -- Tier 1: metadata / socials. ---------------------------------------
        """
        CREATE TABLE IF NOT EXISTS token_metadata (
            mint          TEXT PRIMARY KEY,
            has_twitter   INTEGER,
            has_telegram  INTEGER,
            has_website   INTEGER,
            social_count  INTEGER,
            twitter       TEXT,
            telegram      TEXT,
            website       TEXT,
            description   TEXT,
            image         TEXT,
            fetch_ok      INTEGER NOT NULL,
            fetch_error   TEXT,
            fetched_at    REAL NOT NULL,
            raw_json      TEXT,
            FOREIGN KEY (mint) REFERENCES launches(mint)
        )
        """,

        # -- Tier 2: deployer track record. ------------------------------------
        """
        CREATE TABLE IF NOT EXISTS deployer_stats (
            creator            TEXT PRIMARY KEY,
            prior_launches     INTEGER,
            prior_graduations  INTEGER,
            graduation_rate    REAL,
            wallet_age_hours   REAL,
            is_fresh_wallet    INTEGER,
            computed_at        REAL NOT NULL,
            raw_json           TEXT
        )
        """,

        # -- Tier 2: RugCheck safety report. -----------------------------------
        """
        CREATE TABLE IF NOT EXISTS safety_reports (
            mint               TEXT PRIMARY KEY,
            score              REAL,
            top10_holder_pct   REAL,
            dev_holding_pct    REAL,
            insider_network    INTEGER,
            mint_authority     INTEGER,
            freeze_authority   INTEGER,
            lp_locked_pct      REAL,
            risks_json         TEXT,
            fetch_ok           INTEGER NOT NULL,
            fetch_error        TEXT,
            fetched_at         REAL NOT NULL,
            raw_json           TEXT,
            FOREIGN KEY (mint) REFERENCES launches(mint)
        )
        """,

        # -- Tier 3: bonding-curve time series. --------------------------------
        """
        CREATE TABLE IF NOT EXISTS curve_samples (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            mint                  TEXT NOT NULL,
            sampled_at            REAL NOT NULL,
            virtual_sol           REAL,
            virtual_token         REAL,
            real_sol              REAL,
            real_token            REAL,
            token_total_supply    REAL,
            complete              INTEGER,
            price_sol_per_token   REAL,
            progress_pct          REAL,
            FOREIGN KEY (mint) REFERENCES launches(mint)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_curve_mint_ts ON curve_samples(mint, sampled_at)",

        # -- Watchlist state for the tracker. ----------------------------------
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            mint          TEXT PRIMARY KEY,
            added_at      REAL NOT NULL,
            first_seen_at REAL NOT NULL,
            last_polled   REAL,
            poll_count    INTEGER NOT NULL DEFAULT 0,
            active        INTEGER NOT NULL DEFAULT 1,
            dropped_at    REAL,
            drop_reason   TEXT,
            FOREIGN KEY (mint) REFERENCES launches(mint)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_watchlist_active ON watchlist(active, last_polled)",

        # -- Scores / alerts. Full feature vector persisted for reconstruction. -
        """
        CREATE TABLE IF NOT EXISTS scores (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mint          TEXT NOT NULL,
            scored_at     REAL NOT NULL,
            score         REAL NOT NULL,
            alerted       INTEGER NOT NULL DEFAULT 0,
            price_at_score REAL,
            features_json TEXT NOT NULL,
            breakdown_json TEXT NOT NULL,
            config_hash   TEXT,
            FOREIGN KEY (mint) REFERENCES launches(mint)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_scores_mint ON scores(mint, scored_at)",
        "CREATE INDEX IF NOT EXISTS ix_scores_alerted ON scores(alerted, scored_at)",

        # -- Paper trading. ----------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            mint              TEXT NOT NULL,
            score_id          INTEGER,
            opened_at         REAL NOT NULL,
            entry_price       REAL NOT NULL,
            size_sol          REAL NOT NULL,
            tokens            REAL NOT NULL,
            entry_fees_sol    REAL NOT NULL,
            status            TEXT NOT NULL DEFAULT 'open',
            closed_at         REAL,
            realized_pnl_sol  REAL,
            exit_reason       TEXT,
            FOREIGN KEY (mint) REFERENCES launches(mint)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_paper_status ON paper_positions(status)",
        """
        CREATE TABLE IF NOT EXISTS paper_fills (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id  INTEGER NOT NULL,
            filled_at    REAL NOT NULL,
            side         TEXT NOT NULL,
            price        REAL NOT NULL,
            tokens       REAL NOT NULL,
            sol_delta    REAL NOT NULL,
            fees_sol     REAL NOT NULL,
            reason       TEXT,
            FOREIGN KEY (position_id) REFERENCES paper_positions(id)
        )
        """,

        # -- Outcome labels. The dataset that makes Phase 2 possible. -----------
        """
        CREATE TABLE IF NOT EXISTS labels (
            mint              TEXT NOT NULL,
            horizon_hours     INTEGER NOT NULL,
            reference_price   REAL,
            max_multiple      REAL,
            min_multiple      REAL,
            max_drawdown      REAL,
            time_to_peak_s    REAL,
            graduated         INTEGER,
            rugged            INTEGER,
            alive             INTEGER,
            hit_target        INTEGER,
            computed_at       REAL NOT NULL,
            PRIMARY KEY (mint, horizon_hours)
        )
        """,

        # -- RPC credit accounting. Budget is enforced, not assumed. ------------
        """
        CREATE TABLE IF NOT EXISTS rpc_usage (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            period       TEXT NOT NULL,
            method       TEXT NOT NULL,
            calls        INTEGER NOT NULL DEFAULT 0,
            credits      REAL NOT NULL DEFAULT 0,
            UNIQUE (period, method)
        )
        """,

        # -- Operational log: reconnects, rate-limit hits, budget stops. --------
        """
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            at         REAL NOT NULL,
            kind       TEXT NOT NULL,
            detail     TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_events_at ON events(at, kind)",
    ],

    # ---------------------------------------------------------------------
    # v2 - driven by what the live stream actually sends (verified 2026-08-02,
    # 75s sample). See docs/STREAM_NOTES.md for the raw evidence.
    # ---------------------------------------------------------------------
    2: [
        # The create frame already carries the bonding-curve address and its
        # opening virtual reserves. That saves a PDA derivation and an RPC call
        # per token, and gives an exact t=0 baseline for velocity features.
        "ALTER TABLE launches ADD COLUMN bonding_curve_key TEXT",
        "ALTER TABLE launches ADD COLUMN v_sol_in_curve REAL",
        "ALTER TABLE launches ADD COLUMN v_tokens_in_curve REAL",
        # Verified against 53 live launches: virtual SOL always opens at exactly
        # 30, and the create frame reports state AFTER the dev's pre-buy, so
        #     v_sol_in_curve == 30 + initial_buy_sol   (max residual 3.8e-10)
        # This makes dev pre-buy - a headline rug signal - readable at t=0 with
        # no RPC call, and gives a free integrity check on every frame.
        "ALTER TABLE launches ADD COLUMN initial_buy_tokens REAL",
        # Undocumented field, present on ~98% of frames, true on ~13%.
        "ALTER TABLE launches ADD COLUMN is_mayhem_mode INTEGER",
        # bonk.fun frames use a different shape (solInPool/tokensInPool).
        "ALTER TABLE launches ADD COLUMN sol_in_pool REAL",
        "ALTER TABLE launches ADD COLUMN tokens_in_pool REAL",
        # Frame index since connect. PumpPortal may replay/backfill on
        # subscribe; without this, replayed frames are indistinguishable from
        # live ones and would corrupt any time-to-graduation feature.
        "ALTER TABLE launches ADD COLUMN frame_seq INTEGER",
        "ALTER TABLE launches ADD COLUMN conn_epoch INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_launches_pool ON launches(pool)",

        "ALTER TABLE migrations ADD COLUMN frame_seq INTEGER",
        "ALTER TABLE migrations ADD COLUMN conn_epoch INTEGER",
        # Seconds between our launch frame and our migration frame for the same
        # mint. Implausibly small values (<1s) indicate an instant-bond bundle:
        # create + full-curve buy in a single Jito bundle. Those tokens are
        # unbuyable by us and must be excluded from "fast graduation is good"
        # style features rather than treated as successes.
        "ALTER TABLE migrations ADD COLUMN seconds_since_launch REAL",
        "ALTER TABLE migrations ADD COLUMN instant_bond INTEGER NOT NULL DEFAULT 0",
    ],
}

# A migration observed within this many seconds of our launch frame cannot be a
# genuine organic graduation (115 vSOL is unreachable that fast); it is a bundle.
INSTANT_BOND_SECONDS = 5.0


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database, applying pragmas and migrations."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in _MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------
# Write helpers
# --------------------------------------------------------------------------

def record_launch(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    frame_seq: int | None = None,
    conn_epoch: int | None = None,
) -> bool:
    """Insert a launch event. Returns True if newly recorded, False if a duplicate.

    Field names are taken defensively. Note ``solAmount`` and ``initialBuy`` are
    NOT interchangeable: on pump frames ``solAmount`` is SOL spent and
    ``initialBuy`` is tokens received, so they are stored in separate columns.
    The verbatim payload is retained regardless of parse success.
    """
    mint = payload.get("mint") or payload.get("ca")
    if not mint:
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO launches
            (mint, creator, name, symbol, uri, initial_buy_sol, market_cap_sol,
             signature, pool, event_ts, received_at, tier, raw_json,
             bonding_curve_key, v_sol_in_curve, v_tokens_in_curve,
             initial_buy_tokens, is_mayhem_mode, sol_in_pool, tokens_in_pool,
             frame_seq, conn_epoch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mint,
            payload.get("traderPublicKey") or payload.get("creator"),
            payload.get("name"),
            payload.get("symbol"),
            payload.get("uri"),
            _as_float(payload.get("solAmount")),
            _as_float(payload.get("marketCapSol")),
            payload.get("signature"),
            payload.get("pool"),
            # PumpPortal does not send a timestamp field (verified: 0/48 frames).
            # Kept nullable in case that changes; on-chain block time from the
            # signature is the authoritative launch time for any timing feature.
            _as_float(payload.get("timestamp")),
            time.time(),
            json.dumps(payload, separators=(",", ":")),
            payload.get("bondingCurveKey"),
            _as_float(payload.get("vSolInBondingCurve")),
            _as_float(payload.get("vTokensInBondingCurve")),
            _as_float(payload.get("initialBuy")),
            _as_bool_int(payload.get("is_mayhem_mode")),
            _as_float(payload.get("solInPool")),
            _as_float(payload.get("tokensInPool")),
            frame_seq,
            conn_epoch,
        ),
    )
    return cur.rowcount > 0


def record_migration(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    frame_seq: int | None = None,
    conn_epoch: int | None = None,
) -> bool:
    """Insert a migration event, flagging instant-bond bundles.

    If we saw the launch frame for this mint, the gap between the two frames is
    recorded. A gap under INSTANT_BOND_SECONDS means the curve was filled in the
    same bundle as creation - not an organic graduation, and not something we
    could ever have bought into.
    """
    mint = payload.get("mint") or payload.get("ca")
    if not mint:
        return False

    now = time.time()
    row = conn.execute(
        "SELECT received_at FROM launches WHERE mint = ?", (mint,)
    ).fetchone()
    gap = (now - row["received_at"]) if row else None
    instant = 1 if gap is not None and gap < INSTANT_BOND_SECONDS else 0

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO migrations
            (mint, signature, pool, event_ts, received_at, raw_json,
             frame_seq, conn_epoch, seconds_since_launch, instant_bond)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mint,
            payload.get("signature"),
            payload.get("pool"),
            _as_float(payload.get("timestamp")),
            now,
            json.dumps(payload, separators=(",", ":")),
            frame_seq,
            conn_epoch,
            gap,
            instant,
        ),
    )
    return cur.rowcount > 0


def log_event(conn: sqlite3.Connection, kind: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
        (time.time(), kind, detail),
    )


def set_tier(conn: sqlite3.Connection, mint: str, tier: int, reason: str | None = None) -> None:
    conn.execute(
        "UPDATE launches SET tier = ?, tier_reason = ? WHERE mint = ?",
        (tier, reason, mint),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Summary counts for the heartbeat log."""
    out: dict[str, int] = {}
    for table in ("launches", "migrations", "token_metadata", "curve_samples", "scores"):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        out[table] = row["n"]
    return out


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    conn.executemany(sql, list(rows))
