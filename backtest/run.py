"""Compare player-points predictors on past seasons.

    python backtest/run.py

Each season is predicted gameweek by gameweek from information available at
that deadline. The learned model is trained only on seasons before the one it
is tested on.

FPL's own projection (the archive's `xP`) is deliberately left out. Holding
everything known at the deadline fixed, xP_g still moves with gameweek g's
actual points (coefficient 0.28-0.37, 2022-23 to 2024-25) but not with
gameweek g+1's (~0.01) — it was recorded after the matches. Any model given it
looks far better than it could be live.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import data        # noqa: E402
import features    # noqa: E402
import models      # noqa: E402

TRAIN_FROM = '2022-23'              # first season with xG and `starts`
TEST = ['2023-24', '2024-25', '2025-26']
PICKLE = os.path.join(data.CACHE, 'features.pkl')


def load():
    if os.path.exists(PICKLE):
        f = pd.read_pickle(PICKLE)
    else:
        f = features.build(data.load_all())
        f.to_pickle(PICKLE)
    return f[f.element_type.between(1, 4)]     # 5 = 2024-25 assistant managers


def predictions(f):
    preds = []
    for season in TEST:
        test = f[f.season == season].copy()
        train = f[(f.season >= TRAIN_FROM) & (f.season < season)]
        test['bot'] = models.bot_formula(test)
        test['gbm'], _ = models.gbm(train, test)
        preds.append(test)
    return pd.concat(preds)


def report(p, names):
    # Players a manager would realistically consider: started recently, or
    # a regular last season at the start of this one.
    cand = (p['last3_minutes'] > 0) | ((p['cum_gws'] == 0) & (p['prev_minutes'] >= 900))
    rows = []
    for m in names:
        r = {'model': m}
        r['MAE'] = (p[m] - p.target).abs().mean()
        r['corr'] = p[m].corr(p.target)
        r['corr_cand'] = p.loc[cand, m].corr(p.loc[cand, 'target'])
        # buying: the model's top 10 per position each week, what they scored
        top = (p.sort_values(m, ascending=False).groupby(['season', 'GW', 'element_type']).head(10))
        r['top10/pos'] = top.target.mean()
        # captaincy: the single highest-rated player each week
        cap = p.loc[p.groupby(['season', 'GW'])[m].idxmax()]
        r['captain'] = cap.target.mean()
        r['top3'] = (p.sort_values(m, ascending=False).groupby(['season', 'GW']).head(3)
                     .target.mean())
        rows.append(r)
    return pd.DataFrame(rows).set_index('model')


def main():
    f = load()
    p = predictions(f)
    names = ['bot', 'gbm']
    pd.set_option('display.float_format', '{:.3f}'.format)
    print("All test seasons (2023-24 to 2025-26), per player-gameweek:\n")
    print(report(p, names).to_string())
    for s in TEST:
        print(f"\n{s}:")
        print(report(p[p.season == s], names)[['corr_cand', 'top10/pos', 'captain']].to_string())
    p[['season', 'GW', 'code', 'web_name', 'element_type', 'value', 'target'] + names].to_pickle(
        os.path.join(data.CACHE, 'predictions.pkl'))


if __name__ == '__main__':
    main()
