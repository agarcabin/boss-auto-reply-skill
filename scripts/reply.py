"""CLI and unattended listener for boss-auto-reply (Python standard library)."""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
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
ACCOUNT_CONFIG_FIELDS = (
    'reply_style', 'poll_seconds', 'max_rounds',
    'allowed_jobs', 'job_aliases', 'daily_mapping_file',
)
ACCOUNT_CONFIG_DEFAULTS = {
    'reply_style': '自然亲切',
    'poll_seconds': 6,
    'max_rounds': 0,
    'allowed_jobs': [],
    'job_aliases': {},
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


def config():
    value = load(STATE / 'config.json')
    if value.get('version', 1) < 2:
        value['version'] = 2
        value.setdefault('accounts', {})
        account = str(value.get('account') or '').strip()
        if account:
            value['accounts'][account] = {
                field: copy.deepcopy(value.get(field, ACCOUNT_CONFIG_DEFAULTS[field]))
                for field in ACCOUNT_CONFIG_FIELDS
            }
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


def ps_host():
    host = shutil.which('pwsh')
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
    return host or (str(bundled) if bundled.is_file() else None)


def defaults():
    return {
        'version': 1, 'mode': 'agent', 'poll_seconds': 6, 'max_rounds': 3,
        'cloud_url': 'https://new.jiebo.vip/server/', 'cloud_username': '小八',
        'reply_style': '自然亲切',
        'needs_verification_reply': True,
        'job_config_root': str(Path.home() / 'Desktop/自动招呼/03 岗位配置'),
        'allowed_jobs': [], 'job_aliases': {}, 'binding': None,
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
    check = doctor(cfg)
    if not check['ready_to_listen'] or (send and not check['ready_to_send']):
        raise ValueError('preflight_failed: ' + json.dumps(check, ensure_ascii=False))
    with ProcessLock():
        store = Store(STATE / 'reply.sqlite3')
        store.recover()
        if store.status()['counts'].get('unknown'):
            raise ValueError('resolve_unknown_send_before_start')
        native = Native(cfg)
        cloud = cloud_preflight(cfg)
        store.setting('control', 'running')
        store.setting('pid', os.getpid())
        store.setting('send_enabled', str(bool(send)).lower())
        seeded = False
        skipped = set()
        transient_failures = 0
        current_since_none = True
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
                store.ingest(initial['snapshot'], cfg['allowed_jobs'], cfg['max_rounds'])
                current_since_none = False
        except Exception:
            pass
        # Read all enumerated contacts first, without generating any historical replies.
        # First discovery of each conversation always establishes a baseline.
        try:
            while store.setting('control') != 'stopped':
                store.setting('heartbeat', time.time())
                if store.setting('control') == 'paused':
                    transient_failures = 0
                    time.sleep(1)
                    continue
                try:
                    batch = native.scan(deep=not seeded)
                    if not batch.get('complete'):
                        raise ValueError('inbox_scan_incomplete')
                    targets = batch['unread_ids'][:1]
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
                                try:
                                    native.call('clear_selection')
                                except Exception:
                                    pass
                                continue
                            raise
                        store.setting('overlay_stage',
                                      f"正在读取 {fresh.get('candidate_name', '候选人')} 的消息上下文")
                        store.queue_unread(fresh, cfg['allowed_jobs'], cfg['max_rounds'])
                    if not targets:
                        current = native.call('read_current')
                        if current.get('state') == 'snapshot':
                            current_snapshot = current['snapshot']
                            store.setting('overlay_stage', '正在读取当前已打开会话')
                            if current_since_none:
                                store.queue_unread(current_snapshot, cfg['allowed_jobs'],
                                                   cfg['max_rounds'])
                                current_since_none = False
                            else:
                                ingested = store.ingest(
                                    current_snapshot, cfg['allowed_jobs'],
                                    cfg['max_rounds'])
                                if (ingested.get('state') == 'observed'
                                        and current_snapshot['messages'][-1]['role'] == 'candidate'):
                                    store.queue_unread(
                                        current_snapshot, cfg['allowed_jobs'],
                                        cfg['max_rounds'])
                        elif current.get('state') == 'none':
                            current_since_none = True
                        elif current.get('state') == 'unmapped':
                            current_since_none = True
                            store.setting('overlay_stage', '当前会话岗位未映射，已转人工处理')
                    if store.setting('control') == 'running':
                        seeded = True
                    if cfg['mode'] == 'cloud' and store.setting('control') == 'running':
                        if store.pending():
                            store.setting('overlay_stage', '正在获取知识库推荐回复')
                        for request in store.pending()[:1]:
                            if store.setting('control') != 'running':
                                break
                            answer = cloud.generate(request)
                            store.respond(request['request_id'], request['version'], **answer)
                    if store.setting('control') == 'running':
                        if store.ready():
                            store.setting('overlay_stage', '正在输入并发送推荐回复')
                        for r in store.ready()[:1]:
                            if store.setting('control') != 'running':
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
                                store.setting('overlay_stage', '发送完成，等待下一条红点')
                    store.setting('last_error', '')
                    transient_failures = 0
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
                    store.setting('last_error', message)
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
                base_delay = max(6.0, min(float(cfg['poll_seconds']), 30.0))
                time.sleep(base_delay * random.uniform(.8, 1.35))
        finally:
            store.setting('pid', '')
            store.setting('heartbeat', time.time())
            if store.setting('control') == 'running':
                store.setting('control', 'paused')


def pending(store):
    cfg = config()
    return [enrich(r, cfg) for r in store.pending()]


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
        STATE.mkdir(parents=True, exist_ok=True)
        if not (STATE / 'config.json').exists():
            save(STATE / 'config.json', defaults())
        return output({'config': str(STATE / 'config.json'), 'state_dir': str(STATE)})
    store = Store(STATE / 'reply.sqlite3')
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
