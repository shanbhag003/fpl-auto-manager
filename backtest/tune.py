"""Sweep the bot formula's hand-set constants against the backtest.

    python backtest/tune.py
"""
import os
import sys
import warnings

import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(__file__))
import models   # noqa: E402
import run      # noqa: E402


def score(p, col):
    r = run.report(p, [col]).iloc[0]
    early = run.report(p[p.GW <= 10], [col]).iloc[0]
    return {'corr_cand': r['corr_cand'], 'top10/pos': r['top10/pos'], 'captain': r['captain'],
            'early top10': early['top10/pos'], 'early captain': early['captain']}


def main():
    f = run.load()
    p = f[f.season.isin(run.TEST)].copy()
    rows = []
    for ramp in (450, 900, 1800, 2700, 3600):
        p['x'] = models.bot_formula(p, ramp=ramp)
        rows.append({'ramp (min)': ramp, 'fixture_scale': 0.15, **score(p, 'x')})
    for fs in (0.0, 0.08, 0.25, 0.35):
        p['x'] = models.bot_formula(p, fixture_scale=fs)
        rows.append({'ramp (min)': 900, 'fixture_scale': fs, **score(p, 'x')})
    pd.set_option('display.float_format', '{:.3f}'.format)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == '__main__':
    main()
