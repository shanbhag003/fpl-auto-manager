# FPL Bot — Setup

Everything needed to get the bot running, in order. Roughly 20 minutes from
scratch, or 5 minutes if you already have the function and PuLP layer in place.

---

## 1. The Lambda function

Runtime **Python 3.13**. Handler stays at the default `lambda_function.lambda_handler`.

| Setting | Value | Where |
|---|---|---|
| Timeout | 2 min or more | Configuration → General configuration |
| Memory | 256 MB | same |

The bot's own runs take about 5–15 seconds. The headroom is for cold starts and
retries when FPL is slow.

---

## 2. Layers

Three are needed. Create each under **Lambda → Layers → Create layer**, then
attach them to the function from **Code → Layers → Add a layer → Custom layers**.

| Layer | Zip | What breaks without it |
|---|---|---|
| requests + pandas | you already have this | Everything. The bot won't start. |
| `pulp-layer.zip` | included | Squad optimisation falls back to a pure-Python version, ~1% off optimal |
| `pillow-layer.zip` | included | No squad poster. The email still sends. |

For both included zips: compatible runtime **Python 3.13**, architectures
**x86_64** and **arm64**.

`pillow-layer.zip` also carries the fonts (Space Grotesk, Inter, JetBrains
Mono), because Lambda has none of its own.

---

## 3. Environment variables

**Configuration → Environment variables → Edit.**

| Variable | Required | Notes |
|---|---|---|
| `FPL_TEAM_ID` | **Yes** | Your entry ID, from the URL of your points page |
| `FPL_TOKEN` | No | Bearer token. Without it the bot runs in advisory mode. |
| `SMTP_EMAIL` | No | Gmail address that sends the report |
| `SMTP_APP_PASSWORD` | No | A Gmail **App Password**, not your account password |
| `NOTIFY_EMAIL` | No | Where the report lands |
| `GEMINI_API_KEY` | No | Enables the team news check, free. From aistudio.google.com → Get API key (new keys start `AQ.`). |
| `ANTHROPIC_API_KEY` | No | Paid fallback for team news (~$0.05 a gameweek), used only when `GEMINI_API_KEY` isn't set. |
| `GITHUB_REPO` | No | `owner/repo` the bot publishes the site data to |
| `GITHUB_TOKEN` | No | Token with contents write on that repo; without it publishing is skipped |
| `HUMAN_ENTRY_ID` | No | The hand-picked team the site compares against |

**Use the console for these.** `aws lambda update-function-configuration
--environment` replaces the whole set, which would wipe the token and email
settings. And don't screenshot this page with the values showing.

Delete `FPL_EMAIL` and `FPL_PASSWORD` if they're still there — the endpoint they
were sent to no longer exists.

---

## 4. IAM permissions

The execution role needs SSM access across the whole `/fpl-bot/` path, not one
parameter. **Configuration → Permissions → click the role → edit the policy:**

```json
{
  "Effect": "Allow",
  "Action": ["ssm:GetParameter", "ssm:PutParameter"],
  "Resource": "arn:aws:ssm:ap-south-1:YOUR_ACCOUNT_ID:parameter/fpl-bot/*"
}
```

Four parameters get written there: the last processed gameweek, the last squad
fingerprint, the last outage email, and the last team-news check. A policy
scoped to a single name causes `AccessDeniedException` on the others — not
fatal, but the duplicate-suppression silently stops working.

---

## 5. Schedule

EventBridge, **`rate(2 hours)`**.

The bot only acts inside `ACTION_WINDOW_HOURS` (currently 5) of a deadline, so
most runs print one line and exit. Twelve runs a day is about 360 a month
against a 1,000,000 free tier.

If you narrow the action window below 5 hours, move to `rate(1 hour)` first —
otherwise there are too few attempts left if FPL happens to be down.

---

## 6. Deploy

Don't paste code into the console. Merge the change into `main` and the
**Deploy Lambda** GitHub workflow puts it on the function — see
[LAMBDA_DEPLOY.md](LAMBDA_DEPLOY.md) for the one-time AWS setup. A console edit
makes the next deploy stop rather than overwrite it; run **Lambda snapshot** to
bring it back into the repo first.

The handler stays `lambda_function.lambda_handler`; the workflow reads it from
the function and replaces that file only. **Test** with an empty event `{}`.

Expected output away from a deadline:

```
Next deadline: GW2 in 106.8h (bot acts inside 5h).
[auth] Token valid for another 7.4h.
Deadline Too Far Away
```

---

## 7. The token, on deadline day

This is the one recurring manual step. Tokens last **8 hours**.

1. Log in at fantasy.premierleague.com in Chrome
2. **F12** → **Network** tab → filter `api` → refresh the page
3. Right-click any `fantasy.premierleague.com` request → **Copy** → **Copy as cURL**
4. Find `-H 'X-API-Authorization: Bearer ey...'` and copy everything after `Bearer `
5. Paste into `FPL_TOKEN` → Save

Do it a few hours before the deadline, not at lunchtime. Closing the browser tab
doesn't affect the token; logging out of FPL kills it.

Forget, and nothing breaks — you get an email with the same decisions to apply
by hand, subject line `ACTION NEEDED`.

---

## Testing before a deadline

Locally, with nothing submitted:

- `run_bot(team_id, test_mode=True)` runs the whole gameweek — transfers, lineup,
  chip decision — and submits, emails and saves nothing. Away from a deadline it
  stops at "Deadline Too Far Away"; patch `check_update` to return
  `(True, <gw>)` to force it.
- `tools/check_news.py` runs just the team-news check on the squad and prints
  the headlines found and each risk. Needs `GEMINI_API_KEY` in the environment.

`fpl_bot_hybrid_DRYRUN.py` is an old hand-made copy of the bot and has fallen
behind it. Don't deploy it.

---

## Reading the logs

| Log line | Meaning | Action |
|---|---|---|
| `Deadline Too Far Away` | Working normally | None |
| `[auth] Token valid for another X.Xh` | Automation active | None |
| `[auth] Token rejected (HTTP 403)` | Token expired | Paste a fresh one |
| `[get] ... HTTP 403` | Cloudflare blocked the Lambda IP | Usually clears on retry |
| `[get] ... HTTP 5xx` | FPL is down | None; next run picks it up |
| `[plan] Stopping: ...` | Normal — shows the swap it rejected | Use it to judge the threshold |
| `[optimizer:fallback]` | PuLP layer missing | Attach it for the exact optimum |
| `[poster] Pillow not available` | Pillow layer missing | Attach it for the image |
| `Could not save squad signature` | IAM too narrow | Widen to `parameter/fpl-bot/*` |
| `... was REJECTED by FPL` | A real refusal, with FPL's reason | Read it — usually club limit or budget |

---

## Tuning

All at the top of their sections in `fpl_bot_hybrid.py`. The backtest in
[`backtest/`](backtest/) is where to check a change before making it.

| Constant | Now | Meaning |
|---|---|---|
| `ACTION_WINDOW_HOURS` | 5 | How close to the deadline it decides |
| `FT_ROLL_VALUE` | 2.0 | Points a saved free transfer is worth; also the bar a free transfer must clear over the horizon |
| `HIT_MARGIN` | 2.0 | Charged on top of each −4 hit |
| `MAX_HITS_PER_GW` | 1 | Set to 0 to forbid point hits entirely |
| `BENCH_WEIGHT` | 0.1 | Share of a bench player's score that counts when choosing the squad |
| `FIXTURE_WEIGHTS` | `[1.0, .85, .7, .55, .4]` | Ownership horizon. `[1.0]` reverts to this gameweek only. |
| `AUTO_PLAY_CHIPS` | `True` | `False` stops chips being played (advisory emails still recommend) |
| `CHIP_THRESHOLDS` | TC 9, BB 13.3, FH 6, WC 13.5 | Projected gain at which each chip is played |
| `LLM_TEAM_NEWS_ENABLED` | `True` | `False` reports team news without acting on it |
| `GEMINI_MODELS` | 3.5-flash, 3.5-flash-lite, 3.1-flash-lite, 3.6-flash | Tried in order; retired, out-of-quota or busy models pass to the next |

`MIN_TRANSFER_GAIN` only matters when PuLP is missing and the chained fallback
planner runs.

---

## The two one-off scripts

`collect_preseason.py` and `collect_established.py` already ran. Their output is
frozen into the bot as `PRESEASON` and `PRESEASON_ESTABLISHED`. Keep them as a
record of where the numbers came from — don't put them in Lambda.

---

## Known limitations

- Tokens expire after 8 hours. Unavoidable without reverse-engineering the OAuth refresh flow.
- The FPL API is unofficial. No contract, no versioning — as the login removal showed.
- Advisory mode can't see your free transfer count, so it assumes one.
- The model scores averages, so it can't tell a reliable six from a volatile one.
- Price changes aren't modelled.
- Chip thresholds were tuned in a simulator that can't see FPL's injury flags; live projections run on a similar but not identical scale.
