"""FPL results pass — fills actual points into data/season.json.

Runs in either of two places, and does the same thing in both:

  * GitHub Actions — set LOCAL_DATA=1 (or pass --local). It reads and writes
    ./data on the runner's checkout and lets the workflow commit. No token.
  * AWS Lambda — set GITHUB_REPO and GITHUB_TOKEN. It reads and writes through
    the GitHub Contents API instead.

A second, much simpler Lambda. It contains no scoring logic at all: it reads
what happened and writes it next to what the bot predicted.

Two jobs:
  1. Promote instrumented gameweeks from "projected" to "final" once FPL has
     finished checking the data.
  2. Create records for gameweeks that ran before the snapshot layer existed.
     Those get actuals only, and instrumented=false, so the front end can say
     plainly that no projection exists rather than inventing one.

Runs on a schedule; safe to run as often as you like. It only writes when
something actually changed.

Handler: lambda_function.lambda_handler
Env: GITHUB_REPO, GITHUB_TOKEN, GITHUB_BRANCH, FPL_TEAM_ID, HUMAN_ENTRY_ID
"""
import base64
import json
import os
import time
import traceback
from datetime import datetime, timezone

import requests

FPL = 'https://fantasy.premierleague.com/api'
GH_API = 'https://api.github.com'

BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://fantasy.premierleague.com/',
}

def _normalise_repo(value):
    """Accept a full GitHub URL as well as owner/repo.

    Pasting the browser URL into GITHUB_REPO is the obvious mistake to make,
    and it fails as a 404 on a path the API has never heard of — which reads
    like a permissions problem rather than a typo. Worse, a 404 on the READ
    looks identical to "the file does not exist yet", so the caller would
    happily rebuild a record that already had data in it.
    """
    v = (value or '').strip()
    for prefix in ('https://github.com/', 'http://github.com/',
                   'git@github.com:', 'github.com/'):
        if v.lower().startswith(prefix.lower()):
            v = v[len(prefix):]
            break
    v = v.strip('/')
    if v.endswith('.git'):
        v = v[:-4]
    # Anything beyond owner/repo (…/tree/main, …/blob/…) is not part of the slug.
    parts = [p for p in v.split('/') if p]
    return '/'.join(parts[:2])


GITHUB_REPO = _normalise_repo(os.environ.get('GITHUB_REPO', ''))
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_BRANCH = os.environ.get('GITHUB_BRANCH', 'main')
BOT_ID = int(os.environ.get('FPL_TEAM_ID', '0') or 0)
HUMAN_ID = int(os.environ.get('HUMAN_ENTRY_ID', '0') or 0)

# Filesystem mode: for CI, where the checkout is already on disk and the
# workflow does the committing.
LOCAL = (os.environ.get('LOCAL_DATA') == '1'
         or '--local' in __import__('sys').argv)

POS = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}


# --- plumbing ---------------------------------------------------------------

def get(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
            if r.status_code == 200 and 'json' in r.headers.get('Content-Type', '').lower():
                return r.json()
            if r.status_code == 404:
                return None
            last = f"HTTP {r.status_code}"
            print(f"[get] {url} -> {last}")
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            print(f"[get] {url} -> {last}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not read {url}: {last}")


def _gh_headers():
    return {'Authorization': f'Bearer {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'}


def gh_read(path):
    if LOCAL:
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f), None
        except FileNotFoundError:
            return None, None
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}'
    r = requests.get(url, headers=_gh_headers(), timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    payload = r.json()
    return json.loads(base64.b64decode(payload['content'])), payload['sha']


def gh_write(path, obj, message, sha=None):
    body = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
    if LOCAL:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        print(f"[local] wrote {path} ({len(body)} bytes) — {message}")
        return True
    payload = {'message': message, 'branch': GITHUB_BRANCH,
               'content': base64.b64encode(body.encode('utf-8')).decode('ascii')}
    if sha:
        payload['sha'] = sha
    r = requests.put(f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}',
                     headers=_gh_headers(), json=payload, timeout=25)
    if r.status_code not in (200, 201):
        print(f"[gh] write {path} -> HTTP {r.status_code}: {r.text[:200]!r}")
        return False
    print(f"[gh] wrote {path} ({len(body)} bytes)")
    return True


# --- FPL reads --------------------------------------------------------------

def unplayed_clubs(gw, all_fixtures):
    """Clubs with a fixture in this gameweek that hasn't been played yet.

    `finished` only goes true once FPL has processed the match; between the
    whistle and that it is `finished_provisional`. Both count as played.
    """
    out = set()
    for fx in all_fixtures:
        if int(fx.get('event') or 0) != gw:
            continue
        if not (fx.get('finished') or fx.get('finished_provisional')):
            out.add(int(fx['team_h']))
            out.add(int(fx['team_a']))
    return out


def live_points(gw):
    """{element_id: (total_points, minutes)} for a gameweek."""
    data = get(f'{FPL}/event/{gw}/live/')
    out = {}
    for e in (data or {}).get('elements', []):
        s = e.get('stats', {})
        out[int(e['id'])] = (int(s.get('total_points', 0)), int(s.get('minutes', 0)))
    return out


def entry_picks(entry_id, gw):
    """FPL's record of what an entry actually fielded. None before the deadline."""
    return get(f'{FPL}/entry/{entry_id}/event/{gw}/picks/')


def entry_history(entry_id):
    return get(f'{FPL}/entry/{entry_id}/history/') or {}


def entry_transfers(entry_id):
    by_gw = {}
    for t in get(f'{FPL}/entry/{entry_id}/transfers/') or []:
        by_gw.setdefault(int(t['event']), []).append(t)
    return by_gw


def frozen_projections(gw):
    """{player_id: [score_now, score_run]} written before the deadline, or None."""
    try:
        data, _ = gh_read(f'data/projections/gw{gw}.json')
    except Exception:
        return None
    return (data or {}).get('scores') if data else None


# --- building squad records -------------------------------------------------

def squad_from_picks(picks, elements, live, projections=None, unplayed=frozenset()):
    """Turn FPL's picks payload into schema Player records."""
    out = []
    bench_order = 0
    for p in picks.get('picks', []):
        pid = int(p['element'])
        el = elements.get(pid, {})
        pts, mins = live.get(pid, (None, None))
        starter = int(p['position']) <= 11
        if not starter:
            bench_order += 1
        proj = (projections or {}).get(str(pid)) or [None, None]
        pending = el.get('team_id') in unplayed and not (mins or pts)
        out.append({
            'id': pid,
            'name': el.get('web_name', str(pid)),
            'pos': POS.get(el.get('element_type'), '?'),
            'team': el.get('short', ''),
            'cost': el.get('now_cost', 0),
            'role': 'xi' if starter else 'bench',
            'bench_order': None if starter else bench_order,
            'multiplier': int(p.get('multiplier', 0)),
            'projected_now': proj[0],
            'projected_run': proj[1],
            'actual': None if pending else pts,
            'minutes': None if pending else mins,
            'pending': pending,
            'note': None,
        })
    return out


def projected_total(squad):
    """Sum of frozen projections for the XI, captain counted twice."""
    xi = [p for p in squad if p['role'] == 'xi' and p['projected_now'] is not None]
    if len(xi) < 11:
        return {'xi': None, 'captain_bonus': None, 'total': None}
    total = sum(p['projected_now'] for p in xi)
    cap = next((p['projected_now'] for p in squad
                if p['multiplier'] and p['multiplier'] >= 2
                and p['projected_now'] is not None), 0.0)
    return {'xi': round(total, 2), 'captain_bonus': round(cap, 2),
            'total': round(total + cap, 2)}


def live_total(picks, live):
    """Points so far, summed from the live feed. Used while a gameweek is in
    progress, because entry_history lags behind the matches."""
    total = 0
    bench = 0
    for p in picks.get('picks', []):
        pts = live.get(int(p['element']), (0, 0))[0] or 0
        m = int(p.get('multiplier', 0))
        if m > 0:
            total += pts * m
        elif int(p['position']) > 11:
            bench += pts
    return total, bench


def actuals_from_picks(picks):
    h = picks.get('entry_history', {}) or {}
    return {
        'total': h.get('points'),
        'bench': h.get('points_on_bench'),
        'hits': (h.get('event_transfers_cost') or 0),
        'gw_rank': h.get('rank'),
        'overall_rank': h.get('overall_rank'),
    }


def merge_actuals_into_bot_squad(existing, picks, elements, live, unplayed=frozenset()):
    """Patch actuals onto the squad the BOT recorded, preserving its own fields.

    Deliberately keeps `role` as submitted. An auto-sub shows up as a bench
    player with multiplier 1 — that was FPL's decision, not the bot's, and
    conflating the two would flatter the model.
    """
    by_id = {int(p['element']): p for p in picks.get('picks', [])}
    for p in existing:
        pid = int(p['id'])
        pts, mins = live.get(pid, (None, None))
        pending = elements.get(pid, {}).get('team_id') in unplayed and not (mins or pts)
        p['actual'] = None if pending else pts
        p['minutes'] = None if pending else mins
        p['pending'] = pending
        if pid in by_id:
            p['multiplier'] = int(by_id[pid].get('multiplier', p.get('multiplier', 0)))
        if not p.get('team'):
            p['team'] = elements.get(pid, {}).get('short', '')
    return existing


# --- the pass ---------------------------------------------------------------

def build_uninstrumented_record(gw, event, elements, live, bot_tf,
                                unplayed=frozenset(), is_final=True):
    """A gameweek that ran before the snapshot layer existed. Actuals only."""
    bot_picks = entry_picks(BOT_ID, gw)
    if not bot_picks:
        return None
    squad = squad_from_picks(bot_picks, elements, live, None, unplayed)
    shape = {}
    for p in squad:
        if p['role'] == 'xi':
            shape[p['pos']] = shape.get(p['pos'], 0) + 1
    captain = next((p['id'] for p in squad if p['multiplier'] >= 2), None)

    transfers = []
    for t in bot_tf.get(gw, []):
        oi, ii = int(t['element_out']), int(t['element_in'])
        transfers.append({
            'out': {'id': oi, 'name': elements.get(oi, {}).get('web_name', str(oi)),
                    'cost': t.get('element_out_cost', 0),
                    'pos': POS.get(elements.get(oi, {}).get('element_type'), '?')},
            'in': {'id': ii, 'name': elements.get(ii, {}).get('web_name', str(ii)),
                   'cost': t.get('element_in_cost', 0),
                   'pos': POS.get(elements.get(ii, {}).get('element_type'), '?')},
            'gain': None, 'free': None, 'hit_cost': None,
            'reason': None,
        })

    return {
        'gw': gw,
        'deadline': event.get('deadline_time'),
        'decided_at': None,
        'status': 'final' if is_final else 'live',
        'instrumented': False,
        'mode': 'unknown',
        'bot': {
            'squad': squad, 'captain': captain,
            'vice': next((p['id'] for p in squad
                          if p['multiplier'] == 1 and p['role'] == 'xi'), None),
            'formation': f"{shape.get('DEF',0)}-{shape.get('MID',0)}-{shape.get('FWD',0)}",
            'bank': (bot_picks.get('entry_history') or {}).get('bank', 0),
            'value': (bot_picks.get('entry_history') or {}).get('value', 0),
            'projected': {'xi': None, 'captain_bonus': None, 'total': None},
            'actual': actuals_from_picks(bot_picks),
        },
        'human': {'squad': [], 'captain': None,
                  'projected': {'xi': None, 'captain_bonus': None, 'total': None},
                  'actual': {'total': None, 'bench': None, 'hits': None,
                             'gw_rank': None, 'overall_rank': None}},
        'transfers': transfers,
        'no_transfer_reason': None,
        'news': [],
        'chips_flagged': [],
    }


def write_players(elements):
    """data/players.json: name, position, club and price for every player.

    The squad lookup on the site needs these alongside the projections, which
    carry ids only. Committed only when something in it changed (prices move a
    few times a week), so the history isn't flooded with identical commits.
    """
    players = {str(pid): [e['web_name'], POS.get(e['element_type'], ''),
                          e['short'], e['now_cost']]
               for pid, e in sorted(elements.items())}
    existing, sha = gh_read('data/players.json')
    if existing is not None and existing.get('players') == players:
        print("[players] unchanged — skipping the commit.")
        return
    gh_write('data/players.json',
             {'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'players': players},
             f"players: {len(players)} players", sha)


def run():
    if not LOCAL and not (GITHUB_REPO and GITHUB_TOKEN):
        raise RuntimeError("Set LOCAL_DATA=1 for filesystem mode, "
                           "or GITHUB_REPO and GITHUB_TOKEN for API mode.")
    print("mode:", "filesystem (./data)" if LOCAL else f"GitHub API ({GITHUB_REPO})")

    boot = get(f'{FPL}/bootstrap-static/')
    teams = {int(t['id']): t['short_name'] for t in boot['teams']}
    elements = {}
    for e in boot['elements']:
        elements[int(e['id'])] = {
            'web_name': e['web_name'], 'element_type': int(e['element_type']),
            'now_cost': int(e['now_cost']), 'short': teams.get(int(e['team']), ''),
            'team_id': int(e['team']),
        }
    events = {int(ev['id']): ev for ev in boot['events']}

    # Player metadata for the site's squad lookup. Separate from season.json,
    # and never allowed to stop the results pass.
    try:
        write_players(elements)
    except Exception as e:
        print(f"[players] skipped: {type(e).__name__}: {e}")

    # Every gameweek whose deadline has passed, not just the settled ones.
    # A gameweek is only marked `final` once FPL has finished CHECKING it —
    # `finished` flips at the last whistle, but bonus and Opta corrections land
    # after that. Until then it is carried as `live` and refreshed each run.
    now = datetime.now(timezone.utc).timestamp()
    started = sorted(gw for gw, ev in events.items()
                     if (ev.get('deadline_time_epoch') or 0) < now)
    if not started:
        print("No gameweek has kicked off yet — nothing to do.")
        return
    checked = {gw for gw in started if events[gw].get('data_checked')}
    print(f"{len(started)} gameweek(s) under way or done; "
          f"{len(checked)} fully checked.")

    all_fixtures = get(f'{FPL}/fixtures/') or []

    season, sha = gh_read('data/season.json')
    if season is None:
        season = {'schema_version': 1, 'season': '2026/27', 'generated_at': None,
                  'instrumented_from_gw': None,
                  'entries': {'bot': {'id': BOT_ID, 'name': '', 'manager': ''},
                              'human': {'id': HUMAN_ID, 'name': '', 'manager': ''}},
                  'totals': {'bot': {'points': 0, 'overall_rank': None},
                             'human': {'points': 0, 'overall_rank': None}},
                  'gameweeks': []}

    # A byte-for-byte copy of what the repo holds, so an unchanged run can
    # skip the commit entirely. Without this the function rewrites the same
    # file every few hours for as long as a gameweek sits at `live`, and the
    # commit history — the thing that proves a projection predated kickoff —
    # drowns in identical commits.
    before = json.dumps(season, sort_keys=True, separators=(',', ':'))
    original_stamp = season.get('generated_at')

    by_gw = {int(g['gw']): g for g in season.get('gameweeks', [])}
    bot_tf = entry_transfers(BOT_ID)
    changed = False

    for gw in started:
        rec = by_gw.get(gw)
        # A finalised gameweek never changes again.
        if rec and rec.get('status') == 'final' and rec['bot']['actual'].get('total') is not None:
            continue

        is_final = gw in checked
        live = live_points(gw)
        unplayed = unplayed_clubs(gw, all_fixtures)
        projections = frozen_projections(gw)
        if unplayed:
            print(f"GW{gw}: {len(unplayed)} club(s) still to play.")

        if rec is None:
            built = build_uninstrumented_record(gw, events[gw], elements, live,
                                                bot_tf, unplayed, is_final)
            if built is None:
                print(f"GW{gw}: no published picks for the bot entry — skipping.")
                continue
            rec = built
            by_gw[gw] = rec
            print(f"GW{gw}: created (pre-instrumentation, actuals only).")
        else:
            bot_picks = entry_picks(BOT_ID, gw)
            if not bot_picks:
                print(f"GW{gw}: picks not published yet — leaving as projected.")
                continue
            rec['bot']['squad'] = merge_actuals_into_bot_squad(
                rec['bot']['squad'], bot_picks, elements, live, unplayed)
            rec['bot']['actual'] = actuals_from_picks(bot_picks)
            if rec['bot']['actual']['total'] is None or not is_final:
                lt, lb = live_total(bot_picks, live)
                rec['bot']['actual']['total'] = lt
                if rec['bot']['actual']['bench'] is None:
                    rec['bot']['actual']['bench'] = lb
            rec['status'] = 'final' if is_final else 'live'
            rec['deadline'] = events[gw].get('deadline_time')
            print(f"GW{gw}: {'final' if is_final else 'live'} — "
                  f"{rec['bot']['actual']['total']} pts vs "
                  f"{rec['bot']['projected'].get('total')} projected.")

        # --- the hand-picked benchmark
        if HUMAN_ID:
            hp = entry_picks(HUMAN_ID, gw)
            if hp:
                hsquad = squad_from_picks(hp, elements, live, projections, unplayed)
                rec['human']['squad'] = hsquad
                rec['human']['captain'] = next(
                    (p['id'] for p in hsquad if p['multiplier'] >= 2), None)
                rec['human']['actual'] = actuals_from_picks(hp)
                if rec['human']['actual']['total'] is None or not is_final:
                    lt, lb = live_total(hp, live)
                    rec['human']['actual']['total'] = lt
                    if rec['human']['actual']['bench'] is None:
                        rec['human']['actual']['bench'] = lb
                # Only from frozen projections — never recomputed after the fact.
                rec['human']['projected'] = (projected_total(hsquad) if projections
                                             else {'xi': None, 'captain_bonus': None,
                                                   'total': None})
        changed = True

    if not changed:
        print("Everything already up to date.")
        return

    season['gameweeks'] = [by_gw[k] for k in sorted(by_gw)]

    for key, eid in (('bot', BOT_ID), ('human', HUMAN_ID)):
        if not eid:
            continue
        hist = entry_history(eid)
        current = hist.get('current') or []
        if current:
            last = current[-1]
            season['totals'][key] = {'points': last.get('total_points'),
                                     'overall_rank': last.get('overall_rank')}
        # entry/history lags a live gameweek, so total from the records we hold.
        recorded = sum((g[key]['actual'].get('total') or 0)
                       for g in season['gameweeks'])
        hits = sum((g[key]['actual'].get('hits') or 0) for g in season['gameweeks'])
        if recorded and recorded - hits > (season['totals'][key].get('points') or 0):
            season['totals'][key] = {'points': recorded - hits,
                                     'overall_rank': season['totals'][key].get('overall_rank')}
        meta = get(f'{FPL}/entry/{eid}/') or {}
        season.setdefault('entries', {}).setdefault(key, {})
        season['entries'][key].update({
            'id': eid,
            'name': meta.get('name', ''),
            'manager': f"{meta.get('player_first_name','')} "
                       f"{meta.get('player_last_name','')}".strip(),
        })

    # Tell the page when to expect the next refresh, and when the next
    # gameweek actually locks.
    upcoming = sorted((gw, ev) for gw, ev in events.items()
                      if (ev.get('deadline_time_epoch') or 0) >= now)
    season['refresh_interval_hours'] = float(os.environ.get('REFRESH_HOURS', '6'))
    season['next_deadline'] = ({'gw': upcoming[0][0],
                                'time': upcoming[0][1].get('deadline_time')}
                               if upcoming else None)
    season['live_gw'] = next((int(g['gw']) for g in season['gameweeks']
                              if g.get('status') == 'live'), None)

    instrumented = [int(g['gw']) for g in season['gameweeks'] if g.get('instrumented')]
    season['instrumented_from_gw'] = min(instrumented) if instrumented else None

    # Compare against the repo with generated_at held at its old value — that
    # field moves on every run, so including it would make every run look like
    # a change and defeat the whole check.
    season['generated_at'] = original_stamp
    if json.dumps(season, sort_keys=True, separators=(',', ':')) == before:
        print("Nothing actually changed — skipping the commit.")
        return

    season['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    label = 'final' if max(started) in checked else 'live'
    gh_write('data/season.json', season,
             f"results: GW{max(started)} ({label})", sha)


def lambda_handler(event, context):
    try:
        run()
        return {'statusCode': 200, 'body': 'OK'}
    except Exception as e:
        print(f"!! results pass failed: {e}")
        traceback.print_exc()
        raise


if __name__ == '__main__':
    run()