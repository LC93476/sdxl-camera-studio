# -*- coding: utf-8 -*-
"""JS 词表 / 参数 与 camera_core.py 的一致性检查（不需要 node）。"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))
import camera_core as c  # noqa: E402

JS = os.path.join(ROOT, "js", "sdxl_camera_widget.js")


def grab(name):
    src = open(JS, encoding="utf-8").read()
    m = re.search(r"const " + name + r" = \{(.*?)\n\};", src, re.S)
    if not m:
        raise SystemExit("找不到 " + name)
    body = re.sub(r"^\s*//.*$", "", m.group(1), flags=re.M)
    return dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', body))


def main():
    V = grab("V")
    NEG = grab("NEG")
    T = c.SDXL_TAGS
    bad = 0
    for k, v in T.items():
        if k in ("neg", "weights"):
            continue
        if V.get(k) != v:
            print("V 差异", k, "| py:", v, "| js:", V.get(k))
            bad += 1
    for k, v in T["neg"].items():
        if NEG.get(k) != v:
            print("NEG 差异", k, "| py:", v, "| js:", NEG.get(k))
            bad += 1
    extra_v = set(V) - set(k for k in T if k not in ("neg", "weights"))
    extra_n = set(NEG) - set(T["neg"])
    if extra_v:
        print("JS 多余正向键", sorted(extra_v))
    if extra_n:
        print("JS 多余负向键", sorted(extra_n))
    print("正向键 py=%d js=%d ; 负向键 py=%d js=%d ; 差异=%d"
          % (len([k for k in T if k not in ("neg", "weights")]), len(V), len(T["neg"]), len(NEG), bad))

    # 参数常量
    src = open(JS, encoding="utf-8").read()
    pairs = [
        ("AZ_WEIGHT", T["weights"]["azimuth"]["weight"]),
        ("weight_min", T["weights"]["weight_min"]),
        ("weight_max", T["weights"]["weight_max"]),
        ("neg_weight", T["weights"]["neg_weight"]),
    ]
    for name, pyv in pairs:
        m = re.search(name + r" = ([0-9.]+)", src) if name.startswith("AZ") else re.search(r'"?\b' + name + r'"?\s*[:=]\s*([0-9.]+)', src)
        jsv = float(m.group(1)) if m else None
        flag = "ok" if jsv == float(pyv) else "DIFF"
        print("%-5s %-12s py=%s js=%s" % (flag, name, pyv, jsv))

    # 预设表
    m = re.search(r"const PRESETS = \[(.*?)\n\];", src, re.S)
    rows = re.findall(r'\["([^"]+)", \[([^\]]+)\]', m.group(1))
    from presets import PRESETS
    jsp = {n: [float(x) for x in v.split(",")] for n, v in rows}
    bad2 = 0
    for n, p in PRESETS.items():
        if jsp.get(n) != list(p):
            print("预设差异", n, "py:", p, "js:", jsp.get(n))
            bad2 += 1
    print("预设 py=%d js=%d 差异=%d" % (len(PRESETS), len(jsp), bad2))
    print("TOTAL 差异 = %d" % (bad + bad2))


if __name__ == "__main__":
    main()
