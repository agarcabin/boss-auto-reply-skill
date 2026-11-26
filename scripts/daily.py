"""Daily job-to-knowledge mapping; the UI cannot bypass source or identity checks."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHINA = timezone(timedelta(hours=8))


def today():
    return datetime.now(CHINA).date().isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalize_inventory(payload):
    if payload.get('source') not in ('native_uia', 'native_msaa') or not payload.get('complete'):
        raise ValueError('必须完成原生 UIA/MSAA 岗位列表采集')
    if not payload.get('account') or not isinstance(payload.get('jobs'), list):
        raise ValueError('岗位采集缺少当前账号或岗位列表')
    titles = Counter(str(j.get('title', '')).strip() for j in payload['jobs'])
    normalized = []
    seen = set()
    for item in payload['jobs']:
        title = str(item.get('title', '')).strip()
        if not title or item.get('status') not in ('active', 'inactive', 'unknown'):
            raise ValueError('岗位名称或上架状态无效')
        if item['status'] == 'inactive':
            continue
        platform_id = str(item.get('platform_id') or '').strip()
        if platform_id:
            identity = 'platform:' + platform_id
        elif titles[title] == 1:
            # A unique title may only be resolved within this account's daily confirmed inventory.
            identity = 'daily-title:' + fingerprint([payload['account'], title])[:24]
        else:
            raise ValueError('同名岗位缺少平台职位标识，必须先区分，不能自动合并')
        if identity in seen:
            raise ValueError('岗位标识重复')
        seen.add(identity)
        normalized.append({'key': identity, 'platform_id': platform_id, 'title': title,
                           'status': item['status'], 'status_evidence': str(item.get('status_evidence') or ''),
                           'visibility_warning': str(item.get('visibility_warning') or '')})
    if not normalized:
        raise ValueError('未采集到可映射岗位；不会启动回复')
    return {'date': today(), 'account': payload['account'], 'source': payload['source'],
            'complete': True, 'jobs': normalized, 'inventory_hash': fingerprint(normalized)}


def validate_mapping(inventory, cloud_catalog, choices, config_root=None):
    if inventory['date'] != today():
        raise ValueError('岗位盘点已跨日，需要重新读取')
    choices = {c['boss_key']: c for c in choices}
    records = []
    for boss in inventory['jobs']:
        c = choices.get(boss['key'])
        if not c:
            raise ValueError('每个岗位都需选择对应关系或明确暂不处理')
        if c.get('enabled') is False:
            records.append({**boss, 'enabled': False})
            continue
        if boss['status'] != 'active' or not boss.get('status_evidence'):
            raise ValueError('上架状态未核实的岗位不能启用')
        project = c.get('cloud_project')
        if project == '总库' or project not in cloud_catalog:
            raise ValueError('必须选择云端单一项目，不能使用总库自动回复')
        position = c.get('cloud_position', '')
        if position not in cloud_catalog[project]:
            raise ValueError('必须选择该云端企业下的具体岗位')
        records.append({**boss, 'enabled': True, 'cloud_project': project,
                        'cloud_position': position})
    if not any(r['enabled'] for r in records):
        raise ValueError('至少需要启用一个已映射岗位')
    return {'date': today(), 'account': inventory['account'], 'inventory_hash': inventory['inventory_hash'],
            'config_root': str(config_root) if config_root else '', 'mappings': records, 'confirmed': True}


def current_mapping(state, account, inventory=None):
    path = Path(state) / 'daily' / (today() + '-' + fingerprint(account)[:12] + '.json')
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if not data.get('confirmed') or data.get('account') != account or data.get('date') != today():
        return None
    if inventory and inventory['inventory_hash'] != data['inventory_hash']:
        return None
    return data


def resolve_job(mapping, title, platform_id=''):
    rows = [r for r in mapping['mappings'] if r.get('enabled') and
            ((platform_id and r.get('platform_id') == platform_id) or
             (not platform_id and not r.get('platform_id') and r['title'] == title))]
    if len(rows) != 1:
        raise ValueError('当前会话岗位没有唯一的每日对应关系')
    return rows[0]
