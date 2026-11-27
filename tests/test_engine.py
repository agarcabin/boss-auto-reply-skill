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


class ExchangeSender:
    def __init__(self):
        self.calls = []
        self.exchanges = []

    def send(self, s, text):
        self.calls.append(text)
        return {'state': 'sent', 'conversation_id': s['conversation_id'],
                'job_id': s['job_id'], 'text': text,
                'message_id': f'ex-{len(self.calls)}'}

    def click_exchange(self, kind):
        self.exchanges.append(kind)
        return {'state': 'exchange_sent', 'label': kind}


class MultiSender:
    def __init__(self):
        self.groups = []
        self.ids = 0

    def send_many(self, s, groups):
        self.groups.append([list(group) for group in groups])
        ids = []
        for group in groups:
            for _ in group:
                self.ids += 1
                ids.append(f'multi-{self.ids}')
        texts = [text for group in groups for text in group]
        return {'state': 'sent', 'conversation_id': s['conversation_id'],
                'job_id': s['job_id'], 'message_ids': ids, 'texts': texts,
                'message_id': ids[0], 'text': ' '.join(texts)}


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

    def test_disabled_job_cancels_queued_requests(self):
        _, request = self.event()
        removed = self.store.prune_jobs(['项目B'])
        self.assertEqual(removed, [request['request_id']])
        self.assertEqual(self.store.pending(), [])

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

    def test_manual_handoff_records_question_and_reason(self):
        s, r = self.event()
        self.assertEqual(
            self.store.respond(
                r['request_id'], r['version'], 'manual', '知识库无法确认该问题')['state'],
            'manual',
        )
        handoff = self.store.latest_handoff('c1')
        self.assertTrue(handoff['needs_human'])
        self.assertIn('工作地点呢', handoff['question'])
        self.assertEqual(handoff['reason'], '知识库无法确认该问题')

    def test_human_draft_latches_until_explicit_release(self):
        s, r = self.ready()
        s['draft'] = '我来回复'
        self.assertEqual(self.store.ingest(s, ['项目A'])['state'], 'manual')
        s['draft'] = ''
        self.assertEqual(self.store.ingest(s, ['项目A'])['state'], 'manual')
        self.store.manual('c1', release=True)
        self.assertEqual(self.store.status()['manual_conversations'], 0)

    def test_image_latest_message_latches_only_that_conversation(self):
        self.store.ingest(snapshot(), ['项目A'])
        s = snapshot(extra=[
            {'id': 'img', 'role': 'candidate', 'text': '[图片/附件]', 'kind': 'text'},
        ])
        s['non_text_last'] = True
        result = self.store.ingest(s, ['项目A'])
        self.assertEqual(result['state'], 'manual')
        self.assertEqual(result['reason'], 'non_text_message_needs_human')
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.status()['manual_conversations'], 1)

    def test_next_queued_name_skips_current_conversation(self):
        first = snapshot('c1')
        first['candidate_name'] = '甲'
        second = snapshot('c2')
        second['candidate_name'] = '乙'
        self.store.ingest(first, ['项目A'], new_is_event=True)
        self.store.ingest(second, ['项目A'], new_is_event=True)
        self.assertEqual(self.store.next_queued_name('c1'), '乙')
        self.assertEqual(self.store.next_queued_name('c2'), '甲')

    def test_overlay_label_uses_short_job_name(self):
        self.assertEqual(
            reply.overlay_label(
                {'candidate_name': '李萌', 'job_id': '14 博格华纳'}),
            '李萌（博格华纳）')
        self.assertEqual(
            reply.overlay_label({'candidate_name': '李萌', 'job_id': ''}),
            '李萌')

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
        self.assertEqual(self.store.control('resume')['control'], 'running')
        self.store.resolve(r['request_id'], 'not_sent')
        self.store.control('resume')
        self.assertEqual(self.store.ready(), [])

    def test_resume_reconciles_unknown_send_against_page(self):
        s, r = self.ready()
        self.store.dispatch(r['request_id'], s, Sender(True), True)
        self.store.control('resume')
        result = self.store.reconcile_unknown(lambda cid, previous: s, ['项目A'])
        self.assertEqual(result[0]['state'], 'requeued')
        self.assertEqual(self.store.pending()[0]['request_id'], r['request_id'])
        self.assertEqual(self.store.status()['counts'].get('unknown'), None)
        self.store.respond(r['request_id'], r['version'], 'reply', '地点在宁波。')
        self.assertEqual(len(self.store.ready()), 1)

    def test_reconcile_unknown_recognizes_platform_receipt(self):
        s, r = self.ready()
        self.store.dispatch(r['request_id'], s, Sender(True), True)
        sent = copy.deepcopy(s)
        sent['messages'].append(
            {'id': 'platform-sent', 'role': 'recruiter', 'text': '地点需要核实', 'kind': 'text'})
        result = self.store.reconcile_unknown(lambda cid, previous: sent, ['项目A'])
        self.assertEqual(result[0]['state'], 'sent')
        self.assertEqual(result[0]['message_ids'], ['platform-sent'])
        self.assertEqual(self.store.pending(), [])

    def test_reconcile_unknown_without_page_access_hands_to_human(self):
        s, r = self.ready()
        self.store.dispatch(r['request_id'], s, Sender(True), True)
        self.store.control('resume')

        def unavailable(cid, previous):
            raise ValueError('bound_chat_or_boss_not_visible')

        result = self.store.reconcile_unknown(unavailable, ['项目A'])
        self.assertEqual(result[0]['state'], 'needs_human')
        self.assertEqual(self.store.status()['control'], 'running')
        self.assertEqual(self.store.status()['manual_conversations'], 1)
        self.assertTrue(self.store.latest_handoff('c1')['needs_human'])

    def test_two_quick_candidate_messages_keep_a_live_queue_slot(self):
        s, r = self.ready()
        newest = copy.deepcopy(s)
        newest['messages'].append(
            {'id': 'm4', 'role': 'candidate', 'text': '几点上班', 'kind': 'text'})
        self.assertEqual(self.store.ingest(newest, ['项目A'])['state'], 'pending')
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['snapshot']['messages'][-1]['id'], 'm4')
        # The second message arrived before generation: the same trailing
        # question must not stay stale on a later scan.
        with self.store.db() as db:
            db.execute("UPDATE requests SET state='stale' WHERE state='pending'")
        self.assertEqual(
            self.store.queue_unread(newest, ['项目A'])['state'], 'pending')
        self.assertEqual(len(self.store.pending()), 1)

    def test_reply_current_regenerates_when_candidate_spoke_last(self):
        s, r = self.ready()
        self.assertEqual(self.store.requeue_current(s, ['项目A'])['state'], 'pending')
        self.assertEqual([x['request_id'] for x in self.store.pending()], [r['request_id']])

    def test_forced_reply_works_even_when_our_message_is_last(self):
        s, r = self.ready()
        ours = copy.deepcopy(s)
        ours['messages'].append(
            {'id': 'ours', 'role': 'recruiter', 'text': '刚回复过', 'kind': 'text'})
        self.assertEqual(
            self.store.requeue_current(ours, ['项目A'])['state'], 'no_reply_needed')
        result = self.store.requeue_current(
            ours, ['项目A'], force_reply=True)
        self.assertEqual(result['state'], 'pending')
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].get('force_reply'))
        second = self.store.requeue_current(ours, ['项目A'], force_reply=True)
        self.assertNotEqual(result['request_id'], second['request_id'])
        self.assertEqual(len(self.store.pending()), 1)

    def test_reply_current_does_not_override_human_draft(self):
        s, r = self.ready()
        s['draft'] = '我自己回'
        self.assertEqual(self.store.requeue_current(s, ['项目A'])['state'], 'manual')

    def test_own_reply_echo_is_not_a_human_draft(self):
        s, r = self.event()
        self.store.respond(
            r['request_id'], r['version'], 'reply',
            '长白班和倒班都有的，想赚钱的可以选择倒班。')
        self.assertTrue(self.store.is_own_draft('长白班和倒班都有的'))
        self.assertFalse(self.store.is_own_draft('你好在吗'))
        self.assertFalse(self.store.is_own_draft(''))

    def test_contact_request_uses_wechat_then_phone_once(self):
        self.assertEqual(self.store.exchange_kind('c1', '你好'), '')
        self.assertEqual(
            self.store.exchange_kind('c1', '方便的话留个联系方式'), 'wechat')
        self.store.record_exchange('c1', 'wechat')
        self.assertEqual(self.store.exchange_kind('c1', '加个微信吧'), 'phone')
        self.store.record_exchange('c1', 'phone')
        self.assertEqual(self.store.exchange_kind('c1', '加个微信吧'), '')

    def test_dispatch_clicks_exchange_after_contact_reply(self):
        s, r = self.event()
        self.store.respond(
            r['request_id'], r['version'], 'reply',
            '方便的话留个联系方式，我把岗位说明发您。')
        sender = ExchangeSender()
        result = self.store.dispatch(r['request_id'], s, sender, True)
        self.assertEqual(result['state'], 'sent')
        self.assertEqual(sender.exchanges, ['wechat'])
        self.assertEqual(self.store.exchange_counts('c1'), {'wechat': 1})
        newer = copy.deepcopy(s)
        newer['messages'].append(
            {'id': 'ex-1', 'role': 'recruiter',
             'text': '方便的话留个联系方式，我把岗位说明发您', 'kind': 'text'})
        newer['messages'].append(
            {'id': 'm9', 'role': 'candidate', 'text': '怎么联系', 'kind': 'text'})
        self.store.ingest(newer, ['项目A'])
        request = self.store.pending()[0]
        self.store.respond(
            request['request_id'], request['version'], 'reply', '您加我微信吧。')
        self.assertEqual(
            self.store.dispatch(request['request_id'], newer, sender, True)['state'],
            'sent')
        self.assertEqual(sender.exchanges, ['wechat', 'phone'])

    def test_multi_item_answer_sends_each_group_in_order(self):
        s, r = self.event()
        items = [
            {'text': '你好', 'segments': ['你好']},
            {'text': '岗位在招', 'segments': ['岗位在招']},
        ]
        self.store.respond(
            r['request_id'], r['version'], 'reply', '你好 岗位在招',
            segments=['你好', '岗位在招'], items=items)
        sender = MultiSender()
        result = self.store.dispatch(r['request_id'], s, sender, True)
        self.assertEqual(result['state'], 'sent')
        self.assertEqual(sender.groups, [[['你好'], ['岗位在招']]])
        self.assertEqual(self.store.status()['counts']['sent'], 1)

    def test_reply_current_releases_human_outbound_but_not_takeover(self):
        s, r = self.ready()
        with self.store.db() as db:
            db.execute(
                "UPDATE conversations SET manual=1,reason='human_outbound_detected' "
                "WHERE id='c1'")
        self.assertEqual(self.store.requeue_current(s, ['项目A'])['state'], 'manual')
        self.assertEqual(
            self.store.requeue_current(
                s, ['项目A'], release_latches=True)['state'], 'pending')
        with self.store.db() as db:
            db.execute(
                "UPDATE conversations SET manual=1,reason='user_takeover' WHERE id='c1'")
        self.assertEqual(
            self.store.requeue_current(
                s, ['项目A'], release_latches=True)['state'], 'manual')
        self.assertEqual(
            self.store.requeue_current(
                s, ['项目A'], force_reply=True)['state'], 'pending')

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

    def test_job_checkbox_state_becomes_reply_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '岗位对应关系.json'
            path.write_text(json.dumps({
                'version': 1,
                'accounts': {
                    '账号A': {
                        'records': [
                            {
                                'boss_title': '岗位A',
                                'cloud_company': '公司A',
                                'cloud_position': '职位A',
                                'enabled': True,
                            },
                            {
                                'boss_title': '岗位B',
                                'cloud_company': '公司B',
                                'cloud_position': '职位B',
                                'enabled': False,
                            },
                        ]
                    }
                },
            }, ensure_ascii=False), encoding='utf-8')
            cfg = reply.defaults()
            cfg['account'] = '账号A'
            cfg['cloud_mapping_preferences'] = str(path)
            self.assertTrue(reply.sync_job_selection(cfg))
            self.assertEqual(cfg['allowed_jobs'], ['公司A'])
            self.assertEqual(cfg['job_aliases'], {'岗位A': '公司A'})
            self.assertEqual(cfg['job_enabled'], {'岗位A': True, '岗位B': False})
            self.assertEqual(cfg['enabled_jobs'], ['岗位A'])
            self.assertEqual(cfg['disabled_jobs'], ['岗位B'])


if __name__ == '__main__':
    unittest.main()
