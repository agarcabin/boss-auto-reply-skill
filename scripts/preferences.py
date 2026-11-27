"""Persistent cloud company/position choices for the fixed desktop config folder."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path.home() / 'Desktop/自动招呼/04 回复配置'
LEGACY_ROOT = Path.home() / 'Desktop/自动招呼/03 岗位配置'
FILE_NAME = '岗位对应关系.json'


class Preferences:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_ROOT).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / FILE_NAME
        legacy = LEGACY_ROOT / FILE_NAME
        if (self.root == DEFAULT_ROOT and not self.path.exists()
                and legacy.exists() and legacy != self.path):
            self.path.write_text(legacy.read_text(encoding='utf-8-sig'), encoding='utf-8')
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if data == {}:
                data = {'version': 1, 'accounts': {}}
                self._save(data)
            elif not isinstance(data, dict) or data.get('version') != 1:
                raise ValueError('岗位对应关系文件格式不兼容')
        else:
            data = {'version': 1, 'accounts': {}}
            self._save(data)
        self.data = data

    def _save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)

    def _records(self, account):
        return self.data.setdefault('accounts', {}).setdefault(account, {'records': []})

    def get(self, account, boss):
        records = self._records(account).setdefault('records', [])
        exact = [record for record in records if record.get('boss_key') == boss['key']]
        if len(exact) == 1:
            return dict(exact[0])
        same_title = [record for record in records if record.get('boss_title') == boss['title']]
        return dict(same_title[0]) if len(same_title) == 1 else {}

    def update(self, account, boss, cloud_company='', cloud_position='', enabled=False, confirmed=False):
        records = self._records(account).setdefault('records', [])
        records[:] = [record for record in records if record.get('boss_key') != boss['key']]
        records.append({
            'boss_key': boss['key'],
            'boss_title': boss['title'],
            'cloud_company': cloud_company,
            'cloud_position': cloud_position,
            'enabled': bool(enabled),
            'confirmed': bool(confirmed),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        self._save(self.data)
