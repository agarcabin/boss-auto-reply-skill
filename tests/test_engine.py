import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from engine import Store
import reply


def snapshot(cid='c1', extra=None):
    messages = [{'id': 'm1', 'role': 'candidate', 'text': '你好', 'kind': 'text'}]
    return {'conversation_id': cid, 'job_id': '项目A', 'job_title': '项目A岗位',
            'messages': messages + (extra or []), 'draft': '', 'identity_verified': True, 'complete': True}


class Sender:
    def __init__(self, unknown=False):
        self.calls = []
        self.unknown = unknown

    def send(self, s, text):
        self.calls.append((s['conversation_id'], text))
        if self.unknown:
            raise TimeoutError()
        return {'state': 'sent', 'conversation_id': s['conversation_id'], 'job_id': s['job_id'],
                'text': text, 'message_id': 'sent-1'}


class SegmentedSender:
    def __init__(self):
        self.calls = []

    def send_segments(self, s, segments):
        self.calls.append((s['conversation_id'], list(segments)))
        return {
            'state': 'sent', 'conversation_id': s['conversation_id'],
            'job_id': s['job_id'], 'texts': list(segments),
            'message_ids': [f'sent-{i}' for i in range(len(segments))],
        }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'reply.db')
        self.store.control('resume')

    def tearDown(self):
        self.temp.cleanup()

    def event(self):
        self.store.ingest(snapshot(), ['项目A'])
        s = snapshot(extra=[{'id': 'm2', 'role': 'candidate', 'text': '工作地点呢', 'kind': 'text'}])
        r = self.store.ingest(s, ['项目A'])
        return s, self.store.pending()[0]

    def ready(self):
        s, r = self.event()
        self.store.respond(r['request_id'], r['version'], 'reply', '地点需要核实。')
        return s, r

    def test_baseline_and_duplicate_scans_do_not_reply_to_history(self):
        self.assertEqual(self.store.ingest(snapshot(), ['项目A'])['state'], 'baseline')
        self.assertEqual(self.store.ingest(snapshot(), ['项目A'])['state'], 'unchanged')
        self.assertEqual(self.store.pending(), [])

    def test_one_new_event_survives_restart_without_duplicate(self):
        s, r = self.event()
        again = Store(self.store.path)
        again.ingest(s, ['项目A'])
        self.assertEqual([x['request_id'] for x in again.pending()], [r['request_id']])

    def test_first_new_conversation_after_baseline_is_processed(self):
        result = self.store.ingest(snapshot('brand-new'), ['项目A'], new_is_event=True)
        self.assertEqual(result['state'], 'pending')

    def test_first_unread_conversation_ignores_preexisting_outbound(self):
        s = snapshot('existing-unread', extra=[
            {'id': 'notice', 'role': 'recruiter', 'text': '平台提示', 'kind': 'text'},
            {'id': 'm3', 'role': 'candidate', 'text': '为什么不回我', 'kind': 'text'},
        ])
        result = self.store.queue_unread(s, ['项目A'])
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(self.store.status()['manual_conversations'], 0)

        s['messages'].append({'id': 'human', 'role': 'recruiter',
                              'text': '人工跟进', 'kind': 'text'})
        self.assertEqual(self.store.ingest(s, ['项目A'])['reason'],
                         'human_outbound_detected')

    def test_first_unread_full_reply_chain_reaches_send(self):
        s = snapshot('unread-chain', extra=[
            {'id': 'notice', 'role': 'recruiter', 'text': '平台提示', 'kind': 'text'},
            {'id': 'm3', 'role': 'candidate', 'text': '工资多少', 'kind': 'text'},
        ])
        queued = self.store.queue_unread(s, ['项目A'])
        self.assertEqual(queued['state'], 'pending')
        request = self.store.pending()[0]
        self.store.respond(request['request_id'], request['version'], 'reply', '工资面议。')
        sender = Sender()
        result = self.store.dispatch(request['request_id'], s, sender, True)
        self.assertEqual(result['state'], 'sent')
        self.assertEqual(sender.calls, [('unread-chain', '工资面议')])

    def test_two_candidates_do_not_share_payload(self):
        self.event()
        self.store.ingest(snapshot('c2'), ['项目A'], new_is_event=True)
        requests = self.store.pending()
        self.assertEqual({r['snapshot']['conversation_id'] for r in requests}, {'c1', 'c2'})
        self.assertEqual(len({r['request_id'] for r in requests}), 2)

    def test_new_candidate_message_invalidates_generated_reply(self):
        s, r = self.ready()
        newer = copy.deepcopy(s)
        newer['messages'].append({'id': 'm3', 'role': 'candidate', 'text': '住宿呢', 'kind': 'text'})
        self.store.ingest(newer, ['项目A'])
        sender = Sender()
        self.assertEqual(self.store.dispatch(r['request_id'], newer, sender, True)['state'], 'not_ready')
        self.assertEqual(sender.calls, [])

    def test_stale_agent_response_rejected(self):
        s, r = self.event()
        with self.assertRaises(ValueError):
            self.store.respond(r['request_id'], 'wrong', 'reply', 'hello')

    def test_human_draft_latches_until_explicit_release(self):
        s, r = self.ready()
        s['draft'] = '我来回复'
        self.assertEqual(self.store.ingest(s, ['项目A'])['state'], 'manual')
        s['draft'] = ''
        self.assertEqual(self.store.ingest(s, ['项目A'])['state'], 'manual')
        self.store.manual('c1', release=True)
        self.assertEqual(self.store.status()['manual_conversations'], 0)

    def test_human_outbound_is_not_mistaken_for_candidate(self):
        s, r = self.event()
        s['messages'].append({'id': 'human', 'role': 'recruiter', 'text': '我跟你聊', 'kind': 'text'})
        self.assertEqual(self.store.ingest(s, ['项目A'])['reason'], 'human_outbound_detected')

    def test_pause_and_observe_mode_block_send(self):
        s, r = self.ready()
        sender = Sender()
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, False)['state'], 'blocked')
        self.store.control('pause')
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, True)['state'], 'blocked')
        self.assertEqual(sender.calls, [])

    def test_unknown_send_pauses_and_never_retries(self):
        s, r = self.ready()
        sender = Sender(True)
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, True)['state'], 'unknown')
        self.assertEqual(self.store.status()['control'], 'paused')
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, True)['state'], 'not_ready')
        self.assertEqual(len(sender.calls), 1)
        with self.assertRaises(ValueError):
            self.store.control('resume')
        self.store.resolve(r['request_id'], 'not_sent')
        self.store.control('resume')
        self.assertEqual(self.store.ready(), [])

    def test_process_crash_during_send_requires_platform_check(self):
        s, r = self.ready()
        with self.store.db() as db:
            db.execute("UPDATE requests SET state='sending' WHERE id=?", (r['request_id'],))
        self.store.recover()
        self.assertEqual(self.store.status()['counts']['unknown'], 1)
        self.assertEqual(self.store.status()['control'], 'paused')

    def test_success_requires_new_exact_message_receipt(self):
        s, r = self.ready()
        sender = Sender()
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, True)['state'], 'sent')
        s['messages'].append({'id': 'sent-1', 'role': 'recruiter', 'text': '地点需要核实', 'kind': 'text'})
        self.assertEqual(self.store.ingest(s, ['项目A'])['state'], 'observed')
        self.assertEqual(self.store.status()['manual_conversations'], 0)

    def test_ready_request_can_rebind_when_boss_runtime_id_changes(self):
        s, r = self.ready()
        s['candidate_name'] = '候选人甲'
        s['job_title'] = '项目A岗位'
        with self.store.db() as db:
            payload = json.loads(db.execute(
                'SELECT payload FROM requests WHERE id=?', (r['request_id'],)).fetchone()[0])
            payload['snapshot']['candidate_name'] = '候选人甲'
            payload['snapshot']['job_title'] = '项目A岗位'
            db.execute('UPDATE requests SET payload=? WHERE id=?',
                       (json.dumps(payload, ensure_ascii=False), r['request_id']))
            db.execute('UPDATE conversations SET snapshot=? WHERE id=?',
                       (json.dumps(payload['snapshot'], ensure_ascii=False), 'c1'))
        newer = copy.deepcopy(s)
        newer['conversation_id'] = 'c2'
        self.store.adopt_conversation(r['request_id'], newer)
        request = next(x for x in self.store.ready() if x['id'] == r['request_id'])
        self.assertEqual(request['conversation'], 'c2')
        sender = Sender()
        self.assertEqual(self.store.dispatch(r['request_id'], newer, sender, True)['state'], 'sent')
        self.assertEqual(sender.calls, [('c2', '地点需要核实')])

    def test_period_separated_reply_is_sent_as_separate_messages(self):
        s, r = self.event()
        self.store.respond(r['request_id'], r['version'], 'reply',
                           '第一句。第二句。')
        ready = self.store.ready()[0]
        answer = json.loads(ready['answer'])
        self.assertEqual(answer['segments'], ['第一句', '第二句'])
        self.assertEqual(answer['text'], '第一句 第二句')
        sender = SegmentedSender()
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, True)['state'], 'sent')
        self.assertEqual(sender.calls, [('c1', ['第一句', '第二句'])])

    def test_wrong_conversation_or_job_cannot_send(self):
        s, r = self.ready()
        s['conversation_id'] = 'c-other'
        sender = Sender()
        self.assertEqual(self.store.dispatch(r['request_id'], s, sender, True)['state'], 'stale')
        self.assertEqual(sender.calls, [])

    def test_unknown_role_or_duplicate_message_identity_is_rejected(self):
        for messages in ([{'id': 'm', 'role': 'unknown', 'text': 'hello'}],
                         [{'id': 'm', 'role': 'candidate', 'text': 'hello'}] * 2):
            s = snapshot()
            s['messages'] = messages
            with self.assertRaises(ValueError):
                self.store.ingest(s, ['项目A'])

    def test_round_limit_persists_as_manual(self):
        s, r = self.event()
        with self.store.db() as db:
            db.execute("UPDATE conversations SET rounds=3 WHERE id='c1'")
        s['messages'].append({'id': 'next', 'role': 'candidate', 'text': '还在吗', 'kind': 'text'})
        self.assertEqual(self.store.ingest(s, ['项目A'])['reason'], 'automatic_round_limit')

    def test_first_real_send_can_start_after_draft_calibration(self):
        cfg = reply.defaults()
        cfg.update({
            'mode': 'cloud',
            'allowed_jobs': ['项目A'],
            'job_aliases': {'BOSS岗位': '项目A'},
            'binding': {'version': 2},
        })
        cfg['profile'].update({
            'verified': True,
            'stable_ids_verified': True,
            'draft_verified': True,
            'send_verified': False,
            'first_send_verification_required': False,
            'list_class': 'list',
            'item_class': 'item',
            'message_root_class': 'messages',
            'message_class': 'message',
            'message_text_class': 'text',
            'job_class': 'job',
            'name_class': 'name',
            'editor_class': 'editor',
            'send_class': 'submit',
        })
        self.assertFalse(reply.doctor(cfg)['ready_to_send'])
        cfg['profile']['first_send_verification_required'] = True
        self.assertTrue(reply.doctor(cfg)['ready_to_send'])

    def test_only_known_native_page_errors_are_transient(self):
        self.assertTrue(reply.transient_native_error(RuntimeError('NULL COM pointer access')))
        self.assertTrue(reply.transient_native_error(RuntimeError('bound_chat_or_boss_not_visible')))
        self.assertTrue(reply.transient_native_error(RuntimeError('inbox_scroll_depth_not_verified')))
        self.assertFalse(reply.transient_native_error(RuntimeError('send_result_unknown')))

    def test_account_configs_are_saved_and_restored_independently(self):
        original_state = reply.STATE
        with tempfile.TemporaryDirectory() as temp:
            try:
                reply.STATE = Path(temp)
                cfg = reply.defaults()
                cfg['account'] = '账号A'
                cfg['allowed_jobs'] = ['项目A']
                cfg['job_aliases'] = {'岗位A': '项目A'}
                cfg['daily_mapping_file'] = 'a.json'
                reply.save(reply.STATE / 'config.json', cfg)

                cfg = reply.switch_account(reply.config(), '账号B')
                self.assertEqual(cfg['allowed_jobs'], [])
                self.assertEqual(cfg['job_aliases'], {})
                cfg['allowed_jobs'] = ['项目B']
                cfg['job_aliases'] = {'岗位B': '项目B'}
                cfg['daily_mapping_file'] = 'b.json'
                reply.save(reply.STATE / 'config.json', cfg)

                restored = reply.switch_account(reply.config(), '账号A')
                self.assertEqual(restored['allowed_jobs'], ['项目A'])
                self.assertEqual(restored['job_aliases'], {'岗位A': '项目A'})
                self.assertEqual(restored['daily_mapping_file'], 'a.json')
                saved = reply.load(reply.STATE / 'config.json')
                self.assertEqual(set(saved['accounts']), {'账号A', '账号B'})
            finally:
                reply.STATE = original_state


if __name__ == '__main__':
    unittest.main()
