"""User-bound Windows DPAPI storage for the knowledge API key."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('Windows DPAPI required for stored credentials')
    array = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), ctypes.cast(array, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'BOSS knowledge API', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise OSError(ctypes.get_last_error(), 'DPAPI credential operation failed')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save_key(path, token):
    if not isinstance(token, str) or not token.startswith('rag_') or len(token) < 24:
        raise ValueError('invalid_cloud_api_key')
    path = Path(path)
    encrypted = base64.b64encode(_crypt(token.encode())).decode('ascii')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'dpapi': encrypted}), encoding='utf-8')
    os.replace(temporary, path)


def load_key(cfg):
    if os.environ.get('BOSS_KB_API_KEY'):
        return os.environ['BOSS_KB_API_KEY'].strip()
    location = cfg.get('cloud_api_key_file')
    if not location:
        return ''
    path = Path(location)
    value = path.read_text(encoding='utf-8-sig').strip()
    if value.startswith('{'):
        record = json.loads(value)
        if record.get('dpapi'):
            return _crypt(base64.b64decode(record['dpapi']), decrypt=True).decode('utf-8')
        return str(record.get('token') or record.get('api_key') or '').strip()
    return value
