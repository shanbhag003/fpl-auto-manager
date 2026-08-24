"""Seed Gameweek 1 into data/season.json.

Run once, locally. The GW1 projections were computed before the deadline and
printed to the squad poster, but nothing wrote them to disk — so they go in by
hand here, transcribed from that poster. Everything else (player ids, actual
points, both squads, ranks) comes from the FPL API.

  pip install requests
  python seed_gw1.py                 # writes ./data/ — inspect, then commit
  python seed_gw1.py --push          # commits straight to GitHub instead

Safe to re-run: it merges into an existing season.json rather than replacing it,
and refuses to touch a gameweek the results pass has already marked final.
"""
import argparse
import base64
import json
import os
import sys
import unicodedata
from datetime import datetime, timezone

import requests

FPL = 'https://fantasy.premierleague.com/api'
GH_API = 'https://api.github.com'
HEADERS = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/126.0.0.0 Safari/537.36'),
           'Accept': 'application/json, text/plain, */*',
           'Referer': 'https://fantasy.premierleague.com/'}

BOT_ID = 2673853
HUMAN_ID = 1510697
GW = 1

# The deadline the projections predate. Used for `decided_at`, which is
# deliberately set to the bot's real run time, not to now.
DECIDED_AT = '2026-08-21T12:30:00+00:00'

# ---------------------------------------------------------------------------
# Transcribed from the GW1 squad poster. (web_name, club short, projection)
# The poster carried the LINEUP projection (score_now) only — score_run was
# never printed, so projected_run stays null for this gameweek rather than
# being invented.
# ---------------------------------------------------------------------------
XI = [
    ('Roefs',         'SUN', 4.21),
    ('N.Williams',    'NFO', 4.53),
    ('Guehi',         'MCI', 4.43),
    ('Senesi',        'TOT', 4.31),
    ('B.Fernandes',   'MUN', 7.08),
    ('Semenyo',       'MCI', 5.09),
    ('Enzo',          'CHE', 4.85),
    ('Wilson',        'LEE', 4.69),
    ('Szoboszlai',    'LIV', 4.61),
    ('Thiago',        'BRE', 4.99),
    ('Joao Pedro',    'CHE', 4.85),
]
BENCH = [                      # poster order, best sub first
    ('Virgil',         None, 4.26),
    ('Calvert-Lewin',  None, 4.22),
    ('Van Hecke',      None, 4.03),
    ('Dubravka',       None, 3.29),
]
CAPTAIN_NAME = 'B.Fernandes'
VICE_NAME = 'Semenyo'
SQUAD_VALUE = 1000             # £100.0m, from the poster

POS = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}


def norm(s):
    s = unicodedata.normalize('NFKD', str(s))
    return ''.join(c for c in s if not unicodedata.combining(c)).lower().strip()


def get(url, tries=5):
    """GET with backoff. Cloudflare sometimes blocks datacentre IPs on the
    first attempt, which is what running this from CI or Lambda looks like."""
    import time
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 404:
                return None
            if r.status_code == 200 and 'json' in r.headers.get('Content-Type', '').lower():
                return r.json()
            last = f"HTTP {r.status_code} ({r.headers.get('Content-Type','?')})"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        print(f"  retry {i+1}/{tries} on {url.rsplit('/api/',1)[-1]}: {last}")
        time.sleep(2 ** i)
    raise SystemExit(f"FPL unreachable: {url} — {last}. "
                     "Usually Cloudflare blocking the runner; re-run the job.")


def resolve(elements, teams, name, club):
    """Find one player by display name, disambiguating on club when given."""
    want = norm(name)
    hits = [e for e in elements if norm(e['web_name']) == want]
    if club:
        by_club = [e for e in hits if teams[e['team']] == club]
        if by_club:
            hits = by_club
    if not hits:                       # fall back to a contains match
        hits = [e for e in elements if want in norm(e['web_name'])]
        if club:
            hits = [e for e in hits if teams[e['team']] == club] or hits
    if len(hits) != 1:
        raise SystemExit(
            f"Could not pin down '{name}'"
            + (f" ({club})" if club else "")
            + f" — {len(hits)} candidates: "
            + ", ".join(f"{h['web_name']}/{teams[h['team']]}" for h in hits[:8])
            + "\nEdit the XI/BENCH tables in this script to match FPL's spelling.")
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--push', action='store_true',
                    help='commit to GitHub instead of writing ./data')
    ap.add_argument('--yes', action='store_true',
                    help='never prompt; continue past a poster/FPL squad mismatch')
    args = ap.parse_args()

    print("Reading FPL...")
    boot = get(f'{FPL}/bootstrap-static/')
    elements = boot['elements']
    teams = {int(t['id']): t['short_name'] for t in boot['teams']}
    event = next(e for e in boot['events'] if int(e['id']) == GW)
    finished, checked = bool(event.get('finished')), bool(event.get('data_checked'))
    print(f"  GW{GW}: finished={finished} data_checked={checked}")

    live = {int(e['id']): e.get('stats', {})
            for e in (get(f'{FPL}/event/{GW}/live/') or {}).get('elements', [])}
    bot_picks = get(f'{FPL}/entry/{BOT_ID}/event/{GW}/picks/')
    human_picks = get(f'{FPL}/entry/{HUMAN_ID}/event/{GW}/picks/')
    if not bot_picks:
        raise SystemExit("The bot's GW1 picks aren't published yet — try after the deadline.")

    mult = {int(p['element']): int(p.get('multiplier', 0))
            for p in bot_picks.get('picks', [])}
    submitted = set(mult)

    # --- resolve the poster's fifteen -------------------------------------
    squad, projections, order = [], {}, 0
    for group, role in ((XI, 'xi'), (BENCH, 'bench')):
        for name, club, proj in group:
            e = resolve(elements, teams, name, club)
            pid = int(e['id'])
            if role == 'bench':
                order += 1
            st = live.get(pid, {})
            squad.append({
                'id': pid, 'name': e['web_name'], 'pos': POS[int(e['element_type'])],
                'team': teams[int(e['team'])], 'cost': int(e['now_cost']),
                'role': role, 'bench_order': order if role == 'bench' else None,
                'multiplier': mult.get(pid, 1 if role == 'xi' else 0),
                'projected_now': proj, 'projected_run': None,
                'actual': st.get('total_points'), 'minutes': st.get('minutes'),
                'note': None,
            })
            projections[str(pid)] = [proj, None]
            flag = '' if pid in submitted else '   <-- NOT in the submitted GW1 squad'
            print(f"  {e['web_name']:16} {teams[int(e['team'])]:4} id={pid:<4} "
                  f"proj {proj:>5.2f}  actual {st.get('total_points')}{flag}")

    got = {p['id'] for p in squad}
    if got != submitted:
        print("\n!! The poster and FPL disagree on the squad.")
        print("   only on poster:", sorted(got - submitted))
        print("   only on FPL   :", sorted(submitted - got))
        print("   Fix the tables above before committing.")
        if not args.yes and sys.stdin.isatty():
            if input("   Continue anyway? [y/N] ").strip().lower() != 'y':
                sys.exit(1)
        elif not args.yes:
            sys.exit("   Refusing to continue non-interactively. Re-run with --yes "
                     "once you've checked the difference above.")

    cap = resolve(elements, teams, CAPTAIN_NAME, None)
    vice = resolve(elements, teams, VICE_NAME, None)

    xi_rows = [p for p in squad if p['role'] == 'xi']
    xi_total = sum(p['projected_now'] for p in xi_rows)
    cap_proj = next(p['projected_now'] for p in xi_rows if p['id'] == int(cap['id']))
    shape = {}
    for p in xi_rows:
        shape[p['pos']] = shape.get(p['pos'], 0) + 1

    def actuals(picks):
        h = (picks or {}).get('entry_history') or {}
        return {'total': h.get('points'), 'bench': h.get('points_on_bench'),
                'hits': h.get('event_transfers_cost') or 0,
                'gw_rank': h.get('rank'), 'overall_rank': h.get('overall_rank')}

    human_squad = []
    if human_picks:
        horder = 0
        for p in human_picks['picks']:
            pid = int(p['element'])
            e = next((x for x in elements if int(x['id']) == pid), {})
            starter = int(p['position']) <= 11
            if not starter:
                horder += 1
            st = live.get(pid, {})
            human_squad.append({
                'id': pid, 'name': e.get('web_name', str(pid)),
                'pos': POS.get(int(e.get('element_type', 0)), '?'),
                'team': teams.get(int(e.get('team', 0)), ''),
                'cost': int(e.get('now_cost', 0)),
                'role': 'xi' if starter else 'bench',
                'bench_order': None if starter else horder,
                'multiplier': int(p.get('multiplier', 0)),
                # Only 15 players were frozen for GW1, so the hand-picked squad
                # cannot be scored against the model for this gameweek. Left
                # null rather than recomputed after the fact.
                'projected_now': None, 'projected_run': None,
                'actual': st.get('total_points'), 'minutes': st.get('minutes'),
                'note': None,
            })

    record = {
        'gw': GW,
        'deadline': event.get('deadline_time'),
        'decided_at': DECIDED_AT,
        'status': 'final' if checked else 'projected',
        'instrumented': True,
        'mode': 'automated',
        'bot': {
            'squad': squad, 'captain': int(cap['id']), 'vice': int(vice['id']),
            'formation': f"{shape.get('DEF',0)}-{shape.get('MID',0)}-{shape.get('FWD',0)}",
            'bank': (bot_picks.get('entry_history') or {}).get('bank', 0),
            'value': SQUAD_VALUE,
            'projected': {'xi': round(xi_total, 2),
                          'captain_bonus': round(cap_proj, 2),
                          'total': round(xi_total + cap_proj, 2)},
            'actual': actuals(bot_picks),
        },
        'human': {
            'squad': human_squad,
            'captain': next((int(p['element']) for p in (human_picks or {}).get('picks', [])
                             if int(p.get('multiplier', 0)) >= 2), None),
            'projected': {'xi': None, 'captain_bonus': None, 'total': None},
            'actual': actuals(human_picks),
        },
        'transfers': [],
        'no_transfer_reason': 'First gameweek — the squad was built from scratch, '
                              'so there was nothing to transfer out of.',
        'news': [],
        'chips_flagged': [],
    }

    # --- merge into season.json -------------------------------------------
    season = load_season(args.push)
    if season is None:
        season = {'schema_version': 1, 'season': '2026/27', 'generated_at': None,
                  'instrumented_from_gw': GW,
                  'entries': {'bot': {'id': BOT_ID, 'name': '', 'manager': ''},
                              'human': {'id': HUMAN_ID, 'name': '', 'manager': ''}},
                  'totals': {'bot': {'points': 0, 'overall_rank': None},
                             'human': {'points': 0, 'overall_rank': None}},
                  'gameweeks': []}

    prior = next((g for g in season['gameweeks'] if int(g['gw']) == GW), None)
    if prior and prior.get('status') == 'final' and prior.get('instrumented'):
        raise SystemExit("GW1 is already final and instrumented — nothing to do.")

    season['gameweeks'] = sorted(
        [g for g in season['gameweeks'] if int(g['gw']) != GW] + [record],
        key=lambda g: int(g['gw']))
    season['instrumented_from_gw'] = GW
    season['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')

    for key, eid in (('bot', BOT_ID), ('human', HUMAN_ID)):
        meta = get(f'{FPL}/entry/{eid}/') or {}
        hist = (get(f'{FPL}/entry/{eid}/history/') or {}).get('current') or []
        season['entries'][key] = {
            'id': eid, 'name': meta.get('name', ''),
            'manager': f"{meta.get('player_first_name','')} "
                       f"{meta.get('player_last_name','')}".strip()}
        if hist:
            season['totals'][key] = {'points': hist[-1].get('total_points'),
                                     'overall_rank': hist[-1].get('overall_rank')}

    proj_file = {'gw': GW, 'written_at': DECIDED_AT, 'partial': True,
                 'source': 'transcribed from the GW1 squad poster; '
                           'covers the 15 selected players only',
                 'scores': projections}

    b = season['totals']['bot']['points']
    h = season['totals']['human']['points']
    print(f"\nGW{GW}: bot projected {record['bot']['projected']['total']:.1f}, "
          f"scored {record['bot']['actual']['total']} | "
          f"hand-picked scored {record['human']['actual']['total']}")
    print(f"Season so far — bot {b}, hand-picked {h}")

    if args.push:
        push('data/season.json', season, f'GW{GW}: seed real projections + results')
        push(f'data/projections/gw{GW}.json', proj_file,
             f'GW{GW}: projections, transcribed from the poster')
    else:
        os.makedirs('data/projections', exist_ok=True)
        write('data/season.json', season)
        write(f'data/projections/gw{GW}.json', proj_file)
        print("\nWrote ./data — check it, then commit:")
        print("  git add data && git commit -m 'GW1: real projections and results' && git push")


def write(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, separators=(',', ':'), ensure_ascii=False)
    print(f"  wrote {path}")


def _gh():
    repo, tok = os.environ.get('GITHUB_REPO'), os.environ.get('GITHUB_TOKEN')
    if not (repo and tok):
        raise SystemExit("--push needs GITHUB_REPO and GITHUB_TOKEN in the environment.")
    return repo, {'Authorization': f'Bearer {tok}',
                  'Accept': 'application/vnd.github+json'}


def load_season(from_github):
    if not from_github:
        try:
            return json.load(open('data/season.json', encoding='utf-8'))
        except FileNotFoundError:
            return None
    repo, hdr = _gh()
    branch = os.environ.get('GITHUB_BRANCH', 'main')
    r = requests.get(f'{GH_API}/repos/{repo}/contents/data/season.json?ref={branch}',
                     headers=hdr, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return json.loads(base64.b64decode(r.json()['content']))


def push(path, obj, message):
    repo, hdr = _gh()
    branch = os.environ.get('GITHUB_BRANCH', 'main')
    cur = requests.get(f'{GH_API}/repos/{repo}/contents/{path}?ref={branch}',
                       headers=hdr, timeout=20)
    sha = cur.json().get('sha') if cur.status_code == 200 else None
    body = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
    payload = {'message': message, 'branch': branch,
               'content': base64.b64encode(body.encode()).decode()}
    if sha:
        payload['sha'] = sha
    r = requests.put(f'{GH_API}/repos/{repo}/contents/{path}', headers=hdr,
                     json=payload, timeout=25)
    if r.status_code not in (200, 201):
        raise SystemExit(f"push {path} -> HTTP {r.status_code}: {r.text[:300]}")
    print(f"  pushed {path}")


def lambda_handler(event, context):
    """Fallback route: run this from a throwaway Lambda instead of CI.

    Needs GITHUB_REPO and GITHUB_TOKEN in the environment and the requests
    layer attached. Always pushes; never prompts.
    """
    sys.argv = ['seed_gw1.py', '--push', '--yes']
    main()
    return {'statusCode': 200, 'body': 'GW1 seeded'}


if __name__ == '__main__':
    main()
