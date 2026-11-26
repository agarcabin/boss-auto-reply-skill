"""Passive, bounded calibration capture when the user displays this chat's BOSS page."""
import argparse
import json
from pathlib import Path
import time

from native_uia import Bridge
import reply


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seconds', type=int, default=50)
    p.add_argument('--window', type=int, required=True)
    p.add_argument('--chat-title', required=True)
    args = p.parse_args()
    deadline = time.monotonic() + min(55, max(1, args.seconds))
    while time.monotonic() < deadline:
        try:
            cfg = reply.config()
            b = Bridge(cfg)
            binding = b.bind(args.window, args.chat_title)['binding']
            cfg['binding'], cfg['state_dir'] = binding, str(reply.STATE)
            b.cfg = cfg
            data = b.inspect()
            reply.save(reply.STATE / 'native-calibration.json', data)
            reply.save(reply.STATE / 'config.json', cfg)
            print(json.dumps({'state': 'captured', 'url': data['url'], 'nodes': len(data['hints'])}, ensure_ascii=False))
            return
        except Exception:
            time.sleep(1)
    print(json.dumps({'state': 'not_visible', 'action': 'no_input_or_navigation_performed'}))


if __name__ == '__main__':
    main()
