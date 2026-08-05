import requests
import json
import os
import time
import traceback
import smtplib
from email.mime.text import MIMEText
import pandas as pd
from datetime import datetime, timedelta

# ---- Tunable constants ----
TRANSFER_HIT_COST = 4          # points lost per transfer beyond the free allowance
MIN_TRANSFER_GAIN = 0.5        # minimum projected-points improvement required to transfer at all
TOP_N_CANDIDATES = 3           # shortlist size before any randomness
SSM_PARAM_NAME = "/fpl-bot/last-processed-gameweek"
UNAVAILABLE_STATUSES = {'i', 's', 'u', 'n'}          # injured / suspended / unavailable / not in squad
STATUS_PENALTY = {'a': 0, 'd': -3, 'i': -50, 's': -50, 'u': -50, 'n': -50}
STATUS_LABELS = {'a': 'Available', 'd': 'Doubtful', 'i': 'Injured', 's': 'Suspended',
                  'u': 'Unavailable', 'n': 'Not in squad'}

# --- Chip recommendation thresholds (info-only, nothing is auto-played) ---
BENCH_BOOST_THRESHOLD = 8.0        # combined bench score above which Bench Boost is worth flagging
TRIPLE_CAPTAIN_THRESHOLD = 9.0     # captain score above which Triple Captain is worth flagging
FREE_HIT_BLANK_THRESHOLD = 3       # squad players with 0 fixtures this week before flagging Free Hit
WILDCARD_UNAVAILABLE_THRESHOLD = 3  # squad players injured/suspended/doubtful before flagging Wildcard


# --- Network settings -------------------------------------------------------
# FPL sits behind Cloudflare. A bare python-requests call (no User-Agent) from an
# AWS datacenter IP gets served an HTML block page instead of JSON, which is what
# caused "Expecting value: line 1 column 1 (char 0)".
BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://fantasy.premierleague.com/',
    'X-Requested-With': 'XMLHttpRequest',
}
REQUEST_TIMEOUT = 20       # seconds per attempt
MAX_RETRIES = 4            # attempts before giving up
BACKOFF_BASE = 2           # seconds: 1, 2, 4, 8


class FPLUnavailable(Exception):
    """Raised when the FPL API is unreachable, blocked, or mid-update."""


def get(url, retries=MAX_RETRIES):
    """GET a FPL endpoint and return parsed JSON.

    Unlike a bare json.loads(response.content), this checks the status code and
    content type first, retries transient failures with backoff, and logs the
    start of the response body so CloudWatch shows WHY it failed.
    """
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                if 'json' in content_type.lower():
                    return response.json()

                # 200 but HTML = maintenance page or Cloudflare interstitial
                last_error = f"200 but Content-Type was '{content_type}' (not JSON)"
                print(f"[get] {url} -> {last_error}. Body starts: {response.text[:200]!r}")

            elif response.status_code in (403, 429):
                last_error = f"HTTP {response.status_code} — blocked or rate-limited by Cloudflare"
                print(f"[get] {url} -> {last_error}. Body starts: {response.text[:200]!r}")

            elif response.status_code >= 500:
                last_error = f"HTTP {response.status_code} — FPL server error (likely mid-update)"
                print(f"[get] {url} -> {last_error}")

            else:
                last_error = f"HTTP {response.status_code}"
                print(f"[get] {url} -> {last_error}. Body starts: {response.text[:200]!r}")

        except requests.Timeout:
            last_error = f"timed out after {REQUEST_TIMEOUT}s"
            print(f"[get] {url} -> {last_error}")
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"[get] {url} -> {last_error}")
        except ValueError as e:
            # response.json() failed even though Content-Type claimed JSON
            last_error = f"malformed JSON: {e}"
            print(f"[get] {url} -> {last_error}")

        if attempt < retries - 1:
            wait = BACKOFF_BASE ** attempt
            print(f"[get] retrying in {wait}s ({attempt + 2}/{retries})")
            time.sleep(wait)

    raise FPLUnavailable(
        f"Could not get JSON from {url} after {retries} attempts. Last problem: {last_error}. "
        "Usually this means Cloudflare blocked the Lambda IP, or FPL is mid-gameweek-update. "
        "Nothing was changed on your team."
    )


def check_update(events_df):
    today = datetime.now()
    tomorrow = (today + timedelta(days=1)).timestamp()
    today_ts = today.timestamp()
    upcoming = events_df.loc[events_df.deadline_time_epoch > today_ts]
    if upcoming.empty:
        return False, None
    deadline = upcoming.iloc[0].deadline_time_epoch
    gameweek = upcoming.iloc[0].id
    return deadline < tomorrow, gameweek


# --- Idempotency: remember which gameweek was last actioned ---
def already_processed(gameweek):
    import boto3
    ssm = boto3.client('ssm')
    try:
        resp = ssm.get_parameter(Name=SSM_PARAM_NAME)
        return int(resp['Parameter']['Value']) == gameweek
    except ssm.exceptions.ParameterNotFound:
        return False


def mark_processed(gameweek):
    import boto3
    ssm = boto3.client('ssm')
    ssm.put_parameter(Name=SSM_PARAM_NAME, Value=str(gameweek), Type='String', Overwrite=True)


def get_data(bootstrap_data, gameweek):
    players_df = pd.DataFrame(bootstrap_data['elements'])
    teams_df = pd.DataFrame(bootstrap_data['teams'])

    fixtures = get(f'https://fantasy.premierleague.com/api/fixtures/?event={gameweek}')
    fixtures_df = pd.DataFrame(fixtures)

    players_df['ep_next'] = players_df['ep_next'].astype(float)

    teams = dict(zip(teams_df.id, teams_df.name))
    players_df['team_name'] = players_df['team'].map(teams)

    if fixtures_df.empty:
        players_df['diff'] = 0
        players_df['fixture_count'] = 0
    else:
        home_strength = dict(zip(teams_df.id, teams_df.strength_overall_home))
        away_strength = dict(zip(teams_df.id, teams_df.strength_overall_away))
        fixtures_df['team_a_strength'] = fixtures_df['team_a'].map(away_strength)
        fixtures_df['team_h_strength'] = fixtures_df['team_h'].map(home_strength)

        a_players = pd.merge(players_df, fixtures_df, how="inner", left_on=["team"], right_on=["team_a"],
                     suffixes=("", "_fixture"))
        h_players = pd.merge(players_df, fixtures_df, how="inner", left_on=["team"], right_on=["team_h"],
                     suffixes=("", "_fixture"))
        a_players['diff'] = a_players['team_a_strength'] - a_players['team_h_strength']
        h_players['diff'] = h_players['team_h_strength'] - h_players['team_a_strength']

        fixture_rows = pd.concat([a_players, h_players])
        per_player = fixture_rows.groupby('id').agg(
            diff=('diff', 'mean'),
            fixture_count=('diff', 'size')
        ).reset_index()

        players_df = players_df.merge(per_player, on='id', how='left')
        players_df['diff'] = players_df['diff'].fillna(0)
        players_df['fixture_count'] = players_df['fixture_count'].fillna(0)  # 0 = blank gameweek

    players_df['score'] = (
        (players_df['ep_next'] * players_df['fixture_count'])
        + (players_df['diff'] / 3)
        + players_df['status'].map(STATUS_PENALTY).fillna(-50)
    )

    return players_df, fixtures_df


def pick_out_candidate(my_team):
    worst = my_team.sort_values('score').head(TOP_N_CANDIDATES)
    weights = worst['score'].max() - worst['score'] + 1
    return worst.sample(1, weights=weights)


def pick_in_candidate(potential_players):
    best = potential_players.sort_values('score', ascending=False).head(TOP_N_CANDIDATES)
    if best.empty:
        return None
    weights = best['score'] - best['score'].min() + 1
    return best.sample(1, weights=weights)


def pick_starting_xi(squad_df):
    squad_df = squad_df.sort_values('score', ascending=False)
    gk = squad_df[squad_df.element_type == 1]
    df_ = squad_df[squad_df.element_type == 2]
    mid = squad_df[squad_df.element_type == 3]
    fwd = squad_df[squad_df.element_type == 4]

    starters = pd.concat([gk.head(1), df_.head(3), mid.head(2), fwd.head(1)])
    counts = {2: 3, 3: 2, 4: 1}
    caps = {2: 5, 3: 5, 4: 3}

    remaining_pool = pd.concat([df_.iloc[3:], mid.iloc[2:], fwd.iloc[1:]]).sort_values('score', ascending=False)
    extra_needed = 4
    extras = []
    for _, player in remaining_pool.iterrows():
        if extra_needed == 0:
            break
        et = player['element_type']
        if counts[et] < caps[et]:
            extras.append(player)
            counts[et] += 1
            extra_needed -= 1

    if extras:
        starters = pd.concat([starters, pd.DataFrame(extras)])

    subs = squad_df.loc[~squad_df.id.isin(starters.id)]
    return starters, subs


# --- NEW: human-readable reasoning for a single player's score ---
def explain_player(player_row):
    reasons = []
    reasons.append(f"projected score {player_row.score:.2f} "
                    f"(ep_next {player_row.ep_next:.2f} × {int(player_row.fixture_count)} fixture(s))")

    if player_row.fixture_count == 0:
        reasons.append("blank gameweek — no fixture this week")
    elif player_row.fixture_count > 1:
        reasons.append(f"double gameweek — {int(player_row.fixture_count)} fixtures this week")

    if player_row.diff > 0:
        reasons.append(f"favourable fixture (+{player_row.diff:.1f} difficulty)")
    elif player_row.diff < 0:
        reasons.append(f"tough fixture ({player_row.diff:.1f} difficulty)")

    reasons.append(f"status: {STATUS_LABELS.get(player_row.status, player_row.status)}")
    return "; ".join(reasons)


# --- NEW: reasoning for why a transfer was or wasn't made ---
def explain_transfer_made(player_out, player_in, gain, required_gain, has_free_transfer, hit_cost):
    lines = [
        f"OUT — {player_out.web_name.iat[0]}: {explain_player(player_out.iloc[0])}",
        f"IN  — {player_in.web_name.iat[0]}: {explain_player(player_in.iloc[0])}",
        "",
        f"Projected gain from this swap: {gain:.2f} points."
    ]
    if has_free_transfer:
        lines.append(f"A free transfer was available (minimum bar to bother: {required_gain:.2f} pts) — this cleared it.")
    else:
        lines.append(f"No free transfer was available, so a -{hit_cost} point hit applied. "
                      f"The gain still cleared the higher bar of {required_gain:.2f} pts, so it was worth it.")
    return "\n".join(lines)


def explain_no_transfer(player_out, best_candidate, gain, required_gain, has_free_transfer, hit_cost):
    if best_candidate is None:
        return (f"Weakest player in the squad: {player_out.web_name.iat[0]} "
                f"({explain_player(player_out.iloc[0])}).\n"
                "No replacement was found — every option was ruled out by budget, position, "
                "or the 3-players-per-club limit.")

    lines = [
        f"Weakest player considered: {player_out.web_name.iat[0]}: {explain_player(player_out.iloc[0])}",
        f"Best available replacement: {best_candidate.web_name.iat[0]}: {explain_player(best_candidate.iloc[0])}",
        "",
        f"Projected gain: {gain:.2f} points."
    ]
    if has_free_transfer:
        lines.append(f"A free transfer was available, but {gain:.2f} pts is below the "
                      f"{required_gain:.2f} pt minimum bar to bother making a change.")
    else:
        lines.append(f"No free transfer was available — an extra transfer costs {hit_cost} pts, "
                      f"so a gain of at least {required_gain:.2f} pts was required. "
                      f"The best option only offered {gain:.2f}, so no transfer was made.")
    return "\n".join(lines)


# --- Chip recommendations (info-only — never played automatically) ---
def evaluate_chips(squad_df, bench_df, captain_row):
    recommendations = []

    bench_score = bench_df['score'].sum()
    if bench_score >= BENCH_BOOST_THRESHOLD:
        recommendations.append(("Bench Boost", True,
            f"Your bench is projected {bench_score:.1f} points this week — strong enough that "
            f"playing all 15 could be worth it."))
    else:
        recommendations.append(("Bench Boost", False,
            f"Bench projected only {bench_score:.1f} points this week — not worth playing."))

    cap_score = captain_row.score
    cap_double = captain_row.fixture_count > 1
    if cap_score >= TRIPLE_CAPTAIN_THRESHOLD or cap_double:
        reason = f"{captain_row.web_name} is projected {cap_score:.1f} points"
        if cap_double:
            reason += " across a double gameweek"
        reason += " — a strong week to triple instead of double their points."
        recommendations.append(("Triple Captain", True, reason))
    else:
        recommendations.append(("Triple Captain", False,
            f"{captain_row.web_name}'s projected {cap_score:.1f} points isn't exceptional enough "
            f"to justify Triple Captain this week."))

    blank_count = int((squad_df['fixture_count'] == 0).sum())
    if blank_count >= FREE_HIT_BLANK_THRESHOLD:
        recommendations.append(("Free Hit", True,
            f"{blank_count} of your 15 players have no fixture this week (blank gameweek) — "
            f"Free Hit would let you field a full-strength team just for this week."))
    else:
        recommendations.append(("Free Hit", False,
            f"Only {blank_count} squad player(s) have a blank this week — not enough to justify Free Hit."))

    unavailable_count = int((squad_df['status'] != 'a').sum())
    if unavailable_count >= WILDCARD_UNAVAILABLE_THRESHOLD:
        recommendations.append(("Wildcard", True,
            f"{unavailable_count} of your 15 players are currently doubtful, injured, or suspended — "
            f"worth considering a Wildcard to rebuild properly rather than one-by-one transfers."))
    else:
        recommendations.append(("Wildcard", False,
            f"Only {unavailable_count} squad player(s) are flagged as unavailable — "
            f"a Wildcard doesn't look necessary yet."))

    return recommendations


# --- Suggested rebuild squad, shown only when Wildcard or Free Hit is flagged ---
def build_suggested_squad(players_df, budget):
    """Optimal 15-man squad within `budget`, maximising total `score`.

    Uses PuLP + CBC for a true mathematical optimum. If the PuLP layer isn't
    attached, falls back to build_suggested_squad_fallback(), which gets within
    about 1% without any extra dependency — so the bot never silently returns
    nothing.
    """
    try:
        import pulp
    except ImportError:
        print("[optimizer] PuLP not available — using the pure-Python fallback. "
              "Attach the PuLP layer for the exact optimum.")
        return build_suggested_squad_fallback(players_df, budget)

    POSITION_REQUIREMENTS = {1: 2, 2: 5, 3: 5, 4: 3}
    MAX_PER_CLUB = 3

    eligible = players_df.loc[~players_df.status.isin(UNAVAILABLE_STATUSES)].copy()
    eligible = eligible[eligible['now_cost'].notna() & eligible['score'].notna()]
    if eligible.empty:
        print("[optimizer] No eligible players.")
        return None

    prob = pulp.LpProblem("Chip_Squad_Suggestion", pulp.LpMaximize)
    ids = eligible.id.tolist()
    pick = pulp.LpVariable.dicts("pick", ids, cat="Binary")

    score = dict(zip(eligible.id, eligible.score.astype(float)))
    cost = dict(zip(eligible.id, eligible.now_cost.astype(float)))
    pos = dict(zip(eligible.id, eligible.element_type.astype(int)))
    club = dict(zip(eligible.id, eligible.team.astype(int)))

    prob += pulp.lpSum(pick[i] * score[i] for i in ids)
    prob += pulp.lpSum(pick[i] * cost[i] for i in ids) <= budget

    for p, need in POSITION_REQUIREMENTS.items():
        prob += pulp.lpSum(pick[i] for i in ids if pos[i] == p) == need

    for c in set(club.values()):
        prob += pulp.lpSum(pick[i] for i in ids if club[i] == c) <= MAX_PER_CLUB

    try:
        prob.solve(pulp.PULP_CBC_CMD(msg=0))
    except Exception as e:
        print(f"[optimizer] CBC solver failed ({e}) — using fallback.")
        return build_suggested_squad_fallback(players_df, budget)

    if pulp.LpStatus[prob.status] != "Optimal":
        print(f"[optimizer] No optimal solution ({pulp.LpStatus[prob.status]}) — using fallback.")
        return build_suggested_squad_fallback(players_df, budget)

    chosen = [i for i in ids if pick[i].value() == 1]
    squad = eligible[eligible.id.isin(chosen)].copy()
    print(f"[optimizer] PuLP/CBC optimal — total score {squad.score.sum():.2f}, "
          f"cost {squad.now_cost.sum()/10:.1f}m of {budget/10:.1f}m budget.")
    return squad.sort_values(['element_type', 'score'], ascending=[True, False])


def build_suggested_squad_fallback(players_df, budget):
    """Fallback optimizer — no dependencies. Used only if PuLP is missing.

    Best 15-man squad within `budget`, using only the standard library.

    Replaces the PuLP version so no extra Lambda layer is needed. Runs a greedy
    seed followed by local search: repeatedly tries swapping each squad player
    for every legal alternative and keeps any swap that improves total score.
    Stops when no single swap helps.

    Constraints enforced: 2 GK / 5 DEF / 5 MID / 3 FWD, max 3 per club,
    total cost within budget, no unavailable players.

    Returns a 15-row DataFrame, or None if no legal squad fits the budget.
    """
    POSITION_REQUIREMENTS = {1: 2, 2: 5, 3: 5, 4: 3}
    MAX_PER_CLUB = 3

    eligible = players_df.loc[~players_df.status.isin(UNAVAILABLE_STATUSES)].copy()
    eligible = eligible[eligible['now_cost'].notna() & eligible['score'].notna()]
    if eligible.empty:
        print("[optimizer:fallback] No eligible players.")
        return None

    # Compact records — much faster than repeated DataFrame lookups
    pool = [{'id': int(r.id), 'pos': int(r.element_type), 'club': int(r.team),
             'cost': float(r.now_cost), 'score': float(r.score)}
            for r in eligible.itertuples()]

    by_pos = {}
    for p in pool:
        by_pos.setdefault(p['pos'], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x['score'], reverse=True)

    for pos, need in POSITION_REQUIREMENTS.items():
        if len(by_pos.get(pos, [])) < need:
            print(f"[optimizer:fallback] Not enough eligible players in position {pos}.")
            return None

    def club_ok(squad, club, exclude_id=None):
        n = sum(1 for p in squad if p['club'] == club and p['id'] != exclude_id)
        return n < MAX_PER_CLUB

    # --- greedy seed: cheapest legal squad, so we start inside the budget ---
    squad = []
    for pos, need in POSITION_REQUIREMENTS.items():
        picked = 0
        for p in sorted(by_pos[pos], key=lambda x: x['cost']):
            if picked == need:
                break
            if club_ok(squad, p['club']):
                squad.append(p)
                picked += 1
        if picked < need:
            print(f"[optimizer:fallback] Could not fill position {pos} legally.")
            return None

    spent = sum(p['cost'] for p in squad)
    if spent > budget:
        print(f"[optimizer:fallback] Even the cheapest legal squad costs "
              f"{spent/10:.1f}m, over the {budget/10:.1f}m budget.")
        return None

    # --- local search: swap one player at a time while it helps ---
    squad_ids = {p['id'] for p in squad}
    improved = True
    passes = 0

    while improved and passes < 40:
        improved = False
        passes += 1

        for i, current in enumerate(squad):
            headroom = budget - (spent - current['cost'])
            best_swap, best_delta = None, 0.0

            for cand in by_pos[current['pos']]:
                if cand['id'] in squad_ids:
                    continue
                if cand['cost'] > headroom:
                    continue  # list is score-sorted, not cost-sorted, so keep going
                delta = cand['score'] - current['score']
                if delta <= best_delta:
                    continue
                if cand['club'] != current['club'] and not club_ok(squad, cand['club'],
                                                                   exclude_id=current['id']):
                    continue
                best_swap, best_delta = cand, delta

            if best_swap is not None:
                squad_ids.discard(current['id'])
                squad_ids.add(best_swap['id'])
                spent = spent - current['cost'] + best_swap['cost']
                squad[i] = best_swap
                improved = True

    # --- paired moves: downgrade one slot to afford an upgrade elsewhere ---
    # Single swaps get stuck because no individual change fits the budget.
    # Swapping two at once frees money in one slot and spends it in another.
    improved = True
    pair_passes = 0

    while improved and pair_passes < 60:
        improved = False
        pair_passes += 1

        for i in range(len(squad)):
            if improved:
                break
            for j in range(len(squad)):
                if i == j:
                    continue
                a_cur, b_cur = squad[i], squad[j]

                # cheaper alternatives for slot i, ranked by least score lost
                downgrades = [c for c in by_pos[a_cur['pos']]
                              if c['id'] not in squad_ids and c['cost'] < a_cur['cost']]
                downgrades.sort(key=lambda c: c['score'] - a_cur['score'], reverse=True)

                for down in downgrades[:12]:
                    freed = a_cur['cost'] - down['cost']
                    loss = a_cur['score'] - down['score']
                    headroom = budget - (spent - a_cur['cost'] - b_cur['cost']
                                         + down['cost'])

                    for up in by_pos[b_cur['pos']]:
                        if up['id'] in squad_ids or up['id'] == down['id']:
                            continue
                        if up['cost'] > headroom:
                            continue
                        gain = up['score'] - b_cur['score']
                        if gain - loss <= 1e-9:
                            continue

                        trial = [p for k, p in enumerate(squad) if k not in (i, j)]
                        if not (club_ok(trial, down['club'])
                                and club_ok(trial + [down], up['club'])):
                            continue

                        squad_ids.discard(a_cur['id'])
                        squad_ids.discard(b_cur['id'])
                        squad_ids.add(down['id'])
                        squad_ids.add(up['id'])
                        spent += down['cost'] - a_cur['cost'] + up['cost'] - b_cur['cost']
                        squad[i], squad[j] = down, up
                        improved = True
                        break

                    if improved:
                        break
                if improved:
                    break

    total = sum(p['score'] for p in squad)
    print(f"[optimizer:fallback] {passes} single pass(es), {pair_passes} pair pass(es); "
          f"total score {total:.2f}, cost {spent/10:.1f}m of {budget/10:.1f}m budget.")

    result = eligible[eligible['id'].isin(squad_ids)].copy()
    return result.sort_values(['element_type', 'score'], ascending=[True, False])


def format_squad_table(squad_df):
    """Plain-text, fixed-width table — renders aligned in any monospace-font email view."""
    position_names = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    header = f"{'POS':<4}{'PLAYER':<20}{'TEAM':<14}{'PRICE':>8}{'SCORE':>8}"
    lines = [header, "-" * len(header)]
    total_cost = 0
    for pos in [1, 2, 3, 4]:
        for _, p in squad_df.loc[squad_df.element_type == pos].iterrows():
            total_cost += p.now_cost
            price_str = f"£{p.now_cost / 10:.1f}m"
            lines.append(f"{position_names[pos]:<4}{p.web_name:<20}{p.team_name:<14}{price_str:>8}{p.score:>8.2f}")
    lines.append("-" * len(header))
    lines.append(f"Total cost: £{total_cost / 10:.1f}m")
    return "\n".join(lines)


def format_chip_section(recommendations, suggested_table):
    lines = ["Chip recommendations (info only — nothing is played automatically):"]
    for chip, recommended, reason in recommendations:
        tag = "CONSIDER" if recommended else "skip"
        lines.append(f"  [{tag}] {chip} — {reason}")

    if suggested_table is not None:
        lines.append("")
        lines.append("Suggested squad if you play Wildcard or Free Hit "
                      "(budget = your current squad value + bank):")
        lines.append("")
        lines.append(suggested_table)
    elif suggested_table is None:
        pass  # no Wildcard/Free Hit flagged this week, or optimizer unavailable — nothing to add

    return "\n".join(lines)


def send_email_notification(status_line, reason_text, captain, vice_captain, chip_section):
    """Fire-and-forget info email. No approval step, just a summary of what happened and why."""
    sender = os.environ.get('SMTP_EMAIL')
    app_password = os.environ.get('SMTP_APP_PASSWORD')
    recipient = os.environ.get('NOTIFY_EMAIL')

    if not all([sender, app_password, recipient]):
        print("Email env vars not set — skipping notification.")
        return

    body = (
        "Your FPL team was checked automatically.\n\n"
        f"{status_line}\n\n"
        f"{reason_text}\n\n"
        f"Captain:      {captain}\n"
        f"Vice-captain: {vice_captain}\n\n"
        f"{chip_section}\n"
    )
    msg = MIMEText(body)
    msg['Subject'] = "FPL Bot: Team updated this gameweek"
    msg['From'] = sender
    msg['To'] = recipient

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, app_password)
            server.sendmail(sender, recipient, msg.as_string())
        print("Notification email sent.")
    except Exception as e:
        print(f"Email failed to send: {e}")


# --- NEW: error notification email ---
def send_error_email(error_text):
    sender = os.environ.get('SMTP_EMAIL')
    app_password = os.environ.get('SMTP_APP_PASSWORD')
    recipient = os.environ.get('NOTIFY_EMAIL')

    if not all([sender, app_password, recipient]):
        print("Email env vars not set — could not send error notification either.")
        return

    body = (
        "The FPL bot hit an error and did NOT finish its run this time — "
        "no transfer or lineup change was made.\n\n"
        "Error details:\n"
        f"{error_text}\n\n"
        "Full details are in the AWS Lambda CloudWatch logs for this function."
    )
    msg = MIMEText(body)
    msg['Subject'] = "FPL Bot: Something went wrong"
    msg['From'] = sender
    msg['To'] = recipient

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, app_password)
            server.sendmail(sender, recipient, msg.as_string())
        print("Error notification email sent.")
    except Exception as e:
        # If even the error email fails, just log it — nothing more we can do.
        print(f"Error email itself failed to send: {e}")

def should_send_outage_email():
    """True at most once per ERROR_EMAIL_COOLDOWN_HOURS.

    An FPL outage lasting a day would otherwise send one email per run. After a
    few of those you stop reading them, which defeats the point of alerting.
    """
    param = '/fpl-bot/last-outage-email'
    now = datetime.utcnow()

    try:
        import boto3
        ssm = boto3.client('ssm')
        try:
            resp = ssm.get_parameter(Name=param)
            last = datetime.fromisoformat(resp['Parameter']['Value'])
            hours_since = (now - last).total_seconds() / 3600
            if hours_since < ERROR_EMAIL_COOLDOWN_HOURS:
                print(f"Outage email suppressed — last one was {hours_since:.1f}h ago "
                      f"(cooldown is {ERROR_EMAIL_COOLDOWN_HOURS}h).")
                return False
        except ssm.exceptions.ParameterNotFound:
            pass  # never sent one before

        ssm.put_parameter(Name=param, Value=now.isoformat(),
                          Type='String', Overwrite=True)
        return True

    except Exception as e:
        print(f"Could not check outage-email cooldown ({e}) — sending anyway.")
        return True

ERROR_EMAIL_COOLDOWN_HOURS = 12


# ---------------------------------------------------------------------------
# ADVISORY MODE
#
# This build never logs in and never writes to FPL. FPL replaced its old
# login endpoint (users.premierleague.com) with an OAuth2 identity provider,
# so simple email/password submission no longer works.
#
# Everything here runs off public endpoints. The bot works out what it would
# do and emails you. You make the change in the app yourself.
# ---------------------------------------------------------------------------

POSITION_NAMES = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}


def fetch_public_squad(team_id, players_df, gameweek):
    """Read the current squad without logging in.

    FPL exposes a manager's picks publicly once a gameweek's deadline has
    passed, at /api/entry/{id}/event/{gw}/picks/. Before the first deadline of
    the season nothing is published yet, so this returns None and the caller
    falls back to recommending a squad from scratch.

    Returns (squad_df, bank, squad_value) or (None, None, None).
    """
    try:
        entry = get(f'https://fantasy.premierleague.com/api/entry/{team_id}/')
    except FPLUnavailable:
        raise
    except Exception as e:
        print(f"Could not read entry {team_id}: {e}")
        return None, None, None

    bank = entry.get('last_deadline_bank')
    value = entry.get('last_deadline_value')

    # Picks are published for completed gameweeks only, so look one behind.
    last_gw = gameweek - 1
    if last_gw < 1:
        print("No completed gameweek yet — no published squad to read.")
        return None, bank, value

    try:
        picks = get(f'https://fantasy.premierleague.com/api/entry/{team_id}'
                    f'/event/{last_gw}/picks/')
    except Exception as e:
        print(f"No published picks for GW{last_gw}: {e}")
        return None, bank, value

    ids = [p['element'] for p in picks.get('picks', [])]
    if not ids:
        return None, bank, value

    squad = players_df[players_df['id'].isin(ids)].copy()
    print(f"Read your GW{last_gw} squad: {len(squad)} players "
          f"(bank £{(bank or 0)/10:.1f}m, value £{(value or 0)/10:.1f}m)")
    return squad, bank, value


def recommend_transfer(my_team, players_df, bank):
    """Work out the single transfer worth making. Returns (text, made_bool).

    Assumes one free transfer, since transfer counts are not exposed publicly.
    """
    player_out = pick_out_candidate(my_team)
    out_pos = player_out.element_type.iat[0]
    out_price = player_out.now_cost.iat[0]
    budget = (bank or 0) + out_price

    club_counts = my_team['team'].value_counts()
    full_clubs = club_counts[club_counts >= 3].index.tolist()
    if player_out.team.iat[0] in full_clubs:
        full_clubs.remove(player_out.team.iat[0])

    pool = players_df[
        (players_df['element_type'] == out_pos)
        & (players_df['now_cost'] <= budget)
        & (~players_df['team'].isin(full_clubs))
        & (players_df['status'] == 'a')
        & (~players_df['id'].isin(my_team['id']))
    ]

    player_in = pick_in_candidate(pool)
    if player_in is None:
        return ("No legal replacement was affordable for "
                f"{player_out.web_name.iat[0]}, so no transfer is suggested.", False)

    gain = player_in.score.iat[0] - player_out.score.iat[0]

    if gain >= MIN_TRANSFER_GAIN:
        text = explain_transfer_made(player_out, player_in, gain,
                                     MIN_TRANSFER_GAIN, True, TRANSFER_HIT_COST)
        return text, True

    text = explain_no_transfer(player_out, player_in, gain, MIN_TRANSFER_GAIN,
                               True, TRANSFER_HIT_COST)
    return text, False


def format_xi(starters, subs, captain_row, vice_row):
    lines = []
    shape = starters['element_type'].value_counts()
    lines.append(f"Suggested XI  ({shape.get(2,0)}-{shape.get(3,0)}-{shape.get(4,0)})")
    lines.append("-" * 52)
    for r in starters.sort_values(['element_type', 'score'],
                                  ascending=[True, False]).itertuples():
        mark = ''
        if r.id == captain_row.id:
            mark = '  (C)'
        elif r.id == vice_row.id:
            mark = '  (V)'
        lines.append(f"  {POSITION_NAMES[r.element_type]:<4}{r.web_name:<20}"
                     f"{r.score:>7.2f}{mark}")
    lines.append("")
    lines.append("Bench, in order")
    for r in subs.sort_values('score', ascending=False).itertuples():
        lines.append(f"  {POSITION_NAMES[r.element_type]:<4}{r.web_name:<20}{r.score:>7.2f}")
    return "\n".join(lines)


def send_advice_email(subject_suffix, body):
    sender = os.environ.get('SMTP_EMAIL')
    app_password = os.environ.get('SMTP_APP_PASSWORD')
    recipient = os.environ.get('NOTIFY_EMAIL')

    if not (sender and app_password and recipient):
        print("SMTP env vars not set — skipping email. Body follows:\n")
        print(body)
        return

    msg = MIMEText(body)
    msg['Subject'] = f"FPL Bot: {subject_suffix}"
    msg['From'] = sender
    msg['To'] = recipient

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, app_password)
            server.send_message(msg)
        print("Advice email sent.")
    except Exception as e:
        print(f"Could not send email: {e}")


def run_advisor(team_id, test_mode=False):
    bootstrap_data = get('https://fantasy.premierleague.com/api/bootstrap-static/')
    events_df = pd.DataFrame(bootstrap_data['events'])

    should_update, gameweek = check_update(events_df)
    if gameweek is None:
        print("Season appears to be over.")
        return
    if not should_update:
        print("Deadline Too Far Away")
        return
    if not test_mode and already_processed(gameweek):
        print(f"GW{gameweek} advice already sent — not sending again.")
        return

    players_df, fixtures_df = get_data(bootstrap_data, gameweek)
    print(f"Scored {len(players_df)} players for GW{gameweek}.")

    my_team, bank, value = fetch_public_squad(team_id, players_df, gameweek)

    sections = [
        f"FPL advice for Gameweek {gameweek}.",
        "",
        "This is a recommendation only. Nothing has been changed on your team —",
        "FPL's new login system means the bot can no longer submit for you.",
        "Open the FPL app and make the changes yourself.",
        "",
        "=" * 52,
        "",
    ]

    if my_team is not None and len(my_team) >= 15:
        transfer_text, made = recommend_transfer(my_team, players_df, bank)
        sections.append("TRANSFER" if made else "TRANSFER — none suggested")
        sections.append("-" * 52)
        sections.append(transfer_text)
        sections.append("")
        squad_for_xi = my_team
    else:
        budget = (bank or 0) + (value or 1000)
        print(f"No published squad — building a full 15 for £{budget/10:.1f}m.")
        suggested = build_suggested_squad(players_df, budget)
        if suggested is None:
            sections.append("Could not build a suggested squad (optimizer unavailable).")
            body = "\n".join(sections)
            print(body)
            if not test_mode:
                send_advice_email(f"GW{gameweek} advice", body)
            return
        sections.append(f"SUGGESTED 15-MAN SQUAD  (£{budget/10:.1f}m budget)")
        sections.append("-" * 52)
        sections.append(format_squad_table(suggested))
        sections.append("")
        squad_for_xi = suggested

    starters, subs = pick_starting_xi(squad_for_xi)
    starters = starters.sort_values('score', ascending=False)
    captain_row = starters.iloc[0]
    vice_row = starters.iloc[1]

    sections.append(format_xi(starters, subs, captain_row, vice_row))
    sections.append("")
    sections.append(f"Captain      : {captain_row.web_name}  ({captain_row.score:.2f})")
    sections.append(f"Vice-captain : {vice_row.web_name}  ({vice_row.score:.2f})")
    sections.append("")

    recommendations = evaluate_chips(squad_for_xi, subs, captain_row)
    sections.append(format_chip_section(recommendations, None))

    body = "\n".join(sections)
    print(body)

    if not test_mode:
        send_advice_email(f"GW{gameweek} advice ready", body)
        mark_processed(gameweek)
    else:
        print("\nTEST MODE — no email sent, nothing marked as processed.")


# --- Lambda entry point ---
def lambda_handler(event, context):
    team_id = os.environ.get('FPL_TEAM_ID')
    if not team_id:
        raise Exception("FPL_TEAM_ID environment variable is not set.")

    try:
        run_advisor(team_id, test_mode=False)
        return {"statusCode": 200, "body": "OK"}

    except FPLUnavailable as e:
        message = (
            "The FPL API was unreachable this run, so no advice was produced.\n\n"
            f"{e}\n\n"
            "This is usually temporary. The bot will try again on its next run."
        )
        print(message)
        if should_send_outage_email():
            send_error_email(message)
        raise

    except Exception as e:
        error_details = f"{e}\n\n{traceback.format_exc()}"
        send_error_email(error_details)
        raise
