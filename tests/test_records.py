import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import reply_records


def snapshot(cid='c1', messages=None, job='股权通', candidate='张三'):
    return {
        'conversation_id': cid,
        'candidate_name': candidate,
        'job_id': job,
        'job_title': job + '岗位',
        'messages': messages or [],
        'draft': '',
        'identity_verified': True,
        'complete': True,
    }


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_passive_conversation_and_exchanged_contacts_are_saved(self):
        recorder = reply_records.Recorder('账号A', self.root)
        value = snapshot(messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '你好，我微信是 abc12345，手机 13812345678，简历发你了', 'kind': 'text'},
        ])
        result = recorder.record(value)
        self.assertEqual(result['state'], 'recorded')
        record_path = Path(result['path'])
        self.assertTrue(record_path.is_file())
        self.assertTrue(record_path.with_suffix('.md').is_file())
        record = json.loads(record_path.read_text(encoding='utf-8'))
        self.assertEqual(record['initiation'], '被动招呼')
        self.assertEqual(record['contacts']['wechat']['value'], 'abc12345')
        self.assertEqual(record['contacts']['phone']['value'], '13812345678')
        self.assertEqual(record['contacts']['resume']['value'], '已交换简历')
        self.assertEqual(record['message_count'], 1)

    def test_existing_messages_merge_without_duplicates(self):
        recorder = reply_records.Recorder('账号A', self.root)
        first = snapshot(messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '还在招吗', 'kind': 'text'},
        ])
        recorder.record(first)
        second = snapshot(messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '还在招吗', 'kind': 'text'},
            {'id': 'm2', 'role': 'recruiter', 'text': '在招，方便发我一份简历', 'kind': 'text'},
        ])
        result = recorder.record(second)
        record = result['record']
        self.assertEqual(record['message_count'], 2)
        self.assertEqual(record['reply_count'], 1)
        self.assertEqual(record['contacts'].get('resume'), None)

    def test_resume_request_is_not_counted_as_exchange(self):
        recorder = reply_records.Recorder('账号A', self.root)
        record = recorder.record(snapshot(messages=[
            {'id': 'm1', 'role': 'recruiter', 'text': '简历发我看看', 'kind': 'text'},
        ]))['record']
        self.assertIsNone(record['contacts'].get('resume'))

    def test_wechat_same_as_phone_is_recorded(self):
        recorder = reply_records.Recorder('账号A', self.root)
        record = recorder.record(snapshot(messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '手机 13812345678，微信同号', 'kind': 'text'},
        ]))['record']
        self.assertEqual(record['contacts']['phone']['value'], '13812345678')
        self.assertEqual(record['contacts']['wechat']['value'], '13812345678')

    def test_manual_handoff_is_marked_and_written_to_markdown(self):
        recorder = reply_records.Recorder('账号A', self.root)
        result = recorder.record(snapshot(messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '工资多少', 'kind': 'text'},
        ]), handoff={
            'needs_human': True,
            'status': 'needs_human',
            'question': '工资多少',
            'reason': '知识库没有找到可引用的答案',
            'at': '2026-11-27T11:00:00+08:00',
            'source': 'knowledge_gap',
        })
        record = result['record']
        self.assertEqual(record['reply_status'], 'needs_human')
        self.assertEqual(record['handoff']['question'], '工资多少')
        markdown = Path(result['path']).with_suffix('.md').read_text(encoding='utf-8')
        self.assertIn('未完全回复', markdown)
        self.assertIn('知识库没有找到可引用的答案', markdown)
        data = reply_records.report_data(self.root)
        self.assertEqual(data['metrics']['needs_human'], 1)

    def test_runtime_id_change_merges_same_candidate_and_job(self):
        recorder = reply_records.Recorder('账号A', self.root)
        recorder.record(snapshot(cid='old-id', messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
        ]))
        recorder.record(snapshot(cid='new-id', messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
            {'id': 'm2', 'role': 'recruiter', 'text': '在的', 'kind': 'text'},
        ]))
        records = reply_records.load_all(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['conversation_id'], 'new-id')
        self.assertEqual(records[0]['message_count'], 2)

    def test_message_ids_are_rebuilt_without_duplicating_history(self):
        recorder = reply_records.Recorder('账号A', self.root)
        recorder.record(snapshot(messages=[
            {'id': 'old-m1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
        ]))
        record = recorder.record(snapshot(messages=[
            {'id': 'new-m1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
            {'id': 'new-m2', 'role': 'recruiter', 'text': '在的', 'kind': 'text'},
        ]))['record']
        self.assertEqual(record['message_count'], 2)
        self.assertEqual(
            [message['id'] for message in record['messages']],
            ['new-m1', 'new-m2'],
        )

    def test_existing_sqlite_snapshots_are_backfilled_and_merged(self):
        db_path = self.root / 'reply.sqlite3'
        db = sqlite3.connect(db_path)
        try:
            db.execute(
                'CREATE TABLE conversations(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
            db.execute(
                'CREATE TABLE requests(conversation TEXT, created REAL)')
            db.executemany(
                'INSERT INTO conversations VALUES(?,?)',
                [
                    ('c1', json.dumps(snapshot(cid='c1', messages=[
                        {'id': 'm1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
                    ]), ensure_ascii=False)),
                    ('c2', json.dumps(snapshot(cid='c2', messages=[
                        {'id': 'm1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
                        {'id': 'm2', 'role': 'recruiter', 'text': '在的', 'kind': 'text'},
                    ]), ensure_ascii=False)),
                ],
            )
            db.executemany(
                'INSERT INTO requests VALUES(?,?)',
                [('c1', 1764200000.0), ('c2', 1764300000.0)],
            )
            db.commit()
        finally:
            db.close()
        result = reply_records.backfill_from_store(db_path, '账号A', self.root)
        self.assertEqual(result['snapshots'], 2)
        self.assertEqual(result['records'], 1)
        record = reply_records.load_all(self.root)[0]
        self.assertEqual(record['message_count'], 2)
        self.assertEqual(record['conversation_id'], 'c2')
        self.assertEqual(
            record['first_message_time'],
            datetime.fromtimestamp(1764200000.0).astimezone().isoformat(timespec='seconds'),
        )

    def test_active_initiation_and_report_metrics(self):
        recorder = reply_records.Recorder('账号A', self.root)
        recorder.record(snapshot(cid='c1', messages=[
            {'id': 'a1', 'role': 'recruiter', 'text': '你好，看到你的简历', 'kind': 'text'},
            {'id': 'a2', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
        ]))
        recorder.record(snapshot(cid='c2', candidate='李四', messages=[
            {'id': 'b1', 'role': 'candidate', 'text': '手机 13900001111', 'kind': 'text'},
            {'id': 'b2', 'role': 'recruiter', 'text': '收到', 'kind': 'text'},
        ]))
        data = reply_records.report_data(self.root)
        self.assertEqual(data['metrics']['conversations'], 2)
        self.assertEqual(data['metrics']['replied'], 2)
        self.assertEqual(data['metrics']['messages'], 4)
        self.assertEqual(data['metrics']['phone'], 1)
        self.assertEqual(data['metrics']['active'], 1)
        self.assertEqual(data['metrics']['passive'], 1)
        self.assertEqual(
            {record['conversation_id'] for record in data['records']},
            {'c1', 'c2'},
        )

    def test_history_can_filter_by_candidate_and_job(self):
        recorder = reply_records.Recorder('账号A', self.root)
        recorder.record(snapshot(cid='c1', messages=[
            {'id': 'm1', 'role': 'candidate', 'text': '你好', 'kind': 'text'},
        ]))
        self.assertEqual(len(reply_records.history(candidate='张', root=self.root)), 1)
        self.assertEqual(len(reply_records.history(job='股权通', root=self.root)), 1)
        self.assertEqual(len(reply_records.history(candidate='李', root=self.root)), 0)


if __name__ == '__main__':
    unittest.main()
