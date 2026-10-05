"""Read-only proxy for two public FPL endpoints, for the site's squad lookup.

FPL's API sends no Access-Control-Allow-Origin header, so the browser can't
read it from github.io. This Lambda sits behind a Function URL whose CORS
setting allows the Pages origin, and forwards exactly two path shapes:

    /entry/{id}/
    /entry/{id}/event/{gw}/picks/

Anything else is a 400. It is not a general proxy: no other paths, no query
strings passed on, GET only, no credentials, no secrets. Standard library only,
so the function needs no layers. Deployed by the Deploy Lambda workflow as
`fpl-proxy`; setup in DEPLOY.md.
"""
import json
import re
import urllib.error
import urllib.request

FPL = 'https://fantasy.premierleague.com/api'
ROUTES = (
    re.compile(r'^/entry/(?P<id>\d{1,10})/$'),
    re.compile(r'^/entry/(?P<id>\d{1,10})/event/(?P<gw>\d{1,2})/picks/$'),
)
# FPL sits behind Cloudflare, which serves an HTML block page to bare
# datacenter requests. Same headers as the bot.
HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://fantasy.premierleague.com/',
}
TIMEOUT = 8          # seconds; the function's own timeout should be ~10
CACHE_SECONDS = 60   # picks and entry details change at most a few times a day


def _reply(status, body, cache=False):
    headers = {'Content-Type': 'application/json',
               'Cache-Control': f'public, max-age={CACHE_SECONDS}' if cache else 'no-store'}
    return {'statusCode': status, 'headers': headers,
            'body': body if isinstance(body, str) else json.dumps(body)}


def route(path):
    """The upstream FPL URL for an allowed path, or None."""
    for pattern in ROUTES:
        m = pattern.match(path or '')
        if m:
            gw = m.groupdict().get('gw')
            if gw is not None and not 1 <= int(gw) <= 38:
                return None
            return FPL + path
    return None


def lambda_handler(event, context):
    method = ((event.get('requestContext') or {}).get('http') or {}).get('method', 'GET')
    if method != 'GET':
        return _reply(405, {'error': 'method_not_allowed'})

    upstream = route(event.get('rawPath'))
    if upstream is None:
        return _reply(400, {'error': 'bad_path',
                            'allowed': ['/entry/{id}/', '/entry/{id}/event/{gw}/picks/']})

    req = urllib.request.Request(upstream, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode('utf-8')
            if 'json' not in (r.headers.get('Content-Type') or ''):
                return _reply(502, {'error': 'upstream_not_json'})
            return _reply(200, body, cache=True)
    except urllib.error.HTTPError as e:
        # 404: no such entry, or picks not public yet (before the deadline)
        if e.code == 404:
            return _reply(404, {'error': 'not_found'}, cache=True)
        if e.code == 503:
            return _reply(503, {'error': 'fpl_updating'})   # FPL's own maintenance window
        return _reply(502, {'error': 'upstream_error', 'status': e.code})
    except Exception as e:
        print(f"[proxy] {upstream}: {type(e).__name__}: {e}")
        return _reply(504, {'error': 'upstream_unreachable'})
