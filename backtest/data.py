"""Historical FPL data for the backtest.

Source: github.com/vaastav/Fantasy-Premier-League, which archives every
player's per-fixture stats and FPL's own projection (xP) for each gameweek.
Files are downloaded once into backtest/cache/ (git-ignored).
"""
import os
import urllib.request

import pandas as pd

BASE = 'https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data'
CACHE = os.path.join(os.path.dirname(__file__), 'cache')
FILES = {'gw': 'gws/merged_gw.csv', 'players': 'players_raw.csv', 'fixtures': 'fixtures.csv'}

# xG/xA and `starts` exist from 2022-23. 2021-22 is loaded only as the
# "last season" prior for 2022-23.
SEASONS = ['2021-22', '2022-23', '2023-24', '2024-25', '2025-26', '2026-27']


def _fetch(season, kind):
    path = os.path.join(CACHE, season, os.path.basename(FILES[kind]))
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        urllib.request.urlretrieve(f'{BASE}/{season}/{FILES[kind]}', path)
    return path


def load_season(season):
    """Per-fixture rows for one season, with a stable cross-season player key.

    `element` ids are reassigned every season; `code` is FPL's permanent
    player id, so it links a player to his previous season.
    """
    gw = pd.read_csv(_fetch(season, 'gw'), encoding='utf-8', low_memory=False)
    players = pd.read_csv(_fetch(season, 'players'), encoding='utf-8',
                          usecols=['id', 'code', 'element_type', 'web_name', 'team'])
    fixtures = pd.read_csv(_fetch(season, 'fixtures'),
                           usecols=['id', 'event', 'team_h', 'team_a',
                                    'team_h_difficulty', 'team_a_difficulty'])

    gw = gw.merge(players.rename(columns={'id': 'element', 'team': 'team_id'}),
                  on='element', how='left')
    gw = gw.merge(fixtures.rename(columns={'id': 'fixture'}), on='fixture', how='left')

    gw['was_home'] = gw['was_home'].astype(str).str.lower().eq('true')
    gw['team_id'] = gw['team_h'].where(gw['was_home'], gw['team_a'])
    gw['difficulty'] = gw['team_h_difficulty'].where(gw['was_home'], gw['team_a_difficulty'])
    for col in ('expected_goals', 'expected_assists', 'expected_goal_involvements',
                'expected_goals_conceded', 'starts'):
        if col not in gw.columns:
            gw[col] = float('nan')
    gw['season'] = season
    return gw


def load_all(seasons=SEASONS):
    return pd.concat([load_season(s) for s in seasons], ignore_index=True)
