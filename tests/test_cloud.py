import sys
import unittest
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cloud import Cloud


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.client = Cloud({'cloud_username': '小八', 'allowed_jobs': ['项目A']})
        self.request = {'snapshot': {'job_id': '项目A', 'job_title': '岗位1', 'messages': [
            {'id': '1', 'role': 'recruiter', 'text': '你好'},
            {'id': '2', 'role': 'candidate', 'text': '工资呢'}]}}

    def result(self):
        return {'context_scope': 'request', 'conversation_saved': False, 'needs_verification': False,
                'styles': [{'style': '自然亲切', 'replies': ['这个岗位的工资需要面议。'], 'evidence_ids': [1]}],
                'sources': [{'id': 'e', 'project': '项目A', 'visibility': 'public', 'text': '工资面议'}]}

    def test_legacy_cloud_is_not_called_as_if_stateless(self):
        with patch.object(self.client, 'capabilities', return_value={}), patch.object(self.client, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'upgrade_required'):
                self.client.generate(self.request)
            request.assert_not_called()

    def test_missing_isolation_receipt_is_rejected(self):
        result = self.result()
        result.pop('context_scope')
        with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1}), patch.object(self.client, 'request', return_value=result):
            with self.assertRaisesRegex(ValueError, 'isolation'):
                self.client.generate(self.request)

    def test_current_conversation_only_and_one_style(self):
        with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1}), patch.object(self.client, 'request', return_value=self.result()) as post:
            output = self.client.generate(self.request)
            self.assertEqual(output['action'], 'reply')
            payload = post.call_args.args[1]
            self.assertIs(payload['stateless'], True)
            self.assertEqual(payload['question'], '工资呢')
            self.assertIn('岗位1', payload['conversation'])
            self.assertNotIn('conversation_id', payload)

    def test_uncertainty_uses_safe_confirmation_reply(self):
        result = self.result()
        result['needs_verification'] = True
        with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1}), patch.object(self.client, 'request', return_value=result):
            output = self.client.generate(self.request)
            self.assertEqual(output['action'], 'reply')
            self.assertIn('人事确认', output['text'])
            self.assertIn('联系方式', output['text'])

    def test_confirmation_does_not_reask_after_contact_was_mentioned(self):
        self.request['snapshot']['messages'].insert(
            1, {'id': '3', 'role': 'recruiter', 'text': '方便加个微信吗'})
        result = self.result()
        result['needs_verification'] = True
        with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1}), patch.object(self.client, 'request', return_value=result):
            output = self.client.generate(self.request)
            self.assertEqual(output['action'], 'reply')
            self.assertNotIn('留个联系方式', output['text'])

    def test_internal_or_other_project_evidence_rejected(self):
        for field, value in [('visibility', 'internal'), ('project', '项目B')]:
            result = self.result()
            result['sources'][0][field] = value
            with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1}), patch.object(self.client, 'request', return_value=result):
                with self.assertRaisesRegex(ValueError, 'scope_mismatch'):
                    self.client.generate(self.request)

    def test_search_filters_nonpublic_records(self):
        with patch.object(self.client, 'login'), patch.object(self.client, 'request', return_value={'results': [
            {'id': 1, 'project': '项目A', 'visibility': 'public'},
            {'id': 2, 'project': '项目B', 'visibility': 'public'},
            {'id': 3, 'project': '项目A', 'visibility': 'internal'}]}):
            self.assertEqual([s['id'] for s in self.client.search('项目A', '工资')], [1])

    def test_total_library_is_not_used_for_candidate_reply(self):
        self.request['snapshot']['job_id'] = '总库'
        with self.assertRaisesRegex(ValueError, 'not_explicitly_allowed'):
            self.client.generate(self.request)

    def test_transient_gateway_error_is_retried(self):
        error = HTTPError('https://new.jiebo.vip/server/api/test', 502,
                          'Bad Gateway', None, None)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true}'
        opener = Mock()
        opener.open.side_effect = [error, response]
        self.client.opener = opener
        with patch('cloud.time.sleep') as sleep:
            self.assertEqual(self.client.request('api/test'), {'ok': True})
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once()

    def test_v1_requires_exact_company_and_position(self):
        self.request['snapshot']['knowledge_position'] = '岗位1'
        result = self.result()
        result.update(ok=True, mode='hr', scope={'company': '项目A', 'position': '岗位1'})
        result['sources'] = [{'company': '项目A', 'positions': ['岗位1'], 'text': '工资面议'}]
        with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1, 'external_api': True}), patch.object(self.client, 'request', return_value=result) as post:
            self.assertEqual(self.client.generate(self.request)['action'], 'reply')
            self.assertEqual(post.call_args.args[0], 'api/v1/reply')
            self.assertTrue(post.call_args.kwargs['api'])
            self.assertEqual(post.call_args.args[1]['position'], '岗位1')
        result['scope']['position'] = '另一个岗位'
        with patch.object(self.client, 'capabilities', return_value={'stateless_hr_reply': 1, 'external_api': True}), patch.object(self.client, 'request', return_value=result):
            with self.assertRaisesRegex(ValueError, 'target_mismatch'):
                self.client.generate(self.request)


if __name__ == '__main__':
    unittest.main()
