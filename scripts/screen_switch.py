"""Move only the bound Codex/BOSS window between primary and secondary displays."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


SWP_PASSIVE = 0x0004 | 0x0010 | 0x0200


class MonitorInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('bounds', wintypes.RECT),
                ('work', wintypes.RECT), ('flags', wintypes.DWORD),
                ('device', wintypes.WCHAR * 32)]


def rectangle(rect):
    return rect.left, rect.top, rect.right, rect.bottom


def process_birth(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(ctypes.c_uint64)] * 4
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


class NativeDisplays:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('屏幕切换仅支持 Windows。')
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        self.api.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
        self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        self.callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        self.api.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT),
            self.callback_type, wintypes.LPARAM]
        self.api.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.api.MonitorFromWindow.restype = wintypes.HANDLE
        self.api.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.api.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.api.IsIconic.argtypes = [wintypes.HWND]

    def monitors(self):
        result = []
        errors = []

        @self.callback_type
        def visit(handle, _dc, _rect, _data):
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if not self.api.GetMonitorInfoW(handle, ctypes.byref(info)):
                errors.append(ctypes.get_last_error())
                return False
            result.append(dict(
                handle=int(handle), device=info.device,
                primary=bool(info.flags & 1),
                bounds=rectangle(info.bounds), work=rectangle(info.work)))
            return True

        if not self.api.EnumDisplayMonitors(None, None, visit, 0) or errors:
            raise OSError('无法读取当前显示器。')
        return result

    def monitor_for_window(self, hwnd):
        return int(self.api.MonitorFromWindow(hwnd, 2) or 0)

    def window_pid(self, hwnd):
        pid = wintypes.DWORD()
        if not self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise OSError('绑定的 Codex 窗口已关闭，请重新打开 BOSS。')
        return pid.value

    def move(self, hwnd, rect):
        if self.api.IsIconic(hwnd):
            raise ValueError('Codex 窗口已最小化，请先手动还原窗口。')
        left, top, right, bottom = rect
        if not self.api.SetWindowPos(
                hwnd, None, left, top, right - left, bottom - top, SWP_PASSIVE):
            raise OSError(ctypes.get_last_error(), '窗口移动失败。')


def primary_monitor(monitors):
    primary = [item for item in monitors if item['primary']]
    if len(primary) != 1:
        raise ValueError('无法确定唯一主屏，请检查 Windows 显示设置。')
    return primary[0]


def secondary_monitor(monitors, preferred=None):
    secondary = [item for item in monitors if not item['primary']]
    if not secondary:
        raise ValueError('未检测到已启用的副屏，请先连接显示器或启用虚拟副屏。')
    if preferred:
        match = next((item for item in secondary if item['device'] == preferred), None)
        if match:
            return match
    return sorted(secondary, key=lambda item: item['device'])[0]


def _verify_binding(binding, native):
    if not isinstance(binding, dict) or not binding.get('hwnd') or not binding.get('pid'):
        raise ValueError('尚未绑定 Codex/BOSS 窗口，请先打开 BOSS。')
    hwnd = int(binding['hwnd'])
    pid = int(binding['pid'])
    if native.window_pid(hwnd) != pid:
        raise ValueError('绑定的 Codex 窗口已改变，请重新打开 BOSS。')
    expected_birth = str(binding.get('birth') or '')
    if expected_birth and process_birth(pid) != expected_birth:
        raise ValueError('绑定的 Codex 进程已改变，请重新打开 BOSS。')
    return hwnd


def button_state(binding, native=None):
    native = native or NativeDisplays()
    hwnd = _verify_binding(binding, native)
    monitors = native.monitors()
    primary = primary_monitor(monitors)
    current = native.monitor_for_window(hwnd)
    on_primary = current == primary['handle']
    return dict(
        state='read',
        display='主屏' if on_primary else '副屏',
        label='副屏' if on_primary else '主屏',
        color='yellow' if on_primary else 'green',
        secondary_count=len([m for m in monitors if not m['primary']]),
        hwnd=hwnd)


def toggle(binding, preferred=None, native=None):
    native = native or NativeDisplays()
    hwnd = _verify_binding(binding, native)
    monitors = native.monitors()
    primary = primary_monitor(monitors)
    current = native.monitor_for_window(hwnd)
    target = (secondary_monitor(monitors, preferred)
              if current == primary['handle'] else primary)
    native.move(hwnd, target['work'])
    if native.monitor_for_window(hwnd) != target['handle']:
        raise ValueError('尚未确认窗口已移至目标屏幕，请检查显示设置。')
    result = dict(
        state='moved',
        display='主屏' if target['primary'] else '副屏',
        device=target['device'],
        secondary_device='' if target['primary'] else target['device'],
        hwnd=hwnd)
    result.update(button_state(binding, native))
    return result
