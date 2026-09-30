"""Predictors compared by the backtest. Each returns expected points per player-GW."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import fpl_bot_hybrid as bot     # noqa: E402  (constants and coefficients, not the network code)


def bot_formula(f, use_xp=False, parts=False, ramp=None, fixture_scale=None,
                sp_by_matches=False):
    """The live bot's estimate_base_points + score_now, rebuilt from the table.

    Faithful except for three inputs with no history: pre-season friendlies
    (a small nudge for new signings), FPL's chance_of_playing (unknown here, so
    every player is treated as fully available), and the status penalty.
    Live, the bot blends in FPL's ep_next (up to 50% from GW7). The archive's
    xP cannot stand in for it: it was recorded after the matches and carries
    the result (see run.py). So by default the blend weight is zero and only
    the bot's own history-based estimate is scored.
    """
    n90 = (f['cum_minutes'] / 90).replace(0, np.nan)
    q_now = bot._quality_from_rates((f['cum_total_points'] / n90).fillna(0),
                                    (f['cum_expected_goal_involvements'] / n90).fillna(0),
                                    (f['cum_ict_index'] / n90).fillna(0))
    pn90 = (f['prev_minutes'] / 90).replace(0, np.nan)
    q_last = bot._quality_from_rates((f['prev_total_points'] / pn90).fillna(0),
                                     (f['prev_expected_goal_involvements'] / pn90).fillna(0),
                                     (f['prev_ict_index'] / pn90).fillna(0))
    slope = f['element_type'].map(lambda t: bot.PRICE_TO_PPG[t][0])
    icpt = f['element_type'].map(lambda t: bot.PRICE_TO_PPG[t][1])
    q_price = (slope * f['value'] + icpt).clip(lower=0.5)

    have = f['prev_minutes'].notna()
    prev_min = f['prev_minutes'].fillna(0)
    # `ramp`: minutes of this season before its rates fully replace the prior
    w_now = (f['cum_minutes'] / (ramp or bot.MIN_MINUTES_FOR_HISTORY)).clip(0, 1)
    w_snap = ((prev_min - bot.SNAPSHOT_MIN_MINUTES)
              / (bot.MIN_MINUTES_FOR_HISTORY - bot.SNAPSHOT_MIN_MINUTES)).clip(0, 1)
    prior = (w_snap * q_last + (1 - w_snap) * q_price).where(have, q_price)
    quality = w_now * q_now + (1 - w_now) * prior

    # start probability; `starts` is missing before 2022-23, so use 60+ minutes
    prev_starts = f['prev_starts'].where(f['prev_starts'] > 0, f['prev_minutes'] / 90 * 0.9)
    sp_now = (f['cum_started'] / bot.FULL_SEASON_STARTS).clip(bot.MIN_START_PROB, 1)
    sp_last = (prev_starts.fillna(0) / bot.FULL_SEASON_STARTS).clip(bot.MIN_START_PROB, 1)
    sp_prior = (w_snap * sp_last + (1 - w_snap) * bot.UNPROVEN_START_PROB).where(
        have, bot.UNPROVEN_START_PROB)
    # The live bot weights this season's start rate by the player's OWN minutes,
    # so a player who stops playing never moves off last season's rate.
    # sp_by_matches weights it by how many matches his team has played instead.
    if sp_by_matches:
        w_sp = (f['cum_n_fix'] / (bot.MIN_MINUTES_FOR_HISTORY / 90)).clip(0, 1)
        sp_now = (f['cum_started'] / f['cum_n_fix'].replace(0, np.nan)).fillna(0)
        start_prob = w_sp * sp_now + (1 - w_sp) * sp_prior
    else:
        start_prob = w_now * sp_now + (1 - w_now) * sp_prior

    # recent starts, weighted 0.5/0.3/0.2 and shrunk toward start_prob
    recent_num = recent_den = 0
    for lag, w in zip((1, 2, 3), bot.RECENT_START_WEIGHTS):
        s = f.groupby(['season', 'code'])['started'].shift(lag)
        recent_num = recent_num + w * s.fillna(0)
        recent_den = recent_den + w * s.notna()
    k = bot.RECENT_PRIOR_MATCHES
    recent = recent_num / pd.Series(recent_den, index=f.index).replace(0, np.nan)
    sp_recent = ((recent * sum(bot.RECENT_START_WEIGHTS) + start_prob * k)
                 / (sum(bot.RECENT_START_WEIGHTS) + k)).fillna(start_prob).clip(0, 1)

    hist_now = quality * sp_recent
    w_ep = np.minimum(bot.EP_NEXT_MAX_WEIGHT,
                      np.maximum(0, (f['GW'] - 1) / bot.EP_NEXT_TRUST_GAMEWEEK) * bot.EP_NEXT_MAX_WEIGHT)
    if not use_xp:
        w_ep = 0.0
    scale = bot.FIXTURE_SCALE if fixture_scale is None else fixture_scale
    mult = (1 + (3 - f['difficulty']) * scale).clip(0.8, 1.2)
    # ep_next already covers every fixture in the week; the history estimate is per fixture
    if parts:     # the formula's pieces, as features for the learned model
        return pd.DataFrame({'bot_quality': quality, 'bot_start_prob': sp_recent,
                             'bot_start_prob_season': start_prob,
                             'bot_mult': mult, 'bot_pred': hist_now * f['n_fix'] * mult})
    return w_ep * f['xP'] + (1 - w_ep) * hist_now * f['n_fix'] * mult


def fpl_xp(f):
    return f['xP']


FEATURES = [
    'element_type', 'value', 'n_fix', 'home', 'difficulty', 'GW', 'cum_gws',
    'cum_minutes', 'start_rate',
    'p90_total_points', 'p90_expected_goals', 'p90_expected_assists',
    'p90_expected_goal_involvements', 'p90_expected_goals_conceded', 'p90_ict_index',
    'p90_bps', 'p90_bonus', 'p90_saves',
    'last1_minutes', 'last3_minutes', 'last6_minutes', 'last1_started', 'last3_started',
    'last6_started', 'last3_total_points', 'last6_total_points',
    'last3_expected_goal_involvements', 'last6_expected_goal_involvements', 'last6_bps',
    'prev_minutes', 'prev_starts', 'prev_p90_total_points', 'prev_p90_expected_goal_involvements',
    'prev_p90_expected_goals', 'prev_p90_expected_assists', 'prev_p90_ict_index',
    'prev_p90_bps', 'prev_p90_bonus',
    'team_xg', 'team_xga', 'opp_xg', 'opp_xga', 'team_goals', 'team_ga', 'opp_goals', 'opp_ga',
]
PARAMS = dict(objective='regression', learning_rate=0.03, num_leaves=31,
              min_child_samples=100, subsample=0.8, subsample_freq=1,
              colsample_bytree=0.8, reg_lambda=5.0, n_estimators=600, verbose=-1)


def gbm(train, test, use_xp=False):
    """LightGBM trained only on seasons before the test season."""
    import lightgbm as lgb
    cols = FEATURES + (['xP'] if use_xp else [])
    model = lgb.LGBMRegressor(**PARAMS)
    model.fit(train[cols], train['target'])
    return pd.Series(model.predict(test[cols]), index=test.index), model
