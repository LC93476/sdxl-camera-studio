# -*- coding: utf-8 -*-
"""SDXL 相机控制 —— 核心映射（唯一权威实现）

坐标约定（与 SPEC.md 一致）：
    pos_x ∈ [-1,1]  方位角：-1=左侧 0=正面 +1=背面（θ = pos_x*180°）
    pos_y ∈ [-1,1]  俯仰角：-1=正下方仰视 +1=正上方俯视（elev = pos_y*90°）
    pos_z ∈ [-1,1]  距离：+1 特写 … -1 远景
    roll  ∈ [-1,1]  翻滚角：±1 = ±45°（正=顺时针荷兰角，负=逆时针）

本模块产出的提示词被 ComfyUI 节点（nodes/camera_sdxl_node.py）与
Studio 后端（studio/server.py）共用；js/camera_shared.js 是它的 JS 镜像，
两端一致性由 tools/parity_check.py 校验。
"""

import copy
import math

# ── 输出权重区间（SPEC 2.1） ──────────────────────────────
W_MIN_OUT = 0.05
W_MAX_OUT = 2.0
W_NEUTRAL = 1.0
BARE_EPS = 0.005
AZ_POLE = 0.9

# 极点：|elev| > 75°（即 |pos_y| > 0.8333）时方位角几何意义消失
POLE_DEG = 75.0
POLE_NORM = POLE_DEG / 90.0
POLE_START = 0.75          # 从此处开始线性衰减，1.0 处归零
POLE_LATCH = 0.9           # |pos_y| 超过这里就把 3bin 的 high/low 升级成 bird/worm（真顶视/真贴地）
AZ_SUPPRESS = 0.12         # 有效方位强度低于此值直接丢弃方位词

# ── 档位定义 ────────────────────────────────────────────
AZ_BINS = ["back", "back_left", "left", "front_left", "front", "front_right", "right", "back_right"]
EL_KEYS = ["bird", "worm", "high", "eye", "low"]
DIST_KEYS = ["ecu", "cu", "medium", "cowboy", "full", "wide"]

# 焦距（等效 36mm 全画幅）分档：key → (最大焦距, 原始权重)
LENS_KEYS = ["lens14", "lens24", "lens35", "lens50", "lens85", "lens135"]
LENS_BANDS = ((18.0, "lens14"), (29.0, "lens24"), (43.0, "lens35"),
              (65.0, "lens50"), (105.0, "lens85"), (1e9, "lens135"))
# 原始权重（tanh 曲线输入端，1.0 即 1.00 裸写；≈5 → 1.30，≈3 → 1.22）
LENS_WEIGHT = {"lens14": 5.0, "lens24": 3.2, "lens35": 1.8,
               "lens50": 1.0, "lens85": 4.0, "lens135": 5.0}

FRAME_KEYS = ["subject_left", "subject_right", "subject_center", "subject_low", "subject_high"]
SUBJECT_KEYS = ["subject_front", "subject_turn", "subject_away"]
GAZE_KEYS = ["gaze_viewer", "gaze_away", "gaze_over_shoulder"]

# ── 词表（SPEC 2.5；节点可用 _cfg_json 覆盖单条） ──────────
SDXL_TAGS = {
    # 方位 8 档
    "front": "front view, straight-on shot, facing the camera",
    "front_right": "three-quarter view from the right",
    "right": "side view, profile view from the right",
    "back_right": "three-quarter rear view from the right",
    "back": "from behind, back view, seen from behind",
    "back_left": "three-quarter rear view from the left",
    "left": "side view, profile view from the left",
    "front_left": "three-quarter view from the left",
    # 高度
    "high": "high angle shot, from above",
    "eye": "eye level shot",
    "low": "low angle shot, from below, looking up",
    # 极点高度：只描述机位（不含 "looking down/up at viewer"，视线交给 POLE_GAZE）
    "bird": "bird's-eye view, top-down view, aerial shot, from above",
    "worm": "worm's-eye view, extreme low angle, from below",
    # 距离
    "ecu": "extreme close-up, macro shot",
    "cu": "close-up, portrait shot",
    "medium": "medium shot, waist up",
    "cowboy": "cowboy shot, mid-thigh up",
    "full": "full body shot",
    "wide": "wide shot, long shot, full figure in frame",
    # 倾斜
    "dutch": "dutch angle, canted framing, tilted horizon",
    "dutch_neg": "dutch angle, reverse canted framing, tilted frame",
    # 焦距 / 光学
    "lens14": "ultra wide angle lens, 14mm, strong perspective distortion, exaggerated foreshortening, deep depth of field",
    "lens24": "wide angle lens, 24mm, wide perspective, deep depth of field, environmental context",
    "lens35": "35mm lens, natural perspective, environmental portrait",
    "lens50": "50mm lens, standard perspective, natural proportions",
    "lens85": "85mm lens, telephoto compression, shallow depth of field, background blur, bokeh",
    "lens135": "135mm telephoto lens, strong spatial compression, creamy bokeh, isolated subject",
    # 构图偏移
    "subject_left": "subject placed on the left third, rule of thirds, off-center composition, negative space on the right",
    "subject_right": "subject placed on the right third, rule of thirds, off-center composition, negative space on the left",
    "subject_center": "centered composition, subject in the middle of the frame",
    "subject_low": "extra headroom above, subject in the lower third, low horizon line",
    "subject_high": "subject in the upper third, foreground leading space",
    # 主体朝向 / 视线（与机位解耦）
    "subject_front": "front-facing body, shoulders square to the camera",
    "subject_turn": "body turned three-quarters away from the camera",
    "subject_away": "facing away, back turned to the camera",
    "gaze_viewer": "looking at viewer, direct eye contact",
    "gaze_away": "looking away, gaze averted",
    "gaze_over_shoulder": "looking back over the shoulder at the viewer",
    # 负向镜像
    "neg": {
        "front": "from behind, back view, seen from behind",
        "front_right": "three-quarter rear view from the left",
        "right": "side view from the left, profile from the left",
        "back_right": "three-quarter view from the left",
        "back": "front view, facing the camera",
        "back_left": "three-quarter view from the right",
        "left": "side view from the right",
        "front_left": "three-quarter rear view from the right",
        "high": "low angle shot, from below, looking up",
        "low": "high angle shot, from above",
        "eye": "high angle, low angle",
        "bird": "worm's-eye view, extreme low angle, from below",
        "worm": "bird's-eye view, top-down view, from above",
        "ecu": "wide shot, full body",
        "cu": "wide shot, full body, distant",
        "medium": "close-up, extreme close-up",
        "cowboy": "close-up, extreme close-up",
        "full": "close-up, portrait shot",
        "wide": "close-up, portrait shot",
        "dutch": "horizon level, straight horizon",
        "dutch_neg": "horizon level, straight horizon",
        "lens14": "telephoto compression, compressed perspective",
        "lens24": "telephoto lens, compressed perspective",
        "lens35": "extreme wide angle distortion",
        "lens50": "wide angle distortion, fisheye",
        "lens85": "wide angle distortion, exaggerated perspective",
        "lens135": "wide angle distortion, exaggerated perspective",
        "subject_left": "centered composition, subject in the middle of the frame",
        "subject_right": "centered composition, subject in the middle of the frame",
        "subject_center": "off-center composition, subject pushed to the frame edge",
        "subject_low": "subject centered vertically, no headroom",
        "subject_high": "subject centered vertically, cropped composition",
        "subject_front": "facing away, back turned to the camera",
        "subject_turn": "front view, shoulders square to the camera",
        "subject_away": "front view, facing the camera",
        "gaze_viewer": "looking away, gaze averted",
        "gaze_away": "looking at viewer, direct eye contact",
        "gaze_over_shoulder": "back turned, not looking at the camera",
    },
    # 权重配置（cfg 可覆盖）
    "weights": {
        "azimuth": {"enabled": True, "weight": 10.0, "extra": 0.0, "deadzone_ratio": 0.06},
        "elevation": {"enabled": True, "extra": 0.0, "mult": 10.0, "deadzone": 0.0},
        "distance": {"enabled": True, "extra": 0.0, "slope": 0.5},
        "tilt": {"enabled": True, "extra": 0.0, "slope": 6.0},
        "lens": {"enabled": True, "extra": 0.0},
        "framing": {"enabled": True, "extra": 0.0, "slope": 2.0},
        "subject": {"enabled": True, "extra": 0.0, "slope": 2.0},
        "extra_master": 1.0,
        "weight_min": 0.1,
        "weight_max": 10.0,
        "neg_weight": 1.15,
    },
}

# 极点互斥：竖直轴已给出强语义时，从方位短语里剔除的 token
# 极点区的视线改写：竖直机位下 "looking at viewer" 会把人画成正面站立，
# 换成带俯仰关系的短语才能真的出顶视/贴地视角。
POLE_GAZE = {
    "bird": "looking up at the viewer",
    "worm": "looking down at the viewer",
}

POLE_BLOCK_TOKENS = {
    "bird": ("front view", "straight-on shot", "facing the camera", "eye level shot",
             "looking at viewer", "direct eye contact",
             "from behind", "seen from behind", "back view"),
    "worm": ("eye level shot", "top-down view", "looking down at viewer",
             "from above", "high angle", "bird's-eye view"),
    "high": ("eye level shot",),
    "low": ("eye level shot",),
}


# ══════════════════════ 基础工具 ══════════════════════
def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = lo
    if v != v:  # NaN
        v = lo
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _deep_merge(base, patch):
    out = copy.deepcopy(base)
    if not isinstance(patch, dict):
        return out
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_tags():
    """返回内置词表的深拷贝（节点/服务端各自持有一份，互不污染）。"""
    return copy.deepcopy(SDXL_TAGS)


def _load_tags(cfg):
    if isinstance(cfg, str):
        try:
            import json as _json
            cfg = _json.loads(cfg)
        except ValueError:
            cfg = None
    if not isinstance(cfg, dict):
        return default_tags()
    patch = cfg.get("tags") if isinstance(cfg.get("tags"), dict) else cfg
    tags = default_tags()
    if isinstance(patch, dict):
        for key, value in patch.items():
            if key in tags and isinstance(value, dict) and isinstance(tags[key], dict):
                tags[key].update(value)
            elif isinstance(value, str):
                tags[key] = value
    return tags


def _weights(tags):
    w = tags.get("weights") or {}
    return {
        "azimuth": dict({"enabled": True, "weight": 10.0, "extra": 0.0, "deadzone_ratio": 0.06},
                        **(w.get("azimuth") or {})),
        "elevation": dict({"enabled": True, "extra": 0.0, "mult": 10.0, "deadzone": 0.0},
                          **(w.get("elevation") or {})),
        "distance": dict({"enabled": True, "extra": 0.0, "slope": 0.5},
                         **(w.get("distance") or {})),
        "tilt": dict({"enabled": True, "extra": 0.0, "slope": 6.0},
                     **(w.get("tilt") or {})),
        "lens": dict({"enabled": True, "extra": 0.0}, **(w.get("lens") or {})),
        "framing": dict({"enabled": True, "extra": 0.0, "slope": 2.0},
                        **(w.get("framing") or {})),
        "subject": dict({"enabled": True, "extra": 0.0, "slope": 2.0},
                        **(w.get("subject") or {})),
        "extra_master": float(w.get("extra_master", 1.0) or 1.0),
        "weight_min": float(w.get("weight_min", 0.1) or 0.1),
        "weight_max": float(w.get("weight_max", 10.0) or 10.0),
        "neg_weight": float(w.get("neg_weight", 1.15) or 1.15),
    }


def _split_tags(text):
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


# ══════════════════════ 权重曲线 ══════════════════════
def sdxl_weight(raw, strength=1.0, curve="tanh"):
    """SPEC 2.1：raw∈[weight_min, weight_max] → SDXL 友好的 [0.05, 2.0]。"""
    raw = max(1e-3, _clamp(raw, 1e-3, 1e6))
    strength = 1.0 if strength is None else float(strength)
    if curve == "raw":
        return round(_clamp(raw, W_MIN_OUT, W_MAX_OUT), 2)
    if curve == "linear":
        return round(_clamp(1 + 0.06 * (raw - 1) * strength, W_MIN_OUT, W_MAX_OUT), 2)
    s = 0.55 * strength
    k = 3.3
    if raw >= 1:
        w = 1 + s * (raw - 1) / ((raw - 1) + k)
    else:
        w = 1 - s * (1 - raw) / ((1 - raw) + k)
    return round(_clamp(w, W_MIN_OUT, W_MAX_OUT), 2)


def _emit(phrase, weight):
    """SPEC 2.2：按逗号拆 tag 逐个加权；w≈1 时裸写。"""
    out = []
    for token in _split_tags(phrase):
        if abs(weight - 1) < BARE_EPS:
            out.append(token)
        else:
            out.append("(%s:%.2f)" % (token, weight))
    return out


# ══════════════════════ 各维度映射 ══════════════════════
def az_gate(pos_y):
    """俯仰角对水平方位的压制（0.9 处 1，1.0 处 0）。"""
    return _clamp((1 - abs(_clamp(pos_y, -1, 1))) / (1 - AZ_POLE), 0.0, 1.0)


def pole_gate(pos_y):
    """极点平滑：|pos_y|>0.75 起线性衰减，1.0（正顶/正底）归零。

    SPEC 2.3b：|elev|>75° 时水平方位角在几何上失去意义，必须衰减到 0，
    否则会同时输出 "from behind" 与 "top-down view" 这类自相矛盾的词。
    """
    return _clamp((1 - abs(_clamp(pos_y, -1, 1))) / (1 - POLE_START), 0.0, 1.0)


def azimuth_bins(pos_x, mode="8bin", deadzone_ratio=0.06):
    """返回 [(key, ratio)]；8bin 线性混合，4bin 为原版 cos/sin 对照。"""
    deadzone_ratio = max(0.0, float(deadzone_ratio))
    if mode == "4bin":
        a = _clamp(pos_x, -1, 1) * math.pi
        raw = {
            "front": max(0.0, math.cos(a)),
            "back": max(0.0, -math.cos(a)),
            "right": max(0.0, math.sin(a)),
            "left": max(0.0, -math.sin(a)),
        }
        total = sum(raw.values())
        if total > 0:
            for key in raw:
                raw[key] /= total
        return [[k, raw[k]] for k in ("front", "back", "left", "right") if raw[k] >= deadzone_ratio]

    theta = _clamp(pos_x, -1, 1) * math.pi
    f = (theta + math.pi) / (math.pi / 4.0)
    i0 = int(math.floor(f)) % 8
    frac = f - math.floor(f)
    pairs = [[i0, 1.0 - frac], [(i0 + 1) % 8, frac]]
    return [[AZ_BINS[j], ratio] for j, ratio in pairs if ratio >= deadzone_ratio]


def elevation_key(pos_y, mode="3bin"):
    y = _clamp(pos_y, -1, 1)
    if mode == "5bin" and abs(y) > 0.75:
        return "bird" if y > 0 else "worm"
    if y > 0.2:
        return "high"
    if y >= -0.2:
        return "eye"
    return "low"


def distance_key(pos_z):
    z = _clamp(pos_z, -1, 1)
    if z > 0.7:
        return "ecu"
    if z > 0.2:
        return "cu"
    if z >= 0.0:
        return "medium"
    if z > -0.35:
        return "cowboy"
    if z >= -0.7:
        return "full"
    return "wide"


def lens_key(focal):
    f = _clamp(focal, 4.0, 1200.0)
    for limit, key in LENS_BANDS:
        if f <= limit:
            return key
    return "lens135"


def focal_to_fov(focal):
    """水平视场角（36mm 全画幅宽），单位度。"""
    f = _clamp(focal, 4.0, 1200.0)
    return round(math.degrees(2.0 * math.atan(36.0 / (2.0 * f))), 2)


def framing_keys(pan_x, pan_y):
    """构图偏移 → [(key, ratio)]；pan_x>0 = 相机右移（主体偏左）。"""
    px = _clamp(pan_x, -1, 1)
    py = _clamp(pan_y, -1, 1)
    out = []
    if abs(px) >= 0.12:
        out.append(["subject_left" if px > 0 else "subject_right", min(1.0, abs(px))])
    if abs(py) >= 0.12:
        out.append(["subject_low" if py > 0 else "subject_high", min(1.0, abs(py))])
    if not out:
        out.append(["subject_center", 1.0])
    return out


def wrap_yaw(v):
    """角度折半圈：1.0 = 180°，结果落在 [-1, 1)。"""
    v = _clamp(v, -3.0, 3.0)
    return ((v + 1.0) % 2.0) - 1.0


def rel_yaw(subject_yaw, pos_x):
    """主体相对相机的偏航角（世界坐标系求差）。

    subject_yaw 是主体在**世界坐标系**里的朝向（0 = 面向世界 +Z，±1 = 背向），
    pos_x 是相机方位（0 = 正面机位，±1 = 背面机位），两者同一套角度单位。
    相机绕主体转 pos_x 等价于主体相对相机反向转 pos_x：
        rel = wrap(subject_yaw - pos_x)
    因此 pos_x=1（相机在背后）+ subject_yaw=0（人朝世界正面）→ rel=±1 背身。
    """
    return wrap_yaw(_clamp(subject_yaw, -1, 1) - _clamp(pos_x, -1, 1))


def subject_keys(subject_yaw, pos_x, gaze_mode="auto"):
    """主体朝向 / 视线。返回 (subject_key|None, gaze_key|None, ratio)。

    主体朝向用**相对相机的偏航角** rel_yaw 判定，所以同一个世界系 yaw 在不同机位下
    会得到 front / turn / away 的不同结论 —— 这也是 3D 视口的箭头语义。
    """
    yaw = rel_yaw(subject_yaw, pos_x)
    mag = abs(yaw)
    if mag < 0.28:
        skey, sratio = "subject_front", 1.0
    elif mag < 0.72:
        skey, sratio = "subject_turn", min(1.0, mag)
    else:
        skey, sratio = "subject_away", min(1.0, mag)

    behind_ratio = 0.0
    for key, ratio in azimuth_bins(pos_x):
        if key in ("back", "back_left", "back_right"):
            behind_ratio += ratio

    mode = str(gaze_mode or "auto")
    if mode == "camera":
        gkey = "gaze_viewer"
    elif mode == "away":
        gkey = "gaze_away"
    elif skey == "subject_front":
        gkey = "gaze_viewer"
    elif behind_ratio > 0.45 and mag <= 0.72:
        gkey = "gaze_over_shoulder"
    else:
        gkey = "gaze_away"
    return skey, gkey, sratio


# ══════════════════════ 主入口 ══════════════════════
def compute_camera(pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0, **opts):
    """把 3D 机位换算成 SDXL 正向 / 负向提示词。

    opts: preset, strength, weight_mode, azimuth_mode, elevation_mode,
          neg_strength, extra_positive, extra_negative, cfg,
          focal, pan_x, pan_y, subject_yaw, gaze_mode
    """
    # ── 0. 预设覆盖：preset 非"自定义"时用预设的四轴 + 附加参数 ──
    preset_name = str(opts.get("preset") or "自定义")
    if preset_name and preset_name != "自定义":
        try:
            from presets import preset_params, PRESETS
            pp = preset_params(preset_name) if preset_name in PRESETS else None
        except Exception:
            pp = None
        if pp:
            pos_x = pp.get("pos_x", pos_x)
            pos_y = pp.get("pos_y", pos_y)
            pos_z = pp.get("pos_z", pos_z)
            roll = pp.get("roll", roll)
            for k in ("focal", "pan_x", "pan_y", "subject_yaw"):
                if k in pp:
                    opts = dict(opts)
                    opts[k] = pp[k]
            if pp.get("gaze_mode"):
                opts = dict(opts)
                opts["gaze_mode"] = pp["gaze_mode"]

    pz = _clamp(pos_z, -1, 1)
    py = _clamp(pos_y, -1, 1)
    px = _clamp(pos_x, -1, 1)
    roll = _clamp(roll, -1, 1)
    focal = _clamp(opts.get("focal", 50.0), 4.0, 1200.0)
    pan_x = _clamp(opts.get("pan_x", 0.0), -1, 1)
    pan_y = _clamp(opts.get("pan_y", 0.0), -1, 1)
    subject_yaw = _clamp(opts.get("subject_yaw", 0.0), -1, 1)

    tags = _load_tags(opts.get("cfg"))
    weights = _weights(tags)
    neg_map = tags.get("neg") if isinstance(tags.get("neg"), dict) else {}

    strength = float(opts.get("strength", 1.0) if opts.get("strength") is not None else 1.0)
    curve = str(opts.get("weight_mode") or "tanh")
    az_mode = str(opts.get("azimuth_mode") or "8bin")
    el_mode = str(opts.get("elevation_mode") or "3bin")
    neg_strength = float(opts.get("neg_strength", 1.0) if opts.get("neg_strength") is not None else 0.0)
    gaze_mode = str(opts.get("gaze_mode") or "auto")

    pos_tokens = []
    neg_tokens = []
    parts = []
    bins = []

    def push(kind, key, ratio, raw, weight, note="", phrase_override=None):
        phrase = phrase_override or tags.get(key)
        if not phrase:
            return
        tokens = _emit(phrase, weight)
        if not tokens:
            return
        pos_tokens.extend(tokens)
        parts.append({
            "kind": kind, "key": key, "ratio": round(float(ratio), 4),
            "raw": round(float(raw), 4), "weight": round(float(weight), 3),
            "phrase": phrase, "tokens": tokens, "note": note,
        })
        bins.append([key, round(float(ratio), 4), kind])

    # ── 1. 方位（含极点衰减） ──
    gate = az_gate(py) * pole_gate(py)
    az_cfg = weights["azimuth"]
    az_suppressed = False
    if az_cfg.get("enabled", True) and gate >= AZ_SUPPRESS:
        for key, ratio in azimuth_bins(px, az_mode, az_cfg.get("deadzone_ratio", 0.06)):
            raw = ratio * float(az_cfg.get("weight", 10.0)) * gate + float(az_cfg.get("extra", 0.0))
            if raw < 0.05:
                continue
            push("az", key, ratio, raw, sdxl_weight(raw, strength, curve))
    else:
        az_suppressed = az_cfg.get("enabled", True) is False or gate < AZ_SUPPRESS

    # ── 2. 高度 ──
    ekey = elevation_key(py, el_mode)
    # 极点锁存：3bin 模式在 |py| 接近端点时只给 high/low，出图是"稍高的平视"而不是真的顶视。
    # 到 POLE_LATCH 就把档位升级成 bird/worm —— 用户把高度拖到底，就该看到正上方/正下方。
    if abs(py) > POLE_LATCH and ekey in ("high", "low"):
        ekey = "bird" if py > 0 else "worm"
    el_cfg = weights["elevation"]
    if el_cfg.get("enabled", True):
        eraw = 1.0 if ekey == "eye" else abs(py) * float(el_cfg.get("mult", 10.0))
        eraw += float(el_cfg.get("extra", 0.0))
        push("el", ekey, 1.0, eraw, sdxl_weight(eraw, strength, curve))

    # ── 3. 距离 ──
    dkey = distance_key(pz)
    d_cfg = weights["distance"]
    if d_cfg.get("enabled", True):
        draw = 1 + float(d_cfg.get("slope", 0.5)) * abs(pz) + float(d_cfg.get("extra", 0.0))
        push("di", dkey, 1.0, draw, sdxl_weight(draw, strength, curve))

    # ── 4. 焦距 / 光学 ──
    lkey = lens_key(focal)
    lens_cfg = weights["lens"]
    if lens_cfg.get("enabled", True):
        lraw = LENS_WEIGHT.get(lkey, 1.0) + float(lens_cfg.get("extra", 0.0))
        push("lens", lkey, 1.0, lraw, sdxl_weight(lraw, strength, curve))

    # ── 5. 倾斜（±45°，负值＝反向倾斜） ──
    tilt_cfg = weights["tilt"]
    roll_deg = round(roll * 45.0, 2)
    dutch = False
    if tilt_cfg.get("enabled", True) and abs(roll) > 0.25:
        dutch = True
        traw = 1 + float(tilt_cfg.get("slope", 6.0)) * abs(roll) + float(tilt_cfg.get("extra", 0.0))
        push("tg", "dutch" if roll > 0 else "dutch_neg", 1.0, traw, sdxl_weight(traw, strength, curve))

    # ── 6. 构图偏移（三分法 / 留白） ──
    f_cfg = weights["framing"]
    framing = []
    if f_cfg.get("enabled", True):
        for key, ratio in framing_keys(pan_x, pan_y):
            if key == "subject_center" and not f_cfg.get("emit_center"):
                continue  # 正中构图是默认态，不烧 token
            framing.append(key)
            if key == "subject_center":
                fraw = 1.0  # 居中构图裸写，避免无谓加权
            else:
                fraw = 1 + float(f_cfg.get("slope", 2.0)) * ratio + float(f_cfg.get("extra", 0.0))
            push("frame", key, ratio, fraw, sdxl_weight(fraw, strength, curve))

    # ── 7. 主体朝向 / 视线 ──
    s_cfg = weights["subject"]
    skey, gkey, sratio = subject_keys(subject_yaw, px, gaze_mode)
    rel = rel_yaw(subject_yaw, px)
    front_az = 0.0
    behind_az = 0.0
    for key, ratio in azimuth_bins(px):
        if key == "front":
            front_az = ratio
        elif key in ("back", "back_left", "back_right"):
            behind_az += ratio
    if s_cfg.get("enabled", True):
        # 去重两条：机位正面且主体正对 → "front-facing body" 与 front view 重复；
        # 机位在背后且主体背对 → "facing away" 与 from behind/back view 重复。
        dup_front = skey == "subject_front" and front_az > 0.5
        dup_back = skey == "subject_away" and behind_az > 0.45
        if sratio > 0 and not dup_front and not dup_back:
            sraw = 1 + float(s_cfg.get("slope", 2.0)) * (0.0 if skey == "subject_front" else sratio)
            push("subject", skey, sratio, sraw, sdxl_weight(sraw, strength, curve))
        graw = 1.30 if gkey == "gaze_over_shoulder" else 1.0
        gphrase = None
        if gkey == "gaze_viewer" and abs(py) > 0.75:
            gphrase = POLE_GAZE["bird"] if py > 0 else POLE_GAZE["worm"]
        push("gaze", gkey, 1.0, graw, sdxl_weight(graw, strength, curve), phrase_override=gphrase)

    # ── 8. 极点互斥：剔除与竖直轴冲突的方位 token ──
    blocked = POLE_BLOCK_TOKENS.get(ekey, ()) if abs(py) > 0.75 else ()
    if blocked:
        kept = []
        for part in parts:
            if part["kind"] not in ("az", "gaze", "subject"):
                kept.append(part)
                continue
            tokens = [t for t in part["tokens"] if _strip_weight(t) not in blocked]
            if tokens:
                part["tokens"] = tokens
                kept.append(part)
        parts = kept
        pos_tokens = []
        for part in parts:
            pos_tokens.extend(part["tokens"])

    # ── 9. 负向镜像 ──
    neg_parts = []
    if neg_strength > 0:
        nw = round(_clamp(1 + 0.15 * neg_strength, W_MIN_OUT, W_MAX_OUT), 2)
        seen = set()
        for part in parts:
            phrase = neg_map.get(part["key"])
            if not phrase:
                continue
            for token in _split_tags(phrase):
                if token in seen:
                    continue
                seen.add(token)
                neg_tokens.append("(%s:%.2f)" % (token, nw))
                neg_parts.append({"key": part["key"], "token": token, "weight": nw})

    master = float(weights.get("extra_master", 1.0) or 1.0)
    extra_pos = str(opts.get("extra_positive") or "").strip()
    extra_neg = str(opts.get("extra_negative") or "").strip()
    if extra_pos and master != 1.0:
        extra_pos = ", ".join(_emit(extra_pos, round(_clamp(master, W_MIN_OUT, W_MAX_OUT), 2)))

    positive = ", ".join([t for t in pos_tokens if t] + ([extra_pos] if extra_pos else []))
    negative = ", ".join(neg_tokens + ([extra_neg] if extra_neg else []))

    fan_ratio = 0.0
    for key, ratio in azimuth_bins(px):
        if key in ("back", "back_left", "back_right"):
            fan_ratio += ratio

    camera_info = {
        "azimuth_deg": round(px * 180.0, 2),
        "elev_deg": round(py * 90.0, 2),
        "fov_deg": focal_to_fov(focal),
        "focal": round(focal, 2),
        "lens": lkey,
        "bin": (bins[0][0] if bins else "-"),
        "bins": [[b[0], b[1], b[2]] for b in bins],  # [[key, ratio, kind]]
        "elevation": ekey,
        "distance": dkey,
        "dutch": bool(dutch),
        "roll_deg": roll_deg,
        "az_gate": round(gate, 4),
        "pole_gate": round(pole_gate(py), 4),
        "azimuth_suppressed": bool(az_suppressed),
        "behind_ratio": round(fan_ratio, 3),
        "framing": framing,
        "subject": skey,
        "gaze": gkey,
        "subject_yaw": round(subject_yaw, 3),
        "rel_yaw": round(rel, 3),
        "pan": [round(pan_x, 3), round(pan_y, 3)],
        "tokens": token_budget(positive, negative),
        "preset": str(opts.get("preset") or "自定义"),
        "strength": round(strength, 3),
        "weight_mode": curve,
        "azimuth_mode": az_mode,
        "elevation_mode": el_mode,
        "neg_strength": round(neg_strength, 3),
        "parts": parts,
        "negative_parts": neg_parts,
    }
    return {
        "positive": positive,
        "negative": negative,
        "camera_info": camera_info,
        "parts": parts,
        "negative_parts": neg_parts,
    }


def _strip_weight(token):
    """把 "(front view:1.20)" 还原成 "front view"。"""
    text = str(token or "").strip()
    if text.startswith("(") and text.endswith(")") and ":" in text:
        text = text[1:-1]
        head, _, tail = text.rpartition(":")
        try:
            float(tail)
            return head.strip()
        except ValueError:
            return text
    return text

# ══════════════════════ CLIP token 预算 ══════════════════════
CLIP_TOK_LIMIT = 75     # Chunk 0 上限：75 个内容 token（77 减去两个起止符）
CLIP_TOK_WARN = 65      # 到这儿再往里塞词就会被截断，提示收手


def plain_tags(text):
    """去掉权重语法："(front view:1.40)" → "front view"（非权重的括号原样保留）。"""
    s = str(text or "")
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "(":
            j = s.find(")", i + 1)
            if j < 0:
                out.append(s[i + 1:])
                break
            inner = s[i + 1:j]
            head, sep, tail = inner.rpartition(":")
            if sep:
                try:
                    float(tail)
                    inner = head
                except (TypeError, ValueError):
                    pass
            out.append(inner)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def clip_tokens(text):
    """CLIP BPE 近似计数：按逗号切段，按空白切词，每词 ceil(len*2/9)（≈4.5 字符/token）。

    不加载真分词器（前端 / 节点预览要零依赖），误差约 ±10%，够用来判断超没超 75。
    """
    if not text:
        return 0
    total = 0
    for seg in plain_tags(text).split(","):
        for word in seg.split():
            w = word.strip("()[]{}:;.!?")
            if not w:
                continue
            total += max(1, -(-len(w) * 2 // 9))
    return total


def token_budget(positive, negative=""):
    """相机段 token 预算：{positive, negative, total, limit, warn, level}。

    level: "ok" | "warn"（>65 标黄）| "danger"（>75 标红，Chunk 0 会截断）。
    """
    p = clip_tokens(positive)
    n = clip_tokens(negative)
    total = p + n
    if total > CLIP_TOK_LIMIT:
        level = "danger"
    elif total > CLIP_TOK_WARN:
        level = "warn"
    else:
        level = "ok"
    return {"positive": p, "negative": n, "total": total,
            "limit": CLIP_TOK_LIMIT, "warn": CLIP_TOK_WARN, "level": level}
