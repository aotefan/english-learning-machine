"""Conservative daily test budget, persisted atomically with standard SQLite.
Reservations are not refunded on timeout; the remote API may already bill it.
This is an application guard, not a guarantee about the provider's invoice.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import sqlite3


def positive(value, name):
    try: value=Decimal(str(value))
    except Exception:raise ValueError(f'{name} must be a positive number') from None
    if not value.is_finite() or value<=0:raise ValueError(f'{name} must be a positive number')
    return value


def stored_limits(db, daily_limit, turn_limit):
    """Read the durable override in the caller's current SQLite transaction."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='budget_settings'").fetchone():
        row = db.execute('SELECT turn_limit,limit_micros FROM budget_settings WHERE id=1').fetchone()
        if row:
            return Decimal(row[1]) / 1000000, row[0]
    return daily_limit, turn_limit


def update_limits(path, daily_limit, turn_limit):
    limit = positive(daily_limit, 'daily limit')
    if (type(turn_limit) is not int or not 1 <= turn_limit <= 1000000 or
            limit > 1000000 or limit < Decimal('.01') or limit != limit.quantize(Decimal('.01'))):
        raise ValueError('invalid_budget_settings')
    with sqlite3.connect(path, timeout=5) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS budget_settings '
                   '(id INTEGER PRIMARY KEY CHECK(id=1),turn_limit INTEGER NOT NULL,limit_micros INTEGER NOT NULL)')
        db.execute('INSERT INTO budget_settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET '
                   'turn_limit=excluded.turn_limit,limit_micros=excluded.limit_micros',
                   (turn_limit, int(limit * 1000000)))
    # Usage and billing tables are never reset by a settings change.


def reserve(path, amount, daily_limit, turn_limit, day=None):
    amount=positive(amount,'reservation'); limit=positive(daily_limit,'daily limit')
    if type(turn_limit) is not int or turn_limit<=0:raise ValueError('positive integer turn limit required')
    micros=int((amount*1000000).to_integral_value(rounding=ROUND_CEILING))
    day=day or datetime.now(timezone.utc).date().isoformat()
    Path(path).parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    db=sqlite3.connect(path,timeout=5,isolation_level=None)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, turns INTEGER, micros INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        limit,turn_limit=stored_limits(db,limit,turn_limit)
        cap=int(limit*1000000)
        turns,used=db.execute('SELECT turns,micros FROM daily WHERE day=?',(day,)).fetchone() or (0,0)
        if turns>=turn_limit or used+micros>cap:raise ValueError('daily_cloud_budget_exhausted')
        db.execute('INSERT INTO daily VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET turns=excluded.turns,micros=excluded.micros',
                   (day,turns+1,used+micros))
        db.execute('COMMIT')
        return {'utc_day':day,'reserved_cny':micros/1e6,'daily_reserved_cny':(used+micros)/1e6,'daily_turns':turns+1}
    finally:db.close()
