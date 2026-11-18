# -*- coding: utf-8 -*-
"""生成 docs/screenshots 里两张公开可用（无露骨内容）的示意图。"""
from PIL import Image, ImageDraw, ImageFont
import os

OUT = r"D:/QUARK/sdxl_camera_angle/docs/screenshots"
os.makedirs(OUT, exist_ok=True)

BG = (24, 26, 33)
FG = (226, 232, 240)
DIM = (148, 163, 184)
PURPLE = (167, 139, 250)
PINK = (255, 122, 182)
CYAN = (103, 232, 249)
YELLOW = (255, 215, 94)


def font(size):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf"):
        p = os.path.join(r"C:/Windows/Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


W, H = 1400, 820
im = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(im)
f_big, f_mid, f_sm = font(30), font(20), font(17)

d.text((40, 28), "SDXL 相机控制 · 参数映射", font=f_big, fill=FG)
d.text((40, 66), "pos_x / pos_y / pos_z / roll  →  提示词权重（正负镜像）", font=f_mid, fill=DIM)

# ---- 方位：俯视圆 ----
cx, cy, R = 300, 430, 190
d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=PURPLE, width=3)
d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=DIM)

import math
# theta = pos_x * pi：pos_x=0 正面上方，+0.5 右侧，-0.5 左侧，±1 背面
BINS = [("back", 1.0), ("back_right", 0.75), ("right", 0.5), ("front_right", 0.25),
        ("front", 0.0), ("front_left", -0.25), ("left", -0.5), ("back_left", -0.75)]
for name, px in BINS:
    theta = px * math.pi
    ux, uy = math.sin(theta), -math.cos(theta)   # 单位方向
    x2, y2 = cx + R * ux, cy + R * uy
    x, y = cx + (R + 52) * ux, cy + (R + 52) * uy
    d.line([cx, cy, x2, y2], fill=(60, 65, 80), width=1)
    tw = d.textlength(name, font=f_sm)
    ty = y - 11 if abs(uy) < 0.4 else (y - 11 if uy < 0 else y - 11)
    d.text((x - tw / 2, ty), name, font=f_sm, fill=FG)
    if name == "front":
        d.ellipse([x2 - 9, y2 - 9, x2 + 9, y2 + 9], fill=PINK)

# 相机点示例
theta = 0.5 * math.pi
px = cx + R * math.sin(theta)
py = cy - R * math.cos(theta)
d.ellipse([px - 10, py - 10, px + 10, py + 10], fill=YELLOW)
d.text((cx - 150, cy + R + 96), "pos_x = 0 正面 · +0.5 右侧 · −0.5 左侧 · ±1 背面", font=f_sm, fill=DIM)
d.text((30, 200), "方位 8 档 + 双档插值", font=f_mid, fill=PURPLE)

# ---- 高度：侧视弧 ----
ax, ay, RA = 800, 470, 165
d.arc([ax - RA, ay - RA, ax + RA, ay + RA], start=200, end=340, fill=CYAN, width=3)
d.line([ax - RA - 40, ay, ax + RA + 40, ay], fill=(60, 65, 80), width=1)
for label, frac, txt in [("high", -0.55, "y > 0.2  high angle"), ("eye", 0.0, "eye level（裸写不加权）"), ("low", 0.55, "y < -0.2  low angle")]:
    a = math.radians(270 + frac * 70)
    x = ax + RA * math.cos(a)
    y = ay + RA * math.sin(a)
    d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=CYAN)
    d.text((x + 14, y - 10), label, font=f_sm, fill=FG)
d.text((660, 200), "高度 3 档 / 5 档（含 bird·worm）", font=f_mid, fill=CYAN)
d.text((660, 240), "|pos_y| > 0.9 时方位词自动失效（az_gate）", font=f_sm, fill=DIM)

# ---- 距离：标尺 ----
dx, dy = 660, 600
steps = [("ecu", 0.7, "z > 0.7"), ("cu", 0.2, "0.2 ~ 0.7"), ("medium", 0.0, "0 ~ 0.2"),
         ("cowboy", -0.35, "−0.35 ~ 0"), ("full", -0.7, "−0.7 ~ −0.35"), ("wide", -1.0, "< −0.7")]
x0, x1 = dx, dx + 620
d.line([x0, dy, x1, dy], fill=(80, 88, 106), width=2)
for i, (name, z, rng) in enumerate(steps):
    x = x0 + (x1 - x0) * (i / (len(steps) - 1))
    d.line([x, dy - 7, x, dy + 7], fill=DIM, width=2)
    tw = d.textlength(name, font=f_sm)
    d.text((x - tw / 2, dy + 16), name, font=f_sm, fill=FG)
    tw2 = d.textlength(rng, font=f_sm)
    d.text((x - tw2 / 2, dy - 34), rng, font=font(14), fill=DIM)
d.text((660, 540), "距离 6 档（档内恒定，档间跳变）  pos_z 由 +1 特写到 −1 远景", font=f_mid, fill=YELLOW)

d.text((40, H - 46), "权重曲线：tanh 软压缩，raw=1 时权重=1；输出 (tag:w) 绝对值，w 超过 1.5 容易出噪点",
       font=f_sm, fill=DIM)
im.save(os.path.join(OUT, "mapping.png"), optimize=True)
print("mapping.png", im.size)

# UI 截图转 web 尺寸
ui = Image.open(r"D:/QUARK/_ui_clean.png").convert("RGB")
ui.thumbnail((1500, 1500), Image.LANCZOS)
ui.save(os.path.join(OUT, "ui_overview.png"), optimize=True)
print("ui_overview.png", ui.size)
