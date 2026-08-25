"""Render a share card for one gameweek, straight from data/season.json.

Portrait 1080x1350 — the tallest aspect LinkedIn will show without cropping,
so it occupies the most vertical space in a phone feed.

  python make_card.py             # latest scored gameweek
  python make_card.py --gw 2      # a specific one

Writes docs/cards/gw{n}.png. Fonts are downloaded once into fonts/.
"""
import argparse
import json
import os
import urllib.request

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350

DEEP = (23, 2, 29)
MID = (58, 10, 70)
GLOW = (92, 15, 108)
MINT = (0, 255, 135)
AMBER = (255, 194, 77)
ROSE = (255, 93, 122)
LAV = (199, 179, 207)
LAVD = (154, 133, 164)
WHITE = (255, 255, 255)

FONTS = {
    'display': ('SpaceGrotesk[wght].ttf',
                'https://raw.githubusercontent.com/google/fonts/main/ofl/'
                'spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf'),
    'body': ('Inter[opsz,wght].ttf',
             'https://raw.githubusercontent.com/google/fonts/main/ofl/'
             'inter/Inter%5Bopsz%2Cwght%5D.ttf'),
    'mono': ('JetBrainsMono[wght].ttf',
             'https://raw.githubusercontent.com/google/fonts/main/ofl/'
             'jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf'),
}


def font(kind, size, weight=400):
    name, url = FONTS[kind]
    path = os.path.join('fonts', name)
    if not os.path.exists(path):
        os.makedirs('fonts', exist_ok=True)
        print(f"  downloading {name}")
        urllib.request.urlretrieve(url, path)
    f = ImageFont.truetype(path, size)
    try:
        f.set_variation_by_axes([weight] if kind != 'body' else [14, weight])
    except Exception:
        pass
    return f


def background():
    """The poster's radial gradients, cheaply: vertical blend plus two glows."""
    img = Image.new('RGB', (W, H), DEEP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        # deep -> mid -> deep again, so the middle carries the purple
        k = 1 - abs(t - 0.32) / 0.68
        k = max(0.0, k) ** 1.4
        d.line([(0, y), (W, y)],
               fill=tuple(int(DEEP[i] + (MID[i] - DEEP[i]) * k) for i in range(3)))

    glow = Image.new('RGB', (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-260, -420, 760, 420], fill=GLOW)
    gd.ellipse([W - 520, H - 700, W + 380, H + 260], fill=(42, 7, 51))
    from PIL import ImageFilter
    glow = glow.filter(ImageFilter.GaussianBlur(190))
    return Image.blend(img, Image.blend(img, glow, 0.55), 0.75)


def mono_caps(d, xy, text, size, colour, track=4):
    """Letter-spaced uppercase mono, the way the poster sets small labels."""
    f = font('mono', size, 600)
    x, y = xy
    for ch in text.upper():
        d.text((x, y), ch, font=f, fill=colour)
        x += d.textlength(ch, font=f) + track
    return x


def rounded(d, box, r, fill=None, outline=None, width=2):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def pick(season, gw=None):
    gws = [g for g in season['gameweeks']
           if (g.get('bot', {}).get('actual', {}) or {}).get('total') is not None]
    if not gws:
        raise SystemExit("No scored gameweek yet.")
    if gw:
        m = [g for g in gws if int(g['gw']) == gw]
        if not m:
            raise SystemExit(f"GW{gw} has no result yet.")
        return m[0]
    return max(gws, key=lambda g: int(g['gw']))


def build(season, g):
    img = background()
    d = ImageDraw.Draw(img)
    M = 72

    bot = g['bot']
    proj = (bot.get('projected') or {}).get('total')
    act = (bot.get('actual') or {}).get('total')
    hum = (g.get('human', {}).get('actual') or {}).get('total')
    names = season.get('entries', {})

    # ---- header
    y = 84
    mono_caps(d, (M, y), 'autonomous fpl system', 21, MINT, 5)
    y += 46
    d.text((M, y), f"Gameweek {g['gw']}", font=font('display', 104, 700), fill=WHITE)
    y += 132
    sub = ("It published every prediction before kickoff."
           if g.get('instrumented') else "Results only for this gameweek.")
    d.text((M, y), sub, font=font('body', 30, 400), fill=LAV)
    y += 78

    # ---- the headline pair: projected vs actual
    card_h = 250
    rounded(d, [M, y, W - M, y + card_h], 26,
            fill=(38, 9, 47), outline=(70, 25, 82), width=2)
    half = W / 2
    mono_caps(d, (M + 40, y + 38), 'it predicted', 19, LAVD, 4)
    d.text((M + 40, y + 78), f"{proj:.1f}" if proj is not None else "—",
           font=font('display', 108, 700), fill=LAV)
    mono_caps(d, (half + 28, y + 38), 'it scored', 19, LAVD, 4)
    # Mint would read as success even on a miss, so the headline number carries
    # the verdict rather than the brand colour.
    beat = proj is None or act >= proj
    d.text((half + 28, y + 78), str(act), font=font('display', 108, 700),
           fill=MINT if beat else ROSE)
    d.line([(half, y + 34), (half, y + card_h - 34)], fill=(74, 28, 86), width=2)

    if proj is not None:
        diff = act - proj
        col = MINT if diff >= 0 else ROSE
        label = f"{'+' if diff > 0 else ''}{diff:.1f} vs its own projection"
        d.text((M + 40, y + 196), label, font=font('mono', 26, 600), fill=col)
    y += card_h + 34

    # ---- human comparison
    if hum is not None:
        rounded(d, [M, y, W - M, y + 176], 26,
                fill=(30, 6, 38), outline=(64, 22, 76), width=2)
        gap = act - hum
        bot_leads = gap > 0
        mono_caps(d, (M + 40, y + 34), 'the bot', 18, LAVD, 4)
        d.text((M + 40, y + 68), str(act), font=font('display', 72, 700),
               fill=MINT if bot_leads else WHITE)
        mono_caps(d, (M + 320, y + 34), 'hand-picked by me', 18, LAVD, 4)
        d.text((M + 320, y + 68), str(hum), font=font('display', 72, 700),
               fill=MINT if not bot_leads else WHITE)
        verdict = ("bot ahead" if gap > 0 else "human ahead" if gap < 0 else "level")
        gtxt = f"{'+' if gap > 0 else ''}{gap}"
        gfont = font('display', 60, 700)
        gw_ = d.textlength(gtxt, font=gfont)
        # measure the caption so both sit flush to the same right edge
        vfont = font('mono', 17, 600)
        vw_ = sum(d.textlength(c, font=vfont) + 3 for c in verdict.upper())
        right = W - M - 40
        d.text((right - gw_, y + 52), gtxt, font=gfont, fill=AMBER)
        mono_caps(d, (right - vw_, y + 122), verdict, 17, LAVD, 3)
        y += 176 + 34

    # ---- captain and the biggest miss, the two things people argue about
    squad = bot.get('squad', [])
    scored = [p for p in squad if p.get('actual') is not None
              and p.get('projected_now') is not None]
    cap = next((p for p in squad if p['id'] == bot.get('captain')), None)
    rows, used = [], set()
    if cap and cap.get('actual') is not None:
        rows.append(('captain', cap)); used.add(id(cap))
    if scored:
        by_delta = sorted(scored, key=lambda p: p['actual'] - p['projected_now'])
        # Best available that isn't already shown, then worst available. The
        # captain is frequently also the worst call, which would otherwise
        # silently drop a row and leave a hole above the footer.
        for label, seq in (('best call', reversed(by_delta)),
                           ('worst call', by_delta)):
            for p in seq:
                if id(p) not in used:
                    rows.append((label, p)); used.add(id(p)); break

    rows = rows[:3]
    block = len(rows) * 112 + max(0, len(rows) - 1) * 18
    footer_top = H - 142
    y = max(y, footer_top - block - 28)

    for label, p in rows:
        rounded(d, [M, y, W - M, y + 112], 20,
                fill=(255, 255, 255, 0) if False else (32, 7, 40),
                outline=(60, 20, 72), width=2)
        mono_caps(d, (M + 34, y + 26), label, 17, LAVD, 3)
        d.text((M + 34, y + 54), p['name'], font=font('display', 40, 700), fill=WHITE)
        delta = p['actual'] - p['projected_now']
        col = MINT if delta >= 0 else ROSE
        txt = f"{p['projected_now']:.1f} → {p['actual']}"
        tf = font('mono', 34, 600)
        tw = d.textlength(txt, font=tf)
        d.text((W - M - 34 - tw, y + 40), txt, font=tf, fill=col)
        dtxt = f"{'+' if delta > 0 else ''}{delta:.1f}"
        df = font('mono', 22, 600)
        dw = d.textlength(dtxt, font=df)
        d.text((W - M - 34 - dw, y + 78), dtxt, font=df, fill=col)
        y += 112 + 18

    # ---- footer
    fy = H - 116
    d.line([(M, fy - 26), (W - M, fy - 26)], fill=(66, 24, 78), width=2)
    mono_caps(d, (M, fy), 'python · linear programming · aws lambda', 19, LAVD, 4)
    mono_caps(d, (M, fy + 38), 'predictions published before kickoff', 19, MINT, 4)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gw', type=int, default=None)
    ap.add_argument('--data', default='data/season.json')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    season = json.load(open(a.data, encoding='utf-8'))
    g = pick(season, a.gw)
    img = build(season, g)

    out = a.out or f"docs/cards/gw{g['gw']}.png"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    img.save(out, 'PNG', optimize=True)
    print(f"wrote {out}  ({os.path.getsize(out)//1024} KB)  "
          f"GW{g['gw']}: predicted {(g['bot']['projected'] or {}).get('total')}, "
          f"scored {g['bot']['actual']['total']}")


if __name__ == '__main__':
    main()
