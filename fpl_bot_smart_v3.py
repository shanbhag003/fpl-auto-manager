import requests
import json
import os
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


def get(url):
    response = requests.get(url)
    return json.loads(response.content)


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

        a_players = pd.merge(players_df, fixtures_df, how="inner", left_on=["team"], right_on=["team_a"])
        h_players = pd.merge(players_df, fixtures_df, how="inner", left_on=["team"], right_on=["team_h"])
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
    """
    Optimal 15-player squad within `budget`, maximizing the same composite `score`
    already used for transfer decisions. Requires the PuLP layer — returns None if
    it isn't available (e.g. not yet attached in Lambda, or not installed in Colab),
    so the rest of the bot still runs fine without it.
    """
    try:
        import pulp
    except ImportError:
        return None

    eligible = players_df.loc[~players_df.status.isin(UNAVAILABLE_STATUSES)].copy()
    POSITION_REQUIREMENTS = {1: 2, 2: 5, 3: 5, 4: 3}
    MAX_PER_CLUB = 3

    prob = pulp.LpProblem("Chip_Squad_Suggestion", pulp.LpMaximize)
    ids = eligible.id.tolist()
    pick_vars = pulp.LpVariable.dicts("pick", ids, cat="Binary")

    prob += pulp.lpSum(pick_vars[r.id] * r.score for r in eligible.itertuples())
    prob += pulp.lpSum(pick_vars[r.id] * r.now_cost for r in eligible.itertuples()) <= budget

    for pos, count in POSITION_REQUIREMENTS.items():
        prob += pulp.lpSum(
            pick_vars[r.id] for r in eligible.itertuples() if r.element_type == pos
        ) == count

    for team_id in eligible.team.unique():
        prob += pulp.lpSum(
            pick_vars[r.id] for r in eligible.itertuples() if r.team == team_id
        ) <= MAX_PER_CLUB

    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if pulp.LpStatus[prob.status] != "Optimal":
        return None

    selected_ids = [pid for pid in ids if pick_vars[pid].value() == 1]
    squad = eligible.loc[eligible.id.isin(selected_ids)].copy()
    return squad.sort_values(['element_type', 'score'], ascending=[True, False])


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


def update_team(email, password, team_id, test_mode=True):
    bootstrap_data = get('https://fantasy.premierleague.com/api/bootstrap-static/')
    events_df = pd.DataFrame(bootstrap_data['events'])

    should_update, gameweek = check_update(events_df)
    if not should_update:
        print("Deadline Too Far Away")
        return

    if not test_mode and already_processed(gameweek):
        print(f"Gameweek {gameweek} already processed — skipping to avoid a duplicate transfer.")
        return

    players_df, fixtures_df = get_data(bootstrap_data, gameweek)

    session = requests.session()
    login_data = {
        'login': email, 'password': password,
        'app': 'plfpl-web', 'redirect_uri': 'https://fantasy.premierleague.com/'
    }
    login_url = "https://users.premierleague.com/accounts/login/"
    session.post(url=login_url, data=login_data)

    url = f"https://fantasy.premierleague.com/api/my-team/{team_id}"
    team_response = session.get(url)

    try:
        team = team_response.json()
        if 'picks' not in team:
            raise ValueError("no picks in response")
    except Exception:
        raise Exception(
            "Could not fetch your team — login likely failed. "
            "Double-check FPL_EMAIL and FPL_PASSWORD."
        )

    my_ids = [x['element'] for x in team['picks']]
    my_team = players_df.loc[players_df.id.isin(my_ids)]
    potential_players = players_df.loc[~players_df.id.isin(my_ids)]
    bank = team['transfers']['bank']

    free_transfers = team['transfers']['limit']
    made_already = team['transfers']['made']
    hit_cost = team['transfers'].get('cost') or TRANSFER_HIT_COST
    has_free_transfer = (
        free_transfers is None
        or made_already < free_transfers
        or team['transfers'].get('status') == 'unlimited'
    )

    player_out = pick_out_candidate(my_team)
    position = player_out.element_type.iat[0]
    out_cost = player_out.now_cost.iat[0]
    budget = bank + out_cost

    dups_team = my_team.loc[my_team.id != player_out.id.iat[0]].pivot_table(index=['team'], aggfunc='size')
    invalid_teams = dups_team.loc[dups_team == 3].index.tolist()

    candidates = potential_players.loc[
        (~potential_players.team.isin(invalid_teams)) &
        (potential_players.element_type == position) &
        (potential_players.now_cost <= budget) &
        (~potential_players.status.isin(UNAVAILABLE_STATUSES))
    ]
    player_in = pick_in_candidate(candidates)

    made_transfer = False
    reason_text = ""
    gain = None
    required_gain = MIN_TRANSFER_GAIN if has_free_transfer else (hit_cost + MIN_TRANSFER_GAIN)

    if player_in is not None:
        gain = player_in.score.iat[0] - player_out.score.iat[0]

        if gain >= required_gain:
            made_transfer = True
            reason_text = explain_transfer_made(player_out, player_in, gain, required_gain, has_free_transfer, hit_cost)
            my_team = my_team.loc[my_team.id != player_out.id.iat[0]]
            my_team = pd.concat([my_team, player_in])
            print(reason_text)

            if not test_mode:
                headers = {
                    'content-type': 'application/json',
                    'origin': 'https://fantasy.premierleague.com',
                    'referer': 'https://fantasy.premierleague.com/transfers'
                }
                transfers = [{
                    "element_in": int(player_in.id.iat[0]),
                    "element_out": int(player_out.id.iat[0]),
                    "purchase_price": int(player_in.now_cost.iat[0]),
                    "selling_price": int(player_out.now_cost.iat[0])
                }]
                transfer_payload = {"transfers": transfers, "chip": None, "entry": team_id, "event": int(gameweek)}
                session.post(url='https://fantasy.premierleague.com/api/transfers/',
                             data=json.dumps(transfer_payload), headers=headers)
        else:
            reason_text = explain_no_transfer(player_out, player_in, gain, required_gain, has_free_transfer, hit_cost)
            print(reason_text)
    else:
        reason_text = explain_no_transfer(player_out, None, 0, required_gain, has_free_transfer, hit_cost)
        print(reason_text)

    # --- Pick starting XI + captain ---
    starters, subs = pick_starting_xi(my_team)
    starters_sorted = starters.sort_values('score', ascending=False)
    captain_id, captain_name = starters_sorted.iloc[0].id, starters_sorted.iloc[0].web_name
    vice_captain_id, vice_captain_name = starters_sorted.iloc[1].id, starters_sorted.iloc[1].web_name

    print(f"Captain: {captain_name}, Vice: {vice_captain_name}")

    # --- Chip recommendations (info only — evaluated every run, never auto-played) ---
    chip_recommendations = evaluate_chips(my_team, subs, starters_sorted.iloc[0])

    wildcard_flagged = any(name == "Wildcard" and flagged for name, flagged, _ in chip_recommendations)
    freehit_flagged = any(name == "Free Hit" and flagged for name, flagged, _ in chip_recommendations)

    suggested_table = None
    if wildcard_flagged or freehit_flagged:
        squad_value = team['transfers'].get('value')
        if squad_value is None:
            squad_value = int(my_team['now_cost'].sum())
        rebuild_budget = squad_value + bank

        suggested_squad = build_suggested_squad(players_df, rebuild_budget)
        if suggested_squad is not None:
            suggested_table = format_squad_table(suggested_squad)
        else:
            suggested_table = ("(Could not generate a suggested squad — either the PuLP layer "
                                "isn't attached in Lambda, or no valid squad fit the budget.)")

    chip_section = format_chip_section(chip_recommendations, suggested_table)
    print(chip_section)

    if not test_mode:
        picks = []
        count = 1
        for i in range(1, 5):
            players = starters.loc[starters.element_type == i]
            for ide in players.id.tolist():
                picks.append({"element": int(ide), "is_captain": ide == captain_id,
                              "is_vice_captain": ide == vice_captain_id, "position": count})
                count += 1
        for ide in subs.id.tolist():
            picks.append({"element": int(ide), "is_captain": False,
                          "is_vice_captain": False, "position": count})
            count += 1

        team_sheet = {"picks": picks, "chip": None}
        headers = {
            'content-type': 'application/json',
            'origin': 'https://fantasy.premierleague.com',
            'referer': 'https://fantasy.premierleague.com/my-team'
        }
        session.post(url=f'https://fantasy.premierleague.com/api/my-team/{team_id}/',
                     json=team_sheet, headers=headers)
        print("Team updated live.")

        mark_processed(gameweek)

        status_line = "A transfer was made this gameweek." if made_transfer else "No transfer was made this gameweek."
        send_email_notification(status_line, reason_text, captain_name, vice_captain_name, chip_section)
    else:
        print("TEST MODE — nothing was submitted to FPL, no email sent.")


# --- Lambda entry point ---
def lambda_handler(event, context):
    email = os.environ.get('FPL_EMAIL')
    password = os.environ.get('FPL_PASSWORD')
    team_id = os.environ.get('FPL_TEAM_ID')
    try:
        update_team(email, password, team_id, test_mode=False)
    except Exception as e:
        error_details = f"{e}\n\n{traceback.format_exc()}"
        send_error_email(error_details)
        raise  # still let Lambda/CloudWatch record this run as a failure
