"""Independent stdio MCP server; never starts greeting or sending by import."""
import json
import subprocess
import sys
import time
from engine import Store
import reply
import screen_switch


def spec(name, description, properties=None, required=None):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties or {}, 'required': required or [],
        'additionalProperties': False}}


SPECS = [
    spec('boss_reply_open', 'Hide the Codex sidebar, reuse or open the BOSS chat page, and bind the native window.'),
    spec('boss_reply_daily_start', 'Open BOSS recruitment data with native UIA, verify active jobs, save today inventory and open the mapping window. Never sends before user Save-and-start.'),
    spec('boss_reply_inventory', 'Read current jobs from native recruitment data and job management without opening the mapping window.'),
    spec('boss_reply_prepare', "Save today's UIA/MSAA job inventory and open the native mapping window. Does not send before user Save-and-start.",
         {'inventory': {'type': 'object'}}, ['inventory']),
    spec('boss_reply_daily_status', 'Read mapping-window, daily mapping and start-request state.'),
    spec('boss_reply_cloud_catalog', 'Read actual company/position choices from the authorized knowledge API.'),
    spec('boss_reply_generate', 'Generate one pending request through the cloud HR identity. Does not independently send.',
         {'request_id': {'type': 'string'}}, ['request_id']),
    spec('boss_reply_status', 'Read control, heartbeat, listener health, binding, startup state and queue counts.'),
    spec('boss_reply_pending', 'Read pending requests with public evidence from the authorized cloud project.'),
    spec('boss_reply_wait', 'Wait up to 50 seconds for requests or a control change.', {'seconds': {'type': 'number'}}),
    spec('boss_reply_respond', 'Submit one version-bound reply or human handoff.', {
        'request_id': {'type': 'string'}, 'version': {'type': 'string'},
        'action': {'type': 'string', 'enum': ['reply', 'manual']},
        'text': {'type': 'string'}, 'sources': {'type': 'array'},
        'segments': {'type': 'array'}}, ['request_id', 'version', 'action']),
    spec('boss_reply_control', 'Pause, resume or stop only the independent reply listener.', {
        'action': {'type': 'string', 'enum': ['pause', 'resume', 'stop']}}, ['action']),
    spec('boss_reply_takeover', 'Latch human ownership or explicitly release one conversation.', {
        'conversation_id': {'type': 'string'}, 'release': {'type': 'boolean'}}, ['conversation_id']),
    spec('boss_reply_resolve', 'Record a user-verified platform outcome; never blindly retry.', {
        'request_id': {'type': 'string'}, 'outcome': {'type': 'string', 'enum': ['sent', 'not_sent']},
        'message_id': {'type': 'string'}}, ['request_id', 'outcome']),
    spec('boss_reply_start', 'Open and bind BOSS, then start the independent listener.', {
        'send': {'type': 'boolean'}}, []),
    spec('boss_reply_screen', 'Read or toggle the bound Codex/BOSS window between primary and secondary displays.', {
        'action': {'type': 'string', 'enum': ['status', 'toggle']}}, []),
    spec('boss_reply_history', 'Read locally saved chat history and exchanged contacts; no BOSS page access and no model call.', {
        'candidate': {'type': 'string'}, 'job': {'type': 'string'},
        'conversation_id': {'type': 'string'}, 'limit': {'type': 'number'}}, []),
    spec('boss_reply_report', 'Open the local read-only HTML communication report.', {}, []),
]


def call(name, arguments):
    store = Store(reply.STATE / 'reply.sqlite3')
    handlers = {
        'boss_reply_open': open_boss,
        'boss_reply_daily_start': lambda: daily_start(),
        'boss_reply_inventory': lambda: inventory(),
        'boss_reply_prepare': lambda inventory: __import__('startup').prepare(inventory),
        'boss_reply_daily_status': lambda: __import__('startup').status(),
        'boss_reply_cloud_catalog': lambda: reply.Cloud(reply.config()).catalog(),
        'boss_reply_generate': lambda request_id: generate(store, request_id),
        'boss_reply_status': lambda: status(store),
        'boss_reply_pending': lambda: reply.pending(store),
        'boss_reply_wait': lambda seconds=45: reply.wait(store, seconds),
        'boss_reply_respond': store.respond,
        'boss_reply_control': lambda action: control(store, action),
        'boss_reply_takeover': store.manual,
        'boss_reply_resolve': store.resolve,
        'boss_reply_start': lambda send=True: start(store, send),
        'boss_reply_screen': lambda action='status': screen(store, action),
        'boss_reply_history': lambda candidate='', job='', conversation_id='', limit=20: reply.records_history(
            candidate, job, conversation_id, limit),
        'boss_reply_report': lambda: reply.open_report(),
    }
    known = next((s for s in SPECS if s['name'] == name), None)
    if not known or not isinstance(arguments, dict):
        raise ValueError('unknown_tool_or_invalid_arguments')
    schema = known['inputSchema']
    if set(arguments) - set(schema['properties']) or set(schema['required']) - set(arguments):
        raise ValueError('invalid_tool_arguments')
    for key, value in arguments.items():
        typ = schema['properties'][key].get('type')
        if ((typ == 'string' and not isinstance(value, str)) or
            (typ == 'boolean' and type(value) is not bool) or
            (typ == 'number' and type(value) not in (int, float)) or
            (typ == 'array' and not isinstance(value, list))):
            raise ValueError('invalid_tool_argument_type')
    return handlers[name](**arguments)


def generate(store, request_id):
    rows = [r for r in store.pending() if r['request_id'] == request_id]
    if len(rows) != 1:
        raise ValueError('request_not_pending')
    answer = reply.Cloud(reply.config()).generate(rows[0])
    result = store.respond(request_id, rows[0]['version'], **answer)
    return {**result, 'origin': 'cloud_api', 'identity': 'hr-sim',
            'text': answer['text'], 'sources': answer['sources'], 'sent': False}


def control(store, action):
    if action == 'stop':
        path = reply.STATE / 'start-request.json'
        if path.exists():
            request = reply.load(path)
            request['state'] = 'cancelled'
            reply.save(path, request)
    return store.control(action)


def open_boss():
    cfg = reply.config()
    value = reply.Native(cfg).call('open_boss')
    cfg['binding'] = value['binding']
    cfg['state_dir'] = str(reply.STATE)
    reply.save(reply.STATE / 'config.json', cfg)
    return value


def status(store):
    value = store.status()
    try:
        age = max(0, int(time.time() - float(value.get('heartbeat'))))
    except (TypeError, ValueError):
        age = None
    pid = store.setting('pid') or ''
    value.update(
        heartbeat_age=age,
        listener_alive=bool(pid and screen_switch.process_birth(pid)),
        send_enabled=(store.setting('send_enabled') or '').lower() == 'true',
        pid=pid,
    )
    try:
        cfg = reply.config()
        binding = cfg.get('binding') or {}
    except (OSError, ValueError):
        binding = {}
    value['binding'] = {
        key: binding.get(key)
        for key in ('hwnd', 'pid', 'chat_title')
        if binding.get(key) not in (None, '')
    }
    startup_path = reply.STATE / 'startup-status.json'
    if startup_path.exists():
        startup = reply.load(startup_path)
        value['startup'] = {
            'state': startup.get('state'),
            'message': startup.get('message'),
        }
    return value


def start(store, send=True):
    current = store.status()
    if current.get('control') == 'running':
        return {'state': 'already_running', **status(store)}
    if current.get('control') == 'paused':
        pid = store.setting('pid') or ''
        if pid and screen_switch.process_birth(pid):
            store.control('resume')
            return {'state': 'resumed', **status(store)}
    opened = open_boss()
    reply.Native(reply.config()).call('prepare_messages')
    command = [sys.executable, str(reply.ROOT / 'scripts/reply.py'), 'start']
    if send:
        command.append('--send')
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding='utf-8-sig',
        errors='replace', timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    try:
        value = json.loads(completed.stdout)
    except ValueError:
        raise ValueError('listener_start_failed: ' +
                         (completed.stderr or completed.stdout or '')[-800:])
    if completed.returncode or value.get('error') or value.get('ready_to_listen') is False:
        raise ValueError('listener_start_failed: ' +
                         (value.get('error') or
                          ('; '.join(value.get('problems') or []) if value.get('problems') else '') or
                          completed.stderr or '' or '岗位勾选或监听配置未就绪')[-800:])
    return {'state': 'started', 'open': opened, **value}


def screen(store, action='status'):
    cfg = reply.config()
    binding = cfg.get('binding') or {}
    if action == 'status':
        return screen_switch.button_state(binding)
    if action != 'toggle':
        raise ValueError('screen_action_must_be_status_or_toggle')
    preferred = store.setting('screen_secondary_device') or None
    value = screen_switch.toggle(binding, preferred)
    if value.get('secondary_device'):
        store.setting('screen_secondary_device', value['secondary_device'])
    return value


def inventory():
    cfg = reply.config()
    try:
        binding = reply.Native(cfg).call('auto_bind')['binding']
        cfg['binding'] = binding
        cfg['state_dir'] = str(reply.STATE)
        reply.save(reply.STATE / 'config.json', cfg)
    except Exception:
        if not cfg.get('binding') or cfg['binding'].get('version') != 2:
            raise
    result = reply.Native(cfg).call('inventory')
    from daily import normalize_inventory
    normalized = normalize_inventory(result)
    reply.save(reply.STATE / 'daily-inventory.json', normalized)
    return normalized


def daily_start():
    open_boss()
    value = inventory()
    return __import__('startup').prepare(value)


def serve():
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError()
        except ValueError:
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse_error'}})
            continue
        rid = message.get('id')
        method = message.get('method', '')
        if rid is None:
            continue
        params = message.get('params') or {}
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'boss-auto-reply', 'version': '0.0.3'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': SPECS}
        elif method == 'tools/call':
            try:
                value = call(params.get('name'), params.get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
            except Exception as exc:
                result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
        else:
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'method_not_found'}})
            continue
        send({'jsonrpc': '2.0', 'id': rid, 'result': result})


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    serve()
