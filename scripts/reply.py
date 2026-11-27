"""CLI and unattended listener for boss-auto-reply (Python standard library)."""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from engine import Store
from cloud import Cloud

ROOT = Path(__file__).resolve().parents[1]
# Windows Store Python virtualizes AppData; native PowerShell cannot see those files.
# A user-owned .codex directory is shared by Python, native helpers and the mapping UI.
STATE = Path(os.environ.get('BOSS_REPLY_STATE_DIR') or Path.home() / '.codex/state/boss-auto-reply')
REPLY_CONFIG_ROOT = Path.home() / 'Desktop/自动招呼/04 回复配置'
LEGACY_CONFIG_ROOT = Path.home() / 'Desktop/自动招呼/03 岗位配置'
REPLY_RECORDS_ROOT = Path.home() / 'Desktop/自动招呼/03 回复记录'
ACCOUNT_CONFIG_FIELDS = (
    'reply_style', 'poll_seconds', 'max_rounds',
    'allowed_jobs', 'job_aliases', 'job_enabled',
    'enabled_jobs', 'disabled_jobs', 'daily_mapping_file',
)
ACCOUNT_CONFIG_DEFAULTS = {
    'reply_style': '自然亲切',
    'poll_seconds': 5,
    'max_rounds': 0,
    'allowed_jobs': [],
    'job_aliases': {},
    'job_enabled': {},
    'enabled_jobs': [],
    'disabled_jobs': [],
    'daily_mapping_file': '',
}


def output(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    if path.resolve() == (STATE / 'config.json').resolve() and isinstance(value, dict):
        value['version'] = 2
        account = str(value.get('account') or '').strip()
        if account:
            value.setdefault('accounts', {})[account] = {
                field: copy.deepcopy(value.get(field, ACCOUNT_CONFIG_DEFAULTS[field]))
                for field in ACCOUNT_CONFIG_FIELDS
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def transient_native_error(exc):
    message = str(exc)
    return message in {'COMError'} or any(marker in message for marker in (
        'NULL COM pointer access',
        'bound_chat_or_boss_not_visible',
        'message_page_not_ready',
        'message_page_not_opened',
        'boss_page_navigation_not_confirmed',
        'native_control_not_unique',
        'inbox_scroll_depth_not_verified',
        'inbox_row_not_readable',
        'inbox_scan_incomplete',
    ))


def clear_own_draft(native, store):
    """Remove an editor echo that exactly matches a reply we already produced."""
    try:
        draft = (native.call('draft_text') or {}).get('text') or ''
    except Exception:
        return False
    if not draft.strip() or not store.is_own_draft(draft):
        return False
    try:
        native.call('clear_draft', text=draft)
        return True
    except Exception:
        return False


def overlay_label(snapshot):
    """Candidate + short cloud job name, without the leading project number."""
    name = (snapshot.get('candidate_name') or '').strip()
    job = (snapshot.get('job_id') or snapshot.get('job_title') or '').strip()
    job = re.sub(r'^\d+\s*', '', job)
    if name and job:
        return f'{name}（{job}）'
    return name


def config():
    value = load(STATE / 'config.json')
    changed = False
    config_root = str(value.get('job_config_root') or '')
    if config_root and Path(config_root).name == LEGACY_CONFIG_ROOT.name:
        value['job_config_root'] = str(REPLY_CONFIG_ROOT)
        changed = True
    preferences_path = str(value.get('cloud_mapping_preferences') or '')
    if preferences_path and Path(preferences_path).parent.name == LEGACY_CONFIG_ROOT.name:
        value['cloud_mapping_preferences'] = str(
            REPLY_CONFIG_ROOT / Path(preferences_path).name)
        changed = True
    if not value.get('reply_records_root'):
        value['reply_records_root'] = str(REPLY_RECORDS_ROOT)
        changed = True
    if value.get('version', 1) < 2:
        value['version'] = 2
        value.setdefault('accounts', {})
        account = str(value.get('account') or '').strip()
        if account:
            value['accounts'][account] = {
                field: copy.deepcopy(value.get(field, ACCOUNT_CONFIG_DEFAULTS[field]))
                for field in ACCOUNT_CONFIG_FIELDS
            }
        changed = True
    if changed:
        save(STATE / 'config.json', value)
    return value


def switch_account(cfg, account):
    account = str(account or '').strip()
    if not account:
        raise ValueError('boss_account_name_required')
    cfg.setdefault('accounts', {})
    current = str(cfg.get('account') or '').strip()
    if current and current != account:
        cfg['accounts'][current] = {
            field: copy.deepcopy(cfg.get(field, ACCOUNT_CONFIG_DEFAULTS[field]))
            for field in ACCOUNT_CONFIG_FIELDS
        }
    saved = cfg['accounts'].get(account) or {}
    for field in ACCOUNT_CONFIG_FIELDS:
        cfg[field] = copy.deepcopy(saved.get(field, ACCOUNT_CONFIG_DEFAULTS[field]))
    cfg['version'] = 2
    cfg['account'] = account
    save(STATE / 'config.json', cfg)
    return cfg


def sync_job_selection(cfg):
    """Make the latest mapping-window checkbox state the reply allowlist."""
    path = Path(
        cfg.get('cloud_mapping_preferences')
        or (Path(cfg.get('job_config_root') or REPLY_CONFIG_ROOT) / '岗位对应关系.json')
    )
    if not path.is_file():
        return False
    data = load(path)
    account = str(cfg.get('account') or '')
    records = (
        (data.get('accounts') or {}).get(account, {}).get('records') or []
    )
    if not records:
        return False
    job_enabled = {}
    enabled_jobs = []
    disabled_jobs = []
    allowed_jobs = set()
    job_aliases = {}
    for record in records:
        title = str(record.get('boss_title') or '').strip()
        if not title:
            continue
        enabled = bool(record.get('enabled'))
        job_enabled[title] = enabled
        company = str(record.get('cloud_company') or '').strip()
        position = str(record.get('cloud_position') or '').strip()
        if enabled and company and position:
            enabled_jobs.append(title)
            allowed_jobs.add(company)
            job_aliases[title] = company
        else:
            disabled_jobs.append(title)
    changes = {
        'job_enabled': dict(sorted(job_enabled.items())),
        'enabled_jobs': sorted(enabled_jobs),
        'disabled_jobs': sorted(disabled_jobs),
        'allowed_jobs': sorted(allowed_jobs),
        'job_aliases': dict(sorted(job_aliases.items())),
        'job_selection_source': str(path),
    }
    changed = False
    for key, value in changes.items():
        if cfg.get(key) != value:
            cfg[key] = value
            changed = True
    mapping_path = Path(cfg.get('daily_mapping_file') or '')
    if mapping_path.is_file():
        mapping = load(mapping_path)
        mapping_changed = False
        for row in mapping.get('mappings') or []:
            title = str(row.get('title') or '').strip()
            if title in job_enabled and bool(row.get('enabled')) != job_enabled[title]:
                row['enabled'] = job_enabled[title]
                mapping_changed = True
        for key in ('job_enabled', 'enabled_jobs', 'disabled_jobs'):
            value = changes[key]
            if mapping.get(key) != value:
                mapping[key] = value
                mapping_changed = True
        new_root = str(cfg.get('job_config_root') or REPLY_CONFIG_ROOT)
        if mapping.get('config_root') != new_root:
            mapping['config_root'] = new_root
            mapping_changed = True
        if mapping_changed:
            save(mapping_path, mapping)
    return changed


def refresh_live_settings(cfg):
    """Apply a settings save to a running listener without native re-calibration."""
    latest = config()
    if latest.get('account') != cfg.get('account'):
        return False
    revision = str(latest.get('automation_revision') or '')
    if not revision or revision == str(cfg.get('automation_revision') or ''):
        return False
    for field in ('allowed_jobs', 'job_aliases', 'job_enabled', 'enabled_jobs',
                  'disabled_jobs', 'daily_mapping_file', 'reply_style',
                  'needs_verification_reply', 'poll_seconds', 'max_rounds', 'mode'):
        if field in latest:
            cfg[field] = copy.deepcopy(latest[field])
    cfg['automation_revision'] = revision
    return True


def ps_host():
    host = shutil.which('pwsh')
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
    return host or (str(bundled) if bundled.is_file() else None)


def defaults():
    return {
        'version': 1, 'mode': 'agent', 'poll_seconds': 5, 'max_rounds': 3,
        'cloud_url': 'https://new.jiebo.vip/server/', 'cloud_username': '小八',
        'reply_style': '自然亲切',
        'needs_verification_reply': True,
        'job_config_root': str(REPLY_CONFIG_ROOT),
        'reply_records_root': str(REPLY_RECORDS_ROOT),
        'allowed_jobs': [], 'job_aliases': {}, 'job_enabled': {},
        'enabled_jobs': [], 'disabled_jobs': [], 'binding': None,
        'profile': {
            'verified': False, 'stable_ids_verified': False,
            'draft_verified': False, 'send_verified': False,
            'first_send_verification_required': False,
            'layout_mode': 'text_sequence', 'loaded_list_complete_verified': False,
            'list_class': '', 'item_class': '', 'unread_class': '', 'selected_class': '',
            'contact_name_class': '', 'message_root_class': '', 'message_class': '',
            'message_text_class': '', 'job_class': '', 'name_class': '', 'editor_class': '',
            'send_class': '', 'send_name': '', 'incoming_classes': ['item-friend'],
            'outgoing_classes': ['item-myself'],
            'system_classes': ['system-message', 'message-card-wrap', 'item-system'],
        },
    }


class Native:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, action, **kwargs):
        native_python = Path(self.cfg.get('native_python') or ROOT / 'runtime/env/Scripts/python.exe')
        if os.name != 'nt' or not native_python.is_file():
            raise ValueError('windows_native_uia3_runtime_required')
        STATE.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='native-', suffix='.json', dir=STATE)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'action': action, 'config': self.cfg, **kwargs}, f, ensure_ascii=False)
            result = subprocess.run([str(native_python), '-X', 'utf8',
                                     str(ROOT / 'scripts/native_uia.py'), name],
                                    capture_output=True, text=True, encoding='utf-8-sig',
                                    errors='replace', timeout=50,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                value = json.loads(result.stdout)
            except ValueError:
                raise RuntimeError('native_bridge_failed') from None
            if value.get('error'):
                raise RuntimeError(value['error'])
            if result.returncode:
                raise RuntimeError('native_bridge_failed')
            return value
        finally:
            Path(name).unlink(missing_ok=True)

    def scan(self, deep=False):
        return self.call('scan', deep=deep)

    def read(self, cid, candidate_name=None, job_title=None):
        return self.call(
            'read', conversation_id=cid,
            candidate_name=candidate_name, job_title=job_title)['snapshot']

    def send(self, snapshot, text):
        result = self.call('send', expected=snapshot, text=text)
        if result.get('state') == 'sent':
            latest = config()
            if latest.get('account') == self.cfg.get('account'):
                latest['profile']['send_verified'] = True
                latest['profile']['first_send_verification_required'] = False
                save(STATE / 'config.json', latest)
        return result

    def send_segments(self, snapshot, segments):
        result = self.call('send_segments', expected=snapshot, segments=segments)
        if result.get('state') == 'sent':
            latest = config()
            if latest.get('account') == self.cfg.get('account'):
                latest['profile']['send_verified'] = True
                latest['profile']['first_send_verification_required'] = False
                save(STATE / 'config.json', latest)
        return result

    def send_many(self, snapshot, groups):
        result = self.call('send_many', expected=snapshot, groups=groups)
        if result.get('state') == 'sent':
            latest = config()
            if latest.get('account') == self.cfg.get('account'):
                latest['profile']['send_verified'] = True
                latest['profile']['first_send_verification_required'] = False
                save(STATE / 'config.json', latest)
        return result

    def click_exchange(self, kind):
        return self.call('click_exchange', kind=kind)

    def accept_exchange_request(self):
        return self.call('accept_exchange_request')

    def test_send(self, expected_name, text):
        result = self.call('test_send', expected_name=expected_name, text=text)
        if result.get('state') == 'test_sent':
            latest = config()
            if latest.get('account') == self.cfg.get('account'):
                latest['profile']['send_verified'] = True
                latest['profile']['first_send_verification_required'] = False
                save(STATE / 'config.json', latest)
        return result


def jobs(cfg):
    catalog = Cloud(cfg).catalog()
    return {j['name']: {'id': j['name'], 'title': j['name'], 'chunks': j['chunks'], 'positions': j['positions']}
            for j in catalog['companies']}


def enrich(request, cfg):
    snapshot = request['snapshot']
    question = '\n'.join(m['text'] for m in snapshot['messages'][-6:] if m['role'] == 'candidate')
    evidence = Cloud(cfg).request('api/v1/reply', {'company': snapshot['job_id'],
        'position': snapshot['knowledge_position'], 'question': question,
        'identity': 'pure-query', 'include_sources': True}, api=True).get('results', [])
    return {**request, 'evidence': evidence, 'knowledge_source': cfg['cloud_url'],
            'response_schema': {'request_id': request['request_id'], 'version': request['version'],
                                'action': 'reply|manual', 'text': '一条回复', 'sources': []}}


def doctor(cfg):
    problems = []
    if os.name != 'nt' or not Path(cfg.get('native_python') or ROOT / 'runtime/env/Scripts/python.exe').is_file():
        problems.append('Windows / native UIA3 runtime unavailable')
    if not cfg.get('binding') or cfg['binding'].get('version') != 2:
        problems.append('native UIA3 page is not bound')
    p = cfg['profile']
    if not p.get('verified') or not p.get('stable_ids_verified'):
        problems.append('native UIA profile and stable identities are not calibrated')
    required = ['list_class', 'item_class', 'message_root_class', 'message_class',
                'message_text_class', 'job_class', 'name_class', 'editor_class']
    if p.get('layout_mode') == 'classic':
        required += ['unread_class', 'selected_class', 'contact_name_class']
    for key in required:
        if not p.get(key):
            problems.append('profile missing ' + key)
    if not cfg.get('cloud_username') or cfg.get('cloud_url') != 'https://new.jiebo.vip/server/':
        problems.append('authorized cloud URL and username required')
    if not cfg.get('allowed_jobs') or '总库' in cfg.get('allowed_jobs', []):
        problems.append('explicit individual cloud projects required')
    if not cfg.get('job_aliases') or any(j not in cfg.get('allowed_jobs', []) for j in cfg.get('job_aliases', {}).values()):
        problems.append('exact page job aliases are missing or invalid')
    if cfg.get('mode') not in ('agent', 'cloud'):
        problems.append('unsupported provider mode')
    return {'ready_to_listen': not problems,
            'ready_to_send': not problems and bool(
                p.get('draft_verified')
                and (p.get('send_verified') or p.get('first_send_verification_required'))
                and (p.get('send_class') or p.get('send_name'))),
            'cloud_checked': False, 'problems': problems, 'config': str(STATE / 'config.json')}


def cloud_preflight(cfg):
    cloud = Cloud(cfg)
    cloud.health()
    available = jobs(cfg)
    if any(j not in available for j in cfg['allowed_jobs']):
        raise ValueError('configured_project_not_in_cloud')
    if cfg['mode'] == 'cloud' and cloud.capabilities().get('stateless_hr_reply') != 1:
        raise ValueError('cloud_stateless_reply_upgrade_required')
    return cloud


class ProcessLock:
    """OS-held byte lock survives stale files and is released on process exit."""
    def __init__(self, name='listener.lock'):
        self.name = name

    def __enter__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.file = (STATE / self.name).open('a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError('listener_already_running')
        return self

    def __exit__(self, *args):
        self.file.close()


def listen(send=False):
    cfg = config()
    if sync_job_selection(cfg):
        save(STATE / 'config.json', cfg)
    check = doctor(cfg)
    if not check['ready_to_listen'] or (send and not check['ready_to_send']):
        raise ValueError('preflight_failed: ' + json.dumps(check, ensure_ascii=False))
    with ProcessLock():
        store = Store(STATE / 'reply.sqlite3')
        store.recover()
        native = Native(cfg)
        def read_previous(cid, previous):
            return native.read(cid, candidate_name=previous.get('candidate_name'),
                               job_title=previous.get('job_title'))
        if clear_own_draft(native, store):
            store.setting('overlay_stage', '已清理上次未发送完的草稿')
        if store.status()['counts'].get('unknown'):
            store.setting('overlay_stage', '正在核对上次未确认的发送结果')
            try:
                result = store.reconcile_unknown(
                    read_previous,
                    cfg['allowed_jobs'])
                store.setting('overlay_notice', '已核对上次发送结果：' + json.dumps(
                    [item.get('state') for item in result], ensure_ascii=False))
                store.setting('overlay_notice_until', time.time() + 20)
            except Exception as exc:
                store.setting('last_error', 'unknown_send_reconcile_failed:' + str(exc))
        cloud = cloud_preflight(cfg)
        store.setting('control', 'running')
        store.setting('pid', os.getpid())
        store.setting('send_enabled', str(bool(send)).lower())
        seeded = False
        skipped = set()
        transient_failures = 0
        draft_retries = 0
        last_count_scan = 0.0
        was_paused = False
        current_since_none = True
        from reply_records import Recorder, backfill_from_store
        recorder = Recorder(cfg.get('account', ''), cfg.get('reply_records_root'))
        try:
            backfill_from_store(
                STATE / 'reply.sqlite3', cfg.get('account', ''),
                cfg.get('reply_records_root'))
            store.setting('records_backfill_error', '')
        except Exception as exc:
            store.setting('records_backfill_error', str(exc))

        def remember(snapshot, handoff=None):
            try:
                if handoff is None:
                    handoff = store.latest_handoff(snapshot['conversation_id'])
                recorder.record(snapshot, handoff=handoff)
                store.setting('records_error', '')
            except Exception as exc:
                store.setting('records_error', str(exc))

        def wait_next():
            base_delay = max(3.0, min(float(cfg['poll_seconds']), 30.0))
            deadline = time.monotonic() + base_delay * random.uniform(.8, 1.35)
            last_beat = time.monotonic()
            while time.monotonic() < deadline:
                if store.setting('control') != 'running':
                    return
                if store.setting('overlay_reply_current'):
                    return
                if time.monotonic() - last_beat >= 2.5:
                    store.setting('heartbeat', time.time())
                    last_beat = time.monotonic()
                time.sleep(min(.5, max(.05, deadline - time.monotonic())))

        def ordered(rows, cid):
            rows = list(rows)
            if cid:
                rows.sort(key=lambda row: (
                    (row.get('snapshot') or {}).get('conversation_id')
                    if 'snapshot' in row else row.get('conversation')) != cid)
            return rows

        try:
            from reply_overlay import launch as launch_overlay
            launch_overlay(STATE)
            store.setting('overlay_error', '')
        except Exception as exc:
            store.setting('overlay_error', str(exc))
        # Baseline a conversation that was already open when listening started.
        try:
            initial = native.call('read_current')
            if initial.get('state') == 'snapshot':
                remember(initial['snapshot'])
                store.setting('overlay_current', overlay_label(initial['snapshot']))
                store.ingest(initial['snapshot'], cfg['allowed_jobs'], cfg['max_rounds'])
                current_since_none = False
        except Exception:
            pass
        # Each cycle checks the currently open conversation first. Only when it has
        # no new candidate message do we scan other unread rows. After sending we
        # deliberately keep the conversation selected instead of clicking "全部".
        try:
            while store.setting('control') != 'stopped':
                store.setting('heartbeat', time.time())
                try:
                    if refresh_live_settings(cfg):
                        store.prune_jobs(cfg['allowed_jobs'])
                        store.setting('overlay_notice', '挂机设置已更新，新岗位对应已生效')
                        store.setting('overlay_notice_until', time.time() + 15)
                except Exception as exc:
                    store.setting('last_error', 'settings_reload_failed:' + str(exc))
                if store.setting('control') == 'paused':
                    transient_failures = 0
                    was_paused = True
                    time.sleep(1)
                    continue
                if was_paused:
                    was_paused = False
                    if clear_own_draft(native, store):
                        store.setting('overlay_stage', '已清理残留草稿，继续监听')
                try:
                    if store.status()['counts'].get('unknown'):
                        store.setting('overlay_stage', '正在核对上次未确认的发送结果')
                        store.reconcile_unknown(read_previous, cfg['allowed_jobs'])
                    force_current = bool(store.setting('overlay_reply_current'))
                    if force_current:
                        store.setting('overlay_reply_current', '')
                    current_cid = ''
                    current_event = False
                    current = native.call('read_current')
                    if current.get('state') == 'snapshot':
                        snapshot = current['snapshot']
                        remember(snapshot)
                        store.setting('overlay_current', overlay_label(snapshot))
                        current_cid = snapshot['conversation_id']
                        last_role = snapshot['messages'][-1]['role'] if snapshot['messages'] else ''
                        exchange_accepted = False
                        if snapshot.get('exchange_request'):
                            try:
                                accepted = native.call('accept_exchange_request')
                            except Exception:
                                accepted = {'state': 'error'}
                            if accepted.get('state') == 'accepted':
                                exchange_accepted = True
                                store.setting(
                                    'overlay_notice',
                                    '已同意候选人交换请求，正在生成回复')
                                store.setting('overlay_notice_until', time.time() + 15)
                                time.sleep(.8)
                                try:
                                    refreshed = native.call('read_current')
                                except Exception:
                                    refreshed = {}
                                if refreshed.get('state') == 'snapshot':
                                    snapshot = refreshed['snapshot']
                                    remember(snapshot)
                                    store.setting('overlay_current', overlay_label(snapshot))
                                    last_role = (snapshot['messages'][-1]['role']
                                                 if snapshot['messages'] else '')
                        if current_since_none and not force_current:
                            queued = store.queue_unread(
                                snapshot, cfg['allowed_jobs'], cfg['max_rounds'])
                            current_event = queued.get('state') in ('pending', 'ready')
                            current_since_none = False
                        else:
                            ingested = store.ingest(
                                snapshot, cfg['allowed_jobs'], cfg['max_rounds'])
                            current_event = (
                                ingested.get('state') == 'pending'
                                or (ingested.get('state') == 'observed'
                                    and ingested.get('new_candidate')
                                    and ingested.get('last_role') == 'candidate'))
                            if force_current:
                                queued = store.requeue_current(
                                    snapshot, cfg['allowed_jobs'], cfg['max_rounds'],
                                    release_latches=True, force_reply=True)
                                state = queued.get('state')
                                if state in ('pending', 'ready'):
                                    current_event = True
                                    store.setting(
                                        'overlay_notice',
                                        '已按请求重新生成回复并排队')
                                    store.setting('overlay_notice_until', time.time() + 12)
                                elif state == 'manual':
                                    store.setting(
                                        'overlay_stage', '当前窗口有人工草稿，未自动排队')
                                    store.setting(
                                        'overlay_notice',
                                        '未排队：' + str(queued.get('reason') or ''))
                                    store.setting('overlay_notice_until', time.time() + 12)
                            current_since_none = False
                        if exchange_accepted:
                            queued = store.requeue_current(
                                snapshot, cfg['allowed_jobs'], cfg['max_rounds'],
                                release_latches=True, force_reply=True)
                            if queued.get('state') in ('pending', 'ready'):
                                current_event = True
                        if current_event:
                            store.setting(
                                'overlay_stage',
                                '正在按请求回复当前窗口' if force_current else
                                f"当前窗口有新消息，正在回复 {snapshot.get('candidate_name', '候选人')}")
                            store.setting('overlay_unread_count', '1')
                    elif current.get('state') == 'none':
                        current_since_none = True
                    elif current.get('state') == 'unmapped':
                        current_since_none = True
                        store.setting('overlay_stage', '当前会话岗位未映射，已转人工处理')
                    if not current_event:
                        batch = native.scan(deep=not seeded)
                        last_count_scan = time.monotonic()
                        if not batch.get('complete'):
                            raise ValueError('inbox_scan_incomplete')
                        targets = [
                            cid for cid in batch['unread_ids']
                            if cid != current_cid and cid not in skipped
                        ][:1]
                        store.setting('overlay_visible_count',
                                      batch.get('visible_conversation_count',
                                                batch.get('loaded_conversation_count', 0)))
                        store.setting('overlay_unread_count', len(batch.get('unread_ids', [])))
                        store.setting('overlay_stage',
                                      '发现左侧红点，准备打开会话' if targets else '等待左侧红点提醒')
                        for cid in targets:
                            if store.setting('control') != 'running':
                                break
                            if cid in skipped:
                                continue
                            store.setting('overlay_stage', '正在打开新的未读会话')
                            try:
                                fresh = native.read(cid)
                            except Exception as exc:
                                if 'job_not_mapped' in str(exc):
                                    skipped.add(cid)
                                    store.setting('overlay_stage', '该会话岗位未映射，已打开并转人工处理')
                                    store.setting('overlay_notice', '该会话岗位未映射，已打开并转人工处理')
                                    store.setting('overlay_notice_until', time.time() + 30)
                                    store.setting('overlay_unread_count', 0)
                                    continue
                                raise
                            remember(fresh)
                            store.setting('overlay_current', overlay_label(fresh))
                            current_cid = fresh['conversation_id']
                            current_since_none = False
                            store.setting('overlay_stage',
                                          f"正在读取 {fresh.get('candidate_name', '候选人')} 的消息上下文")
                            store.queue_unread(fresh, cfg['allowed_jobs'], cfg['max_rounds'])
                    if store.setting('control') == 'running':
                        seeded = True
                    if cfg['mode'] == 'cloud' and store.setting('control') == 'running':
                        pending_rows = ordered(store.pending(), current_cid)
                        if pending_rows:
                            store.setting('overlay_stage', '正在获取知识库推荐回复')
                        for request in pending_rows[:1]:
                            if store.setting('control') != 'running':
                                break
                            if store.setting('overlay_reply_current'):
                                break
                            answer = cloud.generate(request)
                            try:
                                store.respond(
                                    request['request_id'], request['version'], **answer)
                            except ValueError as exc:
                                if 'request_not_pending_or_version_changed' in str(exc):
                                    # The candidate sent another message while we
                                    # were generating. The newer event already has
                                    # its own queue slot and will be answered next.
                                    store.setting(
                                        'overlay_stage',
                                        '候选人又发来消息，已合并到新的回复队列')
                                    continue
                                raise
                            if answer.get('action') == 'manual' or answer.get('handoff'):
                                remember(
                                    request['snapshot'],
                                    store.latest_handoff(
                                        request['snapshot']['conversation_id']),
                                )
                    if store.setting('control') == 'running':
                        ready_rows = ordered(store.ready(), current_cid)
                        if ready_rows:
                            store.setting('overlay_stage', '正在输入并发送推荐回复')
                        for r in ready_rows[:1]:
                            if store.setting('control') != 'running':
                                break
                            if store.setting('overlay_reply_current'):
                                break
                            # Re-read immediately before sending. Bridge rechecks once more.
                            expected = json.loads(r['payload'])['snapshot']
                            fresh = native.read(
                                r['conversation'],
                                candidate_name=expected.get('candidate_name'),
                                job_title=expected.get('job_title'))
                            if fresh['conversation_id'] != r['conversation']:
                                store.adopt_conversation(r['id'], fresh)
                                r = next(
                                    row for row in store.ready()
                                    if row['id'] == r['id'])
                            store.ingest(fresh, cfg['allowed_jobs'], cfg['max_rounds'])
                            result = store.dispatch(r['id'], fresh, native, enabled=send)
                            if result['state'] == 'unknown':
                                break
                            if result['state'] == 'sent':
                                store.setting('overlay_stage', '发送完成，等待当前窗口或下一条红点')
                                exchange = result.get('exchange') or {}
                                if exchange.get('state') == 'error':
                                    store.setting(
                                        'overlay_notice',
                                        '交换按钮点击失败：' + str(exchange.get('error') or ''))
                                    store.setting('overlay_notice_until', time.time() + 20)
                                try:
                                    after = native.call('read_current')
                                    if after.get('state') == 'snapshot':
                                        remember(after['snapshot'])
                                except Exception:
                                    pass
                    try:
                        if time.monotonic() - last_count_scan >= 2.5:
                            batch = native.scan(deep=False)
                            last_count_scan = time.monotonic()
                            if batch.get('complete'):
                                store.setting(
                                    'overlay_unread_count',
                                    len(batch.get('unread_ids', [])))
                                store.setting(
                                    'overlay_visible_count',
                                    batch.get('visible_conversation_count',
                                              batch.get('loaded_conversation_count', 0)))
                        store.setting('overlay_queue_next',
                                      store.next_queued_name(current_cid))
                    except Exception:
                        pass
                    store.setting('last_error', '')
                    transient_failures = 0
                    draft_retries = 0
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
                    store.setting('last_error', message)
                    if not isinstance(exc, (ValueError, RuntimeError)):
                        try:
                            import traceback
                            with (STATE / 'listener-error.log').open(
                                    'a', encoding='utf-8') as log:
                                log.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
                                log.write(traceback.format_exc() + '\n')
                        except OSError:
                            pass
                    if 'human_draft_present' in message:
                        # The web editor can still hold the just-sent text for
                        # a moment. Clear our own echo, then re-check before
                        # treating it as a human takeover.
                        if clear_own_draft(native, store):
                            store.setting('overlay_stage', '已清理发送残留草稿')
                            draft_retries = 0
                            continue
                        if draft_retries < 4:
                            draft_retries += 1
                            store.setting('overlay_stage', '输入框刚有内容，稍后重试')
                            time.sleep(1.5)
                            continue
                        # A real human draft belongs to this conversation, but
                        # it must not put the whole listener into a stopped
                        # state. Wait read-only until the draft is sent or
                        # cleared, then continue automatically.
                        store.setting('last_error', '')
                        store.setting('overlay_stage', '输入框有人工草稿，等待发送或清空')
                        store.setting('overlay_notice', '检测到输入框草稿，暂不操作该会话')
                        store.setting('overlay_notice_until', time.time() + 10)
                        draft_retries = 4
                        time.sleep(2)
                        continue
                    if transient_native_error(exc) and transient_failures < 8:
                        transient_failures += 1
                        store.setting('overlay_stage', '页面刷新，正在自动恢复')
                        try:
                            native.call('prepare_messages')
                        except Exception:
                            pass
                        time.sleep(1)
                        continue
                    store.control('pause')
                wait_next()
        finally:
            store.setting('pid', '')
            store.setting('heartbeat', time.time())
            if store.setting('control') == 'running':
                store.setting('control', 'paused')


def pending(store):
    cfg = config()
    return [enrich(r, cfg) for r in store.pending()]


def records_history(candidate='', job='', conversation='', limit=20):
    import reply_records
    return reply_records.history(
        candidate=candidate, job=job, conversation=conversation, limit=limit,
    )


def open_report():
    import reply_report
    cfg = config()
    return reply_report.open_report(STATE, cfg.get('reply_records_root'))


def export_records():
    import reply_records
    cfg = config()
    return reply_records.backfill_from_store(
        STATE / 'reply.sqlite3', cfg.get('account', ''), cfg.get('reply_records_root'))


def wait(store, timeout=45):
    deadline = time.monotonic() + max(0, min(float(timeout), 50))
    while True:
        requests = pending(store)
        state = store.status()
        if requests or state['control'] != 'running' or time.monotonic() >= deadline:
            return {'requests': requests, **state}
        time.sleep(.5)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'doctor', 'status', 'pending', 'pause', 'resume',
                 'stop', 'jobs', 'probe', 'cloud-check', 'daily-status', 'overlay'):
        sub.add_parser(name)
    sub.add_parser('report')
    sub.add_parser('reply-current')
    sub.add_parser('export-records')
    sub.add_parser('export-report')
    p = sub.add_parser('history')
    p.add_argument('--candidate', default='')
    p.add_argument('--job', default='')
    p.add_argument('--conversation', default='')
    p.add_argument('--limit', type=int, default=20)
    p = sub.add_parser('bind')
    p.add_argument('--window', required=True, type=int)
    p.add_argument('--chat-title', required=True)
    p = sub.add_parser('run')
    p.add_argument('--send', action='store_true')
    p = sub.add_parser('start')
    p.add_argument('--send', action='store_true')
    p = sub.add_parser('wait')
    p.add_argument('--seconds', type=float, default=45)
    p = sub.add_parser('respond')
    p.add_argument('--file', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--inventory', required=True)
    for name in ('takeover', 'release'):
        p = sub.add_parser(name)
        p.add_argument('--conversation', required=True)
    p = sub.add_parser('resolve')
    p.add_argument('--request', required=True)
    p.add_argument('--outcome', required=True, choices=['sent', 'not_sent'])
    p.add_argument('--message-id', default='')
    args = parser.parse_args()
    if args.command == 'init':
        for path in (STATE, REPLY_CONFIG_ROOT, REPLY_RECORDS_ROOT):
            path.mkdir(parents=True, exist_ok=True)
        if not (STATE / 'config.json').exists():
            save(STATE / 'config.json', defaults())
        config()
        return output({'config': str(STATE / 'config.json'), 'state_dir': str(STATE)})
    if args.command == 'report':
        return output(open_report())
    if args.command == 'history':
        return output(records_history(
            args.candidate, args.job, args.conversation, args.limit))
    if args.command == 'export-records':
        return output(export_records())
    if args.command == 'export-report':
        import reply_report
        cfg = config()
        return output(reply_report.export_daily_snapshot(
            cfg.get('reply_records_root')))
    store = Store(STATE / 'reply.sqlite3')
    if args.command == 'reply-current':
        store.setting('overlay_reply_current', str(time.time()))
        return output({'state': 'requested', 'message': '已请求立即检查并回复当前窗口'})
    if args.command == 'overlay':
        from reply_overlay import launch as launch_overlay
        return output(launch_overlay(STATE))
    if args.command in ('pause', 'resume', 'stop'):
        if args.command == 'stop':
            request_file = STATE / 'start-request.json'
            if request_file.exists():
                request = load(request_file)
                request['state'] = 'cancelled'
                save(request_file, request)
        return output(store.control(args.command))
    if args.command == 'status':
        return output(store.status())
    if args.command == 'respond':
        return output(store.respond(**load(args.file)))
    if args.command in ('takeover', 'release'):
        return output(store.manual(args.conversation, args.command == 'release'))
    if args.command == 'resolve':
        return output(store.resolve(args.request, args.outcome, args.message_id))
    if args.command == 'wait':
        return output(wait(store, args.seconds))
    if args.command == 'pending':
        return output(pending(store))
    cfg = config()
    if args.command in ('prepare', 'daily-status'):
        import startup
        return output(startup.prepare(load(args.inventory)) if args.command == 'prepare' else startup.status())
    if args.command == 'doctor':
        return output(doctor(cfg))
    if args.command == 'jobs':
        return output([{'id': j['id'], 'title': j['title']} for j in jobs(cfg).values()])
    if args.command == 'cloud-check':
        cloud = Cloud(cfg)
        status = cloud.health()
        caps = cloud.capabilities()
        catalog = cloud.catalog()
        return output({'username': cfg['cloud_username'], 'url': cfg['cloud_url'],
                       'chunks': status.get('chunks'), 'companies': catalog.get('total_companies'),
                       'positions': catalog.get('total_positions'), 'auth': 'dedicated API key',
                       'stateless_hr_reply': caps.get('stateless_hr_reply', 0)})
    if args.command == 'bind':
        result = Native(cfg).call('bind', window=args.window, chat_title=args.chat_title)
        cfg['binding'] = result['binding']
        cfg['state_dir'] = str(STATE)
        # New browser binding invalidates any previous native calibration.
        cfg['profile']['verified'] = cfg['profile']['stable_ids_verified'] = False
        cfg['profile']['draft_verified'] = cfg['profile']['send_verified'] = False
        cfg['profile']['first_send_verification_required'] = False
        save(STATE / 'config.json', cfg)
        return output(result)
    if args.command == 'probe':
        return output(Native(cfg).call('probe'))
    if args.command == 'run':
        return listen(args.send)
    if args.command == 'start':
        if sync_job_selection(cfg):
            save(STATE / 'config.json', cfg)
        check = doctor(cfg)
        if not check['ready_to_listen'] or (args.send and not check['ready_to_send']):
            return output(check)
        cloud_preflight(cfg)
        if os.name != 'nt':
            raise ValueError('Windows required for detached launch')
        # Explicit breakaway: rejection means no child. Do not silently launch a cancellable worker.
        command = [sys.executable, str(Path(__file__).resolve()), 'run'] + (['--send'] if args.send else [])
        with (STATE / 'listener.stderr.log').open('a', encoding='utf-8') as err:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err, creationflags=subprocess.CREATE_NO_WINDOW |
                                    subprocess.CREATE_BREAKAWAY_FROM_JOB)
        time.sleep(.7)
        if proc.poll() is not None:
            raise ValueError('listener_did_not_stay_running; inspect listener.stderr.log')
        return output({'pid': proc.pid, 'state': 'started', 'send_enabled': args.send,
                       'mode': cfg['mode'], 'note': 'Cloud mode generates automatically; Agent mode requires an active consumer'})


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        output({'error': str(exc)})
        sys.exit(1)
