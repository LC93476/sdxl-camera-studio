# -*- coding: utf-8 -*-
"""CameraAngleSDXL —— ComfyUI 节点壳。

只做参数转换，全部算法在 core/camera_core.py（零 ComfyUI 依赖，可单测）。
"""

import json
import os
import sys

# 兼容两种加载路径：作为包（相对导入）或 core 目录直接进 sys.path
try:
    from ..core.camera_core import compute_camera
except ImportError:  # pragma: no cover - ComfyUI 外直接跑时走这里
    _core = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "core")
    if _core not in sys.path:
        sys.path.append(_core)
    from camera_core import compute_camera

try:
    from ..core.presets import PRESET_NAMES
except ImportError:  # pragma: no cover
    from presets import PRESET_NAMES


class CameraAngleSDXL:
    """SDXL 相机控制：3D 参数 → 正/负向提示词 + 相机信息。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 顺序即 UI 顺序，勿调换（SPEC 2.6）
                "pos_x": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "方位角：0 正面，0.5 正右，±1 背面"}),
                "pos_y": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.01,
                                    "display": "number", "tooltip": "高度：>0 俯视，<0 仰视"}),
                "pos_z": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.05,
                                    "display": "number", "tooltip": "距离：>0 更近，<0 更远"}),
                "roll": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.10,
                                   "display": "number", "tooltip": "倾斜：>0 出荷兰角"}),
                "preset": (PRESET_NAMES, {"default": "自定义",
                                          "tooltip": "选预设时覆盖 4 个滑块（滑块值忽略）"}),
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
                                         "tooltip": '词表覆盖 JSON，如 {"front": "frontal view"}'}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("positive", "negative", "camera_info")
    FUNCTION = "execute"
    CATEGORY = "Anima/相机"

    def execute(self, pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0,
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

        out = compute_camera(
            pos_x, pos_y, pos_z, roll,
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
