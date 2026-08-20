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
| `ANTHROPIC_API_KEY` | No | Enables the team news check. Costs about $0.05 a gameweek. |

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

1. **Code** tab → select everything in `lambda_function.py` → delete
2. Paste `fpl_bot_hybrid.py`
3. **Deploy**
4. **Test** with an empty event `{}`

Do not rename the file inside Lambda. The handler expects
`lambda_function.lambda_handler`.

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

`fpl_bot_hybrid_DRYRUN.py` is the same code with three differences: it ignores
the deadline check so it runs any time, it never submits anything to FPL, and it
emails the report so the poster can be checked. Paste it, Test, read the email,
then paste the live build back.

**Swap it back afterwards.** Left in place it would skip the deadline check and
submit nothing on deadline day.

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

All at the top of the file.

| Constant | Now | Meaning |
|---|---|---|
| `ACTION_WINDOW_HOURS` | 5 | How close to the deadline it decides |
| `MIN_TRANSFER_GAIN` | 1.5 | Points per gameweek needed to bother. A *difference* between two players. |
| `MAX_TRANSFERS_PER_GW` | 5 | Safety cap |
| `MAX_HITS_PER_GW` | 1 | Set to 0 to forbid point hits entirely |
| `BENCH_BOOST_THRESHOLD` | 26.0 | A total, so it scales with squad score |
| `TRIPLE_CAPTAIN_THRESHOLD` | 11.0 | Above a single-gameweek maximum, so it only fires in doubles |
| `FIXTURE_WEIGHTS` | `[1.0, .85, .7, .55, .4]` | Ownership horizon. `[1.0]` reverts to this gameweek only. |
| `LLM_TEAM_NEWS_ENABLED` | `False` | `False` reports team news without acting on it |

**Expect one recalibration.** These are tuned on pre-season data, where scores
come entirely from last season. Between GW1 and GW6 the weighting shifts to
FPL's own `ep_next`, which runs on a lower scale. The
`[plan] Stopping: best remaining swap gains X` line in each email tells you how
far off the threshold is.

---

## The two one-off scripts

`collect_preseason.py` and `collect_established.py` already ran. Their output is
frozen into the bot as `PRESEASON` and `PRESEASON_ESTABLISHED`. Keep them as a
record of where the numbers came from — don't put them in Lambda.

---

## Known limitations

- Tokens expire after 8 hours. Unavoidable without reverse-engineering the OAuth refresh flow.
- The FPL API is unofficial. No contract, no versioning — as the login removal showed.
- Advisory mode can't see your free transfer count, so it assumes one and never suggests a hit.
- The model scores averages, so it can't tell a reliable six from a volatile one.
- Price changes aren't modelled.
- Chips are recommended, never played. Deliberate.
