"""Name-authenticated cloud knowledge client. Cookies stay in process memory."""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from credentials import load_key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('cloud_redirect_rejected')


class Cloud:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.get('cloud_url', 'https://new.jiebo.vip/server/').rstrip('/') + '/'
        if self.base != 'https://new.jiebo.vip/server/':
            raise ValueError('cloud_url_must_match_authorized_service')
        self.username = cfg.get('cloud_username', '')
        if not self.username:
            raise ValueError('cloud_username_required')
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_at = 0

    def request(self, route, payload=None, api=False, attempts=4):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if api:
            token = load_key(self.cfg)
            if not token:
                raise ValueError('cloud_api_key_required')
            headers['X-API-Key'] = token
        req = urllib.request.Request(self.base + route, data=data, headers=headers)
        for attempt in range(max(1, int(attempts))):
            try:
                with self.opener.open(req, timeout=45) as response:
                    body = response.read(2_000_001)
                    if len(body) > 2_000_000:
                        raise ValueError('cloud_response_too_large')
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError('cloud_response_invalid')
                    return result
            except urllib.error.HTTPError as exc:
                if exc.code in (502, 503, 504) and attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ValueError('cloud_http_' + str(exc.code)) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ValueError('cloud_unavailable') from None

    def login(self):
        if time.monotonic() - self.logged_at < 21600 and self.logged_at:
            return
        data = self.request('api/login', {'username': self.username})
        if not data.get('logged_in') or data.get('username') != self.username:
            raise ValueError('cloud_login_not_verified')
        self.logged_at = time.monotonic()

    def status(self):
        self.login()
        return self.request('api/status')

    def capabilities(self):
        data = self.request('api/v1')
        if 'POST /api/v1/reply' not in data.get('endpoints', {}):
            raise ValueError('cloud_independent_reply_api_missing')
        return {'stateless_hr_reply': 1, 'external_api': True, 'version': data.get('version')}

    def catalog(self):
        return self.request('api/v1/catalog', api=True)

    def health(self):
        return self.request('api/v1/health', api=True)

    def identities(self):
        return self.request('api/v1/identities', api=True)

    def search(self, project, question):
        self._scope(project)
        self.login()
        data = self.request('api/search', {'project': project, 'question': question[:2000], 'limit': 8})
        # Existing search endpoint also returns internal records. Never feed them into candidate replies.
        return [s for s in data.get('results', [])
                if s.get('visibility') == 'public' and s.get('project') == project]

    def _scope(self, project):
        if project == '总库' or project not in self.cfg.get('allowed_jobs', []):
            raise ValueError('cloud_project_not_explicitly_allowed')

    def generate(self, request):
        snapshot = request['snapshot']
        if not request.get('_single'):
            burst = _burst_indices(snapshot)
            substantive = [
                index for index in burst
                if not _smalltalk(snapshot['messages'][index].get('text', ''))
            ]
            if len(substantive) >= 2:
                results = []
                for index in substantive[-3:]:
                    sub = dict(request, _single=True)
                    sub['_focus_text'] = snapshot['messages'][index].get('text', '')
                    sub['snapshot'] = dict(
                        snapshot, messages=snapshot['messages'][:index + 1])
                    try:
                        results.append(self.generate(sub))
                    except Exception:
                        results.append({
                            'action': 'manual', 'text': '', 'sources': [],
                        })
                budget = 4
                items = []
                for item in results:
                    segments = [s for s in
                                (item.get('segments') or [item.get('text', '')])
                                if s]
                    segments = segments[:2][:budget]
                    budget -= len(segments)
                    if segments:
                        items.append(dict(item, segments=segments,
                                          text=' '.join(segments)))
                if items:
                    total = [s for item in items for s in item['segments']]
                    handoff = next(
                        (item.get('handoff') for item in items
                         if item.get('handoff')), None)
                    if not handoff and any(
                            item.get('action') == 'manual' for item in results):
                        handoff = {
                            'reason': '部分候选人消息无法自动回答，需要人工补充',
                            'source': 'knowledge_gap',
                        }
                    output = {
                        'action': 'reply',
                        'text': ' '.join(total),
                        'segments': total,
                        'sources': [s for item in items
                                    for s in (item.get('sources') or [])],
                        'items': items,
                    }
                    if handoff:
                        output['handoff'] = handoff
                    return output
        project = snapshot['job_id']
        self._scope(project)
        caps = self.capabilities()
        if caps.get('stateless_hr_reply') != 1:
            raise ValueError('cloud_stateless_reply_upgrade_required')
        dialogue = [m for m in snapshot['messages'] if m['role'] in ('candidate', 'recruiter')]
        if not dialogue:
            raise ValueError('no_readable_dialogue')
        forced = bool(request.get('force_reply'))
        if dialogue[-1]['role'] != 'candidate' and not forced:
            raise ValueError('latest_message_is_not_candidate')
        if request.get('_focus_text') and dialogue[-1]['role'] == 'candidate':
            boundary = len(dialogue) - 1
            question = str(request['_focus_text']).strip()
        elif dialogue[-1]['role'] == 'candidate':
            # Latest consecutive candidate messages form one question; the rest is isolated context.
            boundary = len(dialogue) - 1
            while boundary > 0 and dialogue[boundary - 1]['role'] == 'candidate':
                boundary -= 1
            question = '\n'.join(m['text'] for m in dialogue[boundary:])
        else:
            # Explicit "reply now" after our own last message: anchor retrieval
            # on the most recent candidate turn and ask for a fresh follow-up.
            boundary = len(dialogue)
            index = len(dialogue) - 1
            while index >= 0 and dialogue[index]['role'] != 'candidate':
                index -= 1
            if index >= 0:
                start = index
                while start > 0 and dialogue[start - 1]['role'] == 'candidate':
                    start -= 1
                question = '\n'.join(m['text'] for m in dialogue[start:index + 1])
            else:
                question = '请继续跟进这位候选人'
        if not question.strip():
            question = '请继续跟进这位候选人'
        if len(question) > 2000:
            raise ValueError('candidate_question_too_long')
        context = '\n'.join(('候选人：' if m['role'] == 'candidate' else '招聘方：') + m['text']
                            for m in dialogue[max(0, boundary - 20):boundary])[-5500:]
        context = ('本次招聘岗位：' + snapshot.get('job_title', '') + '\n' + context)[-6000:]
        if forced and dialogue[-1]['role'] != 'candidate':
            context += ('\n本次为招聘方主动补发：上一条是我方消息，'
                        '请基于上下文生成一条新的、自然简短、不重复的跟进消息')
            context = context[-6000:]
        if caps.get('external_api'):
            position = snapshot.get('knowledge_position')
            if not position:
                raise ValueError('cloud_position_mapping_required')
            result = self.request('api/v1/reply', {'company': project, 'position': position,
                'question': question, 'conversation': context, 'identity': self.cfg.get('reply_identity', 'hr-sim'),
                'include_sources': True, 'limit': 8}, api=True)
            scope = result.get('scope', {})
            if (not result.get('ok') or result.get('mode') != 'hr' or
                    scope.get('company') != project or scope.get('position') != position):
                raise ValueError('cloud_reply_target_mismatch')
        else:
            # Compatibility only for an explicitly capability-verified isolated legacy service.
            result = self.request('api/hr-reply', {'project': project, 'question': question,
                                  'conversation': context, 'stateless': True, 'limit': 8})
        if result.get('context_scope') != 'request' or result.get('conversation_saved') is not False:
            raise ValueError('cloud_context_isolation_not_confirmed')
        if result.get('needs_verification') is not False:
            if self.cfg.get('needs_verification_reply', True) is False:
                return {
                    'action': 'manual',
                    'text': '知识库无法确认该问题，自动兜底已关闭，需要人工确认',
                    'sources': [],
                }
            contact_mentioned = any(
                re.search(r'微信|联系方式|电话|留个号|加个[vV]', m['text'])
                for m in dialogue if m['role'] == 'recruiter')
            if contact_mentioned:
                text = '这块我向人事确认一下，确认后我第一时间跟您说'
            else:
                text = '这块我向人事确认一下，方便的话留个联系方式，我确认后第一时间跟您说'
            return {
                'action': 'reply',
                'text': text,
                'segments': [text],
                'sources': [],
                'handoff': {
                    'reason': '知识库无法确认该问题，已发送确认话术，等待人工补充',
                    'source': 'knowledge_gap',
                },
            }
        styles = result.get('styles')
        if not isinstance(styles, list):
            raise ValueError('cloud_styles_missing')
        chosen = [s for s in styles if s.get('style') == self.cfg.get('reply_style', '自然亲切')]
        if len(chosen) != 1:
            raise ValueError('cloud_style_not_unique')
        chosen = chosen[0]
        replies = chosen.get('replies', [])
        if not isinstance(replies, list) or not 1 <= len(replies) <= 3 or any(
                not isinstance(x, str) or not x.strip() or len(x) > 120 for x in replies):
            raise ValueError('cloud_reply_text_invalid')
        evidence = result.get('sources', [])
        indices = chosen.get('evidence_ids', [])
        greeting = re.fullmatch(r'(你好[呀啊]?|您好|在吗|你好在吗|嗨|hello|hi)[！!？?。\s]*', question.strip(), re.I)
        if not greeting and not indices:
            if forced:
                contact_mentioned = any(
                    re.search(r'微信|联系方式|电话|留个号|加个[vV]', m['text'])
                    for m in dialogue if m['role'] == 'recruiter')
                text = ('您看还有哪方面想了解的，我再详细跟您说' if contact_mentioned
                        else '方便的话留个联系方式，我把岗位说明发您核对一下')
                return {
                    'action': 'reply',
                    'text': text,
                    'segments': [text],
                    'sources': [],
                }
            return {
                'action': 'manual',
                'text': '知识库没有找到可引用的答案，需要人工补充',
                'sources': [],
            }
        if not isinstance(indices, list) or any(type(i) is not int or not 1 <= i <= len(evidence) for i in indices):
            raise ValueError('cloud_evidence_invalid')
        sources = [evidence[i - 1] for i in indices]
        if caps.get('external_api'):
            if any(s.get('company') != project for s in sources):
                raise ValueError('cloud_evidence_scope_mismatch')
        elif any(s.get('visibility') != 'public' or s.get('project') != project for s in sources):
            raise ValueError('cloud_evidence_scope_mismatch')
        segments = _reply_segments(replies)
        text = ' '.join(segments)
        if re.search(r'\[\d+\]|知识库|检索|内部提醒', text):
            return {
                'action': 'manual',
                'text': '生成内容包含内部提示或引用标记，需要人工处理',
                'sources': sources,
            }
        return {'action': 'reply', 'text': text, 'segments': segments, 'sources': sources}


def _reply_segments(replies):
    parts = []
    for reply in replies:
        for part in str(reply).split('。'):
            part = part.strip().rstrip('。').strip()
            if part:
                parts.append(part)
    if not parts:
        raise ValueError('cloud_reply_text_invalid')
    if len(parts) > 3:
        parts = parts[:2] + [' '.join(parts[2:])]
    return parts


def _burst_indices(snapshot):
    messages = snapshot.get('messages') or []
    indices = []
    for index in range(len(messages) - 1, -1, -1):
        role = messages[index].get('role')
        if role == 'system':
            continue
        if role != 'candidate':
            break
        indices.insert(0, index)
    return indices


def _smalltalk(text):
    return bool(re.fullmatch(
        r'(在吗|在不在|你好|您好|嗨|hello|hi|好的|好|嗯|哦|谢谢|行|'
        r'可以|收到|在的|稍等|等会)[呀啊嘛呢。！!？?\s]*',
        (text or '').strip(), re.I))
