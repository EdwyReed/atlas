"""Standalone Windows metadata GET worker. No files, native API or install path.

The credential is resolved and consumed in this process only. No redirects,
ambient proxies, cookies, exception text or request headers leave the worker.
"""
import json
import os
import math
import time
from email.utils import parsedate_to_datetime
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAXIMUM = 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate(url):
    if not isinstance(url, str) or re.search(r'[\x00-\x20\x7f\\]', url):
        raise ValueError()
    u = urlsplit(url)
    if (u.scheme != 'https' or u.netloc != 'civitai.red' or u.fragment
            or not (re.fullmatch(r'/api/v1/(?:models|model-versions)/[1-9][0-9]*', u.path)
                    or u.path in ('/api/v1/images', '/api/trpc/post.getInfinite', '/api/trpc/image.getInfinite'))):
        raise ValueError()
    from urllib.parse import parse_qs
    if any(k.lower() in ('token', 'access_token', 'api_key') for k in parse_qs(u.query)):
        raise ValueError()
    return url


def credential():
    key = os.environ.get('CIVITAI_API_KEY') or os.environ.get('CIVITAI_API_TOKEN')
    if not key:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as h:
            key = winreg.QueryValueEx(h, 'CIVITAI_API_KEY')[0]
    if not isinstance(key, str) or not key or '\n' in key or '\r' in key:
        raise ValueError()
    return key


def rejection(status, headers):
    value = {'ok': False, 'code': 'SOURCE_HTTP_' + str(status)}
    if status == 429:
        # Only a validated public delay crosses SSH, never arbitrary headers.
        delay = 60.0
        retry = headers.get('Retry-After') if headers else None
        if retry:
            try:
                delay = float(int(retry))
            except (ValueError, TypeError, OverflowError):
                try:
                    delay = parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 60.0
        value['retry_after'] = max(0.0, delay) if math.isfinite(delay) else 60.0
    return value


def run(url):
    try:
        validate(url)
    except Exception:
        return {'ok': False, 'code': 'SOURCE_URL'}
    try:
        key = credential()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, headers={
            'User-Agent': 'AtlasEngine/0.3.0 (+local trusted metadata collector)',
            'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
            'Accept-Encoding': 'identity'}, method='GET')
        with opener.open(request, timeout=35) as response:
            if response.status != 200:
                return rejection(response.status, response.headers)
            raw = response.read(MAXIMUM + 1)
        if len(raw) > MAXIMUM:
            return {'ok': False, 'code': 'SOURCE_LIMIT'}
        value = json.loads(raw)
        if not isinstance(value, dict):
            return {'ok': False, 'code': 'SOURCE_SHAPE'}
        # Fail closed even if the provider unexpectedly echoes an auth value.
        if key in json.dumps(value, ensure_ascii=False):
            return {'ok': False, 'code': 'SOURCE_REDACTED'}
        return {'ok': True, 'value': value}
    except urllib.error.HTTPError as exc:
        return rejection(exc.code, exc.headers)
    except Exception:
        return {'ok': False, 'code': 'SOURCE_TRANSPORT'}


def main(url):
    print(json.dumps(run(url), ensure_ascii=True))
