"""Short-lived review cards backed by the cloud service's learning table."""
import sqlite3
import re
import hashlib
import json
import time
from pathlib import Path


RETENTION_SECONDS = 7 * 86400
REVIEW_DELAYS_SECONDS = (86400, 2 * 86400, 3 * 86400)
DIAGNOSTIC_DEVICE_ID = 'cloud-smoke'


def ensure_learning_schema(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(learning)')}
    for name, definition in (
        ('review_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('last_reviewed_at', 'REAL'),
        ('next_review_at', 'REAL'),
        ('practice_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('last_practiced_at', 'REAL'),
        ('intent', "TEXT NOT NULL DEFAULT 'express'"),
        ('rejected', 'INTEGER NOT NULL DEFAULT 0'),
        ('independent_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('assisted_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('last_review_outcome', 'TEXT'),
        ('review_selected', 'INTEGER NOT NULL DEFAULT 0'),
        ('review_dismissed', 'INTEGER NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            db.execute(f'ALTER TABLE learning ADD COLUMN {name} {definition}')
            if name == 'review_selected':
                # Keep cards that someone already reviewed or practiced.
                db.execute('''UPDATE learning SET review_selected = 1
                              WHERE review_count > 0 OR practice_count > 0''')
    db.execute('CREATE INDEX IF NOT EXISTS learning_due_idx ON learning(next_review_at, time)')
    db.execute('CREATE INDEX IF NOT EXISTS learning_device_time_idx ON learning(device_id, time)')


def _expression_key(english):
    normalized = ' '.join(english.lower().split())
    return re.sub(r'[\s.!?。！？…]+$', '', normalized)


def _review_key(row):
    source = json.dumps([row['device_id'], _expression_key(row['english'])], separators=(',', ':'))
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def _eligible_rows(db, now, *, device_id=None, request_id=None):
    db.row_factory = sqlite3.Row
    query = '''SELECT * FROM learning
               WHERE time >= ? AND english IS NOT NULL AND english != ''
                 AND playback_reported = 1 AND intent = 'express' AND rejected = 0
                 AND device_id != ?'''
    parameters = [now - RETENTION_SECONDS, DIAGNOSTIC_DEVICE_ID]
    for column, value in (('device_id', device_id), ('request_id', request_id)):
        if value is not None:
            query += f' AND {column} = ?'
            parameters.append(value)
    return [dict(row) for row in db.execute(query, parameters) if _expression_key(row['english'])]


def _group_for_request(db, request_id, now):
    target = _eligible_rows(db, now, request_id=request_id)
    if not target:
        return []
    key = _expression_key(target[0]['english'])
    return [row for row in _eligible_rows(db, now, device_id=target[0]['device_id'])
            if _expression_key(row['english']) == key]


def _summarize_group(group):
    """Sum original observations; never copy merged counters back onto each row."""
    selected = [row for row in group if row['review_selected']]
    representative = max(selected or group, key=lambda row: (row['time'], row['request_id']))
    card = {key: representative[key] for key in ('request_id', 'time', 'asr_raw', 'english')}
    card.update(review_key=_review_key(representative), occurrence_count=len(group))
    for field in ('review_count', 'practice_count', 'independent_count', 'assisted_count'):
        card[field] = sum(row[field] for row in group)
    reviewed = [row for row in group if row['review_count'] > 0 and row['last_reviewed_at'] is not None]
    latest = max(reviewed, key=lambda row: (row['last_reviewed_at'], row['time'], row['request_id'])) if reviewed else None
    for field in ('last_reviewed_at', 'next_review_at', 'last_review_outcome'):
        card[field] = latest[field] if latest else None
    practiced = [row['last_practiced_at'] for row in group if row['last_practiced_at'] is not None]
    card['last_practiced_at'] = max(practiced) if practiced else None
    return card


def learning_report(db_path, *, now=None, view='all'):
    if view not in ('all', 'due', 'needs_help'):
        raise ValueError('invalid_review_view')
    now = time.time() if now is None else now
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        groups = {}
        for row in _eligible_rows(db, now):
            groups.setdefault((row['device_id'], _expression_key(row['english'])), []).append(row)
    cards, candidates = [], []
    for group in groups.values():
        if any(row['review_selected'] for row in group):
            card = _summarize_group(group)
            card['due'] = card['next_review_at'] is None or card['next_review_at'] <= now
            cards.append(card)
        elif not any(row['review_dismissed'] for row in group):
            representative = max(group, key=lambda row: (row['time'], row['request_id']))
            candidate = {key: representative[key] for key in ('request_id', 'time', 'asr_raw', 'english')}
            candidate.update(review_key=_review_key(representative), occurrence_count=len(group))
            candidates.append(candidate)
    cards.sort(key=lambda card: (not card['due'], -card['time'], card['request_id']))
    candidates.sort(key=lambda card: (-card['time'], card['request_id']))
    summary = {'selected_count': len(cards), 'due_count': sum(card['due'] for card in cards),
               'needs_help_count': sum(card['last_review_outcome'] == 'assisted' for card in cards),
               'independent_count': sum(card['independent_count'] for card in cards),
               'assisted_count': sum(card['assisted_count'] for card in cards)}
    # Filter complete deduplicated groups before limiting the response; a card
    # beyond the all-view limit must still be reachable through its own view.
    visible = [card for card in cards if view == 'all' or
               (view == 'due' and card['due']) or
               (view == 'needs_help' and card['last_review_outcome'] == 'assisted')]
    return {'cards': visible[:50], 'due_count': summary['due_count'], 'retention_days': 7,
            'summary': summary, 'view': view, 'matched_count': len(visible),
            'read_at': now, 'truncated': len(visible) > 50,
            'candidates': candidates[:20], 'candidates_truncated': len(candidates) > 20}


def mark_reviewed(db_path, request_id, *, outcome='viewed', support='none', now=None):
    """Record a self-reported result; support is declared, not independently observed."""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        return None
    if outcome not in ('viewed', 'independent', 'assisted'):
        raise ValueError('invalid_review_outcome')
    if support not in ('none', 'hint', 'answer'):
        raise ValueError('invalid_review_support')
    if outcome == 'independent' and support != 'none':
        raise ValueError('independent_review_cannot_use_support')
    now = time.time() if now is None else now
    with sqlite3.connect(db_path, timeout=5) as db:
        db.execute('BEGIN IMMEDIATE')
        group = _group_for_request(db, request_id, now)
        if not any(row['review_selected'] for row in group):
            return None
        card = _summarize_group(group)
        count, due_at = card['review_count'], card['next_review_at']
        independent_count, assisted_count = card['independent_count'], card['assisted_count']
        if due_at is not None and due_at > now:
            return {'status': 'not_due', 'next_review_at': due_at}
        count += 1
        if outcome == 'independent':
            independent_count += 1
            delay = REVIEW_DELAYS_SECONDS[min(independent_count, len(REVIEW_DELAYS_SECONDS) - 1)]
            delay = max(delay, 2 * 86400)
        elif outcome == 'assisted':
            assisted_count += 1
            delay = REVIEW_DELAYS_SECONDS[0]
        else:
            delay = REVIEW_DELAYS_SECONDS[min(count - 1, len(REVIEW_DELAYS_SECONDS) - 1)]
        next_review_at = now + delay
        # Record this action once, on the requested member. A duplicate that
        # arrived after selection inherits the group's existing selection.
        db.execute(
            '''UPDATE learning SET review_count = review_count + 1, last_reviewed_at = ?, next_review_at = ?,
                                  independent_count = independent_count + ?, assisted_count = assisted_count + ?,
                                  last_review_outcome = ?, review_selected = 1
               WHERE request_id = ?''',
            (now, next_review_at, int(outcome == 'independent'), int(outcome == 'assisted'), outcome, request_id))
    return {'status': 'reviewed', 'review_count': count, 'next_review_at': next_review_at,
            'independent_count': independent_count, 'assisted_count': assisted_count,
            'last_review_outcome': outcome}


def mark_practiced(db_path, request_id, *, now=None):
    """Count a user's completed browser follow-along; no recording reaches this function."""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        return None
    now = time.time() if now is None else now
    with sqlite3.connect(db_path, timeout=5) as db:
        db.execute('BEGIN IMMEDIATE')
        group = _group_for_request(db, request_id, now)
        if not any(row['review_selected'] for row in group):
            return None
        card = _summarize_group(group)
        count, last_at = card['practice_count'], card['last_practiced_at']
        if last_at is not None and now - last_at < 10:
            return {'status': 'too_soon', 'practice_count': count, 'last_practiced_at': last_at}
        count += 1
        db.execute('''UPDATE learning SET practice_count = practice_count + 1, last_practiced_at = ?,
                      review_selected = 1 WHERE request_id = ?''', (now, request_id))
    return {'status': 'practiced', 'practice_count': count, 'last_practiced_at': now}


def set_candidate_decision(db_path, request_id, decision, *, now=None):
    """Only a deliberate admin choice turns a recent expression into a review card."""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        return None
    if decision not in ('select', 'dismiss'):
        raise ValueError('invalid_candidate_decision')
    now = time.time() if now is None else now
    with sqlite3.connect(db_path, timeout=5) as db:
        db.execute('BEGIN IMMEDIATE')
        group = _group_for_request(db, request_id, now)
        if not group:
            return None
        if any(row['review_selected'] or row['review_dismissed'] for row in group):
            return {'status': 'already_decided'}
        column = 'review_selected' if decision == 'select' else 'review_dismissed'
        db.executemany(f'UPDATE learning SET {column} = 1 WHERE request_id = ?',
                       [(row['request_id'],) for row in group])
    representative = max(group, key=lambda row: (row['time'], row['request_id']))
    return {'status': 'selected' if decision == 'select' else 'dismissed',
            'request_id': representative['request_id'], 'review_key': _review_key(representative)}
