"""Play whole past seasons with the bot's own decision code.

    python backtest/simulate.py

Each gameweek the live functions choose the squad (build_suggested_squad at
GW1, then optimise_transfers), the XI (pick_starting_xi) and the captain, from
the backtest's projections. The result is scored as FPL would: auto-subs,
vice-captain cover, selling prices, the five-transfer bank and -4 hits.

What it can't see, and the live bot can: FPL's chance_of_playing and status
flags, and FPL's own projection. Injured players only look worse once they
stop starting, so absolute totals run low. Comparisons between settings are
the point.
"""
import os
import sys
import warnings
from contextlib import redirect_stdout
from io import StringIO

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(__file__))
import data     # noqa: E402
import models   # noqa: E402
import run      # noqa: E402

bot = models.bot
POOL = {'score': 25, 'score_now': 10, 'cheap': 8}   # candidates per position for the solver


def _team_fixtures(season):
    fx = pd.read_csv(data._fetch(season, 'fixtures'),
                     usecols=['event', 'team_h', 'team_a', 'team_h_difficulty', 'team_a_difficulty'])
    fx = fx.dropna(subset=['event'])
    rows = ([(e, h, d) for e, h, d in zip(fx.event, fx.team_h, fx.team_h_difficulty)]
            + [(e, a, d) for e, a, d in zip(fx.event, fx.team_a, fx.team_a_difficulty)])
    return pd.DataFrame(rows, columns=['GW', 'team', 'difficulty']).astype(int)


def _ownership_view(tf, gw):
    """Per team: fixtures per week and weighted ease over the horizon, as get_data does."""
    weights = bot.FIXTURE_WEIGHTS[:bot.FIXTURE_HORIZON]
    teams = tf.team.unique()
    ease, count = {}, {}
    for t in teams:
        num = den = n = 0.0
        for off, w in enumerate(weights):
            d = tf.difficulty[(tf.team == t) & (tf.GW == gw + off)]
            if len(d):
                num += w * (3.0 - d).sum()
                den += w * len(d)
                n += len(d)
            else:                       # a blank counts as a hard fixture
                num += w * -1.0
                den += w
        ease[t], count[t] = num / den, n / bot.FIXTURE_HORIZON
    return ease, count


def season_weeks(f, season, sp_by_matches=True):
    """{gw: players_df in the shape the live code expects}, plus actual results."""
    s = f[f.season == season].copy()
    s = pd.concat([s, models.bot_formula(s, parts=True, sp_by_matches=sp_by_matches)], axis=1)
    tf = _team_fixtures(season)
    playing = tf.groupby('GW').team.apply(set).to_dict()
    known = {}              # latest row per player, carried through blank weeks
    weeks, actual = {}, {}
    for gw in range(1, 39):
        rows = s[s.GW == gw]
        for r in rows.itertuples():
            known[r.code] = r
        ease, count = _ownership_view(tf, gw)
        here = set(rows.code)
        recs = []
        for code, r in known.items():
            team = int(r.team_id)
            has_row = code in here
            # no row while his team played: he has left the league or the game
            gone = not has_row and team in playing.get(gw, set())
            base_run = r.bot_quality * r.bot_start_prob_season
            mult = min(1.2, max(0.8, 1 + ease.get(team, 0) * bot.FIXTURE_SCALE))
            recs.append({'id': int(code), 'web_name': r.web_name, 'element_type': int(r.element_type),
                         'team': team, 'now_cost': int(r.value), 'status': 'u' if gone else 'a',
                         'score_now': float(r.bot_pred) if has_row else 0.0,
                         'score': 0.0 if gone else base_run * count.get(team, 0) * mult})
        weeks[gw] = pd.DataFrame(recs)
        actual[gw] = dict(zip(rows.code.astype(int), zip(rows.target, rows.minutes)))
    return weeks, actual


def _pool(P, owned):
    """Owned players plus the plausible buys, so each solve takes ~2s not ~20s."""
    keep = set(owned)
    a = P[P.status == 'a']
    for _, g in a.groupby('element_type'):
        keep |= set(g.nlargest(POOL['score'], 'score').id)
        keep |= set(g.nlargest(POOL['score_now'], 'score_now').id)
        keep |= set(g[g.score > 0].nsmallest(POOL['cheap'], 'now_cost').id)
    return P[P.id.isin(keep)]


def _sell_price(buy, now):
    return now if now <= buy else buy + (now - buy) // 2


def _score_week(squad, res, bench_boost=False, cap_mult=2):
    """FPL scoring: XI + auto-subs, captain multiplied (vice if he didn't play).

    Bench Boost scores all fifteen, so no substitutions are needed.
    """
    starters, subs = bot.pick_starting_xi(squad)
    starters = starters.sort_values('score', ascending=False)
    cap, vice = starters.id.iloc[0], starters.id.iloc[1]
    pts = lambda i: res.get(i, (0, 0))[0]
    mins = lambda i: res.get(i, (0, 0))[1]

    xi = list(starters.id)
    if bench_boost:
        xi = list(squad.id)
    else:
        bench = list(subs[subs.element_type != 1].sort_values('score_now', ascending=False).id)
        bench_gk = list(subs[subs.element_type == 1].id)
        pos = dict(zip(squad.id, squad.element_type))
        for i in [i for i in xi if mins(i) == 0]:
            pool = bench_gk if pos[i] == 1 else bench
            for b in list(pool):
                trial = [x for x in xi if x != i] + [b]
                n = pd.Series([pos[x] for x in trial]).value_counts()
                if mins(b) > 0 and all(n.get(p, 0) >= lo for p, (lo, _) in bot.XI_FORMATION.items()):
                    xi, pool[:] = trial, [x for x in pool if x != b]
                    break
    captain = cap if mins(cap) > 0 else vice
    total = sum(pts(i) for i in xi) + (cap_mult - 1) * (pts(captain) if captain in xi else 0)
    return total, pts(captain)


# --- chips ---------------------------------------------------------------------
# Rules as of 2025-26: each chip twice, once per half, one chip per gameweek.
HALVES = [(1, 19), (20, 38)]
CHIPS = ('wc', 'fh', 'bb', 'tc')


def _week_value(squad):
    """This week's projected XI + captain, and bench, from score_now alone."""
    starters, subs = bot.pick_starting_xi(squad)
    v = starters.score_now.astype(float)
    return float(v.sum() + v.max()), float(subs.score_now.astype(float).sum())


def _chip_gains(P, squad_ids, plan_ids, plan_hits, budget, available):
    """Projected gain of each available chip this week, in points.

    TC: one more captain score. BB: the bench's score. FH: the best one-week
    squad's XI + captain over the planned squad's. WC: the full rebuild's
    value over this week and the next four, against the transfer plan's.
    """
    plan_sq = P[P.id.isin(plan_ids)]
    xi_val, bench_val = _week_value(plan_sq)
    starters, _ = bot.pick_starting_xi(plan_sq)
    gains, squads = {}, {}
    if 'tc' in available:
        gains['tc'] = float(starters.score_now.max())
    if 'bb' in available:
        gains['bb'] = bench_val
    if 'fh' in available:
        one_week = _pool(P, squad_ids).assign(score=lambda d: d.score_now)
        fh = bot.build_suggested_squad(one_week, budget)
        if fh is not None:
            squads['fh'] = set(fh.id)
            gains['fh'] = _week_value(P[P.id.isin(squads['fh'])])[0] - xi_val
    if 'wc' in available:
        wc = bot.build_suggested_squad(_pool(P, squad_ids), budget)
        if wc is not None:
            squads['wc'] = set(wc.id)
            gains['wc'] = (bot.squad_value(P[P.id.isin(squads['wc'])])
                           - (bot.squad_value(plan_sq) - plan_hits * bot.TRANSFER_HIT_COST))
    return gains, squads


def _choose_chip(gw, gains, thresholds, available):
    """A chip whose gain clears its threshold or, near the end of a half, the
    best remaining one, so no chip expires unused."""
    first, last = next(h for h in HALVES if h[0] <= gw <= h[1])
    if not gains:
        return None
    if last - gw + 1 <= len(available):
        return max(gains, key=gains.get)
    over = {c: g - thresholds[c] for c, g in gains.items()
            if thresholds.get(c) is not None and g >= thresholds[c]}
    return max(over, key=over.get) if over else None


def play(weeks, actual, optimiser=True, chips=None):
    """One season. `chips`: None for no chips, else {chip: threshold or None}
    (None = only when forced at the end of a half)."""
    squad_ids, bank, ft, buy = None, 0, 0, {}
    available = set()
    log = {'points': 0, 'hits': 0, 'transfers': 0, 'captain': 0}
    log.update({f'{c}_gw': [] for c in CHIPS})
    log.update({f'{c}_pts': 0 for c in CHIPS})
    for gw in range(1, 39):
        P = weeks[gw]
        if chips is not None and gw in (h[0] for h in HALVES):
            available = set(CHIPS)
        chip, week_ids = None, None
        with redirect_stdout(StringIO()):
            if squad_ids is None:
                sq = bot.build_suggested_squad(_pool(P, set()), 1000)
                squad_ids = set(sq.id)
                buy = dict(zip(sq.id, sq.now_cost))
                bank = 1000 - int(sq.now_cost.sum())
            else:
                ft = min(bot.MAX_FREE_TRANSFERS, ft + 1)
                mine = P[P.id.isin(squad_ids)]
                cost = dict(zip(P.id, P.now_cost))
                selling = {i: _sell_price(buy[i], cost[i]) for i in squad_ids}
                if optimiser:
                    plan = bot.optimise_transfers(mine, _pool(P, squad_ids), bank, ft, 0,
                                                  bot.TRANSFER_HIT_COST, selling)
                else:
                    real, bot.optimise_transfers = bot.optimise_transfers, lambda *a, **k: None
                    try:
                        plan = bot.plan_transfers(mine, P, bank, ft, 0,
                                                  bot.TRANSFER_HIT_COST, selling)
                    finally:
                        bot.optimise_transfers = real
                transfers = plan[0] if plan else []
                plan_ids = set(squad_ids)
                for out, inn, _, _ in transfers:
                    plan_ids = (plan_ids - {int(out.id.iat[0])}) | {int(inn.id.iat[0])}
                hits = max(0, len(transfers) - ft)

                if available:
                    budget = bank + sum(selling.values())
                    gains, squads = _chip_gains(P, squad_ids, plan_ids, hits, budget, available)
                    chip = _choose_chip(gw, gains, chips, available)
                    log.setdefault('gain_trace', []).append((gw, gains))

                if chip == 'fh':
                    # this week only: the squad reverts and free transfers are kept
                    week_ids = squads['fh']
                elif chip == 'wc':
                    new = squads['wc']
                    for o in squad_ids - new:
                        bank += selling[o]
                    for n in new - squad_ids:
                        bank -= cost[n]
                        buy[n] = cost[n]
                    squad_ids = new
                else:
                    for out, inn, _, _ in transfers:
                        o, n = int(out.id.iat[0]), int(inn.id.iat[0])
                        bank += selling[o] - cost[n]
                        buy[n] = cost[n]
                    squad_ids = plan_ids
                    ft = max(0, ft - len(transfers))
                    log['hits'] += hits
                    log['transfers'] += len(transfers)
                    log['points'] -= hits * bot.TRANSFER_HIT_COST
        week_sq = P[P.id.isin(week_ids or squad_ids)]
        week, cap = _score_week(week_sq, actual[gw], bench_boost=(chip == 'bb'),
                                cap_mult=3 if chip == 'tc' else 2)
        if chip:
            available.discard(chip)
            log[f'{chip}_gw'].append(gw)
            base, _ = _score_week(P[P.id.isin(squad_ids)], actual[gw])
            log[f'{chip}_pts'] += week - base    # what the chip added this week
        log['points'] += week
        log['captain'] += cap
    return log


def run_configs(seasons, configs):
    """configs: [(name, {CONSTANT: value}, use_optimiser, projections key[, chips])]"""
    rows = []
    for cfg in configs:
        name, consts, opt, proj = cfg[:4]
        chips = cfg[4] if len(cfg) > 4 else None
        saved = {k: getattr(bot, k) for k in consts}
        for k, v in consts.items():
            setattr(bot, k, v)
        try:
            for s, (weeks, actual) in seasons[proj].items():
                rows.append({'config': name, 'season': s, **play(weeks, actual, opt, chips)})
        finally:
            for k, v in saved.items():
                setattr(bot, k, v)
        done = pd.DataFrame(rows[-len(seasons[proj]):])
        print(f"{name:<44} {int(done.points.sum()):>6}  "
              + "  ".join(f"{r.season}:{r.points}" for r in done.itertuples())
              + f"  transfers {done.transfers.sum()}, hits {done.hits.sum()}"
              + ("  chips +" + "/".join(f"{c}:{int(done[f'{c}_pts'].sum())}" for c in CHIPS)
                 if chips is not None else ""), flush=True)
    return pd.DataFrame(rows)


def main():
    f = run.load()
    seasons = {fix: {s: season_weeks(f, s, fix) for s in run.TEST} for fix in (False, True)}
    run_configs(seasons, [
        ('old planner, live model', {}, False, False),
        ('optimiser, live model', {}, True, False),
        ('old planner, start-prob fix', {}, False, True),
        ('optimiser, start-prob fix', {}, True, True),
    ])


if __name__ == '__main__':
    main()
