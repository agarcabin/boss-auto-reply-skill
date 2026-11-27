"""No-activate floating status and controls for BOSS reply automation."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from engine import Store
from overlay_native import NativeRoot
import reply
import screen_switch


PANEL_WIDTH = 760
BUTTON_WIDTH = 520
PANEL_HEIGHT = 80
PANEL_TOP = 6
PANEL_ALPHA = 0.84
TRANSPARENT_KEY = '#010203'
PANEL_BG = '#17212B'
PANEL_FG = '#E8EEF5'
PANEL_MUTED = '#AAB8C7'
PANEL_BORDER = '#3C4D5D'
BUTTON_BG = '#30404F'
BUTTON_ACTIVE_BG = '#42576A'
BUTTON_DISABLED_BG = '#26333E'
BUTTON_BORDER = '#3C4D5D'
SIGNAL_COLORS = {'green': '#4ADE80', 'yellow': '#F6C857',
                 'red': '#EF6262', 'gray': '#94A3B8'}
DISPLAY_BUTTON_COLORS = {
    'green': ('#237A4D', '#2F9A63'),
    'yellow': ('#B98212', '#D29B20'),
    'gray': ('#26333E', '#30404F'),
}


def _rounded_rectangle(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


def process_birth(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ''
    values = [ctypes.c_uint64() for _ in range(4)]
    try:
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
            return ''
        return str(values[0].value)
    finally:
        kernel.CloseHandle(handle)


def enable_dpi_awareness():
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    ctypes.windll.user32.SetProcessDPIAware()


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def titled_windows(expected_title):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
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
        if title.value == expected_title:
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append({'hwnd': int(hwnd), 'pid': pid.value})
        return True

    callback = callback_type(collect)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(callback, 0)
    return found


def overlay_windows():
    return titled_windows('BOSS 回复自动化 · 悬浮窗')


def settings_windows():
    return titled_windows('Boss 回复自动化 · 挂机设置')


def launch(state_dir=None):
    state = Path(state_dir or reply.STATE)
    owner_path = state / 'reply-overlay-owner.json'
    existing = overlay_windows()
    if existing:
        return {'state': 'overlay_already_running', 'pid': existing[0]['pid']}
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('reply_overlay_pythonw_missing')
    command = [str(pythonw), '-X', 'utf8', str(Path(__file__).resolve()),
               '--state-dir', str(state)]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    out_path = state / 'reply-overlay.stdout.log'
    err_path = state / 'reply-overlay.stderr.log'
    with out_path.open('a', encoding='utf-8') as out, err_path.open('a', encoding='utf-8') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   creationflags=flags, startupinfo=startup)
    time.sleep(.8)
    if process.poll() is not None:
        raise RuntimeError('reply_overlay_exited_during_startup')
    windows = overlay_windows()
    owner = _load(owner_path)
    if windows:
        pid = int(owner.get('pid') or windows[0]['pid'])
        if not owner.get('pid'):
            _save(owner_path, {'pid': pid, 'birth': process_birth(pid)})
        return {'state': 'overlay_started', 'pid': pid}
    raise RuntimeError('reply_overlay_window_not_visible')


def button_geometry():
    height = 64
    widths = [118, 84, 84, 96, 84]
    return {'y': 8, 'height': height, 'gap': 8, 'x': 8, 'widths': widths}


class ReplyOverlay:
    def __init__(self, state_dir):
        self.state = Path(state_dir)
        self.db_path = self.state / 'reply.sqlite3'
        _save(self.state / 'reply-overlay-owner.json',
              {'pid': os.getpid(), 'birth': process_birth(os.getpid())})
        self.root = NativeRoot()
        self.root.title('BOSS 回复自动化 · 悬浮窗')
        self.root.withdraw()
        self.root.configure(bg=TRANSPARENT_KEY)
        self.root.attributes('-alpha', PANEL_ALPHA)
        try:
            self.root.attributes('-transparentcolor', TRANSPARENT_KEY)
        except OSError:
            pass
        total_width = PANEL_WIDTH + BUTTON_WIDTH
        left = max(0, (self.root.winfo_screenwidth() - total_width) // 2)
        self.root.geometry(f'{total_width}x{PANEL_HEIGHT}+{left}+{PANEL_TOP}')
        self.canvas = self.root.make_canvas(width=total_width, height=PANEL_HEIGHT,
                                            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        _rounded_rectangle(self.canvas, 0, 0, total_width, PANEL_HEIGHT, 18,
                           fill=PANEL_BG, outline=PANEL_BORDER, width=1)
        self.dot = self.canvas.create_oval(14, 14, 66, 66, fill='#94A3B8', outline='')
        self.count = self.canvas.create_text(40, 40, text='', fill='#FFFFFF',
                                             font=('Microsoft YaHei UI', -18, 'bold'))
        self.state_text = self.canvas.create_text(82, 24, text='正在读取状态…', anchor='w',
                                                  fill=PANEL_FG,
                                                  font=('Microsoft YaHei UI', -13, 'bold'))
        self.detail_text = self.canvas.create_text(82, 51, text='', anchor='w',
                                                   fill=PANEL_MUTED,
                                                   font=('Microsoft YaHei UI', -11))
        geometry = button_geometry()
        self.rects = []
        self.texts = []
        self.enabled = [False, False, False, True, False]
        self.display_button = {'label': '副屏', 'color': 'yellow'}
        self.display_notice = ''
        self.display_notice_until = 0
        x = PANEL_WIDTH + geometry['x']
        for index, (label, width) in enumerate(
                zip(('回复当前窗口', '暂停', '终止', '查看报告', '副屏'),
                    geometry['widths'])):
            x1 = x + sum(geometry['widths'][:index]) + geometry['gap'] * index
            x2 = x1 + width
            self.rects.append(_rounded_rectangle(
                self.canvas, x1, geometry['y'], x2, geometry['y'] + geometry['height'], 12,
                fill=BUTTON_BG, outline=BUTTON_BORDER, width=1, tags=(f'button-{index}',)))
            self.texts.append(self.canvas.create_text(
                (x1 + x2) / 2, geometry['y'] + geometry['height'] / 2, text=label,
                fill='#F4F7FA', font=('Microsoft YaHei UI', -11),
                tags=(f'button-{index}',)))
            self.canvas.tag_bind(f'button-{index}', '<Enter>',
                                 lambda _event, i=index: self.hover(i, True))
            self.canvas.tag_bind(f'button-{index}', '<Leave>',
                                 lambda _event, i=index: self.hover(i, False))
        self.canvas.tag_bind('button-0', '<Button-1>', lambda _event: self.reply_current())
        self.canvas.tag_bind('button-1', '<Button-1>', lambda _event: self.toggle_pause())
        self.canvas.tag_bind('button-2', '<Button-1>', lambda _event: self.stop())
        self.canvas.tag_bind('button-3', '<Button-1>', lambda _event: self.open_report())
        self.canvas.tag_bind('button-4', '<Button-1>', lambda _event: self.toggle_display())
        self.last_data = {}
        self.stopped_since = None
        self.requested_stop = False
        self.root.deiconify()
        self.keep_topmost()
        self.refresh()
        self.root.mainloop()
        if self.requested_stop:
            self.wait_listener_and_confirm()

    def keep_topmost(self):
        self.root.api.user.SetWindowPos(
            self.root.native_hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010)  # NOSIZE | NOMOVE | NOACTIVATE, HWND_TOPMOST

    def log_event(self, message):
        try:
            with (self.state / 'reply-overlay-events.log').open('a', encoding='utf-8') as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def read(self):
        data = {'control': 'stopped', 'counts': {}, 'heartbeat': None,
                'last_error': '', 'pid': ''}
        try:
            with sqlite3.connect(self.db_path, timeout=3) as db:
                db.row_factory = sqlite3.Row
                settings = {row['key']: row['value'] for row in db.execute('SELECT key,value FROM settings')}
                data['control'] = settings.get('control', 'stopped')
                data['heartbeat'] = settings.get('heartbeat')
                data['last_error'] = settings.get('last_error', '')
                data['pid'] = settings.get('pid', '')
                data['stage'] = settings.get('overlay_stage', '正在检查当前可见会话')
                data['current'] = settings.get('overlay_current', '')
                data['queue_next'] = settings.get('overlay_queue_next', '')
                data['notice'] = settings.get('overlay_notice', '')
                data['notice_until'] = settings.get('overlay_notice_until', '0')
                data['visible'] = settings.get('overlay_visible_count', '0')
                data['unread'] = settings.get('overlay_unread_count', '0')
                data['counts'] = {row['state']: row['n'] for row in db.execute(
                    'SELECT state,count(*) n FROM requests GROUP BY state')}
                data['sent'] = db.execute('SELECT count(*) FROM receipts').fetchone()[0]
        except sqlite3.Error:
            pass
        status = _load(self.state / 'startup-status.json')
        data['startup_state'] = status.get('state', '')
        data['startup_message'] = status.get('message', '')
        return data

    def hover(self, index, hover):
        enabled = self.enabled[index]
        fill = BUTTON_ACTIVE_BG if enabled and hover else (BUTTON_BG if enabled else BUTTON_DISABLED_BG)
        if index == 4 and enabled:
            fill = DISPLAY_BUTTON_COLORS[self.display_button['color']][bool(hover)]
        self.canvas.itemconfigure(self.rects[index], fill=fill)

    def toggle_pause(self):
        self.log_event('pause_button')
        if not self.enabled[1]:
            self.log_event('pause_button_disabled')
            return
        try:
            store = Store(self.db_path)
            action = 'resume' if self.read()['control'] == 'paused' else 'pause'
            store.control(action)
            store.setting('last_error', '')
        except Exception as exc:
            try:
                Store(self.db_path).setting('last_error', str(exc))
            except Exception:
                pass
        self.refresh()

    def reply_current(self):
        self.log_event('reply_current_button')
        if not self.enabled[0]:
            self.log_event('reply_current_button_disabled')
            return
        try:
            Store(self.db_path).setting('overlay_reply_current', str(time.time()))
            self.display_notice = '已请求立即检查并回复当前窗口'
        except Exception as exc:
            self.display_notice = '回复当前窗口失败：' + str(exc)
        self.display_notice_until = time.time() + 10
        self.refresh()

    def open_report(self):
        self.log_event('report_button')
        try:
            import reply_report
            result = reply_report.open_report(
                self.state, reply.config().get('reply_records_root'))
            self.display_notice = '报告已打开：' + result.get('url', '')
        except Exception as exc:
            self.display_notice = '报告打开失败：' + str(exc)
        self.display_notice_until = time.time() + 10
        self.refresh()

    def stop(self):
        self.log_event('stop_button')
        self.requested_stop = True
        try:
            Store(self.db_path).control('stop')
            request = self.state / 'start-request.json'
            if request.exists():
                data = _load(request)
                data['state'] = 'cancelled'
                _save(request, data)
        except Exception as exc:
            try:
                Store(self.db_path).setting('last_error', str(exc))
            except Exception:
                pass
        self.refresh()

    def wait_listener_and_confirm(self):
        """Wait until the listener process really exits, then offer the report."""
        pid = ''
        try:
            pid = Store(self.db_path).setting('pid') or ''
        except Exception:
            pass
        deadline = time.monotonic() + 90
        if pid:
            while time.monotonic() < deadline:
                try:
                    alive = bool(screen_switch.process_birth(pid))
                except Exception:
                    alive = False
                if not alive:
                    break
                time.sleep(.4)
        self.confirm_report_after_stop()

    def confirm_report_after_stop(self):
        """After a normal stop, offer today's offline report."""
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.MessageBoxW.argtypes = [
                wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
            user32.MessageBoxW.restype = ctypes.c_int
            choice = user32.MessageBoxW(
                0, '程序已正常关闭，是否打开本日报告？', 'BOSS 自动回复',
                0x00000004 | 0x00000020 | 0x00040000)
            if choice != 6:
                return
            import reply_report
            cfg = reply.config()
            root = Path(cfg.get('reply_records_root') or reply.REPLY_RECORDS_ROOT)
            day = time.strftime('%Y-%m-%d')
            target = root / f'{day}.html'
            if not target.exists():
                reply_report.export_daily_snapshot(root, day)
            os.startfile(str(target))
        except Exception as exc:
            try:
                Store(self.db_path).setting(
                    'last_error', 'report_after_stop_failed:' + str(exc))
            except Exception:
                pass

    def open_settings(self):
        self.log_event('settings_button')
        inventory = _load(self.state / 'daily-inventory.json')
        if not inventory.get('jobs') or inventory.get('date') != time.strftime('%Y-%m-%d'):
            self.log_event('settings_button_disabled')
            self.display_notice = '请先完成今日岗位盘点'
            self.display_notice_until = time.time() + 10
            self.refresh()
            return
        try:
            existing = settings_windows()
            if existing:
                user = ctypes.windll.user32
                user.ShowWindow(existing[0]['hwnd'], 9)
                user.SetForegroundWindow(existing[0]['hwnd'])
                self.display_notice = '设置窗口已打开'
            else:
                pythonw = Path(sys.executable).with_name('pythonw.exe')
                command = [
                    str(pythonw if pythonw.is_file() else sys.executable),
                    '-X', 'utf8', str(Path(__file__).with_name('mapping_window.py')),
                    '--inventory', str(self.state / 'daily-inventory.json'),
                    '--settings', '--state-dir', str(self.state)]
                with (self.state / 'mapping-ui.log').open('a', encoding='utf-8') as log:
                    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                self.display_notice = '已打开挂机设置窗口'
        except Exception as exc:
            self.display_notice = '设置窗口打开失败：' + str(exc)
        self.display_notice_until = time.time() + 10
        self.refresh()

    def toggle_display(self):
        self.log_event('screen_button')
        if not self.enabled[4]:
            self.log_event('screen_button_disabled')
            return
        try:
            cfg = reply.config()
            binding = cfg.get('binding') or {}
            store = Store(self.db_path)
            preferred = store.setting('screen_secondary_device') or None
            result = screen_switch.toggle(binding, preferred)
            if result.get('secondary_device'):
                store.setting('screen_secondary_device', result['secondary_device'])
            self.display_notice = f"Codex 已切换到{result['display']}"
        except Exception as exc:
            self.display_notice = '屏幕切换未完成：' + str(exc)
        self.display_notice_until = time.time() + 10
        self.refresh()

    def refresh(self):
        data = self.read()
        self.last_data = data
        control = data['control']
        counts = data['counts']
        pending = sum(int(counts.get(name, 0)) for name in ('pending', 'ready'))
        unknown = int(counts.get('unknown', 0))
        listener_alive = bool(data['pid'] and process_birth(data['pid']))
        if unknown:
            color, label = SIGNAL_COLORS['red'], '发送结果待核实'
        elif control == 'paused':
            color, label = SIGNAL_COLORS['gray'], '已暂停'
        elif control == 'stopped' or not listener_alive:
            color, label = SIGNAL_COLORS['gray' if control == 'stopped' else 'red'], (
                '已停止' if control == 'stopped' else '监听进程已退出')
        elif data['last_error']:
            color, label = SIGNAL_COLORS['yellow'], '页面刷新，自动恢复中'
        else:
            color, label = SIGNAL_COLORS['green'], '运行中'
        unread = int(str(data.get('unread', '0')).split('+')[0] or 0)
        count = unread if unread else pending
        self.canvas.itemconfigure(self.dot, fill=color)
        self.canvas.itemconfigure(self.count, text=str(count) if count else '')
        try:
            notice_active = float(data.get('notice_until') or 0) > time.time()
        except ValueError:
            notice_active = False
        stage = ((data.get('notice') or data.get('stage')) if notice_active
                 else data.get('stage') or data.get('startup_message') or '正在检查当前可见会话')
        current = (data.get('current') or '').strip()
        if current:
            stage = f'{current}：{stage}'
        next_name = (data.get('queue_next') or '').strip()
        if current and next_name:
            short_current = current.split('（', 1)[0]
            stage = f'{stage}（目前：{short_current}，下一个：{next_name}）'
        heartbeat = ''
        try:
            age = max(0, int(time.time() - float(data['heartbeat'])))
            heartbeat = f' · 心跳 {age} 秒前'
        except (TypeError, ValueError):
            pass
        if next_name:
            heartbeat = ''
        self.canvas.itemconfigure(self.state_text, text=f'{label} · {stage}{heartbeat}')
        detail = (f"可见 {data.get('visible', '0')} · 未读 {data.get('unread', '0')} · "
                  f"待回复 {pending} · 已发送 {data.get('sent', 0)}")
        if self.display_notice and time.time() < self.display_notice_until:
            detail = self.display_notice
        if data['last_error'] and control != 'running':
            detail = str(data['last_error'])[:100]
        self.canvas.itemconfigure(self.detail_text, text=detail)
        display_ok = False
        try:
            self.display_button = screen_switch.button_state(
                (reply.config().get('binding') or {}))
            display_ok = True
        except Exception:
            self.display_button = {'label': '切屏', 'color': 'gray'}
        self.enabled = [control == 'running', control in ('running', 'paused'),
                        control != 'stopped', True, display_ok]
        self.canvas.itemconfigure(self.texts[0],
                                  fill='#F4F7FA' if self.enabled[0] else '#7C8A97')
        self.canvas.itemconfigure(self.rects[0],
                                  fill=BUTTON_BG if self.enabled[0] else BUTTON_DISABLED_BG)
        self.canvas.itemconfigure(self.texts[1],
                                  text=('继续' if control == 'paused' else '暂停'),
                                  fill='#F4F7FA' if self.enabled[1] else '#7C8A97')
        self.canvas.itemconfigure(self.rects[1],
                                  fill=BUTTON_BG if self.enabled[1] else BUTTON_DISABLED_BG)
        self.canvas.itemconfigure(self.texts[2],
                                  fill='#F4F7FA' if self.enabled[2] else '#7C8A97')
        self.canvas.itemconfigure(self.rects[2],
                                  fill=BUTTON_BG if self.enabled[2] else BUTTON_DISABLED_BG)
        self.canvas.itemconfigure(self.texts[3],
                                  fill='#F4F7FA' if self.enabled[3] else '#7C8A97')
        self.canvas.itemconfigure(self.rects[3],
                                  fill=BUTTON_BG if self.enabled[3] else BUTTON_DISABLED_BG)
        self.canvas.itemconfigure(self.texts[4],
                                  text=self.display_button['label'],
                                  fill='#F4F7FA' if self.enabled[4] else '#7C8A97')
        self.canvas.itemconfigure(
            self.rects[4],
            fill=(DISPLAY_BUTTON_COLORS[self.display_button['color']][0]
                  if self.enabled[4] else BUTTON_DISABLED_BG))
        if control == 'stopped':
            if self.stopped_since is None:
                self.stopped_since = time.monotonic()
            elif time.monotonic() - self.stopped_since > 1.5:
                self.root.destroy()
                return
        else:
            self.stopped_since = None
        self.keep_topmost()
        self.root.after(1000, self.refresh)


def main():
    enable_dpi_awareness()
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, default=reply.STATE)
    parser.add_argument('--launch', action='store_true')
    args = parser.parse_args()
    if args.launch:
        print(json.dumps(launch(args.state_dir), ensure_ascii=False))
        return
    owner_path = Path(args.state_dir) / 'reply-overlay-owner.json'
    try:
        ReplyOverlay(args.state_dir)
    finally:
        owner = _load(owner_path)
        if owner.get('pid') == os.getpid():
            try:
                owner_path.unlink()
            except OSError:
                pass


if __name__ == '__main__':
    main()
