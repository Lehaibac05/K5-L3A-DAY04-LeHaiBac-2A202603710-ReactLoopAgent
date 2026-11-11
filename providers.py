"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


# Retry khi provider trả HTTP 429 (rate limit). Có thể chỉnh qua biến môi trường.
MAX_RETRIES_429 = int(os.getenv('PROVIDER_MAX_RETRIES', '6'))
MAX_WAIT_429 = float(os.getenv('PROVIDER_MAX_WAIT', '65'))     # giây, trần cho mỗi lần chờ


def _parse_duration(value):
    """'20.19s', '577ms', '1m2.5s', '7' -> số giây (float); không đọc được -> None."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)                       # Retry-After dạng số giây
    except ValueError:
        pass
    match = re.fullmatch(r'(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?', value)
    if not match or not any(match.groups()):
        return None
    h, m, s, ms = (float(x) if x else 0.0 for x in match.groups())
    return h * 3600 + m * 60 + s + ms / 1000


def _retry_delay(headers, attempt):
    """Ưu tiên Retry-After, rồi x-ratelimit-reset-*; nếu không có thì backoff mũ."""
    candidates = [_parse_duration(headers.get(k)) for k in
                  ('retry-after', 'x-ratelimit-reset-tokens', 'x-ratelimit-reset-requests')] if headers else []
    known = [c for c in candidates if c is not None]
    delay = max(known) if known else 2 ** attempt * 2
    return min(delay + 0.5, MAX_WAIT_429)          # cộng 0.5s cho chắc đã reset


def _limit_summary(exc):
    """Chỉ trích loại giới hạn và số liệu (vd. 'TPM: Limit 8000, Used 0, Requested 9500'), không in body."""
    try:
        message = json.loads(exc.read() or b'{}').get('error', {}).get('message', '')
    except (ValueError, AttributeError, OSError):
        return ''
    kind = re.search(r'\((TPM|TPD|RPM|RPD)\)', message)
    nums = re.search(r'Limit \d+, Used \d+, Requested \d+', message)
    return f' [{kind.group(1) if kind else "?"}: {nums.group(0)}]' if nums else ''


def post_json(url, headers, payload, sleep=time.sleep):
    # Groq (sau Cloudflare) chặn User-Agent mặc định "Python-urllib" bằng HTTP 403 (error 1010).
    base = {'Content-Type': 'application/json', 'User-Agent': 'k5-react-loop-agent/1.0'}
    data = json.dumps(payload).encode()
    for attempt in range(MAX_RETRIES_429 + 1):
        req = urllib.request.Request(url, data=data, headers={**base, **headers})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRIES_429:
                delay = _retry_delay(exc.headers, attempt)
                print(f'[provider] HTTP 429{_limit_summary(exc)}, chờ {delay:.1f}s rồi thử lại '
                      f'({attempt + 1}/{MAX_RETRIES_429})', file=sys.stderr, flush=True)
                sleep(delay)
                continue
            # Do not print response bodies: they may contain account or request data.
            raise RuntimeError(f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.') from exc
        except urllib.error.URLError as exc:
            # Lỗi mạng/DNS tạm thời (vd. getaddrinfo failed): thử lại tối đa 3 lần, chờ 5s/10s/20s.
            if attempt < min(3, MAX_RETRIES_429):
                delay = 5 * 2 ** attempt
                print(f'[provider] Lỗi mạng ({exc.reason}), chờ {delay}s rồi thử lại ({attempt + 1}/3)',
                      file=sys.stderr, flush=True)
                sleep(delay)
                continue
            raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
