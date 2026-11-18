# -*- coding: utf-8 -*-
"""预设机位与多机位序列（SPEC 3）。

PRESETS        —— 名称 → (pos_x, pos_y, pos_z, roll)
PRESET_EXTRAS  —— 名称 → 额外参数覆盖（focal / pan_x / pan_y / subject_yaw / gaze_mode ...）
PRESET_MODES   —— 名称 → 模式覆盖（elevation_mode / strength ...），节点与 Studio 都读它
PRESET_NAMES   —— ['自定义', ...]，给下拉框用（顺序即 UI 顺序）
SEQUENCES      —— 名称 → {"desc": str, "shots": [{"label": str, "params": {...}}]}
"""

PRESETS = {
    "正面平视中景": (0.0, 0.0, 0.0, 0.0),
    "正面仰视全身": (0.0, -0.55, -0.5, 0.0),
    "正面俯视中景": (0.0, 0.55, 0.0, 0.0),
    "右侧90度中景": (0.5, 0.0, 0.0, 0.0),
    "背面全身": (1.0, 0.0, -0.5, 0.0),
    "左前方45度近景": (-0.25, 0.0, 0.35, 0.0),
    "右后方45度中景": (0.75, 0.0, 0.0, 0.0),
    "虫瞰极限仰视": (0.0, -1.0, -0.85, 0.0),
    "鸟瞰极限俯视": (0.0, 1.0, -0.85, 0.0),
    "荷兰角倾斜中景": (0.0, 0.0, 0.0, 0.45),
    "特写面部": (0.0, 0.0, 0.85, 0.0),
    # —— 新增：光学 / 构图 / 主体朝向 ——
    "长焦人像85mm": (0.0, 0.0, 0.1, 0.0),
    "广角环境24mm": (0.0, 0.0, -0.5, 0.0),
    "三分法偏右": (0.0, 0.0, 0.0, 0.0),
    "回眸背面机位": (1.0, 0.0, 0.1, 0.0),
    "荷兰角负斜": (0.0, 0.0, 0.0, -0.45),
}

PRESET_EXTRAS = {
    "长焦人像85mm": {"focal": 85.0, "pan_x": 0.0, "pan_y": 0.0},
    "广角环境24mm": {"focal": 24.0},
    "三分法偏右": {"focal": 35.0, "pan_x": -0.35},
    "回眸背面机位": {"focal": 85.0, "subject_yaw": -0.45, "gaze_mode": "camera"},
    "荷兰角负斜": {"focal": 50.0},
}

PRESET_MODES = {
    "虫瞰极限仰视": {"elevation_mode": "5bin", "strength": 0.7},
    "鸟瞰极限俯视": {"elevation_mode": "5bin", "strength": 0.7},
}

PRESET_NAMES = ["自定义"] + list(PRESETS.keys())

# 多机位序列：Studio 的「序列」面板一键提交（同一 seed 保证同一角色）
SEQUENCES = {
    "三视图（正/45°/侧/背）": {
        "desc": "角色设定图四机位，同 seed 提交",
        "shots": [
            {"label": "正面", "params": {"pos_x": 0.0, "pos_y": 0.0, "pos_z": -0.5, "roll": 0.0, "focal": 85.0}},
            {"label": "右前45°", "params": {"pos_x": 0.25, "pos_y": 0.0, "pos_z": -0.5, "roll": 0.0, "focal": 85.0}},
            {"label": "右侧90°", "params": {"pos_x": 0.5, "pos_y": 0.0, "pos_z": -0.5, "roll": 0.0, "focal": 85.0}},
            {"label": "背面", "params": {"pos_x": 1.0, "pos_y": 0.0, "pos_z": -0.5, "roll": 0.0, "focal": 85.0}},
        ],
    },
    "环绕 8 机位": {
        "desc": "方位角每 45° 一档，其余参数不变",
        "shots": [
            {"label": "0°", "params": {"pos_x": 0.0, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "45°", "params": {"pos_x": 0.25, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "90°", "params": {"pos_x": 0.5, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "135°", "params": {"pos_x": 0.75, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "180°", "params": {"pos_x": 1.0, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "225°", "params": {"pos_x": -0.75, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "270°", "params": {"pos_x": -0.5, "pos_y": 0.0, "pos_z": 0.0}},
            {"label": "315°", "params": {"pos_x": -0.25, "pos_y": 0.0, "pos_z": 0.0}},
        ],
    },
    "推拉（特写→远景）": {
        "desc": "同一方位上的距离序列",
        "shots": [
            {"label": "特写", "params": {"pos_z": 0.85}},
            {"label": "近景", "params": {"pos_z": 0.45}},
            {"label": "中景", "params": {"pos_z": 0.1}},
            {"label": "牛仔", "params": {"pos_z": -0.17}},
            {"label": "全身", "params": {"pos_z": -0.52}},
            {"label": "远景", "params": {"pos_z": -0.85}},
        ],
    },
    "俯仰（虫瞰→鸟瞰）": {
        "desc": "俯仰角五档，启用 5bin",
        "shots": [
            {"label": "虫瞰", "params": {"pos_y": -1.0, "elevation_mode": "5bin"}},
            {"label": "仰视", "params": {"pos_y": -0.55, "elevation_mode": "5bin"}},
            {"label": "平视", "params": {"pos_y": 0.0, "elevation_mode": "5bin"}},
            {"label": "俯视", "params": {"pos_y": 0.55, "elevation_mode": "5bin"}},
            {"label": "鸟瞰", "params": {"pos_y": 1.0, "elevation_mode": "5bin"}},
        ],
    },
}


def get_preset(name):
    """返回 (pose, extras, modes)；未知名称回落到自定义（全 0）。"""
    if name in PRESETS:
        return PRESETS[name], dict(PRESET_EXTRAS.get(name) or {}), dict(PRESET_MODES.get(name) or {})
    return (0.0, 0.0, 0.0, 0.0), {}, {}


def preset_params(name):
    """预设 → 完整参数覆盖 dict（Studio / 节点共用）。"""
    pose, extras, modes = get_preset(name)
    out = {"pos_x": pose[0], "pos_y": pose[1], "pos_z": pose[2], "roll": pose[3]}
    out.update(extras)
    out.update(modes)
    return out


def sequence_names():
    return list(SEQUENCES.keys())


def get_sequence(name):
    seq = SEQUENCES.get(name)
    if not isinstance(seq, dict):
        return None
    shots = []
    for shot in seq.get("shots") or []:
        shots.append({"label": str(shot.get("label") or ""), "params": dict(shot.get("params") or {})})
    return {"desc": str(seq.get("desc") or ""), "shots": shots}
