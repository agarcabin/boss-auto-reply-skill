import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProtocolTests(unittest.TestCase):
    def test_stdio_initialization_list_and_isolated_status(self):
        with tempfile.TemporaryDirectory() as state:
            env = dict(os.environ, BOSS_REPLY_STATE_DIR=state, PYTHONUTF8='1')
            messages = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'boss_reply_status', 'arguments': {}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'boss_reply_control', 'arguments': {'action': 'invalid'}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'boss_reply_takeover', 'arguments': {'conversation_id': 'missing', 'release': 'false'}}},
            ]
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/reply_mcp.py')],
                                    input='\n'.join(json.dumps(m) for m in messages) + '\n', env=env,
                                    text=True, encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(output), 5)
            self.assertEqual(output[0]['result']['serverInfo']['name'], 'boss-auto-reply')
            names = [t['name'] for t in output[1]['result']['tools']]
            self.assertTrue(all(n.startswith('boss_reply_') for n in names))
            self.assertNotIn('recruiting_start', names)
            for name in ('boss_reply_open', 'boss_reply_start', 'boss_reply_screen'):
                self.assertIn(name, names)
            status = json.loads(output[2]['result']['content'][0]['text'])
            self.assertEqual(status['control'], 'stopped')
            self.assertIn('heartbeat_age', status)
            self.assertTrue(output[3]['result']['isError'])
            self.assertTrue(output[4]['result']['isError'])


if __name__ == '__main__':
    unittest.main()
