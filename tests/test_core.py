# -*- coding: utf-8 -*-
"""核心算法自测：纯 assert，python sdxl_camera_angle/tests/test_core.py 直跑。

覆盖 SPEC 验收项：权重曲线、8 档方位与双档插值、方位门控、距离 6 档边界、
dutch、负向开关、权重区间、预设覆盖、camera_info 可 JSON 化。
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_CORE = os.path.join(os.path.dirname(_HERE), "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from camera_core import (  # noqa: E402
    SDXL_TAGS, compute_camera, sdxl_weight, azimuth_bins, distance_key,
    elevation_key, az_gate,
)
from presets import PRESETS, PRESET_NAMES  # noqa: E402

W_OK = 0.05


def split_tokens(s):
    """按顶层逗号切 token（括号内的逗号不切）。"""
    out, buf, depth = [], "", 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return [t for t in out if t]


def weights_of(s):
    """从输出串里取出所有权重；裸短语记 1.0。"""
    ws = []
    for tok in split_tokens(s):
        if tok.startswith("(") and tok.endswith(")"):
            inner = tok[1:-1]
            ws.append(round(float(inner[inner.rfind(":") + 1:]), 2))
        else:
            ws.append(1.0)
    return ws


def pos(p_x=0.0, p_y=0.0, p_z=0.0, roll=0.0, **kw):
    return compute_camera(p_x, p_y, p_z, roll, **kw)


# ══════════ 1. 权重曲线 ══════════
def test_weight_curve():
    cases = [(10.0, 1.40), (5.0, 1.30), (2.0, 1.13), (1.0, 1.00), (0.1, 0.88)]
    for raw, exp in cases:
        got = sdxl_weight(raw)
        assert got == exp, "sdxl_weight(%s)=%s 期望 %s" % (raw, got, exp)
    # 曲线单调，且两端都在 SDXL 友好区间
    prev = 0.0
    for i in range(1, 101):
        w = sdxl_weight(i * 0.1)
        assert w >= prev - 1e-9, "曲线不单调 raw=%s" % (i * 0.1)
        prev = w
    assert 0.05 <= sdxl_weight(1e-6) <= 2.0
    assert 0.05 <= sdxl_weight(9e9) <= 2.0
    # raw 直通 / linear 截断也钳在 [0.05,2.0]
    assert sdxl_weight(99.0, 1.0, "raw") == 2.0
    assert sdxl_weight(0.01, 1.0, "raw") == 0.05
    print("[ok] 权重曲线 tanh: raw 10->%.2f 5->%.2f 2->%.2f 1->%.2f 0.1->%.2f (raw=0.5->%.2f)"
          % (sdxl_weight(10), sdxl_weight(5), sdxl_weight(2),
             sdxl_weight(1), sdxl_weight(0.1), sdxl_weight(0.5)))


# ══════════ 2. 方位档位与双档插值 ══════════
def test_azimuth_bins():
    # pos_x -> (主导档, 期望权重)
    table = [
        (0.0, "front", 1.40),
        (0.25, "front_right", 1.40),
        (0.5, "right", 1.40),
        (0.75, "back_right", 1.40),
        (1.0, "back", 1.40),
        (-0.25, "front_left", 1.40),
        (-0.5, "left", 1.40),
        (-0.75, "back_left", 1.40),
        (-1.0, "back", 1.40),
    ]
    for x, key, w in table:
        out = pos(x)
        bins = out["camera_info"]["bins"]
        assert len(bins) == 1, "pos_x=%s 应为单档, 实际 %s" % (x, bins)
        assert bins[0][0] == key, "pos_x=%s 主导档 %s 期望 %s" % (x, bins[0][0], key)
        phrase = SDXL_TAGS[key].split(",")[0].strip()
        assert pos(x)["positive"].startswith("(%s:%.2f)" % (phrase, w)), \
            "pos_x=%s 输出 %s" % (x, out["positive"][:60])

    # 双档插值：0.125 = front/front_right 各半
    out = pos(0.125)
    bins = out["camera_info"]["bins"]
    assert len(bins) == 2, "pos_x=0.125 应双档, 实际 %s" % (bins,)
    assert [b[0] for b in bins] == ["front", "front_right"], bins
    assert abs(bins[0][1] - 0.5) < 1e-6 and abs(bins[1][1] - 0.5) < 1e-6, bins
    assert "(front view:1.30)" in out["positive"]
    assert "(three-quarter view from the right:1.30)" in out["positive"]

    # 0.05（UI 步长）→ front 0.8 / front_right 0.2
    out = pos(0.05)
    bins = out["camera_info"]["bins"]
    assert abs(bins[0][1] - 0.8) < 1e-6 and abs(bins[1][1] - 0.2) < 1e-6, bins
    assert "(front view:1.37)" in out["positive"], out["positive"][:80]
    assert "(three-quarter view from the right:1.13)" in out["positive"]

    # |pos_x|=1 接缝连续：1.0 与 0.999 主导档都是 back
    assert pos(0.999)["camera_info"]["bin"] == "back"
    assert pos(1.0)["camera_info"]["bin"] == "back"
    # 4bin 对照模式
    four = pos(0.5, azimuth_mode="4bin")
    assert four["camera_info"]["bin"] in ("right", "left"), four["camera_info"]["bins"]
    print("[ok] 方位 8 档: 0->front 0.25->front_right 0.5->right 0.75->back_right "
          "1->back; 0.125 双档各 0.5 (1.30/1.30); 0.05 -> %s" % (bins,))


# ══════════ 3. 方位门控 ══════════
def test_az_gate():
    assert abs(az_gate(0.0) - 1.0) < 1e-9
    assert abs(az_gate(0.9) - 1.0) < 1e-9, az_gate(0.9)
    assert abs(az_gate(0.95) - 0.5) < 1e-9, az_gate(0.95)
    assert az_gate(1.0) == 0.0
    # 权重随门控线性衰减：raw 10 -> 10*0.5 = 5 -> 1.30
    assert "(front view:1.40)" in pos(0.0, 0.0)["positive"]
    assert "(front view:1.40)" in pos(0.0, 0.9)["positive"]
    assert "(front view:1.30)" in pos(0.0, 0.95)["positive"]
    # |pos_y|=1 → 方位完全关闭
    out = pos(0.5, 1.0)
    assert out["camera_info"]["bins"] == [], out["camera_info"]["bins"]
    assert "side view" not in out["positive"], out["positive"]
    print("[ok] az_gate: y=0 ->1.00, y=0.9 ->1.00, y=0.95 ->0.50 (front 1.40->1.30), "
          "y=1.0 -> 0.00 方位无输出")


# ══════════ 4. 距离 6 档边界 ══════════
def test_distance_bins():
    table = [
        (1.0, "ecu"), (0.71, "ecu"), (0.7, "cu"), (0.21, "cu"), (0.2, "medium"),
        (0.0, "medium"), (-0.01, "cowboy"), (-0.34, "cowboy"), (-0.35, "full"),
        (-0.7, "full"), (-0.71, "wide"), (-1.0, "wide"),
    ]
    for z, key in table:
        got = distance_key(z)
        assert got == key, "distance_key(%s)=%s 期望 %s" % (z, got, key)
        assert pos(0.0, 0.0, z)["camera_info"]["distance"] == key
    # 档内权重随 |z| 微增（slope 0.5）
    assert "(medium shot:1.00)" not in pos(0.0, 0.0, 0.0)["positive"]  # 中性写裸短语
    assert "medium shot, waist up" in pos(0.0, 0.0, 0.0)["positive"]
    assert "(extreme close-up:1.07)" in pos(0.0, 0.0, 1.0)["positive"]
    print("[ok] 距离 6 档: .7->cu .2->medium 0->medium -.01->cowboy -.35->full "
          "-.71->wide; z=1.0 ecu 权重 1.07, z=0 medium 裸短语")


# ══════════ 5. dutch angle ══════════
def test_dutch():
    assert pos(0.0, 0.0, 0.0, 0.0)["camera_info"]["dutch"] is False
    out = pos(0.0, 0.0, 0.0, 0.5)
    assert out["camera_info"]["dutch"] is True
    raw = 1.0 + 6.0 * 0.5
    exp = sdxl_weight(raw)
    assert "(dutch angle:%.2f)" % exp in out["positive"], (exp, out["positive"])
    assert pos(0.0, 0.0, 0.0, 0.1)["camera_info"]["dutch"] is True
    assert "dutch" in pos(0.0, 0.0, 0.0, 0.1)["positive"]
    print("[ok] dutch: roll=0 不输出; roll=0.5 -> (dutch angle:%.2f); roll=0.1 出 dutch" % exp)


# ══════════ 6. 负向开关与硬要求 ══════════
def test_negative():
    base = pos(0.0, 0.0, 0.0)
    assert base["negative"] != ""
    assert "front view" not in base["negative"], base["negative"]
    assert "from behind" in base["negative"]
    # neg_strength=0 → 全关
    off = pos(0.0, 0.0, 0.0, neg_strength=0.0)
    assert off["negative"] == "", repr(off["negative"])
    # 负向权重 1.15（neg_strength=1）
    for w in weights_of(base["negative"]):
        assert w == 1.15, weights_of(base["negative"])
    # 正负向镜像：正面 → 压背面
    assert pos(1.0)["negative"].startswith("(front view:1.15)"), pos(1.0)["negative"][:60]
    # extra_negative 原样追加
    ex = pos(0.0, 0.0, 0.0, extra_negative="blurry, lowres")
    assert ex["negative"].endswith("blurry, lowres"), ex["negative"]
    ex2 = pos(0.0, 0.0, 0.0, extra_positive="cinematic lighting")
    assert ex2["positive"].endswith("cinematic lighting"), ex2["positive"]
    print("[ok] 负向: pos_x=0 负向=%r ; neg_strength=0 -> %r"
          % (base["negative"], off["negative"]))


# ══════════ 7. 权重区间全域扫描 ══════════
def test_weight_range():
    vals = [-1.0, -0.9, -0.75, -0.5, -0.35, -0.2, 0.0, 0.05, 0.125, 0.2,
            0.35, 0.5, 0.7, 0.75, 0.9, 1.0]
    n = 0
    for x in vals:
        for y in vals:
            for z in vals:
                for r in (0.0, 0.1, 1.0):
                    for mode in ("tanh", "linear", "raw"):
                        out = pos(x, y, z, r, weight_mode=mode)
                        for s in (out["positive"], out["negative"]):
                            for w in weights_of(s):
                                assert 0.05 <= w <= 2.0, \
                                    "越界 %s w=%s pos=(%s,%s,%s,%s) %s" % (mode, w, x, y, z, r, s)
                                n += 1
                        assert out["positive"], "空正向 pos=(%s,%s,%s)" % (x, y, z)
                        json.dumps(out["camera_info"], allow_nan=False)
    print("[ok] 权重区间: 扫描 %d 个 token，全部 ∈ [0.05, 2.0]；camera_info 均可 json.dumps" % n)


# ══════════ 8. 预设覆盖滑块 ══════════
def test_presets():
    assert PRESET_NAMES[0] == "自定义" and len(PRESET_NAMES) == 12, PRESET_NAMES
    assert len(PRESETS) == 11
    for name in PRESETS:
        t = PRESETS[name]
        assert len(t) == 4 and all(-1.0 <= v <= 1.0 for v in t), (name, t)
    # 滑块全给 0.9，预设必须赢
    out = pos(0.9, 0.9, 0.9, 0.9, preset="右侧90度中景")
    info = out["camera_info"]
    assert info["preset"] == "右侧90度中景"
    assert info["bin"] == "right", info["bins"]
    assert info["elevation"] == "eye"
    assert info["distance"] == "medium"
    assert info["dutch"] is False
    assert "dutch" not in out["positive"]
    # 自定义时滑块生效
    free = pos(0.9, 0.9, 0.9, 0.9, preset="自定义")
    assert free["camera_info"]["preset"] == "自定义"
    assert free["camera_info"]["distance"] == "ecu"
    # 未知预设名 → 当自定义，不崩
    assert pos(0.5, 0, 0, 0, preset="不存在")["camera_info"]["bin"] == "right"
    print("[ok] 预设: 12 个名字（含自定义）; '右侧90度中景' 覆盖滑块 -> bin=right eye medium; "
          "未知名当自定义")


# ══════════ 9. cfg 词表覆盖（深合并） ══════════
def test_cfg_override():
    out = pos(0.0, 0.0, 0.0, cfg={"front": "frontal view"})
    assert "frontal view" in out["positive"], out["positive"][:80]
    assert "front view, facing the camera" not in out["positive"]
    # 深合并：只改方位权重，其余默认值保留
    out2 = pos(0.0, 0.0, 0.0, cfg={"weights": {"azimuth": {"weight": 5.0}}})
    assert "(frontal" not in out2["positive"]
    assert out2["positive"].startswith("(front view:1.30)"), out2["positive"][:40]
    assert out2["camera_info"]["distance"] == "medium"
    # JSON 字符串形式也能吃
    out3 = pos(0.0, 0.0, 0.0, cfg=json.dumps({"front": "dead-on view"}))
    assert "dead-on view" in out3["positive"]
    # 坏 JSON 不崩
    assert pos(0.0, 0.0, 0.0, cfg="{bad json")["positive"]
    print("[ok] cfg: 覆盖 front -> 'frontal view'; 深合并 azimuth.weight=5 -> front 1.30; "
          "JSON 串可吃; 坏 JSON 静默忽略")


# ══════════ 10. 验收组合 ══════════
def test_acceptance():
    out = pos(0.0, 0.0, 0.0)
    ws = weights_of(out["positive"])
    assert 0.05 <= min(ws) and max(ws) <= 1.4, ws
    assert out["positive"].startswith("(front view:1.40)")
    assert "eye level shot" in out["positive"] and "medium shot" in out["positive"]
    info = out["camera_info"]
    for k in ("azimuth_deg", "elev_deg", "bin", "parts", "preset"):
        assert k in info, k
    assert info["azimuth_deg"] == 0.0 and info["elev_deg"] == 0.0 and info["bin"] == "front"
    # 中性权重写裸短语：eye/medium 无括号
    assert "(eye level shot" not in out["positive"]
    print("[ok] 验收 compute_camera(0,0,0,0):\n     positive = %s\n     negative = %s"
          % (out["positive"], out["negative"]))
    print("     info = %s" % json.dumps(info, ensure_ascii=False)[:200])


def main():
    tests = [
        test_weight_curve,
        test_azimuth_bins,
        test_az_gate,
        test_distance_bins,
        test_dutch,
        test_negative,
        test_weight_range,
        test_presets,
        test_cfg_override,
        test_acceptance,
    ]
    for t in tests:
        t()
    print("\nALL %d TESTS PASSED" % len(tests))


if __name__ == "__main__":
    main()
