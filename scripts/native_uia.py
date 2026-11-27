"""Native Windows UIA3/MSAA bridge. No DOM, CDP or browser attachment."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import time

import comtypes.client
comtypes.client.GetModule('UIAutomationCore.dll')
from comtypes.gen import UIAutomationClient as U


_CONTACT_DATE = re.compile(
    r'^(?:刚刚|今天|昨天|前天|\d{1,2}分钟前|\d{1,2}小时前|'
    r'\d{1,2}:\d{2}|'
    r'\d{1,4}[-/]\d{1,2}(?:[-/]\d{1,2})?|'
    r'\d{1,2}月\d{1,2}日)$'
)
_UNREAD_COUNT = re.compile(r'^\d{1,3}\+?$')
_EDITOR_OBJECT_CHARS = re.compile(r'[\ufeff\ufffc\u200b]')
_MESSAGE_METADATA = {'已读', '未读', '送达', '已送达', '发送中'}
_PLATFORM_NOTICE_MARKERS = (
    '该职位是集招职位', '该职位为集招职位',
    '回复需消耗沟通权益', '本条消息不消耗权益',
    '使用企业VIP回复',
    '请求交换微信', '请求交换电话', '请求交换简历',
    '交换微信已发送', '交换电话已发送',
)


def parse_contact_texts(values):
    """Parse the current BOSS row's stable text order without DOM selectors."""
    values = [value.strip() for value in values if isinstance(value, str) and value.strip()]
    date_index = next((i for i, value in enumerate(values[:3]) if _CONTACT_DATE.fullmatch(value)), None)
    if date_index is None or date_index not in (0, 1):
        raise ValueError('contact_date_not_found')
    unread = ''
    if date_index == 1:
        if not _UNREAD_COUNT.fullmatch(values[0]):
            raise ValueError('contact_unread_count_invalid')
        unread = values[0]
    if len(values) < date_index + 3:
        raise ValueError('contact_row_fields_incomplete')
    return {
        'unread': unread,
        'date': values[date_index],
        'candidate_name': values[date_index + 1],
        'job_title': values[date_index + 2],
        'preview': ' '.join(values[date_index + 3:]),
    }


def normalized_editor_text(value):
    """Chromium exposes an empty contenteditable as an object-replacement glyph."""
    return _EDITOR_OBJECT_CHARS.sub('', value or '').strip()


def recover_inventory_title(accessible_name, trailing_texts):
    """Recover a full job title from BOSS's full+ellipsized accessible name."""
    name = (accessible_name or '').strip()
    suffix = ''.join(trailing_texts or [])
    if suffix and name.endswith(suffix):
        name = name[:-len(suffix)].rstrip()
    if '…' not in name:
        return name.strip()
    before = name.split('…', 1)[0]
    candidates = []
    for start in range(1, len(before)):
        tail = before[start:]
        if before.startswith(tail):
            candidates.append(start)
    if len(candidates) != 1:
        raise ValueError('inventory_title_truncated_unrecoverable')
    return before[:candidates[0]].strip()


def classify_message(direct_classes, incoming_classes, outgoing_classes, system_classes):
    """Classify from direct native children, never from message text guesses."""
    tokens = set()
    for value in direct_classes:
        tokens.update(value.split())
    system = set(system_classes) | {'message-card-wrap', 'item-system'}
    incoming = set(incoming_classes)
    outgoing = set(outgoing_classes)
    if tokens.intersection(system):
        return 'system'
    if tokens.intersection(incoming) or 'figure' in tokens:
        return 'candidate'
    if tokens.intersection(outgoing) or 'text' in tokens:
        return 'recruiter'
    return 'unknown'


def is_platform_notice_text(text):
    return any(marker in (text or '') for marker in _PLATFORM_NOTICE_MARKERS)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk', wintypes.WORD),
        ('wScan', wintypes.WORD),
        ('dwFlags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx', wintypes.LONG),
        ('dy', wintypes.LONG),
        ('mouseData', wintypes.DWORD),
        ('dwFlags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ('uMsg', wintypes.DWORD),
        ('wParamL', wintypes.WORD),
        ('wParamH', wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT), ('hi', _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ('union',)
    _fields_ = [('type', wintypes.DWORD), ('union', _INPUTUNION)]


def _keyboard_record(wvk=0, wscan=0, flags=0):
    record = _INPUT(type=1)
    record.ki = _KEYBDINPUT(wVk=wvk, wScan=wscan, dwFlags=flags, time=0, dwExtraInfo=0)
    return record


def _send_keyboard_records(records):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    array = (_INPUT * len(records))(*records)
    if user.SendInput(len(records), array, ctypes.sizeof(_INPUT)) != len(records):
        raise ValueError('native_keyboard_input_rejected')


def click_screen_point(x, y):
    """Click a verified UIA node center. Used only for native toolbar actions."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user.SetCursorPos.restype = wintypes.BOOL
    if not user.SetCursorPos(int(x), int(y)):
        raise ValueError('native_mouse_position_rejected')
    down = _INPUT(type=0)
    down.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=0x0002,
                          time=0, dwExtraInfo=0)
    up = _INPUT(type=0)
    up.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=0x0004,
                        time=0, dwExtraInfo=0)
    array = (_INPUT * 2)(down, up)
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    if user.SendInput(2, array, ctypes.sizeof(_INPUT)) != 2:
        raise ValueError('native_mouse_input_rejected')


def scroll_screen_wheel(x, y, clicks):
    """Scroll a verified area with the mouse wheel (negative = down)."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user.SetCursorPos.restype = wintypes.BOOL
    if not user.SetCursorPos(int(x), int(y)):
        raise ValueError('native_mouse_position_rejected')
    record = _INPUT(type=0)
    record.mi = _MOUSEINPUT(
        dx=0, dy=0,
        mouseData=ctypes.c_uint32(int(clicks) * 120).value,
        dwFlags=0x0800, time=0, dwExtraInfo=0)
    array = (_INPUT * 1)(record)
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    if user.SendInput(1, array, ctypes.sizeof(_INPUT)) != 1:
        raise ValueError('native_mouse_input_rejected')


def is_exchange_request_text(text):
    text = text or ''
    if '交换' not in text:
        return False
    if not any(kind in text for kind in ('微信', '电话', '手机', '简历')):
        return False
    if '已发送' in text or '已通过' in text:
        return False
    return any(marker in text for marker in (
        '请求', '是否同意', '想和您交换', '我想要和您交换', '希望和您交换'))


def _send_unicode_text(value):
    units = value.encode('utf-16-le')
    records = []
    for index in range(0, len(units), 2):
        unit = int.from_bytes(units[index:index + 2], 'little')
        records.append(_keyboard_record(wscan=unit, flags=0x0004))
        records.append(_keyboard_record(wscan=unit, flags=0x0004 | 0x0002))
    if records:
        _send_keyboard_records(records)


def foreground_hwnd():
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    return int(user.GetForegroundWindow() or 0)


def hash_value(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def birth(pid):
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle:
        raise ValueError('bound_process_unavailable')
    values = [ctypes.c_uint64() for _ in range(4)]
    try:
        if not k.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
            raise ValueError('process_creation_time_unavailable')
        return str(values[0].value)
    finally:
        k.CloseHandle(handle)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        self.walker = self.uia.RawViewWalker

    def all(self, root):
        a = root.FindAll(4, self.uia.CreateTrueCondition())
        if a.Length > 6000:
            raise ValueError('native_tree_exceeds_bound')
        return [a.GetElement(i) for i in range(a.Length)]

    def rid(self, node):
        return ','.join(map(str, node.GetRuntimeId()))

    def url(self, node):
        value = node.GetCurrentPropertyValue(30045)
        return value if isinstance(value, str) else ''

    def docs(self, root):
        a = root.FindAll(4, self.uia.CreatePropertyCondition(30003, 50030))
        return [a.GetElement(i) for i in range(a.Length)]

    def window_docs(self, hwnd):
        # Electron's embedded browser can be a separate native fragment root.
        # A FindAll on only the main HWND may return a cached inactive chat document.
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        windows = [int(hwnd)]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(child, _):
            if len(windows) >= 80:
                return False
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(child, name, len(name))
            if user.IsWindowVisible(child) and ('Chrome' in name.value or 'WebView' in name.value):
                windows.append(int(child))
            return True
        callback = callback_type(collect)
        user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        user.EnumChildWindows(int(hwnd), callback, 0)
        result = {}
        for handle in windows:
            try:
                node = self.uia.ElementFromHandle(handle)
                docs = self.docs(node)
                if node.CurrentControlType == 50030:
                    docs.append(node)
                for doc in docs:
                    result[self.rid(doc)] = doc
            except Exception:
                continue
        return list(result.values())

    def browser_child_windows(self, hwnd):
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        handles = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(child, _):
            if len(handles) >= 80:
                return False
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(child, name, len(name))
            if user.IsWindowVisible(child) and (
                    name.value.startswith('Chrome_RenderWidgetHost') or 'WebView' in name.value):
                handles.append(int(child))
            return True

        callback = callback_type(collect)
        user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        user.EnumChildWindows(int(hwnd), callback, 0)
        return handles

    def boss_render_window(self):
        matches = []
        for handle in self.browser_child_windows(self.cfg['binding']['hwnd']):
            try:
                node = self.uia.ElementFromHandle(handle)
                docs = self.docs(node)
                if node.CurrentControlType == 50030:
                    docs.append(node)
                if any(not doc.CurrentIsOffscreen
                       and self.url(doc).startswith('https://www.zhipin.com/web/chat/')
                       and not self.url(doc).startswith('https://www.zhipin.com/web/frame/')
                       for doc in docs):
                    matches.append(handle)
            except Exception:
                continue
        if len(matches) != 1:
            raise ValueError('boss_render_window_not_unique')
        return matches[0]

    def pattern(self, node, ident, interface):
        try:
            pointer = node.GetCurrentPattern(ident)
            return pointer.QueryInterface(interface) if pointer else None
        except Exception:
            return None

    def find_all_named(self, root, name):
        """Fast UIA name lookup; avoids walking the whole BOSS tree."""
        try:
            condition = self.uia.CreatePropertyCondition(30005, name)
            found = root.FindAll(4, condition)
            return [found.GetElement(i) for i in range(found.Length)]
        except Exception:
            return []

    def text(self, node):
        p = self.pattern(node, 10002, U.IUIAutomationValuePattern)
        if p:
            return p.CurrentValue or ''
        p = self.pattern(node, 10014, U.IUIAutomationTextPattern)
        if p:
            return p.DocumentRange.GetText(-1) or ''
        return node.CurrentName or ''

    def text_values(self, root, excluded=None):
        excluded = set(excluded or ())
        values = []
        for item in self.all(root):
            if item.CurrentControlType != 50020:
                continue
            value = self.text(item).strip()
            if value and value not in excluded:
                values.append(value)
        return values

    def single_text(self, root, token, error):
        values = self.text_values(self.one(root, token))
        if not values:
            raise ValueError(error)
        # BOSS can split one name into multiple adjacent accessibility Text
        # nodes (for example "刘极" + "伋").
        return ''.join(values)

    def contact_fields(self, row):
        return parse_contact_texts(self.text_values(row))

    def contact_id(self, fields, row=None, runtime=None):
        b = self.cfg['binding']
        runtime = runtime or (self.rid(row) if row is not None else '')
        return 'session:' + hash_value([
            b.get('hwnd'), b.get('pid'), b.get('birth'), b.get('chat_title'),
            self.cfg.get('account', ''), fields['candidate_name'], fields['job_title'], runtime,
        ])[:32]

    def editor_text(self, editor=None):
        editor = editor or self.one(self.bound(), self.cfg['profile']['editor_class'], visible=True)
        return normalized_editor_text(self.text(editor))

    def focus_editor(self):
        main = int(self.cfg['binding']['hwnd'])
        if foreground_hwnd() != main:
            raise ValueError('native_typing_requires_existing_foreground')
        editor = self.one(self.bound(), self.cfg['profile']['editor_class'], visible=True)
        render = self.boss_render_window()
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user.SetFocus.argtypes = [wintypes.HWND]
        user.SetFocus.restype = wintypes.HWND
        user.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user.ScreenToClient.restype = wintypes.BOOL
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
        user.PostMessageW.restype = wintypes.BOOL
        process_id = wintypes.DWORD()
        target_thread = user.GetWindowThreadProcessId(main, ctypes.byref(process_id))
        current_thread = kernel.GetCurrentThreadId()
        attached = bool(user.AttachThreadInput(current_thread, target_thread, True))
        try:
            user.SetFocus(render)
            time.sleep(.08)
            editor.SetFocus()
            if self.focused(editor, main, timeout=1.2):
                return editor
            # The in-app browser can leave DOM focus in the Codex composer
            # after UIA SetFocus. A click on the editor's own accessible point
            # restores focus without using coordinates for submit.
            point, ok = editor.GetClickablePoint()
            if ok:
                client = wintypes.POINT(point.x, point.y)
                if user.ScreenToClient(render, ctypes.byref(client)):
                    lparam = (client.y << 16) | (client.x & 0xffff)
                    user.PostMessageW(render, 0x0200, 0, lparam)
                    user.PostMessageW(render, 0x0201, 1, lparam)
                    user.PostMessageW(render, 0x0202, 0, lparam)
                    if self.focused(editor, main, timeout=1.0):
                        return editor
        finally:
            if attached:
                user.AttachThreadInput(current_thread, target_thread, False)
        raise ValueError('native_editor_focus_not_verified')

    def focused(self, editor, main, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if foreground_hwnd() != main:
                raise ValueError('foreground_changed_during_typing')
            try:
                focused = self.uia.GetFocusedElement()
                if self.rid(focused) == self.rid(editor):
                    return True
            except Exception:
                pass
            time.sleep(.04)
        return False

    def wait_editor_text(self, editor, expected):
        # Chromium/editor text is normalized on read, so a verification point
        # that lands right after a space must compare the same normalized form.
        expected = normalized_editor_text(expected)
        deadline = time.monotonic() + .9
        while time.monotonic() < deadline:
            actual = self.editor_text(editor)
            if actual == expected:
                return
            time.sleep(.025)
        raise ValueError('native_editor_text_not_verified')

    def type_editor_text(self, value):
        if not isinstance(value, str) or not value or len(value) > 1000:
            raise ValueError('reply_text_invalid')
        if value != value.strip() or any(char in value for char in '\r\n\0'):
            raise ValueError('reply_text_must_be_single_line')
        editor = self.focus_editor()
        if self.editor_text(editor):
            raise ValueError('human_draft_present')
        typed = ''
        for index, char in enumerate(value):
            if foreground_hwnd() != int(self.cfg['binding']['hwnd']):
                raise ValueError('foreground_changed_during_typing')
            _send_unicode_text(char)
            typed += char
            time.sleep(random.uniform(.025, .065) + (.12 if char in '，。？！；：' else 0))
            if (index + 1) % 4 == 0 or char in '，。？！；：' or index + 1 == len(value):
                self.wait_editor_text(editor, typed)
        return editor

    def clear_editor(self, expected):
        editor = self.focus_editor()
        if self.editor_text(editor) != expected:
            raise ValueError('draft_changed_refusing_to_clear')
        _send_keyboard_records([
            _keyboard_record(wvk=0x11),
            _keyboard_record(wvk=0x41),
            _keyboard_record(wvk=0x41, flags=0x0002),
            _keyboard_record(wvk=0x11, flags=0x0002),
            _keyboard_record(wvk=0x08),
            _keyboard_record(wvk=0x08, flags=0x0002),
        ])
        self.wait_editor_text(editor, '')

    def nodes(self, root, token, visible=False):
        if not token:
            raise ValueError('native_selector_not_configured')
        required = set(token.split())
        return [n for n in self.all(root) if required.issubset(set(n.CurrentClassName.split()))
                and (not visible or not n.CurrentIsOffscreen)]

    def one(self, root, token, visible=False):
        nodes = self.nodes(root, token, visible=visible)
        if not nodes:
            # Chromium may expose a virtual list container through element
            # ancestry while omitting it from Document.FindAll descendants.
            required = set(token.split())
            candidates = {}
            for node in self.all(root):
                parent = node
                for _ in range(10):
                    try:
                        parent = self.walker.GetParentElement(parent)
                    except Exception:
                        break
                    if (required.issubset(set(parent.CurrentClassName.split()))
                            and (not visible or not parent.CurrentIsOffscreen)):
                        candidates[self.rid(parent)] = parent
                        break
            nodes = list(candidates.values())
        if len(nodes) != 1:
            raise ValueError('native_control_not_unique:' + token)
        return nodes[0]

    def bound(self):
        b = self.cfg.get('binding') or {}
        if b.get('version') != 2 or birth(b.get('pid', 0)) != b.get('birth'):
            raise ValueError('native_uia3_binding_required')
        w = self.uia.ElementFromHandle(int(b['hwnd']))
        if w.CurrentProcessId != b['pid']:
            raise ValueError('bound_window_changed')
        self.dismiss_boss_login_notice(int(b['hwnd']))
        docs = self.window_docs(b['hwnd'])
        chats = [d for d in docs if d.CurrentName == b['chat_title'] and self.url(d).startswith('app://')]
        bosses = [d for d in docs if not d.CurrentIsOffscreen
                  and self.url(d).startswith('https://www.zhipin.com/web/chat/')
                  and not self.url(d).startswith('https://www.zhipin.com/web/frame/')]
        if len(chats) != 1 or chats[0].CurrentIsOffscreen or len(bosses) != 1:
            raise ValueError('bound_chat_or_boss_not_visible')
        self.cfg['binding']['runtime_id'] = self.rid(bosses[0])
        return bosses[0]

    def bind(self, hwnd, chat_title):
        w = self.uia.ElementFromHandle(int(hwnd))
        self.dismiss_boss_login_notice(int(hwnd))
        docs = self.window_docs(hwnd)
        chats = [d for d in docs if d.CurrentName == chat_title and self.url(d).startswith('app://')]
        bosses = [d for d in docs if self.url(d).startswith('https://www.zhipin.com/web/chat/')]
        if len(chats) != 1 or len(bosses) != 1 or chats[0].CurrentIsOffscreen:
            raise ValueError('unique_current_chat_and_boss_required')
        pid = int(w.CurrentProcessId)
        return {'binding': {'version': 2, 'hwnd': int(hwnd), 'pid': pid, 'birth': birth(pid),
                            'chat_title': chat_title, 'runtime_id': self.rid(bosses[0])}}

    def host_window(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        allowed_pids = set()
        for image in ('ChatGPT.exe', 'Codex.exe'):
            try:
                output = subprocess.run(
                    ['tasklist', '/FI', f'IMAGENAME eq {image}', '/FO', 'CSV', '/NH'],
                    capture_output=True, text=True, encoding='utf-8-sig',
                    timeout=3, creationflags=subprocess.CREATE_NO_WINDOW).stdout
                for line in output.splitlines():
                    if line.startswith('"'):
                        allowed_pids.add(int(line.split('","')[1]))
            except Exception:
                pass
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        matches = []

        def collect(hwnd, _):
            if not user.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in allowed_pids:
                return True
            try:
                docs = self.window_docs(int(hwnd))
                chats = [
                    doc for doc in docs
                    if self.url(doc).startswith('app://')
                    and not doc.CurrentIsOffscreen]
                if len(chats) == 1:
                    matches.append((int(hwnd), chats[0].CurrentName))
            except Exception:
                pass
            return True

        callback = callback_type(collect)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows(callback, 0)
        if len(matches) != 1:
            raise ValueError('unique_visible_codex_chat_window_required')
        return matches[0]

    def visible_boss_docs(self, hwnd):
        return [
            doc for doc in self.window_docs(hwnd)
            if not doc.CurrentIsOffscreen
            and self.url(doc).startswith('https://www.zhipin.com/web/chat/')
            and not self.url(doc).startswith('https://www.zhipin.com/web/frame/')]

    def collapse_sidebar(self, root):
        hide_names = {
            'Hide sidebar', 'Close sidebar', '隐藏侧边栏', '隐藏边栏',
            '收起侧边栏', '关闭侧边栏', '收起聊天列表'}
        show_names = {
            'Show sidebar', 'Open sidebar', '显示侧边栏', '展开侧边栏',
            '打开侧边栏'}

        def find():
            return [
                node for node in self.all(root)
                if node.CurrentControlType == 50000
                and not node.CurrentIsOffscreen
                and node.CurrentName in hide_names | show_names]

        nodes = find()
        if len(nodes) != 1:
            raise ValueError('sidebar_control_not_unique')
        if nodes[0].CurrentName in show_names:
            return {'state': 'collapsed', 'changed': False}
        expand = self.pattern(
            nodes[0], 10005, U.IUIAutomationExpandCollapsePattern)
        if expand:
            expand.Collapse()
        else:
            self.invoke(nodes[0])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(.1)
            nodes = find()
            if len(nodes) == 1 and nodes[0].CurrentName in show_names:
                return {'state': 'collapsed', 'changed': True}
        raise ValueError('sidebar_collapse_unconfirmed')

    def open_boss_tab(self, root):
        url = 'https://www.zhipin.com/web/chat/index'

        def boss_tabs():
            return [
                node for node in self.all(root)
                if node.CurrentControlType == 50019
                and not node.CurrentIsOffscreen
                and node.CurrentName == 'BOSS直聘']

        tabs = boss_tabs()
        if tabs:
            self.invoke(tabs[0])
            return 'existing_tab'
        show = [
            node for node in self.all(root)
            if node.CurrentControlType == 50000
            and not node.CurrentIsOffscreen
            and node.CurrentName in ('Show tabs', '显示标签页')]
        if len(show) == 1:
            toggle = self.pattern(
                show[0], 10015, U.IUIAutomationTogglePattern)
            if toggle:
                toggle.Toggle()
            else:
                self.invoke(show[0])
            time.sleep(.5)
            tabs = boss_tabs()
            if tabs:
                self.invoke(tabs[0])
                return 'existing_tab'
        addresses = [
            node for node in self.all(root)
            if node.CurrentControlType == 50003
            and not node.CurrentIsOffscreen
            and ('搜索或输入网址' in node.CurrentName
                 or '搜索文件或输入 URL' in node.CurrentName)]
        if len(addresses) != 1:
            raise ValueError('codex_address_bar_not_unique')
        value = self.pattern(
            addresses[0], 10002, U.IUIAutomationValuePattern)
        if not value:
            raise ValueError('codex_address_bar_value_pattern_unavailable')
        value.SetValue(url)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            suggestions = [
                node for node in self.all(root)
                if node.CurrentControlType == 50007
                and not node.CurrentIsOffscreen
                and node.CurrentName.startswith('BOSS直聘')
                and 'zhipin.com/web/chat' in node.CurrentName]
            if suggestions:
                self.invoke(suggestions[0])
                return 'new_tab'
            time.sleep(.1)
        raise ValueError('boss_address_suggestion_not_found')

    def open_boss(self):
        hwnd, chat_title = self.host_window()
        self.dismiss_boss_login_notice(int(hwnd))
        root = self.uia.ElementFromHandle(int(hwnd))
        sidebar = self.collapse_sidebar(root)
        bosses = self.visible_boss_docs(hwnd)
        opened = 'already_open'
        if not bosses:
            opened = self.open_boss_tab(root)
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                time.sleep(.25)
                self.dismiss_boss_login_notice(int(hwnd))
                bosses = self.visible_boss_docs(hwnd)
                if len(bosses) == 1:
                    break
        if len(bosses) != 1:
            raise ValueError('boss_page_not_ready_after_open')
        sidebar = self.collapse_sidebar(self.uia.ElementFromHandle(int(hwnd)))
        binding = self.bind(hwnd, chat_title)
        return {
            'state': 'ready',
            'opened': opened,
            'sidebar': sidebar,
            'chat_title': chat_title,
            **binding}

    def auto_bind(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        allowed_pids = set()
        for image in ('ChatGPT.exe', 'Codex.exe'):
            try:
                output = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {image}', '/FO', 'CSV', '/NH'],
                                        capture_output=True, text=True, encoding='utf-8-sig',
                                        timeout=3, creationflags=subprocess.CREATE_NO_WINDOW).stdout
                for line in output.splitlines():
                    if line.startswith('"'):
                        allowed_pids.add(int(line.split('","')[1]))
            except Exception:
                pass
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        matches = []
        def collect(hwnd, _):
            if not user.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in allowed_pids:
                return True
            try:
                docs = self.window_docs(int(hwnd))
                chats = [d for d in docs if self.url(d).startswith('app://') and not d.CurrentIsOffscreen]
                bosses = [d for d in docs if self.url(d).startswith('https://www.zhipin.com/web/chat/')
                          and not d.CurrentIsOffscreen]
                if len(chats) == 1 and len(bosses) == 1:
                    matches.append((int(hwnd), chats[0].CurrentName))
            except Exception:
                pass
            return True
        callback = callback_type(collect)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows(callback, 0)
        if len(matches) != 1:
            raise ValueError('unique_visible_codex_boss_window_required')
        return self.bind(*matches[0])

    def invoke(self, node):
        # Native MSAA default action first; choose a route BEFORE dispatch, never retry an unknown dispatch.
        legacy = self.pattern(node, 10018, U.IUIAutomationLegacyIAccessiblePattern)
        if legacy and legacy.CurrentDefaultAction:
            legacy.DoDefaultAction()
            return 'msaa_default_action'
        invoke = self.pattern(node, 10000, U.IUIAutomationInvokePattern)
        if invoke:
            invoke.Invoke()
            return 'uia_invoke'
        select = self.pattern(node, 10010, U.IUIAutomationSelectionItemPattern)
        if select:
            select.Select()
            return 'uia_select'
        raise ValueError('native_action_pattern_unavailable')

    def invoke_with_parents(self, node):
        for _ in range(6):
            if self.pattern(node, 10000, U.IUIAutomationInvokePattern):
                return self.invoke(node)
            legacy = self.pattern(node, 10018, U.IUIAutomationLegacyIAccessiblePattern)
            if legacy and legacy.CurrentDefaultAction:
                return self.invoke(node)
            node = self.walker.GetParentElement(node)
        raise ValueError('native_named_target_not_invokable')

    def invoke_near(self, node, depth=2):
        """Invoke the node or a close ancestor; never the whole document."""
        for _ in range(depth + 1):
            if self.pattern(node, 10000, U.IUIAutomationInvokePattern):
                return self.invoke(node)
            legacy = self.pattern(node, 10018, U.IUIAutomationLegacyIAccessiblePattern)
            if legacy and legacy.CurrentDefaultAction:
                return self.invoke(node)
            try:
                node = self.walker.GetParentElement(node)
            except Exception:
                break
        return None

    def click_node_center(self, node):
        """Invoke a node, or click the verified center of its closest container."""
        route = self.invoke_near(node, depth=1)
        if route:
            return route
        target = node
        try:
            parent = self.walker.GetParentElement(node)
            if parent is not None:
                inner = node.CurrentBoundingRectangle
                outer = parent.CurrentBoundingRectangle
                if (outer.right > outer.left and outer.bottom > outer.top
                        and outer.left <= inner.left and outer.top <= inner.top
                        and outer.right >= inner.right and outer.bottom >= inner.bottom):
                    target = parent
        except Exception:
            pass
        rect = target.CurrentBoundingRectangle
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise ValueError('toolbar_target_not_clickable')
        if foreground_hwnd() != int(self.cfg['binding']['hwnd']):
            raise ValueError('native_click_requires_foreground')
        click_screen_point((rect.left + rect.right) // 2,
                           (rect.top + rect.bottom) // 2)
        return 'screen_click'

    def confirm_exchange_dialog(self, label):
        """BOSS asks 确定与对方交换微信/电话吗？ before sending the request."""
        texts = {
            '换微信': '确定与对方交换微信吗？',
            '换电话': '确定与对方交换电话吗？',
            '求简历': '确定与对方交换简历吗？',
        }
        dialog_text = texts.get(label)
        if not dialog_text:
            return None
        for _ in range(8):
            time.sleep(.25)
            try:
                root = self.bound()
            except Exception:
                continue
            dialog = [n for n in self.find_all_named(root, dialog_text)
                      if not n.CurrentIsOffscreen]
            if not dialog:
                continue
            rect = dialog[0].CurrentBoundingRectangle
            center = ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2)
            confirms = [n for n in self.find_all_named(root, '确定')
                        if not n.CurrentIsOffscreen]
            if confirms:
                def distance(node):
                    box = node.CurrentBoundingRectangle
                    return (abs((box.left + box.right) / 2 - center[0])
                            + abs((box.top + box.bottom) / 2 - center[1]))
                confirms.sort(key=distance)
                return self.click_node_center(confirms[0])
            cancels = [n for n in self.find_all_named(root, '取消')
                       if not n.CurrentIsOffscreen]
            if cancels:
                self.click_node_center(cancels[0])
            raise ValueError('exchange_confirm_unavailable:' + label)
        return None

    def named_action(self, root, label):
        found = [n for n in self.all(root) if n.CurrentName == label and not n.CurrentIsOffscreen]
        if len(found) > 1:
            preferred = [n for n in found
                         if n.CurrentControlType in (50020, 50005)]
            if preferred:
                found = preferred
        if len(found) > 1:
            def area(node):
                rect = node.CurrentBoundingRectangle
                return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            found.sort(key=area)
            if area(found[1]) > area(found[0]) * 1.2:
                found = [found[0]]
        if len(found) != 1:
            raise ValueError('native_named_target_not_unique:' + label)
        return self.invoke_with_parents(found[0])

    def find_toolbar(self, label):
        root = self.bound()
        found = []
        for node in self.all(root):
            try:
                if node.CurrentIsOffscreen:
                    continue
                name = (node.CurrentName or '').strip()
                if name == label or (name.startswith(label)
                                     and len(name) <= len(label) + 3):
                    found.append(node)
            except Exception:
                continue
        if len(found) > 1:
            links = [n for n in found if n.CurrentControlType == 50005]
            if len(links) == 1:
                found = links
        if len(found) != 1:
            raise ValueError('toolbar_target_not_unique:' + label)
        return found[0]

    def click_toolbar(self, label):
        """Click a BOSS conversation toolbar action by its visible label.

        The WeChat/phone exchange spans can carry a remaining-count badge, so
        the label may be followed by a short number.
        """
        node = self.find_toolbar(label)
        if not node.CurrentIsEnabled:
            raise ValueError('toolbar_target_disabled:' + label)
        route = self.click_node_center(node)
        confirmed_dialog = self.confirm_exchange_dialog(label)
        return {'state': 'exchange_sent' if confirmed_dialog else 'exchange_clicked',
                'label': label, 'route': route,
                'confirm_route': confirmed_dialog}

    def dismiss_boss_login_notice(self, hwnd):
        # Chromium exposes this tab-modal as a separate RootView window rather
        # than a document descendant. Dismiss only the exact known login notice.
        try:
            root = self.uia.ElementFromHandle(int(hwnd))
            dialogs = [n for n in self.all(root)
                       if n.CurrentControlType == 50032
                       and 'zhipin.com' in (n.CurrentName or '').lower()]
        except Exception:
            return 'not_found'
        for dialog in dialogs:
            try:
                items = self.all(dialog)
            except Exception:
                continue
            notice = [n for n in items if n.CurrentControlType == 50020
                      and ('登录过' in (n.CurrentName or '')
                           or '请勿重复登录' in (n.CurrentName or ''))]
            if not notice:
                continue
            buttons = [n for n in items if n.CurrentControlType == 50000
                       and (n.CurrentName or '').strip() in ('确定', '确认', 'OK')
                       and not n.CurrentIsOffscreen]
            if len(buttons) != 1:
                raise ValueError('boss_login_notice_confirm_not_unique')
            self.invoke(buttons[0])
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    remaining = [n for n in self.all(self.uia.ElementFromHandle(int(hwnd)))
                                 if n.CurrentControlType == 50032
                                 and 'zhipin.com' in (n.CurrentName or '').lower()
                                 and not n.CurrentIsOffscreen]
                except Exception:
                    remaining = []
                if not remaining:
                    return 'dismissed'
                time.sleep(.15)
            raise ValueError('boss_login_notice_dismiss_unconfirmed')
        return 'not_found'

    def inspect(self, pattern='chat|message|editor|user-list|job|select|position|name'):
        root = self.bound()
        hints = []
        for n in self.all(root):
            if not re.search(pattern, n.CurrentClassName):
                continue
            legacy = self.pattern(n, 10018, U.IUIAutomationLegacyIAccessiblePattern)
            hints.append({'class': n.CurrentClassName, 'name': n.CurrentName, 'id': n.CurrentAutomationId,
                          'runtime': self.rid(n), 'offscreen': bool(n.CurrentIsOffscreen),
                          'parent_class': self.walker.GetParentElement(n).CurrentClassName,
                          'legacy_action': legacy.CurrentDefaultAction if legacy else None,
                          'value_pattern': bool(self.pattern(n, 10002, U.IUIAutomationValuePattern)),
                          'scroll_pattern': bool(self.pattern(n, 10004, U.IUIAutomationScrollPattern))})
            if len(hints) >= 100:
                break
        return {'state': 'inspect', 'url': self.url(root), 'hints': hints}

    def wait_url(self, fragment, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if fragment in self.url(self.bound()):
                    return
            except Exception:
                pass
            time.sleep(.15)
        raise ValueError('boss_page_navigation_not_confirmed')

    def wait_messages_ready(self, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                root = self.bound()
                if '/web/chat/index' not in self.url(root):
                    raise ValueError('message_page_not_opened')
                editors = self.nodes(root, self.cfg['profile']['editor_class'], visible=True)
                rows = self.nodes(root, self.cfg['profile']['item_class'], visible=True)
                names = self.nodes(root, self.cfg['profile']['name_class'], visible=True)
                jobs = self.nodes(root, self.cfg['profile']['job_class'], visible=True)
                if len(editors) == 1 and rows and len(names) == 1 and len(jobs) == 1:
                    return root
            except Exception:
                pass
            time.sleep(.2)
        raise ValueError('message_page_not_ready')

    def child_texts(self, node):
        values = []
        for item in self.all(node):
            if item.CurrentControlType == 50020 and item.CurrentName.strip():
                values.append(item.CurrentName)
        return values

    def detect_account(self):
        root = self.bound()
        ignored = {'更多', '全部职位', '今日数据'}
        candidates = []
        for node in self.all(root):
            if 'ui-dropmenu-label' not in node.CurrentClassName.split() or node.CurrentIsOffscreen:
                continue
            values = self.text_values(node)
            if len(values) == 1 and values[0] not in ignored and values[0].strip():
                candidates.append(values[0].strip())
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) != 1:
            raise ValueError('boss_account_name_not_unique')
        return candidates[0]

    def inventory(self):
        root = self.bound()
        if '/web/chat/data-recruit' not in self.url(root):
            self.named_action(root, '招聘数据')
            self.wait_url('/web/chat/data-recruit')
        deadline = time.monotonic() + 8
        while True:
            root = self.bound()
            selections = [n for n in self.all(root)
                          if 'ui-select-selection' in n.CurrentClassName.split()
                          and not n.CurrentIsOffscreen]
            candidates = []
            for node in selections:
                parent = node
                for _ in range(3):
                    parent = self.walker.GetParentElement(parent)
                    if 'select-job' in parent.CurrentClassName.split():
                        candidates.append(node)
                        break
            if not candidates:
                candidates = [n for n in self.all(root)
                              if not n.CurrentIsOffscreen and n.CurrentName.strip()
                              and ('\ue603' in n.CurrentName or n.CurrentName.endswith(''))]
            if len(candidates) == 1:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(.2)
        if len(candidates) != 1:
            raise ValueError('inventory_job_dropdown_not_unique')
        dropdowns = [n for n in self.all(root)
                     if 'ui-dropdown-list' in n.CurrentClassName.split()
                     and not n.CurrentIsOffscreen]
        if not dropdowns:
            self.invoke(candidates[0])
            time.sleep(.3)
            dropdowns = [n for n in self.all(self.bound())
                         if 'ui-dropdown-list' in n.CurrentClassName.split()
                         and not n.CurrentIsOffscreen]
        if len(dropdowns) != 1:
            raise ValueError('inventory_dropdown_not_unique')
        dropdown = dropdowns[0]
        option_nodes = [n for n in self.all(dropdown)
                        if n.CurrentControlType == 50007
                        and 'ui-select-item' in n.CurrentClassName.split()
                        and n.CurrentName.strip()]
        options = [recover_inventory_title(n.CurrentName, self.child_texts(n))
                   for n in option_nodes]
        if '全部职位' not in options:
            raise ValueError('inventory_dropdown_not_opened')
        discovered = list(dict.fromkeys(value for value in options if value != '全部职位'))
        if not discovered:
            raise ValueError('inventory_dropdown_empty')
        # User-defined scope: the current visible options in the Recruitment
        # Data job selector are the daily jobs that may be mapped. Do not open
        # Job Management or infer additional statuses.
        jobs = []
        for title in discovered:
            closed = bool(re.search(r'[（(]关闭[)）]', title))
            jobs.append({
                'platform_id': '',
                'title': title,
                'status': 'inactive' if closed else 'active',
                'status_evidence': ('招聘数据岗位名称含“关闭”' if closed
                                    else '招聘数据右上角岗位下拉可见岗位'),
                'visibility_warning': '',
            })
        account = self.detect_account()
        selected = [n for n in option_nodes
                    if 'ui-select-item-selected' in n.CurrentClassName.split()]
        if len(selected) == 1:
            try:
                self.invoke(selected[0])
                time.sleep(.2)
            except Exception:
                pass
        return {'source': 'native_uia', 'account': account, 'complete': True, 'jobs': jobs,
                'daily_dropdown_count': len(discovered)}

    def selected(self, root):
        p = self.cfg['profile']
        rows = self.nodes(self.one(root, p['list_class'], visible=True), p['item_class'])
        header_name = self.single_text(root, p['name_class'], 'candidate_name_not_unique')
        header_job = self.single_text(root, p['job_class'], 'job_title_not_unique')
        selected = []
        for row in rows:
            try:
                fields = self.contact_fields(row)
            except ValueError:
                continue
            if fields['candidate_name'] == header_name and fields['job_title'] == header_job:
                selected.append(row)
        if len(selected) != 1:
            raise ValueError('selected_contact_not_unique')
        return selected[0]

    def identity(self, node, name=''):
        aid = node.CurrentAutomationId
        if aid:
            return 'automation:' + aid
        b = self.cfg['binding']
        return 'session:' + hash_value([
            b.get('hwnd'), b.get('pid'), b.get('birth'), b.get('chat_title'),
            self.cfg.get('account', ''),
            self.rid(node), name,
        ])[:32]

    def snapshot(self, allow_unmapped=False):
        root, p = self.bound(), self.cfg['profile']
        if not allow_unmapped and (not p.get('verified') or not p.get('stable_ids_verified')):
            raise ValueError('native_calibration_required')
        row = self.selected(root)
        fields = self.contact_fields(row)
        name = fields['candidate_name']
        header = self.single_text(root, p['name_class'], 'candidate_header_not_unique')
        if not name or name != header:
            raise ValueError('candidate_header_not_matched')
        title = self.single_text(root, p['job_class'], 'job_header_not_unique')
        project = self.cfg.get('job_aliases', {}).get(title)
        if project in self.cfg.get('allowed_jobs', []):
            from daily import current_mapping, resolve_job
            mapping = current_mapping(Path(self.cfg['state_dir']), self.cfg['account'])
            if not mapping or resolve_job(mapping, title)['cloud_project'] != project:
                raise ValueError('daily_mapping_expired_or_changed')
            knowledge_position = resolve_job(mapping, title)['cloud_position']
            mapping_hash = hash_value(mapping)
        elif allow_unmapped:
            project = 'native-test'
            knowledge_position = ''
            mapping_hash = ''
        else:
            raise ValueError('job_not_mapped')
        scope = self.one(root, p['message_root_class'], visible=True)
        records = []
        for message in self.nodes(scope, p['message_class']):
            direct = []
            child = self.walker.GetFirstChildElement(message)
            while child:
                direct.append(child.CurrentClassName)
                child = self.walker.GetNextSiblingElement(child)
            role = classify_message(direct, p['incoming_classes'], p['outgoing_classes'],
                                    p.get('system_classes', []))
            records.append((role, message))
        first_system = next((index for index, (role, _) in enumerate(records) if role == 'system'), None)
        # The first native job card marks the start of this conversation. Earlier
        # text is account-operation chrome such as "沟通钥匙" consumption.
        dialogue = records[first_system + 1:] if first_system is not None else records
        messages = []
        non_text_last = False
        non_text_node = None
        non_text_role = ''
        exchange_request = False
        for role, message in dialogue:
            if role == 'system':
                try:
                    for value in self.text_values(
                            message, excluded=_MESSAGE_METADATA):
                        if is_exchange_request_text(value):
                            exchange_request = True
                            break
                except Exception:
                    pass
                continue
            if role == 'unknown':
                if message.CurrentIsOffscreen:
                    continue
                raise ValueError('message_direction_unknown')
            text_boxes = self.nodes(message, p['message_text_class'])
            values = []
            if len(text_boxes) == 1:
                values = self.text_values(text_boxes[0], excluded=_MESSAGE_METADATA)
            if not values:
                if message.CurrentIsOffscreen:
                    continue
                # Image, file or platform card: remember that the latest
                # visible event is not text so the conversation goes to a
                # human without blocking every other conversation.
                non_text_last = True
                non_text_node = message
                non_text_role = role
                continue
            text = '\n'.join(values).strip()
            if not text:
                non_text_last = True
                non_text_node = message
                non_text_role = role
                continue
            if role == 'recruiter' and is_platform_notice_text(text):
                role = 'system'
            if role == 'system':
                continue
            messages.append({'id': self.identity(message, text), 'role': role, 'text': text, 'kind': 'text'})
            non_text_last = False
        if not messages:
            if non_text_last and non_text_node is not None:
                messages = [{'id': self.identity(non_text_node, '[附件]'),
                             'role': non_text_role if non_text_role in
                             ('candidate', 'recruiter') else 'candidate',
                             'text': '[图片/附件]', 'kind': 'text'}]
            else:
                raise ValueError('no_readable_dialogue')
        return {'conversation_id': self.contact_id(fields, row=row), 'candidate_name': name, 'job_id': project,
                'knowledge_position': knowledge_position, 'mapping_hash': mapping_hash,
                'job_title': title, 'messages': messages,
                'non_text_last': non_text_last,
                'exchange_request': exchange_request,
                'draft': self.editor_text(self.one(root, p['editor_class'], visible=True)),
                'identity_verified': True, 'identity_scope': 'logical_name_job_and_runtime_messages',
                'complete': True}

    def listing_records(self):
        p = self.cfg['profile']
        last_error = None
        listing = None
        for _ in range(4):
            try:
                listing = self.one(self.bound(), p['list_class'], visible=True)
                break
            except ValueError as exc:
                last_error = exc
                if ('not_unique' not in str(exc)
                        and 'NULL COM' not in str(exc)):
                    raise
                time.sleep(.3)
        if listing is None:
            raise last_error
        records = []
        for row in self.nodes(listing, p['item_class']):
            try:
                fields = self.contact_fields(row)
            except ValueError:
                raise ValueError('inbox_row_not_readable')
            unread = bool(fields['unread'])
            if p.get('unread_class') and self.nodes(row, p['unread_class']):
                unread = True
            records.append({'fields': fields, 'unread': unread, 'runtime': self.rid(row),
                            'visible': not row.CurrentIsOffscreen})
        return records

    def scan(self, deep=False):
        root, p = self.bound(), self.cfg['profile']
        if not p.get('verified'):
            raise ValueError('native_calibration_required')
        editors = self.nodes(root, p['editor_class'], visible=True)
        if len(editors) == 1 and self.editor_text(editors[0]):
            raise ValueError('human_draft_present')
        records = self.listing_records()
        ids, unread = [], []
        for record in records:
            fields = record['fields']
            cid = self.contact_id(fields, runtime=record['runtime'])
            ids.append(cid)
            if record['unread']:
                unread.append(cid)
        if not unread:
            badge = self.menu_unread_count(root)
            if badge:
                # The list may be scrolled below the red dots. Reset it to the
                # top, then filter to unread conversations if still unseen.
                try:
                    self.click_list_tab('全部')
                    time.sleep(.7)
                    records = self.listing_records()
                    ids, unread = [], []
                    for record in records:
                        fields = record['fields']
                        cid = self.contact_id(fields, runtime=record['runtime'])
                        ids.append(cid)
                        if record['unread']:
                            unread.append(cid)
                    if not unread:
                        self.click_list_tab('未读')
                        time.sleep(1.2)
                        try:
                            switched = self.listing_records()
                        except Exception:
                            switched = None
                        if switched:
                            records = switched
                            ids, unread = [], []
                            for record in records:
                                fields = record['fields']
                                cid = self.contact_id(fields, runtime=record['runtime'])
                                ids.append(cid)
                                unread.append(cid)
                        else:
                            self.click_list_tab('全部')
                            time.sleep(.8)
                except Exception:
                    pass
        if len(ids) != len(set(ids)):
            raise ValueError('contact_identity_collision')
        return {'complete': True, 'scan_scope': 'loaded_sidebar_window',
                'conversation_ids': ids, 'unread_ids': unread,
                'loaded_conversation_count': len(ids),
                'visible_conversation_count': sum(record['visible'] for record in records)}

    def menu_unread_count(self, root=None):
        root = root or self.bound()
        try:
            condition = self.uia.CreatePropertyCondition(30003, 50005)
            found = root.FindAll(4, condition)
            for index in range(found.Length):
                node = found.GetElement(index)
                if node.CurrentIsOffscreen:
                    continue
                match = re.fullmatch(
                    r'消息\s*(\d+)', (node.CurrentName or '').strip())
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        return 0

    def click_list_tab(self, label):
        root = self.bound()
        found = [n for n in self.all(root)
                 if not n.CurrentIsOffscreen
                 and (n.CurrentName or '').strip() == label]
        if not found:
            raise ValueError('list_tab_not_found:' + label)
        # The list sub-tabs sit below the top inbox tabs; pick the lowest.
        found.sort(key=lambda n: n.CurrentBoundingRectangle.top, reverse=True)
        return self.click_node_center(found[0])

    def read(self, cid, candidate_name=None, job_title=None):
        root, p = self.bound(), self.cfg['profile']
        editors = self.nodes(root, p['editor_class'], visible=True)
        if len(editors) > 1:
            raise ValueError('editor_not_unique')
        if len(editors) == 1 and self.editor_text(editors[0]):
            raise ValueError('human_draft_present')
        rows = self.nodes(self.one(root, p['list_class'], visible=True), p['item_class'])
        matches = []
        for row in rows:
            fields = self.contact_fields(row)
            if (self.contact_id(fields, row=row) == cid
                    or (candidate_name and job_title
                        and fields['candidate_name'] == candidate_name
                        and fields['job_title'] == job_title)):
                matches.append(row)
        if len(matches) != 1:
            raise ValueError('conversation_not_uniquely_available')
        selected = None
        if editors:
            try:
                selected = self.selected(root)
            except ValueError:
                selected = None
        if selected is None or self.rid(selected) != self.rid(matches[0]):
            self.invoke(matches[0])
            deadline = time.monotonic() + 5
            last_error = None
            while True:
                try:
                    result = self.snapshot()
                    if (result['conversation_id'] == cid
                            or (candidate_name and job_title
                                and result.get('candidate_name') == candidate_name
                                and result.get('job_title') == job_title)):
                        return result
                except ValueError as exc:
                    last_error = exc
                    if 'job_not_mapped' in str(exc):
                        raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(.2)
            if last_error:
                raise ValueError('conversation_snapshot_failed:' + str(last_error))
            raise ValueError('conversation_not_opened')
        result = self.snapshot()
        if (result['conversation_id'] != cid
                and not (candidate_name and job_title
                         and result.get('candidate_name') == candidate_name
                         and result.get('job_title') == job_title)):
            raise ValueError('conversation_not_opened')
        return result

    def send(self, expected, text):
        from engine import Store
        p = self.cfg['profile']
        if not p.get('send_verified') and not p.get('first_send_verification_required'):
            raise ValueError('native_send_not_verified')
        before = self.snapshot()
        if Store.version(before) != Store.version(expected) or before['draft'].strip():
            raise ValueError('pre_send_context_changed')
        self.type_editor_text(text)
        fresh = self.snapshot()
        comparison = dict(fresh, draft='')
        if Store.version(comparison) != Store.version(expected) or fresh['draft'] != text:
            raise ValueError('context_changed_during_typing')
        send_token = p.get('send_class') or p.get('send_name') or ''
        send_nodes = self.nodes(self.bound(), send_token, visible=True)
        if len(send_nodes) != 1 or not send_nodes[0].CurrentIsEnabled:
            raise ValueError('send_control_not_unique')
        self.invoke(send_nodes[0])
        old_ids = {m['id'] for m in expected['messages']}
        last_error = None
        for _ in range(24):
            time.sleep(.25)
            try:
                current = self.snapshot()
            except Exception as exc:
                # Chromium can briefly invalidate UIA objects while the list
                # refreshes after submit. Keep polling before declaring unknown.
                last_error = exc
                continue
            if current['conversation_id'] != expected['conversation_id']:
                if (current.get('candidate_name') != expected.get('candidate_name')
                        or current.get('job_title') != expected.get('job_title')
                        or current.get('job_id') != expected.get('job_id')):
                    raise ValueError('conversation_changed_after_send')
            sent = [m for m in current['messages'] if m['id'] not in old_ids and m['role'] == 'recruiter' and m['text'] == text]
            if len(sent) == 1 and not current['draft'].strip():
                return {'state': 'sent', 'conversation_id': expected['conversation_id'],
                        'job_id': expected['job_id'],
                        'message_id': sent[0]['id'], 'text': text,
                        'selection_cleared': False, 'selection_kept': True}
        if last_error is not None:
            raise last_error
        raise ValueError('send_result_unknown')

    def clear_selection(self):
        try:
            root = self.bound()
            selected = []
            for node in self.all(root):
                if 'chat-label-item' not in node.CurrentClassName.split():
                    continue
                if 'selected' not in node.CurrentClassName.split() or node.CurrentIsOffscreen:
                    continue
                values = self.text_values(node)
                if values and values[0] == '全部':
                    selected.append(node)
            if not selected:
                return {'state': 'already_cleared'}
            if len(selected) != 1:
                return {'state': 'selection_tab_not_unique'}
            self.invoke(selected[0])
        except Exception as exc:
            # A confirmed sent message must not become "unknown" only because
            # the list refresh invalidated a UIA object during cleanup.
            return {'state': 'selection_clear_unconfirmed',
                    'error': str(exc) or type(exc).__name__}
        time.sleep(.8)
        try:
            editors = self.nodes(self.bound(), self.cfg['profile']['editor_class'], visible=True)
            return {'state': 'cleared' if not editors else 'selection_clear_unconfirmed'}
        except Exception as exc:
            return {'state': 'selection_clear_unconfirmed',
                    'error': str(exc) or type(exc).__name__}

    def send_segments(self, expected, segments):
        from engine import Store
        p = self.cfg['profile']
        if not p.get('send_verified') and not p.get('first_send_verification_required'):
            raise ValueError('send_not_calibrated')
        if (not isinstance(segments, list) or not 1 <= len(segments) <= 3
                or any(not isinstance(x, str) or not x.strip() for x in segments)):
            raise ValueError('reply_segments_invalid')
        segments = [x.strip().rstrip('。').strip() for x in segments]
        if any(not x or len(x) > 300 for x in segments):
            raise ValueError('reply_segments_invalid')
        before = self.snapshot()
        if Store.version(before) != Store.version(expected) or before['draft'].strip():
            raise ValueError('pre_send_context_changed')
        current = expected
        message_ids = []
        texts = []
        for text in segments:
            self.type_editor_text(text)
            typed = self.snapshot()
            comparison = dict(typed, draft='')
            same_thread = (
                comparison.get('candidate_name') == current.get('candidate_name')
                and comparison.get('job_title') == current.get('job_title')
                and comparison.get('job_id') == current.get('job_id'))
            if not same_thread or Store.version(dict(comparison, conversation_id=current['conversation_id'])) != Store.version(current) or typed['draft'] != text:
                raise ValueError('context_changed_during_typing')
            send_token = p.get('send_class') or p.get('send_name') or ''
            send_nodes = self.nodes(self.bound(), send_token, visible=True)
            if len(send_nodes) != 1 or not send_nodes[0].CurrentIsEnabled:
                raise ValueError('send_control_not_unique')
            self.invoke(send_nodes[0])
            old_ids = {m['id'] for m in typed['messages']}
            last_error = None
            sent = None
            last_snapshot = None
            for _ in range(24):
                time.sleep(.25)
                try:
                    last_snapshot = self.snapshot()
                except Exception as exc:
                    last_error = exc
                    continue
                if last_snapshot['conversation_id'] != current['conversation_id']:
                    if (last_snapshot.get('candidate_name') != current.get('candidate_name')
                            or last_snapshot.get('job_title') != current.get('job_title')
                            or last_snapshot.get('job_id') != current.get('job_id')):
                        raise ValueError('conversation_changed_after_send')
                candidates = [
                    m for m in last_snapshot['messages']
                    if m['id'] not in old_ids and m['role'] == 'recruiter'
                    and m['text'] == text]
                if len(candidates) == 1 and not last_snapshot['draft'].strip():
                    sent = candidates[0]
                    break
            if sent is None:
                if last_error is not None:
                    raise last_error
                raise ValueError('send_result_unknown')
            message_ids.append(sent['id'])
            texts.append(text)
            current = dict(last_snapshot, draft='')
            time.sleep(random.uniform(.35, .8))
        return {
            'state': 'sent',
            'conversation_id': expected['conversation_id'],
            'job_id': expected['job_id'],
            'message_id': message_ids[0],
            'message_ids': message_ids,
            'texts': texts,
            'text': ' '.join(texts),
            'selection_cleared': False,
            'selection_kept': True,
        }

    def test_send(self, expected_name, text):
        from engine import Store
        if not self.cfg['profile'].get('draft_verified'):
            raise ValueError('native_draft_not_verified')
        before = self.snapshot(allow_unmapped=True)
        if before['candidate_name'] != expected_name:
            raise ValueError('test_conversation_not_matched')
        if before['draft']:
            raise ValueError('human_draft_present')
        time.sleep(random.uniform(.8, 1.5))
        self.type_editor_text(text)
        time.sleep(random.uniform(.8, 1.6))
        fresh = self.snapshot(allow_unmapped=True)
        comparison = dict(fresh, draft='')
        if Store.version(comparison) != Store.version(before) or fresh['draft'] != text:
            raise ValueError('context_changed_before_test_send')
        send_nodes = self.nodes(self.bound(), self.cfg['profile']['send_class'], visible=True)
        if len(send_nodes) != 1 or not send_nodes[0].CurrentIsEnabled:
            raise ValueError('send_control_not_unique')
        self.invoke(send_nodes[0])
        old_ids = {message['id'] for message in before['messages']}
        for _ in range(20):
            time.sleep(.25)
            current = self.snapshot(allow_unmapped=True)
            sent = [message for message in current['messages']
                    if message['id'] not in old_ids and message['role'] == 'recruiter'
                    and message['text'] == text]
            if len(sent) == 1 and not current['draft']:
                return {'state': 'test_sent', 'conversation_id': current['conversation_id'],
                        'message_id': sent[0]['id'], 'text': text,
                        'selection_cleared': False, 'selection_kept': True}
        raise ValueError('send_result_unknown')

    def draft_test(self, text, clear=True):
        if self.editor_text():
            raise ValueError('human_draft_present')
        editor = self.type_editor_text(text)
        if self.editor_text(editor) != text:
            raise ValueError('draft_result_not_verified')
        result = {'state': 'draft_verified', 'length': len(text), 'cleared': False}
        if clear:
            self.clear_editor(text)
            result.update(state='draft_verified_and_cleared', cleared=True)
        return result

    def draft_text(self):
        """Read the editor without treating its content as a takeover latch."""
        return {'state': 'draft', 'text': self.editor_text()}

    def send_many(self, expected, groups):
        """Send several reply groups to the same conversation in order."""
        if (not isinstance(groups, list) or not 1 <= len(groups) <= 3
                or any(not isinstance(group, list) for group in groups)):
            raise ValueError('reply_groups_invalid')
        current = expected
        message_ids = []
        texts = []
        for index, group in enumerate(groups):
            receipt = self.send_segments(current, group)
            message_ids.extend(receipt.get('message_ids') or [])
            texts.extend(receipt.get('texts') or [])
            if index + 1 < len(groups):
                current = self.snapshot()
        return {
            'state': 'sent',
            'conversation_id': expected['conversation_id'],
            'job_id': expected['job_id'],
            'message_ids': message_ids,
            'texts': texts,
            'message_id': message_ids[0] if message_ids else '',
            'text': ' '.join(texts),
        }

    def click_exchange(self, kind):
        labels = {'wechat': '换微信', 'phone': '换电话', 'resume': '求简历'}
        label = labels.get(kind)
        if not label:
            raise ValueError('exchange_kind_invalid')
        return self.click_toolbar(label)

    def exchange_target(self, kind):
        labels = {'wechat': '换微信', 'phone': '换电话', 'resume': '求简历'}
        label = labels.get(kind)
        if not label:
            raise ValueError('exchange_kind_invalid')
        node = self.find_toolbar(label)
        rect = node.CurrentBoundingRectangle
        return {
            'state': 'target', 'label': label,
            'enabled': bool(node.CurrentIsEnabled),
            'rect': [rect.left, rect.top, rect.right, rect.bottom],
            'foreground': foreground_hwnd() == int(self.cfg['binding']['hwnd']),
        }

    def accept_exchange_request(self):
        """Accept a candidate-initiated WeChat/phone/resume exchange."""
        root = self.bound()
        p = self.cfg['profile']
        requests = []
        buttons = []

        def collect(root):
            requests.clear()
            buttons.clear()
            for node in self.all(root):
                try:
                    name = (node.CurrentName or '').strip()
                    if not name or len(name) > 80:
                        continue
                    if is_exchange_request_text(name):
                        requests.append(node)
                    elif name in ('同意', '接受', '同意交换', '确认交换'):
                        buttons.append(node)
                except Exception:
                    continue

        collect(root)
        if not requests:
            return {'state': 'none'}
        visible = [node for node in requests if not node.CurrentIsOffscreen]
        if not visible:
            try:
                scope = self.one(root, p['message_root_class'], visible=True)
                box = scope.CurrentBoundingRectangle
                scroll_screen_wheel(
                    (box.left + box.right) // 2,
                    (box.top + box.bottom) // 2, -10)
                time.sleep(.8)
            except Exception:
                pass
            root = self.bound()
            collect(root)
            visible = [node for node in requests if not node.CurrentIsOffscreen]
        usable = [node for node in buttons if not node.CurrentIsOffscreen]
        if not usable:
            return {'state': 'request_not_visible' if not visible
                    else 'request_without_button'}
        rect = (visible[0] if visible else requests[0]).CurrentBoundingRectangle
        center = ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2)

        def distance(node):
            box = node.CurrentBoundingRectangle
            return (abs((box.left + box.right) / 2 - center[0])
                    + abs((box.top + box.bottom) / 2 - center[1]))

        usable.sort(key=distance)
        node = usable[0]
        if distance(node) > 420:
            return {'state': 'request_not_visible'}
        route = self.click_node_center(node)
        return {'state': 'accepted', 'route': route,
                'label': (node.CurrentName or '').strip()}

    def clear_draft(self, expected):
        """Clear only the exact text that the caller already verified."""
        text = self.editor_text()
        if text != expected:
            raise ValueError('draft_changed_refusing_to_clear')
        if text:
            self.clear_editor(text)
        return {'state': 'cleared', 'text': expected}


def main(request):
    bridge = Bridge(request['config'])
    action = request['action']
    if action == 'bind':
        return bridge.bind(request['window'], request['chat_title'])
    if action == 'auto_bind':
        return bridge.auto_bind()
    if action == 'open_boss':
        return bridge.open_boss()
    if action in ('inspect', 'probe'):
        return bridge.inspect(request.get('filter') or 'chat|message|editor|job|select|position|name')
    if action == 'prepare_messages':
        root = bridge.bound()
        target = 'https://www.zhipin.com/web/chat/index'
        if bridge.url(root).split('?')[0].rstrip('/') != target:
            menu = bridge.one(root, 'menu-chat')
            candidates = [n for n in bridge.all(menu) if n.CurrentControlType == 50005 and
                          not n.CurrentIsOffscreen]
            if len(candidates) != 1:
                raise ValueError('message_page_entry_not_unique')
            bridge.invoke(candidates[0])
            time.sleep(.7)
        try:
            bridge.wait_messages_ready(timeout=2)
        except ValueError:
            root = bridge.bound()
            rows = bridge.nodes(root, bridge.cfg['profile']['item_class'], visible=True)
            if not rows:
                raise ValueError('conversation_rows_not_visible')
            target = rows[0]
            for row in rows:
                try:
                    if bridge.contact_fields(row)['unread']:
                        target = row
                        break
                except ValueError:
                    continue
            bridge.invoke(target)
            bridge.wait_messages_ready()
        return {'state': 'messages_ready'}
    if action == 'read_current':
        try:
            return {'state': 'snapshot', 'snapshot': bridge.snapshot()}
        except ValueError as exc:
            message = str(exc)
            if 'job_not_mapped' in message:
                return {'state': 'unmapped'}
            if message in (
                    'selected_contact_not_unique', 'candidate_header_not_matched',
                    'candidate_header_not_unique', 'job_header_not_unique',
                    'message_root_not_unique', 'no_readable_dialogue') or (
                    message.startswith('native_control_not_unique:')):
                return {'state': 'none'}
            raise
    if action == 'scan':
        return bridge.scan(bool(request.get('deep')))
    if action == 'inventory':
        return bridge.inventory()
    if action == 'read':
        return {'snapshot': bridge.read(
            request['conversation_id'],
            candidate_name=request.get('candidate_name'),
            job_title=request.get('job_title'))}
    if action == 'send':
        return bridge.send(request['expected'], request['text'])
    if action == 'send_segments':
        return bridge.send_segments(request['expected'], request['segments'])
    if action == 'send_many':
        return bridge.send_many(request['expected'], request['groups'])
    if action == 'click_exchange':
        return bridge.click_exchange(request['kind'])
    if action == 'exchange_target':
        return bridge.exchange_target(request['kind'])
    if action == 'accept_exchange_request':
        return bridge.accept_exchange_request()
    if action == 'test_send':
        return bridge.test_send(request['expected_name'], request['text'])
    if action == 'clear_selection':
        return bridge.clear_selection()
    if action == 'draft_test':
        return bridge.draft_test(request['text'], request.get('clear', True))
    if action == 'draft_text':
        return bridge.draft_text()
    if action == 'clear_draft':
        return bridge.clear_draft(request.get('text') or '')
    raise ValueError('unknown_native_action')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
        print(json.dumps(main(request), ensure_ascii=False))
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({'error': error}, ensure_ascii=False))
        sys.exit(1)
