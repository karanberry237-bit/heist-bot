"""Draws the /rank card image (Statbot-style dashboard) with Pillow.

Pillow comes with matplotlib, and so do the DejaVu fonts used here, so there is
nothing extra to install.
"""
import io
import os
from functools import lru_cache

import matplotlib
from PIL import Image, ImageDraw, ImageFont

S = 2  # draw at 2x, then shrink for smooth edges
W, H = 1000, 560
BG = (43, 45, 49, 255)
PANEL = (35, 36, 40, 255)
ROW = (47, 49, 54, 255)
CHIP = (64, 66, 73, 255)
BAR = (88, 101, 242, 255)
BAR_DIM = (62, 68, 150, 255)
WHITE = (255, 255, 255, 255)
MUTED = (160, 164, 172, 255)
GRID = (58, 60, 66, 255)
GREEN = (59, 190, 99)
PINK = (222, 80, 140)
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")


@lru_cache(maxsize=None)
def F(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(_FONT_DIR, name), int(size * S))


def render_rank_card(d: dict) -> io.BytesIO:
    """d keys: name, server, avatar (bytes|None), created, joined, level, rank,
    xp_into, xp_need, msgs[3], voice[3], top[3], chart_m[14], chart_v[14], bot."""
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1, r, fill):
        dr.rounded_rectangle([int(x0 * S), int(y0 * S), int(x1 * S), int(y1 * S)], radius=int(r * S), fill=fill)

    def text(x, y, s, size, fill=WHITE, bold=False, anchor="la"):
        dr.text((int(x * S), int(y * S)), s, font=F(size, bold), fill=fill, anchor=anchor)

    def width(s, size, bold=False):
        return dr.textlength(s, font=F(size, bold)) / S

    def fit(s, size, maxw, bold=False):
        if width(s, size, bold) <= maxw:
            return s
        while s and width(s + "…", size, bold) > maxw:
            s = s[:-1]
        return s + "…"

    def panel(x, y, w, h, title):
        box(x, y, x + w, y + h, 12, PANEL)
        text(x + 14, y + 12, title, 16, bold=True)

    def stat_row(x, y, w, label, num, unit=""):
        box(x, y, x + w, y + 32, 8, ROW)
        box(x, y, x + 54, y + 32, 8, CHIP)
        text(x + 27, y + 16, label, 15, bold=True, anchor="mm")
        text(x + 66, y + 16, num, 16, bold=True, anchor="lm")
        if unit:
            text(x + 66 + width(num, 16, True) + 6, y + 17, unit, 13, MUTED, anchor="lm")

    # ---- card + header
    box(0, 0, W, H, 20, BG)
    if d.get("avatar"):
        try:
            a = Image.open(io.BytesIO(d["avatar"])).convert("RGBA").resize((72 * S, 72 * S), RESAMPLE)
            m = Image.new("L", a.size, 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, a.size[0] - 1, a.size[1] - 1], radius=16 * S, fill=255)
            img.paste(a, (24 * S, 22 * S), m)
        except Exception:
            pass
    text(112, 26, fit(d["name"], 30, 470, True), 30, bold=True)
    text(112, 68, fit(d["server"], 17, 470), 17, MUTED)
    for i, (label, value) in enumerate((("Created On", d["created"]), ("Joined On", d["joined"]))):
        x = 604 + i * 190
        box(x, 24, x + 92, 46, 6, CHIP)
        text(x + 46, 35, label, 12, MUTED, True, "mm")
        box(x, 50, x + 178, 84, 8, CHIP)
        text(x + 89, 67, value, 15, WHITE, True, "mm")

    # ---- row 1: level, messages, voice
    pw = 308
    panel(24, 108, pw, 186, "Level & Rank")
    stat_row(36, 148, pw - 24, "Level", str(d["level"]))
    stat_row(36, 188, pw - 24, "Rank", f"#{d['rank']}")
    track = pw - 24
    box(36, 234, 36 + track, 248, 7, ROW)
    frac = d["xp_into"] / d["xp_need"] if d["xp_need"] else 0
    if frac > 0:
        box(36, 234, 36 + max(14, track * frac), 248, 7, BAR)
    text(24 + pw / 2, 271, f"{d['xp_into']:,} / {d['xp_need']:,} XP to level {d['level'] + 1}", 13, MUTED, anchor="mm")

    panel(24 + pw + 14, 108, pw, 186, "Messages")
    panel(24 + 2 * (pw + 14), 108, pw, 186, "Voice Activity")
    for i, lab in enumerate(("1d", "7d", "14d")):
        stat_row(24 + pw + 26, 148 + 40 * i, pw - 24, lab, f"{d['msgs'][i]:,}", "messages")
        stat_row(24 + 2 * (pw + 14) + 12, 148 + 40 * i, pw - 24, lab, f"{d['voice'][i]:.1f}", "hours")

    # ---- row 2: top channels + chart
    panel(24, 308, 460, 206, "Top Channels")
    for i, r in enumerate(d["top"]):
        y = 348 + i * 50
        box(36, y, 472, y + 40, 8, ROW)
        if r:
            box(36, y, 36 + max(46, 436 * r["frac"]), y + 40, 8, BAR_DIM)
            box(36, y, 82, y + 40, 8, CHIP)
            text(59, y + 20, r["kind"], 14, WHITE, True, "mm")
            text(96, y + 20, fit(r["label"], 15, 250, True), 15, WHITE, True, "lm")
            text(460, y + 20, r["value"], 13, MUTED, anchor="rm")
        else:
            text(50, y + 20, "No data", 13, MUTED, anchor="lm")

    panel(498, 308, 478, 206, "Charts")
    lx = 962
    for name, col in (("Voice", PINK), ("Message", GREEN)):
        text(lx, 326, name, 13, MUTED, anchor="rm")
        lx -= width(name, 13) + 12
        dr.ellipse([int((lx - 8) * S), int(322 * S), int(lx * S), int(330 * S)], fill=col + (255,))
        lx -= 22

    cx0, cx1, cy0, cy1 = 514, 960, 356, 498
    for k in range(3):
        gy = cy0 + k * (cy1 - cy0) / 2
        dr.line([int(cx0 * S), int(gy * S), int(cx1 * S), int(gy * S)], fill=GRID, width=S)
    series = ((d["chart_m"], GREEN), (d["chart_v"], PINK))
    if not any(max(s) > 0 for s, _ in series):
        text((cx0 + cx1) / 2, (cy0 + cy1) / 2, "No data yet", 14, MUTED, anchor="mm")
    else:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        lines = []
        for vals, col in series:
            top = max(vals)
            if top <= 0:
                continue
            pts = [
                (int((cx0 + i * (cx1 - cx0) / 13) * S), int((cy1 - (v / top) * (cy1 - cy0 - 8)) * S))
                for i, v in enumerate(vals)
            ]
            od.polygon(pts + [(int(cx1 * S), int(cy1 * S)), (int(cx0 * S), int(cy1 * S))], fill=col + (50,))
            lines.append((pts, col))
        img.alpha_composite(overlay)
        for pts, col in lines:
            dr.line(pts, fill=col + (255,), width=int(2.5 * S), joint="curve")

    # ---- footer
    text(24, 538, "Server Lookback: Last 14 days — Timezone: UTC", 13, MUTED, anchor="lm")
    text(W - 24, 538, f"Powered by {d['bot']}", 13, MUTED, True, "rm")

    out = io.BytesIO()
    img.resize((W, H), RESAMPLE).save(out, "PNG")
    out.seek(0)
    return out
