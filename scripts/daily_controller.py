"""Persistent native startup controller; Save-and-start is consumed once."""
import argparse
import copy
import os
from pathlib import Path
import subprocess
import sys
import time

import reply


def save_status(account, state, message, **extra):
    reply.save(reply.STATE / 'startup-status.json',
               {'account': account, 'state': state, 'message': message, **extra})


def verify_profile(cfg):
    if cfg['profile'].get('verified') and cfg['profile'].get('stable_ids_verified'):
        return cfg
    probe = copy.deepcopy(cfg)
    probe['profile']['verified'] = True
    probe['profile']['stable_ids_verified'] = True
    last_error = None
    for _ in range(8):
        try:
            reply.Native(probe).call('prepare_messages')
            batch = reply.Native(probe).call('scan', deep=False)
            if not batch.get('complete'):
                raise ValueError('inbox_scan_incomplete')
            cfg = reply.config()
            cfg['profile']['verified'] = True
            cfg['profile']['stable_ids_verified'] = True
            cfg['profile']['loaded_list_complete_verified'] = True
            reply.save(reply.STATE / 'config.json', cfg)
            return cfg
        except Exception as exc:
            last_error = exc
            time.sleep(.5)
    raise ValueError('native_inbox_calibration_failed:' + str(last_error))


def verify_draft(cfg):
    last_error = None
    for _ in range(12):
        try:
            result = reply.Native(cfg).call('draft_test', text='测试输入，不会发送', clear=True)
            if result.get('state') != 'draft_verified_and_cleared' or not result.get('cleared'):
                raise ValueError('native_draft_calibration_incomplete')
            cfg = reply.config()
            cfg['profile']['draft_verified'] = True
            if not cfg['profile'].get('send_verified'):
                cfg['profile']['first_send_verification_required'] = True
            reply.save(reply.STATE / 'config.json', cfg)
            return cfg
        except Exception as exc:
            last_error = exc
            if 'foreground' not in str(exc) and 'focus' not in str(exc):
                raise
            time.sleep(.5)
    raise ValueError('native_editor_calibration_failed:' + str(last_error))


def consume_start(window, inventory_path):
    cfg = reply.config()
    account = reply.load(inventory_path)['account']
    save_status(account, 'calibrating', '岗位关系已保存，正在自动切换消息页并校准原生控件。')
    waiting = False
    prepared = False
    for _ in range(120):
        try:
            reply.Native(cfg).call('prepare_messages')
            prepared = True
            break
        except Exception as exc:
            if 'bound_chat_or_boss_not_visible' not in str(exc):
                raise
            if not waiting:
                save_status(account, 'waiting_for_boss',
                            '岗位关系已保存。请保持 BOSS 聊天页在 Codex 中可见，检测到后会自动继续。')
                waiting = True
            time.sleep(.75)
    if not prepared:
        raise ValueError('boss_page_not_visible')

    if window.poll() is None:
        window.terminate()
        try:
            window.wait(timeout=3)
        except subprocess.TimeoutExpired:
            window.kill()
    time.sleep(.8)
    cfg = verify_profile(cfg)
    cfg = verify_draft(cfg)
    try:
        reply.Native(cfg).call('clear_selection')
    except Exception:
        pass
    check = reply.doctor(cfg)
    if not check['ready_to_send']:
        raise ValueError('startup_preflight_failed:' + '|'.join(check['problems']))
    reply.cloud_preflight(cfg)

    path = reply.STATE / 'start-request.json'
    request = reply.load(path)
    request['state'] = 'starting'
    reply.save(path, request)
    save_status(account, 'starting', '原生页面已校准，正在启动挂机回复。')
    python = str(Path(sys.executable).with_name('python.exe'))
    result = subprocess.run(
        [python, str(reply.ROOT / 'scripts/reply.py'), 'start', '--send'],
        capture_output=True, text=True, encoding='utf-8-sig', timeout=50,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    value = __import__('json').loads(result.stdout)
    if result.returncode or value.get('state') != 'started':
        raise ValueError('listener_start_failed')
    request['state'] = 'started'
    reply.save(path, request)
    message = ('挂机已启动，发送路线已验证。'
               if cfg['profile'].get('send_verified') else
               '挂机已启动。首次真实回复将以平台新增消息回执自动完成发送验证。')
    save_status(account, 'started', message, pid=value['pid'])


def run(inventory):
    with reply.ProcessLock('daily-controller.lock'):
        current = reply.load(inventory)
        account = current['account']
        command = [sys.executable, str(reply.ROOT / 'scripts/mapping_window.py'), '--inventory', str(inventory)]
        with (reply.STATE / 'mapping-ui.log').open('a', encoding='utf-8') as log:
            window = subprocess.Popen(command, stdout=log, stderr=log,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        while window.poll() is None:
            path = reply.STATE / 'start-request.json'
            request = reply.load(path) if path.exists() else {}
            if request.get('account') != account:
                break
            if request.get('state') == 'cancelled':
                break
            if request.get('state') in ('requested', 'blocked'):
                try:
                    consume_start(window, inventory)
                except Exception as exc:
                    request['state'] = 'blocked'
                    reply.save(path, request)
                    save_status(account, 'blocked',
                                '自动进入下一步失败，已安全停止：' + str(exc))
                break
            time.sleep(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--inventory', required=True)
    args = p.parse_args()
    try:
        run(Path(args.inventory))
    except Exception as exc:
        reply.save(reply.STATE / 'startup-status.json', {'state': 'error', 'message': '启动窗口异常：' + type(exc).__name__})
