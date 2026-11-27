import json
import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import reply_records
import reply_report


class ReportTests(unittest.TestCase):
    def record_one(self, root):
        recorder = reply_records.Recorder('账号A', root)
        recorder.record({
            'conversation_id': 'c1',
            'candidate_name': '张三',
            'job_id': '股权通',
            'job_title': '股权通岗位',
            'messages': [
                {'id': 'm1', 'role': 'candidate', 'text': '微信 abc12345', 'kind': 'text'},
            ],
            'draft': '',
            'identity_verified': True,
            'complete': True,
        })

    def test_report_server_serves_html_and_live_data(self):
        with tempfile.TemporaryDirectory() as temp:
            self.record_one(temp)
            reply_report.ReportHandler.template = Path(
                reply_report.__file__).with_name('reply_report.html').read_bytes()
            reply_report.ReportHandler.records_root = temp
            server = ThreadingHTTPServer(('127.0.0.1', 0), reply_report.ReportHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health') as response:
                    health = json.loads(response.read().decode('utf-8'))
                self.assertEqual(health['app'], reply_report.APP_ID)
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/data') as response:
                    data = json.loads(response.read().decode('utf-8'))
                self.assertEqual(data['metrics']['wechat'], 1)
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as response:
                    html = response.read().decode('utf-8')
                self.assertIn('BOSS 回复沟通报告', html)
                self.assertIn('查看聊天记录', html)
                self.assertIn("get('mode') === 'history'", html)
                self.assertIn('historySelect', html)
                self.assertIn('row-chat-btn', html)
                self.assertIn('<th>企业</th>', html)
                self.assertIn('enterprise-cell', html)
                self.assertIn('openDrawer(record)', html)
                self.assertNotIn('window.open', html)
                self.assertIn('handoffFilter', html)
                self.assertIn('未完全回复', html)
                self.assertIn('handoff-note', html)
            finally:
                server.shutdown()
                server.server_close()

    def test_daily_snapshot_is_self_contained_html(self):
        with tempfile.TemporaryDirectory() as temp:
            self.record_one(temp)
            result = reply_report.export_daily_snapshot(temp, date='2026-11-27')
            target = Path(result['path'])
            self.assertEqual(target.name, '2026-11-27.html')
            self.assertEqual(result['records'], 1)
            html = target.read_text(encoding='utf-8')
            self.assertIn('staticMode', html)
            self.assertIn('openDrawer(record)', html)
            self.assertNotIn('window.open', html)
            match = re.search(
                r'<script id="embeddedData" type="application/json">(.*?)</script>',
                html, re.S)
            self.assertIsNotNone(match)
            data = json.loads(match.group(1))
            self.assertEqual(data['records'][0]['candidate_name'], '张三')
            self.assertEqual(data['records'][0]['conversation_id'], 'c1')


if __name__ == '__main__':
    unittest.main()
