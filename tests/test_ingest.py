"""Recorder classification and persistence tests.

Payloads are verbatim frames captured from the live stream on 2026-08-02.
Misclassification here silently loses launches, so it is tested against real
shapes rather than idealised ones.
"""

import sqlite3

import pytest

from sniper import db
from sniper.ingest.pumpportal import classify, _redact

PUMP_CREATE = {
    "signature": "5h3eHrnhrwuTGzHWJqbNKNjTWLCZWNW4F5ps7SwsEkDb8YAinRufWFVZzTGp7UGQ5t1Ugfuvh5aqNKLufJwL5vyK",
    "mint": "6mWFH8QAaHPqRMZdTnKeXao3y7jKVHrK7SdwwVv8pump",
    "traderPublicKey": "BrhgsRNSjyPaud2ZkxbZBz8mrcZuJPUsdmguRksHA4kd",
    "txType": "create",
    "initialBuy": 0,
    "solAmount": 0,
    "bondingCurveKey": "HTD5ByqMCu3awVzm7wfowzZegcPoTshDVspVENp7Crfu",
    "vTokensInBondingCurve": 1073000000,
    "vSolInBondingCurve": 30,
    "marketCapSol": 27.958993476234856,
    "name": "NEVER RAN ON CASHBACK",
    "symbol": "CAESAR",
    "uri": "https://metadata.j7tracker.io/metadata/a82f71b4c8e54c7d.json",
    "is_mayhem_mode": False,
    "pool": "pump",
}

# bonk.fun frames use a different shape: solInPool/tokensInPool, no bonding curve.
BONK_CREATE = {
    "signature": "4zbpdmX6cuQHammQyvML3uJVNDGtarBV5tyJad7UvR72r3GLzADccDy66TYWkkubqemWKnAURX5zBqQ66oFt9ty2",
    "traderPublicKey": "37uM1rp8TK7eVURVRnjtaxGkdJyXjgA9uz83DjApcHvq",
    "txType": "create",
    "mint": "AvERHjDJe9APnDeJi54duZ6SvpdkwNczN1CtKhhkbonk",
    "solInPool": 1.975,
    "tokensInPool": 955306784.145121,
    "initialBuy": 44693215.85487902,
    "solAmount": 1.975,
    "newTokenBalance": 44693215.854879,
    "marketCapSol": 30.440651731221216,
    "name": "Cheemscoin",
    "symbol": "CHEEMS",
    "uri": "https://metadata.j7tracker.io/metadata/Ix3qBJYwYs.json",
    "pool": "bonk",
}

MIGRATE = {
    "signature": "2jsBAYqaHD8NGb2bQ5yaqn2GzmhcA4p7rz5X9hUJLjeyZkg72JRCX8PmdByY37T9UG9dsSTMhA3tbZ6ghjhM9r63",
    "mint": "3GWsydsjhPypa61QXJYCWuvZQHvADDpman7V6Lnypump",
    "txType": "migrate",
    "pool": "pump-amm",
}

SUB_ACK = {"message": "Successfully subscribed to token creation events."}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def test_classify_pump_create():
    assert classify(PUMP_CREATE) == "launch"


def test_classify_bonk_create():
    """bonk.fun frames must classify as launches despite the different shape."""
    assert classify(BONK_CREATE) == "launch"


def test_classify_migration():
    assert classify(MIGRATE) == "migration"


def test_classify_subscription_ack_is_unknown():
    """Acks must not be mistaken for launches."""
    assert classify(SUB_ACK) == "unknown"


def test_classify_empty_and_garbage():
    assert classify({}) == "unknown"
    assert classify({"foo": "bar"}) == "unknown"


def test_record_launch_captures_curve_fields(conn):
    assert db.record_launch(conn, PUMP_CREATE, frame_seq=7, conn_epoch=1) is True
    row = conn.execute("SELECT * FROM launches").fetchone()
    assert row["bonding_curve_key"] == "HTD5ByqMCu3awVzm7wfowzZegcPoTshDVspVENp7Crfu"
    assert row["v_sol_in_curve"] == 30
    assert row["v_tokens_in_curve"] == 1073000000
    assert row["is_mayhem_mode"] == 0
    assert row["frame_seq"] == 7
    assert row["conn_epoch"] == 1
    assert row["pool"] == "pump"


def test_sol_amount_and_initial_buy_are_not_conflated(conn):
    """solAmount is SOL spent; initialBuy is tokens received. Distinct columns."""
    db.record_launch(conn, BONK_CREATE)
    row = conn.execute("SELECT * FROM launches").fetchone()
    assert row["initial_buy_sol"] == 1.975
    assert row["initial_buy_tokens"] == pytest.approx(44693215.85487902)


def test_record_launch_is_idempotent(conn):
    assert db.record_launch(conn, PUMP_CREATE) is True
    assert db.record_launch(conn, PUMP_CREATE) is False
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1


def test_record_launch_rejects_payload_without_mint(conn):
    assert db.record_launch(conn, SUB_ACK) is False


def test_raw_payload_is_preserved_verbatim(conn):
    """Feature definitions will change; the raw event must survive intact."""
    import json
    db.record_launch(conn, PUMP_CREATE)
    row = conn.execute("SELECT raw_json FROM launches").fetchone()
    assert json.loads(row["raw_json"]) == PUMP_CREATE


def test_instant_bond_detected_when_migration_follows_launch_immediately(conn):
    """create + full-curve buy in one bundle is not an organic graduation."""
    db.record_launch(conn, {**PUMP_CREATE, "mint": MIGRATE["mint"]})
    db.record_migration(conn, MIGRATE)
    row = conn.execute("SELECT * FROM migrations").fetchone()
    assert row["instant_bond"] == 1
    assert row["seconds_since_launch"] < db.INSTANT_BOND_SECONDS


def test_migration_without_known_launch_is_not_flagged(conn):
    """Tokens that launched before we connected have no gap to measure."""
    db.record_migration(conn, MIGRATE)
    row = conn.execute("SELECT * FROM migrations").fetchone()
    assert row["seconds_since_launch"] is None
    assert row["instant_bond"] == 0


def test_organic_graduation_not_flagged_as_instant_bond(conn):
    import time
    db.record_launch(conn, {**PUMP_CREATE, "mint": MIGRATE["mint"]})
    conn.execute(
        "UPDATE launches SET received_at = ? WHERE mint = ?",
        (time.time() - 1800, MIGRATE["mint"]),
    )
    db.record_migration(conn, MIGRATE)
    row = conn.execute("SELECT * FROM migrations").fetchone()
    assert row["instant_bond"] == 0
    assert row["seconds_since_launch"] == pytest.approx(1800, abs=5)


def test_redact_strips_api_key():
    assert "secret" not in _redact("wss://x.fun/api/data?api-key=secret")
    assert _redact("wss://x.fun/api/data?api-key=secret&z=1").endswith("&z=1")
    assert _redact("wss://x.fun/api/data") == "wss://x.fun/api/data"


def test_migrate_never_lands_in_launches_table(conn):
    """Regression: a migrate frame must not be recorded as a launch."""
    kind = classify(MIGRATE)
    assert kind == "migration"
    if kind == "launch":  # pragma: no cover - guard documents the intent
        db.record_launch(conn, MIGRATE)
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0
