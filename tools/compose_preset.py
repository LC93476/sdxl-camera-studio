# -*- coding: utf-8 -*-
"""从 out/raw 里挑每个预设的最新一张，拼成最终预设对比图。"""
import glob, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))
from presets import PRESETS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "out", "raw")
os.chdir(RAW)

def newest(name):
    hits = glob.glob("preset_%s_*.png" % name)
    if not hits:
        return None
    hits.sort(key=lambda p: (os.path.getmtime(p)))
    return os.path.join(RAW, hits[-1])

items = []
for name in PRESETS:
    p = newest(name)
    px, py_, pz, rl = PRESETS[name]
    sub = "x=%+.2f  y=%+.2f  z=%+.2f  roll=%.2f" % (px, py_, pz, rl)
    if p:
        items.append((p, name, sub))
    else:
        print("MISSING", name)

cols = 6
cell = 300
from PIL import Image, ImageDraw, ImageFont
def font(sz):
    for c in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\arial.ttf"):
        if os.path.exists(c):
            try: return ImageFont.truetype(c, sz)
            except Exception: pass
    return ImageFont.load_default()
f, fs = font(24), font(16)
ims = []
for p, cap, sub in items:
    im = Image.open(p).convert("RGB")
    ims.append((im.resize((cell, int(im.height * cell / im.width))), cap, sub))
ch = max(i[0].height for i in ims) + 56
rows = (len(ims) + cols - 1) // cols
sheet = Image.new("RGB", (cols * cell + (cols + 1) * 8, rows * ch + (rows + 1) * 8), (24, 24, 28))
d = ImageDraw.Draw(sheet)
for i, (im, cap, sub) in enumerate(ims):
    x = 8 + (i % cols) * (cell + 8); y = 8 + (i // cols) * (ch + 8)
    sheet.paste(im, (x, y))
    d.text((x + 4, y + im.height + 4), cap, font=f, fill=(255, 235, 120))
    d.text((x + 4, y + im.height + 30), sub, font=fs, fill=(170, 200, 255))
out = os.path.join(ROOT, "out", "grid_preset_final.jpg")
sheet.save(out, quality=93)
print("GRID", out, sheet.size, len(items), "cells")
