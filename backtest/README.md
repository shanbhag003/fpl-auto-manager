# Backtest

Replays past seasons gameweek by gameweek, using only what was known at each
deadline, to measure the player model and tune its constants.

```
pip install pandas numpy lightgbm
python backtest/run.py     # bot formula vs a learned model
python backtest/tune.py    # sweep the formula's hand-set constants
```

Data comes from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League)
and is downloaded once into `backtest/cache/` (git-ignored). Seasons 2022-23
onward have xG and starts. 2021-22 is used only as the "last season" input,
and the tests run on 2023-24, 2024-25 and 2025-26.

## Findings (30 Sep 2026)

**The archive's `xP` is recorded after the matches.** With everything known at
the deadline held fixed, xP for a gameweek still moves with that gameweek's
actual points (regression coefficient 0.28–0.37) but not with the next
gameweek's (~0.01). A model given it captained players averaging 12–14 points
a week, which isn't achievable live. It's excluded. That also means the bot's
`ep_next` blend, which is legitimate live, can't be backtested with this data.

**The bot's formula beats a learned model where it matters.**
All three test seasons, per player-gameweek:

| Model | MAE | Ranking corr.* | Top-10 per position | Captain |
|---|---|---|---|---|
| Bot formula (no `ep_next`) | 1.39 | 0.380 | **4.16** | **7.74** |
| LightGBM | 0.99 | 0.390 | 4.06 | 6.89 |
| LightGBM + formula as features, Poisson | 0.98 | 0.404 | 4.13 | 6.61 |
| Two-stage (P(plays) × points if plays) | 0.99 | 0.387 | 4.07 | 6.47 |

\*Correlation among players who started recently. "Top-10 per position" is
the average actual points of the model's ten best-rated players in each
position each week. "Captain" is the actual points of its single top pick.

The learned models are better calibrated but pull elite players toward the
mean, which costs about a point a week in captaincy. Machine learning isn't
justified on this evidence.

**The formula's constants are near their best.** Varying how fast this
season's rates replace last season's (450–3,600 minutes; currently 900) or the
fixture-difficulty scale (0–0.35; currently 0.15) moves the metrics by less
than the week-to-week noise. Removing fixture difficulty altogether is clearly
worse (top-10 falls to 4.02).

## Not covered yet

The decision layer: transfers, hits, the bench and chips over a whole season.
That needs a season simulator, and it's what `FT_ROLL_VALUE`, `HIT_MARGIN` and
`BENCH_WEIGHT` should be tuned against.
