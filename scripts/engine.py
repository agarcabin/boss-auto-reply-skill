"""Durable, independent reply queue. No browser or model dependencies."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def reply_segments(text):
    """Split sentence periods into separate messages, without trailing periods."""
    parts = [part.strip() for part in str(text or '').split('。') if part.strip()]
    if not parts:
        return []
    if len(parts) > 3:
        parts = parts[:2] + [' '.join(parts[2:])]
    return [part.rstrip('。').strip() for part in parts if part.rstrip('。').strip()]


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES('control','stopped');
            CREATE TABLE IF NOT EXISTS conversations(
              id TEXT PRIMARY KEY, job TEXT NOT NULL, version TEXT NOT NULL,
              snapshot TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0,
              reason TEXT NOT NULL DEFAULT '', rounds INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY, conversation TEXT NOT NULL, version TEXT NOT NULL,
              state TEXT NOT NULL, payload TEXT NOT NULL, answer TEXT,
              reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS request_state ON requests(state, created);
            CREATE TABLE IF NOT EXISTS receipts(
              message TEXT NOT NULL, conversation TEXT NOT NULL, job TEXT NOT NULL,
              request TEXT NOT NULL, PRIMARY KEY(conversation,message));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def setting(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, str(value)))
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def control(self, action):
        values = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}
        if action not in values:
            raise ValueError('invalid_control')
        with self.db() as db:
            if action == 'resume' and db.execute("SELECT 1 FROM requests WHERE state IN ('sending','unknown')").fetchone():
                raise ValueError('resolve_unknown_send_before_resume')
            db.execute("UPDATE settings SET value=? WHERE key='control'", (values[action],))
        return self.status()

    def status(self):
        with self.db() as db:
            counts = {r['state']: r['n'] for r in db.execute('SELECT state,count(*) n FROM requests GROUP BY state')}
            control = db.execute("SELECT value FROM settings WHERE key='control'").fetchone()[0]
            manual = db.execute('SELECT count(*) FROM conversations WHERE manual=1').fetchone()[0]
        return {'control': control, 'counts': counts, 'manual_conversations': manual,
                'heartbeat': self.setting('heartbeat'), 'last_error': self.setting('last_error')}

    def recover(self):
        # A crash after dispatch may have sent. Never put it back into the send queue.
        with self.db() as db:
            db.execute("UPDATE requests SET state='unknown',reason='process_interrupted_during_send' WHERE state='sending'")
            if db.execute("SELECT 1 FROM requests WHERE state='unknown'").fetchone():
                db.execute("UPDATE settings SET value='paused' WHERE key='control'")

    @staticmethod
    def validate(snapshot):
        for field in ('conversation_id', 'job_id', 'messages'):
            if not snapshot.get(field):
                raise ValueError('snapshot_missing_' + field)
        if not snapshot.get('identity_verified') or not snapshot.get('complete'):
            raise ValueError('snapshot_not_verified')
        ids = set()
        for m in snapshot['messages']:
            if not m.get('id') or m['id'] in ids or m.get('role') not in ('candidate', 'recruiter', 'system'):
                raise ValueError('message_identity_or_role_invalid')
            if not isinstance(m.get('text'), str) or not m['text'].strip() or m.get('kind', 'text') != 'text':
                raise ValueError('non_text_or_empty_message')
            ids.add(m['id'])

    @staticmethod
    def version(s):
        return digest({'conversation_id': s['conversation_id'], 'job_id': s['job_id'],
                       'job_title': s.get('job_title', ''),
                       'knowledge_position': s.get('knowledge_position', ''),
                       'mapping_hash': s.get('mapping_hash', ''),
                       'messages': s['messages'], 'draft': s.get('draft', '')})

    def ingest(self, snapshot, allowed_jobs, max_rounds=3, new_is_event=False, baseline_only=False):
        self.validate(snapshot)
        cid, job = snapshot['conversation_id'], snapshot['job_id']
        if job not in allowed_jobs:
            return {'state': 'ignored', 'reason': 'job_not_allowed'}
        version = self.version(snapshot)
        now = time.time()
        with self.db() as db:
            old = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            first_seen = old is None
            if not old:
                db.execute('INSERT INTO conversations(id,job,version,snapshot) VALUES(?,?,?,?)',
                           (cid, job, version, json.dumps(snapshot, ensure_ascii=False)))
                if not new_is_event or baseline_only:
                    return {'state': 'baseline'}
                old = dict(db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone())
                old['version'] = ''
                previous = dict(snapshot, messages=[])
                old['snapshot'] = json.dumps(previous)
            if old['version'] == version:
                return {'state': 'unchanged'}
            previous = json.loads(old['snapshot'])
            db.execute('UPDATE conversations SET job=?,version=?,snapshot=? WHERE id=?',
                       (job, version, json.dumps(snapshot, ensure_ascii=False), cid))
            db.execute("UPDATE requests SET state='stale',reason='context_changed',updated=? WHERE conversation=? AND state IN ('pending','ready')", (now, cid))
            if baseline_only:
                return {'state': 'baseline'}
            old_ids = {m['id'] for m in previous['messages']}
            new = [m for m in snapshot['messages'] if m['id'] not in old_ids and m['role'] != 'system']
            owned = [m for m in new if m['role'] == 'recruiter']
            # A first sighting establishes the conversation baseline. Existing
            # recruiter messages may come from prior outreach or platform
            # notices and must not latch the conversation as human-owned.
            unexpected = [] if first_seen else [m for m in owned if not db.execute(
                'SELECT 1 FROM receipts WHERE conversation=? AND message=?', (cid, m['id'])).fetchone()]
            reason = ''
            if old['job'] != job:
                reason = 'job_changed'
            elif snapshot.get('draft', '').strip():
                reason = 'human_draft_present'
            elif unexpected:
                reason = 'human_outbound_detected'
            elif max_rounds and old['rounds'] >= max_rounds:
                reason = 'automatic_round_limit'
            if reason:
                db.execute('UPDATE conversations SET manual=1,reason=? WHERE id=?', (reason, cid))
                return {'state': 'manual', 'reason': reason}
            if old['manual']:
                return {'state': 'manual', 'reason': old['reason']}
            dialogue = [m for m in snapshot['messages'] if m['role'] != 'system']
            if not new or not dialogue or dialogue[-1]['role'] != 'candidate':
                return {'state': 'observed'}
            incoming = [m for m in new if m['role'] == 'candidate']
            if not incoming:
                return {'state': 'observed'}
            # Same event cannot be recreated by a restart or repeated scan.
            trailing = []
            for message in reversed(dialogue):
                if message['role'] != 'candidate':
                    break
                trailing.insert(0, message['id'])
            request_id = digest([cid, job, trailing])
            payload = dict(request_id=request_id, version=version, snapshot=snapshot)
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                       (request_id, cid, version, 'pending', json.dumps(payload, ensure_ascii=False),
                        None, '', now, now))
            return {'state': 'pending', 'request_id': request_id}

    def pending(self):
        with self.db() as db:
            rows = db.execute("SELECT payload FROM requests WHERE state='pending' ORDER BY created LIMIT 20").fetchall()
            return [json.loads(r[0]) for r in rows]

    def queue_unread(self, snapshot, allowed_jobs, max_rounds=3):
        """Include existing unread at startup without replaying already answered history."""
        result = self.ingest(snapshot, allowed_jobs, max_rounds, new_is_event=True)
        if result['state'] in ('ignored', 'manual', 'pending'):
            return result
        dialogue = [m for m in snapshot['messages'] if m['role'] != 'system']
        if not dialogue or dialogue[-1]['role'] != 'candidate':
            return {'state': 'observed'}
        trailing = []
        for message in reversed(dialogue):
            if message['role'] != 'candidate':
                break
            trailing.insert(0, message['id'])
        cid, job = snapshot['conversation_id'], snapshot['job_id']
        request_id = digest([cid, job, trailing])
        with self.db() as db:
            current = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            if current['manual'] or snapshot.get('draft', '').strip() or (max_rounds and current['rounds'] >= max_rounds):
                return {'state': 'manual'}
            known = db.execute('SELECT state FROM requests WHERE id=?', (request_id,)).fetchone()
            if known:
                return {'state': known['state'], 'request_id': request_id}
            payload = {'request_id': request_id, 'version': self.version(snapshot), 'snapshot': snapshot}
            now = time.time()
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                       (request_id, cid, payload['version'], 'pending', json.dumps(payload, ensure_ascii=False), None, '', now, now))
        return {'state': 'pending', 'request_id': request_id}

    def respond(self, request_id, version, action, text='', sources=None,
                segments=None):
        if action not in ('reply', 'manual'):
            raise ValueError('action_must_be_reply_or_manual')
        if action == 'reply' and (not isinstance(text, str) or not text.strip() or len(text) > 1000):
            raise ValueError('reply_text_invalid')
        with self.db() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if not row or row['state'] != 'pending' or row['version'] != version:
                raise ValueError('request_not_pending_or_version_changed')
            current = db.execute('SELECT * FROM conversations WHERE id=?', (row['conversation'],)).fetchone()
            if current['version'] != version or current['manual']:
                raise ValueError('conversation_changed_or_manual')
            if action == 'manual':
                db.execute("UPDATE conversations SET manual=1,reason='agent_handoff' WHERE id=?", (row['conversation'],))
            answer_text = text.strip()
            answer_segments = []
            if action == 'reply':
                answer_segments = [
                    part.strip() for part in (segments or reply_segments(answer_text))
                    if isinstance(part, str) and part.strip()]
                answer_segments = [
                    part.rstrip('。').strip() for part in answer_segments
                    if part.rstrip('。').strip()]
                if not answer_segments or any(len(part) > 300 for part in answer_segments):
                    raise ValueError('reply_segments_invalid')
                answer_text = ' '.join(answer_segments)
            db.execute('UPDATE requests SET state=?,answer=?,updated=? WHERE id=?',
                       ('ready' if action == 'reply' else 'manual',
                        json.dumps({'text': answer_text, 'segments': answer_segments,
                                    'sources': sources or []}, ensure_ascii=False),
                        time.time(), request_id))
        return {'state': 'ready' if action == 'reply' else 'manual', 'request_id': request_id}

    def manual(self, conversation_id, release=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
            if not row:
                raise ValueError('unknown_conversation')
            if db.execute("SELECT 1 FROM requests WHERE conversation=? AND state IN ('sending','unknown')", (conversation_id,)).fetchone():
                raise ValueError('resolve_unknown_send_first')
            db.execute('UPDATE conversations SET manual=?,reason=?,rounds=0 WHERE id=?',
                       (0 if release else 1, '' if release else 'user_takeover', conversation_id))
            db.execute("UPDATE requests SET state='stale',reason='manual_control' WHERE conversation=? AND state IN ('pending','ready')", (conversation_id,))
        return {'state': 'released' if release else 'manual', 'conversation_id': conversation_id}

    def adopt_conversation(self, request_id, snapshot):
        """Rebind a pending/ready request after BOSS changes the row runtime id."""
        self.validate(snapshot)
        version = self.version(snapshot)
        with self.db() as db:
            r = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if not r or r['state'] not in ('pending', 'ready'):
                raise ValueError('request_not_pending_or_ready')
            payload = json.loads(r['payload'])
            previous = payload['snapshot']
            if (snapshot.get('candidate_name') != previous.get('candidate_name')
                    or snapshot.get('job_title') != previous.get('job_title')
                    or snapshot.get('job_id') != previous.get('job_id')):
                raise ValueError('adopted_conversation_identity_mismatch')
            old_id = r['conversation']
            new_id = snapshot['conversation_id']
            if old_id == new_id:
                return {'state': 'unchanged', 'conversation_id': new_id}
            old = db.execute('SELECT * FROM conversations WHERE id=?', (old_id,)).fetchone()
            current = db.execute('SELECT * FROM conversations WHERE id=?', (new_id,)).fetchone()
            manual = max(int(old['manual']) if old else 0,
                         int(current['manual']) if current else 0)
            rounds = max(int(old['rounds']) if old else 0,
                         int(current['rounds']) if current else 0)
            reason = ((current['reason'] if current else '')
                      or (old['reason'] if old else ''))
            db.execute('DELETE FROM conversations WHERE id IN (?,?)', (old_id, new_id))
            db.execute(
                'INSERT INTO conversations(id,job,version,snapshot,manual,reason,rounds) '
                'VALUES(?,?,?,?,?,?,?)',
                (new_id, snapshot['job_id'], version,
                 json.dumps(snapshot, ensure_ascii=False), manual, reason, rounds))
            for receipt in db.execute(
                    'SELECT message,request FROM receipts WHERE conversation=?',
                    (old_id,)).fetchall():
                db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)',
                           (receipt['message'], new_id, snapshot['job_id'],
                            receipt['request']))
            db.execute('DELETE FROM receipts WHERE conversation=?', (old_id,))
            payload.update(snapshot=snapshot, version=version)
            db.execute(
                'UPDATE requests SET conversation=?,version=?,payload=?,updated=? '
                'WHERE id=?',
                (new_id, version, json.dumps(payload, ensure_ascii=False),
                 time.time(), request_id))
        return {'state': 'adopted', 'conversation_id': new_id}

    def ready(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM requests WHERE state='ready' ORDER BY created LIMIT 20").fetchall()
            return [dict(r) for r in rows]

    def dispatch(self, request_id, fresh, adapter, enabled=False):
        self.validate(fresh)
        with self.db() as db:
            r = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if not r or r['state'] != 'ready':
                return {'state': 'not_ready'}
            c = db.execute('SELECT * FROM conversations WHERE id=?', (r['conversation'],)).fetchone()
            control = db.execute("SELECT value FROM settings WHERE key='control'").fetchone()[0]
            if not enabled or control != 'running' or c['manual']:
                return {'state': 'blocked'}
            if (fresh['conversation_id'] != r['conversation'] or fresh['job_id'] != c['job']
                    or self.version(fresh) != r['version'] or fresh.get('draft', '').strip()):
                db.execute("UPDATE requests SET state='stale',reason='pre_send_context_changed' WHERE id=?", (request_id,))
                return {'state': 'stale'}
            answer = json.loads(r['answer'])
            # Commit intention BEFORE dispatch. If interrupted, recover() marks unknown.
            db.execute("UPDATE requests SET state='sending',updated=? WHERE id=?", (time.time(), request_id))
        try:
            segments = answer.get('segments') or [answer['text']]
            existing_ids = {m['id'] for m in fresh['messages']}
            if len(segments) > 1 and hasattr(adapter, 'send_segments'):
                receipt = adapter.send_segments(fresh, segments)
                message_ids = receipt.get('message_ids') or []
                valid = (
                    receipt.get('state') == 'sent'
                    and receipt.get('conversation_id') == r['conversation']
                    and receipt.get('job_id') == c['job']
                    and receipt.get('texts') == segments
                    and len(message_ids) == len(segments)
                    and all(message_id and message_id not in existing_ids
                            for message_id in message_ids))
            else:
                receipt = adapter.send(fresh, answer['text'])
                message_ids = [receipt.get('message_id')]
                valid = (
                    receipt.get('state') == 'sent'
                    and receipt.get('conversation_id') == r['conversation']
                    and receipt.get('job_id') == c['job']
                    and receipt.get('text') == answer['text']
                    and receipt.get('message_id')
                    and receipt.get('message_id') not in existing_ids)
            if not valid:
                raise ValueError('send_receipt_not_verified')
            with self.db() as db:
                for message_id in message_ids:
                    db.execute('INSERT INTO receipts VALUES(?,?,?,?)',
                               (message_id, r['conversation'], c['job'], request_id))
                db.execute("UPDATE requests SET state='sent',updated=? WHERE id=?", (time.time(), request_id))
                db.execute('UPDATE conversations SET rounds=rounds+1 WHERE id=?', (r['conversation'],))
            return {'state': 'sent', 'request_id': request_id}
        except Exception as exc:
            with self.db() as db:
                reason = str(exc) or type(exc).__name__
                db.execute("UPDATE requests SET state='unknown',reason=?,updated=? WHERE id=?", (reason, time.time(), request_id))
                db.execute("UPDATE settings SET value='paused' WHERE key='control'")
            return {'state': 'unknown', 'request_id': request_id}

    def resolve(self, request_id, outcome, message_id=''):
        if outcome not in ('sent', 'not_sent') or (outcome == 'sent' and not message_id):
            raise ValueError('explicit_platform_outcome_required')
        with self.db() as db:
            r = db.execute("SELECT * FROM requests WHERE id=? AND state='unknown'", (request_id,)).fetchone()
            if not r:
                raise ValueError('unknown_send_not_found')
            if outcome == 'sent':
                payload = json.loads(r['payload'])
                db.execute('INSERT INTO receipts VALUES(?,?,?,?)', (message_id, r['conversation'], payload['snapshot']['job_id'], request_id))
                db.execute('UPDATE conversations SET rounds=rounds+1 WHERE id=?', (r['conversation'],))
            # Even confirmed not_sent is NOT retried automatically.
            db.execute('UPDATE requests SET state=?,reason=? WHERE id=?',
                       ('sent' if outcome == 'sent' else 'cancelled', 'user_verified', request_id))
        return {'state': outcome, 'request_id': request_id}
