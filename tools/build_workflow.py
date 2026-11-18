# -*- coding: utf-8 -*-
"""生成可在 ComfyUI 前端打开的工作流（litegraph 格式），用于手动调摄像机出图。

用法: python tools/build_workflow.py [输出路径]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))
from presets import PRESET_NAMES  # noqa: E402

BASE_POS = ("masterpiece, best quality, very aesthetic, absurdres, 1girl, solo, standing, "
            "arms behind back, long black hair, purple eyes, soft lighting, simple background")
BASE_NEG = ("lowres, jpeg artifacts, worst quality, watermark, low quality, bad anatomy, "
            "bad proportions, extra digits, fewer digits, missing fingers, extra limbs, blurry, out of frame")

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(ROOT), "SDXL.CameraAngle.json")

nodes = []
links = []
_lid = [0]


def link(src, sslot, dst, dslot, typ):
    _lid[0] += 1
    links.append([_lid[0], src, sslot, dst, dslot, typ])
    return _lid[0]


def node(nid, typ, pos, size, inputs, outputs, widgets, title=None):
    n = {"id": nid, "type": typ, "pos": pos, "size": size, "flags": {}, "order": len(nodes),
         "mode": 0, "inputs": inputs, "outputs": outputs,
         "properties": {"Node name for S&R": typ}, "widgets_values": widgets}
    if title:
        n["title"] = title
    nodes.append(n)
    return n


def out(name, typ, slot):
    return {"name": name, "type": typ, "links": [], "slot_index": slot}


# ---- 8 检查点 ----
n8 = node(8, "CheckpointLoaderSimple", [40, 80], [400, 100], [], [out("MODEL", "MODEL", 0), out("CLIP", "CLIP", 1), out("VAE", "VAE", 2)],
          ["WAI-Nsfw-Illustrious-17.safetensors"], "模型 WAI-Illustrious v17")
# ---- 10 LoRA ----
n10 = node(10, "LoraLoader", [40, 230], [400, 130],
           [{"name": "model", "type": "MODEL", "link": None}, {"name": "clip", "type": "CLIP", "link": None}],
           [out("MODEL", "MODEL", 0), out("CLIP", "CLIP", 1)],
           ["feiyue_xianxinglu_v2.safetensors", 0.8, 0.8], "飞跃 LoRA")
# ---- 9001 相机节点 ----
n9001 = node(9001, "CameraAngleSDXL", [480, 80], [460, 760], [],
             [out("positive", "STRING", 0), out("negative", "STRING", 1), out("camera_info", "STRING", 2)],
             [0.0, 0.0, 0.0, 0.0,        # pos_x / pos_y / pos_z / roll
              50.0, 0.0, 0.0, 0.0, "auto",  # focal / pan_x / pan_y / subject_yaw / gaze_mode
              "自定义", 1.0, "tanh", "8bin", "3bin", 1.0,
              BASE_POS, BASE_NEG, "{}",
              "", ""],  # subject_prompt / scene_prompt（可选拼接端口）
             "SDXL 相机控制（拖 3D 手柄或调参数）")
# ---- 11/12 文本编码 ----
n11 = node(11, "CLIPTextEncode", [980, 200], [420, 180],
           [{"name": "clip", "type": "CLIP", "link": None},
            {"name": "text", "type": "STRING", "link": None, "widget": {"name": "text"}}],
           [out("CONDITIONING", "CONDITIONING", 0)], [""], "正向提示词（由相机节点驱动）")
n12 = node(12, "CLIPTextEncode", [980, 420], [420, 180],
           [{"name": "clip", "type": "CLIP", "link": None},
            {"name": "text", "type": "STRING", "link": None, "widget": {"name": "text"}}],
           [out("CONDITIONING", "CONDITIONING", 0)], [""], "负向提示词（由相机节点驱动）")
# ---- 13 latent ----
n13 = node(13, "EmptyLatentImage", [40, 420], [400, 110], [],
           [out("LATENT", "LATENT", 0)], [832, 1216, 1], "出图尺寸")
# ---- 14 KSampler ----
n14 = node(14, "KSampler", [980, 660], [340, 262],
           [{"name": "model", "type": "MODEL", "link": None},
            {"name": "positive", "type": "CONDITIONING", "link": None},
            {"name": "negative", "type": "CONDITIONING", "link": None},
            {"name": "latent_image", "type": "LATENT", "link": None}],
           [out("LATENT", "LATENT", 0)],
           [20261117, "randomize", 26, 5.0, "euler_ancestral", "karras", 1.0], "采样器")
# ---- 15 VAE 解码 ----
n15 = node(15, "VAEDecode", [1360, 660], [210, 46],
           [{"name": "samples", "type": "LATENT", "link": None}, {"name": "vae", "type": "VAE", "link": None}],
           [out("IMAGE", "IMAGE", 0)], [])
# ---- 110 保存 ----
n110 = node(110, "SaveImage", [1620, 660], [400, 420],
            [{"name": "images", "type": "IMAGE", "link": None}], [], ["camera/angle"], "保存")

# ---- 连线 ----
l8m = link(8, 0, 10, 0, "MODEL")
l8c = link(8, 1, 10, 1, "CLIP")
l10m = link(10, 0, 14, 0, "MODEL")
l10c11 = link(10, 1, 11, 0, "CLIP")
l10c12 = link(10, 1, 12, 0, "CLIP")
lpos = link(9001, 0, 11, 1, "STRING")
lneg = link(9001, 1, 12, 1, "STRING")
l11 = link(11, 0, 14, 1, "CONDITIONING")
l12 = link(12, 0, 14, 2, "CONDITIONING")
l13 = link(13, 0, 14, 3, "LATENT")
l14 = link(14, 0, 15, 0, "LATENT")
l8v = link(8, 2, 15, 1, "VAE")
l15 = link(15, 0, 110, 0, "IMAGE")

n8["outputs"][0]["links"] = [l8m]
n8["outputs"][1]["links"] = [l8c]
n8["outputs"][2]["links"] = [l8v]
n10["inputs"][0]["link"] = l8m
n10["inputs"][1]["link"] = l8c
n10["outputs"][0]["links"] = [l10m]
n10["outputs"][1]["links"] = [l10c11, l10c12]
n9001["outputs"][0]["links"] = [lpos]
n9001["outputs"][1]["links"] = [lneg]
n11["inputs"][0]["link"] = l10c11
n11["inputs"][1]["link"] = lpos
n11["outputs"][0]["links"] = [l11]
n12["inputs"][0]["link"] = l10c12
n12["inputs"][1]["link"] = lneg
n12["outputs"][0]["links"] = [l12]
n13["outputs"][0]["links"] = [l13]
n14["inputs"][0]["link"] = l10m
n14["inputs"][1]["link"] = l11
n14["inputs"][2]["link"] = l12
n14["inputs"][3]["link"] = l13
n14["outputs"][0]["links"] = [l14]
n15["inputs"][0]["link"] = l14
n15["inputs"][1]["link"] = l8v
n15["outputs"][0]["links"] = [l15]
n110["inputs"][0]["link"] = l15

wf = {
    "last_node_id": 9001,
    "last_link_id": _lid[0],
    "nodes": nodes,
    "links": links,
    "groups": [{"title": "相机控制 → 提示词", "bounding": [420, 20, 1400, 900], "color": "#3f789e",
                "font_size": 24, "flags": {}}],
    "config": {},
    "extra": {"ds": {"scale": 0.72, "offset": [20, 40]},
              "note": "把 CameraAngleSDXL 的 positive/negative 接到两个 CLIPTextEncode 的 text；拖动 3D 手柄或改参数即可换机位。"},
    "version": 0.4,
}

os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(wf, fh, ensure_ascii=False, indent=1)
print("写入", OUT, len(nodes), "节点", len(links), "连线")
print("预设:", ", ".join(PRESET_NAMES))
