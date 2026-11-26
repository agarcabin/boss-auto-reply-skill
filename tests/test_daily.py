import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from daily import normalize_inventory, validate_mapping, resolve_job, current_mapping, today, fingerprint
from engine import Store
from preferences import Preferences


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_root = self.root / '岗位配置'
        self.cloud = {'云端企业A': ['装配工', '普工']}
        self.inventory = normalize_inventory({'source': 'native_uia', 'account': '账号A', 'complete': True,
            'jobs': [{'title': 'BOSS普工', 'status': 'active', 'status_evidence': '职位管理：开放中'},
                     {'title': 'BOSS销售', 'status': 'inactive', 'status_evidence': '待开放'}]})
        self.choices = [{'boss_key': self.inventory['jobs'][0]['key'], 'enabled': True,
                         'cloud_project': '云端企业A', 'cloud_position': '装配工'}]

    def tearDown(self):
        self.temp.cleanup()

    def test_only_active_job_is_mapped(self):
        self.assertEqual([j['title'] for j in self.inventory['jobs']], ['BOSS普工'])
        mapped = validate_mapping(self.inventory, self.cloud, self.choices, self.config_root)
        self.assertEqual(resolve_job(mapped, 'BOSS普工')['cloud_position'], '装配工')

    def test_duplicate_titles_need_platform_ids(self):
        with self.assertRaises(ValueError):
            normalize_inventory({'source': 'native_uia', 'account': 'a', 'complete': True,
              'jobs': [{'title': '普工', 'status': 'active'}, {'title': '普工', 'status': 'active'}]})

    def test_unknown_status_or_wrong_company_or_role_cannot_start(self):
        altered = copy.deepcopy(self.inventory)
        altered['jobs'][0]['status'] = 'unknown'
        with self.assertRaises(ValueError):
            validate_mapping(altered, self.cloud, self.choices, self.config_root)
        with self.assertRaises(ValueError):
            validate_mapping(self.inventory, {'云端企业A': ['不同岗位']}, self.choices, self.config_root)
        changed = copy.deepcopy(self.choices)
        changed[0]['cloud_project'] = '另一家企业'
        with self.assertRaises(ValueError):
            validate_mapping(self.inventory, self.cloud, changed, self.config_root)

    def test_same_day_reuse_checks_config_version_and_account(self):
        mapped = validate_mapping(self.inventory, self.cloud, self.choices, self.config_root)
        folder = self.root / 'daily'
        folder.mkdir()
        path = folder / (today() + '-' + fingerprint('账号A')[:12] + '.json')
        path.write_text(json.dumps(mapped), encoding='utf-8')
        self.assertIsNotNone(current_mapping(self.root, '账号A', self.inventory))
        self.assertIsNone(current_mapping(self.root, '账号B', self.inventory))
        changed_inventory = copy.deepcopy(self.inventory)
        changed_inventory['inventory_hash'] = 'changed'
        self.assertIsNone(current_mapping(self.root, '账号A', changed_inventory))

    def test_previous_cloud_choice_is_restored_and_folder_created(self):
        preferences = Preferences(self.config_root)
        boss = self.inventory['jobs'][0]
        preferences.update('账号A', boss, '云端企业A', '装配工', enabled=True, confirmed=True)
        restored = Preferences(self.config_root).get('账号A', boss)
        self.assertEqual(restored['cloud_company'], '云端企业A')
        self.assertEqual(restored['cloud_position'], '装配工')
        self.assertTrue(restored['enabled'])

    def test_existing_unread_is_queued_once_at_startup(self):
        store = Store(self.root / 'reply.db')
        s = {'conversation_id': 'c', 'job_id': 'j', 'messages': [{'id': '1', 'role': 'candidate', 'text': '你好'}],
             'identity_verified': True, 'complete': True, 'draft': ''}
        store.ingest(s, ['j'])
        store.queue_unread(s, ['j'])
        store.queue_unread(s, ['j'])
        self.assertEqual(len(store.pending()), 1)


if __name__ == '__main__':
    unittest.main()
