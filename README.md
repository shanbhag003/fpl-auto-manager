# FPL Bot

An autonomous Fantasy Premier League manager. It scores every player in the
game, decides transfers, picks the starting XI and captain, submits all of it,
emails an explanation of why — and publishes every prediction before kickoff so
the model can be marked in public.

It runs unattended on AWS Lambda. When it can't sign in to FPL, it doesn't
fail — it emails the same decisions for you to apply by hand.

**→ [Live results](https://shanbhag003.github.io/fpl-auto-manager/)**

<p align="center">
  <img src="docs/poster.png" width="480" alt="Generated squad poster">
</p>

---

## What it does

Every two hours it wakes up, checks how close the deadline is, and goes back to
sleep unless it's within five hours — late enough that the Friday press
conferences are already out.

When it does act:

1. Rates all ~570 players on expected points per fixture
2. Checks current team news for anything the data can't show
3. Plans transfers, chaining up to five while each clears a threshold
4. Picks the best legal XI, captain, vice-captain and bench order
5. Flags any chip worth considering — never plays one
6. Submits everything, then emails the reasoning with a shareable squad image
7. Commits its projection for every player in the game, before a ball is kicked

A second job runs every six hours and fills in what actually happened, so the
site shows live scores during a gameweek rather than waiting for it to end.

---

## The interesting parts

**Two scores, not one.** Who you *own* is a five-gameweek question, because
transfers are scarce and a player is bought for a run of fixtures. Who *starts*
is a this-week question. A single number can't answer both — weight it forward
and captaincy suffers, weight it on this week and transfers become
short-sighted. So the bot computes both.

**The model is backtested, not assumed.** Player scoring is a regression on
points, expected goal involvement and ICT per 90 minutes, fitted on 874
player-season pairs from 2009 onwards. Out-of-sample R² against next-season
output went from 0.302 to 0.368 against a points-only baseline.

**The predictions are published before kickoff, and can't be edited after.**
Every gameweek, a projection for all ~570 players is committed to this
repository before the deadline, and the file is never rewritten. The commit
timestamp is the proof. That's what makes the comparison on the site mean
anything — including against a hand-picked human team whose squad isn't even
public until after the deadline.

**It degrades instead of failing.** FPL retired the login endpoint this project
depended on, mid-build. Rather than patch around it, the system was rebuilt so
that being unable to sign in produces a different outcome, not a failed one.
Same for the optimiser: no linear programming layer means a pure-Python
fallback within about 1% of optimal, with a line in the log saying so. Same for
publishing: a GitHub outage costs a chart, never a gameweek.

**The LLM only ever lowers a rating.** Team news — rotation, a manager resting
someone, a signing short of match fitness — is read by Claude with web search.
It can reduce a player's projection, never raise it, so a wrong call costs at
most one player rather than talking the bot into a bad buy. It runs in shadow
mode until its judgement has been checked against a few gameweeks.

---

## Stack

Python · pandas · PuLP (linear programming) · Pillow · AWS Lambda ·
EventBridge · SSM Parameter Store · Claude API with web search ·
GitHub Pages · GitHub Actions

The front end is one HTML file with no build step and no dependencies beyond
three web fonts. It reads a single JSON file.

---

## Setup

See [SETUP.md](SETUP.md) — layers, environment variables, IAM, schedule, and
the one recurring manual step. [DEPLOY.md](DEPLOY.md) covers the publishing
side: the GitHub token, the results job, and Pages.

Short version: create three Lambda layers, set `FPL_TEAM_ID`, paste
`fpl_bot_hybrid.py` into the function, and point EventBridge at it.

---

## Repository

```
fpl_bot_hybrid.py          the bot
fpl_bot_hybrid_DRYRUN.py   same code, submits nothing — for testing
fpl_results.py             fills in actual points; second Lambda, every 6h
index.html                 the public site, served by GitHub Pages
data/season.json           every gameweek: squads, projections, results
data/projections/          per-gameweek projections, write-once
SETUP.md                   deployment guide for the bot
DEPLOY.md                  deployment guide for the site
SCHEMA.md                  the JSON contract between them
HOW_IT_WORKS.md            the modelling and design decisions
collect_preseason.py       one-off data collection, already run
collect_established.py     one-off data collection, already run
seed_gw1.py                one-off, already run — see below
layers/                    prebuilt Lambda layers
```

`seed_gw1.py` exists because the publishing layer was built after GW1 had been
played. It transcribes that gameweek's projections from the emailed poster,
which predates kickoff, rather than recomputing them from data that now includes
the results. It is hardcoded to GW1 and should not be run again.

---

## What it deliberately doesn't do

- **Play chips.** Wildcard, Free Hit, Bench Boost and Triple Captain are
  one-shot decisions worth too much to hand to a threshold. The bot flags them.
- **Model price changes.** Not attempted.
- **Value volatility.** It scores averages, so it can't distinguish a reliable
  six from an explosive one — which is exactly the difference that wins a
  gameweek. This is the largest known gap.
- **Plan beyond five gameweeks.** The horizon is a tunable constant.
- **Rewrite history.** A published projection is never edited, and a gameweek
  marked final is never touched again.

---

## A note on the FPL API

It's unofficial. No contract, no versioning, no notice of change — as the login
removal demonstrated mid-project. Every call goes through a wrapper that checks
the status code and content type before parsing, retries with backoff, and logs
what actually came back rather than a `JSONDecodeError` pointing at character
zero.

Two flags in particular are worth knowing about. `finished` on a gameweek flips
at the final whistle, but bonus points and stat corrections land afterwards —
`data_checked` is the one that means settled. And on individual fixtures,
`finished_provisional` covers the window between the whistle and processing, so
treating only `finished` as played will tell you a match that ended hours ago
hasn't been played yet.

---

## Licence

MIT. Not affiliated with the Premier League or Fantasy Premier League.
