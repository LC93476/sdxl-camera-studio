# -*- coding: utf-8 -*-
"""量一下词表真实 token 成本（CLIP-L tokenizer，与出图时同一套）。"""
import sys, json
sys.path.insert(0, r"D:/QUARK/ComfyUI-aki-v3.2/ComfyUI")
from comfy.sd1_clip import SDTokenizer

tk = SDTokenizer()
sys.path.insert(0, r"D:/QUARK/sdxl_camera_angle/core")
from camera_core import SDXL_TAGS

def count(s):
    # tokenize_with_weights 会先剥掉 (x:1.40)，返回 [(token, weight), ...]
    toks = tk.tokenize_with_weights(s)[0]
    ids = [t[0] for t in toks]
    end = ids.index(49407) if 49407 in ids else len(ids)
    return end - 1  # 去掉 BOS

rows = []
tot = 0
for k, v in SDXL_TAGS.items():
    if not isinstance(v, str):
        continue
    n = count(v)
    rows.append((k, n, v))
    tot += n
rows.sort(key=lambda r: -r[1])
for k, n, v in rows:
    print("%-12s %2d  %s" % (k, n, v))
print("TOTAL", tot, "over", len(rows), "keys")
