"""Collect pre-season data for ESTABLISHED players (those with a LAST_SEASON record).

Same source and method as collect_preseason.py, but for the players the first
pass skipped. Run once; the output is frozen into the bot.
"""
import json, time, unicodedata, sys, types
import requests
import pandas as pd

sys.path.insert(0, '.')
sys.modules['boto3'] = types.ModuleType('boto3')
import fpl_bot_hybrid as h

H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0',
     'Accept': 'application/json'}
FOTMOB = 'https://www.fotmob.com/api/data'
TOP5 = {47: 'Premier League', 87: 'La Liga', 54: 'Bundesliga',
        55: 'Serie A', 53: 'Ligue 1'}


def norm(s):
    s = unicodedata.normalize('NFKD', str(s))
    return ''.join(c for c in s if not unicodedata.combining(c)).lower().strip()


def get(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=25)
            if r.status_code == 200 and 'json' in r.headers.get('Content-Type', ''):
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 ** i)
    return None


def build_top5_clubs():
    names = set()
    for lid in TOP5:
        d = get(f'{FOTMOB}/leagues?id={lid}')
        if not d:
            continue

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get('id'), int) and o['id'] > 1000 and o.get('name'):
                    names.add(norm(o['name']))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(d.get('table'))
        time.sleep(0.8)
    return names


def club_squad(team_id):
    d = get(f'{FOTMOB}/teams?id={team_id}')
    if not d:
        return {}
    out = {}
    for group in d.get('squad', {}).get('squad', []):
        if group.get('title') == 'coach':
            continue
        for m in group.get('members', []):
            if m.get('id') and m.get('name'):
                out[norm(m['name'])] = int(m['id'])
    return out


def match_player(row, squad):
    first, second, web = norm(row.first_name), norm(row.second_name), norm(row.web_name)
    full = f"{first} {second}".strip()
    if full in squad:
        return squad[full]
    for key, pid in squad.items():
        if key == second or key.endswith(' ' + second):
            return pid
    for key, pid in squad.items():
        if web and web in key:
            return pid
    for key, pid in squad.items():
        if second and second in key.split():
            return pid
    return None


def preseason_record(player_id, top5):
    d = get(f'{FOTMOB}/playerData?id={player_id}')
    if not d:
        return None
    matches = []
    for m in d.get('recentMatches') or []:
        if m.get('matchDate', {}).get('utcTime', '')[:10] < '2026-06-15':
            continue
        league = str(m.get('leagueName', ''))
        if 'Club Friendlies' not in league and 'Community Shield' not in league:
            continue
        matches.append({
            'min': int(m.get('minutesPlayed') or 0),
            'g': int(m.get('goals') or 0),
            'a': int(m.get('assists') or 0),
            'bench': bool(m.get('onBench')),
            'serious': norm(m.get('opponentTeamName', '')) in top5,
        })
    apps = [m for m in matches if m['min'] > 0]
    starts = [m for m in apps if not m['bench'] and m['min'] >= 45]
    ser = [m for m in apps if m['serious']]
    return {
        'games': len(matches), 'starts': len(starts),
        'minutes': sum(m['min'] for m in apps),
        'serious_mins': sum(m['min'] for m in ser),
        'serious_ga': sum(m['g'] + m['a'] for m in ser),
    }


def main():
    print("building top-5 club list...")
    top5 = build_top5_clubs()
    print(f"  {len(top5)} clubs\n")

    bs = h.get('https://fantasy.premierleague.com/api/bootstrap-static/')
    p = pd.DataFrame(bs['elements'])
    teams = dict(zip(pd.DataFrame(bs['teams']).id, pd.DataFrame(bs['teams']).name))
    p['team_name'] = p['team'].map(teams)
    # established = has a LAST_SEASON record
    est = p[p.id.isin(h.LAST_SEASON.keys())]
    print(f"{len(est)} established players to collect\n")

    pl = get(f'{FOTMOB}/leagues?id=47')
    clubs = {}

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get('id'), int) and o['id'] > 1000 and o.get('name'):
                clubs[o['name']] = o['id']
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(pl.get('table'))

    fix = {'Man City': 'Manchester City', 'Man Utd': 'Manchester United',
           'Spurs': 'Tottenham Hotspur', 'Newcastle': 'Newcastle United',
           "Nott'm Forest": 'Nottingham Forest', 'Bournemouth': 'AFC Bournemouth',
           'Brighton': 'Brighton & Hove Albion', 'Leeds': 'Leeds United',
           'Ipswich': 'Ipswich Town', 'Coventry': 'Coventry City', 'Hull': 'Hull City'}

    import os
    results, misses = {}, []
    if os.path.exists('established_preseason.json'):
        prev=json.load(open('established_preseason.json'))
        results={int(k):v for k,v in prev['results'].items()}; misses=prev['misses']
        print(f"  resuming with {len(results)} already collected")
    done_clubs=set()
    for club, group in est.groupby('team_name'):
        if all(int(r.id) in results or r.web_name in misses for r in group.itertuples()):
            continue
        tid = clubs.get(fix.get(club, club))
        if not tid:
            print(f"  ! no club id for {club}")
            continue
        squad = club_squad(tid)
        time.sleep(0.8)
        got = 0
        for row in group.itertuples():
            if int(row.id) in results or row.web_name in misses:
                continue
            pid = match_player(row, squad)
            if not pid:
                misses.append(row.web_name)
                continue
            rec = preseason_record(pid, top5)
            time.sleep(0.5)
            if rec is None:
                misses.append(row.web_name)
                continue
            results[int(row.id)] = rec
            got += 1
        print(f"  {club:22} {got}/{len(group)}", flush=True)
        json.dump({'results': results, 'misses': misses},
                  open('established_preseason.json','w'))

    print(f"\n{len(results)} collected, {len(misses)} missed")
    zero = [k for k, v in results.items() if v['games'] == 0]
    print(f"{len(zero)} played no club friendlies at all")
    json.dump({'results': results, 'misses': misses},
              open('established_preseason.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
