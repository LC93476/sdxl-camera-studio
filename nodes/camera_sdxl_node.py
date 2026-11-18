# -*- coding: utf-8 -*-
"""CameraAngleSDXL —— ComfyUI 节点壳。

只做参数转换，全部算法在 core/camera_core.py（零 ComfyUI 依赖，可单测）。
预设不是"提示词宏"，而是把一组机位参数写回滑块（preset != 自定义 时生效）。
"""

import json
import os
import sys

# 兼容两种加载路径：作为包（相对导入）或 core 目录直接进 sys.path
try:
    from ..core.camera_core import compute_camera
    from ..core.presets import PRESET_NAMES, preset_params
except ImportError:  # pragma: no cover - ComfyUI 外直接跑时走这里
    _core = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "core")
    if _core not in sys.path:
        sys.path.append(_core)
    from camera_core import compute_camera
    from presets import PRESET_NAMES, preset_params

GAZE_MODES = ["auto", "camera", "away"]


class CameraAngleSDXL:
    """SDXL 相机控制：3D 参数 → 正/负向提示词 + 相机信息。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 顺序即 UI 顺序（SPEC 2.6）；新参数一律追加在 roll 之后
                "pos_x": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "方位角：0 正面，0.5 正右，±1 背面"}),
                "pos_y": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "高度：>0 俯视，<0 仰视；±0.75 以外为极点"}),
                "pos_z": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "距离：>0 更近，<0 更远"}),
                "roll": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.1,
                                   "display": "number", "tooltip": "倾斜：±1 = ±45°，|roll|>0.25 出荷兰角（负值反向）"}),
                "focal": ("FLOAT", {"default": 50.0, "min": 8.0, "max": 400.0, "step": 1.0,
                                    "display": "number", "tooltip": "等效焦距 mm：14 超广角 / 24 广角 / 35 人文 / 50 标准 / 85 人像 / 135 长焦"}),
                "pan_x": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "构图横移：>0 相机右移（主体偏左三分线）"}),
                "pan_y": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "构图纵移：>0 主体压低（上方留白）"}),
                "subject_yaw": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                          "display": "number", "tooltip": "主体朝向：0 面向镜头，±1 完全背对（与机位解耦）"}),
                "gaze_mode": (GAZE_MODES, {"default": "auto",
                                           "tooltip": "视线：auto 按朝向推导；camera 始终看镜头；away 始终看别处"}),
                "preset": (PRESET_NAMES, {"default": "自定义",
                                          "tooltip": "选预设时覆盖机位滑块（滑块值忽略）"}),
                "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                       "display": "number", "tooltip": "权重强度"}),
                "weight_mode": (["tanh", "linear", "raw"], {"default": "tanh"}),
                "azimuth_mode": (["8bin", "4bin"], {"default": "8bin"}),
                "elevation_mode": (["3bin", "5bin"], {"default": "3bin"}),
                "neg_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                           "display": "number", "tooltip": "负向强度，0 = 关闭负向"}),
                "extra_positive": ("STRING", {"default": "", "multiline": True,
                                              "tooltip": "附加正向提示词，原样追加"}),
                "extra_negative": ("STRING", {"default": "", "multiline": True,
                                              "tooltip": "附加负向提示词，原样追加"}),
                "_cfg_json": ("STRING", {"default": "{}", "multiline": False,
                                         "tooltip": '词表覆盖 JSON，如 {"tags": {"front": "frontal view"}}'}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("positive", "negative", "camera_info")
    FUNCTION = "execute"
    CATEGORY = "Anima/相机"

    def execute(self, pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0,
                focal=50.0, pan_x=0.0, pan_y=0.0, subject_yaw=0.0, gaze_mode="auto",
                preset="自定义", strength=1.0, weight_mode="tanh",
                azimuth_mode="8bin", elevation_mode="3bin", neg_strength=1.0,
                extra_positive="", extra_negative="", _cfg_json="{}"):
        cfg = {}
        try:
            parsed = json.loads(_cfg_json) if _cfg_json and _cfg_json.strip() else {}
            if isinstance(parsed, dict):
                cfg = parsed
        except Exception:
            cfg = {}

        # 预设：把机位参数写回（SPEC 3）；模式类覆盖只动 elevation_mode / strength
        if preset and preset != "自定义":
            over = preset_params(preset)
            pos_x = over.get("pos_x", pos_x)
            pos_y = over.get("pos_y", pos_y)
            pos_z = over.get("pos_z", pos_z)
            roll = over.get("roll", roll)
            focal = over.get("focal", focal)
            pan_x = over.get("pan_x", pan_x)
            pan_y = over.get("pan_y", pan_y)
            subject_yaw = over.get("subject_yaw", subject_yaw)
            gaze_mode = over.get("gaze_mode", gaze_mode)
            elevation_mode = over.get("elevation_mode", elevation_mode)
            strength = over.get("strength", strength)

        out = compute_camera(
            pos_x, pos_y, pos_z, roll,
            focal=focal, pan_x=pan_x, pan_y=pan_y,
            subject_yaw=subject_yaw, gaze_mode=gaze_mode,
            preset=preset, strength=strength, weight_mode=weight_mode,
            azimuth_mode=azimuth_mode, elevation_mode=elevation_mode,
            neg_strength=neg_strength, extra_positive=extra_positive,
            extra_negative=extra_negative, cfg=cfg,
        )
        info = json.dumps(out["camera_info"], ensure_ascii=False)
        return (out["positive"], out["negative"], info)


NODE_CLASS_MAPPINGS = {"CameraAngleSDXL": CameraAngleSDXL}
NODE_DISPLAY_NAME_MAPPINGS = {"CameraAngleSDXL": "SDXL 相机控制"}

__all__ = ["CameraAngleSDXL", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
