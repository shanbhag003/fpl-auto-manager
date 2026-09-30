# Backtest

Replays past seasons gameweek by gameweek, using only what was known at each
deadline, to measure the player model and tune its constants.

```
pip install pandas numpy lightgbm
python backtest/run.py     # bot formula vs a learned model
python backtest/tune.py    # sweep the formula's hand-set constants
python backtest/simulate.py  # play whole seasons with the bot's own decision code
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

## Season simulator (`simulate.py`)

It plays 2023-24 to 2025-26 using the live bot's own functions:
`build_suggested_squad` at GW1, then `optimise_transfers` (or the old chained
planner), `pick_starting_xi` and its captain choice, fed with the backtest's
projections. Results are scored as FPL scores them: auto-subs, vice-captain
cover, selling prices at half the profit, the five-transfer bank and −4 hits.

It can't see FPL's injury and status flags, so totals run low. Compare
settings with each other, not with real managers.

Total points over the three seasons:

| Setup | Total | Per season |
|---|---|---|
| Old planner, live model (what ran GW1–5 of 2026-27) | 4,334 | 1,445 |
| Transfer optimiser, live model (PR #3) | 5,295 | 1,765 |
| Old planner + start-probability fix | 6,110 | 2,037 |
| **Transfer optimiser + start-probability fix** | **6,703** | **2,234** |

**The start-probability fix.** The live model's season start rate was
`starts / 34`, weighted by the player's own minutes:

- At GW10 an ever-present starter read as a 29% starter, below a bench
  player still carrying last season's rate.
- A player who stopped playing never moved off last season's rate, because his
  minutes stopped growing. In the 2024-25 replay the top "players to own" in
  GW10 included three who had left the league.

It's now starts per fixture the player was part of, weighted by the number of
those fixtures. On predictions alone: MAE 1.39 → 1.08, top-10 per position
4.16 → 4.24, captain 7.74 → 7.83.

**The optimiser's constants don't matter much.** With the fix in place,
sweeping `FT_ROLL_VALUE` (0.5–4), `HIT_MARGIN` (0–4, or no hits),
`MAX_HITS_PER_GW` (0–2) and `BENCH_WEIGHT` (0–0.3) gave 6,639–6,860 in
total, with no consistent direction. That's path-dependent noise, so the live
values (2.0, 2.0, 1, 0.1) stay.

## Not covered yet

Chips, which the bot doesn't play, and FPL's live projection (`ep_next`),
which this archive can't supply honestly.
