"""One row per player per gameweek, with only information known at the deadline.

Every feature for gameweek g is built from gameweeks < g (plus the player's
price and the fixture itself), so nothing from the match being predicted
leaks in. The target is the player's total points in gameweek g.
"""
import numpy as np
import pandas as pd

STATS = ['minutes', 'total_points', 'expected_goals', 'expected_assists',
         'expected_goal_involvements', 'expected_goals_conceded', 'ict_index',
         'bps', 'bonus', 'saves', 'clean_sheets', 'goals_scored', 'assists']
TEAM_WINDOW = 6          # team form: last six fixtures
RECENT = (1, 3, 6)       # player form windows, in gameweeks


def _per_gw(fx):
    """Sum the fixture rows of a double gameweek into one player-GW row."""
    fx = fx.assign(started=(fx['minutes'] >= 60).astype(float),
                   n_fix=1, home=fx['was_home'].astype(float))
    agg = {c: 'sum' for c in STATS + ['started', 'n_fix', 'home']}
    agg.update({'starts': 'sum', 'value': 'first', 'xP': 'first', 'element_type': 'first',
                'team_id': 'first', 'web_name': 'first', 'difficulty': 'mean'})
    g = fx.groupby(['season', 'code', 'GW'], as_index=False).agg(agg)
    g['home'] = g['home'] / g['n_fix']
    return g.sort_values(['season', 'code', 'GW'])


def _team_form(fx):
    """Rolling team xG for/against over the previous TEAM_WINDOW fixtures."""
    side = (fx.groupby(['season', 'fixture', 'GW', 'team_id', 'opponent_team'], as_index=False)
              [['expected_goals', 'goals_scored']].sum()
              .rename(columns={'expected_goals': 'xg', 'goals_scored': 'goals'}))
    opp = side[['season', 'fixture', 'team_id', 'xg', 'goals']].rename(
        columns={'team_id': 'opponent_team', 'xg': 'xga', 'goals': 'ga'})
    side = side.merge(opp, on=['season', 'fixture', 'opponent_team'], how='left')
    side = side.sort_values(['season', 'team_id', 'GW'])
    out = []
    for (season, team), t in side.groupby(['season', 'team_id']):
        t = t.groupby('GW', as_index=False)[['xg', 'xga', 'goals', 'ga']].mean()
        roll = t[['xg', 'xga', 'goals', 'ga']].rolling(TEAM_WINDOW, min_periods=1).mean().shift(1)
        roll['GW'] = t['GW'].values
        full = pd.DataFrame({'GW': range(1, 39)}).merge(roll, on='GW', how='left').ffill()
        full['GW'] = range(1, 39)
        full['season'], full['team_id'] = season, team
        out.append(full)
    return pd.concat(out, ignore_index=True)


def build(fx):
    """Feature table from load_all() output."""
    g = _per_gw(fx)
    grp = g.groupby(['season', 'code'])

    # --- season to date, strictly before this gameweek
    for c in STATS + ['started', 'n_fix']:
        g[f'cum_{c}'] = grp[c].cumsum() - g[c]
    g['cum_gws'] = grp.cumcount()
    n90 = (g['cum_minutes'] / 90).replace(0, np.nan)
    for c in ['total_points', 'expected_goals', 'expected_assists',
              'expected_goal_involvements', 'expected_goals_conceded', 'ict_index',
              'bps', 'bonus', 'saves']:
        g[f'p90_{c}'] = g[f'cum_{c}'] / n90
    g['start_rate'] = g['cum_started'] / g['cum_n_fix'].replace(0, np.nan)

    # --- recent form
    for w in RECENT:
        for c in ('minutes', 'total_points', 'expected_goal_involvements', 'started', 'bps'):
            g[f'last{w}_{c}'] = grp[c].transform(
                lambda s: s.rolling(w, min_periods=1).sum().shift(1))

    # --- last season, linked by FPL's permanent `code`
    prev = {s: p for s, p in zip(sorted(g.season.unique())[1:], sorted(g.season.unique()))}
    last = (g.groupby(['season', 'code'])[STATS + ['starts']].sum().reset_index())
    last['season'] = last['season'].map({v: k for k, v in prev.items()})
    last = last.dropna(subset=['season'])
    last.columns = ['season', 'code'] + [f'prev_{c}' for c in STATS + ['starts']]
    g = g.merge(last, on=['season', 'code'], how='left')
    n90p = (g['prev_minutes'] / 90).replace(0, np.nan)
    for c in ['total_points', 'expected_goal_involvements', 'ict_index', 'bps', 'bonus',
              'expected_goals', 'expected_assists']:
        g[f'prev_p90_{c}'] = g[f'prev_{c}'] / n90p

    # --- team and opponent strength
    tf = _team_form(fx)
    g = g.merge(tf.rename(columns={'xg': 'team_xg', 'xga': 'team_xga',
                                   'goals': 'team_goals', 'ga': 'team_ga'}),
                on=['season', 'team_id', 'GW'], how='left')
    opp = (fx.groupby(['season', 'code', 'GW'])['opponent_team'].first().reset_index())
    g = g.merge(opp, on=['season', 'code', 'GW'], how='left')
    g = g.merge(tf.rename(columns={'team_id': 'opponent_team', 'xg': 'opp_xg', 'xga': 'opp_xga',
                                   'goals': 'opp_goals', 'ga': 'opp_ga'}),
                on=['season', 'opponent_team', 'GW'], how='left')

    g['target'] = g['total_points']
    return g
