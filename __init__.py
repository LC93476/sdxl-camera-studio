# -*- coding: utf-8 -*-
"""SDXL 相机控制 —— 把 Anima 的相机角度原理移植到 SDXL。

纯提示词权重生成器：3D 参数 → 正向 + 负向 + camera_info，不依赖 LoRA/ControlNet。
"""

from .nodes.camera_sdxl_node import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# 标准前端扩展目录：ComfyUI 会把 ./js 挂到 /extensions/sdxl_camera_angle/ 下
WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
