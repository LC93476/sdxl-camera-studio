# -*- coding: utf-8 -*-
"""SDXL 相机工作台 · 持久化层

只管磁盘，不碰 HTTP、不碰 ComfyUI：

- state/config.json   用户参数持久化
- state/presets.json  用户保存的相机姿态（UI 预设）
- out/                出图根目录 + 画廊索引（mtime 倒序）

所有写盘都走「临时文件 + os.replace」，进程被杀也不会留下半个 JSON。
只依赖标准库。
"""

import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # sdxl_camera_angle/
WEB_DIR = HERE / "web"
STATE_DIR = HERE / "state"
OUT_DIR = HERE / "out"
CONFIG_PATH = STATE_DIR / "config.json"
PRESETS_PATH = STATE_DIR / "presets.json"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# 前端需要的全部默认值（缺字段时前端/后端都靠这份兜底）
DEFAULT_CONFIG = {
    # 相机
    "pos_x": 0.0,
    "pos_y": 0.0,
    "pos_z": 0.0,
    "roll": 0.0,
    "focal": 50.0,
    "pan_x": 0.0,
    "pan_y": 0.0,
    "subject_yaw": 0.0,
    "gaze_mode": "auto",
    "preset": "自定义",
    "strength": 1.0,
    "weight_mode": "tanh",
    "azimuth_mode": "8bin",
    "elevation_mode": "3bin",
    "neg_strength": 1.0,
    "extra_positive": "",
    "extra_negative": "",
    "manual_positive": "",
    "manual_negative": "",
    # 采样
    "seed": -1,
    "randomize": True,
    "steps": 28,
    "cfg": 5.0,
    "width": 832,
    "height": 1216,
    "sampler": "euler_ancestral",
    "scheduler": "karras",
    "checkpoint": "",
    "lora_name": "",
    "lora_strength": 0.8,
    "batch": 1,
    "save_prefix": "camera",
    # 外部工作流模板（空 = 用内置最小图）
    "workflow_template": "",
    "negative_mode": "append",
    "pose_id": "",
    # 环境
    "comfy_url": "http://127.0.0.1:8188",
}


# ══════════════════════ 目录与原子写 ══════════════════════
def ensure_dirs():
    """建好 state/ 与 out/（幂等）。"""
    for d in (STATE_DIR, OUT_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return {"state": str(STATE_DIR), "out": str(OUT_DIR)}


def _dump(fh, data):
    json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


def _write_hint(path, err):
    """写不进去时给一句能照着做的提示。

    Windows 上最常见的原因不是磁盘满，而是「进程完整性级别」：
    ComfyUI 便携版自带的 python.exe 是**低完整性**进程（S-1-16-4096），
    只能写标了低完整性标签的目录；目标目录没有标签就会被拒绝（EACCES）。
    修法见 README「写不进 state / out 目录」。
    """
    return ("写不进 %s（%s）。若启动用的是 ComfyUI 便携版 python，"
            "多半是低完整性标签问题：对项目目录执行 "
            "icacls \"<项目目录>\" /setintegritylevel (OI)(CI)L 后重开服务。" % (path, err))


def atomic_write_json(path, data):
    """临时文件 + os.replace；同目录建临时文件，保证跨盘也不出错。

    临时文件被拒（EACCES/EPERM/EROFS）时退化成直接写目标文件：
    先保住数据，再往上抛可读的错误，而不是丢一个裸 traceback 给前端。
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    tmp = path.parent / ("%s.%s.tmp" % (path.name, uuid.uuid4().hex[:8]))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            _dump(fh, data)
        os.replace(str(tmp), str(path))
        return path
    except OSError as err:
        if getattr(err, "errno", None) not in (13, 1, 30, None):
            raise
        tmp_error = err
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            _dump(fh, data)
    except OSError as err:
        raise StoreWriteError(_write_hint(path, err)) from err
    return path


def read_json(path, default=None):
    """读 JSON；文件缺失/损坏都返回 default（损坏件改名留档，不覆盖用户数据）。"""
    path = Path(path)
    if not path.is_file():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        try:
            path.replace(path.with_suffix(path.suffix + ".broken"))
        except OSError:
            pass
        return default
    return data


# ══════════════════════ 类型归整 ══════════════════════
def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _sanitize(defaults, data):
    """按 defaults 的类型把 data 归整，多余键丢弃、缺失键补默认。"""
    out = dict(defaults)
    if not isinstance(data, dict):
        return out
    for key, dv in defaults.items():
        if key not in data or data[key] is None:
            continue
        v = data[key]
        if isinstance(dv, bool):
            out[key] = _as_bool(v)
        elif isinstance(dv, int):
            try:
                out[key] = int(float(v))
            except (TypeError, ValueError):
                pass
        elif isinstance(dv, float):
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                pass
        else:
            out[key] = str(v)
    return out


class StoreWriteError(OSError):
    """state/out 目录不可写：带人话提示，前端可以直接显示。"""


# ══════════════════════ config ══════════════════════
def load_config():
    """返回完整配置（与 DEFAULT_CONFIG 同键集）。"""
    return _sanitize(DEFAULT_CONFIG, read_json(CONFIG_PATH, {}))


def save_config(config):
    """整份保存，返回落盘后的配置。"""
    cfg = _sanitize(DEFAULT_CONFIG, config)
    ensure_dirs()
    atomic_write_json(CONFIG_PATH, cfg)  # 失败抛 StoreWriteError（带提示）
    return cfg


def update_config(patch):
    """按字段合并保存（前端只发变化的那几项）。"""
    cur = load_config()
    if isinstance(patch, dict):
        for key, value in patch.items():
            if key in DEFAULT_CONFIG:
                cur[key] = value
    return save_config(cur)


# ══════════════════════ UI 预设 ══════════════════════
# v2：用户预设从一个 4 元组扩展成「当前全部相机参数」。
# 旧文件里的 [pos_x, pos_y, pos_z, roll] 读时自动补齐下面这份默认，不改用户盘上数据。
PRESET_SCHEMA = {
    "pos_x": 0.0,
    "pos_y": 0.0,
    "pos_z": 0.0,
    "roll": 0.0,
    "focal": 50.0,
    "pan_x": 0.0,
    "pan_y": 0.0,
    "subject_yaw": 0.0,
    "gaze_mode": "auto",
    "preset": "自定义",
    "strength": 1.0,
    "weight_mode": "tanh",
    "azimuth_mode": "8bin",
    "elevation_mode": "3bin",
    "neg_strength": 1.0,
    "extra_positive": "",
    "extra_negative": "",
}

PRESET_KEYS = tuple(PRESET_SCHEMA.keys())
LEGACY_PRESET_KEYS = ("pos_x", "pos_y", "pos_z", "roll")
PRESET_NAME_MAX = 40


def _coerce_preset(value):
    """dict 或旧式 [x, y, z, roll] → 补齐 v2 默认的完整参数 dict；不合法返回 None。"""
    if isinstance(value, (list, tuple)):
        if len(value) < 4:
            return None
        out = dict(PRESET_SCHEMA)
        for key, raw in zip(LEGACY_PRESET_KEYS, list(value)[:4]):
            try:
                out[key] = round(float(raw), 4)
            except (TypeError, ValueError):
                return None
        return out
    if not isinstance(value, dict):
        return None
    out = dict(PRESET_SCHEMA)
    touched = False
    for key, default in PRESET_SCHEMA.items():
        if key not in value or value[key] is None:
            continue
        raw = value[key]
        if isinstance(default, bool):
            out[key] = _as_bool(raw)
        elif isinstance(default, (int, float)):
            try:
                out[key] = round(float(raw), 4)
            except (TypeError, ValueError):
                continue
        else:
            out[key] = str(raw)[:200]
        touched = True
    return out if touched else None


def _coerce_vals(vals):
    """兼容层：旧调用点拿到的是 [x, y, z, roll]（4 个 float）或 None。"""
    cv = _coerce_preset(vals)
    if cv is None:
        return None
    return [cv[key] for key in LEGACY_PRESET_KEYS]


def load_presets():
    """{名字: 完整参数 dict}（旧式 4 元组读时补齐默认值）。"""
    raw = read_json(PRESETS_PATH, {})
    out = {}
    if isinstance(raw, dict):
        for name, vals in raw.items():
            name = str(name).strip()[:PRESET_NAME_MAX]
            if not name:
                continue
            cv = _coerce_preset(vals)
            if cv is not None:
                out[name] = cv
    return out


def save_presets(presets):
    data = {}
    if isinstance(presets, dict):
        for name, vals in presets.items():
            name = str(name).strip()[:PRESET_NAME_MAX]
            cv = _coerce_preset(vals)
            if name and cv is not None:
                data[name] = cv
    ensure_dirs()
    atomic_write_json(PRESETS_PATH, data)
    return data


def set_ui_preset(name, vals):
    """新增/覆盖一个用户预设（vals 可以是 dict，也可以是旧式 4 元组）；返回 (ok, 全量预设 或 错误串)。"""
    name = str(name or "").strip()[:PRESET_NAME_MAX]
    if not name:
        return False, "name 不能为空"
    cv = _coerce_preset(vals)
    if cv is None:
        return False, "vals 必须是参数字典，或旧式 4 个数字 [pos_x, pos_y, pos_z, roll]"
    presets = load_presets()
    presets[name] = cv
    return True, save_presets(presets)


def export_presets(presets=None):
    """导出体 {"version": 2, "presets": {名字: dict}}；给了 presets 就完全不碰磁盘。"""
    data = load_presets() if presets is None else presets
    clean = {}
    if isinstance(data, dict):
        for name, vals in data.items():
            key = str(name).strip()[:PRESET_NAME_MAX]
            cv = _coerce_preset(vals)
            if key and cv is not None:
                clean[key] = cv
    return {"version": 2, "presets": clean}


def import_presets(payload, persist=True):
    """合并导入：接受 {"presets": {...}} / {"version": 2, "presets": {...}} / 裸 {名字: 参数}。

    返回 (added, 合并后的全量预设)。persist=False 时只算不写盘（纯函数，便于单测）。
    """
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("presets"), dict):
        data = payload["presets"]
    if not isinstance(data, dict):
        return 0, load_presets()
    presets = load_presets()
    added = 0
    for name, vals in data.items():
        key = str(name).strip()[:PRESET_NAME_MAX]
        cv = _coerce_preset(vals)
        if not key or cv is None:
            continue
        presets[key] = cv
        added += 1
    if not persist:
        return added, presets
    return added, save_presets(presets)


def delete_ui_preset(name):
    """删除用户预设；返回 (是否删掉了, 全量预设)。"""
    name = str(name or "").strip()
    presets = load_presets()
    existed = name in presets
    if existed:
        presets.pop(name, None)
        save_presets(presets)
    return existed, presets


# ══════════════════════ 输出目录 / 画廊 ══════════════════════
_SAFE_STEM = re.compile("[^A-Za-z0-9._-]+")


def unique_output_path(orig_name):
    """<原名去扩展>_<短uuid>.png 落到 out/，绝不覆盖已有文件。"""
    ensure_dirs()
    stem = Path(str(orig_name or "image")).stem
    stem = _SAFE_STEM.sub("_", stem).strip("._-")[:60] or "image"
    while True:
        dest = OUT_DIR / ("%s_%s.png" % (stem, uuid.uuid4().hex[:8]))
        if not dest.exists():
            return dest


def out_rel(path):
    """out/ 内的相对路径（用于 /api/view?local=…）。"""
    path = Path(path)
    try:
        return os.path.relpath(str(path), str(OUT_DIR)).replace(os.sep, "/")
    except ValueError:
        return path.name


def meta_path(image_path):
    return Path(str(image_path) + ".json")


def write_meta(image_path, meta):
    """在图片旁边记一份 sidecar（seed 等），画廊用它显示角标。"""
    if not isinstance(meta, dict):
        return None
    data = {k: v for k, v in meta.items() if v is not None}
    try:
        return atomic_write_json(meta_path(image_path), data)
    except OSError:
        return None


def read_meta(image_path):
    data = read_json(meta_path(image_path), {})
    return data if isinstance(data, dict) else {}


def list_gallery(limit=200):
    """out/ 里所有图片，mtime 倒序，最多 limit 条。"""
    ensure_dirs()
    rows = []
    try:
        entries = list(OUT_DIR.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in IMAGE_EXTS:
                continue
            st = entry.stat()
        except OSError:
            continue
        rel = out_rel(entry)
        item = {
            "url": "/api/view?local=" + urllib.parse.quote(rel),
            "filename": entry.name,
            "local": rel,
            "mtime": round(st.st_mtime, 3),
            "size": st.st_size,
        }
        meta = read_meta(entry)
        if "seed" in meta:
            item["seed"] = meta["seed"]
        if "prompt_id" in meta:
            item["prompt_id"] = meta["prompt_id"]
        rows.append((st.st_mtime, item))
    rows.sort(key=lambda kv: kv[0], reverse=True)
    return [item for _, item in rows[:max(1, int(limit))]]


def clear_gallery():
    """删掉 out/ 里的图片与 sidecar，返回删除数量（其它文件不动）。"""
    ensure_dirs()
    removed = 0
    try:
        entries = list(OUT_DIR.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            low = entry.name.lower()
            if entry.suffix.lower() in IMAGE_EXTS or low.endswith(".png.json") \
                    or low.endswith(".jpg.json") or low.endswith(".jpeg.json") \
                    or low.endswith(".webp.json"):
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def now():
    return time.time()


__all__ = [
    "HERE", "ROOT", "WEB_DIR", "STATE_DIR", "OUT_DIR", "CONFIG_PATH", "PRESETS_PATH",
    "IMAGE_EXTS", "DEFAULT_CONFIG",
    "ensure_dirs", "atomic_write_json", "read_json",
    "load_config", "save_config", "update_config",
    "PRESET_SCHEMA", "PRESET_KEYS", "PRESET_NAME_MAX",
    "load_presets", "save_presets", "set_ui_preset", "delete_ui_preset",
    "export_presets", "import_presets",
    "unique_output_path", "out_rel", "write_meta", "read_meta",
    "list_gallery", "clear_gallery", "meta_path", "now",
]
