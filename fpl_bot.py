import requests
import json
import os
import time
import traceback
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import base64

# ---- Tunable constants ----
TRANSFER_HIT_COST = 4          # points lost per transfer beyond the free allowance
MIN_TRANSFER_GAIN = 1.5        # minimum projected-points gain (per gameweek) to bother transferring.
                               # This is a DIFFERENCE between two players, so appearance points
                               # cancel out — 1.5 means 1.5 pts of extra goals/assists/clean sheets.
                               # For scale: best realistic swap from a good squad gains ~3.5.
TOP_N_CANDIDATES = 3           # shortlist size before any randomness
TRANSFER_OUT_ATTEMPTS = 4      # outgoing players to try before abandoning a swap
SSM_PARAM_NAME = "/fpl-bot/last-processed-gameweek"
UNAVAILABLE_STATUSES = {'i', 's', 'u', 'n'}          # injured / suspended / unavailable / not in squad
STATUS_PENALTY = {'a': 0, 'd': -3, 'i': -50, 's': -50, 'u': -50, 'n': -50}
STATUS_LABELS = {'a': 'Available', 'd': 'Doubtful', 'i': 'Injured', 's': 'Suspended',
                  'u': 'Unavailable', 'n': 'Not in squad'}

# --- Chip recommendation thresholds (info-only, nothing is auto-played) ---
BENCH_BOOST_THRESHOLD = 26.0       # combined bench score above which Bench Boost is worth flagging
                                   # (a strong bench is ~17 in a single GW, ~34 in a double)
TRIPLE_CAPTAIN_THRESHOLD = 11.0    # captain score above which Triple Captain is worth flagging
                                   # (best captain is ~7.4 single GW, ~14 in a double)
FREE_HIT_BLANK_THRESHOLD = 3       # squad players with 0 fixtures this week before flagging Free Hit
WILDCARD_UNAVAILABLE_THRESHOLD = 3  # squad players injured/suspended/doubtful before flagging Wildcard


# --- Network settings -------------------------------------------------------
# FPL sits behind Cloudflare. A bare python-requests call (no User-Agent) from an
# AWS datacenter IP gets served an HTML block page instead of JSON, which is what
# caused "Expecting value: line 1 column 1 (char 0)".
BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://fantasy.premierleague.com/',
    'X-Requested-With': 'XMLHttpRequest',
}
REQUEST_TIMEOUT = 20       # seconds per attempt
MAX_RETRIES = 4            # attempts before giving up
BACKOFF_BASE = 2           # seconds: 1, 2, 4, 8



def _as_utc(dt):
    """Treat a naive timestamp as UTC.

    Values written by earlier versions of this bot used datetime.utcnow(),
    which produced naive strings. Comparing those against an aware datetime
    raises, so they are tagged rather than reparsed.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FPLUnavailable(Exception):
    """Raised when the FPL API is unreachable, blocked, or mid-update."""


def get(url, retries=MAX_RETRIES):
    """GET a FPL endpoint and return parsed JSON.

    Unlike a bare json.loads(response.content), this checks the status code and
    content type first, retries transient failures with backoff, and logs the
    start of the response body so CloudWatch shows WHY it failed.
    """
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                if 'json' in content_type.lower():
                    return response.json()

                # 200 but HTML = maintenance page or Cloudflare interstitial
                last_error = f"200 but Content-Type was '{content_type}' (not JSON)"
                print(f"[get] {url} -> {last_error}. Body starts: {response.text[:200]!r}")

            elif response.status_code in (403, 429):
                last_error = f"HTTP {response.status_code} — blocked or rate-limited by Cloudflare"
                print(f"[get] {url} -> {last_error}. Body starts: {response.text[:200]!r}")

            elif response.status_code >= 500:
                last_error = f"HTTP {response.status_code} — FPL server error (likely mid-update)"
                print(f"[get] {url} -> {last_error}")

            else:
                last_error = f"HTTP {response.status_code}"
                print(f"[get] {url} -> {last_error}. Body starts: {response.text[:200]!r}")

        except requests.Timeout:
            last_error = f"timed out after {REQUEST_TIMEOUT}s"
            print(f"[get] {url} -> {last_error}")
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"[get] {url} -> {last_error}")
        except ValueError as e:
            # response.json() failed even though Content-Type claimed JSON
            last_error = f"malformed JSON: {e}"
            print(f"[get] {url} -> {last_error}")

        if attempt < retries - 1:
            wait = BACKOFF_BASE ** attempt
            print(f"[get] retrying in {wait}s ({attempt + 2}/{retries})")
            time.sleep(wait)

    raise FPLUnavailable(
        f"Could not get JSON from {url} after {retries} attempts. Last problem: {last_error}. "
        "Usually this means Cloudflare blocked the Lambda IP, or FPL is mid-gameweek-update. "
        "Nothing was changed on your team."
    )


# How close to the deadline the bot acts. The FIRST run inside this window
# makes the decision and marks the gameweek done, so this effectively sets
# "how late do we decide".
#
# Why not 24 hours: a Friday 17:30 UTC deadline covers matches through Sunday
# and Monday, and those managers hold their press conferences on Friday morning
# (roughly 08:00-13:00 UTC). Acting at T-24h means deciding on Thursday evening
# and missing every one of them.
#
# The trade-off is retries. At T-5h with an hourly schedule there are about five
# attempts; on a 2-hourly schedule only two or three. Run EventBridge hourly if
# you narrow this further.
ACTION_WINDOW_HOURS = 5


def check_update(events_df):
    now_ts = datetime.now().timestamp()
    upcoming = events_df.loc[events_df.deadline_time_epoch > now_ts]
    if upcoming.empty:
        return False, None

    deadline = upcoming.iloc[0].deadline_time_epoch
    gameweek = upcoming.iloc[0].id
    hours_away = (deadline - now_ts) / 3600

    within = hours_away <= ACTION_WINDOW_HOURS
    if not within:
        print(f"Next deadline: GW{gameweek} in {hours_away:.1f}h "
              f"(bot acts inside {ACTION_WINDOW_HOURS}h).")
    else:
        print(f"GW{gameweek} deadline is {hours_away:.1f}h away — inside the action window.")
    return within, gameweek


# --- Idempotency: remember which gameweek was last actioned ---
def already_processed(gameweek):
    import boto3
    ssm = boto3.client('ssm')
    try:
        resp = ssm.get_parameter(Name=SSM_PARAM_NAME)
        return int(resp['Parameter']['Value']) == gameweek
    except ssm.exceptions.ParameterNotFound:
        return False


def mark_processed(gameweek):
    """Record that this gameweek has been actioned.

    Called immediately after the last successful submission, and deliberately
    never raises: by this point the transfers and lineup are already live on
    FPL, so crashing here would send an error email claiming nothing changed
    and let the next run submit everything a second time.

    Returns True on success. A False means the duplicate guard is not in place
    for this gameweek, which is worth seeing in the log.
    """
    try:
        import boto3
        ssm = boto3.client('ssm')
        ssm.put_parameter(Name=SSM_PARAM_NAME, Value=str(gameweek),
                          Type='String', Overwrite=True)
        return True
    except Exception as e:
        print(f"!! Could not record GW{gameweek} as processed ({e}). "
              "The changes ARE live on FPL, but the next run may repeat them. "
              "Check ssm:PutParameter on parameter/fpl-bot/*.")
        return False


# Price -> points-per-game fit by position, calibrated on players with 900+
# Premier League minutes. Used only as a fallback for players with no PL
# history (new signings, promoted clubs) where points_per_game is 0.
# --- Quality model: points per 90, expected goal involvements, ICT ---
# Regression fitted on 874 player-season pairs from FPL's own history_past
# (2009-2024). Predicts a player's points per 90 next season from last
# season's per-90 rates.
#
#   expected_p90 = 2.047 + 0.339*p90 + 0.796*xgi90 + 0.110*ict90
#
# Out-of-sample (fit on the older half, tested on the newer half):
#   points-per-90 alone        R2 = 0.302
#   plus xGI and ICT           R2 = 0.368
#
# xGI (expected goal involvements) is the standard "underlying" stat: it
# regresses less than actual goals, so a player who created plenty of chances
# but finished badly is correctly rated above their raw points.
QUALITY_INTERCEPT = 2.047
QUALITY_W_P90 = 0.339
QUALITY_W_XGI90 = 0.796
QUALITY_W_ICT90 = 0.110

PRICE_TO_PPG = {
    1: (0.0745, -0.21),   # GK
    2: (0.1056, -2.00),   # DEF
    3: (0.0690, -0.95),   # MID
    4: (0.0476,  0.21),   # FWD
}

SNAPSHOT_MIN_MINUTES = 300        # below this, last season's rates are noise; use price instead
MIN_MINUTES_FOR_HISTORY = 900     # below this, lean on the price fallback
FULL_SEASON_STARTS = 34           # starts implying a nailed-on starter
MIN_START_PROB = 0.25             # floor, so a bit-part player isn't zeroed
UNPROVEN_START_PROB = 0.65        # prior for players with no PL history
EP_NEXT_TRUST_GAMEWEEK = 6        # gameweek by which ep_next reaches its full weight
EP_NEXT_MAX_WEIGHT = 0.5          # ...and that full weight is half, never all of it


# Snapshot of 2025/26 final stats, captured 2026-08-06 from bootstrap-static.
# FPL zeroes these when the new season starts, so they are frozen here.
# Format: player_id: [minutes, starts, total_points, xGI, ICT]
LAST_SEASON = {
    1:[3330, 37, 162, 0.1, 57.5], 2:[90, 1, 2, 0.0, 2.0], 4:[2750, 30, 209, 4.7, 125.0], 5:[2452, 28, 149, 6.2, 125.7],
    6:[2614, 30, 137, 2.1, 84.3], 7:[697, 5, 29, 0.3, 17.6], 8:[1697, 22, 109, 4.1, 77.9], 9:[1787, 20, 87, 2.0, 70.9],
    10:[699, 9, 45, 1.1, 34.3], 11:[986, 9, 40, 0.7, 32.9], 12:[2218, 25, 157, 14.7, 230.6], 13:[3093, 35, 184, 10.5, 215.4],
    14:[1885, 22, 113, 7.8, 115.1], 15:[1363, 16, 74, 4.9, 104.4], 16:[1205, 16, 72, 4.1, 101.3], 17:[1024, 10, 67, 4.8, 73.6],
    18:[1065, 11, 57, 5.4, 79.0], 19:[2991, 34, 133, 5.1, 126.2], 20:[152, 1, 16, 1.5, 16.6], 21:[101, 1, 7, 0.1, 4.0],
    22:[165, 0, 8, 0.3, 5.4], 24:[118, 0, 13, 0.8, 10.0], 25:[2217, 26, 128, 14.2, 157.5], 26:[577, 7, 36, 4.0, 49.9],
    27:[418, 3, 24, 3.0, 47.5], 28:[2835, 32, 120, 0.1, 79.2], 29:[585, 6, 16, 0.0, 13.9], 30:[1842, 21, 97, 4.0, 98.5],
    31:[3035, 34, 100, 1.9, 84.0], 32:[3016, 34, 117, 4.1, 137.5], 33:[1322, 15, 50, 1.7, 46.8], 34:[1673, 18, 64, 1.7, 45.1],
    35:[1042, 11, 40, 0.2, 20.6], 36:[1590, 17, 54, 3.6, 94.3], 37:[942, 11, 29, 0.4, 22.7], 38:[83, 1, 5, 0.0, 2.3],
    40:[3280, 37, 169, 11.2, 232.0], 41:[1753, 21, 102, 5.6, 121.4], 42:[909, 9, 44, 2.4, 42.0], 43:[1857, 21, 68, 4.4, 113.1],
    44:[450, 3, 21, 2.0, 30.1], 45:[2136, 28, 103, 7.1, 137.3], 46:[844, 7, 56, 2.8, 58.1], 47:[1410, 17, 61, 1.4, 64.2],
    48:[1755, 21, 76, 2.4, 74.4], 50:[16, 0, 1, 0.0, 0.0], 51:[25, 0, 2, 0.1, 0.3], 52:[47, 0, 3, 0.1, 1.5],
    54:[2207, 25, 85, 2.5, 102.0], 55:[2833, 33, 167, 16.6, 225.2], 56:[278, 2, 23, 2.4, 27.1], 57:[3420, 38, 124, 0.0, 82.4],
    60:[2102, 22, 110, 2.0, 86.3], 61:[3378, 38, 165, 4.4, 162.6], 62:[1275, 15, 46, 0.2, 34.4], 63:[367, 4, 19, 0.3, 12.2],
    64:[1073, 14, 58, 0.2, 27.1], 66:[34, 0, 3, 0.0, 0.6], 67:[1110, 13, 67, 4.5, 75.3], 68:[2732, 31, 137, 13.7, 207.9],
    69:[2848, 34, 136, 6.7, 139.0], 70:[945, 10, 41, 3.9, 58.8], 71:[872, 8, 40, 0.6, 40.6], 72:[105, 0, 11, 0.4, 8.9],
    73:[1769, 21, 77, 1.3, 66.3], 74:[1188, 13, 59, 9.2, 104.1], 75:[978, 9, 47, 2.6, 64.7], 76:[135, 2, 8, 0.4, 9.6],
    77:[1074, 10, 67, 4.2, 76.1], 78:[1663, 21, 113, 9.8, 139.9], 79:[2741, 32, 115, 12.1, 155.5], 80:[214, 0, 27, 2.1, 19.1],
    82:[3330, 37, 143, 0.2, 93.8], 83:[90, 1, 1, 0.0, 1.3], 84:[2971, 32, 129, 4.0, 131.9], 85:[2761, 30, 113, 4.8, 106.3],
    86:[1960, 22, 115, 5.5, 106.4], 87:[1805, 20, 76, 1.4, 62.7], 88:[3258, 37, 113, 4.2, 121.7], 89:[1322, 14, 52, 0.7, 26.9],
    90:[679, 8, 37, 0.6, 16.0], 91:[318, 4, 14, 0.1, 12.3], 94:[2744, 32, 125, 14.1, 172.8], 95:[2271, 25, 136, 12.1, 162.8],
    96:[2046, 24, 113, 8.0, 155.0], 97:[2239, 26, 98, 8.3, 126.9], 98:[1434, 15, 74, 3.1, 79.0], 99:[45, 1, 1, 0.0, 0.6],
    100:[100, 1, 13, 0.7, 8.1], 101:[1911, 22, 76, 2.6, 74.4], 102:[2652, 30, 104, 2.6, 83.4], 103:[2, 0, 2, 0.1, 1.6],
    104:[88, 0, 12, 0.4, 4.6], 106:[3282, 37, 181, 22.4, 256.9], 107:[1, 0, 1, 0.0, 0.0], 109:[3420, 38, 130, 0.1, 94.7],
    112:[3210, 36, 148, 4.9, 152.0], 113:[3130, 34, 118, 5.4, 123.7], 114:[799, 9, 39, 0.5, 31.1], 115:[1404, 17, 84, 5.7, 99.9],
    116:[2837, 31, 100, 2.4, 98.7], 117:[193, 2, 10, 0.0, 4.8], 118:[103, 1, 6, 0.0, 3.6], 121:[1714, 19, 62, 6.8, 112.3],
    122:[2389, 26, 117, 8.9, 180.3], 123:[1736, 20, 91, 7.3, 94.8], 124:[1636, 18, 78, 5.3, 128.5], 125:[1776, 20, 87, 5.8, 113.5],
    126:[202, 2, 16, 1.4, 19.6], 127:[2117, 26, 97, 8.0, 119.0], 128:[81, 1, 3, 0.3, 7.4], 129:[1921, 20, 98, 3.9, 84.5],
    130:[1901, 23, 94, 3.8, 97.7], 131:[1646, 23, 56, 1.5, 36.8], 132:[92, 1, 4, 0.0, 1.8], 133:[57, 0, 5, 0.1, 1.6],
    134:[1, 0, 1, 0.0, 0.0], 136:[2249, 26, 126, 13.8, 168.2], 137:[132, 1, 16, 0.6, 17.7], 138:[351, 2, 35, 2.7, 52.4],
    140:[3040, 35, 113, 0.0, 86.7], 141:[378, 3, 9, 0.0, 7.9], 142:[1957, 20, 115, 4.2, 106.1], 143:[2780, 31, 136, 2.5, 99.1],
    144:[2255, 26, 96, 4.5, 109.8], 145:[1718, 20, 65, 1.3, 60.4], 146:[470, 6, 7, 0.1, 11.4], 147:[788, 8, 34, 0.5, 29.3],
    148:[1139, 12, 19, 0.9, 38.4], 149:[225, 2, 8, 0.1, 8.0], 150:[100, 1, 3, 0.3, 1.3], 151:[657, 8, 39, 0.8, 24.4],
    154:[1954, 24, 114, 13.0, 147.7], 155:[3114, 35, 157, 18.5, 264.6], 156:[2629, 30, 125, 11.4, 186.7], 157:[839, 12, 58, 5.0, 72.6],
    158:[486, 5, 25, 1.8, 31.3], 159:[2796, 32, 110, 3.8, 109.2], 160:[1261, 14, 56, 7.1, 100.5], 161:[374, 4, 13, 0.8, 9.5],
    162:[1247, 13, 47, 2.9, 47.5], 163:[26, 0, 1, 0.0, 1.6], 165:[2658, 31, 177, 16.9, 212.1], 167:[1092, 12, 45, 4.8, 47.3],
    168:[265, 1, 13, 1.8, 13.1], 169:[1, 0, 1, 0.0, 0.2], 198:[3330, 37, 131, 0.1, 84.7], 199:[90, 1, 7, 0.0, 2.3],
    200:[3085, 35, 154, 3.3, 124.3], 201:[2400, 29, 136, 6.4, 139.5], 202:[2825, 31, 128, 4.0, 107.6], 203:[1331, 14, 72, 1.8, 48.1],
    204:[3253, 36, 135, 4.1, 136.0], 205:[99, 0, 6, 0.1, 4.5], 206:[550, 6, 20, 0.5, 18.3], 208:[2173, 24, 117, 12.1, 135.1],
    209:[1710, 19, 65, 3.8, 75.8], 210:[2552, 29, 112, 9.0, 139.6], 211:[2074, 26, 77, 10.7, 141.4], 212:[1560, 19, 45, 3.0, 60.9],
    213:[1722, 18, 69, 3.3, 72.5], 214:[1899, 22, 67, 4.9, 74.5], 215:[785, 7, 36, 2.8, 38.4], 216:[40, 0, 4, 0.0, 1.5],
    220:[90, 1, 2, 0.2, 4.9], 221:[7, 0, 2, 0.0, 0.3], 222:[2301, 26, 75, 6.4, 91.5], 223:[2210, 25, 114, 15.7, 150.8],
    224:[414, 2, 23, 2.2, 37.8], 225:[159, 0, 17, 0.5, 8.8], 226:[3420, 38, 135, 0.2, 90.8], 229:[3330, 37, 170, 4.6, 165.7],
    230:[678, 7, 32, 0.5, 29.3], 231:[2588, 29, 131, 2.8, 111.7], 232:[3118, 35, 116, 2.7, 107.5], 233:[2959, 33, 95, 2.7, 102.3],
    234:[238, 3, 22, 0.1, 8.8], 236:[2629, 30, 151, 9.7, 166.0], 237:[2781, 32, 128, 11.1, 166.5], 238:[1627, 18, 79, 5.4, 129.2],
    239:[3413, 38, 159, 8.3, 208.4], 240:[1477, 17, 68, 1.2, 45.9], 241:[1161, 14, 53, 3.3, 52.2], 242:[351, 1, 16, 1.8, 23.9],
    243:[679, 6, 29, 1.7, 41.8], 244:[524, 6, 21, 1.0, 21.0], 245:[349, 4, 18, 0.3, 13.6], 246:[675, 6, 36, 1.0, 26.3],
    248:[1551, 17, 104, 9.1, 124.5], 249:[1898, 21, 95, 8.6, 102.5], 250:[3420, 38, 122, 0.0, 81.5], 253:[2882, 33, 123, 3.3, 107.0],
    254:[1487, 17, 65, 2.6, 79.3], 255:[935, 11, 39, 0.6, 31.2], 256:[1791, 21, 81, 2.4, 73.9], 257:[2532, 28, 103, 2.0, 84.5],
    258:[1838, 20, 61, 3.0, 86.2], 259:[812, 8, 31, 0.8, 23.0], 260:[2674, 32, 168, 10.7, 196.9], 261:[2420, 29, 103, 6.6, 159.4],
    262:[1909, 22, 80, 5.8, 102.6], 263:[1030, 9, 49, 2.7, 70.4], 264:[1030, 11, 42, 3.6, 66.4], 265:[2904, 34, 91, 3.0, 91.0],
    266:[756, 6, 41, 2.0, 41.4], 267:[1730, 20, 67, 4.1, 93.4], 268:[1290, 15, 63, 3.1, 73.3], 269:[1816, 20, 95, 3.4, 86.7],
    270:[89, 1, 15, 0.6, 9.7], 271:[987, 10, 36, 3.5, 47.3], 272:[49, 0, 6, 0.1, 2.3], 325:[1980, 22, 71, 0.1, 53.4],
    326:[1440, 16, 43, 0.0, 32.2], 327:[1888, 21, 99, 1.6, 86.0], 328:[2935, 33, 108, 3.8, 113.1], 329:[2952, 33, 109, 2.0, 109.1],
    330:[2795, 32, 96, 5.2, 100.3], 331:[2637, 31, 68, 4.0, 96.4], 332:[1893, 21, 94, 3.0, 88.3], 333:[429, 5, 25, 0.2, 12.2],
    335:[2369, 28, 137, 7.3, 188.8], 336:[1553, 19, 109, 6.6, 115.0], 337:[2449, 30, 126, 8.1, 146.7], 338:[3119, 35, 134, 4.7, 114.7],
    339:[1003, 10, 56, 3.1, 81.2], 341:[521, 4, 32, 1.2, 27.5], 342:[262, 1, 12, 0.5, 11.5], 343:[547, 6, 23, 1.6, 33.9],
    344:[1318, 15, 56, 1.4, 43.2], 345:[1307, 14, 67, 2.8, 63.6], 346:[2721, 30, 142, 16.6, 194.7], 347:[1065, 10, 73, 8.8, 87.8],
    348:[244, 2, 17, 1.0, 16.5], 350:[2340, 26, 91, 0.0, 47.8], 351:[867, 10, 34, 0.0, 27.5], 352:[212, 2, 6, 0.0, 8.4],
    356:[3420, 38, 175, 5.2, 187.6], 357:[1032, 12, 63, 2.0, 56.5], 358:[2251, 27, 85, 2.2, 99.4], 359:[594, 7, 33, 0.9, 23.2],
    360:[928, 12, 42, 1.0, 32.3], 366:[2374, 27, 125, 12.0, 210.0], 367:[2736, 32, 131, 13.0, 208.4], 368:[3232, 36, 160, 11.6, 260.9],
    369:[547, 5, 43, 3.4, 60.1], 370:[317, 1, 37, 2.1, 34.9], 371:[2991, 34, 144, 4.2, 140.1], 372:[2654, 31, 108, 5.6, 139.9],
    373:[1923, 18, 81, 5.0, 104.6], 374:[170, 1, 12, 0.1, 8.0], 375:[21, 0, 6, 0.4, 2.1], 379:[694, 8, 41, 2.7, 36.9],
    380:[1797, 21, 125, 12.6, 156.2], 383:[110, 1, 5, 0.4, 7.4], 384:[3060, 34, 135, 0.1, 68.0], 385:[360, 4, 13, 0.0, 11.9],
    387:[2643, 29, 160, 8.8, 164.1], 388:[3150, 35, 179, 6.4, 160.9], 389:[2861, 32, 154, 2.6, 137.6], 390:[2139, 24, 113, 1.2, 81.0],
    391:[1370, 16, 79, 3.0, 69.8], 392:[971, 12, 60, 2.5, 50.2], 393:[1427, 15, 67, 1.9, 43.9], 394:[135, 2, 5, 0.3, 7.5],
    395:[401, 4, 19, 1.6, 23.8], 397:[3200, 37, 202, 14.2, 257.7], 398:[2078, 23, 131, 10.8, 201.9], 399:[1772, 19, 135, 13.1, 218.1],
    400:[1773, 19, 120, 8.7, 202.5], 401:[691, 8, 56, 3.5, 64.4], 402:[1510, 17, 69, 3.6, 90.3], 403:[817, 7, 43, 4.1, 65.8],
    404:[1623, 19, 92, 7.0, 111.2], 405:[1561, 17, 63, 1.8, 61.5], 406:[125, 1, 9, 0.6, 11.5], 409:[25, 0, 2, 0.2, 3.7],
    411:[2953, 34, 239, 28.2, 302.3], 412:[2880, 32, 109, 0.0, 63.7], 413:[540, 6, 11, 0.1, 12.8], 415:[1440, 15, 95, 4.2, 120.3],
    416:[1170, 13, 43, 2.2, 45.7], 417:[2609, 29, 111, 4.5, 119.6], 418:[1649, 19, 90, 1.6, 63.0], 419:[1229, 13, 51, 0.8, 42.0],
    420:[1730, 18, 56, 1.9, 53.2], 421:[920, 11, 42, 0.5, 29.1], 422:[977, 11, 53, 0.5, 36.8], 423:[3220, 38, 113, 2.7, 97.4],
    425:[2, 0, 1, 0.0, 0.4], 426:[3065, 35, 235, 23.1, 381.4], 427:[2611, 31, 148, 17.0, 231.4], 428:[2493, 29, 143, 10.3, 202.5],
    430:[1010, 12, 58, 3.2, 58.0], 431:[2339, 27, 91, 10.2, 163.5], 432:[1653, 16, 73, 1.8, 82.1], 433:[876, 8, 38, 0.9, 29.8],
    434:[107, 0, 3, 0.1, 4.8], 435:[40, 0, 3, 0.1, 1.7], 437:[15, 0, 1, 0.0, 1.4], 438:[17, 0, 2, 0.0, 0.3],
    439:[1630, 17, 111, 9.5, 143.4], 440:[607, 5, 42, 3.1, 58.3], 442:[2416, 27, 96, 0.2, 70.7], 445:[2963, 33, 126, 5.8, 138.9],
    446:[1089, 11, 51, 1.6, 46.4], 447:[1834, 21, 89, 2.6, 87.4], 448:[2195, 25, 93, 1.4, 82.1], 449:[2176, 24, 79, 3.8, 118.0],
    450:[1326, 14, 59, 1.0, 46.1], 451:[7, 0, 2, 0.0, 1.3], 452:[2456, 27, 154, 10.6, 202.9], 453:[1951, 19, 106, 9.6, 147.0],
    454:[1302, 14, 48, 3.4, 57.8], 455:[2534, 31, 81, 4.6, 113.4], 456:[1441, 15, 62, 2.6, 79.9], 457:[1606, 19, 82, 5.7, 106.0],
    458:[1950, 23, 73, 3.7, 95.0], 459:[1493, 15, 72, 3.5, 73.0], 460:[972, 10, 38, 2.3, 55.5], 463:[1896, 24, 108, 8.1, 117.5],
    464:[517, 4, 27, 3.7, 25.5], 465:[805, 8, 76, 4.9, 62.2], 466:[13, 0, 1, 0.0, 0.5], 467:[2667, 30, 105, 0.1, 74.0],
    468:[437, 5, 11, 0.0, 5.7], 469:[3203, 36, 128, 6.0, 175.6], 470:[1340, 14, 53, 1.5, 50.5], 471:[3375, 37, 119, 1.5, 86.9],
    472:[2130, 25, 83, 1.2, 74.0], 473:[1587, 18, 67, 1.2, 50.5], 474:[514, 6, 13, 0.3, 14.2], 475:[1025, 11, 39, 1.8, 43.5],
    477:[165, 2, 3, 0.0, 3.2], 478:[353, 3, 11, 0.4, 10.4], 480:[3101, 35, 188, 13.7, 242.5], 481:[3332, 37, 180, 7.7, 219.0],
    482:[1838, 21, 88, 5.8, 130.6], 483:[1167, 14, 50, 2.6, 56.6], 484:[1676, 17, 82, 5.9, 121.6], 485:[549, 6, 26, 1.6, 35.6],
    486:[288, 2, 17, 0.6, 17.3], 487:[1168, 17, 48, 2.2, 59.7], 488:[2073, 25, 89, 3.0, 76.8], 489:[602, 2, 30, 0.9, 27.2],
    490:[896, 11, 41, 4.6, 48.0], 491:[2293, 28, 114, 7.7, 136.4], 492:[480, 3, 43, 4.5, 48.4], 493:[89, 0, 9, 0.5, 6.3],
    494:[2790, 31, 90, 0.0, 72.2], 496:[630, 7, 20, 0.0, 10.3], 497:[3150, 35, 96, 0.0, 97.9], 498:[3288, 37, 175, 6.2, 168.6],
    499:[2793, 32, 117, 5.4, 177.5], 500:[1869, 22, 91, 2.6, 93.2], 501:[1489, 17, 63, 0.9, 55.5], 502:[1165, 11, 55, 2.5, 68.2],
    503:[3041, 35, 116, 3.2, 99.6], 505:[2049, 23, 78, 2.7, 70.5], 506:[1335, 14, 29, 0.9, 49.8], 508:[136, 2, 10, 0.6, 14.2],
    509:[1, 0, 1, 0.0, 0.1], 510:[1, 0, 1, 0.0, 0.0], 511:[142, 2, 3, 0.1, 6.2], 512:[1535, 19, 75, 4.3, 104.7],
    513:[1747, 19, 80, 6.9, 113.9], 514:[1334, 13, 78, 4.9, 107.6], 515:[34, 0, 3, 0.3, 2.8], 516:[1880, 23, 86, 1.6, 67.4],
    517:[972, 10, 46, 3.1, 67.6], 518:[1388, 13, 71, 2.7, 68.2], 519:[1183, 14, 44, 1.9, 55.1], 520:[955, 11, 49, 1.7, 41.5],
    522:[1471, 18, 61, 2.8, 55.5], 524:[14, 0, 1, 0.0, 0.2], 525:[3017, 35, 135, 5.0, 146.3], 526:[997, 11, 40, 3.3, 62.7],
    527:[1954, 20, 119, 10.1, 165.2], 528:[7, 0, 2, 0.0, 0.7], 529:[3150, 35, 136, 0.1, 93.1], 531:[270, 3, 16, 0.0, 7.4],
    532:[2144, 24, 116, 3.1, 121.5], 533:[2784, 32, 151, 4.3, 132.9], 534:[3032, 34, 110, 4.3, 120.9], 535:[2797, 32, 125, 2.5, 117.0],
    536:[1966, 23, 74, 0.8, 54.7], 537:[133, 1, 2, 0.0, 3.6], 539:[565, 5, 26, 0.3, 17.1], 540:[125, 2, 1, 0.1, 4.9],
    542:[2930, 33, 147, 11.2, 186.9], 543:[1403, 15, 65, 4.1, 53.9], 544:[2901, 32, 124, 4.7, 138.1], 545:[2891, 33, 80, 2.9, 84.6],
    546:[649, 9, 34, 1.0, 27.0], 547:[399, 2, 14, 1.4, 12.7], 548:[758, 11, 36, 1.8, 36.0], 549:[1553, 16, 83, 3.3, 69.1],
    550:[38, 1, 1, 0.0, 1.0], 551:[401, 5, 13, 0.9, 17.8], 552:[1920, 22, 92, 6.7, 106.7], 553:[1148, 11, 74, 5.1, 71.5],
}


# Pre-season record for players with no Premier League history, collected once
# on 2026-08-17 from public club-friendly and Community Shield results.
# Club friendlies are over, so this never needs refreshing during the season.
# Format: fpl_id: [starts, games, total_minutes, minutes_vs_top5, goals+assists_vs_top5]
# "top5" = opponent plays in the Premier League, La Liga, Bundesliga, Serie A or Ligue 1.
PRESEASON = {
    3:[1,4,101,73,0], 23:[1,4,116,87,0], 39:[1,5,124,80,0],
    58:[0,3,0,0,0], 59:[0,3,44,0,0], 93:[0,1,0,0,0],
    105:[1,2,109,109,0], 108:[0,2,19,19,0], 110:[2,2,180,180,0],
    111:[0,1,0,0,0], 119:[0,2,43,43,0], 120:[0,1,24,24,0],
    135:[0,2,54,54,0], 152:[4,5,337,311,0], 164:[2,4,166,76,0],
    166:[1,3,155,69,0], 171:[0,2,0,0,0], 172:[1,3,90,0,0],
    173:[2,3,136,90,0], 174:[1,3,134,90,0], 175:[3,3,226,180,0],
    176:[2,3,224,180,0], 177:[0,3,44,0,0], 178:[1,3,46,0,0],
    179:[0,3,44,0,0], 182:[2,2,180,180,0], 184:[3,3,226,180,0],
    185:[1,3,134,90,1], 186:[1,1,46,0,0], 187:[1,3,46,0,0],
    188:[1,2,67,2,0], 190:[3,3,226,180,1], 191:[0,3,25,0,0],
    192:[0,2,44,0,0], 194:[2,2,180,180,0], 195:[3,3,245,180,2],
    207:[2,2,122,77,0], 218:[2,3,120,30,0], 219:[2,2,90,0,0],
    227:[6,7,400,247,0], 228:[0,7,140,23,0], 235:[2,7,176,39,0],
    247:[5,5,379,333,0], 251:[0,2,0,0,0], 273:[0,4,44,0,0],
    274:[3,3,226,0,0], 277:[3,4,241,60,0], 279:[4,5,331,150,0],
    280:[3,4,254,180,0], 281:[3,3,152,0,0], 282:[4,5,335,150,0],
    283:[1,1,46,0,0], 284:[1,5,89,0,0], 285:[2,5,179,90,0],
    286:[3,4,226,180,0], 287:[3,5,301,120,0], 288:[1,5,163,0,0],
    289:[4,5,361,180,0], 290:[4,5,361,180,0], 292:[1,5,157,0,0],
    294:[1,2,61,0,0], 295:[4,5,361,180,0], 296:[1,5,105,0,0],
    297:[1,1,61,0,0], 300:[0,1,0,0,0], 301:[3,3,240,150,0],
    302:[0,2,0,0,0], 303:[1,4,93,93,0], 304:[5,5,369,279,1],
    305:[5,5,346,263,2], 306:[4,5,374,284,0], 307:[0,4,81,81,0],
    308:[1,5,187,97,0], 309:[5,5,376,286,0], 313:[2,4,177,177,1],
    314:[0,3,30,30,0], 315:[3,4,243,180,0], 316:[5,5,312,249,3],
    317:[0,2,27,0,0], 318:[0,1,44,44,0], 320:[0,4,111,111,2],
    321:[2,4,237,237,1], 323:[1,4,93,93,1], 324:[3,5,213,123,0],
    334:[5,5,377,377,1], 355:[0,5,14,0,0], 364:[4,5,241,196,0],
    365:[2,5,190,127,1], 377:[1,2,77,77,0], 386:[0,3,44,0,0],
    396:[2,3,90,90,0], 407:[0,3,58,30,0], 410:[0,3,28,0,0],
    414:[3,4,184,139,0], 424:[3,6,194,104,0], 429:[0,1,29,29,0],
    436:[2,4,119,45,0], 443:[0,4,0,0,0], 444:[2,3,156,90,0],
    461:[3,3,180,180,1], 462:[5,5,360,180,0], 476:[0,2,88,0,0],
    479:[0,2,88,0,0], 495:[1,2,90,0,0], 504:[2,2,111,111,0],
    523:[3,3,189,81,2], 541:[1,1,46,46,0], 554:[0,3,30,30,0],
    555:[1,3,45,0,0], 556:[0,2,30,30,0], 557:[5,6,349,242,4],
    559:[3,3,180,180,0], 560:[1,3,74,74,0], 561:[0,5,113,58,0],
    562:[4,4,228,182,0], 564:[2,2,180,180,0], 565:[2,2,161,161,1],
    566:[1,2,90,90,0], 567:[3,3,270,270,0], 568:[0,1,0,0,0],
    569:[3,3,253,163,0], 570:[1,2,120,120,0], 571:[1,1,90,90,1],
    572:[2,3,180,180,0], 574:[2,2,150,150,0], 575:[1,1,65,65,0],
    576:[1,1,60,60,0], 577:[1,1,59,59,0], 578:[0,3,16,13,0],
    579:[0,1,0,0,0], 580:[0,2,41,41,0], 581:[1,1,46,46,0],
    582:[2,3,168,60,0], 583:[0,1,14,14,0], 584:[0,1,25,25,0],
    585:[0,1,30,30,0], 586:[0,1,30,30,0], 587:[0,1,0,0,0],
}

# --- Pre-season adjustment for players new to the Premier League ---------------
# Two stages, in the order they can be trusted:
#
#   1. MINUTES. Whether a manager starts someone in friendlies is that manager's
#      own revealed opinion of the pecking order. This replaces the flat 0.65
#      start-probability guess with something evidence-based.
#
#   2. OUTPUT, but only against top-5-league opposition. Goals against a
#      fourth-tier side say nothing; goals against Dortmund say a little. This
#      adjustment is deliberately small — friendly output is weak evidence and,
#      unlike everything else in the model, it cannot be backtested.
# Pre-season record for ESTABLISHED players (those with a LAST_SEASON entry),
# collected once on 2026-08-17 alongside PRESEASON. Only players with 2+ club
# friendlies are included: below that, zero minutes is ambiguous — it can mean
# a deep World Cup run and planned rest (Watkins), a long-term injury
# (Maddison), or genuine exclusion, and the data cannot tell them apart.
# Format: fpl_id: [starts, games, total_minutes, minutes_vs_top5, goals+assists_vs_top5]
PRESEASON_ESTABLISHED = {
    1:[2,2,135,135,0], 2:[4,5,214,152,0], 4:[3,4,226,226,0],
    7:[5,5,339,277,2], 8:[4,5,273,211,1], 9:[2,5,199,166,1],
    10:[4,5,303,241,0], 11:[5,5,320,258,0], 14:[0,2,38,38,0],
    15:[2,3,162,162,1], 16:[1,2,88,88,0], 17:[0,2,28,28,0],
    18:[1,3,105,105,1], 20:[4,5,230,168,0], 22:[4,4,241,179,1],
    24:[1,3,73,45,0], 25:[2,5,201,168,1], 26:[3,5,259,226,1],
    29:[5,5,318,182,0], 32:[4,5,282,190,0], 33:[3,4,231,159,0],
    34:[1,4,191,147,0], 35:[3,5,265,120,1], 36:[3,5,316,200,0],
    37:[3,3,190,144,0], 41:[3,4,245,199,0], 42:[3,3,172,126,0],
    43:[1,3,144,144,0], 44:[1,2,113,0,0], 45:[0,2,88,44,1],
    46:[3,3,200,154,0], 47:[1,3,133,104,0], 51:[5,5,301,182,1],
    52:[2,2,136,90,0], 54:[1,3,134,90,1], 57:[3,3,226,180,0],
    60:[3,3,182,136,0], 61:[3,3,216,170,0], 62:[1,3,178,134,0],
    64:[3,3,202,156,0], 67:[2,2,151,151,0], 68:[3,3,216,170,1],
    69:[2,2,136,90,0], 70:[2,3,180,136,0], 71:[2,3,224,180,0],
    72:[1,3,101,29,0], 76:[1,3,134,90,0], 77:[0,2,87,43,0],
    79:[3,3,197,151,2], 82:[2,2,180,180,0], 84:[2,2,180,180,1],
    86:[2,2,169,169,1], 90:[1,2,118,118,0], 91:[1,2,109,109,1],
    95:[2,2,180,180,2], 97:[2,2,161,161,1], 98:[2,2,180,180,0],
    104:[2,3,222,178,0], 106:[2,2,161,161,4], 109:[2,2,180,180,0],
    114:[2,2,151,151,0], 115:[1,2,96,96,0], 116:[1,2,69,69,0],
    123:[2,2,141,141,1], 124:[2,2,175,175,0], 125:[2,2,133,133,2],
    127:[2,2,126,126,1], 129:[2,2,175,175,2], 130:[2,2,137,137,1],
    136:[2,2,103,103,0], 138:[0,2,47,47,0], 140:[3,4,222,196,0],
    145:[4,5,351,325,0], 147:[3,5,208,55,0], 148:[3,4,226,226,0],
    150:[1,3,56,56,0], 151:[2,5,217,153,0], 154:[3,4,267,241,0],
    156:[2,3,135,135,1], 157:[3,6,250,134,1], 158:[3,6,272,159,0],
    159:[3,3,227,227,2], 161:[2,5,174,174,1], 162:[6,6,359,253,0],
    165:[4,5,347,321,4], 167:[2,5,188,34,0], 199:[3,3,149,59,0],
    203:[3,3,180,90,0], 204:[3,3,180,90,0], 205:[2,3,120,30,0],
    209:[2,3,90,0,0], 212:[3,3,149,59,0], 215:[2,3,90,0,0],
    216:[2,2,90,0,0], 224:[2,3,133,43,0], 226:[1,2,90,90,0],
    229:[5,7,467,280,0], 230:[4,4,286,240,0], 231:[4,7,370,174,0],
    232:[4,7,330,173,0], 233:[5,6,426,321,0], 234:[1,4,67,67,0],
    236:[5,7,431,271,0], 237:[2,3,181,181,2], 240:[3,5,268,89,0],
    241:[2,5,232,77,0], 242:[4,7,354,187,1], 243:[2,7,228,109,0],
    244:[6,7,486,288,2], 245:[4,6,322,199,0], 246:[6,7,505,280,1],
    248:[3,7,242,98,0], 249:[4,7,372,262,2], 250:[2,2,180,180,0],
    254:[1,2,120,120,0], 257:[2,2,180,180,0], 258:[2,2,164,164,1],
    259:[2,4,214,207,0], 260:[5,6,315,271,0], 261:[2,2,150,150,1],
    264:[2,2,172,172,0], 265:[2,2,164,164,0], 268:[1,2,109,109,0],
    327:[2,3,122,122,0], 329:[6,6,424,334,2], 330:[5,6,398,354,0],
    331:[1,2,107,107,0], 332:[5,6,414,324,1], 333:[4,5,277,277,1],
    335:[3,6,253,209,1], 336:[1,2,68,68,0], 337:[3,4,176,176,4],
    338:[5,6,401,311,1], 339:[5,6,267,221,1], 341:[2,5,174,98,1],
    343:[4,6,288,212,1], 345:[3,6,208,164,1], 346:[4,4,217,217,5],
    347:[3,6,217,173,3], 348:[1,6,100,54,0], 351:[5,5,349,273,0],
    352:[0,3,23,23,0], 357:[3,4,246,219,1], 358:[3,4,190,145,1],
    366:[2,2,104,104,2], 368:[4,4,265,189,2], 369:[3,4,166,121,0],
    370:[3,4,151,151,1], 371:[2,2,104,104,1], 373:[3,3,135,90,0],
    374:[2,2,116,116,0], 375:[4,5,237,192,0], 379:[2,2,104,104,2],
    383:[3,5,190,127,1], 384:[3,3,270,270,0], 385:[2,2,135,135,0],
    389:[1,2,90,90,0], 390:[2,2,180,180,0], 391:[3,3,225,225,0],
    392:[1,3,58,58,1], 393:[2,2,180,180,0], 395:[2,3,165,165,0],
    397:[3,3,221,221,3], 398:[3,3,218,218,0], 401:[1,2,114,114,2],
    403:[2,2,160,160,0], 404:[3,3,197,135,0], 406:[3,3,185,185,0],
    412:[2,2,151,151,0], 415:[6,6,349,243,1], 417:[2,3,134,134,0],
    418:[5,5,343,253,1], 420:[5,6,331,225,0], 421:[6,6,347,241,0],
    422:[2,3,136,136,0], 423:[5,6,346,256,0], 427:[5,6,341,235,4],
    428:[1,2,90,90,0], 430:[3,4,216,110,0], 431:[4,4,232,232,1],
    434:[2,6,173,99,0], 435:[4,6,298,192,0], 438:[3,6,205,131,0],
    440:[3,6,210,104,1], 442:[2,2,114,0,0], 445:[3,3,208,208,1],
    446:[2,3,126,45,0], 447:[4,4,315,225,0], 449:[4,4,306,135,0],
    451:[5,5,331,171,2], 453:[5,5,348,180,1], 454:[3,4,248,174,1],
    455:[2,2,135,90,0], 456:[5,5,374,202,0], 457:[5,5,331,171,2],
    458:[1,2,68,68,0], 460:[4,5,327,155,0], 463:[3,3,198,198,0],
    464:[3,3,180,180,2], 465:[3,3,180,180,1], 467:[5,5,318,136,0],
    468:[0,2,88,0,0], 469:[5,5,318,136,0], 470:[2,2,92,0,0],
    471:[5,5,318,136,0], 472:[2,2,136,136,0], 473:[5,5,318,136,0],
    474:[2,2,92,0,0], 477:[0,2,88,0,0], 478:[0,2,88,0,0],
    480:[5,5,318,136,0], 483:[2,2,136,136,1], 485:[0,2,88,0,0],
    486:[4,5,316,134,0], 487:[1,2,90,0,0], 488:[2,2,136,136,0],
    489:[3,4,226,134,0], 491:[5,5,318,136,0], 492:[0,5,132,44,0],
    493:[0,2,88,0,0], 496:[2,2,135,90,0], 497:[2,4,135,0,0],
    508:[3,4,200,81,0], 514:[2,3,162,90,2], 519:[2,3,153,81,0],
    522:[3,3,197,90,0], 525:[1,2,73,0,0], 526:[2,2,91,46,0],
    527:[2,3,154,81,1], 528:[2,4,132,0,0], 529:[3,3,240,180,0],
    531:[3,4,204,158,0], 532:[5,5,292,246,0], 533:[4,4,210,111,0],
    534:[6,6,398,292,2], 536:[6,6,397,291,0], 537:[1,6,182,94,0],
    539:[6,6,397,291,0], 542:[5,5,322,276,1], 543:[1,4,153,93,0],
    544:[2,2,136,136,0], 545:[3,4,237,177,0], 546:[0,2,53,23,0],
    547:[3,4,163,117,0], 548:[4,6,269,163,1], 549:[1,2,108,108,0],
    550:[1,2,46,0,0], 551:[2,4,211,167,0], 552:[3,4,236,176,1],
    553:[1,3,88,88,1],
}

# Established players get a much smaller pre-season adjustment than unproven
# ones: last season is ~3,000 minutes of competitive evidence, pre-season is a
# few hundred minutes of friendlies. It can nudge the pecking order, not rewrite
# it. Players with fewer than 2 friendlies are absent from the table entirely,
# because zero minutes is ambiguous — see the note above PRESEASON_ESTABLISHED.
PRESEASON_ESTABLISHED_WEIGHT = 0.25


def established_preseason_adjustment(player_id, base_start_prob):
    """Nudge an established player's start probability using friendly minutes.

    Returns the adjusted probability. Absent players are returned unchanged, so
    a missing record can never penalise anyone.
    """
    rec = PRESEASON_ESTABLISHED.get(int(player_id))
    if not rec:
        return base_start_prob
    starts, games, minutes = rec[0], rec[1], rec[2]
    if games < PRESEASON_MIN_GAMES:
        return base_start_prob

    start_share = starts / games
    minutes_share = min(1.0, minutes / (games * 75.0))
    evidence = 0.7 * start_share + 0.3 * minutes_share

    w = PRESEASON_ESTABLISHED_WEIGHT
    return max(MIN_START_PROB, min(1.0, (1 - w) * base_start_prob + w * evidence))


PRESEASON_MIN_GAMES = 2       # below this, the sample is too small to read
PRESEASON_MIN_SERIOUS = 90    # minutes vs top-5 opposition before output counts
PRESEASON_MAX_BOOST = 0.30    # cap on stage 2, so one friendly cannot dominate


def preseason_start_prob(player_id, default=UNPROVEN_START_PROB):
    """Stage 1 — start probability from friendly minutes."""
    rec = PRESEASON.get(int(player_id))
    if not rec:
        return default
    starts, games, minutes = rec[0], rec[1], rec[2]
    if games < PRESEASON_MIN_GAMES:
        return default

    start_share = starts / games
    minutes_share = min(1.0, minutes / (games * 75.0))   # 75 mins ~ a full outing

    # Weight starts more than raw minutes: a substitute can rack up minutes in a
    # friendly without being anywhere near the first-choice XI.
    evidence = 0.7 * start_share + 0.3 * minutes_share

    # Blend toward the evidence as the sample grows; 5+ games is a full picture.
    weight = min(1.0, games / 5.0)
    prob = (1 - weight) * default + weight * evidence
    return max(MIN_START_PROB, min(1.0, prob))


def preseason_quality_boost(player_id):
    """Stage 2 — small multiplier from output against serious opposition."""
    rec = PRESEASON.get(int(player_id))
    if not rec:
        return 1.0
    serious_mins, serious_ga = rec[3], rec[4]
    if serious_mins < PRESEASON_MIN_SERIOUS or serious_ga <= 0:
        return 1.0

    ga90 = serious_ga / (serious_mins / 90.0)
    # 0.5 G+A per 90 against top-5 opposition is a strong friendly return.
    boost = min(PRESEASON_MAX_BOOST, ga90 * 0.4)
    return 1.0 + boost


def _quality_from_rates(p90, xgi90, ict90):
    """Expected points per 90 from the fitted regression."""
    return (QUALITY_INTERCEPT
            + QUALITY_W_P90 * p90
            + QUALITY_W_XGI90 * xgi90
            + QUALITY_W_ICT90 * ict90)


def estimate_base_points(players_df, gameweek):
    """Expected points per fixture for every player.

    Three sources of evidence, blended by how much of each we actually have:

      1. This season's rates      — best, but empty until games are played
      2. LAST_SEASON snapshot     — frozen before FPL zeroed the fields
      3. Price-implied output     — for players with no history at all

    Without the snapshot the model collapses at GW2-3: FPL resets every counter
    when a season starts, so `minutes` is around 90, the per-90 rates come from
    a single match, and ep_next is still only weighted about 17%. The snapshot
    covers exactly that window.
    """
    df = players_df
    num = lambda c: pd.to_numeric(df[c], errors='coerce').fillna(0.0)

    minutes = num('minutes')
    starts = num('starts')
    cost = num('now_cost')
    ep_next = num('ep_next')

    # --- 1. this season's rates
    nineties = (minutes / 90).replace(0, np.nan)
    quality_now = _quality_from_rates(
        (num('total_points') / nineties).fillna(0.0),
        (num('expected_goal_involvements') / nineties).fillna(0.0),
        (num('ict_index') / nineties).fillna(0.0))

    # --- 2. last season, from the frozen snapshot
    snap = df['id'].map(LAST_SEASON)
    have_snap = snap.notna()

    def snap_field(i):
        return snap.map(lambda v: v[i] if isinstance(v, list) else 0.0).astype(float)

    snap_minutes = snap_field(0)
    snap_starts = snap_field(1)
    snap_nineties = (snap_minutes / 90).replace(0, np.nan)
    quality_last = _quality_from_rates(
        (snap_field(2) / snap_nineties).fillna(0.0),
        (snap_field(3) / snap_nineties).fillna(0.0),
        (snap_field(4) / snap_nineties).fillna(0.0))

    # --- 3. price fallback
    slope = df['element_type'].map(lambda t: PRICE_TO_PPG.get(t, (0.06, 0))[0])
    intercept = df['element_type'].map(lambda t: PRICE_TO_PPG.get(t, (0.06, 0))[1])
    quality_price = (slope * cost + intercept).clip(lower=0.5)

    # --- blend: this season as far as it goes, then snapshot, then price
    w_now = (minutes / MIN_MINUTES_FOR_HISTORY).clip(0, 1)

    # The snapshot needs the same shrinkage as live data. A player with 90
    # minutes and one lucky return last season has a per-90 rate that looks
    # elite; without this, fringe players outrank genuine premiums whenever
    # the current season is still empty.
    # Threshold from a backtest on 1001 player-season pairs: below ~300 prior
    # minutes, last season's rates predict WORSE than price (corr -0.08 vs
    # -0.01); above it they win clearly (0.59-0.63 vs 0.32-0.43). So the
    # snapshot earns weight only past 300 minutes, reaching full at 900.
    w_snap = ((snap_minutes - SNAPSHOT_MIN_MINUTES)
              / (MIN_MINUTES_FOR_HISTORY - SNAPSHOT_MIN_MINUTES)).clip(0, 1)
    prior = w_snap * quality_last + (1 - w_snap) * quality_price
    prior = prior.where(have_snap, quality_price)

    quality = w_now * quality_now + (1 - w_now) * prior

    # Stage 2: nudge quality for unproven players who produced against serious
    # opposition. Only applies where there is no PL history to rely on instead.
    boost = df['id'].map(preseason_quality_boost)
    quality = quality * boost.where(~have_snap, 1.0)

    # --- start probability, from whichever season has more evidence
    start_prob_now = (starts / FULL_SEASON_STARTS).clip(MIN_START_PROB, 1.0)
    start_prob_last = (snap_starts / FULL_SEASON_STARTS).clip(MIN_START_PROB, 1.0)

    # Players with no PL history fall back to pre-season evidence where we have
    # it, and to the flat prior where we don't.
    unproven_prior = df['id'].map(preseason_start_prob)

    # Established players: nudge last season's start rate with pre-season
    # minutes, at a quarter of the weight. Absent records change nothing.
    start_prob_last = pd.Series(
        [established_preseason_adjustment(i, sp)
         for i, sp in zip(df['id'], start_prob_last)],
        index=start_prob_last.index)

    start_prob_prior = (w_snap * start_prob_last + (1 - w_snap) * unproven_prior)
    start_prob_prior = start_prob_prior.where(have_snap, unproven_prior)
    start_prob = w_now * start_prob_now + (1 - w_now) * start_prob_prior

    history_estimate = quality * start_prob

    # --- blend with ep_next once FPL's own projection means something
    #
    # ep_next is a placeholder before the season starts (about 24 distinct
    # values across 570 players), so it earns weight gradually. But it never
    # takes over completely: FPL's projection may well be good in-season, and
    # it may use inputs we don't have, yet there is no way to check — FPL does
    # not publish historical ep_next values, so it cannot be backtested the way
    # the p90/xGI/ICT model was.
    #
    # With two plausible estimators and no way to rank them, combining beats
    # choosing. The cap keeps our own measured model in the mix all season
    # instead of handing over to an untested one at GW7.
    w = min(EP_NEXT_MAX_WEIGHT,
            max(0.0, (gameweek - 1) / EP_NEXT_TRUST_GAMEWEEK) * EP_NEXT_MAX_WEIGHT)
    base = w * ep_next + (1 - w) * history_estimate

    n_snap = int(have_snap.sum())
    n_now = int((w_now > 0.5).sum())
    print(f"[scoring] GW{gameweek}: ep_next {w:.0%} / history {1-w:.0%}. "
          f"{n_now} players on this season's data, {n_snap} covered by the "
          f"last-season snapshot.")
    return base



# --- Forward-looking fixture difficulty -------------------------------------
# The bot re-decides every week, so in principle it could just buy a player when
# their fixtures turn. In practice transfers are scarce: knowing a good run
# starts in GW4 means buying at GW2 with a spare transfer, rather than being
# forced into it later at the cost of a hit.
#
# So the OWNERSHIP score averages difficulty over the next few gameweeks with
# only gentle decay — a player is bought for a run, not one match. The LINEUP
# score uses this gameweek alone, because who starts on Saturday has nothing to
# do with fixtures a month out. Same inputs, two horizons, two questions.
FIXTURE_HORIZON = 5
FIXTURE_WEIGHTS = [1.0, 0.85, 0.7, 0.55, 0.4]   # ownership view: genuinely forward-looking
FIXTURE_SCALE = 0.15      # ease of -0.95..+0.61 becomes a 0.86x..1.09x multiplier


def fixture_outlook(fixtures_all, gameweek, weights=None):
    """Weighted fixture ease per team over the next FIXTURE_HORIZON gameweeks.

    Returns {team_id: ease}, where ease is positive for a kind run and negative
    for a hard one. Doubles count twice and blanks count as a hard fixture,
    since both are real effects on what a player is worth.
    """
    weights = weights or FIXTURE_WEIGHTS[:FIXTURE_HORIZON]
    outlook, totals = {}, {}
    for offset, w in enumerate(weights):
        gw = gameweek + offset
        block = fixtures_all.loc[fixtures_all.event == gw]
        seen = set()
        for r in block.itertuples():
            for team, diff in ((r.team_h, r.team_h_difficulty),
                               (r.team_a, r.team_a_difficulty)):
                # difficulty runs 1 (easy) to 5 (hard); centre it on 3
                outlook[team] = outlook.get(team, 0.0) + w * (3.0 - float(diff))
                totals[team] = totals.get(team, 0.0) + w
                seen.add(team)
        # a team with no fixture this week is treated as a hard one
        for team in set(fixtures_all.team_h) | set(fixtures_all.team_a):
            if team not in seen:
                outlook[team] = outlook.get(team, 0.0) + w * (-1.0)
                totals[team] = totals.get(team, 0.0) + w

    return {t: outlook[t] / totals[t] for t in outlook if totals.get(t)}


def get_data(bootstrap_data, gameweek):
    players_df = pd.DataFrame(bootstrap_data['elements'])
    teams_df = pd.DataFrame(bootstrap_data['teams'])

    fixtures = get(f'https://fantasy.premierleague.com/api/fixtures/?event={gameweek}')
    fixtures_df = pd.DataFrame(fixtures)

    players_df['ep_next'] = players_df['ep_next'].astype(float)

    teams = dict(zip(teams_df.id, teams_df.name))
    players_df['team_name'] = players_df['team'].map(teams)

    if fixtures_df.empty:
        players_df['diff'] = 0
        players_df['fixture_count'] = 0
    else:
        home_strength = dict(zip(teams_df.id, teams_df.strength_overall_home))
        away_strength = dict(zip(teams_df.id, teams_df.strength_overall_away))
        fixtures_df['team_a_strength'] = fixtures_df['team_a'].map(away_strength)
        fixtures_df['team_h_strength'] = fixtures_df['team_h'].map(home_strength)

        a_players = pd.merge(players_df, fixtures_df, how="inner", left_on=["team"], right_on=["team_a"],
                     suffixes=("", "_fixture"))
        h_players = pd.merge(players_df, fixtures_df, how="inner", left_on=["team"], right_on=["team_h"],
                     suffixes=("", "_fixture"))
        a_players['diff'] = a_players['team_a_strength'] - a_players['team_h_strength']
        h_players['diff'] = h_players['team_h_strength'] - h_players['team_a_strength']

        fixture_rows = pd.concat([a_players, h_players])
        per_player = fixture_rows.groupby('id').agg(
            diff=('diff', 'mean'),
            fixture_count=('diff', 'size')
        ).reset_index()

        players_df = players_df.merge(per_player, on='id', how='left')
        players_df['diff'] = players_df['diff'].fillna(0)
        players_df['fixture_count'] = players_df['fixture_count'].fillna(0)  # 0 = blank gameweek

    players_df['base_points'] = estimate_base_points(players_df, gameweek)

    # Forward-looking fixture term, replacing the old single-gameweek one.
    try:
        all_fixtures = pd.DataFrame(get('https://fantasy.premierleague.com/api/fixtures/'))
        ease_run = fixture_outlook(all_fixtures, gameweek)
        ease_now = fixture_outlook(all_fixtures, gameweek, weights=[1.0])
        players_df['fixture_ease'] = players_df['team'].map(ease_run).fillna(0.0)
        players_df['fixture_ease_now'] = players_df['team'].map(ease_now).fillna(0.0)

        # Average fixtures per gameweek over the horizon, so a double or blank
        # in a coming week counts toward whether the player is worth owning.
        counts = {}
        for off in range(FIXTURE_HORIZON):
            block = all_fixtures.loc[all_fixtures.event == gameweek + off]
            for r in block.itertuples():
                counts[r.team_h] = counts.get(r.team_h, 0) + 1
                counts[r.team_a] = counts.get(r.team_a, 0) + 1
        players_df['avg_fixtures'] = (players_df['team'].map(counts).fillna(0)
                                      / FIXTURE_HORIZON)
        span = FIXTURE_HORIZON
    except Exception as e:
        print(f"[fixtures] Could not read the fixture list ({e}) — "
              "falling back to this gameweek only.")
        players_df['fixture_ease'] = 0.0
        players_df['fixture_ease_now'] = 0.0
        players_df['avg_fixtures'] = players_df['fixture_count']
        span = 1

    # TWO SCORES, because the bot answers two questions on different horizons.
    #
    #   score_now  — "who starts and who captains THIS week". Next month's
    #                fixtures are irrelevant to a Saturday lineup decision.
    #   score_run  — "who do I own". Transfers are the scarce resource, so a
    #                player is bought for a run of fixtures, not one match.
    #
    # A single number cannot serve both: weight it forward and captaincy
    # suffers, weight it on this week and transfers become short-sighted.
    #
    # Multiplicative rather than additive — an easy run is worth more to a
    # 6-point player than to a 2-point one. A flat bonus would favour cheap
    # players from weak teams with kind fixtures.
    penalty = players_df['status'].map(STATUS_PENALTY).fillna(-50)

    mult_now = (1.0 + players_df['fixture_ease_now'] * FIXTURE_SCALE).clip(0.8, 1.2)
    players_df['score_now'] = (
        players_df['base_points'] * players_df['fixture_count'] * mult_now + penalty)

    mult_run = (1.0 + players_df['fixture_ease'] * FIXTURE_SCALE).clip(0.8, 1.2)
    players_df['score_run'] = (
        players_df['base_points'] * players_df['avg_fixtures'] * mult_run + penalty)

    # `score` stays as the ownership view, so everything that selects a squad
    # keeps working unchanged. Lineup code asks for score_now explicitly.
    players_df['score'] = players_df['score_run']

    print(f"[fixtures] Ownership view over {span} gameweek(s) "
          f"({mult_run.min():.2f}-{mult_run.max():.2f}x); "
          f"lineup view on GW{gameweek} alone.")

    return players_df, fixtures_df


def pick_out_candidate(my_team):
    worst = my_team.sort_values('score').head(TOP_N_CANDIDATES)
    weights = worst['score'].max() - worst['score'] + 1
    return worst.sample(1, weights=weights)


def pick_in_candidate(potential_players):
    best = potential_players.sort_values('score', ascending=False).head(TOP_N_CANDIDATES)
    if best.empty:
        return None
    weights = best['score'] - best['score'].min() + 1
    return best.sample(1, weights=weights)


def pick_starting_xi(squad_df):
    """Best legal XI for THIS gameweek.

    Uses score_now, not score: who to start on Saturday has nothing to do with
    a player's fixtures next month. Falls back to score if the lineup view is
    missing, so this still works on any DataFrame.
    """
    if 'score_now' in squad_df.columns:
        squad_df = squad_df.assign(score=squad_df['score_now'])
    squad_df = squad_df.sort_values('score', ascending=False)
    gk = squad_df[squad_df.element_type == 1]
    df_ = squad_df[squad_df.element_type == 2]
    mid = squad_df[squad_df.element_type == 3]
    fwd = squad_df[squad_df.element_type == 4]

    starters = pd.concat([gk.head(1), df_.head(3), mid.head(2), fwd.head(1)])
    counts = {2: 3, 3: 2, 4: 1}
    caps = {2: 5, 3: 5, 4: 3}

    remaining_pool = pd.concat([df_.iloc[3:], mid.iloc[2:], fwd.iloc[1:]]).sort_values('score', ascending=False)
    extra_needed = 4
    extras = []
    for _, player in remaining_pool.iterrows():
        if extra_needed == 0:
            break
        et = player['element_type']
        if counts[et] < caps[et]:
            extras.append(player)
            counts[et] += 1
            extra_needed -= 1

    if extras:
        starters = pd.concat([starters, pd.DataFrame(extras)])

    subs = squad_df.loc[~squad_df.id.isin(starters.id)]

    # A squad that can't field a legal XI means the input was malformed —
    # better to say so than to email an eight-man team.
    if len(starters) != 11 or len(subs) != 4:
        raise ValueError(
            f"Could not build a legal XI: {len(starters)} starters and "
            f"{len(subs)} on the bench from a squad of {len(squad_df)}. "
            f"Positions present: {squad_df['element_type'].value_counts().to_dict()}")

    return starters, subs


# --- NEW: human-readable reasoning for a single player's score ---
def explain_player(player_row):
    reasons = []
    reasons.append(f"projected score {player_row.score:.2f} "
                    f"({player_row['base_points']:.2f} pts/fixture × "
                    f"{int(player_row.fixture_count)} fixture(s))")
    ppg = player_row.get('points_per_game')
    starts = player_row.get('starts')
    if ppg is not None and starts is not None:
        reasons.append(f"last season {float(ppg):.1f} pts/game over {int(starts)} starts")

    if player_row.fixture_count == 0:
        reasons.append("blank gameweek — no fixture this week")
    elif player_row.fixture_count > 1:
        reasons.append(f"double gameweek — {int(player_row.fixture_count)} fixtures this week")

    # NOTE: use ['diff'], not .diff — pandas Series has a .diff() method that
    # shadows the column, so attribute access returns the method object.
    fixture_diff = player_row['diff']
    if fixture_diff > 0:
        reasons.append(f"favourable fixture (+{fixture_diff:.1f} difficulty)")
    elif fixture_diff < 0:
        reasons.append(f"tough fixture ({fixture_diff:.1f} difficulty)")

    reasons.append(f"status: {STATUS_LABELS.get(player_row.status, player_row.status)}")
    return "; ".join(reasons)


# --- NEW: reasoning for why a transfer was or wasn't made ---
def explain_transfer_made(player_out, player_in, gain, required_gain, has_free_transfer, hit_cost):
    lines = [
        f"OUT — {player_out.web_name.iat[0]}: {explain_player(player_out.iloc[0])}",
        f"IN  — {player_in.web_name.iat[0]}: {explain_player(player_in.iloc[0])}",
        "",
        f"Projected gain from this swap: {gain:.2f} points."
    ]
    if has_free_transfer:
        lines.append(f"A free transfer was available (minimum bar to bother: {required_gain:.2f} pts) — this cleared it.")
    else:
        lines.append(f"No free transfer was available, so a -{hit_cost} point hit applied. "
                      f"The gain still cleared the higher bar of {required_gain:.2f} pts, so it was worth it.")
    return "\n".join(lines)


def explain_no_transfer(player_out, best_candidate, gain, required_gain, has_free_transfer, hit_cost):
    if best_candidate is None:
        return (f"Weakest player in the squad: {player_out.web_name.iat[0]} "
                f"({explain_player(player_out.iloc[0])}).\n"
                "No replacement was found — every option was ruled out by budget, position, "
                "or the 3-players-per-club limit.")

    lines = [
        f"Weakest player considered: {player_out.web_name.iat[0]}: {explain_player(player_out.iloc[0])}",
        f"Best available replacement: {best_candidate.web_name.iat[0]}: {explain_player(best_candidate.iloc[0])}",
        "",
        f"Projected gain: {gain:.2f} points."
    ]
    if has_free_transfer:
        lines.append(f"A free transfer was available, but {gain:.2f} pts is below the "
                      f"{required_gain:.2f} pt minimum bar to bother making a change.")
        lines.append("(Nothing is lost by waiting — the transfer stays available.)")
    else:
        lines.append(f"No free transfer was available — an extra transfer costs {hit_cost} pts, "
                      f"so a gain of at least {required_gain:.2f} pts was required. "
                      f"The best option only offered {gain:.2f}, so no transfer was made.")
    return "\n".join(lines)


# --- Chip recommendations (info-only — never played automatically) ---
def evaluate_chips(squad_df, bench_df, captain_row):
    recommendations = []

    bench_score = bench_df['score'].sum()
    if bench_score >= BENCH_BOOST_THRESHOLD:
        recommendations.append(("Bench Boost", True,
            f"Your bench is projected {bench_score:.1f} points this week — strong enough that "
            f"playing all 15 could be worth it."))
    else:
        recommendations.append(("Bench Boost", False,
            f"Bench projected only {bench_score:.1f} points this week — not worth playing."))

    cap_score = captain_row.score
    cap_double = captain_row.fixture_count > 1
    if cap_score >= TRIPLE_CAPTAIN_THRESHOLD or cap_double:
        reason = f"{captain_row.web_name} is projected {cap_score:.1f} points"
        if cap_double:
            reason += " across a double gameweek"
        reason += " — a strong week to triple instead of double their points."
        recommendations.append(("Triple Captain", True, reason))
    else:
        recommendations.append(("Triple Captain", False,
            f"{captain_row.web_name}'s projected {cap_score:.1f} points isn't exceptional enough "
            f"to justify Triple Captain this week."))

    blank_count = int((squad_df['fixture_count'] == 0).sum())
    if blank_count >= FREE_HIT_BLANK_THRESHOLD:
        recommendations.append(("Free Hit", True,
            f"{blank_count} of your 15 players have no fixture this week (blank gameweek) — "
            f"Free Hit would let you field a full-strength team just for this week."))
    else:
        recommendations.append(("Free Hit", False,
            f"Only {blank_count} squad player(s) have a blank this week — not enough to justify Free Hit."))

    unavailable_count = int((squad_df['status'] != 'a').sum())
    if unavailable_count >= WILDCARD_UNAVAILABLE_THRESHOLD:
        recommendations.append(("Wildcard", True,
            f"{unavailable_count} of your 15 players are currently doubtful, injured, or suspended — "
            f"worth considering a Wildcard to rebuild properly rather than one-by-one transfers."))
    else:
        recommendations.append(("Wildcard", False,
            f"Only {unavailable_count} squad player(s) are flagged as unavailable — "
            f"a Wildcard doesn't look necessary yet."))

    return recommendations


# --- Suggested rebuild squad, shown only when Wildcard or Free Hit is flagged ---
def build_suggested_squad(players_df, budget):
    """Optimal 15-man squad within `budget`, maximising total `score`.

    Uses PuLP + CBC for a true mathematical optimum. If the PuLP layer isn't
    attached, falls back to build_suggested_squad_fallback(), which gets within
    about 1% without any extra dependency — so the bot never silently returns
    nothing.
    """
    try:
        import pulp
    except ImportError:
        print("[optimizer] PuLP not available — using the pure-Python fallback. "
              "Attach the PuLP layer for the exact optimum.")
        return build_suggested_squad_fallback(players_df, budget)

    POSITION_REQUIREMENTS = {1: 2, 2: 5, 3: 5, 4: 3}
    MAX_PER_CLUB = 3

    eligible = players_df.loc[~players_df.status.isin(UNAVAILABLE_STATUSES)].copy()
    eligible = eligible[eligible['now_cost'].notna() & eligible['score'].notna()]
    if eligible.empty:
        print("[optimizer] No eligible players.")
        return None

    prob = pulp.LpProblem("Chip_Squad_Suggestion", pulp.LpMaximize)
    ids = eligible.id.tolist()
    pick = pulp.LpVariable.dicts("pick", ids, cat="Binary")

    score = dict(zip(eligible.id, eligible.score.astype(float)))
    cost = dict(zip(eligible.id, eligible.now_cost.astype(float)))
    pos = dict(zip(eligible.id, eligible.element_type.astype(int)))
    club = dict(zip(eligible.id, eligible.team.astype(int)))

    prob += pulp.lpSum(pick[i] * score[i] for i in ids)
    prob += pulp.lpSum(pick[i] * cost[i] for i in ids) <= budget

    for p, need in POSITION_REQUIREMENTS.items():
        prob += pulp.lpSum(pick[i] for i in ids if pos[i] == p) == need

    for c in set(club.values()):
        prob += pulp.lpSum(pick[i] for i in ids if club[i] == c) <= MAX_PER_CLUB

    try:
        prob.solve(pulp.PULP_CBC_CMD(msg=0))
    except Exception as e:
        print(f"[optimizer] CBC solver failed ({e}) — using fallback.")
        return build_suggested_squad_fallback(players_df, budget)

    if pulp.LpStatus[prob.status] != "Optimal":
        print(f"[optimizer] No optimal solution ({pulp.LpStatus[prob.status]}) — using fallback.")
        return build_suggested_squad_fallback(players_df, budget)

    chosen = [i for i in ids if pick[i].value() == 1]
    squad = eligible[eligible.id.isin(chosen)].copy()
    print(f"[optimizer] PuLP/CBC optimal — total score {squad.score.sum():.2f}, "
          f"cost {squad.now_cost.sum()/10:.1f}m of {budget/10:.1f}m budget.")
    return squad.sort_values(['element_type', 'score'], ascending=[True, False])


def build_suggested_squad_fallback(players_df, budget):
    """Fallback optimizer — no dependencies. Used only if PuLP is missing.

    Best 15-man squad within `budget`, using only the standard library.

    Replaces the PuLP version so no extra Lambda layer is needed. Runs a greedy
    seed followed by local search: repeatedly tries swapping each squad player
    for every legal alternative and keeps any swap that improves total score.
    Stops when no single swap helps.

    Constraints enforced: 2 GK / 5 DEF / 5 MID / 3 FWD, max 3 per club,
    total cost within budget, no unavailable players.

    Returns a 15-row DataFrame, or None if no legal squad fits the budget.
    """
    POSITION_REQUIREMENTS = {1: 2, 2: 5, 3: 5, 4: 3}
    MAX_PER_CLUB = 3

    eligible = players_df.loc[~players_df.status.isin(UNAVAILABLE_STATUSES)].copy()
    eligible = eligible[eligible['now_cost'].notna() & eligible['score'].notna()]
    if eligible.empty:
        print("[optimizer:fallback] No eligible players.")
        return None

    # Compact records — much faster than repeated DataFrame lookups
    pool = [{'id': int(r.id), 'pos': int(r.element_type), 'club': int(r.team),
             'cost': float(r.now_cost), 'score': float(r.score)}
            for r in eligible.itertuples()]

    by_pos = {}
    for p in pool:
        by_pos.setdefault(p['pos'], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x['score'], reverse=True)

    for pos, need in POSITION_REQUIREMENTS.items():
        if len(by_pos.get(pos, [])) < need:
            print(f"[optimizer:fallback] Not enough eligible players in position {pos}.")
            return None

    def club_ok(squad, club, exclude_id=None):
        n = sum(1 for p in squad if p['club'] == club and p['id'] != exclude_id)
        return n < MAX_PER_CLUB

    # --- greedy seed: cheapest legal squad, so we start inside the budget ---
    squad = []
    for pos, need in POSITION_REQUIREMENTS.items():
        picked = 0
        for p in sorted(by_pos[pos], key=lambda x: x['cost']):
            if picked == need:
                break
            if club_ok(squad, p['club']):
                squad.append(p)
                picked += 1
        if picked < need:
            print(f"[optimizer:fallback] Could not fill position {pos} legally.")
            return None

    spent = sum(p['cost'] for p in squad)
    if spent > budget:
        print(f"[optimizer:fallback] Even the cheapest legal squad costs "
              f"{spent/10:.1f}m, over the {budget/10:.1f}m budget.")
        return None

    # --- local search: swap one player at a time while it helps ---
    squad_ids = {p['id'] for p in squad}
    improved = True
    passes = 0

    while improved and passes < 40:
        improved = False
        passes += 1

        for i, current in enumerate(squad):
            headroom = budget - (spent - current['cost'])
            best_swap, best_delta = None, 0.0

            for cand in by_pos[current['pos']]:
                if cand['id'] in squad_ids:
                    continue
                if cand['cost'] > headroom:
                    continue  # list is score-sorted, not cost-sorted, so keep going
                delta = cand['score'] - current['score']
                if delta <= best_delta:
                    continue
                if cand['club'] != current['club'] and not club_ok(squad, cand['club'],
                                                                   exclude_id=current['id']):
                    continue
                best_swap, best_delta = cand, delta

            if best_swap is not None:
                squad_ids.discard(current['id'])
                squad_ids.add(best_swap['id'])
                spent = spent - current['cost'] + best_swap['cost']
                squad[i] = best_swap
                improved = True

    # --- paired moves: downgrade one slot to afford an upgrade elsewhere ---
    # Single swaps get stuck because no individual change fits the budget.
    # Swapping two at once frees money in one slot and spends it in another.
    improved = True
    pair_passes = 0

    while improved and pair_passes < 60:
        improved = False
        pair_passes += 1

        for i in range(len(squad)):
            if improved:
                break
            for j in range(len(squad)):
                if i == j:
                    continue
                a_cur, b_cur = squad[i], squad[j]

                # cheaper alternatives for slot i, ranked by least score lost
                downgrades = [c for c in by_pos[a_cur['pos']]
                              if c['id'] not in squad_ids and c['cost'] < a_cur['cost']]
                downgrades.sort(key=lambda c: c['score'] - a_cur['score'], reverse=True)

                for down in downgrades[:12]:
                    freed = a_cur['cost'] - down['cost']
                    loss = a_cur['score'] - down['score']
                    headroom = budget - (spent - a_cur['cost'] - b_cur['cost']
                                         + down['cost'])

                    for up in by_pos[b_cur['pos']]:
                        if up['id'] in squad_ids or up['id'] == down['id']:
                            continue
                        if up['cost'] > headroom:
                            continue
                        gain = up['score'] - b_cur['score']
                        if gain - loss <= 1e-9:
                            continue

                        trial = [p for k, p in enumerate(squad) if k not in (i, j)]
                        if not (club_ok(trial, down['club'])
                                and club_ok(trial + [down], up['club'])):
                            continue

                        squad_ids.discard(a_cur['id'])
                        squad_ids.discard(b_cur['id'])
                        squad_ids.add(down['id'])
                        squad_ids.add(up['id'])
                        spent += down['cost'] - a_cur['cost'] + up['cost'] - b_cur['cost']
                        squad[i], squad[j] = down, up
                        improved = True
                        break

                    if improved:
                        break
                if improved:
                    break

    total = sum(p['score'] for p in squad)
    print(f"[optimizer:fallback] {passes} single pass(es), {pair_passes} pair pass(es); "
          f"total score {total:.2f}, cost {spent/10:.1f}m of {budget/10:.1f}m budget.")

    result = eligible[eligible['id'].isin(squad_ids)].copy()
    return result.sort_values(['element_type', 'score'], ascending=[True, False])


def format_squad_table(squad_df):
    """Plain-text, fixed-width table — renders aligned in any monospace-font email view."""
    position_names = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    header = f"{'POS':<4}{'PLAYER':<20}{'TEAM':<14}{'PRICE':>8}{'SCORE':>8}"
    lines = [header, "-" * len(header)]
    total_cost = 0
    for pos in [1, 2, 3, 4]:
        for _, p in squad_df.loc[squad_df.element_type == pos].iterrows():
            total_cost += p.now_cost
            price_str = f"£{p.now_cost / 10:.1f}m"
            lines.append(f"{position_names[pos]:<4}{p.web_name:<20}{p.team_name:<14}{price_str:>8}{p.score:>8.2f}")
    lines.append("-" * len(header))
    lines.append(f"Total cost: £{total_cost / 10:.1f}m")
    return "\n".join(lines)


def format_chip_section(recommendations, suggested_table):
    lines = ["Chip recommendations (info only — nothing is played automatically):"]
    for chip, recommended, reason in recommendations:
        tag = "CONSIDER" if recommended else "skip"
        lines.append(f"  [{tag}] {chip} — {reason}")

    if suggested_table is not None:
        lines.append("")
        lines.append("Suggested squad if you play Wildcard or Free Hit "
                      "(budget = your current squad value + bank):")
        lines.append("")
        lines.append(suggested_table)
    elif suggested_table is None:
        pass  # no Wildcard/Free Hit flagged this week, or optimizer unavailable — nothing to add

    return "\n".join(lines)


def send_error_email(error_text):
    sender = os.environ.get('SMTP_EMAIL')
    app_password = os.environ.get('SMTP_APP_PASSWORD')
    recipient = os.environ.get('NOTIFY_EMAIL')

    if not all([sender, app_password, recipient]):
        print("Email env vars not set — could not send error notification either.")
        return

    body = (
        "The FPL bot hit an error and did not finish its run.\n\n"
        "In almost every case nothing was submitted — the bot checks each step "
        "before acting. The exception is a failure AFTER a transfer or lineup "
        "was accepted, so check the log below for 'accepted by FPL' before "
        "assuming your team is untouched.\n\n"
        "Error details:\n"
        f"{error_text}\n\n"
        "Full details are in the AWS Lambda CloudWatch logs for this function."
    )
    msg = MIMEText(body)
    msg['Subject'] = "FPL Bot: Something went wrong"
    msg['From'] = sender
    msg['To'] = recipient

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, app_password)
            server.sendmail(sender, recipient, msg.as_string())
        print("Error notification email sent.")
    except Exception as e:
        # If even the error email fails, just log it — nothing more we can do.
        print(f"Error email itself failed to send: {e}")

def should_send_outage_email():
    """True at most once per ERROR_EMAIL_COOLDOWN_HOURS.

    An FPL outage lasting a day would otherwise send one email per run. After a
    few of those you stop reading them, which defeats the point of alerting.
    """
    param = '/fpl-bot/last-outage-email'
    now = datetime.now(timezone.utc)

    try:
        import boto3
        ssm = boto3.client('ssm')
        try:
            resp = ssm.get_parameter(Name=param)
            last = _as_utc(datetime.fromisoformat(resp['Parameter']['Value']))
            hours_since = (now - last).total_seconds() / 3600
            if hours_since < ERROR_EMAIL_COOLDOWN_HOURS:
                print(f"Outage email suppressed — last one was {hours_since:.1f}h ago "
                      f"(cooldown is {ERROR_EMAIL_COOLDOWN_HOURS}h).")
                return False
        except ssm.exceptions.ParameterNotFound:
            pass  # never sent one before

        ssm.put_parameter(Name=param, Value=now.isoformat(),
                          Type='String', Overwrite=True)
        return True

    except Exception as e:
        print(f"Could not check outage-email cooldown ({e}) — sending anyway.")
        return True
ERROR_EMAIL_COOLDOWN_HOURS = 12


# ---------------------------------------------------------------------------
# ADVISORY MODE
#
# This build never logs in and never writes to FPL. FPL replaced its old
# login endpoint (users.premierleague.com) with an OAuth2 identity provider,
# so simple email/password submission no longer works.
#
# Everything here runs off public endpoints. The bot works out what it would
# do and emails you. You make the change in the app yourself.
# ---------------------------------------------------------------------------

POSITION_NAMES = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}


def fetch_public_squad(team_id, players_df, gameweek):
    """Read the current squad without logging in.

    FPL exposes a manager's picks publicly once a gameweek's deadline has
    passed, at /api/entry/{id}/event/{gw}/picks/. Before the first deadline of
    the season nothing is published yet, so this returns None and the caller
    falls back to recommending a squad from scratch.

    Returns (squad_df, bank, squad_value) or (None, None, None).
    """
    try:
        entry = get(f'https://fantasy.premierleague.com/api/entry/{team_id}/')
    except FPLUnavailable:
        raise
    except Exception as e:
        print(f"Could not read entry {team_id}: {e}")
        return None, None, None

    bank = entry.get('last_deadline_bank')
    value = entry.get('last_deadline_value')

    # Picks are published for completed gameweeks only, so look one behind.
    last_gw = gameweek - 1
    if last_gw < 1:
        print("No completed gameweek yet — no published squad to read.")
        return None, bank, value

    try:
        picks = get(f'https://fantasy.premierleague.com/api/entry/{team_id}'
                    f'/event/{last_gw}/picks/')
    except Exception as e:
        print(f"No published picks for GW{last_gw}: {e}")
        return None, bank, value

    ids = [p['element'] for p in picks.get('picks', [])]
    if not ids:
        return None, bank, value

    squad = players_df[players_df['id'].isin(ids)].copy()
    print(f"Read your GW{last_gw} squad: {len(squad)} players "
          f"(bank £{(bank or 0)/10:.1f}m, value £{(value or 0)/10:.1f}m)")
    return squad, bank, value


def format_xi(starters, subs, captain_row, vice_row):
    lines = []
    shape = starters['element_type'].value_counts()
    lines.append(f"Suggested XI  ({shape.get(2,0)}-{shape.get(3,0)}-{shape.get(4,0)})")
    lines.append("-" * 52)
    for r in starters.sort_values(['element_type', 'score'],
                                  ascending=[True, False]).itertuples():
        mark = ''
        if r.id == captain_row.id:
            mark = '  (C)'
        elif r.id == vice_row.id:
            mark = '  (V)'
        lines.append(f"  {POSITION_NAMES[r.element_type]:<4}{r.web_name:<20}"
                     f"{r.score:>7.2f}{mark}")
    lines.append("")
    lines.append("Bench, in order")
    for r in subs.sort_values('score', ascending=False).itertuples():
        lines.append(f"  {POSITION_NAMES[r.element_type]:<4}{r.web_name:<20}{r.score:>7.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML EMAIL
#
# Email clients are stuck in 2005: inline styles only, tables for layout, no
# flexbox or grid. Everything below is deliberately plain HTML for that reason.
# The plain-text version is still sent alongside, so clients that refuse HTML
# (and screen readers) get the same information.
# ---------------------------------------------------------------------------

FPL_PURPLE = '#37003C'
FPL_PINK = '#FF2882'
FPL_GREEN = '#00A862'
FPL_AMBER = '#B26A00'
INK = '#1B1020'
MUTED = '#6B5A72'
ROW_ALT = '#F4F0F6'
RULE = '#E3DAE8'

POS_SHORT = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}


def _h(text):
    """Escape user-facing strings so a stray & or < can't break the layout."""
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _section_title(text):
    return (f'<div style="padding:24px 22px 6px;font-size:16px;font-weight:700;'
            f'color:{INK};">{_h(text)}</div>')


def _note(text):
    return (f'<div style="padding:0 22px;color:{MUTED};font-size:13px;'
            f'line-height:1.6;margin-top:6px;">{_h(text)}</div>')


def _player_rows(rows, captain_id=None, vice_id=None, score_key='score'):
    """Table rows for a list of player Series.

    score_key picks which view to show: 'score_now' for lineup tables, since
    that is the number that decided who starts.
    """
    html = []
    for i, r in enumerate(rows):
        bg = '#FFFFFF' if i % 2 == 0 else ROW_ALT
        badge = ''
        if captain_id is not None and int(r['id']) == captain_id:
            badge = (f'<span style="background:{FPL_PINK};color:#FFF;font-size:10px;'
                     f'font-weight:700;padding:1px 5px;border-radius:3px;'
                     f'margin-left:6px;">C</span>')
        elif vice_id is not None and int(r['id']) == vice_id:
            badge = (f'<span style="background:{MUTED};color:#FFF;font-size:10px;'
                     f'font-weight:700;padding:1px 5px;border-radius:3px;'
                     f'margin-left:6px;">V</span>')
        html.append(
            f'<tr style="background:{bg};">'
            f'<td style="padding:9px 6px;color:{MUTED};font-size:12px;width:38px;">'
            f'{POS_SHORT.get(int(r["element_type"]), "")}</td>'
            f'<td style="padding:9px 6px;font-weight:600;font-size:14px;">'
            f'{_h(r["web_name"])}{badge}</td>'
            f'<td style="padding:9px 6px;text-align:right;font-size:13px;color:{MUTED};">'
            f'£{float(r["now_cost"])/10:.1f}m</td>'
            f'<td style="padding:9px 6px;text-align:right;font-size:14px;font-weight:600;">'
            f'{float(r.get(score_key, r["score"])):.2f}</td></tr>')
    return ''.join(html)



# ---------------------------------------------------------------------------
# SQUAD POSTER
#
# Draws the gameweek squad as a shareable PNG and attaches it to the email.
# Requires the Pillow layer (which also carries the fonts — Lambda has none of
# its own). If the layer is missing, the email still sends without the image.
# ---------------------------------------------------------------------------

POSTER_W, POSTER_H = 1080, 1350          # 4:5, the portrait ratio LinkedIn keeps
FONT_DIR = '/opt/python/fonts'           # where the Lambda layer puts them

C_DEEP = (23, 2, 29)
C_MID = (58, 10, 70)
C_GLOW = (92, 15, 108)
C_MINT = (0, 255, 135)
C_AMBER = (255, 194, 77)
C_WHITE = (255, 255, 255)
C_LAV = (199, 179, 207)
C_LAV_D = (154, 133, 164)
C_GLOW2 = (120, 40, 130)


def _font(name, size, weight='Regular'):
    from PIL import ImageFont
    for base in (FONT_DIR, os.path.join(os.path.dirname(__file__), 'fonts'), 'fonts'):
        path = os.path.join(base, f'{name}.ttf')
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
            try:
                f.set_variation_by_name(weight)
            except Exception:
                pass
            return f
    return None



_TEXT_PROBE = None


def _fit_font(text, max_w, name='Inter', size=24, weight='SemiBold', floor=15):
    """Largest font size at which `text` fits inside max_w pixels.

    Player names vary a lot in length — "Enzo" against "Calvert-Lewin" — and a
    fixed size makes the long ones spill past the card edge.
    """
    global _TEXT_PROBE
    from PIL import ImageDraw, Image
    if _TEXT_PROBE is None:
        _TEXT_PROBE = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    probe = _TEXT_PROBE
    while size >= floor:
        f = _font(name, size, weight)
        if f is None:
            return None
        if probe.textlength(str(text), font=f) <= max_w:
            return f
        size -= 1
    return _font(name, floor, weight)


def _gradient(size):
    """Deep purple wash with a glow behind the title and a second one low right."""
    from PIL import Image
    import math
    w, h = size
    img = Image.new('RGB', (w, h), C_DEEP)
    px = img.load()
    for y in range(h):
        t = y / h
        base = tuple(int(C_MID[i] + (C_DEEP[i] - C_MID[i]) * min(1.0, t * 1.3))
                     for i in range(3))
        for x in range(0, w, 3):
            dx, dy = (x - w * 0.42) / (w * 0.62), (y + h * 0.05) / (h * 0.40)
            g = max(0.0, 1.0 - math.sqrt(dx * dx + dy * dy)) ** 2
            # a cooler second glow keeps the lower half from going flat
            dx2, dy2 = (x - w * 0.92) / (w * 0.55), (y - h * 0.82) / (h * 0.34)
            g2 = max(0.0, 1.0 - math.sqrt(dx2 * dx2 + dy2 * dy2)) ** 2
            col = tuple(min(255, int(base[i]
                                     + (C_GLOW[i] - base[i]) * g * 0.9
                                     + (C_GLOW2[i] - base[i]) * g2 * 0.5))
                        for i in range(3))
            for xx in range(x, min(x + 3, w)):
                px[xx, y] = col
    return img


def _card(draw, box, fill=(255, 255, 255, 14), outline=(255, 255, 255, 34), r=14, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def _glow(draw, box, colour, r=14, layers=5, spread=9):
    """Cheap outer glow: concentric rounded rectangles, fading outwards."""
    for i in range(layers, 0, -1):
        pad = i * spread / layers
        a = int(colour[3] * (1 - i / (layers + 1)) ** 2)
        if a <= 0:
            continue
        draw.rounded_rectangle(
            (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
            radius=r + pad, outline=(colour[0], colour[1], colour[2], a), width=2)


def _text(draw, xy, s, font, fill, anchor='la'):
    if font:
        draw.text(xy, str(s), font=font, fill=fill, anchor=anchor)


def _fmt_rank(n):
    if n is None:
        return None
    n = int(n)
    if n >= 1_000_000:
        return f'{n/1_000_000:.2f}M'
    if n >= 10_000:
        return f'{n/1000:.0f}K'
    if n >= 1000:
        return f'{n/1000:.1f}K'
    return f'{n:,}'


def build_squad_poster(gameweek, starters, subs, captain_row, vice_row,
                       history=None, squad_cost=None, overall_rank=None,
                       total_points=None, out_path='/tmp/squad.png'):
    """Render the squad to a PNG. Returns the path, or None if Pillow is absent."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[poster] Pillow not available — skipping the image. "
              "Attach the Pillow layer to enable it.")
        return None

    try:
        img = _gradient((POSTER_W, POSTER_H)).convert('RGBA')

        # ImageDraw writes pixels rather than blending them, so every
        # translucent shape goes onto its own transparent layer first. Text is
        # drawn afterwards, on the composited result.
        shapes = Image.new('RGBA', (POSTER_W, POSTER_H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shapes, 'RGBA')

        class _Deferred:
            def __init__(self):
                self.calls = []

            def text(self, *a, **k):
                self.calls.append((a, k))
        d = _Deferred()

        f_eyebrow = _font('JetBrainsMono', 19, 'Medium')
        f_title = _font('SpaceGrotesk', 60, 'Bold')
        f_sub = _font('Inter', 22, 'Regular')
        f_name = _font('Inter', 24, 'SemiBold')
        f_club = _font('JetBrainsMono', 14, 'Regular')
        f_score = _font('SpaceGrotesk', 29, 'Bold')
        f_badge = _font('JetBrainsMono', 13, 'Bold')
        f_lbl = _font('JetBrainsMono', 15, 'Medium')
        f_stat_n = _font('SpaceGrotesk', 34, 'Bold')
        f_stat_l = _font('JetBrainsMono', 13, 'Regular')
        f_hist_n = _font('SpaceGrotesk', 28, 'Bold')
        f_foot = _font('JetBrainsMono', 15, 'Regular')

        M = 48
        y = 44

        _text(d, (M, y), 'PICKED BY THE BOT', f_eyebrow, C_MINT)
        y += 34
        _text(d, (M, y), f'Gameweek {gameweek}', f_title, C_WHITE)
        y += 74
        _text(d, (M, y), "Every number is the bot's projection for this gameweek.",
              f_sub, C_LAV)
        y += 44

        # ---- headline stats -------------------------------------------------
        # Before any gameweek is played there is no rank or points total, and a
        # first-season manager has no previous season to fall back on either.
        # Those cards are simply left out rather than filled with a placeholder.
        stats = []
        if total_points is not None:
            stats.append((f'{int(total_points):,}', 'TOTAL POINTS'))
        rank_s = _fmt_rank(overall_rank)
        if rank_s:
            stats.append((rank_s, 'OVERALL RANK'))
        if squad_cost is not None:
            stats.append((f'£{squad_cost/10:.1f}m', 'SQUAD VALUE'))

        if stats:
            sw = (POSTER_W - 2 * M - (len(stats) - 1) * 12) / len(stats)
            for i, (val, lab) in enumerate(stats):
                x0 = M + i * (sw + 12)
                box = (x0, y, x0 + sw, y + 92)
                _card(sd, box, fill=(255, 255, 255, 16), outline=(0, 255, 135, 55), r=14)
                sd.rounded_rectangle((x0, y, x0 + 4, y + 92), radius=2,
                                     fill=(0, 255, 135, 190))
                _text(d, (x0 + sw / 2, y + 34), val, f_stat_n, C_WHITE, anchor='mm')
                _text(d, (x0 + sw / 2, y + 68), lab, f_stat_l, C_LAV_D, anchor='mm')
            y += 92 + 20

        # ---- history strip --------------------------------------------------
        hist = [h for h in (history or []) if h.get('points') is not None]
        if hist:
            _text(d, (M, y), 'POINTS BY GAMEWEEK', f_lbl, C_LAV_D)
            y += 26
            strip_h = 76
            show = hist[-9:]
            cell = (POSTER_W - 2 * M) / max(len(show), 1)
            for i, h in enumerate(show):
                x0 = M + i * cell
                _card(sd, (x0 + 3, y, x0 + cell - 3, y + strip_h),
                      fill=(255, 255, 255, 13), outline=(255, 255, 255, 30), r=10)
                cx = x0 + cell / 2
                _text(d, (cx, y + 26), h['points'], f_hist_n, C_WHITE, anchor='mm')
                _text(d, (cx, y + 55), f"GW{h['event']}", f_stat_l, C_LAV_D, anchor='mm')
            y += strip_h + 24

        # ---- pitch ----------------------------------------------------------
        cap_id, vice_id = int(captain_row['id']), int(vice_row['id'])
        lineup_key = 'score_now' if 'score_now' in starters.columns else 'score'
        bench_bottom = POSTER_H - 132
        pitch_top, pitch_bot = y, bench_bottom - 122

        _card(sd, (M, pitch_top, POSTER_W - M, pitch_bot),
              fill=(0, 255, 135, 7), outline=(0, 255, 135, 38), r=20)

        # --- backdrop -------------------------------------------------------
        # A pitch drawn in perspective, narrower at the top, so the empty space
        # either side of the goalkeeper and forwards reads as depth rather than
        # as a gap. Everything here stays under alpha 22 so the player cards
        # remain the thing your eye lands on.
        ph = pitch_bot - pitch_top
        inset_top, inset_bot = 148, 26
        cxm = POSTER_W / 2

        def _edge(t, side):
            """x of a touchline at vertical fraction t (0 = far end)."""
            inset = inset_top + (inset_bot - inset_top) * t
            half = (POSTER_W - 2 * M) / 2 - inset
            return cxm + (half if side > 0 else -half)

        bands = 7
        for i in range(0, bands, 2):
            t0, t1 = i / bands, (i + 1) / bands
            y0, y1 = pitch_top + ph * t0, pitch_top + ph * t1
            sd.polygon([(_edge(t0, -1), y0), (_edge(t0, 1), y0),
                        (_edge(t1, 1), y1), (_edge(t1, -1), y1)],
                       fill=(255, 255, 255, 6))

        sd.line([(_edge(0, -1), pitch_top), (_edge(1, -1), pitch_bot)],
                fill=(255, 255, 255, 20), width=1)
        sd.line([(_edge(0, 1), pitch_top), (_edge(1, 1), pitch_bot)],
                fill=(255, 255, 255, 20), width=1)
        sd.line([(_edge(0.5, -1), pitch_top + ph * 0.5),
                 (_edge(0.5, 1), pitch_top + ph * 0.5)],
                fill=(255, 255, 255, 16), width=1)

        for w_frac, t_end, alpha in ((0.42, 0.17, 16), (0.17, 0.055, 20)):
            hw = ((_edge(0, 1) - _edge(0, -1)) / 2) * w_frac
            ye = pitch_top + ph * t_end
            sd.line([(cxm - hw, pitch_top), (cxm - hw, ye)], fill=(255, 255, 255, alpha), width=1)
            sd.line([(cxm + hw, pitch_top), (cxm + hw, ye)], fill=(255, 255, 255, alpha), width=1)
            sd.line([(cxm - hw, ye), (cxm + hw, ye)], fill=(255, 255, 255, alpha), width=1)

        my = pitch_top + ph * 0.5
        crx = ((_edge(0.5, 1) - _edge(0.5, -1)) / 2) * 0.30
        sd.ellipse((cxm - crx, my - crx * 0.34, cxm + crx, my + crx * 0.34),
                   outline=(255, 255, 255, 14), width=1)

        rows = [starters[starters.element_type == p].sort_values(lineup_key,
                ascending=False) for p in (1, 2, 3, 4)]
        rows = [(p, r) for p, r in zip((1, 2, 3, 4), rows) if len(r)]
        band_h = (pitch_bot - pitch_top - 34) / len(rows)

        for bi, (pos, band) in enumerate(rows):
            cy = pitch_top + 17 + band_h * bi + band_h / 2
            n = len(band)
            cw, gap = 158, 12
            x = (POSTER_W - (n * cw + (n - 1) * gap)) / 2
            for r in band.itertuples():
                pid = int(r.id)
                is_cap, is_vice = pid == cap_id, pid == vice_id
                box = (x, cy - 48, x + cw, cy + 48)
                if is_cap:
                    _glow(sd, box, (0, 255, 135, 110), r=14)
                    _card(sd, box, fill=(0, 255, 135, 34), outline=(0, 255, 135, 190),
                          r=14, width=2)
                elif is_vice:
                    _glow(sd, box, (255, 194, 77, 80), r=14)
                    _card(sd, box, fill=(255, 194, 77, 26), outline=(255, 194, 77, 150),
                          r=14, width=2)
                else:
                    _card(sd, box, fill=(255, 255, 255, 19), outline=(255, 255, 255, 42), r=14)
                    # a soft top edge gives the flat cards some lift
                    sd.line([(x + 14, cy - 48), (x + cw - 14, cy - 48)],
                            fill=(255, 255, 255, 55), width=1)
                cx = x + cw / 2
                if is_cap or is_vice:
                    col = C_MINT if is_cap else C_AMBER
                    sd.rounded_rectangle((cx - 27, cy - 60, cx + 27, cy - 41),
                                         radius=9, fill=col)
                    _text(d, (cx, cy - 51), 'CAPT' if is_cap else 'VICE', f_badge,
                          (10, 46, 27) if is_cap else (58, 38, 0), anchor='mm')
                _text(d, (cx, cy - 26), r.web_name,
                      _fit_font(r.web_name, cw - 18) or f_name, C_WHITE, anchor='mm')
                _text(d, (cx, cy - 1), getattr(r, 'team_name', '')[:14].upper(),
                      f_club, C_LAV_D, anchor='mm')
                _text(d, (cx, cy + 29), f'{getattr(r, lineup_key):.2f}', f_score,
                      C_AMBER if is_vice else C_MINT, anchor='mm')
                x += cw + gap

        # ---- bench ----------------------------------------------------------
        by = pitch_bot + 20
        _text(d, (POSTER_W / 2, by), 'BENCH, IN ORDER', f_lbl, C_LAV_D, anchor='ma')
        by += 26
        bench = subs.sort_values(lineup_key, ascending=False)
        n = len(bench)
        cw, gap = 158, 12
        x = (POSTER_W - (n * cw + (n - 1) * gap)) / 2
        for i, r in enumerate(bench.itertuples()):
            _card(sd, (x, by, x + cw, by + 72), fill=(255, 255, 255, 10),
                  outline=(255, 255, 255, 26), r=12)
            cx = x + cw / 2
            _text(d, (cx, by + 24), r.web_name,
                  _fit_font(r.web_name, cw - 18) or f_name, C_LAV, anchor='mm')
            _text(d, (cx, by + 52), f'{getattr(r, lineup_key):.2f}', f_score,
                  C_LAV_D, anchor='mm')
            x += cw + gap

        # ---- footer ---------------------------------------------------------
        fy = POSTER_H - 62
        sd.line([(M, fy - 24), (POSTER_W - M, fy - 24)], fill=(255, 255, 255, 30), width=1)
        _text(d, (M, fy), 'AUTONOMOUS FPL SYSTEM', f_foot, C_MINT)
        _text(d, (POSTER_W - M, fy),
              'PYTHON · LINEAR PROGRAMMING · AWS LAMBDA', f_foot, C_LAV_D, anchor='ra')

        img = Image.alpha_composite(img, shapes)
        td = ImageDraw.Draw(img, 'RGBA')
        for args, kwargs in d.calls:
            td.text(*args, **kwargs)

        img.convert('RGB').save(out_path, 'PNG', optimize=True)
        print(f"[poster] Written to {out_path}")
        return out_path

    except Exception as e:
        print(f"[poster] Could not draw the image ({type(e).__name__}: {e}) — "
              "the email will send without it.")
        return None


def fetch_entry_summary(team_id):
    """Points per gameweek, plus overall rank and total.

    Rank is null until a gameweek completes, so before the season starts this
    falls back to last season's finishing position.
    """
    history, rank, total = [], None, None
    try:
        d = get(f'https://fantasy.premierleague.com/api/entry/{team_id}/history/')
        history = [{'event': h['event'], 'points': h['points']}
                   for h in d.get('current', [])]
        if d.get('current'):
            last = d['current'][-1]
            rank = last.get('overall_rank')
            total = last.get('total_points')
        else:
            print("[poster] No gameweeks played yet — leaving rank and total out.")
    except Exception as e:
        print(f"[poster] Could not read entry history ({e}).")
    return history, rank, total


def build_html_email(gameweek, automated, deadline_note, headline,
                     news_risks, incoming_risks, players_df,
                     transfer_lines, starters, subs, captain_row, vice_row,
                     chip_lines, news_applied):
    """Assemble the whole email. Every argument is optional except the squad."""
    accent = FPL_GREEN if automated else FPL_AMBER
    status = ('Your team has been updated' if automated
              else 'Action needed — nothing was changed')

    P = []
    P.append('<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">')
    P.append(
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
        f'sans-serif;max-width:600px;margin:0 auto;color:{INK};">')

    # --- header
    P.append(
        f'<div style="background:{FPL_PURPLE};padding:24px 22px;">'
        f'<div style="color:#FFF;font-size:24px;font-weight:700;letter-spacing:-.3px;">'
        f'Gameweek {gameweek}</div>'
        f'<div style="color:{FPL_PINK};font-size:15px;font-weight:600;margin-top:4px;">'
        f'{_h(status)}</div></div>')

    # --- lead
    P.append(
        f'<div style="padding:20px 22px;font-size:15px;line-height:1.55;">'
        f'{headline}<br><br><span style="color:{MUTED};">{_h(deadline_note)}</span></div>')

    # --- team news
    all_risks = dict(news_risks or {})
    all_risks.update(incoming_risks or {})
    if all_risks:
        P.append(_section_title('Team news'))
        names = dict(zip(players_df['id'], players_df['web_name']))
        rows = []
        for i, (pid, r) in enumerate(sorted(all_risks.items(),
                                            key=lambda kv: -kv[1]['risk'])):
            bg = '#FFFFFF' if i % 2 == 0 else ROW_ALT
            src = f" · {_h(r['source'])}" if r.get('source') else ''
            rows.append(
                f'<tr style="background:{bg};">'
                f'<td style="padding:9px 6px;font-weight:600;font-size:14px;'
                f'vertical-align:top;width:120px;">{_h(names.get(pid, pid))}</td>'
                f'<td style="padding:9px 6px;font-size:13px;color:{MUTED};'
                f'line-height:1.5;">{_h(r["reason"])}{src}</td>'
                f'<td style="padding:9px 6px;text-align:right;font-size:14px;'
                f'font-weight:600;color:{FPL_PINK};vertical-align:top;">'
                f'{int(r["risk"]*100)}%</td></tr>')
        P.append(f'<div style="padding:0 22px;"><table style="width:100%;'
                 f'border-collapse:collapse;">{"".join(rows)}</table></div>')
        P.append(_note('Risk of missing the game or being rotated. '
                       + ('These players were rated down before the squad was picked.'
                          if news_applied else
                          'Reporting only — nothing was changed because of this.')))

    # --- transfers
    P.append(_section_title(f'Transfers ({len(transfer_lines)})'
                            if transfer_lines else 'Transfers — none'))
    if transfer_lines:
        rows = []
        for i, t in enumerate(transfer_lines):
            bg = '#FFFFFF' if i % 2 == 0 else ROW_ALT
            colour = FPL_GREEN if t['gain'] >= 0 else MUTED
            rows.append(
                f'<tr style="background:{bg};">'
                f'<td style="padding:10px 6px;font-size:14px;line-height:1.5;">'
                f'<span style="color:{MUTED};">OUT</span> '
                f'<b>{_h(t["out"])}</b><br>'
                f'<span style="color:{MUTED};">IN</span> '
                f'<b>{_h(t["in"])}</b>'
                f'<div style="color:{MUTED};font-size:13px;margin-top:4px;">'
                f'{_h(t["why"])}</div></td>'
                f'<td style="padding:10px 6px;text-align:right;font-size:15px;'
                f'font-weight:700;color:{colour};vertical-align:top;white-space:nowrap;">'
                f'{t["gain"]:+.2f}</td></tr>')
        P.append(f'<div style="padding:0 22px;"><table style="width:100%;'
                 f'border-collapse:collapse;">{"".join(rows)}</table></div>')
        total = sum(t['gain'] for t in transfer_lines)
        P.append(_note(f'Projected gain: {total:+.2f} points a week.'))
    else:
        P.append(_note('Nothing cleared the threshold this week, so the squad stands.'))

    # --- starting XI
    shape = starters['element_type'].value_counts()
    formation = (f"{int(shape.get(2,0))}-{int(shape.get(3,0))}-{int(shape.get(4,0))}")
    P.append(_section_title(f'Starting XI  ·  {formation}'))
    lineup_key = 'score_now' if 'score_now' in starters.columns else 'score'
    ordered = starters.sort_values(['element_type', lineup_key], ascending=[True, False])
    P.append(
        f'<div style="padding:0 22px;"><table style="width:100%;border-collapse:collapse;">'
        f'<tr style="border-bottom:2px solid {FPL_PINK};">'
        f'<th style="text-align:left;padding:6px;font-size:11px;color:{MUTED};'
        f'letter-spacing:.5px;">POS</th>'
        f'<th style="text-align:left;padding:6px;font-size:11px;color:{MUTED};'
        f'letter-spacing:.5px;">PLAYER</th>'
        f'<th style="text-align:right;padding:6px;font-size:11px;color:{MUTED};">PRICE</th>'
        f'<th style="text-align:right;padding:6px;font-size:11px;color:{MUTED};">THIS GW</th></tr>'
        + _player_rows([r for _, r in ordered.iterrows()],
                       int(captain_row['id']), int(vice_row['id']),
                       score_key='score_now')
        + '</table></div>')

    # --- bench
    P.append(f'<div style="padding:18px 22px 4px;font-size:11px;color:{MUTED};'
             f'letter-spacing:.5px;">BENCH, IN ORDER</div>')
    bench = subs.sort_values(lineup_key, ascending=False)
    P.append(f'<div style="padding:0 22px;"><table style="width:100%;'
             f'border-collapse:collapse;">'
             + _player_rows([r for _, r in bench.iterrows()],
                             score_key='score_now') + '</table></div>')

    # --- chips
    if chip_lines:
        P.append(_section_title('Chips'))
        rows = []
        for i, c in enumerate(chip_lines):
            bg = '#FFFFFF' if i % 2 == 0 else ROW_ALT
            mark = ('<span style="color:%s;font-weight:700;">CONSIDER</span>' % FPL_GREEN
                    if c['recommend'] else
                    '<span style="color:%s;">skip</span>' % MUTED)
            rows.append(
                f'<tr style="background:{bg};">'
                f'<td style="padding:9px 6px;font-weight:600;font-size:14px;'
                f'vertical-align:top;width:110px;">{_h(c["name"])}</td>'
                f'<td style="padding:9px 6px;font-size:13px;color:{MUTED};'
                f'line-height:1.5;">{_h(c["reason"])}</td>'
                f'<td style="padding:9px 6px;text-align:right;font-size:12px;'
                f'vertical-align:top;white-space:nowrap;">{mark}</td></tr>')
        P.append(f'<div style="padding:0 22px;"><table style="width:100%;'
                 f'border-collapse:collapse;">{"".join(rows)}</table></div>')
        P.append(_note('Chips are flagged, never played automatically.'))

    # --- how it works
    P.append(_section_title('How the scores are worked out'))
    P.append(
        f'<div style="padding:0 22px;font-size:14px;line-height:1.7;color:#2C1F33;">'
        f'<ol style="margin:4px 0;padding-left:20px;">'
        f'<li>Every player gets an expected points figure per fixture, from a '
        f'regression on points, goal involvement and ICT per 90 minutes, fitted on '
        f'874 player-seasons.</li>'
        f'<li>That is multiplied by how reliably they actually start, and by their '
        f'fixtures. A blank gameweek scores zero, a double counts twice.</li>'
        f'<li>Two views come out of it. Who to <b>own</b> is judged over the next '
        f'{FIXTURE_HORIZON} gameweeks, because transfers are scarce and a player is '
        f'bought for a run. Who to <b>start</b> is judged on this gameweek alone.</li>'
        f'<li>Linear programming then picks the best legal 15 within budget, at most '
        f'three players per club.</li></ol></div>')

    P.append(
        f'<div style="padding:18px 22px 26px;font-size:12px;color:{MUTED};'
        f'line-height:1.6;border-top:1px solid {RULE};margin-top:18px;">'
        f'Not included: price changes, and how explosive a player is rather than '
        f'how consistent — the model scores averages, so it cannot tell a reliable '
        f'six from a volatile one.<br>'
        f'Data: the official Fantasy Premier League API, last season and pre-season '
        f'records collected once, and team news read at the deadline.</div>')

    P.append('</div>')
    return ''.join(P)


def send_advice_email(subject_suffix, body, html=None, image_path=None):
    """Send the summary. HTML when we have it, plain text either way.

    The squad poster is attached when it rendered, so it can be saved and
    posted straight from the phone.
    """
    sender = os.environ.get('SMTP_EMAIL')
    app_password = os.environ.get('SMTP_APP_PASSWORD')
    recipient = os.environ.get('NOTIFY_EMAIL')

    if not (sender and app_password and recipient):
        print("SMTP env vars not set — skipping email. Body follows:\n")
        print(body)
        return

    if html or image_path:
        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(body, 'plain', 'utf-8'))
        if html:
            alt.attach(MIMEText(html, 'html', 'utf-8'))
        if image_path and os.path.exists(image_path):
            msg = MIMEMultipart('mixed')
            msg.attach(alt)
            try:
                with open(image_path, 'rb') as fh:
                    part = MIMEImage(fh.read(), name=os.path.basename(image_path))
                part.add_header('Content-Disposition', 'attachment',
                                filename=os.path.basename(image_path))
                msg.attach(part)
            except Exception as e:
                print(f"Could not attach the squad image: {e}")
        else:
            msg = alt
    else:
        msg = MIMEText(body, 'plain', 'utf-8')

    msg['Subject'] = f"FPL Bot: {subject_suffix}"
    msg['From'] = sender
    msg['To'] = recipient

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, app_password)
            server.send_message(msg)
        print("Advice email sent.")
    except Exception as e:
        print(f"Could not send email: {e}")


# ---------------------------------------------------------------------------
# HYBRID MODE
#
# The bot tries to do everything itself. If it can't authenticate, it falls
# back to emailing you the recommendation so you can make the change manually.
#
# FPL replaced its old login (users.premierleague.com, now dead) with an OAuth2
# identity provider, so email+password submission no longer works. The way
# round it is a session bearer token: you log in through the browser, copy the
# token, and the bot reuses it until it expires (8 hours).
#
# SETUP (one-off, takes 2 minutes):
#   1. Log in at https://fantasy.premierleague.com in Chrome.
#   2. F12 -> Network tab -> refresh the page.
#   3. Click any request to fantasy.premierleague.com.
#   4. Right-click the request -> Copy -> Copy as cURL.
#   5. Find the line starting  -H 'X-API-Authorization: Bearer ey...'
#      and copy everything after "Bearer " (the long ey... string).
#   6. Lambda -> Configuration -> Environment variables -> add
#         FPL_TOKEN = <that string>
#
# TOKENS LAST 8 HOURS. Refresh it on deadline day, before the bot runs.
#
# When the token expires the bot emails you advice instead of silently
# failing, and tells you to refresh it. Nothing breaks.
# ---------------------------------------------------------------------------


def token_expiry_hours(token):
    """Hours until the bearer token expires, or None if it can't be read."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        remaining = claims['exp'] - datetime.now(timezone.utc).timestamp()
        return remaining / 3600
    except Exception:
        return None


def build_authenticated_session(team_id):
    """Return (session, my_team_json) if the stored bearer token still works.

    Returns (None, None) if there is no token or it has expired — the caller
    then falls back to advisory mode rather than failing the run.
    """
    token = os.environ.get('FPL_TOKEN', '').strip()
    if not token:
        print("[auth] No FPL_TOKEN set — advisory mode.")
        return None, None

    if token.lower().startswith('bearer '):
        token = token[7:].strip()

    hours = token_expiry_hours(token)
    if hours is not None:
        if hours <= 0:
            print(f"[auth] Token expired {abs(hours):.1f}h ago — advisory mode. "
                  "Paste a fresh FPL_TOKEN to restore automation.")
            return None, None
        print(f"[auth] Token valid for another {hours:.1f}h.")

    session = requests.session()
    session.headers.update(BROWSER_HEADERS)
    session.headers['X-API-Authorization'] = f'Bearer {token}'
    session.headers['X-API-Language'] = 'en'

    url = f'https://fantasy.premierleague.com/api/my-team/{team_id}/'
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"[auth] Could not reach my-team endpoint: {type(e).__name__}: {e}")
        return None, None

    if resp.status_code in (401, 403):
        print(f"[auth] Token rejected (HTTP {resp.status_code}) — advisory mode. "
              "Paste a fresh FPL_TOKEN to restore automation.")
        return None, None

    if resp.status_code != 200:
        print(f"[auth] my-team returned HTTP {resp.status_code} — advisory mode. "
              f"Body starts: {resp.text[:150]!r}")
        return None, None

    try:
        data = resp.json()
    except ValueError:
        print("[auth] my-team returned non-JSON — advisory mode.")
        return None, None

    if 'picks' not in data:
        print(f"[auth] Authenticated but no picks returned (keys: {list(data.keys())}). "
              "Check FPL_TEAM_ID matches the account. Advisory mode.")
        return None, None

    print(f"[auth] Token valid — {len(data['picks'])} picks read. Automation active.")
    return session, data


def post_checked(session, url, label, **kwargs):
    """POST and raise a clear error if FPL didn't accept it."""
    kwargs.setdefault('timeout', REQUEST_TIMEOUT)
    try:
        resp = session.post(url=url, **kwargs)
    except requests.RequestException as e:
        raise Exception(f"{label} request failed: {type(e).__name__}: {e}")

    # Any 2xx is a success. FPL returns 202 Accepted for lineup changes and
    # echoes the updated team back, which is not a rejection.
    if not (200 <= resp.status_code < 300):
        raise Exception(f"{label} was REJECTED by FPL — HTTP {resp.status_code}. "
                        f"Response: {resp.text[:300]!r}")

    print(f"[{label}] accepted by FPL (HTTP {resp.status_code}).")
    return resp


def submit_lineup(session, team_id, starters, subs, captain_id, vice_captain_id):
    """Submit the XI, bench order, captain and vice-captain.

    FPL's position rules are strict:
      1        starting goalkeeper
      2-11     the ten outfield starters
      12       the SUBSTITUTE GOALKEEPER — this slot is reserved for a keeper
      13-15    outfield substitutes, in the order they should come on

    Putting a keeper anywhere in 13-15, or an outfielder at 12, is rejected
    with "Sub-position not allowed for element type".
    """
    picks, position = [], 1

    # 1-11: starters, goalkeeper first
    for pos in range(1, 5):
        for pid in starters.loc[starters.element_type == pos].id.tolist():
            picks.append({"element": int(pid), "is_captain": pid == captain_id,
                          "is_vice_captain": pid == vice_captain_id,
                          "position": position})
            position += 1

    if position != 12:
        raise Exception(f"Expected 11 starters, built {position - 1}. Not submitting.")

    # 12: the substitute keeper, whatever they scored
    bench_gk = subs.loc[subs.element_type == 1]
    if len(bench_gk) != 1:
        raise Exception(f"Expected exactly 1 bench goalkeeper, found {len(bench_gk)}. "
                        "Not submitting.")
    picks.append({"element": int(bench_gk.id.iat[0]), "is_captain": False,
                  "is_vice_captain": False, "position": 12})
    position = 13

    # 13-15: outfield subs, best first
    outfield = subs.loc[subs.element_type != 1].sort_values('score', ascending=False)
    for pid in outfield.id.tolist():
        picks.append({"element": int(pid), "is_captain": False,
                      "is_vice_captain": False, "position": position})
        position += 1

    if len(picks) != 15:
        raise Exception(f"Built {len(picks)} picks, expected 15. Not submitting.")

    headers = {
        'content-type': 'application/json',
        'origin': 'https://fantasy.premierleague.com',
        'referer': 'https://fantasy.premierleague.com/my-team',
    }
    post_checked(session, f'https://fantasy.premierleague.com/api/my-team/{team_id}/',
                 label="Lineup", json={"picks": picks, "chip": None}, headers=headers)


def squad_signature(ids):
    """Stable fingerprint of a squad, so we only email when it actually changes."""
    return ",".join(str(i) for i in sorted(int(x) for x in ids))


def last_squad_signature():
    try:
        import boto3
        ssm = boto3.client('ssm')
        resp = ssm.get_parameter(Name='/fpl-bot/last-squad')
        return resp['Parameter']['Value']
    except Exception:
        return None


def save_squad_signature(sig):
    try:
        import boto3
        ssm = boto3.client('ssm')
        ssm.put_parameter(Name='/fpl-bot/last-squad', Value=sig,
                          Type='String', Overwrite=True)
    except Exception as e:
        print(f"Could not save squad signature: {e}")


def describe_player(row):
    """One plain-English line explaining what the numbers say about a player.

    The score alone tells a reader nothing. This turns it into the two or
    three facts that actually drove it.
    """
    bits = []

    snap = LAST_SEASON.get(int(row['id']))
    mins = float(pd.to_numeric(row.get('minutes', 0), errors='coerce') or 0)
    if mins < 300 and snap:
        mins, starts, pts, xgi, ict = snap
        season = ""
    elif snap and mins < 300:
        starts = 0; pts = 0; xgi = 0; ict = 0; season = ""
    else:
        starts = float(pd.to_numeric(row.get('starts', 0), errors='coerce') or 0)
        pts = float(pd.to_numeric(row.get('total_points', 0), errors='coerce') or 0)
        xgi = float(pd.to_numeric(row.get('expected_goal_involvements', 0), errors='coerce') or 0)
        season = ""

    # how reliably they play
    if mins <= 0:
        bits.append("no Premier League minutes yet, so rated on price")
    else:
        if starts >= 30:
            bits.append(f"a regular starter ({int(starts)} starts)")
        elif starts >= 15:
            bits.append(f"starts most weeks ({int(starts)} starts)")
        elif starts >= 5:
            bits.append(f"rotated ({int(starts)} starts)")
        else:
            bits.append(f"barely plays ({int(starts)} starts)")

        if mins > 0:
            per90 = pts / (mins / 90)
            if per90 >= 5.5:
                bits.append(f"scores heavily when he plays ({per90:.1f} pts per 90)")
            elif per90 >= 4.0:
                bits.append(f"solid returns ({per90:.1f} pts per 90)")
            elif per90 >= 2.5:
                bits.append(f"modest returns ({per90:.1f} pts per 90)")
            else:
                bits.append(f"low returns ({per90:.1f} pts per 90)")

            xgi90 = xgi / (mins / 90) if mins else 0
            if xgi90 >= 0.55:
                bits.append(f"heavily involved in goals ({xgi90:.2f} xGI per 90)")
            elif xgi90 >= 0.3:
                bits.append(f"chips in with goals and assists ({xgi90:.2f} xGI per 90)")

    # fixtures
    fc = int(pd.to_numeric(row.get('fixture_count', 1), errors='coerce') or 0)
    if fc == 0:
        bits.append("blank gameweek — no fixture at all")
    elif fc > 1:
        bits.append(f"a double gameweek ({fc} fixtures)")

    diff = float(pd.to_numeric(row.get('diff', 0), errors='coerce') or 0)
    if diff >= 10:
        bits.append("a favourable fixture")
    elif diff <= -10:
        bits.append("a tough fixture")

    status = str(row.get('status', 'a'))
    if status == 'd':
        bits.append("carrying a fitness doubt")
    elif status in ('i', 's', 'u', 'n'):
        bits.append("unavailable — injured, suspended or out of the squad")

    return "; ".join(bits) if bits else "no distinguishing data"


def explain_swap(player_out_row, player_in_row, gain):
    """Two lines of plain English for one transfer, plus a verdict."""
    out_name = player_out_row['web_name']
    in_name = player_in_row['web_name']

    lines = [f"  OUT  {out_name}  ({float(player_out_row['score']):.2f})",
             f"       {describe_player(player_out_row)}",
             f"  IN   {in_name}  ({float(player_in_row['score']):.2f})",
             f"       {describe_player(player_in_row)}"]

    # why, in one sentence
    reasons = []
    o_fc = int(pd.to_numeric(player_out_row.get('fixture_count', 1), errors='coerce') or 0)
    i_fc = int(pd.to_numeric(player_in_row.get('fixture_count', 1), errors='coerce') or 0)
    if i_fc > o_fc:
        reasons.append(f"{in_name} plays {i_fc} time(s) this week against {o_fc}")
    if str(player_out_row.get('status', 'a')) != 'a':
        reasons.append(f"{out_name} is not fully available")
    if not reasons:
        o_min = float(pd.to_numeric(player_out_row.get('minutes', 0), errors='coerce') or 0)
        i_min = float(pd.to_numeric(player_in_row.get('minutes', 0), errors='coerce') or 0)
        o_st = LAST_SEASON.get(int(player_out_row['id']), [o_min, 0, 0, 0, 0])[1]
        i_st = LAST_SEASON.get(int(player_in_row['id']), [i_min, 0, 0, 0, 0])[1]
        if i_st > o_st + 8:
            reasons.append(f"{in_name} is far more likely to start")
        else:
            reasons.append(f"{in_name} produces more per game")

    out_cost = float(pd.to_numeric(player_out_row.get('now_cost', 0), errors='coerce') or 0)
    in_cost = float(pd.to_numeric(player_in_row.get('now_cost', 0), errors='coerce') or 0)

    if gain < 0 and in_cost < out_cost:
        freed = (out_cost - in_cost) / 10
        lines.append(f"       -> a downgrade on paper ({gain:+.2f} a week), but it frees "
                     f"GBP{freed:.1f}m to strengthen elsewhere. The squad as a whole "
                     f"comes out ahead.")
    elif gain < 0:
        lines.append(f"       -> {gain:+.2f} a week on its own; taken to keep the rest "
                     f"of the squad legal on budget and club limits.")
    else:
        verdict = " and ".join(reasons)
        lines.append(f"       -> {verdict}, worth about {gain:+.2f} points a week.")
    return "\n".join(lines)




LLM_NEWS_COOLDOWN_HOURS = 2        # minimum gap between team-news calls


def news_check_is_worthwhile(should_update, gameweek):
    """Should we spend money on a team-news call right now?

    Two gates, both about not paying for nothing:

    1. Team news does not exist until the deadline is close. Press conferences
       land in the last day or two. Searching 13 days out returns nothing and
       still costs $0.05 a time.
    2. On the unlimited-transfer path the bot runs every two hours with no
       once-per-gameweek guard, so without a cooldown it would call on every
       single run.
    """
    if not should_update:
        print("[news] Deadline not close enough for team news to exist — skipping.")
        return False

    param = '/fpl-bot/last-news-check'
    now = datetime.now(timezone.utc)
    try:
        import boto3
        ssm = boto3.client('ssm')
        try:
            raw = ssm.get_parameter(Name=param)['Parameter']['Value']
            last_gw, last_time = raw.split('|')
            hours = (now - _as_utc(datetime.fromisoformat(last_time))).total_seconds() / 3600
            if int(last_gw) == int(gameweek) and hours < LLM_NEWS_COOLDOWN_HOURS:
                print(f"[news] Checked {hours:.1f}h ago for GW{gameweek} "
                      f"(cooldown {LLM_NEWS_COOLDOWN_HOURS}h) — skipping.")
                return False
        except ssm.exceptions.ParameterNotFound:
            pass
        ssm.put_parameter(Name=param, Value=f"{gameweek}|{now.isoformat()}",
                          Type='String', Overwrite=True)
    except Exception as e:
        print(f"[news] Could not check the cooldown ({e}) — proceeding.")
    return True


LLM_INCOMING_MAX_SEARCHES = 2      # incoming is 1-3 players, so fewer clubs to check


def check_incoming(incoming_ids, players_df, gameweek, wanted=True):
    """Team news on players about to be BOUGHT.

    The squad check happens before transfers are planned, so it can't cover
    anyone arriving. Buying a player who turns out to be rested wastes the
    transfer outright, which is worse than keeping a mediocre one.

    Returns (risks, adjusted_players_df, section_text).
    """
    incoming_ids = [int(i) for i in incoming_ids]
    if not incoming_ids or not wanted:
        return {}, players_df, ""

    incoming = players_df[players_df['id'].isin(incoming_ids)]
    risks = fetch_minutes_risk(incoming, gameweek,
                               max_searches=LLM_INCOMING_MAX_SEARCHES,
                               label='incoming')
    if not risks:
        return {}, players_df, ""

    if LLM_TEAM_NEWS_ENABLED:
        print(f"[news/incoming] {len(risks)} incoming player(s) flagged — "
              "re-planning so the transfer isn't wasted.")
        players_df = apply_minutes_risk(players_df, risks)
    else:
        print(f"[news/incoming] {len(risks)} incoming player(s) flagged, but the "
              "layer is set to report only — the transfer stands.")

    names = dict(zip(players_df['id'], players_df['web_name']))
    lines = ["TEAM NEWS ON INCOMING PLAYERS", "-" * 52]
    for pid, r in sorted(risks.items(), key=lambda kv: -kv[1]['risk']):
        lines.append(f"  {names.get(pid, pid)} — {int(r['risk']*100)}% risk of "
                     "missing or being rotated")
        lines.append(f"      {r['reason']}" + (f"  ({r['source']})" if r['source'] else ""))
    lines.append("")
    lines.append("These transfers were reconsidered." if LLM_TEAM_NEWS_ENABLED
                 else "Reporting only — the transfers below went ahead regardless.")
    return risks, players_df, "\n".join(lines)


def rebuild_squad_unlimited(session, team_id, gameweek, players_df, team_data,
                            test_mode=False, news_hook=None):
    """Rebuild the whole squad to the optimum. Used when transfers are free.

    Applies when FPL reports limit=None — before the GW1 deadline, and after a
    Wildcard or Free Hit. The one-in-one-out logic is wrong here: with no cost
    per transfer there is no reason to settle for a single swap.

    Returns (transfers_list, optimal_squad_df, budget).
    """
    picks = team_data['picks']
    selling = {int(p['element']): int(p.get('selling_price',
               p.get('purchase_price', 0))) for p in picks}
    current_ids = set(selling)
    bank = team_data.get('transfers', {}).get('bank', 0)
    budget = bank + sum(selling.values())

    print(f"[rebuild] Unlimited transfers — optimising the full squad. "
          f"Budget £{budget/10:.1f}m (bank £{bank/10:.1f}m + squad £{sum(selling.values())/10:.1f}m).")

    optimal = build_suggested_squad(players_df, budget)
    if optimal is None:
        print("[rebuild] Optimizer returned nothing — leaving the squad alone.")
        return [], None, budget

    optimal_ids = set(int(i) for i in optimal.id)
    out_ids = current_ids - optimal_ids
    in_ids = optimal_ids - current_ids

    if not out_ids:
        print("[rebuild] Squad is already optimal — no transfers needed.")
        return [], optimal, budget

    # Pair leavers with arrivals position by position. Both squads have the
    # same 2/5/5/3 shape, so the counts match within each position.
    lookup = players_df.set_index('id', drop=False)
    transfers = []
    for pos in (1, 2, 3, 4):
        outs = [i for i in out_ids if int(lookup.at[i, 'element_type']) == pos]
        ins = [i for i in in_ids if int(lookup.at[i, 'element_type']) == pos]
        # Pair worst-leaving with best-arriving. Sorting by id instead makes
        # individual pairs look nonsensical (a good player "swapped" for a
        # worse one) even though the overall squad improves.
        outs.sort(key=lambda i: float(lookup.at[i, 'score']))
        ins.sort(key=lambda i: float(lookup.at[i, 'score']), reverse=True)
        for o, n in zip(outs, ins):
            transfers.append({
                "element_in": int(n),
                "element_out": int(o),
                "purchase_price": int(lookup.at[n, 'now_cost']),
                "selling_price": int(selling[o]),
            })

    # Check the arrivals before anything is submitted. A rebuild can bring in
    # a dozen players, so an unchecked one is a dozen chances to buy a dud.
    if news_hook is not None and transfers:
        adjusted = news_hook([t['element_in'] for t in transfers], players_df)
        if adjusted is not None:
            return rebuild_squad_unlimited(session, team_id, gameweek, adjusted,
                                           team_data, test_mode, news_hook=None)

    print(f"[rebuild] {len(transfers)} transfer(s) to reach the optimum.")
    for t in transfers:
        print(f"    OUT {lookup.at[t['element_out'], 'web_name']:<18} "
              f"IN {lookup.at[t['element_in'], 'web_name']:<18} "
              f"({lookup.at[t['element_in'], 'score'] - lookup.at[t['element_out'], 'score']:+.2f})")

    if not test_mode and transfers:
        headers = {
            'content-type': 'application/json',
            'origin': 'https://fantasy.premierleague.com',
            'referer': 'https://fantasy.premierleague.com/transfers',
        }
        payload = {"transfers": transfers, "chip": None,
                   "entry": int(team_id), "event": int(gameweek)}
        post_checked(session, 'https://fantasy.premierleague.com/api/transfers/',
                     label="Bulk transfer", data=json.dumps(payload), headers=headers)

    return transfers, optimal, budget


def format_rebuild_section(transfers, players_df, budget):
    lookup = players_df.set_index('id', drop=False)
    lines = [f"FULL SQUAD REBUILD  (unlimited free transfers, £{budget/10:.1f}m budget)",
             "-" * 52]
    if not transfers:
        lines.append("Your squad is already optimal — nothing changed.")
        return "\n".join(lines)

    lines.append(f"{len(transfers)} transfer(s) made:")
    lines.append("")
    total = 0.0
    for t in transfers:
        out_row = lookup.loc[t['element_out']]
        in_row = lookup.loc[t['element_in']]
        gain = float(in_row['score']) - float(out_row['score'])
        total += gain
        lines.append(explain_swap(out_row, in_row, gain))
        lines.append("")
    lines.append("")
    lines.append(f"Total projected improvement: +{total:.2f} points.")
    lines.append("")
    lines.append("Transfers are free right now, so the whole squad was optimised")
    lines.append("rather than making a single swap. This will run again as the")
    lines.append("deadline approaches, once real team news is available.")
    return "\n".join(lines)


MAX_TRANSFERS_PER_GW = 5      # safety cap, matches FPL's free-transfer bank limit
MAX_HITS_PER_GW = 1           # how many -4 hits the bot may take in one gameweek


def plan_transfers(my_team, players_df, bank, free_transfers, made,
                   hit_cost, selling_prices=None):
    """Plan every worthwhile transfer, not just one.

    FPL lets you bank up to 5 free transfers. Making only one swap when three
    are free and all three clear the bar leaves points on the table.

    Each swap is evaluated against the squad as it stands after the previous
    one, so the budget and club limits stay correct as it goes. It stops as
    soon as the next-best swap fails to clear its threshold.

    Returns (transfers, squad, log_lines) where transfers is a list of
    (player_out, player_in, gain, was_free).
    """
    available_free = max(0, (free_transfers or 0) - (made or 0))
    selling_prices = selling_prices or {}

    squad = my_team.copy()
    transfers, log = [], []
    hits_taken = 0
    just_bought = set()

    log.append(f"{available_free} free transfer(s) available.")

    while len(transfers) < MAX_TRANSFERS_PER_GW:
        is_free = len(transfers) < available_free

        if not is_free and hits_taken >= MAX_HITS_PER_GW:
            log.append(f"Stopping: free transfers used and the {MAX_HITS_PER_GW}-hit "
                       "limit for this gameweek is reached.")
            break

        required = MIN_TRANSFER_GAIN if is_free else (hit_cost + MIN_TRANSFER_GAIN)

        # Never sell someone bought earlier in this same plan, but still count
        # them for club limits and the already-own check.
        sellable = squad.loc[~squad['id'].isin(just_bought)] if just_bought else squad
        if sellable.empty:
            break

        # pick_out_candidate is weighted-random, so one unlucky draw can land on
        # a player nobody affordable improves. Try a few before giving up rather
        # than ending the whole plan on a single miss.
        player_out = player_in = None
        gain = 0.0
        tried = set()
        for _ in range(TRANSFER_OUT_ATTEMPTS):
            cand_out, cand_in, cand_gain = choose_transfer(
                sellable, players_df, bank,
                selling_prices=selling_prices, full_squad=squad,
                exclude_out=tried)
            if cand_out is None:
                break
            tried.add(int(cand_out.id.iat[0]))
            if cand_in is not None and cand_gain > gain:
                player_out, player_in, gain = cand_out, cand_in, cand_gain
            if player_out is None:
                player_out = cand_out

        if player_in is None:
            log.append(f"Stopping: no legal replacement affordable after trying "
                       f"{len(tried)} outgoing candidate(s).")
            break

        if gain < required:
            label = "free transfer" if is_free else f"a -{hit_cost} hit"
            log.append(f"Stopping: best remaining swap "
                       f"({player_out.web_name.iat[0]} -> {player_in.web_name.iat[0]}) "
                       f"gains {gain:.2f}, below the {required:.2f} bar for {label}.")
            break

        out_id = int(player_out.id.iat[0])
        sell_price = selling_prices.get(out_id, int(player_out.now_cost.iat[0]))
        bank = bank + sell_price - int(player_in.now_cost.iat[0])
        if bank < 0:
            log.append(f"Stopping: that swap would leave the bank at "
                       f"GBP{bank/10:.1f}m, which FPL would reject.")
            break

        squad = squad.loc[squad['id'] != out_id]
        squad = pd.concat([squad, player_in])
        just_bought.add(int(player_in.id.iat[0]))

        transfers.append((player_out, player_in, gain, is_free))
        if not is_free:
            hits_taken += 1

        log.append(f"{'FREE' if is_free else f'-{hit_cost} HIT'}: "
                   f"{player_out.web_name.iat[0]} -> {player_in.web_name.iat[0]} "
                   f"(+{gain:.2f}), bank now GBP{bank/10:.1f}m")

    if len(transfers) == MAX_TRANSFERS_PER_GW:
        log.append(f"Stopping: hit the {MAX_TRANSFERS_PER_GW}-transfer safety cap.")

    return transfers, squad, log


def format_transfer_plan(transfers, log, hit_cost):
    """Email text for a multi-transfer gameweek."""
    if not transfers:
        lines = ["No transfer was worth making this gameweek.", ""]
        lines += log
        return "\n".join(lines)

    hits = sum(1 for t in transfers if not t[3])
    total_gain = sum(t[2] for t in transfers)
    net = total_gain - hits * hit_cost

    lines = [f"{len(transfers)} transfer(s) made "
             f"({len(transfers) - hits} free, {hits} on a hit).", ""]

    for player_out, player_in, gain, was_free in transfers:
        tag = "free" if was_free else f"-{hit_cost} hit"
        lines.append(f"  OUT {player_out.web_name.iat[0]:<18} "
                     f"IN {player_in.web_name.iat[0]:<18} +{gain:.2f}  ({tag})")
        lines.append(f"      out: {explain_player(player_out.iloc[0])}")
        lines.append(f"      in : {explain_player(player_in.iloc[0])}")
        lines.append("")

    lines.append(f"Projected gain: +{total_gain:.2f} points.")
    if hits:
        lines.append(f"Less {hits} x {hit_cost} pts for hits = net +{net:.2f} points.")
    lines.append("")
    lines += log
    return "\n".join(lines)


def submit_transfers_bulk(session, team_id, gameweek, transfers, selling_prices=None):
    """Send every planned transfer in a single request."""
    selling_prices = selling_prices or {}
    payload_transfers = []
    for player_out, player_in, _gain, _free in transfers:
        out_id = int(player_out.id.iat[0])
        payload_transfers.append({
            "element_in": int(player_in.id.iat[0]),
            "element_out": out_id,
            "purchase_price": int(player_in.now_cost.iat[0]),
            "selling_price": selling_prices.get(out_id, int(player_out.now_cost.iat[0])),
        })

    headers = {
        'content-type': 'application/json',
        'origin': 'https://fantasy.premierleague.com',
        'referer': 'https://fantasy.premierleague.com/transfers',
    }
    payload = {"transfers": payload_transfers, "chip": None,
               "entry": int(team_id), "event": int(gameweek)}
    post_checked(session, 'https://fantasy.premierleague.com/api/transfers/',
                 label=f"{len(payload_transfers)} transfer(s)",
                 data=json.dumps(payload), headers=headers)


# ---------------------------------------------------------------------------
# TEAM NEWS LAYER  (optional, costs money, off by default)
#
# The scoring model only sees numbers. It cannot know that a manager has said
# a player will be rested, that a signing hasn't featured in pre-season, or
# that someone is being eased back after a knock. FPL's `status` field only
# flags injuries and suspensions — a fit player who won't start still reads
# as fully available.
#
# This asks Claude to check current team news for the squad and return a
# "minutes risk" per player. The risk can only ever LOWER a player's rating,
# never raise it, so a wrong answer costs at most one good player.
#
# COST: web search is $10 per 1,000 searches. Searching by club rather than
# by player keeps it to ~3 searches per gameweek — roughly $1.30 a season.
#
# SETUP:
#   ANTHROPIC_API_KEY  environment variable in Lambda
#   LLM_TEAM_NEWS_ENABLED below: leave False until you trust its judgement.
#   While False it still runs and still reports, but changes nothing.
# ---------------------------------------------------------------------------

LLM_TEAM_NEWS_ENABLED = False      # False = report only. True = let it affect decisions.
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_MAX_SEARCHES = 3               # hard cap. Each search costs $0.01.
LLM_TIMEOUT = 45
LLM_MAX_RISK = 0.75                # never wipe a player out completely on news alone


def fetch_minutes_risk(squad_df, gameweek, max_searches=None, label='squad'):
    """Ask Claude for current team news on the squad.

    Returns {player_id: {"risk": float, "reason": str}}. Empty dict on any
    failure — the bot then behaves exactly as it does today.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        print("[news] No ANTHROPIC_API_KEY set — skipping team news check.")
        return {}

    if squad_df is None or len(squad_df) == 0:
        return {}
    max_searches = max_searches or LLM_MAX_SEARCHES

    # Group by club: one search covers every player at that club, and FPL
    # caps you at 3 per club, so 15 players is only ~8 clubs.
    # Include each player's actual record. Without it the model has no anchor
    # and will fill in plausible-sounding narratives — it once flagged a
    # 20-goal ever-present as "a new signing still building match fitness".
    by_club = {}
    for r in squad_df.itertuples():
        pid = int(r.id)
        snap = LAST_SEASON.get(pid)
        if snap:
            mins, starts, pts, _xgi, _ict = snap
            record = (f"{starts} starts, {mins} mins, {pts} FPL points last season")
        else:
            record = "no Premier League record — likely a new signing or promoted"
        by_club.setdefault(r.team_name, []).append(
            (pid, f"{r.web_name} (id {pid}) — {record}"))

    roster = "\n".join(
        f"{club}:\n" + "\n".join(f"  - {line}" for _, line in players)
        for club, players in sorted(by_club.items()))

    prompt = f"""You are checking Fantasy Premier League team news before a deadline.

For each player below, judge how likely they are to MISS or be heavily rotated
in Premier League Gameweek {gameweek}. You are looking for things statistics
cannot show:

- a manager saying the player will be rested or rotated
- a player not featuring in pre-season or returning late from injury
- a new signing not yet integrated or lacking match fitness
- a player recently dropped, or losing their place to someone else
- an imminent transfer, or a cup or European game affecting selection

The record shown for each player is fact, taken from official data. Do NOT
contradict it. A player with 30+ starts last season is an established regular,
not a new signing and not short of match fitness — if you are about to write
that, you are wrong, so leave them out instead.

Search by CLUB, not by player — one search covers every player at that club.
You may run at most {max_searches} searches, so prioritise the clubs where
news is most likely to matter. Use only what you find in search results; do not
answer from memory, because squad news goes stale within days.

Squad:
{roster}

Return ONLY a JSON object, no prose and no markdown fences. Include a player
ONLY if a search result explicitly said something about THAT player. If you did
not read it in a result, omit them — an empty list is a perfectly good answer
and far better than a guess. In "reason", state what the source actually said,
not your interpretation of it.

{{"players": [
  {{"id": 123, "risk": 0.6, "reason": "Guardiola confirmed he will be rested for the cup", "source": "bbc.co.uk"}}
]}}

risk is 0.0 to 1.0: 0.2 slight doubt, 0.5 likely rotated, 0.8 very unlikely to
start. Keep reason under 15 words. If you found nothing, return {{"players": []}}."""

    body = {
        "model": LLM_MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_searches,
        }],
    }

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            data=json.dumps(body), timeout=LLM_TIMEOUT)
    except requests.RequestException as e:
        print(f"[news] Request failed ({type(e).__name__}) — continuing without team news.")
        return {}

    if resp.status_code != 200:
        print(f"[news] HTTP {resp.status_code} — continuing without team news. "
              f"{resp.text[:200]!r}")
        return {}

    try:
        data = resp.json()
    except ValueError:
        print("[news] Non-JSON response — continuing without team news.")
        return {}

    searches = sum(1 for b in data.get('content', [])
                   if b.get('type') == 'server_tool_use')
    usage = data.get('usage', {})
    cost = (searches * 0.01
            + usage.get('input_tokens', 0) / 1e6 * 1.0
            + usage.get('output_tokens', 0) / 1e6 * 5.0)
    print(f"[news/{label}] {searches} search(es), {usage.get('input_tokens',0)} in / "
          f"{usage.get('output_tokens',0)} out tokens — about ${cost:.3f} this run.")

    text = "".join(b.get('text', '') for b in data.get('content', [])
                   if b.get('type') == 'text')
    text = text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end == -1:
        print("[news] No JSON found in the reply — continuing without team news.")
        return {}

    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError as e:
        print(f"[news] Could not parse the reply ({e}) — continuing without team news.")
        return {}

    valid_ids = set(int(i) for i in squad_df['id'])
    risks = {}
    for entry in parsed.get('players', []):
        try:
            pid = int(entry['id'])
            risk = float(entry['risk'])
        except (KeyError, TypeError, ValueError):
            continue
        if pid not in valid_ids:
            continue                      # ignore anyone not actually in the squad
        risk = max(0.0, min(LLM_MAX_RISK, risk))
        if risk <= 0:
            continue

        # Guard against the specific failure we've seen: claiming an
        # established regular is a new signing short of match fitness. If the
        # player started 25+ games last season, that reason is false by
        # definition, so drop it rather than act on it.
        reason = str(entry.get('reason', ''))
        snap = LAST_SEASON.get(pid)
        if snap and snap[1] >= 25:
            lowered = reason.lower()
            if any(p in lowered for p in ('new signing', 'newly signed', 'just joined',
                                          'recent arrival', 'late arrival', 'yet to settle',
                                          'building match fitness', 'lacks match fitness',
                                          'not integrated', 'adapting to')):
                print(f"[news] Rejected claim about player {pid}: "
                      f"{reason[:70]!r} — they started {snap[1]} games last season.")
                continue

        risks[pid] = {"risk": risk,
                      "reason": reason[:120],
                      "source": str(entry.get('source', ''))[:60]}
    return risks


def apply_minutes_risk(players_df, risks):
    """Lower the score of flagged players. Never raises anyone."""
    if not risks:
        return players_df
    df = players_df.copy()
    factor = df['id'].map(lambda i: 1.0 - risks.get(int(i), {}).get('risk', 0.0))
    df['base_points'] = df['base_points'] * factor
    df['score'] = (df['base_points'] * df['fixture_count']
                   + df['diff'] / 3
                   + df['status'].map(STATUS_PENALTY).fillna(-50))
    return df


def format_news_section(risks, players_df, applied):
    """Email text for what the team news check found."""
    header = ("TEAM NEWS CHECK" if applied
              else "TEAM NEWS CHECK  (reporting only — no effect on decisions yet)")
    lines = [header, "-" * 52]

    if not risks:
        lines.append("Nothing found that the numbers don't already cover.")
        return "\n".join(lines)

    names = dict(zip(players_df['id'], players_df['web_name']))
    for pid, r in sorted(risks.items(), key=lambda kv: -kv[1]['risk']):
        pct = int(r['risk'] * 100)
        lines.append(f"  {names.get(pid, pid)} — {pct}% risk of missing or being rotated")
        lines.append(f"      {r['reason']}" + (f"  ({r['source']})" if r['source'] else ""))
    lines.append("")
    if applied:
        lines.append("These players were rated down accordingly before the squad was picked.")
    else:
        lines.append("Nothing was changed because of this. Set LLM_TEAM_NEWS_ENABLED = True")
        lines.append("once you are happy the judgement is sound.")
    return "\n".join(lines)


def run_bot(team_id, test_mode=False):
    bootstrap_data = get('https://fantasy.premierleague.com/api/bootstrap-static/')
    events_df = pd.DataFrame(bootstrap_data['events'])

    should_update, gameweek = check_update(events_df)
    if gameweek is None:
        print("Season appears to be over.")
        return

    # --- authenticate first: whether we act early depends on transfer status ---
    session, team_data = build_authenticated_session(team_id)
    automated = session is not None

    unlimited = bool(automated
                     and (team_data.get('transfers', {}).get('limit') is None))

    if not should_update:
        # Normally we wait for the 24h window so decisions use real team news.
        # The exception is unlimited transfers (pre-GW1, or after a Wildcard /
        # Free Hit): transfers cost nothing, so there is no reason not to hold
        # the optimal squad in the meantime. The gameweek is NOT marked as
        # processed, so this re-runs with better data closer to the deadline.
        if not unlimited:
            print("Deadline Too Far Away")
            return
        print("Deadline is far away, but transfers are unlimited — "
              "optimising the squad early.")

    if should_update and not test_mode and already_processed(gameweek):
        print(f"GW{gameweek} already handled — skipping.")
        return

    players_df, fixtures_df = get_data(bootstrap_data, gameweek)
    print(f"Scored {len(players_df)} players for GW{gameweek}.")

    if automated:
        ids = [p['element'] for p in team_data['picks']]
        my_team = players_df[players_df['id'].isin(ids)].copy()
        transfers_info = team_data.get('transfers', {})
        print(f"[transfers] raw from FPL: {transfers_info}")

        bank = transfers_info.get('bank', 0)
        limit = transfers_info.get('limit')
        made = transfers_info.get('made', 0) or 0

        # limit=None means UNLIMITED free transfers (pre-GW1, or after a
        # Wildcard/Free Hit). Treating it as 0 makes the bot demand a huge
        # gain before it will act, which is the opposite of correct.
        if limit is None:
            has_free_transfer = True
            free_transfers = None
            print("[transfers] limit is None -> unlimited free transfers.")
        else:
            free_transfers = max(0, int(limit))
            has_free_transfer = free_transfers > made
            print(f"[transfers] {free_transfers} free, {made} already made "
                  f"-> free transfer available: {has_free_transfer}")

        # The points penalty per extra transfer is an FPL rule (4), not
        # something to infer from the response. FPL's 'cost' field reports the
        # total hit already accrued this gameweek, not the per-transfer rate.
        hit_cost = TRANSFER_HIT_COST
    else:
        my_team, bank, value = fetch_public_squad(team_id, players_df, gameweek)
        # No authenticated view of the transfer bank, so assume a single free
        # transfer and never suggest a hit. Conservative, which is right when
        # a human is going to approve this anyway.
        has_free_transfer, hit_cost = True, TRANSFER_HIT_COST
        free_transfers, made = 1, 0

    # --- team news: one check, before any branch ---------------------------
    # The bot has three ways to arrive at a squad (rebuild on unlimited
    # transfers, plan individual transfers, or build from scratch when no
    # squad is published). Checking here means all three inherit the result,
    # rather than needing the check bolted onto each one.
    news_risks = {}
    news_section = ""
    news_wanted = news_check_is_worthwhile(should_update, gameweek)
    if news_wanted and my_team is not None and len(my_team) >= 15:
        news_risks = fetch_minutes_risk(my_team, gameweek)
        if news_risks and LLM_TEAM_NEWS_ENABLED:
            print(f"[news] {len(news_risks)} player(s) flagged — ratings lowered "
                  "before any decision is made.")
            players_df = apply_minutes_risk(players_df, news_risks)
            my_team = players_df[players_df['id'].isin(my_team['id'])].copy()
        elif news_risks:
            print(f"[news] {len(news_risks)} player(s) flagged, but the layer is "
                  "set to report only — nothing changed.")
        news_section = format_news_section(news_risks, players_df,
                                           LLM_TEAM_NEWS_ENABLED)

    header = [
        f"FPL Gameweek {gameweek}",
        "",
    ]
    if automated:
        header += ["Your team has been updated automatically. "
                   "Nothing for you to do.", ""]
    else:
        header += [
            "AUTOMATION UNAVAILABLE — this is a recommendation only.",
            "Nothing has been changed. Make these changes in the FPL app yourself.",
            "",
            "To restore automation, refresh the FPL_TOKEN environment variable",
            "in Lambda (see the setup notes at the top of the code).",
            "",
        ]
    header += ["=" * 52, ""]
    sections = list(header)

    # --- unlimited transfers: rebuild the whole squad, not one swap ---
    if unlimited and my_team is not None and len(my_team) >= 15:
        incoming_section = {}

        def _news_hook(incoming_ids, pdf):
            risks, adjusted, section = check_incoming(incoming_ids, pdf, gameweek,
                                                      wanted=news_wanted)
            incoming_section['text'] = section
            incoming_section['risks'] = risks
            return adjusted if (risks and LLM_TEAM_NEWS_ENABLED) else None

        transfers, optimal, budget = rebuild_squad_unlimited(
            session, team_id, gameweek, players_df, team_data, test_mode,
            news_hook=_news_hook)

        squad = optimal if optimal is not None else my_team
        if news_section:
            sections.append(news_section)
            sections.append("")
        if incoming_section.get('text'):
            sections.append(incoming_section['text'])
            sections.append("")
        sections.append(format_rebuild_section(transfers, players_df, budget))
        sections.append("")

        starters, subs = pick_starting_xi(squad)
        starters = starters.sort_values('score', ascending=False)
        captain_row, vice_row = starters.iloc[0], starters.iloc[1]

        if automated and not test_mode:
            submit_lineup(session, team_id, starters, subs,
                          int(captain_row.id), int(vice_row.id))

        sections.append(format_xi(starters, subs, captain_row, vice_row))
        sections.append("")
        sections.append(f"Captain      : {captain_row.web_name}  ({captain_row.score:.2f})")
        sections.append(f"Vice-captain : {vice_row.web_name}  ({vice_row.score:.2f})")
        sections.append("")
        sections.append(format_chip_section(evaluate_chips(squad, subs, captain_row), None))

        body = "\n".join(sections)
        print(body)

        lookup = players_df.set_index('id', drop=False)
        tl = []
        for t in transfers:
            o, n = lookup.loc[t['element_out']], lookup.loc[t['element_in']]
            tl.append({'out': o['web_name'], 'in': n['web_name'],
                       'gain': float(n['score']) - float(o['score']),
                       'why': describe_player(n)})
        html = build_html_email(
            gameweek, automated,
            "Transfers are free right now, so the whole squad was optimised rather "
            "than a single swap. This runs again nearer the deadline, with team news.",
            ("Your team has been updated automatically. Nothing for you to do."
             if automated else
             "<b>Automation is unavailable.</b> Make these changes in the FPL app yourself."),
            news_risks, incoming_section.get('risks'), players_df, tl,
            starters, subs, captain_row, vice_row,
            [{'name': c[0], 'recommend': c[1], 'reason': c[2]}
             for c in evaluate_chips(squad, subs, captain_row)],
            LLM_TEAM_NEWS_ENABLED)

        poster = build_squad_poster(
            gameweek, starters, subs, captain_row, vice_row,
            *(lambda hi, rk, tp: (hi, int(squad['now_cost'].sum()), rk, tp))(
                *fetch_entry_summary(team_id)))

        if test_mode:
            print("\nTEST MODE — nothing submitted, no email, nothing saved.")
            return

        # Only email when the squad actually changed. On a 2-hourly schedule
        # this would otherwise fire every run for the next fortnight.
        sig = squad_signature(squad.id)
        if sig == last_squad_signature():
            print("Squad unchanged since last run — no email sent.")
            return
        save_squad_signature(sig)
        send_advice_email(f"GW{gameweek} squad optimised "
                          f"({len(transfers)} transfer(s))", body, html, poster)
        # Deliberately NOT mark_processed: this must run again nearer the
        # deadline, when real team news is available.
        return

    # --- squad: real one if we have it, otherwise build a suggestion ---
    if my_team is not None and len(my_team) >= 15:
        # Selling prices differ from current prices once a player's value has
        # moved, so use FPL's figures where we have them.
        original_squad_ids = set(int(i) for i in my_team['id'])
        selling_prices = {}
        if automated:
            selling_prices = {int(p['element']): int(p.get('selling_price',
                              p.get('purchase_price', 0)))
                              for p in team_data.get('picks', [])}

        transfers, my_team, plan_log = plan_transfers(
            my_team, players_df, bank, free_transfers, made, hit_cost, selling_prices)

        for line in plan_log:
            print(f"[plan] {line}")


        # Check anyone we're about to buy, before committing the transfer.
        incoming_ids = [int(t[1].id.iat[0]) for t in transfers]
        in_risks, players_df, in_section = check_incoming(
            incoming_ids, players_df, gameweek, wanted=news_wanted)

        if in_risks and LLM_TEAM_NEWS_ENABLED:
            my_team = players_df[players_df['id'].isin(original_squad_ids)].copy()
            transfers, my_team, plan_log = plan_transfers(
                my_team, players_df, bank, free_transfers, made,
                hit_cost, selling_prices)
            for line in plan_log:
                print(f"[plan/news] {line}")

        if in_section:
            sections.append(in_section)
            sections.append("")

        if transfers and automated and not test_mode:
            submit_transfers_bulk(session, team_id, gameweek, transfers, selling_prices)

        sections.append(news_section)
        sections.append("")
        sections.append(f"TRANSFERS ({len(transfers)})" if transfers else "TRANSFERS — none")
        sections.append("-" * 52)
        sections.append(format_transfer_plan(transfers, plan_log, hit_cost))
        sections.append("")
        squad = my_team
    else:
        budget = (bank or 0) + 1000
        print(f"No squad available — building a full 15 for £{budget/10:.1f}m.")
        squad = build_suggested_squad(players_df, budget)
        if squad is None:
            body = "\n".join(sections + ["Could not build a squad suggestion."])
            print(body)
            if not test_mode:
                send_advice_email(f"GW{gameweek} — could not build squad", body)
            return
        # Same team-news check as the transfer path. This matters most here:
        # a squad built from scratch has no human sanity-check behind it.
        news_risks = fetch_minutes_risk(squad, gameweek) if news_wanted else {}
        if news_risks and LLM_TEAM_NEWS_ENABLED:
            print(f"[news] {len(news_risks)} player(s) flagged — rebuilding the squad.")
            players_df = apply_minutes_risk(players_df, news_risks)
            rebuilt = build_suggested_squad(players_df, budget)
            if rebuilt is not None:
                squad = rebuilt
        elif news_risks:
            print(f"[news] {len(news_risks)} player(s) flagged, but the layer is set "
                  "to report only — nothing changed.")

        sections.append(format_news_section(news_risks, players_df,
                                            LLM_TEAM_NEWS_ENABLED))
        sections.append("")
        sections.append(f"SUGGESTED 15-MAN SQUAD  (£{budget/10:.1f}m budget)")
        sections.append("-" * 52)
        sections.append(format_squad_table(squad))
        sections.append("")

    # --- lineup ---
    starters, subs = pick_starting_xi(squad)
    starters = starters.sort_values('score', ascending=False)
    captain_row, vice_row = starters.iloc[0], starters.iloc[1]

    if automated and not test_mode and len(squad) >= 15:
        submit_lineup(session, team_id, starters, subs,
                      int(captain_row.id), int(vice_row.id))

    sections.append(format_xi(starters, subs, captain_row, vice_row))
    sections.append("")
    sections.append(f"Captain      : {captain_row.web_name}  ({captain_row.score:.2f})")
    sections.append(f"Vice-captain : {vice_row.web_name}  ({vice_row.score:.2f})")
    sections.append("")
    sections.append(format_chip_section(evaluate_chips(squad, subs, captain_row), None))

    body = "\n".join(sections)
    print(body)

    # `transfers` and `in_risks` only exist on the path that plans transfers;
    # the build-from-scratch path never defines them.
    _transfers = locals().get('transfers') or []
    _in_risks = locals().get('in_risks') or None

    tl = []
    for player_out, player_in, gain, was_free in _transfers:
        tl.append({'out': player_out.web_name.iat[0], 'in': player_in.web_name.iat[0],
                   'gain': float(gain),
                   'why': ('free transfer · ' if was_free else f'-{hit_cost} pt hit · ')
                          + describe_player(player_in.iloc[0])})
    html = build_html_email(
        gameweek, automated,
        f"Decided {ACTION_WINDOW_HOURS} hours before the deadline, once the "
        f"press conferences were out.",
        ("Your team has been updated automatically. Nothing for you to do."
         if automated else
         "<b>Automation is unavailable.</b> Make these changes in the FPL app yourself."),
        news_risks, _in_risks, players_df, tl,
        starters, subs, captain_row, vice_row,
        [{'name': c[0], 'recommend': c[1], 'reason': c[2]}
         for c in evaluate_chips(squad, subs, captain_row)],
        LLM_TEAM_NEWS_ENABLED)

    poster = build_squad_poster(
        gameweek, starters, subs, captain_row, vice_row,
        *(lambda hi, rk, tp: (hi, int(squad['now_cost'].sum()), rk, tp))(
            *fetch_entry_summary(team_id)))

    if test_mode:
        print("\nTEST MODE — nothing submitted, no email, nothing saved.")
        return

    subject = (f"GW{gameweek} team updated" if automated
               else f"GW{gameweek} ACTION NEEDED — automation is down")
    # Record first, email second. The submissions above are already live, so
    # the duplicate guard matters more than the notification.
    mark_processed(gameweek)
    send_advice_email(subject, body, html, poster)


def choose_transfer(my_team, players_df, bank, selling_prices=None,
                    full_squad=None, exclude_out=None):
    """Pick the out/in pair and the projected gain. Returns (out, in, gain).

    my_team      players that may be SOLD
    full_squad   everything currently owned, for club limits and the
                 already-own check. Defaults to my_team when they're the same.
    selling_prices  FPL's sale value per player id. A player whose price has
                 risen sells for less than now_cost, so using now_cost here
                 would overstate the budget and FPL would reject the transfer.
    exclude_out  ids that must not be chosen as the outgoing player
    """
    owned = my_team if full_squad is None else full_squad
    selling_prices = selling_prices or {}

    sellable = my_team
    if exclude_out:
        sellable = sellable.loc[~sellable['id'].isin(exclude_out)]
    if sellable.empty:
        return None, None, 0.0

    player_out = pick_out_candidate(sellable)
    out_id = int(player_out.id.iat[0])
    sale_value = selling_prices.get(out_id, int(player_out.now_cost.iat[0]))
    budget = (bank or 0) + sale_value

    # Club limits and "do I already own him" must be judged against the WHOLE
    # squad, not just the players available to sell.
    club_counts = owned['team'].value_counts()
    full_clubs = [c for c, n in club_counts.items()
                  if n >= 3 and c != player_out.team.iat[0]]

    pool = players_df[
        (players_df['element_type'] == player_out.element_type.iat[0])
        & (players_df['now_cost'] <= budget)
        & (~players_df['team'].isin(full_clubs))
        & (players_df['status'] == 'a')
        & (~players_df['id'].isin(owned['id']))
    ]

    player_in = pick_in_candidate(pool)
    if player_in is None:
        return player_out, None, 0.0
    return player_out, player_in, player_in.score.iat[0] - player_out.score.iat[0]


# --- Lambda entry point ---
def lambda_handler(event, context):
    team_id = os.environ.get('FPL_TEAM_ID')
    if not team_id:
        raise Exception("FPL_TEAM_ID environment variable is not set.")

    try:
        run_bot(team_id, test_mode=False)
        return {"statusCode": 200, "body": "OK"}

    except FPLUnavailable as e:
        message = ("The FPL API was unreachable this run, so nothing was done.\n\n"
                   f"{e}\n\nThis is usually temporary — it will retry next run.")
        print(message)
        if should_send_outage_email():
            send_error_email(message)
        raise

    except Exception as e:
        error_details = f"{e}\n\n{traceback.format_exc()}"
        send_error_email(error_details)
        raise
