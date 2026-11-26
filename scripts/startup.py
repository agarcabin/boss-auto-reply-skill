"""Prepare the daily inventory and launch the user-owned native mapping window."""
import json
import os
from pathlib import Path
import subprocess
import time

from daily import normalize_inventory, current_mapping
from engine import Store
import reply


def status():
    result = {'state': 'not_prepared'}
    for name in ('startup-status.json', 'start-request.json'):
        path = reply.STATE / name
        if path.exists():
            result[name.replace('.json', '')] = reply.load(path)
    if 'startup-status' in result:
        result['state'] = result['startup-status'].get('state', result['state'])
    return result


def prepare(inventory):
    normalized = normalize_inventory(inventory)
    path = reply.STATE / 'daily-inventory.json'
    reply.save(path, normalized)
    cfg = reply.config()
    cfg = reply.switch_account(cfg, normalized['account'])
    cfg['state_dir'] = str(reply.STATE)
    reply.save(reply.STATE / 'config.json', cfg)
    old = current_mapping(reply.STATE, normalized['account'], normalized)
    reply.save(reply.STATE / 'start-request.json', {
        'account': normalized['account'], 'state': 'awaiting_mapping',
        'same_day_mapping_reused': bool(old), 'send_authorized': True,
    })
    reply.save(reply.STATE / 'startup-status.json', {'account': normalized['account'], 'state': 'mapping',
               'message': '请选择云端企业和云端岗位。保存前不会自动回复。'})
    native_python = Path(cfg.get('native_python') or reply.ROOT / 'runtime/env/Scripts/python.exe')
    native_pythonw = native_python.with_name('pythonw.exe')
    if not native_pythonw.exists():
        raise ValueError('native_mapping_runtime_missing')
    launch_request = reply.STATE / 'mapping-launch.json'
    reply.save(launch_request, {'pythonw': str(native_pythonw), 'controller': str(reply.ROOT / 'scripts/daily_controller.py'),
                               'inventory': str(path), 'state_dir': str(reply.STATE)})
    script = reply.ROOT / 'scripts/launch_mapping.ps1'
    r = subprocess.run([reply.ps_host(), '-NoProfile', '-STA', '-File', str(script), '-RequestFile', str(launch_request)],
                       capture_output=True, text=True, encoding='utf-8-sig', timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise ValueError('mapping_window_launch_failed')
    return {'state': 'mapping_window_requested', 'account': normalized['account'],
            'jobs': [j['title'] for j in normalized['jobs']], 'same_day_mapping_reused': bool(old)}
