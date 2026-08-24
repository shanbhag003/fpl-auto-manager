# How It Works

The design decisions, what was measured, and what is still guesswork.

---

## 1. Scoring a player

Everything the bot does comes from one number per player per gameweek.

```
score = expected_points_per_fixture
        × fixtures_this_period
        × fixture_difficulty_multiplier
        + availability_penalty
```

### Expected points per fixture

The first version used FPL's own `ep_next`. That turned out to be a placeholder
before a season starts — about **24 distinct values across 570 players**, capped
at 4.0, so a backup goalkeeper and the best striker in the league scored the
same. Ranking on it did real damage: in one run the bot sold three of the
squad's best per-game players and bought a forward who had started three games
all season. The code ran perfectly. The thinking was wrong.

The replacement is a regression fitted on **874 player-season pairs** pulled
from FPL's own `history_past` endpoint, 2009 onwards:

```
expected_p90 = 2.047
             + 0.339 × points_per_90
             + 0.796 × xGI_per_90
             + 0.110 × ICT_per_90
```

Validated by fitting on the older half of the seasons and testing on the newer:

| Predictor | Out-of-sample R² |
|---|---|
| points per 90 alone | 0.302 |
| plus xGI and ICT | **0.368** |

xGI earns its weight because it regresses less than actual goals — a player who
created plenty of chances but finished badly is correctly rated above their raw
points.

### Multiplied by start probability

A great player who doesn't play scores nothing, so quality is multiplied by how
reliably they actually start. That figure comes from starts per season, with
pre-season minutes adjusting it.

This is the term that caught the earlier mistake. The three-start forward had
respectable per-90 numbers; what he lacked was minutes.

### The price fallback

148 of 508 available players had **zero Premier League history** — new signings
and promoted clubs. Scoring them zero would be its own bug, so price stands in,
calibrated per position on players who do have history:

| Position | Fit | r | n |
|---|---|---|---|
| GK | ppg ≈ 0.0745 × price − 0.21 | 0.82 | 19 |
| DEF | ppg ≈ 0.1056 × price − 2.00 | 0.73 | 98 |
| MID | ppg ≈ 0.0690 × price − 0.95 | 0.74 | 126 |
| FWD | ppg ≈ 0.0476 × price + 0.21 | 0.83 | 24 |

Price isn't a placeholder — the market has already priced in scouting, form and
league quality. It's a real signal, just a weaker one.

---

## 2. Frozen historical data

FPL zeroes every counter when a season starts. Before that happened, three
datasets were captured once and embedded in the code:

| Dataset | Players | Purpose |
|---|---|---|
| `LAST_SEASON` | 400 | Survives the reset; carries the model through GW1–10 |
| `PRESEASON` | 138 | Players new to the league — friendly starts and minutes |
| `PRESEASON_ESTABLISHED` | 250 | Everyone else's pre-season minutes |

They're priors, not answers. Each decays out as real minutes accumulate
(`weight = minutes / 900`), so by about ten games played a player is scored
entirely on the current season.

### Why the threshold is 300 minutes

The first version of the pre-season data made things **worse** — fringe players
topped the rankings, because someone with 90 minutes and one lucky return had a
per-90 rate that looked elite. Shrinkage was being applied to live data but not
to the snapshot.

A backtest on 1,001 player-season pairs gave a clean threshold:

| Prior-season minutes | Snapshot | Price | Winner |
|---|---|---|---|
| 1–300 | −0.083 | −0.005 | price |
| 300–900 | 0.634 | 0.400 | snapshot |
| 900–2000 | 0.593 | 0.316 | snapshot |
| 2000+ | 0.624 | 0.432 | snapshot |

Below 300 minutes, last season's rates predict *worse than nothing*. So the
snapshot earns weight only past 300, reaching full at 900.

### Why established players get only a quarter weight

Last season is roughly 3,000 minutes of competitive evidence. Pre-season is a
few hundred minutes of friendlies. It can nudge the pecking order, not rewrite
it.

And players with fewer than two friendlies are excluded from the table entirely
rather than penalised, because zero minutes is ambiguous. One striker had none —
he'd been at the World Cup until mid-July and was then rested. Another had none
because of a long-term injury. A third could be out of favour. The data cannot
tell them apart, so it doesn't try.

---

## 3. Two scores, one squad

The bot answers two questions on different horizons, and for a long time it used
one number for both. That was the deepest design flaw in the project.

```
score_run  = base × avg_fixtures over 5 GWs × ease(5 GWs)
             → who to own. Drives squad selection and transfers.

score_now  = base × fixtures this GW × ease(this GW)
             → who to start. Drives XI, captain, vice, bench order.
```

Transfers are the scarce resource. Knowing a good run starts in GW4 means buying
at GW2 with a spare transfer rather than being forced into a points hit later.
But next month's fixtures have nothing to do with whether to bench someone on
Saturday.

You can see the split working in the output: a defender rated fifth-best to own
can be second-best to start this week. Under a single score, one of those two
decisions was always going to be wrong.

### Fixture difficulty is multiplicative

An easy run should be worth more to a six-point player than to a two-point one.
The first version added a flat bonus and cheap players from weak teams with kind
fixtures flooded the squad — a constant is worth proportionally more to a low
scorer. It's now a multiplier, clamped to 0.86–1.09.

---

## 4. Choosing transfers

Two completely different strategies, selected by whether transfers currently
cost anything.

**Unlimited** (before the first deadline, or after a Wildcard or Free Hit): the
whole squad is re-optimised. With no cost per transfer there's no reason to
settle for one swap.

FPL signals this with `limit: null`. An early version read that as `limit or 0`
— turning *unlimited* into *none available* and making the bot demand an
impossible gain before acting.

**Limited**: swaps are chained while each clears its threshold.

| Situation | Bar |
|---|---|
| Within the free allowance | 1.5 points per gameweek |
| Beyond it | 5.5 points (the 4-point hit, plus a real gain on top) |

### Calibrating that threshold

`MIN_TRANSFER_GAIN` is a **difference between two players**, so appearance
points cancel out. 1.5 means 1.5 points of extra goals, assists and clean
sheets per gameweek.

An earlier version set it to 6.0, derived by scaling in proportion to total
squad score. That was wrong reasoning — a difference between two players doesn't
scale with a sum of fifteen. Against an already-good squad the best available
upgrades were:

```
DEF: best available 4.70 vs weakest owned 4.76   →  −0.06
MID: best available 7.37 vs weakest owned 4.67   →  +2.70
FWD: best available 7.13 vs weakest owned 3.62   →  +3.52
```

Maximum realistic gain is about 3.5. At 6.0 the bot would never have transferred
all season.

---

## 5. Squad optimisation

Picking 15 players is a constrained optimisation, not a ranking:

```
MAXIMIZE     Σ score[p] × pick[p]
SUBJECT TO   Σ cost[p] × pick[p] ≤ budget
             exactly 2 GK, 5 DEF, 5 MID, 3 FWD
             at most 3 players per club
WHERE        pick[p] ∈ {0, 1}
```

Roughly 570 binary variables, solved by CBC in about a second. Greedy selection
fails here because of the budget: spending everything on five premiums leaves
nothing for the other ten.

This is also why the most expensive striker in the game usually doesn't make the
squad. Forcing him in costs about **two points a week** — his price starves the
other fourteen slots. On the model's numbers that's correct, though see the
limitations below.

A pure-Python fallback exists for when the solver layer is missing: greedy seed,
single-swap local search, then paired swaps. The paired moves matter — single
swaps get stuck because no individual upgrade fits the remaining budget, so it
needs to downgrade one slot to fund another. Benchmarked within 1% of optimal.

---

## 6. Team news

The model only sees numbers. It cannot know that a manager has said a player
will be rested, that a signing hasn't featured in pre-season, or that someone is
being eased back. FPL's `status` field flags injuries and suspensions only — a
fit player who won't start reads as fully available.

So Claude, with web search, checks the squad and every incoming transfer, and
returns a minutes-risk per player.

Four constraints, all learned the hard way:

**It can only lower a rating, never raise one.** A wrong call costs at most one
good player. It can't talk the bot into a bad buy.

**Every claim is checked against the player's real record.** The layer once
reported that a 31-start, 177-point forward was "a new signing still building
match fitness" — and cited two real websites that had said no such thing. The
prompt now includes each player's actual record, and a code guard rejects
new-signing claims about anyone with 25+ starts last season.

**Risk is capped at 0.75**, so news alone can never fully bench a player. Only
a real injury flag does that.

**It fails safe.** Timeout, bad JSON, no API key, or a hallucinated player ID
not in the squad — all return an empty result and the bot proceeds exactly as it
would without the layer.

It also runs in **shadow mode** by default: it searches, reports in the email,
and changes nothing. Everything else in this project was validated against
historical data. This can't be — you'd need archived press conferences — so the
only validation available is watching it for a few gameweeks and deciding
whether you'd have agreed.

Cost is controlled by searching per club rather than per player (15 players span
about 8 clubs), a hard cap of three searches, a gate on deadline proximity, and
a cooldown. About $0.05 a gameweek.

---

## 7. Publishing the decisions

For most of this project the only output was an email to one person. That makes
a claim like "the model is calibrated" unfalsifiable by anyone else, so the bot
now publishes what it decided to a static site.

The architecture is deliberately dull: the bot writes JSON to a repository, and
a single HTML file reads it. No server, no database, no API.

```
bot (pre-deadline)  ──► data/projections/gw{n}.json   all ~570 players
                    ──► data/season.json              this gameweek's decision

results pass (6h)   ──► data/season.json              actual points, both squads

index.html          ◄── data/season.json              the whole site
```

### Why the projections are written before kickoff

This is the only part that actually matters. A projection published after the
results are known is not a projection.

So `data/projections/gw{n}.json` is committed in the same run that submits the
team, and it is **write-once** — if the bot runs again before the deadline,
`season.json` is overwritten but the projections file is not. The commit
timestamp in the repository is the evidence, and anyone can check it against the
deadline.

The same rule closes an awkward hole in the human comparison. The benchmark is a
second, hand-picked FPL entry, and its squad isn't public until after the
deadline — so scoring that squad against the model would ordinarily mean
computing its projection after kickoff. Freezing all 570 players rather than just
the fifteen the bot owns removes the problem entirely: whatever the human picked,
there is already a number for it from before the match.

### `finished` is not the same as `data_checked`

FPL exposes both. `finished` flips at the final whistle; `data_checked` flips
once bonus points and Opta corrections have settled, which can be hours later.

An early version of the results pass gated on `finished` and would have frozen
provisional bonus into the permanent record. It now gates on `data_checked` for
finality, and carries an in-flight gameweek as `live` — refreshed every six
hours, corrected as many times as needed, and only ever marked `final` once.

### What the bot decided vs what FPL did

Auto-substitutions are FPL's decision, not the model's. So a bench player who
came on keeps `role: "bench"` and gains `multiplier: 1`; a starter who was
subbed out keeps `role: "xi"` with `multiplier: 0`. The site badges them rather
than moving them.

Conflating the two would flatter the model — an auto-sub that rescued a blank
gameweek would be credited to a selection it never made.

### Zero is not the same as no data

The first live version showed `0` for every Chelsea player while Chelsea were
still to kick off. Fabricating a result is worse than showing nothing, and it
made a perfectly reasonable squad look like a disaster.

The fix reads the fixture list rather than trusting the absence of points, and
takes a belt-and-braces approach because the fixture flags themselves are
unreliable:

| Signal | Meaning |
|---|---|
| `finished` on a fixture | played and fully processed |
| `finished_provisional` | played, awaiting processing — also counts as played |
| `?event=` filter | not always honoured; the event is re-checked in code |
| player has minutes | has played, whatever the fixture flags say |

That last row is the one that makes it robust. A player with 90 minutes on the
board cannot be marked as yet to play, no matter what the fixture endpoint
returns.

### The bar chart was measuring the wrong thing

Each player card carries a small bar. The first version encoded **prediction
error** — length was how far the model missed, colour was the direction.

It failed immediately in use. A striker who was projected 5.0 and scored 0, and a
defender projected 4.4 who scored 10, drew almost identical bars, because both
missed by about five points. Only the colour distinguished a disaster from the
best return of the week.

It now encodes **points scored**, on a scale shared across the squad, with a pale
tick marking where the prediction sat. Ten points is visibly twice five, and the
error is still legible as the gap between bar and tick. Same information,
readable in a glance instead of requiring a legend.

A recurring theme in this project: the arithmetic was right both times, and the
choice of what to display was the thing that was wrong.

### Gameweek 1 is a partial record

The publishing layer was built after GW1 had already been played. The bot's GW1
projections were computed, printed to CloudWatch, rendered onto the emailed
poster, and then discarded.

Recomputing them afterwards was rejected — the inputs have moved and the results
are known, so the output would be a fitted number wearing a prediction's
clothes. They were instead transcribed by hand from the poster, which
demonstrably predates kickoff, and the projections file for GW1 is flagged
`partial: true` because it covers fifteen players rather than all 570.

The consequence is that GW1 can be compared at squad level but not at player
level, and the hand-picked team has no GW1 projection at all. The site says so
rather than hiding it.

---

## 8. Degrading instead of failing

FPL removed the login endpoint this project was built on. Not deprecated —
the hostname stopped resolving, and authentication moved to an OAuth2 provider
with no email-and-password equivalent.

The response was to change what failure means:

| Situation | Behaviour |
|---|---|
| Token valid | Submits transfers and lineup, emails what it did |
| Token expired | Same decisions, emailed to apply by hand |
| FPL unreachable | Four retries with backoff, then waits for the next run |
| Optimiser layer missing | Pure-Python fallback, ~1% off optimal, logged |
| Image layer missing | Email sends without the poster |
| SSM write fails | Logs loudly; never crashes after a live submission |
| GitHub write fails | Logs; the site goes a gameweek stale, nothing else |

Two outcomes at the auth fork, and neither is a failure.

The publishing layer follows the same rule and is the lowest-priority thing in
the system. It runs after submission, catches everything, and returns quietly. A
GitHub outage costs a chart, never a gameweek. Advisory-mode runs still publish,
tagged `mode: "advisory"`, so the history stays complete even in the weeks the
token had expired.

### Things only a live write revealed

Three bugs were invisible to any dry run, because a dry run never posts:

- **Bench slot 12 is reserved for the substitute goalkeeper.** Sorting all four
  substitutes by score puts a keeper at 13–15 whenever they aren't the best sub,
  and FPL rejects the lineup.
- **HTTP 202 means Accepted.** An early version whitelisted 200, 201 and 204, so
  it reported a successful submission as a rejection.
- **`player_row.diff` returns a pandas method**, not the column — `diff` is a
  real Series method. `player_row['diff']` is the fix.

---

## 9. What isn't solved

**Volatility.** The model scores averages, so it cannot distinguish a reliable
six from a volatile one — which is exactly the difference that wins a gameweek.
Captaincy makes it worse: a captained explosive player doubles a high-variance
score, and the model can't see that.

**Coefficient mismatch.** The regression was fitted to predict *next season*
from *last season*, but is applied to predict *this gameweek* from *this
season's rates*. Probably in the right ballpark, not calibrated for the job.
The published projections now make this measurable rather than theoretical — a
few gameweeks of residuals will show whether it is bias or variance.

**`ep_next` is capped at 50%.** FPL's own projection may well be good in-season,
and may use inputs unavailable here — but FPL doesn't publish historical values,
so it can't be backtested the way the rest of the model was. With two plausible
estimators and no way to rank them, combining beats choosing. The cap is a
judgement call, not a measured optimum.

**Price changes.** Not modelled at all.

**A benchmark of one.** The comparison is against a single hand-picked entry over
a single season. That is an anecdote, not evidence — 38 gameweeks against one
opponent cannot separate a better model from better luck. Overall rank is the
more honest number, and it is on the page for that reason.

**Token expiry.** Eight hours, refreshed by hand on deadline day. Removing this
means reverse-engineering the OAuth refresh flow.
