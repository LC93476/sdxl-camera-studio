# -*- coding: utf-8 -*-
"""核心算法自测：纯 assert，python tests/test_core.py 直跑。

覆盖 SPEC 第 2 节（原算法）与第 7/8/9 节（v2：光学焦距、roll ±45°、
构图偏移、主体朝向/视线解耦、极点消歧）。
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "core"), os.path.join(_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from camera_core import (  # noqa: E402
    SDXL_TAGS, compute_camera, sdxl_weight, azimuth_bins, distance_key,
    elevation_key, az_gate, pole_gate, lens_key, focal_to_fov, framing_keys,
    subject_keys, rel_yaw, clip_tokens, token_budget, LENS_WEIGHT,
    CLIP_TOK_LIMIT, CLIP_TOK_WARN,
)
from presets import (  # noqa: E402
    PRESETS, PRESET_NAMES, PRESET_EXTRAS, SEQUENCES, preset_params,
    sequence_names, get_sequence,
)


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


def az_bins(out):
    """只取方位档（camera_info.bins 里 kind=='az'）的 [key, ratio]。"""
    return [[b[0], b[1]] for b in out["camera_info"]["bins"] if len(b) > 2 and b[2] == "az"]


def tokens_of(s):
    """去权重后的 token 文本集合（判断冲突词用）。"""
    out = set()
    for tok in split_tokens(s):
        if tok.startswith("(") and tok.endswith(")"):
            inner = tok[1:-1]
            out.add(inner[:inner.rfind(":")].strip())
        else:
            out.add(tok)
    return out


# ══════════ 1. 权重曲线 ══════════
def test_weight_curve():
    cases = [(10.0, 1.40), (5.0, 1.30), (2.0, 1.13), (1.0, 1.00), (0.1, 0.88)]
    for raw, exp in cases:
        got = sdxl_weight(raw)
        assert got == exp, "sdxl_weight(%s)=%s 期望 %s" % (raw, got, exp)
    prev = 0.0
    for i in range(1, 101):
        w = sdxl_weight(i * 0.1)
        assert w >= prev - 1e-9, "曲线不单调 raw=%s" % (i * 0.1)
        prev = w
    assert 0.05 <= sdxl_weight(1e-6) <= 2.0
    assert 0.05 <= sdxl_weight(9e9) <= 2.0
    assert sdxl_weight(99.0, 1.0, "raw") == 2.0
    assert sdxl_weight(0.01, 1.0, "raw") == 0.05
    print("[ok] 权重曲线 tanh: raw 10->%.2f 5->%.2f 2->%.2f 1->%.2f 0.1->%.2f"
          % (sdxl_weight(10), sdxl_weight(5), sdxl_weight(2),
             sdxl_weight(1), sdxl_weight(0.1)))


# ══════════ 2. 方位档位与双档插值 ══════════
def test_azimuth_bins():
    table = [
        (0.0, "front"), (0.25, "front_right"), (0.5, "right"), (0.75, "back_right"),
        (1.0, "back"), (-0.25, "front_left"), (-0.5, "left"), (-0.75, "back_left"),
        (-1.0, "back"),
    ]
    for x, key in table:
        out = pos(x)
        bins = az_bins(out)
        assert len(bins) == 1, "pos_x=%s 应为单档, 实际 %s" % (x, bins)
        assert bins[0][0] == key, "pos_x=%s 主导档 %s 期望 %s" % (x, bins[0][0], key)
        phrase = SDXL_TAGS[key].split(",")[0].strip()
        assert ("(%s:1.40)" % phrase) in out["positive"], "pos_x=%s 输出 %s" % (x, out["positive"][:80])

    out = pos(0.125)
    bins = az_bins(out)
    assert len(bins) == 2 and [b[0] for b in bins] == ["front", "front_right"], bins
    assert abs(bins[0][1] - 0.5) < 1e-6 and abs(bins[1][1] - 0.5) < 1e-6, bins
    assert "(front view:1.30)" in out["positive"]
    assert "(three-quarter view from the right:1.30)" in out["positive"]

    out = pos(0.05)
    bins = az_bins(out)
    assert abs(bins[0][1] - 0.8) < 1e-6 and abs(bins[1][1] - 0.2) < 1e-6, bins
    assert "(front view:1.37)" in out["positive"]
    assert "(three-quarter view from the right:1.13)" in out["positive"]

    assert pos(0.999)["camera_info"]["bin"] == "back"
    assert pos(1.0)["camera_info"]["bin"] == "back"
    assert pos(0.5, azimuth_mode="4bin")["camera_info"]["bin"] in ("right", "left")
    for x in (-1.0, -0.5, 0.0, 0.5, 1.0):
        assert len(azimuth_bins(x)) >= 1
    print("[ok] 方位 8 档 + 0.125 双档 1.30/1.30 + 0.05 -> 1.37/1.13 + 4bin 对照")


# ══════════ 3. 方位门控与极点平滑 ══════════
def test_gates():
    assert abs(az_gate(0.0) - 1.0) < 1e-9
    assert abs(az_gate(0.9) - 1.0) < 1e-9
    assert abs(az_gate(0.95) - 0.5) < 1e-9
    assert az_gate(1.0) == 0.0

    assert abs(pole_gate(0.0) - 1.0) < 1e-9
    assert abs(pole_gate(0.75) - 1.0) < 1e-9
    assert abs(pole_gate(0.9) - 0.4) < 1e-9
    assert abs(pole_gate(1.0)) < 1e-9

    # 0.9（81°）：方位被极点压制到 0.4 → front 1.40 -> 1.26
    out = pos(0.0, 0.9)
    assert "(front view:1.26)" in out["positive"], out["positive"][:80]
    assert out["camera_info"]["pole_gate"] == 0.4

    # 0.95：有效强度 0.5*0.2=0.10 < 0.12 → 方位整体抑制
    out = pos(0.0, 0.95)
    assert out["camera_info"]["azimuth_suppressed"] is True, out["camera_info"]
    assert "front view" not in out["positive"], out["positive"]

    out = pos(0.5, 1.0)
    assert az_bins(out) == [], az_bins(out)
    assert "side view" not in out["positive"]
    print("[ok] 门控: az_gate 0.95->0.50; pole_gate 0.75->1 0.9->0.4 1.0->0; "
          "0.9 front 1.26; 0.95 方位抑制")


# ══════════ 4. 距离 6 档边界 ══════════
def test_distance_bins():
    table = [
        (1.0, "ecu"), (0.71, "ecu"), (0.7, "cu"), (0.21, "cu"), (0.2, "medium"),
        (0.0, "medium"), (-0.01, "cowboy"), (-0.34, "cowboy"), (-0.35, "full"),
        (-0.7, "full"), (-0.71, "wide"), (-1.0, "wide"),
    ]
    for z, key in table:
        assert distance_key(z) == key, "distance_key(%s)=%s" % (z, distance_key(z))
        assert pos(0.0, 0.0, z)["camera_info"]["distance"] == key
    assert "medium shot, waist up" in pos(0.0, 0.0, 0.0)["positive"]
    assert "(extreme close-up:1.07)" in pos(0.0, 0.0, 1.0)["positive"]
    print("[ok] 距离 6 档边界全对；z=1.0 ecu 1.07，z=0 medium 裸短语")


# ══════════ 5. 焦距 / FOV ══════════
def test_lens():
    table = [(14, "lens14", 104.25), (24, "lens24", 73.74), (35, "lens35", 54.43),
             (50, "lens50", 39.6), (85, "lens85", 23.91), (135, "lens135", 15.19)]
    for focal, key, fov in table:
        assert lens_key(focal) == key, (focal, lens_key(focal))
        assert abs(focal_to_fov(focal) - fov) < 0.02, (focal, focal_to_fov(focal))
        out = pos(0.0, 0.0, 0.0, focal=focal)
        info = out["camera_info"]
        assert info["lens"] == key and abs(info["fov_deg"] - fov) < 0.02, info
        assert info["focal"] == float(focal)
        first = SDXL_TAGS[key].split(",")[0].strip()
        w = sdxl_weight(LENS_WEIGHT[key])
        token = first if abs(w - 1) < 0.005 else "(%s:%.2f)" % (first, w)
        assert token in out["positive"], (key, token, out["positive"][:120])
    # 分档边界
    assert lens_key(17.9) == "lens14" and lens_key(18.1) == "lens24"
    assert lens_key(120) == "lens135" and lens_key(4) == "lens14"
    # 超广角带畸变词、长焦带压缩/虚化词
    assert "strong perspective distortion" in pos(0.0, 0.0, 0.0, focal=14)["positive"]
    assert "creamy bokeh" in pos(0.0, 0.0, 0.0, focal=135)["positive"]
    # 负向镜像：广角 → 压长焦压缩
    assert "telephoto compression" in pos(0.0, 0.0, 0.0, focal=14, neg_strength=1.0)["negative"]
    print("[ok] 焦距: 14/24/35/50/85/135 -> %s；FOV 104.25/73.74/54.43/39.6/23.91/15.19；"
          "14mm 1.30、135mm 1.30、50mm 裸短语" % [lens_key(f) for f, _, _ in table])


# ══════════ 6. roll ±45° ══════════
def test_roll():
    assert pos(0.0, 0.0, 0.0, 0.0)["camera_info"]["dutch"] is False
    assert pos(0.0, 0.0, 0.0, 0.1)["camera_info"]["dutch"] is False, "0.1(4.5°) 低于阈值"
    out = pos(0.0, 0.0, 0.0, 0.3)
    assert out["camera_info"]["dutch"] is True
    assert out["camera_info"]["roll_deg"] == 13.5
    exp = sdxl_weight(1 + 6 * 0.3)
    assert "(dutch angle:%.2f)" % exp in out["positive"], out["positive"]
    neg = pos(0.0, 0.0, 0.0, -0.3)
    assert neg["camera_info"]["dutch"] is True and neg["camera_info"]["roll_deg"] == -13.5
    assert "(reverse canted framing:%.2f)" % exp in neg["positive"], neg["positive"]
    assert pos(0.0, 0.0, 0.0, -1.0)["camera_info"]["roll_deg"] == -45.0
    print("[ok] roll ±45°: |roll|<=0.25 不输出; 0.3 -> dutch 正向 %.2f; -0.3 -> reverse cant %.2f" % (exp, exp))


# ══════════ 7. 构图偏移（三分法 / 留白） ══════════
def test_framing():
    assert framing_keys(0.0, 0.0) == [["subject_center", 1.0]]
    out = pos(0.0, 0.0, 0.0, pan_x=0.5)
    assert out["camera_info"]["framing"] == ["subject_left"], out["camera_info"]["framing"]
    assert "(rule of thirds:1.13)" in out["positive"], out["positive"]
    assert "negative space on the right" in out["positive"]
    out = pos(0.0, 0.0, 0.0, pan_x=-0.6)
    assert out["camera_info"]["framing"] == ["subject_right"]
    assert "subject placed on the right third" in out["positive"]
    out = pos(0.0, 0.0, 0.0, pan_y=0.5)
    assert out["camera_info"]["framing"] == ["subject_low"]
    assert "extra headroom above" in out["positive"]
    out = pos(0.0, 0.0, 0.0, pan_y=-0.5)
    assert "subject in the upper third" in out["positive"]
    # 正中构图默认不发词（省 token）
    assert "centered composition" not in pos(0.0, 0.0, 0.0)["positive"]
    # xy 同时偏移 → 两个词都出
    both = pos(0.0, 0.0, 0.0, pan_x=0.5, pan_y=0.5)
    assert both["camera_info"]["framing"] == ["subject_left", "subject_low"], both["camera_info"]["framing"]
    print("[ok] 构图偏移: pan_x=0.5 -> 左三分(1.13)+右侧留白; pan_x=-0.6 -> 右三分; "
          "pan_y=±0.5 -> 下三分/上三分; 正中默认不发词")


# ══════════ 8. 主体朝向 / 视线解耦 ══════════
def test_subject_gaze():
    # 正面机位 + 正对：subject_front 与方位词重复 → 跳过，只留视线
    out = pos(0.0, 0.0, 0.0)
    assert out["camera_info"]["subject"] == "subject_front"
    assert "front-facing body" not in out["positive"], "正面机位不该重复朝向词"
    assert "looking at viewer" in out["positive"]
    # 世界系朝向：相机在背后（pos_x=1）+ 人朝世界正面（yaw=0）→ 相对相机是背身
    out = pos(1.0, 0.0, 0.0, subject_yaw=0.0)
    toks = tokens_of(out["positive"])
    info = out["camera_info"]
    assert "from behind" in toks, toks
    assert "front-facing body" not in toks, "背面机位不该再说人正对镜头"
    assert info["rel_yaw"] == -1.0, info["rel_yaw"]
    assert info["subject"] == "subject_away", info["subject"]
    assert info["gaze"] == "gaze_away", info["gaze"]
    # 与 from behind/back view 重复，subject_away 不再重复发一遍
    assert "facing away" not in toks, toks
    assert "looking at viewer" not in toks

    # 背面机位 + 人转 0.4（世界系）→ 相对 0.6 背身 3/4，过头看镜头
    out = pos(1.0, 0.0, 0.0, subject_yaw=0.4)
    info = out["camera_info"]
    assert abs(info["rel_yaw"] - (-0.6)) < 1e-6, info["rel_yaw"]
    assert info["subject"] == "subject_turn", info["subject"]
    assert info["gaze"] == "gaze_over_shoulder", info["gaze"]
    assert "(body turned three-quarters away from the camera:1.15)" in out["positive"], out["positive"]

    # 正面机位 + 人朝前：仍是正对
    out = pos(0.0, 0.0, 0.0, subject_yaw=0.0)
    assert out["camera_info"]["rel_yaw"] == 0.0
    assert out["camera_info"]["gaze"] == "gaze_viewer"
    # 转身 3/4 背对 + 背面机位 → 过头看镜头
    out = pos(1.0, 0.0, 0.0, subject_yaw=-0.45)
    assert out["camera_info"]["subject"] == "subject_turn"
    assert out["camera_info"]["gaze"] == "gaze_over_shoulder"
    assert "(looking back over the shoulder at the viewer:1.05)" in out["positive"], out["positive"]
    # 完全背对 → gaze_away
    out = pos(0.0, 0.0, 0.0, subject_yaw=0.9)
    assert out["camera_info"]["subject"] == "subject_away"
    assert out["camera_info"]["gaze"] == "gaze_away"
    assert "(facing away:1.19)" in out["positive"], out["positive"]
    # gaze_mode 强制
    assert pos(0.0, 0.0, 0.0, subject_yaw=0.9, gaze_mode="camera")["camera_info"]["gaze"] == "gaze_viewer"
    assert pos(0.0, 0.0, 0.0, gaze_mode="away")["camera_info"]["gaze"] == "gaze_away"
    # 主体朝向不影响机位方位词（解耦）
    a = pos(0.0, 0.0, 0.0, subject_yaw=0.9)["camera_info"]["bin"]
    assert a == "front"
    print("[ok] 主体朝向/视线: yaw 是世界系朝向, rel=wrap(yaw-pos_x); "
          "背机位+yaw0 -> away+away(去重不发 facing away); 背机位+yaw0.4 -> turn+over_shoulder; "
          "正面+正对去重; yaw=0.9 -> away(1.19)")


# ══════════ 8b. CLIP token 预算 ══════════
def test_token_budget():
    out = pos(0.0, 0.0, 0.0, neg_strength=1.0)
    tb = out["camera_info"]["tokens"]
    assert tb["limit"] == 75 and tb["warn"] == 65, tb
    assert tb["positive"] == clip_tokens(out["positive"]), tb
    assert tb["negative"] == clip_tokens(out["negative"]), tb
    assert tb["total"] == tb["positive"] + tb["negative"], tb
    assert tb["level"] in ("ok", "warn", "danger"), tb
    # 权重语法不额外计数
    assert clip_tokens("(front view:1.40)") == clip_tokens("front view") == 3, clip_tokens("front view")
    assert clip_tokens("") == 0
    # 逐档判定：75 是上限，65 开始提示
    assert token_budget("a" * 3)["level"] == "ok"
    heavy = ", ".join(["subject placed on the left third"] * 8)
    assert token_budget(heavy)["level"] == "warn", token_budget(heavy)
    huge = ", ".join(["subject placed on the left third"] * 12)
    assert token_budget(huge)["level"] == "danger", token_budget(huge)
    assert CLIP_TOK_LIMIT == 75 and CLIP_TOK_WARN == 65
    print("[ok] token 预算: 默认 %d tok(%s)；权重语法不计数；>65 标黄 >75 标红"
          % (tb["total"], tb["level"]))


def test_world_yaw_geometry():
    # 相对角 = wrap(yaw - pos_x)：同一 yaw 在不同机位下结论不同
    assert rel_yaw(0.0, 0.0) == 0.0
    assert abs(rel_yaw(0.0, 1.0) + 1.0) < 1e-9
    assert abs(rel_yaw(0.0, -1.0) + 1.0) < 1e-9
    assert abs(rel_yaw(0.5, 0.5)) < 1e-9
    assert abs(rel_yaw(0.4, 1.0) + 0.6) < 1e-9
    # 环绕一圈：yaw 固定时，机位每转 0.25（45°）相对角就变 0.25
    for px, want in ((0.0, 0.5), (0.25, 0.25), (-0.25, 0.75), (0.5, 0.0)):
        got = rel_yaw(0.5, px)
        assert abs(((got - want + 1.0) % 2.0) - 1.0) < 1e-9, (px, got, want)
    # subject_keys 与 rel_yaw 同步：|rel|<0.28 正对、<0.72 转身、否则背对
    assert subject_keys(0.0, 1.0)[0] == "subject_away"
    assert subject_keys(0.5, 1.0)[0] == "subject_turn"
    assert subject_keys(0.5, 0.5)[0] == "subject_front"
    print("[ok] 世界系朝向: rel=wrap(yaw-pos_x)；yaw=0.5 在 pos_x=0.5 时正对、在 pos_x=1 时背对")


# ══════════ 9. 极点互斥 ══════════
def test_pole_conflicts():
    out = pos(0.0, 1.0, 0.0, elevation_mode="5bin")
    toks = tokens_of(out["positive"])
    assert "top-down view" in toks
    assert "from behind" not in toks and "seen from behind" not in toks
    assert out["camera_info"]["azimuth_suppressed"] is True

    # 0.8（72°）：bird 已触发，但方位还在（0.8*10=8 → 1.37），冲突 token 被剔除
    out = pos(0.0, 0.8, 0.0, elevation_mode="5bin")
    toks = tokens_of(out["positive"])
    assert "front view" not in toks and "facing the camera" not in toks, toks
    assert "from above" in toks
    assert out["camera_info"]["azimuth_suppressed"] is False

    # 全角度扫描：任意组合不得同时出现互斥短语
    bad = []
    for x in (-1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0):
        for y in (-1.0, -0.85, -0.8, -0.5, 0.0, 0.5, 0.8, 0.85, 1.0):
            for mode in ("3bin", "5bin"):
                t = tokens_of(pos(x, y, 0.0, elevation_mode=mode)["positive"])
                if t & {"from behind", "back view", "seen from behind"} and t & {"top-down view", "looking down at viewer"}:
                    bad.append((x, y, mode))
                if t & {"from below", "low angle"} and t & {"from above", "high angle"}:
                    bad.append((x, y, mode))
    assert not bad, "极点冲突组合: %s" % bad[:6]

    # 极点视线改写：竖直机位不得再发 "looking at viewer"（会画成正面站立）
    up = tokens_of(pos(0.0, 0.95, 0.0)["positive"])
    assert "looking up at the viewer" in up, up
    assert "looking at viewer" not in up and "direct eye contact" not in up, up
    down = tokens_of(pos(0.0, -0.95, 0.0)["positive"])
    assert "looking down at the viewer" in down, down
    assert "looking at viewer" not in down, down
    # 非极点区保持原短语
    mid = tokens_of(pos(0.0, 0.5, 0.0)["positive"])
    assert "looking at viewer" in mid and "direct eye contact" in mid, mid

    # 极点锁存（v2.1）：3bin 拖到底也要真顶视/真贴地，而不是"稍高的平视"
    lat = tokens_of(pos(0.0, 0.95, 0.0)["positive"])
    assert "bird's-eye view" in lat and "top-down view" in lat, lat
    assert "high angle shot" not in lat, lat
    floor = tokens_of(pos(0.0, -0.95, 0.0)["positive"])
    assert "worm's-eye view" in floor and "from below" in floor, floor
    assert "low angle shot" not in floor, floor
    # 未到锁存阈值（0.9）仍是 3bin 的档位词
    near = tokens_of(pos(0.0, 0.8, 0.0)["positive"])
    assert "high angle shot" in near and "bird's-eye view" not in near, near
    print("[ok] 极点: py=1.0 5bin 无 from behind 且 azimuth_suppressed；py=0.8 冲突 token 被剔除；"
          "8 方位 × 9 俯仰 × 2 模式 = 144 组无互斥词；极点视线改 looking up/down at the viewer")


# ══════════ 10. 负向开关与硬要求 ══════════
def test_negative():
    base = pos(0.0, 0.0, 0.0, neg_strength=1.0)
    assert base["negative"] != ""
    assert "front view" not in base["negative"]
    assert "from behind" in base["negative"]
    off = pos(0.0, 0.0, 0.0, neg_strength=0.0)
    assert off["negative"] == "", repr(off["negative"])
    for w in weights_of(base["negative"]):
        assert w == 1.15, weights_of(base["negative"])
    assert pos(1.0, neg_strength=1.0)["negative"].startswith("(front view:1.15)")
    # 负向去重（同一 token 只出一次）
    neg_tokens = split_tokens(base["negative"])
    assert len(neg_tokens) == len(set(neg_tokens)), "负向有重复 token"
    ex = pos(0.0, 0.0, 0.0, extra_negative="blurry, lowres")
    assert ex["negative"].endswith("blurry, lowres")
    ex2 = pos(0.0, 0.0, 0.0, extra_positive="cinematic lighting")
    assert ex2["positive"].endswith("cinematic lighting")
    print("[ok] 负向: 默认 %d 个 token 全 1.15 且无重复; neg_strength=0 -> 空串"
          % len(neg_tokens))


# ══════════ 11. 权重区间全域扫描（含 v2 参数） ══════════
def test_weight_range():
    vals = [-1.0, -0.75, -0.35, -0.2, 0.0, 0.05, 0.125, 0.25, 0.5, 0.75, 0.9, 1.0]
    n = 0
    for x in vals:
        for y in vals:
            for z in (1.0, 0.5, 0.0, -0.5, -1.0, 0.7, -0.35):
                for r in (-1.0, -0.3, 0.0, 0.3, 1.0):
                    for focal in (14.0, 35.0, 50.0, 135.0):
                        out = compute_camera(x, y, z, r, focal=focal,
                                             pan_x=x * 0.5, pan_y=y * 0.5,
                                             subject_yaw=z, neg_strength=1.0)
                        for s in (out["positive"], out["negative"]):
                            for w in weights_of(s):
                                assert 0.05 <= w <= 2.0, "越界 %s" % s
                                n += 1
                        assert out["positive"]
                        json.dumps(out["camera_info"], allow_nan=False)
    print("[ok] 权重区间: 扫描 %d 个 token 全部 ∈ [0.05,2.0]；camera_info 可 json.dumps" % n)


# ══════════ 12. 预设（含 v2 扩展与预设生效） ══════════
def test_presets():
    assert PRESET_NAMES[0] == "自定义"
    assert len(PRESETS) == 16, len(PRESETS)
    assert len(PRESET_NAMES) == 17, len(PRESET_NAMES)
    for name, t in PRESETS.items():
        assert len(t) == 4 and all(-1.0 <= v <= 1.0 for v in t), (name, t)
    assert set(PRESET_EXTRAS) <= set(PRESETS)
    for name in PRESET_EXTRAS:
        assert preset_params(name)["pos_x"] == PRESETS[name][0]

    # 预设覆盖滑块
    out = pos(0.9, 0.9, 0.9, 0.9, preset="右侧90度中景")
    info = out["camera_info"]
    assert info["bin"] == "right" and info["elevation"] == "eye"
    assert info["distance"] == "medium" and info["dutch"] is False

    # v2 预设：长焦人像学 85mm
    p = preset_params("长焦人像85mm")
    assert p["focal"] == 85.0 and p["pos_z"] == 0.1
    out = pos(0.0, 0.0, 0.0, focal=p["focal"])
    assert out["camera_info"]["lens"] == "lens85"
    # 三分法预设
    p = preset_params("三分法偏右")
    assert p["pan_x"] == -0.35 and p["focal"] == 35.0
    # 回眸预设
    p = preset_params("回眸背面机位")
    assert p["pos_x"] == 1.0 and p["subject_yaw"] == -0.45 and p["gaze_mode"] == "camera"
    # 负向荷兰角
    p = preset_params("荷兰角负斜")
    assert PRESETS["荷兰角负斜"][3] == -0.45
    # 未知预设不崩
    assert pos(0.5, preset="不存在")["camera_info"]["bin"] == "right"
    print("[ok] 预设: %d 条 + 自定义 = %d 个名字；长焦85/三分法/回眸/负斜 参数齐全" %
          (len(PRESETS), len(PRESET_NAMES)))


# ══════════ 13. 多机位序列 ══════════
def test_sequences():
    names = sequence_names()
    assert len(names) == 4, names
    for name in names:
        seq = get_sequence(name)
        assert seq and len(seq["shots"]) >= 4, name
        for shot in seq["shots"]:
            assert shot["label"], name
            merged = dict(pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0, focal=50.0)
            merged.update(shot["params"])
            out = compute_camera(merged.pop("pos_x"), merged.pop("pos_y"),
                                 merged.pop("pos_z"), merged.pop("roll"), **merged)
            assert out["positive"], (name, shot)
    tri = get_sequence("三视图（正/45°/侧/背）")
    assert [s["label"] for s in tri["shots"]] == ["正面", "右前45°", "右侧90°", "背面"]
    b = get_sequence("环绕 8 机位")
    assert len(b["shots"]) == 8
    assert get_sequence("不存在") is None
    print("[ok] 序列: %s，三视图 4 张 / 环绕 8 张 / 推拉 6 张 / 俯仰 5 张 均可独立成词" % ", ".join(names))


# ══════════ 14. cfg 词表覆盖（深合并） ══════════
def test_cfg_override():
    out = pos(0.0, 0.0, 0.0, cfg={"front": "frontal view"})
    assert "frontal view" in out["positive"]
    assert "front view, straight-on shot" not in out["positive"]
    out2 = pos(0.0, 0.0, 0.0, cfg={"weights": {"azimuth": {"weight": 5.0}}})
    assert out2["positive"].startswith("(frontal" if False else "(front view:1.30)"), out2["positive"][:40]
    assert out2["camera_info"]["distance"] == "medium"
    out3 = pos(0.0, 0.0, 0.0, cfg=json.dumps({"front": "dead-on view"}))
    assert "dead-on view" in out3["positive"]
    assert pos(0.0, 0.0, 0.0, cfg="{bad json")["positive"]
    # 关闭某一维度
    off = pos(0.0, 0.0, 0.0, focal=14, cfg={"weights": {"lens": {"enabled": False}}})
    assert off["camera_info"]["lens"] == "lens14"
    assert "ultra wide angle lens" not in off["positive"]
    print("[ok] cfg: 覆盖 front / 深合并 azimuth.weight=5 -> 1.30 / JSON 串可吃 / 坏 JSON 静默 / "
          "lens.enabled=False 可关维度")


# ══════════ 15. 验收组合 ══════════
def test_acceptance():
    out = pos(0.0, 0.0, 0.0, neg_strength=1.0)
    ws = weights_of(out["positive"])
    assert 0.05 <= min(ws) and max(ws) <= 1.4, ws
    assert out["positive"].startswith("(front view:1.40)")
    assert "eye level shot" in out["positive"] and "medium shot" in out["positive"]
    info = out["camera_info"]
    for k in ("azimuth_deg", "elev_deg", "bin", "parts", "preset", "focal", "fov_deg",
              "lens", "roll_deg", "pole_gate", "azimuth_suppressed", "framing",
              "subject", "gaze"):
        assert k in info, k
    assert info["azimuth_deg"] == 0.0 and info["elev_deg"] == 0.0 and info["bin"] == "front"
    assert "(eye level shot" not in out["positive"]
    kinds = {p["kind"] for p in info["parts"]}
    assert kinds == {"az", "el", "di", "lens", "gaze"}, kinds
    print("[ok] 验收 compute_camera(0,0,0,0):\n     positive = %s\n     negative = %s"
          % (out["positive"], out["negative"][:120]))


def main():
    tests = [
        test_weight_curve,
        test_azimuth_bins,
        test_gates,
        test_distance_bins,
        test_lens,
        test_roll,
        test_framing,
        test_subject_gaze,
        test_token_budget,
        test_world_yaw_geometry,
        test_pole_conflicts,
        test_negative,
        test_weight_range,
        test_presets,
        test_sequences,
        test_cfg_override,
        test_acceptance,
    ]
    for t in tests:
        t()
    print("\nALL %d TESTS PASSED" % len(tests))


if __name__ == "__main__":
    main()
