"""Local read-only HTML report server for saved BOSS conversations."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import reply_records


APP_ID = 'boss-auto-reply-report'
HISTORY_WINDOW_TITLE = 'BOSS 聊天记录'
BASE_PORT = 8765
MAX_PORT = 8795
STATIC_MARKER = '<script id="embeddedData" type="application/json"></script>'
SNAPSHOT_INTERVAL = 30.0
_last_snapshot_export = 0.0


def _state(state_dir):
    return Path(state_dir or Path.home() / '.codex/state/boss-auto-reply')


def _meta_path(state_dir):
    return _state(state_dir) / 'report-server.json'


def _read_meta(state_dir):
    try:
        value = json.loads(_meta_path(state_dir).read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _health(port, timeout=.6):
    try:
        with urllib.request.urlopen(
            f'http://127.0.0.1:{int(port)}/health', timeout=timeout
        ) as response:
            value = json.loads(response.read().decode('utf-8'))
            return value.get('app') == APP_ID
    except Exception:
        return False


def _free_port():
    for port in range(BASE_PORT, MAX_PORT + 1):
        if _health(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                continue
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def build_snapshot_html(records_root):
    """Return one self-contained HTML page with the report data embedded."""
    template = Path(__file__).with_name('reply_report.html').read_text(encoding='utf-8')
    if STATIC_MARKER not in template:
        raise RuntimeError('report_embed_marker_missing')
    data = reply_records.report_data(records_root)
    payload = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    return template.replace(
        STATIC_MARKER,
        f'<script id="embeddedData" type="application/json">{payload}</script>',
    ), data


def snapshot_path(records_root, date=None):
    return Path(records_root) / f'{date or time.strftime("%Y-%m-%d")}.html'


def export_daily_snapshot(records_root, date=None):
    """Write today's offline report as <records>/YYYY-MM-DD.html."""
    root = Path(records_root)
    root.mkdir(parents=True, exist_ok=True)
    html, data = build_snapshot_html(root)
    target = snapshot_path(root, date)
    temp = target.with_name(target.name + '.tmp')
    temp.write_text(html, encoding='utf-8')
    os.replace(temp, target)
    return {
        'state': 'exported',
        'path': str(target),
        'records': len(data.get('records') or []),
        'generated_at': data.get('generated_at'),
    }


def _maybe_export_snapshot(records_root, force=False):
    global _last_snapshot_export
    now = time.monotonic()
    if not force and now - _last_snapshot_export < SNAPSHOT_INTERVAL:
        return None
    _last_snapshot_export = now
    try:
        return export_daily_snapshot(records_root)
    except (OSError, ValueError):
        return None


class ReportHandler(BaseHTTPRequestHandler):
    records_root = None
    template = b''

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self._send(200, self.template, 'text/html; charset=utf-8')
            return
        if path == '/api/data':
            body = json.dumps(
                reply_records.report_data(self.records_root),
                ensure_ascii=False,
            ).encode('utf-8')
            _maybe_export_snapshot(self.records_root)
            self._send(200, body, 'application/json; charset=utf-8')
            return
        if path == '/health':
            body = json.dumps({'app': APP_ID, 'ok': True}).encode('utf-8')
            self._send(200, body, 'application/json; charset=utf-8')
            return
        self._send(404, b'not found', 'text/plain; charset=utf-8')

    def log_message(self, format, *args):
        return


def serve(port, state_dir, records_root=None):
    template_path = Path(__file__).with_name('reply_report.html')
    ReportHandler.template = template_path.read_bytes()
    ReportHandler.records_root = records_root
    _maybe_export_snapshot(records_root, force=True)
    stop_exports = threading.Event()

    def export_loop():
        while not stop_exports.wait(SNAPSHOT_INTERVAL):
            _maybe_export_snapshot(records_root, force=True)

    threading.Thread(target=export_loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', int(port)), ReportHandler)
    meta = {
        'app': APP_ID,
        'pid': os.getpid(),
        'port': int(port),
        'url': f'http://127.0.0.1:{int(port)}/',
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    meta_path = _meta_path(state_dir)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        server.serve_forever()
    finally:
        server.server_close()


def ensure_server(state_dir=None, records_root=None):
    state_dir = _state(state_dir)
    meta = _read_meta(state_dir)
    port = int(meta.get('port') or 0)
    if port and _health(port):
        base_url = f'http://127.0.0.1:{port}/'
        return {'state': 'already_running', 'base_url': base_url, 'port': port}
    port = _free_port()
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    executable = pythonw if pythonw.is_file() else Path(sys.executable)
    command = [
        str(executable), '-X', 'utf8', str(Path(__file__).resolve()),
        '--serve', '--port', str(port), '--state-dir', str(state_dir),
    ]
    if records_root:
        command.extend(['--records-root', str(records_root)])
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    if os.name == 'nt':
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    log_path = state_dir / 'report-server.log'
    state_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as log:
        try:
            subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=flags,
            )
        except OSError:
            subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _health(port):
            base_url = f'http://127.0.0.1:{port}/'
            return {'state': 'started', 'base_url': base_url, 'port': port}
        time.sleep(.15)
    raise RuntimeError('report_server_did_not_start')


def open_report(state_dir=None, records_root=None):
    value = ensure_server(state_dir, records_root)
    webbrowser.open(value['base_url'])
    return {
        'state': 'already_running' if value['state'] == 'already_running' else 'opened',
        'url': value['base_url'],
        'port': value['port'],
    }


def _edge_path():
    candidates = [shutil.which('msedge')]
    for base in (
            os.environ.get('ProgramFiles(x86)'),
            os.environ.get('ProgramFiles'),
            os.environ.get('LOCALAPPDATA')):
        if base:
            candidates.append(str(Path(base) / 'Microsoft/Edge/Application/msedge.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _screen_size():
    try:
        user = ctypes.windll.user32
        return int(user.GetSystemMetrics(0)), int(user.GetSystemMetrics(1))
    except (AttributeError, OSError):
        return 0, 0


def _window_by_title(prefix):
    try:
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = []

        def collect(hwnd, _):
            if not user.IsWindowVisible(hwnd):
                return True
            length = user.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user.GetWindowTextW(hwnd, title, len(title))
            if title.value.startswith(prefix):
                found.append(int(hwnd))
            return True

        callback = callback_type(collect)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows(callback, 0)
        return found
    except (AttributeError, OSError):
        return []


def _pin_history_window(prefix=HISTORY_WINDOW_TITLE, timeout=3.0):
    deadline = time.monotonic() + timeout
    screen_width, _ = _screen_size()
    width, height = 560, 800
    x = max(10, screen_width - width - 18) if screen_width else 80
    y = 64
    while time.monotonic() < deadline:
        windows = _window_by_title(prefix)
        if windows:
            try:
                user = ctypes.windll.user32
                user.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, wintypes.UINT,
                ]
                user.SetWindowPos.restype = wintypes.BOOL
                user.SetWindowPos(
                    wintypes.HWND(windows[0]), ctypes.c_void_p(-1),
                    x, y, width, height,
                    0x0010 | 0x0040)  # NOACTIVATE | SHOWWINDOW, HWND_TOPMOST
                return int(windows[0])
            except (AttributeError, OSError):
                return 0
        time.sleep(.12)
    return 0


def open_history(state_dir=None, records_root=None):
    value = ensure_server(state_dir, records_root)
    url = value['base_url'] + '?mode=history'
    existing = _window_by_title(HISTORY_WINDOW_TITLE)
    if existing:
        _pin_history_window()
        return {'state': 'already_open', 'url': url, 'window': 'edge_app'}
    edge = _edge_path()
    if not edge:
        webbrowser.open(url)
        return {'state': 'opened', 'url': url, 'window': 'browser'}
    screen_width, _ = _screen_size()
    x = max(10, screen_width - 578) if screen_width else 80
    command = [
        str(edge), f'--app={url}', '--window-size=560,800',
        f'--window-position={x},64', '--no-first-run',
    ]
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        webbrowser.open(url)
        return {'state': 'opened', 'url': url, 'window': 'browser'}
    _pin_history_window()
    return {'state': 'opened', 'url': url, 'window': 'edge_app'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=BASE_PORT)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--records-root', type=Path)
    parser.add_argument('--data', action='store_true')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--open-history', action='store_true')
    args = parser.parse_args()
    if args.data:
        print(json.dumps(
            reply_records.report_data(args.records_root),
            ensure_ascii=False,
        ))
        return
    if args.open:
        print(json.dumps(
            open_report(args.state_dir, args.records_root),
            ensure_ascii=False,
        ))
        return
    if args.open_history:
        print(json.dumps(
            open_history(args.state_dir, args.records_root),
            ensure_ascii=False,
        ))
        return
    if args.serve:
        serve(args.port, args.state_dir, args.records_root)
        return
    parser.error('choose --serve, --data or --open')


if __name__ == '__main__':
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()

