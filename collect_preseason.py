"""One-time collection of pre-season data for players new to the Premier League.

Run once, before the season starts, while club friendlies are still in FotMob's
recent-match window. Writes a Python dict that gets pasted into the bot, exactly
like the LAST_SEASON snapshot. The bot never calls FotMob at runtime.

Two things come out of it, per player:
  1. start_share  — share of club friendlies started (the reliable signal)
  2. output       — goals + assists, but ONLY against top-5-league opposition
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

# FotMob league ids for the top 5 European leagues, used to judge whether a
# friendly opponent was serious opposition.
TOP5 = {47: 'Premier League', 87: 'La Liga', 54: 'Bundesliga',
        55: 'Serie A', 53: 'Ligue 1'}


def norm(s):
    """Strip accents and case so 'Guéhi' matches 'Guehi'."""
    s = unicodedata.normalize('NFKD', str(s))
    return ''.join(c for c in s if not unicodedata.combining(c)).lower().strip()


def get(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=25)
            if r.status_code == 200 and 'json' in r.headers.get('Content-Type', ''):
                return r.json()
            print(f"    HTTP {r.status_code} on {url[-40:]}")
        except requests.RequestException as e:
            print(f"    {type(e).__name__}")
        time.sleep(2 ** i)
    return None


def build_top5_clubs():
    """Club names in the top 5 leagues, so friendly opponents can be graded."""
    names = set()
    for lid, lname in TOP5.items():
        d = get(f'{FOTMOB}/leagues?id={lid}')
        if not d:
            print(f"  ! could not load {lname}")
            continue
        found = set()

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get('id'), int) and o.get('id') > 1000 and o.get('name'):
                    found.add(norm(o['name']))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(d.get('table'))
        print(f"  {lname}: {len(found)} clubs")
        names |= found
        time.sleep(1)
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
    """Find a player in their club's squad. Club-scoped, so collisions are rare."""
    first, second = norm(row.first_name), norm(row.second_name)
    web = norm(row.web_name)
    full = f"{first} {second}".strip()

    if full in squad:
        return squad[full], 'full name'
    for key, pid in squad.items():
        if key == second or key.endswith(' ' + second):
            return pid, 'surname'
    for key, pid in squad.items():
        if web and web in key:
            return pid, 'web name'
    for key, pid in squad.items():
        if second and second in key.split():
            return pid, 'surname token'
    return None, None


def preseason_record(player_id, top5_clubs):
    """Club friendlies and the Community Shield since June. Internationals excluded."""
    d = get(f'{FOTMOB}/playerData?id={player_id}')
    if not d:
        return None

    matches = []
    for m in d.get('recentMatches') or []:
        date = m.get('matchDate', {}).get('utcTime', '')[:10]
        if date < '2026-06-15':
            continue
        league = str(m.get('leagueName', ''))
        # club-level only: 'Friendlies' without 'Club' is international
        if 'Club Friendlies' not in league and 'Community Shield' not in league:
            continue
        matches.append({
            'date': date,
            'opp': m.get('opponentTeamName', ''),
            'min': int(m.get('minutesPlayed') or 0),
            'g': int(m.get('goals') or 0),
            'a': int(m.get('assists') or 0),
            'bench': bool(m.get('onBench')),
            'serious': norm(m.get('opponentTeamName', '')) in top5_clubs,
        })

    if not matches:
        return None

    appearances = [m for m in matches if m['min'] > 0]
    starts = [m for m in appearances if not m['bench'] and m['min'] >= 45]
    serious = [m for m in appearances if m['serious']]

    return {
        'games': len(matches),
        'apps': len(appearances),
        'starts': len(starts),
        'minutes': sum(m['min'] for m in appearances),
        'serious_mins': sum(m['min'] for m in serious),
        'serious_ga': sum(m['g'] + m['a'] for m in serious),
        'total_ga': sum(m['g'] + m['a'] for m in appearances),
    }


def main():
    print("1. Building top-5-league club list...")
    top5 = build_top5_clubs()
    print(f"   {len(top5)} clubs total\n")

    raw = json.load(open('pl_clubs.json'))
    pl_clubs = {v: int(k) for k, v in raw.items()}   # name -> id
    name_fix = {'Man City': 'Manchester City', 'Man Utd': 'Manchester United',
                'Spurs': 'Tottenham Hotspur', 'Newcastle': 'Newcastle United',
                'Nott\'m Forest': 'Nottingham Forest', 'Bournemouth': 'AFC Bournemouth',
                'Brighton': 'Brighton & Hove Albion', 'Leeds': 'Leeds United',
                'Ipswich': 'Ipswich Town', 'Coventry': 'Coventry City',
                'Hull': 'Hull City', 'Crystal Palace': 'Crystal Palace'}

    unproven = pd.read_csv('unproven.csv')
    print(f"2. Fetching squads and matching {len(unproven)} players...\n")

    results, unmatched, no_data = {}, [], []
    for club, group in unproven.groupby('team_name'):
        fot_name = name_fix.get(club, club)
        team_id = pl_clubs.get(fot_name)
        if not team_id:
            print(f"  ! no FotMob id for {club}")
            unmatched += list(group.web_name)
            continue

        squad = club_squad(team_id)
        print(f"  {club}: squad {len(squad)}, matching {len(group)} players")
        time.sleep(1)

        for row in group.itertuples():
            pid, how = match_player(row, squad)
            if not pid:
                unmatched.append(f"{row.web_name} ({club})")
                continue
            rec = preseason_record(pid, top5)
            time.sleep(0.6)
            if not rec:
                no_data.append(f"{row.web_name} ({club})")
                continue
            results[int(row.id)] = rec
            print(f"      {row.web_name:20} {rec['starts']}/{rec['games']} starts, "
                  f"{rec['minutes']:>4} min, {rec['serious_ga']} G+A vs top-5")

    print(f"\n3. Done. {len(results)} players with data, "
          f"{len(unmatched)} unmatched, {len(no_data)} matched but no friendlies.")
    json.dump({'results': results, 'unmatched': unmatched, 'no_data': no_data},
              open('preseason_raw.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
