# -*- coding: utf-8 -*-
"""生成应用图标 studio/icon.ico（橙色相机光圈 + 取景框）。"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# 背景圆角方块
d.rounded_rectangle([6, 6, S - 6, S - 6], radius=52, fill=(22, 23, 27, 255))
# 取景框（倾斜 12 度，暗示 roll）
frame = Image.new("RGBA", (S, S), (0, 0, 0, 0))
fd = ImageDraw.Draw(frame)
fd.rounded_rectangle([54, 74, S - 54, S - 62], radius=14, outline=(255, 215, 94, 255), width=11)
fd.line([54, 96, S - 54, S - 84], fill=(255, 215, 94, 90), width=3)   # 三分线示意
img.alpha_composite(frame.rotate(-12, resample=Image.BICUBIC, center=(S // 2, S // 2)))
# 光圈
cx, cy, r = S // 2, S // 2 + 4, 46
d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 122, 182, 255), width=12)
for i in range(6):
    import math
    a = i * math.pi / 3 + 0.35
    d.line([cx + r * 0.42 * math.cos(a), cy + r * 0.42 * math.sin(a),
            cx - r * 0.42 * math.cos(a), cy - r * 0.42 * math.sin(a)],
           fill=(167, 139, 250, 235), width=7)
d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(255, 215, 94, 255))
path = os.path.join(HERE, "icon.ico")
img.save(path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
img.save(os.path.join(HERE, "icon.png"))
print("ICON", path, os.path.getsize(path), "bytes")
