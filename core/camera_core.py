# -*- coding: utf-8 -*-
"""SDXL 相机控制核心算法（纯 Python，零 ComfyUI 依赖，可直接单测）。

设计契约见 SPEC.md：原 Anima 节点是「提示词权重生成器」，这里把它移植到 SDXL：
raw 权重 [0.1,10] 经 tanh 压缩到 SDXL 友好区间，方位改 8 方向双档插值，
并补上负向镜像压制。
"""

import copy
import json
import math

# ── 方位 8 档，索引 j 对应中心角 theta_j = -pi + j*pi/4（逆时针） ──
AZ_BINS = ["back", "back_left", "left", "front_left",
           "front", "front_right", "right", "back_right"]
EL_KEYS = ["bird", "worm", "high", "eye", "low"]
DIST_KEYS = ["ecu", "cu", "medium", "cowboy", "full", "wide"]

# ── 默认词表（SDXL 摄影术语；键名同 SPEC 2.5，外部 cfg 可覆盖任一项） ──
SDXL_TAGS = {
    # 方位
    "front": "front view, straight-on shot, looking at viewer",
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
    "bird": "from above, high angle, looking down at viewer, top-down view",
    "worm": "from below, low angle, looking up at viewer",
    # 距离
    "ecu": "extreme close-up, macro shot",
    "cu": "close-up, portrait shot",
    "medium": "medium shot, waist up",
    "cowboy": "cowboy shot, mid-thigh up",
    "full": "full body shot",
    "wide": "wide shot, long shot, full figure in frame",
    # 倾斜
    "dutch": "dutch angle, canted framing, tilted horizon",
    # ── 负向镜像（每个轴取相反方向） ──
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
        "bird": "worm's-eye view, extreme low angle",
        "eye": "high angle, low angle",
        "worm": "bird's-eye view, top-down shot",
        "low": "high angle shot, from above",
        "ecu": "wide shot, full body",
        "cu": "wide shot, full body, distant",
        "medium": "close-up, extreme close-up",
        "cowboy": "close-up, extreme close-up",
        "full": "close-up, portrait shot",
        "wide": "close-up, portrait shot",
        "dutch": "horizon level, straight horizon",
    },
    # ── 权重参数（沿用原仓库 _tags_json 的可调项） ──
    "weights": {
        "azimuth": {"enabled": True, "weight": 10.0, "extra": 0.0, "deadzone_ratio": 0.06},
        "elevation": {"enabled": True, "extra": 0.0, "mult": 10.0, "deadzone": 0.06},
        "distance": {"enabled": True, "extra": 0.0, "slope": 0.5},
        "tilt": {"enabled": True, "extra": 0.0, "slope": 6.0},
        "extra_master": 1.0,
        "weight_min": 0.1,
        "weight_max": 10.0,
        "neg_weight": 1.15,
    },
}

W_MIN_OUT = 0.05   # 最终输出硬下界
W_MAX_OUT = 2.0    # 最终输出硬上界
W_NEUTRAL = 1.0
BARE_EPS = 0.005   # |w-1| < 0.005 输出裸短语
AZ_POLE = 0.9      # 方位门控极点


# ══════════════════════ 工具函数 ══════════════════════
def _clamp(v, lo, hi):
    v = float(v)
    return lo if v < lo else (hi if v > hi else v)


def _deep_merge(base, override):
    """深合并（沿用原仓库思路），返回 base。"""
    if not isinstance(override, dict):
        return base
    for k, v in override.items():
        if isinstance(base.get(k), dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def default_tags():
    """返回默认词表的独立副本。"""
    return copy.deepcopy(SDXL_TAGS)


def _load_tags(cfg=None):
    """cfg 支持扁平词表，也支持 {"tags": {...}, "weights": {...}} 包装（与前端 _cfg_json 一致）。"""
    tags = default_tags()
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg) if cfg.strip() else None
        except Exception:
            cfg = None
    if isinstance(cfg, dict):
        if isinstance(cfg.get("tags"), dict):
            _deep_merge(tags, cfg["tags"])
            rest = {k: v for k, v in cfg.items() if k != "tags"}
            _deep_merge(tags, rest)
        elif isinstance(cfg.get("vocab"), dict):
            _deep_merge(tags, cfg["vocab"])
        else:
            _deep_merge(tags, cfg)
    return tags


def _split_tags(phrase):
    """短语按逗号拆成多个 tag（沿用原仓库 _split_tags）。"""
    return [t.strip() for t in str(phrase).split(",") if t.strip()]


def sdxl_weight(raw, strength=1.0, curve="tanh"):
    """raw 权重 [0.1,10] → SDXL 可用权重 [0.05,2.0]，1.0 为中性。"""
    raw = max(1e-3, float(raw))
    strength = float(strength)
    if curve == "raw":                      # 直通，调试用
        return round(_clamp(raw, W_MIN_OUT, W_MAX_OUT), 2)
    if curve == "linear":                   # 线性截断
        return round(_clamp(1.0 + 0.06 * (raw - 1.0) * strength, W_MIN_OUT, W_MAX_OUT), 2)
    S = 0.55 * strength
    K = 3.3
    if raw >= W_NEUTRAL:
        w = 1.0 + S * (raw - 1.0) / (raw - 1.0 + K)
    else:
        w = 1.0 - S * (1.0 - raw) / ((1.0 - raw) + K)
    return round(_clamp(w, W_MIN_OUT, W_MAX_OUT), 2)


def _emit(phrase, w):
    """把短语渲染成 token 列表：中性写裸短语，否则逐 tag 加权重。"""
    w = round(_clamp(w, W_MIN_OUT, W_MAX_OUT), 2)
    tags = _split_tags(phrase)
    if not tags:
        return []
    if abs(w - W_NEUTRAL) < BARE_EPS:
        return list(tags)
    return ["(%s:%.2f)" % (t, w) for t in tags]


# ══════════════════════ 分档 ══════════════════════
def azimuth_bins(pos_x, mode="8bin", deadzone_ratio=0.06):
    """方位分档 → [(key, ratio)]，ratio ∈ (0,1]，合计 1。"""
    x = _clamp(pos_x, -1.0, 1.0)
    if mode == "4bin":                      # 沿用原仓库 cos/sin 四向分解
        az = x * math.pi
        raw = {
            "front": max(0.0, math.cos(az)),
            "back": max(0.0, -math.cos(az)),
            "right": max(0.0, math.sin(az)),
            "left": max(0.0, -math.sin(az)),
        }
        s = sum(raw.values())
        if s <= 0.0:
            return []
        out = []
        for key in ("front", "back", "left", "right"):
            ratio = raw[key] / s
            if ratio >= deadzone_ratio:
                out.append((key, ratio))
        return out
    # 8 档：theta = x*pi，f = (theta+pi)/(pi/4) ∈ [0,8]，线性双档插值
    theta = x * math.pi
    f = (theta + math.pi) / (math.pi / 4.0)
    i0 = int(f)
    frac = f - i0
    ka, kb = AZ_BINS[i0 % 8], AZ_BINS[(i0 + 1) % 8]
    out = []
    for key, ratio in ((ka, 1.0 - frac), (kb, frac)):
        if ratio >= deadzone_ratio:
            out.append((key, ratio))
    return out


def elevation_key(pos_y, mode="3bin"):
    """高度分档：默认 3 档；5 档在 |pos_y|>0.75 时插入 bird/worm。"""
    y = float(pos_y)
    if mode == "5bin":
        if y > 0.75:
            return "bird"
        if y < -0.75:
            return "worm"
    if y > 0.2:
        return "high"
    if y >= -0.2:
        return "eye"
    return "low"


def distance_key(pos_z):
    """距离 6 档（阈值沿用原仓库，medium 收 0 点以匹配验收）。"""
    z = float(pos_z)
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


def az_gate(pos_y):
    """方位门控：|pos_y|<=0.9 为 1，之后线性衰减，1.0 处归零。"""
    return _clamp((1.0 - abs(float(pos_y))) / (1.0 - AZ_POLE), 0.0, 1.0)


# ══════════════════════ 主入口 ══════════════════════
def compute_camera(pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0, **opts):
    """3D 相机参数 → SDXL 正/负向提示词。

    返回 {"positive": str, "negative": str, "camera_info": dict, "parts": [[phrase, w], ...]}
    """
    preset = opts.get("preset", "自定义") or "自定义"
    strength = float(opts.get("strength", 1.0))
    weight_mode = opts.get("weight_mode", "tanh") or "tanh"
    azimuth_mode = opts.get("azimuth_mode", "8bin") or "8bin"
    elevation_mode = opts.get("elevation_mode", "3bin") or "3bin"
    neg_strength = float(opts.get("neg_strength", 1.0))
    extra_positive = str(opts.get("extra_positive", "") or "").strip()
    extra_negative = str(opts.get("extra_negative", "") or "").strip()
    tags = _load_tags(opts.get("cfg"))

    # 预设姿态覆盖滑块
    if preset != "自定义":
        try:
            from .presets import PRESETS, PRESET_MODES
        except ImportError:
            from presets import PRESETS, PRESET_MODES
        if preset in PRESETS:
            pos_x, pos_y, pos_z, roll = PRESETS[preset]
            over = PRESET_MODES.get(preset) or {}
            if "elevation_mode" in over:
                elevation_mode = over["elevation_mode"]
            if "strength" in over:
                strength = float(over["strength"])

    pos_x = _clamp(pos_x, -1.0, 1.0)
    pos_y = _clamp(pos_y, -1.0, 1.0)
    pos_z = _clamp(pos_z, -1.0, 1.0)
    roll = _clamp(roll, 0.0, 1.0)
    strength = _clamp(strength, 0.0, 2.0)
    neg_strength = _clamp(neg_strength, 0.0, 2.0)

    W = tags.get("weights", {})
    em = float(W.get("extra_master", 1.0))
    wcap = float(W.get("weight_max", 10.0))
    az_cfg = W.get("azimuth", {})
    el_cfg = W.get("elevation", {})
    di_cfg = W.get("distance", {})
    ti_cfg = W.get("tilt", {})

    parts = []      # [[phrase, w], ...] 正向
    negs = []       # [[phrase, w], ...] 负向
    seen_neg = set()
    info_bins = []
    gate = az_gate(pos_y)

    # ---------- 方位 ----------
    e0 = sdxl_weight
    if az_cfg.get("enabled", True) and gate > 0.0:
        az_budget = (float(az_cfg.get("weight", 10.0))
                     + float(az_cfg.get("extra", 0.0)) * em) * gate
        for key, ratio in azimuth_bins(pos_x, azimuth_mode,
                                       float(az_cfg.get("deadzone_ratio", 0.06))):
            raw = ratio * az_budget
            raw = min(wcap + max(0.0, float(az_cfg.get("extra", 0.0)) * em), raw)
            w = e0(raw, strength, weight_mode)
            parts.append([tags.get(key, ""), w])
            info_bins.append([key, round(ratio, 4)])
            neg_phrase = tags.get("neg", {}).get(key, "")
            if neg_strength > 0.0 and neg_phrase and neg_phrase not in seen_neg:
                seen_neg.add(neg_phrase)
                negs.append([neg_phrase, w])

    # ---------- 高度 ----------
    el_key = elevation_key(pos_y, elevation_mode)
    if el_cfg.get("enabled", True):
        el_extra = float(el_cfg.get("extra", 0.0)) * em
        if el_key == "eye":
            raw = 1.0 + el_extra                     # 平视固定中性，不给底噪
        else:
            raw = abs(pos_y) * float(el_cfg.get("mult", 10.0)) + el_extra
        if raw >= float(el_cfg.get("deadzone", 0.06)):
            w = e0(min(wcap, raw), strength, weight_mode)
            parts.append([tags.get(el_key, ""), w])
            np_ = tags.get("neg", {}).get(el_key, "")
            if neg_strength > 0.0 and np_ and np_ not in seen_neg:
                seen_neg.add(np_)
                negs.append([np_, w])

    # ---------- 距离 ----------
    di_key = distance_key(pos_z)
    if di_cfg.get("enabled", True):
        raw = 1.0 + float(di_cfg.get("slope", 0.5)) * abs(pos_z) \
            + float(di_cfg.get("extra", 0.0)) * em
        w = e0(min(wcap, raw), strength, weight_mode)
        parts.append([tags.get(di_key, ""), w])
        np_ = tags.get("neg", {}).get(di_key, "")
        if neg_strength > 0.0 and np_ and np_ not in seen_neg:
            seen_neg.add(np_)
            negs.append([np_, w])

    # ---------- 倾斜 ----------
    dutch = roll > 0.0
    if ti_cfg.get("enabled", True) and dutch:
        raw = 1.0 + float(ti_cfg.get("slope", 6.0)) * roll \
            + float(ti_cfg.get("extra", 0.0)) * em
        w = e0(raw, strength, weight_mode)
        parts.append([tags.get("dutch", ""), w])
        np_ = tags.get("neg", {}).get("dutch", "")
        if neg_strength > 0.0 and np_ and np_ not in seen_neg:
            seen_neg.add(np_)
            negs.append([np_, w])

    # ---------- 额外提示词 ----------
    tokens = []
    for phrase, w in parts:
        tokens.extend(_emit(phrase, w))
    if extra_positive:
        tokens.append(extra_positive)

    neg_tokens = []
    neg_w = 1.0 + (float(W.get("neg_weight", 1.15)) - 1.0) * neg_strength
    if neg_strength > 0.0:
        for phrase, _src_w in negs:
            neg_tokens.extend(_emit(phrase, neg_w))
    if extra_negative:
        neg_tokens.append(extra_negative)

    bin_name = None
    if info_bins:
        bin_name = max(info_bins, key=lambda kv: kv[1])[0]

    camera_info = {
        "azimuth_deg": round(pos_x * 180.0, 2),
        "elev_deg": round(pos_y * 90.0, 2),
        "bin": bin_name,
        "bins": [[k, r] for k, r in info_bins],
        "elevation": el_key,
        "distance": di_key,
        "dutch": bool(dutch),
        "az_gate": round(gate, 4),
        "preset": preset,
        "strength": round(strength, 3),
        "weight_mode": weight_mode,
        "azimuth_mode": azimuth_mode,
        "elevation_mode": elevation_mode,
        "neg_strength": round(neg_strength, 3),
        "parts": [[p, w] for p, w in parts],
        "negative_parts": [[p, w] for p, w in negs],
    }
    return {
        "positive": ", ".join(tokens),
        "negative": ", ".join(neg_tokens),
        "camera_info": camera_info,
        "parts": [[p, w] for p, w in parts],
    }


__all__ = [
    "SDXL_TAGS", "AZ_BINS", "EL_KEYS", "DIST_KEYS",
    "sdxl_weight", "compute_camera", "default_tags",
    "azimuth_bins", "elevation_key", "distance_key", "az_gate",
]
