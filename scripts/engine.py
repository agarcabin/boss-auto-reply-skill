"""Durable, independent reply queue. No browser or model dependencies."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
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


def candidate_question(snapshot):
    dialogue = [
        message for message in (snapshot.get('messages') or [])
        if message.get('role') != 'system'
    ]
    trailing = []
    for message in reversed(dialogue):
        if message.get('role') != 'candidate':
            break
        trailing.insert(0, message.get('text') or '')
    return '\n'.join(text for text in trailing if text).strip()[:2000]


# Latches raised by the sender itself may be cleared by an explicit platform
# re-check. Human drafts, human outbound messages and manual takeovers may not.
AUTOMATION_MANUAL_REASONS = ('send_result_unverified', 'reply_partially_sent')


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
            CREATE TABLE IF NOT EXISTS exchanges(
              conversation TEXT NOT NULL, kind TEXT NOT NULL,
              count INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
              PRIMARY KEY(conversation,kind));
            CREATE TABLE IF NOT EXISTS handoffs(
              conversation TEXT PRIMARY KEY, question TEXT NOT NULL DEFAULT '',
              reason TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
              request TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
              updated REAL NOT NULL);
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
            # An interrupted send must be verified against the platform, but that
            # happens in the listener before any new send. Blocking resume here
            # left the operator unable to continue after a slow native timeout.
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
                       'messages': s['messages'], 'draft': s.get('draft', ''),
                       'non_text_last': bool(s.get('non_text_last'))})

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
                previous = json.loads(old['snapshot'])
                dialogue = [m for m in previous.get('messages', []) if m.get('role') != 'system']
                return {'state': 'unchanged', 'new_candidate': False,
                        'last_role': dialogue[-1]['role'] if dialogue else ''}
            previous = json.loads(old['snapshot'])
            db.execute('UPDATE conversations SET job=?,version=?,snapshot=? WHERE id=?',
                       (job, version, json.dumps(snapshot, ensure_ascii=False), cid))
            db.execute("UPDATE requests SET state='stale',reason='context_changed',updated=? WHERE conversation=? AND state IN ('pending','ready')", (now, cid))
            if baseline_only:
                return {'state': 'baseline', 'new_candidate': False,
                        'last_role': previous['messages'][-1]['role'] if previous.get('messages') else ''}
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
            elif snapshot.get('non_text_last'):
                reason = 'non_text_message_needs_human'
            elif unexpected:
                reason = 'human_outbound_detected'
            elif max_rounds and old['rounds'] >= max_rounds:
                reason = 'automatic_round_limit'
            if reason:
                db.execute('UPDATE conversations SET manual=1,reason=? WHERE id=?', (reason, cid))
                dialogue = [m for m in snapshot['messages'] if m['role'] != 'system']
                return {'state': 'manual', 'reason': reason, 'new_candidate': False,
                        'last_role': dialogue[-1]['role'] if dialogue else ''}
            if old['manual']:
                dialogue = [m for m in snapshot['messages'] if m['role'] != 'system']
                return {'state': 'manual', 'reason': old['reason'], 'new_candidate': False,
                        'last_role': dialogue[-1]['role'] if dialogue else ''}
            dialogue = [m for m in snapshot['messages'] if m['role'] != 'system']
            incoming = [m for m in new if m['role'] == 'candidate']
            last_role = dialogue[-1]['role'] if dialogue else ''
            if not new or not dialogue or last_role != 'candidate':
                return {'state': 'observed', 'new_candidate': bool(incoming),
                        'last_role': last_role}
            if not incoming:
                return {'state': 'observed', 'new_candidate': False,
                        'last_role': last_role}
            # Same event cannot be recreated by a restart or repeated scan.
            trailing = []
            for message in reversed(dialogue):
                if message['role'] != 'candidate':
                    break
                trailing.insert(0, message['id'])
            request_id = digest([cid, job, trailing])
            payload = dict(request_id=request_id, version=version, snapshot=snapshot)
            known = db.execute('SELECT state FROM requests WHERE id=?', (request_id,)).fetchone()
            if known and known['state'] in ('stale', 'cancelled'):
                # Two quick candidate messages can invalidate a queued reply
                # before it is sent. Keep the same durable queue slot but make
                # it live again for the current context instead of silently
                # dropping the conversation.
                db.execute(
                    "UPDATE requests SET state='pending',version=?,payload=?,answer=NULL,"
                    "reason='requeued_latest_context',updated=? WHERE id=?",
                    (version, json.dumps(payload, ensure_ascii=False), now, request_id))
            else:
                db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                           (request_id, cid, version, 'pending', json.dumps(payload, ensure_ascii=False),
                            None, '', now, now))
            return {'state': 'pending', 'request_id': request_id,
                    'new_candidate': True, 'last_role': last_role}

    def pending(self):
        with self.db() as db:
            rows = db.execute("SELECT payload FROM requests WHERE state='pending' ORDER BY created LIMIT 20").fetchall()
            return [json.loads(r[0]) for r in rows]

    def next_queued_name(self, exclude=''):
        with self.db() as db:
            rows = db.execute(
                "SELECT payload FROM requests WHERE state IN ('pending','ready') "
                "ORDER BY created LIMIT 20").fetchall()
        for row in rows:
            try:
                payload = json.loads(row['payload'])
            except (TypeError, ValueError):
                continue
            snapshot = payload.get('snapshot') or {}
            if (snapshot.get('conversation_id') or '') == exclude:
                continue
            name = (snapshot.get('candidate_name') or '').strip()
            if name:
                return name
        return ''

    def prune_jobs(self, allowed_jobs):
        """Cancel queued requests whose page job is no longer enabled."""
        allowed = set(allowed_jobs or [])
        removed = []
        with self.db() as db:
            rows = db.execute(
                "SELECT id,payload FROM requests WHERE state IN ('pending','ready')").fetchall()
            for row in rows:
                try:
                    job = json.loads(row['payload'])['snapshot']['job_id']
                except (KeyError, TypeError, ValueError):
                    continue
                if job in allowed:
                    continue
                db.execute(
                    "UPDATE requests SET state='cancelled',reason='job_disabled_by_settings',updated=? "
                    "WHERE id=?", (time.time(), row['id']))
                removed.append(row['id'])
        return removed

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
            version = self.version(snapshot)
            payload = {'request_id': request_id, 'version': version, 'snapshot': snapshot}
            now = time.time()
            known = db.execute('SELECT state FROM requests WHERE id=?', (request_id,)).fetchone()
            if known:
                if known['state'] in ('stale', 'cancelled'):
                    db.execute(
                        "UPDATE requests SET state='pending',version=?,payload=?,answer=NULL,"
                        "reason='',updated=? WHERE id=?",
                        (version, json.dumps(payload, ensure_ascii=False), now, request_id))
                    return {'state': 'pending', 'request_id': request_id}
                return {'state': known['state'], 'request_id': request_id}
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                       (request_id, cid, version, 'pending', json.dumps(payload, ensure_ascii=False), None, '', now, now))
        return {'state': 'pending', 'request_id': request_id}

    def requeue_current(self, snapshot, allowed_jobs, max_rounds=0,
                        release_latches=False, force_reply=False):
        """Queue the current candidate question again after an explicit re-check.

        Used by the overlay's "回复当前窗口" button and by unknown-send
        reconciliation. The caller has already read the page, so a trailing
        candidate message means no reply of ours is present.
        """
        self.validate(snapshot)
        cid, job = snapshot['conversation_id'], snapshot['job_id']
        if job not in set(allowed_jobs or []):
            return {'state': 'ignored', 'reason': 'job_not_allowed'}
        if (snapshot.get('draft') or '').strip():
            return {'state': 'manual', 'reason': 'human_draft_present'}
        dialogue = [m for m in snapshot['messages'] if m.get('role') != 'system']
        if not dialogue:
            return {'state': 'observed'}
        last_role = dialogue[-1]['role']
        if last_role != 'candidate' and not force_reply:
            return {'state': 'no_reply_needed', 'last_role': last_role}
        trailing = []
        for message in reversed(dialogue):
            if message['role'] != 'candidate':
                break
            trailing.insert(0, message['id'])
        # A forced reply may repeat even when the last message is ours, so it
        # needs a fresh queue slot instead of colliding with an older receipt.
        request_id = digest(
            [cid, job, trailing, 'forced', time.time_ns()]
            if force_reply else [cid, job, trailing])
        version = self.version(snapshot)
        payload = {'request_id': request_id, 'version': version, 'snapshot': snapshot}
        if force_reply:
            payload['force_reply'] = True
        now = time.time()
        with self.db() as db:
            current = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            if current and current['manual'] and not force_reply:
                reason = current['reason']
                allowed = (reason in AUTOMATION_MANUAL_REASONS
                           or (release_latches and reason in (
                               'human_outbound_detected', 'agent_handoff')))
                if not allowed:
                    return {'state': 'manual', 'reason': reason}
            if (max_rounds and current and current['rounds'] >= max_rounds
                    and not force_reply):
                return {'state': 'manual', 'reason': 'automatic_round_limit'}
            db.execute(
                "UPDATE conversations SET version=?,snapshot=?,manual=0,reason='' "
                "WHERE id=?",
                (version, json.dumps(snapshot, ensure_ascii=False), cid))
            db.execute(
                "UPDATE requests SET state='stale',reason='superseded_by_reply_current',"
                "updated=? WHERE conversation=? AND state IN ('pending','ready') AND id<>?",
                (now, cid, request_id))
            db.execute(
                "UPDATE requests SET state='cancelled',reason='superseded_by_reply_current',"
                "updated=? WHERE conversation=? AND state IN ('sending','unknown') AND id<>?",
                (now, cid, request_id))
            known = db.execute('SELECT state FROM requests WHERE id=?', (request_id,)).fetchone()
            if known and known['state'] == 'sent' and not force_reply:
                return {'state': 'already_replied', 'request_id': request_id}
            if known:
                db.execute(
                    "UPDATE requests SET state='pending',version=?,payload=?,answer=NULL,"
                    "reason='reply_current_requested',updated=? WHERE id=?",
                    (version, json.dumps(payload, ensure_ascii=False), now, request_id))
            else:
                db.execute(
                    'INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                    (request_id, cid, version, 'pending',
                     json.dumps(payload, ensure_ascii=False), None,
                     'reply_current_requested', now, now))
            db.execute('DELETE FROM handoffs WHERE conversation=?', (cid,))
        return {'state': 'pending', 'request_id': request_id}

    def reconcile_unknown(self, read_conversation, allowed_jobs):
        """Verify interrupted sends against the page before resuming.

        A matching platform message is recorded as sent. A trailing candidate
        message proves our reply is absent and is queued again for a fresh
        generation. Anything ambiguous is handed to a human, never retried
        blindly.
        """
        results = []
        with self.db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM requests WHERE state IN ('sending','unknown') "
                "ORDER BY created")]
        for row in rows:
            request_id = row['id']
            if row['state'] == 'sending':
                with self.db() as db:
                    db.execute(
                        "UPDATE requests SET state='unknown',"
                        "reason='process_interrupted_during_send',updated=? WHERE id=?",
                        (time.time(), request_id))
            payload = json.loads(row['payload'])
            previous = payload.get('snapshot') or {}
            try:
                answer = json.loads(row['answer'] or '{}')
            except ValueError:
                answer = {}
            segments = [str(part).strip() for part in
                        (answer.get('segments') or [answer.get('text') or ''])
                        if str(part).strip()]
            try:
                fresh = read_conversation(row['conversation'], previous)
                self.validate(fresh)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                self.record_handoff(
                    row['conversation'], candidate_question(previous),
                    '上次发送结果未能自动核实，请人工确认',
                    'agent_handoff', request_id)
                with self.db() as db:
                    db.execute(
                        "UPDATE requests SET state='cancelled',reason=?,updated=? WHERE id=?",
                        ('unverified:' + reason[:120], time.time(), request_id))
                    db.execute(
                        "UPDATE conversations SET manual=1,reason='send_result_unverified' "
                        "WHERE id=?", (row['conversation'],))
                results.append({'state': 'needs_human', 'request_id': request_id,
                                'reason': reason})
                continue
            old_ids = {m['id'] for m in previous.get('messages') or []}
            new_out = [m for m in fresh['messages']
                       if m['id'] not in old_ids and m['role'] == 'recruiter']
            remaining = list(new_out)
            matched = []
            for segment in segments:
                hit = next((m for m in remaining if m['text'] == segment), None)
                if not hit:
                    break
                matched.append(hit)
                remaining.remove(hit)
            if segments and len(matched) == len(segments):
                with self.db() as db:
                    for message in matched:
                        db.execute(
                            'INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)',
                            (message['id'], row['conversation'], fresh['job_id'],
                             request_id))
                    db.execute(
                        "UPDATE requests SET state='sent',"
                        "reason='reconciled_platform_receipt',updated=? WHERE id=?",
                        (time.time(), request_id))
                    db.execute(
                        "UPDATE conversations SET version=?,snapshot=?,rounds=rounds+1 "
                        "WHERE id=?",
                        (self.version(fresh), json.dumps(fresh, ensure_ascii=False),
                         row['conversation']))
                results.append({'state': 'sent', 'request_id': request_id,
                                'message_ids': [m['id'] for m in matched]})
                continue
            partial = bool(matched)
            dialogue = [m for m in fresh['messages'] if m.get('role') != 'system']
            last_role = dialogue[-1]['role'] if dialogue else ''
            with self.db() as db:
                for message in matched:
                    db.execute(
                        'INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)',
                        (message['id'], row['conversation'], fresh['job_id'],
                         request_id))
                db.execute(
                    "UPDATE requests SET state='cancelled',reason=?,updated=? WHERE id=?",
                    ('partially_sent' if partial else 'verified_not_sent',
                     time.time(), request_id))
            if not partial and last_role == 'candidate':
                queued = self.requeue_current(fresh, allowed_jobs, 0)
                results.append({'state': 'requeued', 'request_id': request_id,
                                'queue_state': queued.get('state'),
                                'queue_request': queued.get('request_id')})
                continue
            self.record_handoff(
                row['conversation'],
                candidate_question(previous) or candidate_question(fresh),
                '部分回复已发送，请人工补齐' if partial
                else '页面最后一条不是候选人消息，发送结果需人工确认',
                'agent_handoff', request_id)
            with self.db() as db:
                db.execute(
                    "UPDATE conversations SET manual=1,reason=?,rounds=rounds+? WHERE id=?",
                    ('reply_partially_sent' if partial else 'send_result_conflict',
                     1 if partial else 0, row['conversation']))
            results.append({'state': 'needs_human', 'request_id': request_id,
                            'sent_segments': len(matched)})
        return results

    def respond(self, request_id, version, action, text='', sources=None,
                segments=None, handoff=None, items=None):
        if action not in ('reply', 'manual'):
            raise ValueError('action_must_be_reply_or_manual')
        if action == 'reply' and (not isinstance(text, str) or not text.strip() or len(text) > 1000):
            raise ValueError('reply_text_invalid')
        with self.db() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if not row or row['state'] != 'pending' or row['version'] != version:
                raise ValueError('request_not_pending_or_version_changed')
            payload = json.loads(row['payload'])
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
            stored = {'text': answer_text, 'segments': answer_segments,
                      'sources': sources or []}
            if items:
                stored['items'] = items
            db.execute('UPDATE requests SET state=?,answer=?,updated=? WHERE id=?',
                       ('ready' if action == 'reply' else 'manual',
                        json.dumps(stored, ensure_ascii=False),
                        time.time(), request_id))
            handoff_row = None
            if action == 'manual' or isinstance(handoff, dict):
                info = handoff if isinstance(handoff, dict) else {}
                handoff_row = (
                    row['conversation'],
                    candidate_question(payload.get('snapshot') or {}),
                    str(info.get('reason') or answer_text or '需要人工判断'),
                    str(info.get('source') or (
                        'agent_handoff' if action == 'manual' else 'knowledge_gap')),
                    request_id,
                )
        if handoff_row:
            self.record_handoff(*handoff_row)
        return {'state': 'ready' if action == 'reply' else 'manual', 'request_id': request_id}

    def record_handoff(self, conversation, question, reason, source, request_id=''):
        now = time.time()
        with self.db() as db:
            row = db.execute(
                'SELECT created FROM handoffs WHERE conversation=?', (conversation,)
            ).fetchone()
            created = row['created'] if row else now
            db.execute(
                'INSERT OR REPLACE INTO handoffs '
                '(conversation,question,reason,source,request,created,updated) '
                'VALUES(?,?,?,?,?,?,?)',
                (conversation, question, reason, source, request_id, created, now),
            )
        return {'state': 'recorded', 'conversation_id': conversation}

    def latest_handoff(self, conversation):
        with self.db() as db:
            row = db.execute(
                'SELECT * FROM handoffs WHERE conversation=?', (conversation,)
            ).fetchone()
        if not row:
            return None
        return {
            'needs_human': True,
            'status': 'needs_human',
            'question': row['question'],
            'reason': row['reason'],
            'source': row['source'],
            'at': datetime.fromtimestamp(row['updated']).astimezone().isoformat(
                timespec='seconds'),
        }

    def is_own_draft(self, text, max_age=1800):
        """True when the editor still holds text this automation just typed.

        BOSS can re-fill the editor for a moment after a successful send. That
        echo must not be mistaken for a human takeover.
        """
        text = (text or '').strip()
        if len(text) < 2:
            return False
        cutoff = time.time() - max_age
        with self.db() as db:
            rows = db.execute(
                "SELECT answer FROM requests WHERE answer IS NOT NULL AND updated>=? "
                "ORDER BY updated DESC LIMIT 40", (cutoff,)).fetchall()
        for row in rows:
            try:
                answer = json.loads(row['answer'])
            except (TypeError, ValueError):
                continue
            candidates = answer.get('segments') or [answer.get('text') or '']
            for candidate in candidates:
                candidate = (candidate or '').strip().rstrip('。').strip()
                if not candidate:
                    continue
                if text == candidate:
                    return True
                if len(text) >= 6 and (candidate.startswith(text)
                                       or text.startswith(candidate)):
                    return True
        return False

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

    def exchange_counts(self, conversation):
        with self.db() as db:
            rows = db.execute(
                'SELECT kind,count FROM exchanges WHERE conversation=?',
                (conversation,)).fetchall()
        return {row['kind']: row['count'] for row in rows}

    def record_exchange(self, conversation, kind):
        now = time.time()
        with self.db() as db:
            row = db.execute(
                'SELECT count FROM exchanges WHERE conversation=? AND kind=?',
                (conversation, kind)).fetchone()
            count = (row['count'] if row else 0) + 1
            db.execute(
                'INSERT OR REPLACE INTO exchanges VALUES(?,?,?,?)',
                (conversation, kind, count, now))
        return count

    def exchange_kind(self, conversation, text):
        """First contact request -> WeChat, second -> phone, then stop."""
        if not re.search(
                r'微信|联系方式|手机号|加个?我|加下我|留个?号|加个?[vV]|'
                r'留.{0,4}电话|电话联系|交换电话|打个?电话',
                text or ''):
            return ''
        counts = self.exchange_counts(conversation)
        if not counts.get('wechat'):
            return 'wechat'
        if not counts.get('phone'):
            return 'phone'
        return ''

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
            existing_ids = {m['id'] for m in fresh['messages']}
            groups = []
            for item in answer.get('items') or []:
                group = [s for s in (item.get('segments') or [item.get('text', '')]) if s]
                if group:
                    groups.append(group)
            if not groups:
                groups = [[s for s in (answer.get('segments') or [answer.get('text', '')]) if s]]
            if not groups or not groups[0]:
                raise ValueError('reply_segments_invalid')
            if len(groups) > 1:
                if not hasattr(adapter, 'send_many'):
                    raise ValueError('multi_reply_not_supported')
                receipt = adapter.send_many(fresh, groups)
                message_ids = receipt.get('message_ids') or []
                expected_texts = [s for group in groups for s in group]
                valid = (
                    receipt.get('state') == 'sent'
                    and receipt.get('conversation_id') == r['conversation']
                    and receipt.get('job_id') == c['job']
                    and receipt.get('texts') == expected_texts
                    and len(message_ids) == len(expected_texts)
                    and all(message_id and message_id not in existing_ids
                            for message_id in message_ids))
            else:
                segments = groups[0]
            if len(groups) == 1 and len(segments) > 1 and hasattr(adapter, 'send_segments'):
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
            elif len(groups) == 1:
                text = segments[0]
                receipt = adapter.send(fresh, text)
                message_ids = [receipt.get('message_id')]
                valid = (
                    receipt.get('state') == 'sent'
                    and receipt.get('conversation_id') == r['conversation']
                    and receipt.get('job_id') == c['job']
                    and receipt.get('text') == text
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
            exchange = self.exchange_kind(r['conversation'], answer.get('text') or '')
            exchange_result = None
            if exchange and hasattr(adapter, 'click_exchange'):
                try:
                    exchange_result = adapter.click_exchange(exchange)
                    self.record_exchange(r['conversation'], exchange)
                except Exception as exc:
                    exchange_result = {
                        'state': 'error', 'kind': exchange,
                        'error': str(exc) or type(exc).__name__,
                    }
            return {'state': 'sent', 'request_id': request_id,
                    'exchange': exchange_result}
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
