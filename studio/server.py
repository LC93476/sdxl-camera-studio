# -*- coding: utf-8 -*-
"""SDXL 相机工作台 · HTTP 服务（纯标准库）

启动：
    python studio/server.py --port 8770 --comfy http://127.0.0.1:8188 [--no-browser]

启动后第一行固定打印（启动器 launch.py 靠 grep 这行）：
    [studio] http://127.0.0.1:8770

路由与文件归属见 studio/CONTRACT.md。三条设计红线：
1. 只用标准库（ThreadingHTTPServer + json/urllib/threading/os/pathlib/subprocess/time/uuid）；
2. 除 /api/generate 之外，任何路由都不因为 ComfyUI 离线而 500 —— 一律降级返回；
3. 静态文件只从 studio/web 出，resolve 之后必须仍在 web 根内（防目录穿越）。
"""

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import pathlib
import random
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE_DIR = os.path.join(ROOT, "core")
TOOLS_DIR = os.path.join(ROOT, "tools")
JS_DIR = os.path.join(ROOT, "js")
STATE_DIR = os.path.join(HERE, "state")
CN_DIR = os.path.join(STATE_DIR, "controlnet")
BATCHES_PATH = os.path.join(STATE_DIR, "batches.json")
for _p in (CORE_DIR, HERE, TOOLS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import comfy_bridge  # noqa: E402
import store  # noqa: E402
from camera_core import (  # noqa: E402
    AZ_BINS, DIST_KEYS, EL_KEYS, CLIP_TOK_LIMIT, CLIP_TOK_WARN, compute_camera, default_tags,
    token_budget,
)
from presets import (  # noqa: E402
    PRESETS, PRESET_MODES, PRESET_NAMES, get_sequence, preset_params, sequence_names,
)

try:  # 真分词器 / 估算都封在 tools/token_count.py；缺文件也不能让服务起不来
    import token_count  # noqa: E402
except Exception:  # noqa: BLE001
    token_count = None

VERSION = "1.0"
MAX_BODY = 4 * 1024 * 1024
MAX_TASKS = 60
MAX_BATCH_SHOTS = 24
MAX_BATCH_RECORDS = 50
CN_MAX_BYTES = 8 * 1024 * 1024
CN_DATA_PREFIX = "data:image/png;base64,"
CN_MAGIC = b"\x89PNG\r\n\x1a\n"
BATCH_LOCK = threading.Lock()

CAM_KEYS = (
    "pos_x", "pos_y", "pos_z", "roll", "preset", "strength",
    "weight_mode", "azimuth_mode", "elevation_mode", "neg_strength",
    "extra_positive", "extra_negative",
    # v2：光学 / 构图平移 / 主体朝向
    "focal", "pan_x", "pan_y", "subject_yaw", "gaze_mode",
)

# v2 枚举（/api/meta 下发，前端下拉框用）
FOCALS = [14, 24, 35, 50, 85, 135]
GAZE_MODES = ["auto", "camera", "away"]

DEFAULT_ENUMS = {
    "weight_mode": ["tanh", "linear", "raw"],
    "azimuth_mode": ["8bin", "4bin"],
    "elevation_mode": ["3bin", "5bin"],
}

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".hdr": "image/vnd.radiance",
}

# ══════════════════════ 全局状态 ══════════════════════
BRIDGE = comfy_bridge.ComfyBridge("http://127.0.0.1:8188")
PORT = 8770
TASKS = {}
TASK_LOCK = threading.Lock()
CURRENT = {"prompt_id": None}


# ══════════════════════ 小工具 ══════════════════════
def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _one(qs, key, default=""):
    vals = qs.get(key) or []
    if not vals:
        return default
    return vals[0]


def _guess_mime(path):
    return MIME_TYPES.get(pathlib.Path(str(path)).suffix.lower(), "application/octet-stream")


def _resolve_under(root, rel):
    """把 rel 限制在 root 内；越界/非法返回 None。"""
    root_abs = os.path.normcase(str(pathlib.Path(str(root)).resolve()))
    raw = urllib.parse.unquote(str(rel or "")).replace(chr(92), "/").lstrip("/")
    if not raw:
        raw = "index.html"
    try:
        cand = pathlib.Path(str(root), raw).resolve()
    except (OSError, RuntimeError):
        return None
    cand_s = os.path.normcase(str(cand))
    if cand_s != root_abs and not cand_s.startswith(root_abs + os.sep):
        return None
    return str(cand)


def _cam_params(body, base=None):
    """把请求体和配置合并成 compute_camera 的参数。

    v2：preset 不是「自定义」且是已知预设时，用 presets.preset_params() 整份覆盖
    相机参数（含 focal / pan / subject_yaw / gaze_mode 与 elevation_mode / strength）。
    未知预设不覆盖，交给 build_preview 给 warn。
    """
    src = dict(base or {})
    if isinstance(body, dict):
        for key in CAM_KEYS:
            if body.get(key) is not None:
                src[key] = body[key]
    params = {
        "pos_x": _num(src.get("pos_x"), 0.0),
        "pos_y": _num(src.get("pos_y"), 0.0),
        "pos_z": _num(src.get("pos_z"), 0.0),
        "roll": _num(src.get("roll"), 0.0),
        "preset": str(src.get("preset") or "自定义"),
        "strength": _num(src.get("strength"), 1.0),
        "weight_mode": str(src.get("weight_mode") or "tanh"),
        "azimuth_mode": str(src.get("azimuth_mode") or "8bin"),
        "elevation_mode": str(src.get("elevation_mode") or "3bin"),
        "neg_strength": _num(src.get("neg_strength"), 1.0),
        "extra_positive": str(src.get("extra_positive") or ""),
        "extra_negative": str(src.get("extra_negative") or ""),
        "focal": _num(src.get("focal"), 50.0),
        "pan_x": _num(src.get("pan_x"), 0.0),
        "pan_y": _num(src.get("pan_y"), 0.0),
        "subject_yaw": _num(src.get("subject_yaw"), 0.0),
        "gaze_mode": str(src.get("gaze_mode") or "auto"),
    }
    preset = params["preset"]
    if preset != "自定义" and preset in PRESETS:
        over = preset_params(preset)
        for key in ("pos_x", "pos_y", "pos_z", "roll", "focal", "pan_x", "pan_y",
                    "subject_yaw", "strength", "neg_strength"):
            if key in over:
                params[key] = _num(over[key], params[key])
        for key in ("gaze_mode", "weight_mode", "azimuth_mode", "elevation_mode"):
            if key in over:
                params[key] = str(over[key])
    return params


def _append_manual(base, extra):
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return base
    if not base:
        return extra
    return base + ", " + extra


# ══════════════════ token 计数（真分词器缺失时退化为估算） ══════════════════
def _fallback_tag_tokens(tag):
    """估算单 tag：去权重文本按 3.6 字符/token 向上取整，加权 tag 额外 +3。"""
    text = str(tag or "").strip()
    if not text:
        return 0
    extra = 0
    if text.startswith("(") and text.endswith(")") and ":" in text:
        head, _sep, tail = text[1:-1].rpartition(":")
        try:
            float(tail)
        except ValueError:
            pass
        else:
            text = head.strip()
            extra = 3
    if not text:
        return extra
    return int(math.ceil(len(text) / 3.6)) + extra


def _fallback_count(text):
    total = 0
    for tag in str(text or "").replace("\n", ",").split(","):
        total += _fallback_tag_tokens(tag)
    return total


def _fallback_fragments(parts):
    out = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        for tag in str(part.get("phrase") or "").replace("\n", ",").split(","):
            if not tag.strip():
                continue
            out.append({
                "kind": str(part.get("kind") or ""),
                "key": str(part.get("key") or ""),
                "text": tag.strip(),
                "tokens": _fallback_tag_tokens(tag),
            })
    return out


def _tok_count(text):
    if token_count is not None:
        try:
            return int(token_count.count(text))
        except BaseException:  # noqa: BLE001 - 计数失败只是退化，不允许影响路由
            pass
    return _fallback_count(text)


def _tok_count_tags(tags):
    if token_count is not None:
        try:
            return int(token_count.count_tags(tags))
        except BaseException:  # noqa: BLE001
            pass
    return sum(_fallback_tag_tokens(t) for t in (tags or []))


def _tok_method():
    if token_count is not None:
        try:
            return str(token_count.method())
        except BaseException:  # noqa: BLE001
            pass
    return "estimate"


def _tok_fragments(parts):
    if token_count is not None:
        try:
            items = token_count.fragments(parts)
            if isinstance(items, list):
                return items
        except BaseException:  # noqa: BLE001
            pass
    return _fallback_fragments(parts)


def build_preview(body, base=None):
    """/api/preview 的全部逻辑；/api/generate 复用同一份结果（所见即所得）。"""
    body = body if isinstance(body, dict) else {}
    params = _cam_params(body, base)
    res = compute_camera(**params)

    positive = _append_manual(res.get("positive") or "", body.get("manual_positive") or "")
    negative = _append_manual(res.get("negative") or "", body.get("manual_negative") or "")

    parts = res.get("parts") or []
    token_parts = _tok_fragments(parts)
    tok_pos = int(_tok_count(positive))
    tok_neg = int(_tok_count(negative))
    method = _tok_method()

    warn = []
    if method != "tokenizer":
        warn.append(
            "tokens are estimated (no CLIP tokenizer): strip-weight chars / 3.6 rounded up, +3 per weighted tag"
        )
    preset = params["preset"]
    if preset != "自定义" and preset in PRESETS:
        over = preset_params(preset)
        bits = []
        for key in ("focal", "pan_x", "pan_y", "subject_yaw", "gaze_mode",
                    "azimuth_mode", "elevation_mode", "strength", "weight_mode", "neg_strength"):
            if key not in over:
                continue
            value = over[key]
            if isinstance(value, float):
                bits.append("%s=%g" % (key, value))
            else:
                bits.append("%s=%s" % (key, value))
        if bits:
            warn.append("preset [%s] overrides %s" % (preset, ", ".join(bits)))
    elif preset != "自定义":
        warn.append("unknown preset [%s], sliders used as-is" % preset)

    return {
        "positive": positive,
        "negative": negative,
        "camera_info": res.get("camera_info") or {},
        "parts": parts,
        "token_parts": token_parts,
        "tokens": {
            "positive": tok_pos,
            "negative": tok_neg,
            "total": tok_pos + tok_neg,
            "limit": CLIP_TOK_LIMIT,   # Chunk 0 上限（75）
            "warn": CLIP_TOK_WARN,     # 到这儿标黄（65）
            "level": token_budget(positive, negative)["level"],
            "method": method,
        },
        "warn": warn,
        "params": params,
    }


# ══════════════════════ 任务表 ══════════════════════
def _trim_tasks():
    """只留最近 MAX_TASKS 个（调用方自己持锁）。"""
    if len(TASKS) <= MAX_TASKS:
        return
    ordered = sorted(TASKS.values(), key=lambda t: t.get("t0") or 0.0)
    for task in ordered[:len(TASKS) - MAX_TASKS]:
        TASKS.pop(task.get("prompt_id"), None)


def _collect_images(prompt_id, entry, task):
    """把 ComfyUI 的图下载到 out/，返回契约里的 images[]。"""
    images = []
    seed = (task or {}).get("seed")
    for item in comfy_bridge.history_images(entry):
        filename = item["filename"]
        subfolder = item["subfolder"]
        image_type = item["type"]
        record = {
            "filename": filename,
            "subfolder": subfolder,
            "type": image_type,
        }
        proxy_url = "/api/view?" + urllib.parse.urlencode({
            "filename": filename,
            "subfolder": subfolder,
            "type": image_type,
        })
        try:
            blob = BRIDGE.view_bytes(filename, subfolder, image_type)
        except comfy_bridge.ComfyError as exc:
            record["url"] = proxy_url
            record["error"] = str(exc)
            images.append(record)
            continue
        dest = store.unique_output_path(filename)
        try:
            with open(dest, "wb") as fh:
                fh.write(blob)
        except OSError as exc:
            record["url"] = proxy_url
            record["error"] = str(exc)
            images.append(record)
            continue
        store.write_meta(dest, {
            "seed": seed,
            "prompt_id": prompt_id,
            "source_filename": filename,
            "steps": (task or {}).get("steps"),
            "created": round(time.time(), 3),
        })
        rel = store.out_rel(dest)
        record["local"] = rel
        record["url"] = "/api/view?local=" + urllib.parse.quote(rel)
        record["seed"] = seed
        images.append(record)
    return images


def api_progress(prompt_id):
    """state: queued | running | done | error。

    先 GET /history/{id}（拿到 outputs 即 done 并抓图），否则 GET /queue 判 queued/running。
    running 的 progress 用 已过时间 / (steps * 0.55) 粗估，上限 0.95；
    ComfyUI 离线时保留上一次状态并附 warn，不抛错。
    """
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return {
            "state": "error", "progress": 0.0, "node": "", "queue_pos": 0,
            "images": [], "error": "prompt_id 必填", "elapsed": 0.0,
        }

    with TASK_LOCK:
        task = TASKS.get(prompt_id)
        task = dict(task) if task else None

    steps = int((task or {}).get("steps") or 28)
    t0 = (task or {}).get("t0")
    elapsed = max(0.0, time.time() - t0) if t0 else 0.0

    out = {
        "state": "queued",
        "progress": 0.0,
        "node": "",
        "queue_pos": 0,
        "images": [],
        "error": None,
        "elapsed": round(elapsed, 2),
    }

    if task and task.get("images"):
        out.update(state="done", progress=1.0, node="SaveImage", images=task["images"])
        return out

    try:
        entry = BRIDGE.history(prompt_id)
    except comfy_bridge.ComfyError as exc:
        if task and task.get("last_state"):
            out.update(task["last_state"])
        out["warn"] = str(exc)
        out["elapsed"] = round(elapsed, 2)
        return out

    if entry is not None:
        status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
        if status.get("status_str") == "error":
            out.update(
                state="error",
                node="",
                error=comfy_bridge.history_error(entry) or "ComfyUI 执行失败（详见 ComfyUI 控制台）",
            )
            return out
        if comfy_bridge.history_images(entry):
            images = _collect_images(prompt_id, entry, task)
            with TASK_LOCK:
                if prompt_id in TASKS:
                    TASKS[prompt_id]["images"] = images
                    TASKS[prompt_id]["last_state"] = {"state": "done", "progress": 1.0}
            out.update(state="done", progress=1.0, node="SaveImage", images=images)
            if not images:
                out.update(state="error", error="图已生成但下载失败")
            return out
        if elapsed > 3.0:
            out.update(state="error", error="ComfyUI 记录了该任务但没有输出图像（可能被中断）")
            return out

    try:
        state, position = BRIDGE.queue_state(prompt_id)
    except comfy_bridge.ComfyError as exc:
        if task and task.get("last_state"):
            out.update(task["last_state"])
        out["warn"] = str(exc)
        return out

    if state == "running":
        estimate = max(1.0, steps * 0.55)
        out.update(
            state="running",
            node="KSampler",
            progress=round(min(0.95, elapsed / estimate), 4),
        )
    elif state == "queued":
        out.update(state="queued", queue_pos=int(position), progress=0.0)
    elif elapsed > 20.0:
        out.update(
            state="error",
            error="prompt 既不在队列也不在历史里（可能已被 interrupt 或 ComfyUI 重启）",
        )
    else:
        out.update(state="queued", queue_pos=0)

    with TASK_LOCK:
        if prompt_id in TASKS:
            TASKS[prompt_id]["last_state"] = {
                "state": out["state"], "progress": out["progress"], "node": out["node"],
            }
    return out


# ══════════════════════ API 实现 ══════════════════════
def api_health():
    try:
        reachable = bool(BRIDGE.reachable())
    except Exception:
        reachable = False
    return {
        "ok": True,
        "version": VERSION,
        "comfy_url": BRIDGE.base,
        "comfy_reachable": reachable,
        "port": PORT,
        "out_dir": str(store.OUT_DIR),
        "web_dir": str(store.WEB_DIR),
    }


def api_meta():
    vocab = {
        "positive": dict(default_tags()),
        "az_bins": list(AZ_BINS),
        "el_keys": list(EL_KEYS),
        "dist_keys": list(DIST_KEYS),
    }
    vocab["positive"].pop("neg", None)
    vocab["positive"].pop("weights", None)
    vocab["negative"] = dict(default_tags().get("neg") or {})
    return {
        "version": VERSION,
        "presets": {name: list(vals) for name, vals in PRESETS.items()},
        "preset_names": list(PRESET_NAMES),
        "preset_modes": {name: dict(vals) for name, vals in PRESET_MODES.items()},
        "ui_presets": store.load_presets(),
        "defaults": dict(store.DEFAULT_CONFIG),
        "enums": dict(DEFAULT_ENUMS),
        "vocab": vocab,
        "templates": api_templates().get("templates") or [],
        # v2：光学 / 朝向枚举、序列清单（名字 → 说明）、仓库 js/ 资产
        "focals": list(FOCALS),
        "gaze_modes": list(GAZE_MODES),
        "sequences": {
            name: str((get_sequence(name) or {}).get("desc") or "")
            for name in sequence_names()
        },
        "vendored": _js_assets(),
    }


def _js_assets():
    """仓库根 js/ 下的静态资产（前端据此判断 three.js / camera_shared.js 是否就位）。"""
    out = {}
    try:
        names = sorted(os.listdir(JS_DIR))
    except OSError:
        return out
    for name in names:
        path = os.path.join(JS_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            out[name] = os.path.getsize(path)
        except OSError:
            out[name] = 0
    return out


def api_sequences():
    """GET /api/sequences：完整序列定义（名字 → desc + shots）。"""
    seqs = {}
    for name in sequence_names():
        seq = get_sequence(name) or {}
        seqs[name] = {
            "desc": str(seq.get("desc") or ""),
            "shots": list(seq.get("shots") or []),
        }
    return {"ok": True, "names": list(seqs.keys()), "sequences": seqs}


def api_templates():
    """外部工作流模板清单（ComfyUI API 格式 JSON）。"""
    try:
        import template_bridge
        items = template_bridge.list_templates()
    except Exception as exc:
        return {"templates": [], "dirs": [], "error": str(exc)}
    return {"templates": items, "dirs": list(getattr(template_bridge, "TEMPLATE_DIRS", []))}


def api_models():
    try:
        return BRIDGE.models()
    except Exception as exc:
        return comfy_bridge.ComfyBridge.empty_models(str(exc))


def api_interrupt():
    try:
        BRIDGE.interrupt()
    except comfy_bridge.ComfyError as exc:
        return False, {"ok": False, "error": str(exc)}
    with TASK_LOCK:
        CURRENT["prompt_id"] = None
        for task in TASKS.values():
            if task.get("last_state", {}).get("state") in ("running", "queued"):
                task["last_state"] = {"state": "error", "progress": 0.0, "node": ""}
    return True, {"ok": True}


def api_open_folder():
    store.ensure_dirs()
    path = str(store.OUT_DIR)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        return False, {"ok": False, "error": str(exc), "path": path}
    return True, {"ok": True, "path": path}


def _gen_params(body, cfg):
    """采样 / 模型参数（/api/generate 与 /api/sequence 共用，避免两份真源）。"""
    body = body if isinstance(body, dict) else {}
    gen = {
        "checkpoint": str(body.get("checkpoint") or cfg.get("checkpoint") or "").strip(),
        "lora_name": str(body.get("lora_name") or body.get("lora") or cfg.get("lora_name") or "").strip(),
        "lora_strength": _num(body.get("lora_strength"), cfg.get("lora_strength") or 0.0),
        "steps": max(1, int(_num(body.get("steps"), cfg.get("steps") or 28))),
        "cfg": _num(body.get("cfg"), cfg.get("cfg") or 5.0),
        "width": max(64, int(_num(body.get("width"), cfg.get("width") or 832))),
        "height": max(64, int(_num(body.get("height"), cfg.get("height") or 1216))),
        "sampler": str(body.get("sampler") or cfg.get("sampler") or "euler_ancestral"),
        "scheduler": str(body.get("scheduler") or cfg.get("scheduler") or "karras"),
        "batch": max(1, int(_num(body.get("batch"), cfg.get("batch") or 1))),
        "save_prefix": str(body.get("save_prefix") or cfg.get("save_prefix") or "camera").strip() or "camera",
        # 外部工作流模板：非空就把相机提示词注入模板，而不是用内置最小图
        "workflow_template": str(body.get("workflow_template") or cfg.get("workflow_template") or "").strip(),
        "negative_mode": str(body.get("negative_mode") or cfg.get("negative_mode") or "append").strip() or "append",
        "template_vars": body.get("template_vars") if isinstance(body.get("template_vars"), dict) else {},
    }
    pose_id = str(body.get("pose_id") or cfg.get("pose_id") or "").strip()
    if pose_id:
        gen["template_vars"].setdefault("pose", pose_id)

    # v2.3：body.template 是 /api/templates 里的模板 id 或路径（等价于 workflow_template）
    template = str(body.get("template") or "").strip()
    if template and not gen["workflow_template"]:
        gen["workflow_template"] = template

    if gen["workflow_template"] and not gen["checkpoint"]:
        # 模板自带 checkpoint，不需要前端再选（缺了也只有内置图才必须）
        gen["checkpoint"] = ""
    return gen


def api_generate(body):
    """返回 (http_status, payload)。"""
    body = body if isinstance(body, dict) else {}
    cfg = store.load_config()
    preview = build_preview(body, cfg)
    gen = _gen_params(body, cfg)

    if not gen["checkpoint"] and not gen["workflow_template"]:
        return 400, {
            "error": "checkpoint 未选择：先 GET /api/comfy/models 取 checkpoints，再在请求体里带 checkpoint",
            "hint": '例如 {"checkpoint": "Illustrious-XL-v1.0.safetensors", "randomize": true}',
            "checkpoints": api_models().get("checkpoints") or [],
        }

    randomize = body.get("randomize")
    if randomize is None:
        randomize = cfg.get("randomize", True)
    randomize = _truthy(randomize)
    try:
        seed_in = int(float(body.get("seed", cfg.get("seed", -1))))
    except (TypeError, ValueError):
        seed_in = -1
    if randomize or seed_in < 0:
        seed = random.randrange(0, 2 ** 31)
        seed_source = "random"
    else:
        seed = seed_in & 0xFFFFFFFF
        seed_source = "fixed"
    gen["seed"] = seed

    if not BRIDGE.reachable():
        return 503, {"error": "ComfyUI 未启动", "comfy_url": BRIDGE.base}

    template_warn = []
    template_name = ""
    try:
        graph, graph_meta = _build_graph_safe(
            gen, preview["positive"], preview["negative"], template_warn)
        template_warn.extend(list(graph_meta.get("warn") or []))
        template_name = str(graph_meta.get("template") or "")
        prompt_id = BRIDGE.submit(graph)
    except comfy_bridge.ComfyError as exc:
        return 502, {"error": str(exc), "comfy_url": BRIDGE.base}

    with TASK_LOCK:
        TASKS[prompt_id] = {
            "prompt_id": prompt_id,
            "t0": time.time(),
            "steps": gen["steps"],
            "seed": seed,
            "seed_source": seed_source,
            "images": None,
            "gen": dict(gen),
            "positive": preview["positive"],
            "negative": preview["negative"],
            "last_state": {"state": "queued", "progress": 0.0, "node": ""},
        }
        CURRENT["prompt_id"] = prompt_id
        _trim_tasks()

    return 200, {
        "prompt_id": prompt_id,
        "template": template_name,
        "warn": template_warn,
        "seed_used": seed,
        "seed_source": seed_source,
        "steps": gen["steps"],
        "cfg": gen["cfg"],
        "width": gen["width"],
        "height": gen["height"],
        "sampler": gen["sampler"],
        "scheduler": gen["scheduler"],
        "checkpoint": gen["checkpoint"],
        "lora_name": gen["lora_name"],
        "batch": gen["batch"],
        "positive": preview["positive"],
        "negative": preview["negative"],
    }


def api_sequence(body):
    """POST /api/sequence：按预设序列逐张 compute + submit；离线整单 503，绝不半提交。

    先把所有机位的提示词和采样参数算完（不提交），再统一判在线、统一取 seed，
    最后逐张 submit —— 任何一张失败都带着已提交的真实结果返回，不谎报全成功。
    """
    body = body if isinstance(body, dict) else {}
    name = str(body.get("name") or "").strip()
    seq = get_sequence(name)
    if seq is None:
        return 404, {"error": "unknown sequence: %s" % (name or "-"), "names": sequence_names()}
    shots = list(seq.get("shots") or [])
    if not shots:
        return 400, {"error": "序列没有机位: %s" % name, "names": sequence_names()}

    cfg = store.load_config()
    base = body.get("base") if isinstance(body.get("base"), dict) else {}
    same_seed = _truthy(body.get("same_seed", True))

    plans = []
    for shot in shots:
        over = dict(base)
        over.update(shot.get("params") or {})
        preview = build_preview(over, cfg)
        plans.append({
            "label": str(shot.get("label") or ""),
            "preview": preview,
            "gen": _gen_params(over, cfg),
        })

    if not plans[0]["gen"]["checkpoint"] and not plans[0]["gen"]["workflow_template"]:
        return 400, {
            "error": "checkpoint 未选择：先 GET /api/comfy/models 取 checkpoints，再在 base 里带 checkpoint",
            "hint": '例如 {"name": "环绕 8 机位", "base": {"checkpoint": "Illustrious-XL-v1.0.safetensors"}}',
            "checkpoints": api_models().get("checkpoints") or [],
        }

    try:
        seed_in = int(float(body.get("seed", base.get("seed", -1))))
    except (TypeError, ValueError):
        seed_in = -1
    randomize = body.get("randomize")
    randomize = (seed_in < 0) if randomize is None else _truthy(randomize)

    if randomize or seed_in < 0:
        first = random.randrange(0, 2 ** 31)
        seeds = [first if same_seed else random.randrange(0, 2 ** 31) for _ in plans]
        seed_source = "random"
    else:
        fixed = seed_in & 0xFFFFFFFF
        seeds = [fixed if same_seed else (fixed + i) & 0xFFFFFFFF for i in range(len(plans))]
        seed_source = "fixed"

    # 在线判定放在任何 submit 之前：离线就是整单 503，一张都不进队列
    if not BRIDGE.reachable():
        return 503, {
            "error": "ComfyUI 未启动，整单未提交",
            "comfy_url": BRIDGE.base,
            "name": name,
            "same_seed": bool(same_seed),
            "shots": [],
        }

    results = []
    for idx, plan in enumerate(plans):
        preview = plan["preview"]
        gen = dict(plan["gen"])
        gen["seed"] = seeds[idx]
        shot_warn = []
        try:
            graph, graph_meta = _build_graph_safe(
                gen, preview["positive"], preview["negative"], shot_warn)
            prompt_id = BRIDGE.submit(graph)
        except comfy_bridge.ComfyError as exc:
            return 502, {
                "error": str(exc),
                "comfy_url": BRIDGE.base,
                "name": name,
                "same_seed": bool(same_seed),
                "shots": results,
                "failed_label": plan["label"],
            }
        with TASK_LOCK:
            TASKS[prompt_id] = {
                "prompt_id": prompt_id,
                "t0": time.time(),
                "steps": gen["steps"],
                "seed": seeds[idx],
                "seed_source": seed_source,
                "images": None,
                "gen": dict(gen),
                "positive": preview["positive"],
                "negative": preview["negative"],
                "last_state": {"state": "queued", "progress": 0.0, "node": ""},
            }
            CURRENT["prompt_id"] = prompt_id
            _trim_tasks()
        results.append({
            "label": plan["label"],
            "prompt_id": prompt_id,
            "seed": seeds[idx],
            "positive": preview["positive"],
            "negative": preview["negative"],
            "params": preview["params"],
            "template": str(graph_meta.get("template") or ""),
            "warn": shot_warn + list(graph_meta.get("warn") or []),
        })

    return 200, {
        "ok": True,
        "name": name,
        "desc": str(seq.get("desc") or ""),
        "same_seed": bool(same_seed),
        "shots": results,
    }


# ══════════════════════ v2.3：模板注入兜底 / ControlNet 上传 / 批量队列 ══════════════════════
def _build_graph_safe(gen, positive, negative, warn):
    """建图；模板注入失败只记 warn 并回退内置最小图，绝不把 500 抛到路由层。"""
    try:
        return BRIDGE.build_graph(gen, positive, negative)
    except comfy_bridge.ComfyError as exc:
        tpl = str((gen or {}).get("workflow_template") or "").strip()
        if not tpl:
            raise
        warn.append("模板注入失败，已回退内置图: %s" % exc)
        alt = dict(gen)
        alt["workflow_template"] = ""
        return BRIDGE.build_prompt(alt, positive, negative), {
            "template": "", "warn": [], "fallback": True,
        }


def _empty_sniff():
    return {"camera": [], "positive": [], "negative": [], "sampler": "", "latent": "",
            "save": "", "controlnet": {"depth": [], "pose": []}, "warn": []}


def api_templates_inspect(body):
    """POST /api/templates/inspect：模板 id / 路径 → 会被注入哪几个节点。"""
    body = body if isinstance(body, dict) else {}
    ref = str(body.get("ref") or body.get("template") or "").strip()
    if not ref:
        return 400, {"ok": False, "error": "ref 必填（模板 id 或路径）",
                     "template": "", "nodes": 0, "sniff": _empty_sniff(), "warn": []}
    try:
        import template_bridge
    except Exception as exc:  # noqa: BLE001
        return 503, {"ok": False, "error": "template_bridge 不可用: %s" % exc,
                     "template": ref, "nodes": 0, "sniff": _empty_sniff(), "warn": []}
    try:
        info = template_bridge.inspect(ref)
    except template_bridge.TemplateError as exc:
        return 200, {"ok": False, "error": str(exc), "template": ref, "nodes": 0,
                     "node_list": [], "vars": [], "sniff": _empty_sniff(),
                     "warn": [str(exc)]}
    except Exception as exc:  # noqa: BLE001
        return 200, {"ok": False, "error": "inspect 失败: %s" % exc, "template": ref,
                     "nodes": 0, "node_list": [], "vars": [],
                     "sniff": _empty_sniff(), "warn": [str(exc)]}

    sniff = {key: info.get(key) for key in
             ("camera", "positive", "negative", "sampler", "latent", "save", "controlnet")}
    payload = {
        "ok": True,
        "template": info.get("template") or "",
        "template_path": info.get("template_path") or "",
        "nodes": int(info.get("nodes") or 0),
        "node_list": list(info.get("node_list") or []),
        "vars": list(info.get("vars") or []),
        "schema_title": info.get("schema_title") or "",
        "sniff": sniff,
        "warn": list(info.get("warn") or []),
    }
    payload.update(sniff)  # 同时给扁平字段，前端老写法也能读
    return 200, payload


def api_controlnet_upload(body):
    """POST /api/controlnet/upload：data URL → ComfyUI input/（顺带存一份本地副本）。"""
    body = body if isinstance(body, dict) else {}
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in ("depth", "pose"):
        return 400, {"error": 'kind 只能是 "depth" 或 "pose"', "kind": kind}
    data_url = str(body.get("data_url") or "").strip()
    if not data_url.startswith(CN_DATA_PREFIX):
        return 400, {"error": "data_url 必须是 data:image/png;base64,... 形式的 PNG data URL"}
    try:
        blob = base64.b64decode(data_url[len(CN_DATA_PREFIX):], validate=False)
    except (binascii.Error, ValueError) as exc:
        return 400, {"error": "base64 解码失败: %s" % exc}
    if not blob:
        return 400, {"error": "PNG 内容为空"}
    if not blob.startswith(CN_MAGIC):
        return 400, {"error": "解码结果不是 PNG（缺 PNG 文件头）"}
    if len(blob) > CN_MAX_BYTES:
        return 413, {"error": "PNG 太大（上限 %d 字节）" % CN_MAX_BYTES, "bytes": len(blob)}

    digest = hashlib.sha1(blob).hexdigest()
    filename = "cn_%s_%s.png" % (kind, digest[:8])
    rel = "controlnet/" + filename
    local = os.path.join(CN_DIR, filename)
    local_error = ""
    try:
        os.makedirs(CN_DIR, exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(blob)
    except OSError as exc:
        local_error = str(exc)
        sys.stdout.write("[studio] controlnet 本地副本写失败: %s\n" % exc)
        sys.stdout.flush()

    payload = {
        "ok": True,
        "kind": kind,
        "filename": filename,
        "subfolder": "",
        "type": "input",
        "bytes": len(blob),
        "sha1": digest,
        "local": rel,
    }
    if local_error:
        payload["local_error"] = local_error
    try:
        up = BRIDGE.upload_image(blob, filename, subfolder="", image_type="input")
    except comfy_bridge.ComfyError as exc:
        # 离线：文件已落本地副本，文件名可复用；HTTP 层按契约回 503
        out = dict(payload)
        out.update({"ok": False, "error": str(exc), "comfy_url": BRIDGE.base})
        return 503, out
    payload["filename"] = str(up.get("name") or filename)
    payload["subfolder"] = str(up.get("subfolder") or "")
    payload["type"] = str(up.get("type") or "input")
    return 200, payload


# ---------- 批量队列（studio/state/batches.json） ----------
def _load_batches():
    try:
        with open(BATCHES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("batches"), dict):
        return data["batches"]
    return data if isinstance(data, dict) else {}


def _save_batches(batches):
    ordered = sorted(batches.items(), key=lambda kv: (kv[1] or {}).get("t0") or 0.0, reverse=True)
    keep = dict(ordered[:MAX_BATCH_RECORDS])
    try:
        os.makedirs(os.path.dirname(BATCHES_PATH), exist_ok=True)
        tmp = BATCHES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "batches": keep}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, BATCHES_PATH)
    except OSError as exc:
        sys.stdout.write("[studio] batches.json 落盘失败: %s\n" % exc)
        sys.stdout.flush()
    return keep


def _remember_batch(record):
    with BATCH_LOCK:
        batches = _load_batches()
        batches[record["batch_id"]] = record
        _save_batches(batches)
    return record


def api_batch(body):
    """POST /api/batch：任意机位列表 → 逐张 compute + submit；离线整单 503 不半提交。"""
    body = body if isinstance(body, dict) else {}
    name = str(body.get("name") or "批量").strip() or "批量"
    base = body.get("base") if isinstance(body.get("base"), dict) else {}
    shots_in = body.get("shots")
    if not isinstance(shots_in, list) or not shots_in:
        return 400, {"ok": False, "error": "shots 必须是非空数组: [{label, params}]"}
    if len(shots_in) > MAX_BATCH_SHOTS:
        return 400, {"ok": False, "error": "一次最多 %d 张" % MAX_BATCH_SHOTS,
                     "count": len(shots_in)}
    same_seed = _truthy(body.get("same_seed", True))
    cfg = store.load_config()

    plans = []
    for idx, shot in enumerate(shots_in):
        shot = shot if isinstance(shot, dict) else {}
        params = shot.get("params") if isinstance(shot.get("params"), dict) else {}
        over = dict(base)
        over.update(params)
        over.pop("base", None)
        label = str(shot.get("label") or ("第 %d 张" % (idx + 1)))
        try:
            preview = build_preview(over, cfg)
        except Exception as exc:  # noqa: BLE001
            return 400, {"ok": False, "error": "第 %d 张参数算不出来: %s" % (idx + 1, exc),
                         "label": label}
        plans.append({"label": label, "preview": preview, "gen": _gen_params(over, cfg)})

    if not plans[0]["gen"]["checkpoint"] and not plans[0]["gen"]["workflow_template"]:
        return 400, {
            "ok": False,
            "error": "checkpoint 未选择：先 GET /api/comfy/models 取 checkpoints，再在 base 里带 checkpoint",
            "hint": '例如 {"name": "三视图", "base": {"checkpoint": "Illustrious-XL-v1.0.safetensors"}, "shots": [{"label": "正面"}]}',
            "checkpoints": api_models().get("checkpoints") or [],
        }

    try:
        seed_in = int(float(body.get("seed", base.get("seed", -1))))
    except (TypeError, ValueError):
        seed_in = -1
    randomize = body.get("randomize")
    randomize = (seed_in < 0) if randomize is None else _truthy(randomize)
    if randomize or seed_in < 0:
        first = random.randrange(0, 2 ** 31)
        seeds = [first if same_seed else random.randrange(0, 2 ** 31) for _ in plans]
        seed_source = "random"
    else:
        fixed = seed_in & 0xFFFFFFFF
        seeds = [fixed if same_seed else (fixed + i) & 0xFFFFFFFF for i in range(len(plans))]
        seed_source = "fixed"
    seed_used = seeds[0] if seeds else 0

    # 在线判定放在任何 submit 之前：离线就是整单 503，一张都不进队列
    if not BRIDGE.reachable():
        return 503, {
            "ok": False,
            "error": "ComfyUI 未启动，整单未提交",
            "comfy_url": BRIDGE.base,
            "batch_id": "",
            "name": name,
            "seed": seed_used,
            "same_seed": bool(same_seed),
            "jobs": [],
        }

    batch_id = "b" + time.strftime("%Y%m%d%H%M%S") + os.urandom(2).hex()
    jobs = []
    for idx, plan in enumerate(plans):
        preview = plan["preview"]
        gen = dict(plan["gen"])
        gen["seed"] = seeds[idx]
        warn = []
        try:
            graph, graph_meta = _build_graph_safe(gen, preview["positive"], preview["negative"], warn)
            prompt_id = BRIDGE.submit(graph)
        except comfy_bridge.ComfyError as exc:
            return 502, {
                "ok": False,
                "error": str(exc),
                "comfy_url": BRIDGE.base,
                "batch_id": batch_id,
                "name": name,
                "seed": seed_used,
                "same_seed": bool(same_seed),
                "jobs": jobs,
                "failed_label": plan["label"],
            }
        with TASK_LOCK:
            TASKS[prompt_id] = {
                "prompt_id": prompt_id,
                "t0": time.time(),
                "steps": gen["steps"],
                "seed": seeds[idx],
                "seed_source": seed_source,
                "images": None,
                "gen": dict(gen),
                "positive": preview["positive"],
                "negative": preview["negative"],
                "last_state": {"state": "queued", "progress": 0.0, "node": ""},
            }
            CURRENT["prompt_id"] = prompt_id
            _trim_tasks()
        jobs.append({
            "label": plan["label"],
            "prompt_id": prompt_id,
            "seed": seeds[idx],
            "positive": preview["positive"],
            "negative": preview["negative"],
            "params": preview["params"],
            "template": str(graph_meta.get("template") or ""),
            "warn": warn + list(graph_meta.get("warn") or []),
        })

    record = {
        "batch_id": batch_id,
        "name": name,
        "seed": seed_used,
        "same_seed": bool(same_seed),
        "seed_source": seed_source,
        "t0": time.time(),
        "created": round(time.time(), 3),
        "jobs": [{"label": j["label"], "prompt_id": j["prompt_id"], "seed": j["seed"]}
                 for j in jobs],
    }
    _remember_batch(record)
    return 200, {
        "ok": True,
        "batch_id": batch_id,
        "name": name,
        "seed": seed_used,
        "same_seed": bool(same_seed),
        "jobs": jobs,
    }


def api_batch_status(qs=None, batch_id=""):
    """GET /api/batch：不带 batch_id 列最近记录，带了就聚合每张的进度。"""
    if isinstance(qs, dict):
        batch_id = batch_id or _one(qs, "batch_id")
    batch_id = str(batch_id or "").strip()
    with BATCH_LOCK:
        batches = _load_batches()

    if not batch_id:
        recent = sorted(batches.values(), key=lambda r: (r or {}).get("t0") or 0.0, reverse=True)
        return 200, {"ok": True, "batches": [{
            "batch_id": r.get("batch_id"),
            "name": r.get("name") or "",
            "seed": r.get("seed"),
            "same_seed": bool(r.get("same_seed")),
            "total": len(r.get("jobs") or []),
            "created": r.get("created"),
        } for r in recent[:20]]}

    record = batches.get(batch_id)
    if not isinstance(record, dict):
        return 404, {"ok": False, "error": "unknown batch_id: %s" % batch_id,
                     "batch_id": batch_id}

    jobs = []
    done = 0
    states = []
    for job in record.get("jobs") or []:
        prompt_id = str((job or {}).get("prompt_id") or "")
        if prompt_id:
            try:
                prog = api_progress(prompt_id)
            except Exception as exc:  # noqa: BLE001
                prog = {"state": "error", "progress": 0.0, "images": [],
                        "error": "progress 失败: %s" % exc}
        else:
            prog = {"state": "error", "progress": 0.0, "images": [], "error": "缺 prompt_id"}
        state = str(prog.get("state") or "queued")
        states.append(state)
        if state == "done":
            done += 1
        entry = {
            "label": (job or {}).get("label") or "",
            "prompt_id": prompt_id,
            "seed": (job or {}).get("seed"),
            "state": state,
            "progress": float(prog.get("progress") or 0.0),
            "node": prog.get("node") or "",
            "images": list(prog.get("images") or []),
            "error": prog.get("error"),
        }
        if prog.get("warn"):
            entry["warn"] = prog["warn"]
        jobs.append(entry)

    total = len(jobs)
    if total and done == total:
        state = "done"
    elif any(s in ("running", "queued") for s in states):
        state = "running" if (done or any(s == "running" for s in states)) else "queued"
    elif any(s == "error" for s in states):
        state = "error"
    else:
        state = "queued"

    return 200, {
        "ok": True,
        "batch_id": batch_id,
        "name": record.get("name") or "",
        "seed": record.get("seed"),
        "same_seed": bool(record.get("same_seed")),
        "state": state,
        "done": done,
        "total": total,
        "jobs": jobs,
        "created": record.get("created"),
    }


# ══════════════════════ HTTP 处理器 ══════════════════════
class StudioHandler(BaseHTTPRequestHandler):
    server_version = "CameraStudio/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 日志 ----------
    def log_message(self, fmt, *args):
        sys.stdout.write("[studio] %s - %s" % (self.address_string(), fmt % args))
        sys.stdout.write(chr(10))
        sys.stdout.flush()

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)

    # ---------- 响应 ----------
    def _send(self, status, body, content_type, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8")

    def _err(self, status, message, **extra):
        payload = {"error": str(message)}
        payload.update(extra)
        self._json(payload, status)

    def _fault(self, exc):
        sys.stdout.write("[studio] unhandled: %s" % exc)
        sys.stdout.write(chr(10))
        traceback.print_exc()
        sys.stdout.flush()
        try:
            self._err(500, "internal error: %s" % exc)
        except Exception:
            pass

    # ---------- 请求体 ----------
    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        length = max(0, min(length, MAX_BODY))
        if not length:
            return {}
        try:
            raw = self.rfile.read(length)
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 分发 ----------
    def do_GET(self):
        try:
            self._route("GET")
        except Exception as exc:
            self._fault(exc)

    def do_HEAD(self):
        try:
            self._route("GET")
        except Exception as exc:
            self._fault(exc)

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as exc:
            self._fault(exc)

    def do_DELETE(self):
        try:
            self._route("DELETE")
        except Exception as exc:
            self._fault(exc)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain; charset=utf-8", {
            "Allow": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        })

    def _route(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path or "/")
        qs = urllib.parse.parse_qs(parsed.query or "")

        if method == "GET":
            if path == "/api/health":
                return self._json(api_health())
            if path == "/api/meta":
                return self._json(api_meta())
            if path == "/api/sequences":
                return self._json(api_sequences())
            if path == "/api/batch":
                status, payload = api_batch_status(qs)
                return self._json(payload, status)
            if path == "/api/ui_presets/export":
                return self._export_presets()
            if path == "/api/comfy/models":
                return self._json(api_models())
            if path == "/api/templates":
                return self._json(api_templates())
            if path == "/api/config":
                return self._json(store.load_config())
            if path == "/api/gallery":
                limit = int(_num(_one(qs, "limit"), 200)) or 200
                return self._json({
                    "items": store.list_gallery(min(200, max(1, limit))),
                    "out_dir": str(store.OUT_DIR),
                })
            if path == "/api/progress":
                return self._json(api_progress(_one(qs, "prompt_id")))
            if path == "/api/view":
                return self._view(qs)
            if path.startswith("/api/"):
                return self._err(404, "unknown endpoint: %s" % path)
            if path == "/js" or path.startswith("/js/"):
                return self._js(path)
            return self._static(path)

        if method == "POST":
            if path == "/api/preview":
                try:
                    return self._json(build_preview(self._body(), store.load_config()))
                except Exception as exc:
                    return self._err(400, "preview failed: %s" % exc)
            if path == "/api/generate":
                status, payload = api_generate(self._body())
                return self._json(payload, status)
            if path == "/api/interrupt":
                # ComfyUI 离线时也回 200 + ok:false：除 generate 外任何路由都不因离线报 5xx
                _ok, payload = api_interrupt()
                return self._json(payload, 200)
            if path == "/api/gallery/clear":
                return self._json({"ok": True, "cleared": store.clear_gallery()})
            if path == "/api/config":
                body = self._body()
                try:
                    return self._json({"ok": True, "config": store.update_config(body)})
                except OSError as exc:
                    # 目录不可写（低完整性标签、只读盘）：回 200 + ok:false，
                    # 前端把这句话显示出来，别抛 500 让界面看着像崩了。
                    sys.stdout.write("[studio] config 落盘失败: %s" % exc)
                    sys.stdout.write(chr(10))
                    sys.stdout.flush()
                    return self._json({"ok": False, "message": str(exc),
                                       "config": store.load_config()}, 200)
            if path == "/api/ui_presets":
                body = self._body()
                name = str(body.get("name") or "").strip()
                vals = body.get("vals")
                ok, result = store.set_ui_preset(name, vals)
                if not ok:
                    return self._err(400, result)
                return self._json({"ok": True, "name": name[:40], "ui_presets": result})
            if path == "/api/ui_presets/import":
                added, merged = store.import_presets(self._body())
                return self._json({"ok": True, "added": int(added), "ui_presets": merged})
            if path == "/api/sequence":
                status, payload = api_sequence(self._body())
                return self._json(payload, status)
            if path == "/api/controlnet/upload":
                status, payload = api_controlnet_upload(self._body())
                return self._json(payload, status)
            if path == "/api/templates/inspect":
                status, payload = api_templates_inspect(self._body())
                return self._json(payload, status)
            if path == "/api/batch":
                status, payload = api_batch(self._body())
                return self._json(payload, status)
            if path == "/api/open_folder":
                _ok, payload = api_open_folder()
                return self._json(payload, 200 if payload.get("ok") else 500)
            if path.startswith("/api/"):
                return self._err(404, "unknown endpoint: %s" % path)
            return self._err(404, "unknown endpoint: %s" % path)

        if method == "DELETE":
            if path == "/api/ui_presets":
                name = _one(qs, "name") or str(self._body().get("name") or "")
                existed, presets = store.delete_ui_preset(name)
                return self._json({"ok": True, "deleted": existed, "name": name, "ui_presets": presets})
            if path.startswith("/api/"):
                return self._err(404, "unknown endpoint: %s" % path)

        return self._err(405, "method not allowed: %s %s" % (method, path))

    # ---------- /api/view ----------
    def _view(self, qs):
        local = _one(qs, "local")
        if local:
            target = _resolve_under(store.OUT_DIR, local)
            if not target:
                # resolve 之后不在 out 根内：契约要求 400/403，这里给 403
                return self._err(403, "forbidden path")
            if not os.path.isfile(target):
                return self._err(404, "file not found: %s" % local)
            try:
                with open(target, "rb") as fh:
                    blob = fh.read()
            except OSError as exc:
                return self._err(404, "cannot read: %s" % exc)
            return self._send(200, blob, _guess_mime(target), {"Cache-Control": "max-age=60"})

        filename = _one(qs, "filename")
        if not filename:
            return self._err(400, "local 或 filename 必填")
        try:
            blob = BRIDGE.view_bytes(
                filename,
                _one(qs, "subfolder"),
                _one(qs, "type") or "output",
            )
        except comfy_bridge.ComfyError as exc:
            # 图要么在 ComfyUI 里、要么不在；离线也按「找不到」回 404，不抛 5xx
            return self._err(404, str(exc))
        return self._send(200, blob, _guess_mime(filename))

    # ---------- /api/ui_presets/export ----------
    def _export_presets(self):
        raw = json.dumps(store.export_presets(), ensure_ascii=False, indent=2)
        return self._send(
            200,
            raw,
            "application/json; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="ui_presets.json"'},
        )

    # ---------- /js/*（仓库根 js/，防穿越） ----------
    def _js(self, url_path):
        rel = urllib.parse.unquote(url_path or "/js/")[len("/js"):].lstrip("/")
        if not rel:
            return self._err(404, "js file required: /js/<file>", js_dir=str(JS_DIR))
        target = _resolve_under(JS_DIR, rel)
        if not target:
            return self._err(403, "forbidden path")
        if not os.path.isfile(target):
            return self._err(404, "js file not found: %s" % rel, js_dir=str(JS_DIR))
        try:
            with open(target, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            return self._err(500, "cannot read %s: %s" % (rel, exc))
        ctype = _guess_mime(target)
        if pathlib.Path(target).suffix.lower() in (".js", ".mjs"):
            ctype = "text/javascript; charset=utf-8"
        return self._send(200, blob, ctype)

    # ---------- 静态 ----------
    def _static(self, url_path):
        rel = urllib.parse.unquote(url_path or "/").lstrip("/")
        if not rel:
            rel = "index.html"
        if rel.endswith("/"):
            rel = rel + "index.html"
        target = _resolve_under(store.WEB_DIR, rel)
        if not target:
            return self._err(403, "forbidden path")
        if os.path.isdir(target):
            target = _resolve_under(store.WEB_DIR, rel.rstrip("/") + "/index.html")
            if not target:
                return self._err(403, "forbidden path")
        if not os.path.isfile(target):
            return self._err(404, "static file not found: %s" % rel, web_dir=str(store.WEB_DIR))
        try:
            with open(target, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            return self._err(500, "cannot read %s: %s" % (rel, exc))
        return self._send(200, blob, _guess_mime(target))


# ══════════════════════ 入口 ══════════════════════
def main(argv=None):
    global BRIDGE, PORT
    parser = argparse.ArgumentParser(description="SDXL camera studio backend")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--comfy", default="http://127.0.0.1:8188")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    PORT = int(args.port)
    BRIDGE = comfy_bridge.ComfyBridge(args.comfy)
    store.ensure_dirs()

    reachable = BRIDGE.reachable()
    url = "http://127.0.0.1:%d" % PORT

    print("[studio] %s" % url)
    print("[studio] comfy: %s (%s)" % (BRIDGE.base, "online" if reachable else "offline"))
    print("[studio] web:   %s" % store.WEB_DIR)
    print("[studio] out:   %s" % store.OUT_DIR)
    print("[studio] state: %s" % store.STATE_DIR)
    sys.stdout.flush()

    try:
        httpd = ThreadingHTTPServer((args.host, PORT), StudioHandler)
    except OSError as exc:
        print("[studio] cannot bind %s:%d - %s" % (args.host, PORT, exc))
        sys.stdout.flush()
        return 2
    httpd.daemon_threads = True

    if not args.no_browser:
        def _open():
            time.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("[studio] keyboard interrupt, stopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
