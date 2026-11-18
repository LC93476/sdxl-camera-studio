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

JS = os.path.join(ROOT, "js", "camera_shared.js")      # v2 单一真源
WIDGET = os.path.join(ROOT, "js", "sdxl_camera_widget.js")
STUDIO_CAM = os.path.join(ROOT, "studio", "web", "camera3d.js")


def grab(name):
    src = open(JS, encoding="utf-8").read()
    m = re.search(r"(?:export\s+)?const\s+" + name + r"\s*=\s*\{(.*?)\n\};", src, re.S)
    if not m:
        raise SystemExit("找不到 " + name)
    body = re.sub(r"^\s*//.*$", "", m.group(1), flags=re.M)
    return dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', body))


# ── v2 增补：光学 / 构图 / 极点 / 预设附加 / 单一真源结构 ──
def _num(src, name):
    m = re.search(r"(?:export\s+)?const\s+" + name + r"\s*=\s*(-?[0-9.]+)\s*;", src)
    if m:
        return float(m.group(1))
    # 允许 "A / B" 这类表达式（POLE_NORM = POLE_DEG / 90.0）
    m = re.search(r"(?:export\s+)?const\s+" + name + r"\s*=\s*([A-Z_]+)\s*/\s*(-?[0-9.]+)", src)
    if m:
        base = _num(src, m.group(1))
        if base is not None:
            return base / float(m.group(2))
    return None


def _strlist(src, name):
    m = re.search(r"(?:export\s+)?const\s+" + name + r"\s*=\s*\[(.*?)\];", src, re.S)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else None


def _nummap(body):
    body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
    return {k: float(v) for k, v in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(-?[0-9.]+)", body)}


def check_v2(src):
    bad = 0
    # 1) 数值常量
    pairs = [("W_MIN_OUT", c.W_MIN_OUT), ("W_MAX_OUT", c.W_MAX_OUT),
             ("AZ_POLE", c.AZ_POLE), ("POLE_DEG", c.POLE_DEG),
             ("POLE_NORM", c.POLE_NORM), ("POLE_START", c.POLE_START),
             ("AZ_SUPPRESS", c.AZ_SUPPRESS), ("BARE_EPS", c.BARE_EPS)]
    for name, pyv in pairs:
        jsv = _num(src, name)
        if jsv is None or abs(jsv - float(pyv)) > 1e-9:
            print("常量差异", name, "py:", pyv, "js:", jsv)
            bad += 1
    print("[v2] 数值常量 %d 个（W_MIN_OUT/W_MAX_OUT/AZ_POLE/POLE_DEG/POLE_NORM/POLE_START/AZ_SUPPRESS/BARE_EPS）" % len(pairs))

    # 2) 档位列表
    for name, pyv in (("AZ_BINS", c.AZ_BINS), ("EL_KEYS", c.EL_KEYS),
                      ("DIST_KEYS", c.DIST_KEYS), ("LENS_KEYS", c.LENS_KEYS),
                      ("FRAME_KEYS", c.FRAME_KEYS), ("SUBJECT_KEYS", c.SUBJECT_KEYS),
                      ("GAZE_KEYS", c.GAZE_KEYS)):
        jsv = _strlist(src, name)
        if jsv != list(pyv):
            print("档位差异", name, "py:", list(pyv), "js:", jsv)
            bad += 1
    print("[v2] 档位表 7 组（AZ_BINS/EL_KEYS/DIST_KEYS/LENS_KEYS/FRAME_KEYS/SUBJECT_KEYS/GAZE_KEYS）")

    # 3) 焦距权重与分档
    m = re.search(r"const\s+LENS_WEIGHT\s*=\s*\{(.*?)\n\};", src, re.S)
    lw = _nummap(m.group(1)) if m else {}
    for k, v in c.LENS_WEIGHT.items():
        if abs(lw.get(k, -1) - float(v)) > 1e-9:
            print("LENS_WEIGHT 差异", k, "py:", v, "js:", lw.get(k))
            bad += 1
    if set(lw) != set(c.LENS_WEIGHT):
        print("LENS_WEIGHT 键集差异 py:", sorted(c.LENS_WEIGHT), "js:", sorted(lw))
        bad += 1
    m = re.search(r"const\s+LENS_BANDS\s*=\s*\[(.*?)\n\];", src, re.S)
    js_bands = [[float(b), k] for b, k in
                re.findall(r'\[\s*(-?[0-9.eE+]+)\s*,\s*"([a-z0-9_]+)"\s*\]', m.group(1))] if m else []
    py_bands = [[float(b), k] for b, k in c.LENS_BANDS]
    if js_bands != py_bands:
        print("LENS_BANDS 差异 py:", py_bands, "js:", js_bands)
        bad += 1
    print("[v2] LENS_WEIGHT %d 档 + LENS_BANDS %d 段" % (len(c.LENS_WEIGHT), len(py_bands)))

    # 4) 极点屏蔽表
    m = re.search(r"const\s+POLE_BLOCK_TOKENS\s*=\s*\{(.*?)\n\};", src, re.S)
    js_pb = {}
    if m:
        for key, arr in re.findall(r"([a-z]+)\s*:\s*\[(.*?)\]", m.group(1), re.S):
            js_pb[key] = re.findall(r'"([^"]*)"', arr)
    py_pb = {k: list(v) for k, v in c.POLE_BLOCK_TOKENS.items()}
    if js_pb != py_pb:
        for k in sorted(set(js_pb) | set(py_pb)):
            if js_pb.get(k) != py_pb.get(k):
                print("POLE_BLOCK_TOKENS 差异", k, "py:", py_pb.get(k), "js:", js_pb.get(k))
                bad += 1
    print("[v2] POLE_BLOCK_TOKENS %d 组（极点互斥）" % len(py_pb))

    # 4b) 极点视线改写表
    m = re.search(r"const\s+POLE_GAZE\s*=\s*\{(.*?)\n\};", src, re.S)
    js_pg = dict(re.findall(r'([a-z]+)\s*:\s*"([^"]*)"', m.group(1))) if m else {}
    py_pg = {k: v for k, v in c.POLE_GAZE.items()}
    if js_pg != py_pg:
        for k in sorted(set(js_pg) | set(py_pg)):
            if js_pg.get(k) != py_pg.get(k):
                print("POLE_GAZE 差异", k, "py:", py_pg.get(k), "js:", js_pg.get(k))
                bad += 1
    print("[v2] POLE_GAZE %d 条（极点视线改写）" % len(py_pg))

    # 5) 预设附加参数与模式
    from presets import PRESET_EXTRAS, PRESET_MODES
    for label, pyv in (("PRESET_EXTRAS", PRESET_EXTRAS), ("PRESET_MODES", PRESET_MODES)):
        m = re.search(r"const\s+" + label + r"\s*=\s*\{(.*?)\n\};", src, re.S)
        jsd = {}
        if m:
            for name, inner in re.findall(r'"([^"]+)"\s*:\s*\{([^}]*)\}', m.group(1)):
                d = {}
                for k, v in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*("(?:[^"]*)"|-?[0-9.]+)', inner):
                    d[k] = v.strip('"') if v.startswith('"') else float(v)
                jsd[name] = d
        if jsd != pyv:
            for name in sorted(set(jsd) | set(pyv)):
                if jsd.get(name) != pyv.get(name):
                    print(label, "差异", name, "py:", pyv.get(name), "js:", jsd.get(name))
                    bad += 1
        print("[v2] %s %d 条" % (label, len(pyv)))

    # 6) 单一真源结构：两个前端都只 import，不带词表副本
    for path, label in ((WIDGET, "js/sdxl_camera_widget.js"),
                        (STUDIO_CAM, "studio/web/camera3d.js")):
        if not os.path.isfile(path):
            print("[v2] %s 不存在，跳过" % label)
            continue
        t = open(path, encoding="utf-8").read()
        if "camera_shared.js" not in t:
            print("[v2] %s 未 import camera_shared.js" % label)
            bad += 1
        if re.search(r"const\s+(V|NEG)\s*=\s*\{", t):
            print("[v2] %s 自带 V/NEG 词表副本" % label)
            bad += 1
    return bad


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
    bad3 = check_v2(src)
    print("TOTAL 差异 = %d" % (bad + bad2 + bad3))
    return 0 if (bad + bad2 + bad3) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
