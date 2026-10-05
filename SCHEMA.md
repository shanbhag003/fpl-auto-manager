# Data schema

The contract between the Python side and the site. Everything the site shows
comes from these files in `data/`. Change a field's meaning and `index.html`
breaks; add a field and nothing does, because readers ignore keys they don't
know.

| File | Written by | When | Rewritten? |
|---|---|---|---|
| `season.json` | bot (decision) + `fpl_results.py` (results) | each decision; every 6h | yes |
| `projections/gw{n}.json` | bot | the decision run before GW *n*'s deadline | **never** |
| `players.json` | `fpl_results.py` | every 6h, only if something changed | yes |

Costs and prices are FPL's `now_cost`: tenths of £m (`55` = £5.5m). Player
`id`s are FPL element ids, which are stable within a season.

---

## `projections/gw{n}.json` — frozen before kickoff

```json
{
  "gw": 6,
  "written_at": "2026-10-10T05:12:44+00:00",
  "scores": { "426": [5.83, 6.10], "...": [] },
  "fpl":    { "426": [6.0, null],  "...": [] }
}
```

| Key | Meaning |
|---|---|
| `scores[id]` | `[score_now, score_run]`. `score_now` is this gameweek's projection, which picks the XI and captain. `score_run` is the five-gameweek ownership score per gameweek, used for transfers. |
| `fpl[id]` | `[ep_next, chance_of_playing_next_round]`, as FPL published them before this deadline. From GW6. |
| `partial` | `true` only in GW1, which was transcribed from the emailed poster and covers the bot's 15 players. |

Write-once: if the file exists, the bot doesn't touch it, so the commit
timestamp proves the numbers predate kickoff.

Scores can be negative. A player FPL lists as injured, suspended or gone
carries a −50 penalty in `score_now`, and in `score_run` when he's out for the
whole horizon. Display them as 0, not as a projection.

---

## `players.json` — metadata for the squad lookup

```json
{ "updated": "2026-10-05T14:35:35+00:00",
  "players": { "426": ["B.Fernandes", "MID", "MUN", 119], "...": [] } }
```

`[web_name, position, club short name, now_cost]`. Position is `GK`, `DEF`,
`MID` or `FWD`. Committed only when a value changes, so `updated` is the time
of the last change rather than of the last run.

---

## `season.json` — the season so far

### Top level

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | `1` |
| `season` | str | `"2026/27"` |
| `generated_at` | ISO time | last time the file changed |
| `instrumented_from_gw` | int | first gameweek with projections |
| `entries.bot`, `entries.human` | `{id, name, manager}` | the two FPL entries compared |
| `totals.bot`, `totals.human` | `{points, overall_rank}` | season totals |
| `refresh_interval_hours` | float | results-pass schedule, for "next update" |
| `next_deadline` | `{gw, time}` or null | the next gameweek to lock |
| `live_gw` | int or null | gameweek currently `live` |
| `gameweeks` | array | one record per gameweek, below |

### A gameweek

| Key | Meaning |
|---|---|
| `gw`, `deadline`, `decided_at` | gameweek, its deadline, when the bot decided |
| `status` | `"live"` (scores provisional, refreshed) or `"final"` (FPL's `data_checked`; never touched again) |
| `instrumented` | `true` if projections were recorded for it |
| `mode` | `"automated"` (submitted by the bot) or `"advisory"` (emailed; applied by hand) |
| `bot`, `human` | a squad record, below (`human` has no `formation`/`bank`/`value`/`vice`) |
| `transfers` | `[{out, in, gain, free, hit_cost, reason}]`, where `out`/`in` are `{id, name, cost, pos}` |
| `no_transfer_reason` | why nothing was bought, when `transfers` is empty |
| `news` | `[{id, name, risk, reason, applied}]`: team-news flags, `risk` 0–0.75 |
| `chips_flagged` | `[{name, reason}]`: chips played or recommended that week |

### A squad record

| Key | Meaning |
|---|---|
| `squad` | 15 players, below |
| `captain`, `vice` | player ids |
| `formation`, `bank`, `value` | e.g. `"4-4-2"`, tenths of £m |
| `projected` | `{xi, captain_bonus, total}`, from the frozen projections |
| `actual` | `{total, bench, hits, gw_rank, overall_rank}`, from FPL, null until scored |

### A player in a squad

| Key | Meaning |
|---|---|
| `id`, `name`, `pos`, `team`, `cost` | identity; `team` is the club short name |
| `role` | `"xi"` or `"bench"`, as the bot picked them |
| `bench_order` | 1–4 for the bench, else null |
| `multiplier` | FPL's: 2 captain (3 with Triple Captain), 1 played, 0 benched or subbed out. Auto-subs change this, not `role`. |
| `projected_now`, `projected_run` | from the frozen projections |
| `actual`, `minutes` | FPL's, null until his fixture is played |
| `pending` | `true` while his club is still to play this gameweek |
| `note` | a one-line explanation, or null |
