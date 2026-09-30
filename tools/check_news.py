"""Run the bot's team-news check on a squad, locally, without touching FPL.

    PowerShell:
        $env:GEMINI_API_KEY = Read-Host "Gemini key"; python -X utf8 tools/check_news.py

Uses the same code as the Lambda: Google News headlines per club, read by
free-tier Gemini. Prints the headlines found and the risk it would apply to
each player. Nothing is submitted or saved.
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import fpl_bot_hybrid as bot     # noqa: E402

TEAM_ID = int(os.environ.get('FPL_TEAM_ID', '2673853'))


def main():
    if not os.environ.get('GEMINI_API_KEY'):
        sys.exit("Set GEMINI_API_KEY first (see the docstring).")
    boot = bot.get('https://fantasy.premierleague.com/api/bootstrap-static/')
    gw = next(e for e in boot['events'] if e['is_next'])['id']
    with contextlib.redirect_stdout(io.StringIO()):
        players, _ = bot.get_data(boot, gw)
        squad, _, _ = bot.fetch_public_squad(TEAM_ID, players, gw)
    print(f"GW{gw}, squad of entry {TEAM_ID}: {', '.join(squad.web_name)}\n")

    risks = bot.fetch_minutes_risk(squad, gw)
    print()
    if not risks:
        print("No player flagged.")
    names = dict(zip(squad.id, squad.web_name))
    for pid, r in sorted(risks.items(), key=lambda kv: -kv[1]['risk']):
        print(f"{names.get(pid, pid):<16} risk {r['risk']:.2f}  {r['reason']}  [{r.get('source', '')}]")


if __name__ == '__main__':
    main()
