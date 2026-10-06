"""Share card for the site's "Your team" lookup, for any FPL entry.

Same numbers as the site computes in the browser, from the same committed
files: data/projections/gw{n}.json (frozen before kickoff) and
data/players.json, plus the entry's public picks from FPL.

  python make_lookup_card.py 1510697           # its latest gameweek
  python make_lookup_card.py 1510697 --gw 5

Writes docs/cards/team_{id}_gw{n}.png, 1080x1350 like make_card.py.
"""
import argparse
import json
import os

import requests
from PIL import ImageDraw

from make_card import (W, H, MINT, AMBER, LAV, LAVD, WHITE,
                       background, font, mono_caps, rounded)

FPL = 'https://fantasy.premierleague.com/api'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
SITE = 'shanbhag003.github.io/fpl-auto-manager'
UPGRADE_BUDGET = 20          # tenths of £m, as on the site
SHOWN = 5


def get(path):
    r = requests.get(f'{FPL}/{path}', headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def score(entry_id, gw=None):
    """The lookup's numbers, computed exactly as index.html does."""
    entry = get(f'entry/{entry_id}/')
    gw = gw or entry['current_event']
    picks = get(f'entry/{entry_id}/event/{gw}/picks/')
    proj = json.load(open(f'data/projections/gw{gw}.json', encoding='utf-8'))
    if proj.get('partial'):
        raise SystemExit(f'GW{gw} projections are partial; no honest score for this squad.')
    players = json.load(open('data/players.json', encoding='utf-8'))['players']
    scores = proj['scores']
    now = lambda i: max(0.0, scores[str(i)][0]) if str(i) in scores and scores[str(i)][0] is not None else None
    run = lambda i: max(0.0, scores[str(i)][1]) if str(i) in scores and scores[str(i)][1] is not None else None

    squad = []
    for p in sorted(picks['picks'], key=lambda p: p['position']):
        name, pos, club, cost = players.get(str(p['element']), [f"#{p['element']}", '', '', 0])
        squad.append(dict(id=p['element'], name=name, pos=pos, club=club, cost=cost,
                          xi=p['position'] <= 11, mult=p['multiplier'],
                          cap=p['is_captain'], now=now(p['element'])))
    xi = [p for p in squad if p['xi'] and p['now'] is not None]
    total = sum((p['now'] or 0) * p['mult'] for p in squad)
    captain = next(p for p in squad if p['cap'])
    best = max(xi, key=lambda p: p['now'])
    weakest = min(xi, key=lambda p: p['now'])

    owned = {str(p['id']) for p in squad}
    clubs = {}
    for p in squad:
        clubs[p['club']] = clubs.get(p['club'], 0) + 1
    swaps = []
    for out in squad:
        base = run(out['id'])
        if base is None:
            continue
        for pid, (name, pos, club, cost) in players.items():
            if pid in owned or pos != out['pos'] or cost > out['cost'] + UPGRADE_BUDGET:
                continue
            if club != out['club'] and clubs.get(club, 0) >= 3:
                continue
            r = run(pid)
            if r is None or r - base <= 0.05:
                continue
            swaps.append(dict(gain=r - base, now=r, was=base, out=out['name'],
                              id=pid, name=name, club=club, cost=cost))
    swaps.sort(key=lambda s: -s['gain'])
    picked, outs, ins = [], set(), set()
    for s in swaps:
        if s['out'] in outs or s['id'] in ins:
            continue
        picked.append(s); outs.add(s['out']); ins.add(s['id'])
        if len(picked) == SHOWN:
            break
    return dict(team=entry['name'], gw=gw, total=total, captain=captain, best=best,
                weakest=weakest, upgrades=picked, chip=picks.get('active_chip'))


def draw(s):
    img = background()
    d = ImageDraw.Draw(img)
    M = 72
    y = 82
    mono_caps(d, (M, y), 'autonomous fpl system · new', 22, MINT, 6)
    y += 52
    d.text((M, y), 'Score your', font=font('display', 86, 700), fill=WHITE)
    y += 88
    d.text((M, y), 'own team', font=font('display', 86, 700), fill=AMBER)
    y += 110
    d.text((M, y), 'Any FPL squad, rated by my model with the', font=font('body', 30, 400), fill=LAV)
    y += 40
    d.text((M, y), 'projections it committed before kickoff.', font=font('body', 30, 400), fill=LAV)
    y += 66

    # ---- the squad's verdict
    box_top, box_h = y, 244
    rounded(d, [M, box_top, W - M, box_top + box_h], 26,
            fill=(36, 9, 40), outline=(120, 86, 40), width=2)
    mono_caps(d, (M + 40, box_top + 38), f"{s['team']} · gw{s['gw']}", 19, AMBER, 4)
    mono_caps(d, (M + 40, box_top + 84), 'model total', 17, LAVD, 3)
    d.text((M + 36, box_top + 104), f"{s['total']:.1f}", font=font('display', 104, 700), fill=WHITE)
    mono_caps(d, (M + 40, box_top + 218), 'xi + captain, projected', 15, LAVD, 3)

    cx = M + 450
    d.line([(cx - 34, box_top + 80), (cx - 34, box_top + box_h - 34)], fill=(70, 30, 80), width=2)
    cap = s['captain']
    agrees = cap['id'] == s['best']['id'] or cap['now'] >= s['best']['now']
    mono_caps(d, (cx, box_top + 84), 'captain', 17, LAVD, 3)
    d.text((cx, box_top + 108), cap['name'], font=font('display', 40, 700), fill=WHITE)
    d.text((W - M - 40 - d.textlength(f"{cap['now']:.1f}", font=font('mono', 34, 600)), box_top + 112),
           f"{cap['now']:.1f}", font=font('mono', 34, 600), fill=WHITE)
    mono_caps(d, (cx, box_top + 160), 'model agrees' if agrees else f"model: {s['best']['name']}",
              15, MINT if agrees else AMBER, 3)
    wk = s['weakest']
    mono_caps(d, (cx, box_top + 196), 'weakest starter', 15, LAVD, 3)
    d.text((cx + 250, box_top + 188), f"{wk['name']}  {wk['now']:.1f}",
           font=font('body', 26, 600), fill=LAV)
    y = box_top + box_h + 40

    # ---- the five upgrades
    mono_caps(d, (M, y), 'top 5 upgrades · next five gameweeks', 19, LAVD, 4)
    y += 40
    row_h, gap = 78, 10
    for i, u in enumerate(s['upgrades']):
        rounded(d, [M, y, W - M, y + row_h], 20, fill=(32, 7, 40), outline=(60, 20, 72), width=2)
        d.text((M + 30, y + 23), str(i + 1), font=font('mono', 26, 600), fill=LAVD)
        nf = font('display', 32, 700)
        x = M + 74
        d.text((x, y + 12), u['out'], font=nf, fill=WHITE)
        x += d.textlength(u['out'], font=nf) + 14
        d.text((x, y + 12), '→', font=font('body', 30, 500), fill=LAVD)
        x += 44
        d.text((x, y + 12), u['name'], font=nf, fill=MINT)
        d.text((M + 74, y + 48), f"{u['club']} · £{u['cost'] / 10:.1f}m",
               font=font('mono', 18, 400), fill=LAVD)
        g = f"+{u['gain']:.1f}"
        gf = font('mono', 34, 600)
        d.text((W - M - 30 - d.textlength(g, font=gf), y + 10), g, font=gf, fill=MINT)
        mono_caps(d, (W - M - 30, y + 50), 'pts/wk', 14, LAVD, 2, anchor='r')
        y += row_h + gap
    assert y - gap < H - 132 - 24, f'upgrade rows end at {y - gap}, too close to the footer'

    # ---- footer
    footer_rule = H - 132
    d.line([(M, footer_rule), (W - M, footer_rule)], fill=(66, 24, 78), width=2)
    mono_caps(d, (M, footer_rule + 30), 'try yours', 19, LAVD, 4)
    d.text((M, footer_rule + 58), SITE, font=font('mono', 30, 600), fill=AMBER)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('entry', type=int)
    ap.add_argument('--gw', type=int, default=None)
    a = ap.parse_args()
    s = score(a.entry, a.gw)
    out = f"docs/cards/team_{a.entry}_gw{s['gw']}.png"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    draw(s).save(out, 'PNG', optimize=True)
    print(f"wrote {out}: {s['team']} GW{s['gw']} total {s['total']:.1f}, captain "
          f"{s['captain']['name']} {s['captain']['now']:.1f}, weakest {s['weakest']['name']} "
          f"{s['weakest']['now']:.1f}")
    for u in s['upgrades']:
        print(f"  {u['out']} -> {u['name']} +{u['gain']:.1f}")


if __name__ == '__main__':
    main()
