"""Local per-conversation BOSS chat history, contacts and report data."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path


DEFAULT_ROOT = Path.home() / 'Desktop/自动招呼/03 回复记录'
ACCOUNT_FALLBACK = '未命名账号'
_LOCK = threading.Lock()

_WECHAT = re.compile(
    r'(?:微信|wechat|weixin|vx|wx|v信|威信|薇信)'
    r'\s*(?:号|账号|id|是|为)?\s*[:：=]?\s*'
    r'([A-Za-z][A-Za-z0-9_-]{5,19})',
    re.IGNORECASE,
)
_PHONE = re.compile(r'(?<!\d)(1[3-9]\d(?:[ -]?\d){8})(?!\d)')
_RESUME_ASK = re.compile(r'(?:发我|发一份|发送|发过来|给我).{0,6}简历')
_RESUME_EXCHANGE = re.compile(
    r'(?:简历.{0,12}(?:发|传|给|附件|查收|收到|下载|上传|交换)|'
    r'(?:发|传|给|附件|查收|收到|下载|上传|交换).{0,12}简历)'
)


def records_root(root=None):
    return Path(
        root or os.environ.get('BOSS_REPLY_RECORDS_DIR') or DEFAULT_ROOT
    ).expanduser()


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _safe(value, fallback='未命名'):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', str(value or '')).strip(' .')
    return text[:80] or fallback


def _display_time(value):
    try:
        return datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return value or ''


def _message_role(message):
    return message.get('role') if isinstance(message, dict) else ''


def _phone_value(text):
    match = _PHONE.search(text or '')
    if not match:
        return ''
    digits = re.sub(r'\D', '', match.group(1))
    return digits if len(digits) == 11 else ''


def _wechat_value(text):
    match = _WECHAT.search(text or '')
    if not match:
        return ''
    value = match.group(1).strip('_-')
    return value if len(value) >= 6 else ''


def _resume_value(text, role):
    text = text or ''
    if '简历' not in text:
        return ''
    if role == 'recruiter' and (
            _RESUME_ASK.search(text)
            or re.search(r'简历.{0,6}(?:发我|给我|发一份|发过来)', text)):
        return ''
    if role == 'candidate' and re.search(r'(?:发我|给我).{0,6}简历', text):
        return ''
    if role in ('candidate', 'recruiter') and _RESUME_EXCHANGE.search(text):
        return '已交换简历'
    if role == 'candidate' and re.search(r'(?:这|那就)?是?我的简历|简历如下|简历在这里', text):
        return '已交换简历'
    return ''


def _set_contact(contacts, key, value, role, at, evidence):
    if not value:
        return
    current = contacts.get(key) or {}
    if current.get('value') and current.get('role') == 'candidate':
        return
    if current.get('value') == value and current.get('role') == role:
        return
    contacts[key] = {
        'value': value,
        'role': role,
        'at': at,
        'evidence': (evidence or '')[:160],
    }


def extract_contacts(messages, existing=None):
    contacts = json.loads(json.dumps(existing or {}, ensure_ascii=False))
    last_phone = ''
    for message in messages:
        text = message.get('text') or ''
        role = message.get('role') or ''
        at = message.get('first_seen') or ''
        phone = _phone_value(text)
        if phone:
            last_phone = phone
        wechat = _wechat_value(text)
        same_number = re.search(r'微信.{0,6}(?:同号|同手机|一样)', text)
        if not wechat and (phone or last_phone) and same_number:
            wechat = phone or last_phone
        _set_contact(contacts, 'wechat', wechat, role, at, text)
        _set_contact(contacts, 'phone', phone, role, at, text)
        resume = _resume_value(text, role)
        if resume:
            _set_contact(contacts, 'resume', resume, role, at, text)
    return contacts


def _empty_record(snapshot, account):
    return {
        'version': 1,
        'conversation_id': snapshot.get('conversation_id', ''),
        'account': account or ACCOUNT_FALLBACK,
        'candidate_name': snapshot.get('candidate_name', ''),
        'job_id': snapshot.get('job_id', ''),
        'knowledge_position': snapshot.get('knowledge_position', ''),
        'job_title': snapshot.get('job_title', ''),
        'first_message_time': '',
        'last_message_time': '',
        'first_message_role': '',
        'initiation': '',
        'messages': [],
        'contacts': {},
        'message_count': 0,
        'reply_count': 0,
        'reply_status': 'complete',
        'handoff': None,
    }


def _record_path(root, account, snapshot):
    directory = records_root(root) / 'conversations' / _safe(account, ACCOUNT_FALLBACK)
    candidate = _safe(snapshot.get('candidate_name'), '')
    job = _safe(snapshot.get('job_title') or snapshot.get('job_id'), '')
    if candidate and job:
        key = hashlib.sha1(
            f'{account}|{candidate}|{job}'.encode('utf-8')
        ).hexdigest()[:10]
        return directory / f'{candidate}__{job}__{key}.json'
    return directory / (_safe(snapshot.get('conversation_id'), 'conversation') + '.json')


def _markdown(record):
    contacts = record.get('contacts') or {}
    lines = [
        f"# {record.get('candidate_name') or '候选人'} · {record.get('job_title') or '未标注岗位'}",
        '',
        f"- BOSS 账号：{record.get('account') or ACCOUNT_FALLBACK}",
        f"- 会话 ID：{record.get('conversation_id') or ''}",
        f"- 首次沟通：{_display_time(record.get('first_message_time'))}",
        f"- 消息发起方：{record.get('initiation') or '未判断'}",
        f"- 微信：{(contacts.get('wechat') or {}).get('value') or '否'}",
        f"- 手机号：{(contacts.get('phone') or {}).get('value') or '否'}",
        f"- 简历：{(contacts.get('resume') or {}).get('value') or '否'}",
        f"- 消息数：{record.get('message_count', 0)}",
        f"- 我方回复：{record.get('reply_count', 0)}",
        '',
        '## 聊天记录',
        '',
    ]
    for index, message in enumerate(record.get('messages') or [], 1):
        role = '候选人' if message.get('role') == 'candidate' else '我方'
        text = (message.get('text') or '').replace('\n', ' / ')
        lines.append(
            f"{index}. {_display_time(message.get('first_seen'))} {role}：{text}"
        )
    handoff = record.get('handoff') or {}
    if handoff:
        lines.extend([
            '',
            '## 未完全回复 / 需人工',
            '',
            f"- 问题：{handoff.get('question') or '未记录'}",
            f"- 原因：{handoff.get('reason') or '未记录'}",
            f"- 时间：{_display_time(handoff.get('at'))}",
        ])
    return '\n'.join(lines).rstrip() + '\n'


class Recorder:
    """Merge one conversation snapshot into a durable local JSON/Markdown record."""

    def __init__(self, account='', root=None):
        self.account = str(account or ACCOUNT_FALLBACK).strip() or ACCOUNT_FALLBACK
        self.root = records_root(root)

    def _load(self, path):
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def record(self, snapshot, first_seen_hint='', handoff=None):
        if not isinstance(snapshot, dict) or not snapshot.get('conversation_id'):
            return {'state': 'ignored', 'reason': 'snapshot_missing_conversation'}
        cid = str(snapshot['conversation_id'])
        path = _record_path(self.root, self.account, snapshot)
        legacy = path.parent / (_safe(cid, 'conversation') + '.json')
        if not path.exists() and legacy.exists():
            path = legacy
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            record = self._load(path) or _empty_record(snapshot, self.account)
            now = _now()
            changed = False
            messages = record.get('messages') or []
            incoming = [
                message for message in (snapshot.get('messages') or [])
                if isinstance(message, dict) and message.get('id')
                and message.get('role') in ('candidate', 'recruiter')
            ]
            old_keys = [
                (message.get('role'), message.get('text') or '')
                for message in messages
            ]
            new_keys = [
                (message.get('role'), message.get('text') or '')
                for message in incoming
            ]
            matcher = SequenceMatcher(None, old_keys, new_keys, autojunk=False)
            matched = set()
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    existing = messages[block.a + offset]
                    message = incoming[block.b + offset]
                    matched.add(block.b + offset)
                    if existing.get('id') != message['id']:
                        existing['id'] = message['id']
                        changed = True
            by_id = {
                message.get('id'): message for message in messages
                if message.get('id')
            }
            for index, message in enumerate(incoming):
                if index in matched:
                    continue
                existing = by_id.get(message['id'])
                if existing is None:
                    existing = {
                        'id': message['id'],
                        'role': message.get('role'),
                        'text': message.get('text') or '',
                        'kind': message.get('kind') or 'text',
                        'first_seen': now,
                    }
                    messages.append(existing)
                    by_id[message['id']] = existing
                    changed = True
                elif (existing.get('role') != message.get('role')
                      or existing.get('text') != (message.get('text') or '')
                      or existing.get('kind') != (message.get('kind') or 'text')):
                    existing.update(
                        role=message.get('role'),
                        text=message.get('text') or '',
                        kind=message.get('kind') or 'text',
                    )
                    changed = True
            first = next(
                (message for message in messages if _message_role(message) in ('candidate', 'recruiter')),
                None,
            )
            first_role = _message_role(first)
            initiation = (
                '被动招呼' if first_role == 'candidate'
                else '主动招呼' if first_role == 'recruiter'
                else ''
            )
            existing_first = record.get('first_message_time') or ''
            if first_seen_hint and (not existing_first or first_seen_hint < existing_first):
                first_time = first_seen_hint
            else:
                first_time = existing_first or (
                    first.get('first_seen') if first else now)
            metadata = {
                'conversation_id': snapshot.get('conversation_id')
                or record.get('conversation_id') or cid,
                'account': self.account,
                'candidate_name': snapshot.get('candidate_name') or record.get('candidate_name') or '',
                'job_id': snapshot.get('job_id') or record.get('job_id') or '',
                'knowledge_position': snapshot.get('knowledge_position')
                or record.get('knowledge_position') or '',
                'job_title': snapshot.get('job_title') or record.get('job_title') or '',
                'first_message_time': first_time,
                'last_message_time': messages[-1].get('first_seen') if messages else now,
                'first_message_role': first_role,
                'initiation': initiation,
                'messages': messages,
                'message_count': len(messages),
                'reply_count': sum(
                    1 for message in messages if _message_role(message) == 'recruiter'
                ),
            }
            effective_handoff = handoff or record.get('handoff')
            metadata['handoff'] = effective_handoff or None
            metadata['reply_status'] = 'needs_human' if effective_handoff else 'complete'
            for key, value in metadata.items():
                if record.get(key) != value:
                    record[key] = value
                    changed = True
            contacts = extract_contacts(messages, record.get('contacts'))
            if contacts != record.get('contacts'):
                record['contacts'] = contacts
                changed = True
            if not changed and path.exists():
                return {'state': 'unchanged', 'path': str(path), 'record': record}
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            os.replace(temporary, path)
            markdown_path = path.with_suffix('.md')
            markdown_temporary = markdown_path.with_name(markdown_path.name + '.tmp')
            markdown_temporary.write_text(_markdown(record), encoding='utf-8')
            os.replace(markdown_temporary, markdown_path)
            return {'state': 'recorded', 'path': str(path), 'record': record}


def load_all(root=None):
    base = records_root(root) / 'conversations'
    if not base.exists():
        return []
    records = []
    for path in base.rglob('*.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict) or not value.get('conversation_id'):
            continue
        value['_path'] = str(path)
        records.append(value)
    records.sort(
        key=lambda item: item.get('first_message_time') or item.get('last_message_time') or '',
        reverse=True,
    )
    return records


def metrics(records):
    records = records or []
    return {
        'conversations': len(records),
        'replied': sum(1 for record in records if int(record.get('reply_count') or 0) > 0),
        'messages': sum(int(record.get('message_count') or 0) for record in records),
        'wechat': sum(1 for record in records if (record.get('contacts') or {}).get('wechat', {}).get('value')),
        'phone': sum(1 for record in records if (record.get('contacts') or {}).get('phone', {}).get('value')),
        'resume': sum(1 for record in records if (record.get('contacts') or {}).get('resume', {}).get('value')),
        'needs_human': sum(1 for record in records if record.get('handoff')),
        'passive': sum(1 for record in records if record.get('initiation') == '被动招呼'),
        'active': sum(1 for record in records if record.get('initiation') == '主动招呼'),
    }


def report_data(root=None):
    records = load_all(root)
    for record in records:
        record.pop('_path', None)
    return {
        'generated_at': _now(),
        'records': records,
        'metrics': metrics(records),
    }


def backfill_from_store(db_path, account='', root=None):
    """Merge already-validated SQLite conversation snapshots into the record folder."""
    recorder = Recorder(account, root)
    snapshots = []
    hints = {}
    db = None
    try:
        db = sqlite3.connect(Path(db_path), timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT snapshot FROM conversations').fetchall()
        try:
            for row in db.execute(
                    'SELECT conversation,min(created) created FROM requests GROUP BY conversation'):
                if row['created'] is not None:
                    hints[row['conversation']] = datetime.fromtimestamp(
                        float(row['created'])).astimezone().isoformat(timespec='seconds')
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return {'state': 'store_unavailable', 'snapshots': 0,
                'records': len(load_all(root))}
    finally:
        if db is not None:
            db.close()
    for row in rows:
        try:
            snapshot = json.loads(row['snapshot'])
        except (TypeError, ValueError):
            continue
        if isinstance(snapshot, dict) and snapshot.get('conversation_id') and snapshot.get('messages'):
            snapshots.append(snapshot)
    errors = []
    groups = {}
    for snapshot in snapshots:
        key = (
            snapshot.get('candidate_name') or '',
            snapshot.get('job_title') or snapshot.get('job_id') or '',
        )
        if not all(key):
            key = (snapshot.get('conversation_id'), '')
        groups.setdefault(key, []).append(snapshot)
    for group in groups.values():
        group.sort(key=lambda value: len(value.get('messages') or []))
        possible = [
            hints[value.get('conversation_id')]
            for value in group
            if value.get('conversation_id') in hints
        ]
        hint = min(possible) if possible else ''
        for snapshot in group:
            try:
                recorder.record(snapshot, first_seen_hint=hint)
            except Exception as exc:
                errors.append(str(exc))
    return {
        'state': 'backfilled',
        'snapshots': len(snapshots),
        'records': len(load_all(root)),
        'errors': errors[:3],
    }


def history(candidate='', job='', conversation='', root=None, limit=20):
    records = load_all(root)
    candidate = str(candidate or '').strip().lower()
    job = str(job or '').strip().lower()
    conversation = str(conversation or '').strip()
    matched = []
    for record in records:
        if conversation and record.get('conversation_id') != conversation:
            continue
        if candidate and candidate not in str(record.get('candidate_name') or '').lower():
            continue
        if job and job not in (
            str(record.get('job_title') or '') + ' ' + str(record.get('job_id') or '')
        ).lower():
            continue
        matched.append(record)
    return matched[:max(1, int(limit or 20))]

