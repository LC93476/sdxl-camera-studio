# -*- coding: utf-8 -*-
"""token 统计 —— 能给真分词器就用真的，拿不到就估算（绝不给路由层抛异常）。

对外只有四个函数：
    count(text) -> int              整段提示词（可含 "(tag:1.20)" 权重语法）
    count_tags(tags) -> int         tag 列表逐条相加
    fragments(parts) -> list        compute_camera 的 parts[] → [{kind,key,text,tokens}]
    method() -> "tokenizer"|"estimate"

估算规则（SPEC v2 token 预算）：
    去掉权重的 tag 文本按 3.6 字符 / token 向上取整；
    每个带权重的 tag（"(side view:1.20)"）额外 +3 token（权重语法本身的成本）。

真分词器：sys.path 里塞 ComfyUI 根目录，import comfy.sd1_clip.SDTokenizer，
用 tokenize_with_weights 数 token（与出图时同一套 CLIP-L 分词）。import 失败/超时/异常
一律静默降级到估算 —— 这个模块永远不抛。
"""

import math
import os
import re
import sys
import threading

# ComfyUI 根目录（可用环境变量覆盖；默认本机安装路径）
COMFY_DIR = os.environ.get("SDXL_COMFY_DIR") or r"D:/QUARK/ComfyUI-aki-v3.2/ComfyUI"

CHARS_PER_TOKEN = 3.6     # 估算：每个 token 吃掉的字符数
WEIGHT_EXTRA = 3          # 估算：加权 tag 的额外 token
BOS_ID = 49406
EOS_ID = 49407

_WEIGHT_RE = re.compile(r"^\((.*):\s*([-+]?[0-9]*\.?[0-9]+)\s*\)$", re.S)

_LOCK = threading.Lock()
_CACHE = {"tried": False, "tk": None, "err": ""}


# ────────────────────────── 估算 ──────────────────────────
def split_tags(text):
    """按逗号拆 tag，去空。"""
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


def unweight(tag):
    """("(side view:1.20)") → ("side view", True)；无权重 → (原文, False)。"""
    text = str(tag or "").strip()
    m = _WEIGHT_RE.match(text)
    if m:
        return m.group(1).strip(), True
    return text, False


def _estimate_tag(tag):
    plain, weighted = unweight(tag)
    n = int(math.ceil(len(plain) / CHARS_PER_TOKEN)) if plain else 0
    if weighted:
        n += WEIGHT_EXTRA
    return n


def estimate(text):
    try:
        return sum(_estimate_tag(tag) for tag in split_tags(text))
    except Exception:
        return 0


# ────────────────────────── 真分词器 ──────────────────────────
def _load_tokenizer():
    """懒加载一次；失败就永久记失败（不反复 import 拖慢预览）。"""
    with _LOCK:
        if _CACHE["tried"]:
            return _CACHE["tk"]
        _CACHE["tried"] = True
        try:
            if COMFY_DIR and COMFY_DIR not in sys.path:
                sys.path.insert(0, COMFY_DIR)
            from comfy.sd1_clip import SDTokenizer  # noqa: F401  只在有 ComfyUI 时可用
            tok = SDTokenizer()
            if _tokenizer_count(tok, "front view") <= 0:
                raise RuntimeError("tokenizer produced 0 tokens")
            _CACHE["tk"] = tok
        except BaseException as exc:   # noqa: BLE001 - 连 SystemExit/KeyboardInterrupt 都不许冒
            _CACHE["tk"] = None
            _CACHE["err"] = "%s: %s" % (type(exc).__name__, exc)
        return _CACHE["tk"]


def tokenizer_error():
    """降级原因（调试用，路由层可以打进 warn）。"""
    _load_tokenizer()
    return _CACHE["err"]


def _tokenizer_count(tok, text):
    chunk = tok.tokenize_with_weights(str(text or ""))
    if isinstance(chunk, tuple):
        chunk = list(chunk)
    if not isinstance(chunk, list) or not chunk:
        return 0
    toks = chunk[0] if isinstance(chunk[0], list) else chunk
    ids = []
    for item in toks:
        if isinstance(item, (tuple, list)):
            if item:
                ids.append(item[0])
        else:
            ids.append(item)
    ints = [i for i in ids if isinstance(i, int)]
    if not ints:
        return max(1, len(ids)) if ids else 0
    if EOS_ID in ints:
        ints = ints[:ints.index(EOS_ID)]
    if ints and ints[0] == BOS_ID:
        ints = ints[1:]
    return max(0, len(ints))


# ────────────────────────── 对外接口 ──────────────────────────
def method():
    """"tokenizer"（真 CLIP-L 分词）或 "estimate"（3.6 字符/token 估算）。"""
    try:
        return "tokenizer" if _load_tokenizer() is not None else "estimate"
    except BaseException:
        return "estimate"


def count(text):
    """整段文本的 token 数。"""
    try:
        tok = _load_tokenizer()
        if tok is not None:
            n = _tokenizer_count(tok, text)
            if n >= 0:
                return int(n)
    except BaseException:
        pass
    return int(estimate(text))


def count_tags(tags):
    """tag 列表（可含权重语法）逐条相加；这是 token_parts 的逐段口径。"""
    if isinstance(tags, str):
        tags = split_tags(tags)
    if not isinstance(tags, (list, tuple)):
        return 0
    try:
        tok = _load_tokenizer()
        if tok is not None:
            return int(sum(_tokenizer_count(tok, tag) for tag in tags))
    except BaseException:
        pass
    return int(sum(_estimate_tag(tag) for tag in tags))


def fragments(parts):
    """compute_camera 的 parts[] → [{"kind","key","text","tokens"}]（逐段计数）。"""
    out = []
    if not isinstance(parts, (list, tuple)):
        return out
    for part in parts:
        if not isinstance(part, dict):
            continue
        tokens = part.get("tokens")
        if isinstance(tokens, (list, tuple)) and tokens:
            tags = [str(t) for t in tokens]
        else:
            tags = split_tags(part.get("phrase") or "")
        if not tags:
            continue
        text = ", ".join(tags)
        out.append({
            "kind": str(part.get("kind") or ""),
            "key": str(part.get("key") or ""),
            "text": text,
            "tokens": count_tags(tags),
        })
    return out


__all__ = [
    "COMFY_DIR", "CHARS_PER_TOKEN", "WEIGHT_EXTRA",
    "split_tags", "unweight", "estimate",
    "count", "count_tags", "fragments", "method", "tokenizer_error",
]
