#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SDXL 相机工作台 · 端到端契约测试（只依赖标准库）

用法：
    D:/QUARK/ComfyUI-aki-v3.2/python/python.exe D:/QUARK/sdxl_camera_angle/studio/tools/e2e_test.py
    ... e2e_test.py --port 8850         # 固定端口
    ... e2e_test.py --no-generate       # 跳过真实出图
    ... e2e_test.py --keep-server       # 结束后不杀服务（调试）

流程：
    1. 在 8800-8899 里挑空闲端口，Popen([PY, 'studio/server.py', '--port', P, '--no-browser'])；
    2. 轮询 /api/health 直到 ok 或 25s 超时；
    3. 逐条跑契约 API，打印 PASS/FAIL 与关键字段；
    4. ComfyUI(127.0.0.1:8188) 在线时做一次真实出图：640x640 / steps=12，轮询到 done，校验 PNG；
    5. 汇总 PASS/FAIL/SKIP，FAIL>0 退出码 1。

退出码：0 全绿；1 有 FAIL；3 server.py 不在（队友还没写完）。
"""
from __future__ import print_function

import argparse
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
STUDIO_DIR = os.path.dirname(TOOLS_DIR)
APP_DIR = os.path.dirname(STUDIO_DIR)
SERVER_PY = os.path.join(STUDIO_DIR, "server.py")
WEB_DIR = os.path.join(STUDIO_DIR, "web")
OUT_DIR = os.path.join(STUDIO_DIR, "out")
PY = r"D:\QUARK\ComfyUI-aki-v3.2\python\python.exe"
COMFY = "http://127.0.0.1:8188"
CKPT_DIR = r"D:\QUARK\ComfyUI-aki-v3.2\ComfyUI\models\checkpoints"

# 契约里的相机参数（compute_camera 的入参面）
CAMERA_DEFAULT = {
    "pos_x": 0.0, "pos_y": 0.0, "pos_z": 0.0, "roll": 0.0,
    "preset": "自定义", "strength": 1.0, "weight_mode": "tanh",
    "azimuth_mode": "8bin", "elevation_mode": "3bin", "neg_strength": 1.0,
    "extra_positive": "", "extra_negative": "",
    "manual_positive": "", "manual_negative": "",
}
CAMERA_KEYS = list(CAMERA_DEFAULT.keys())
GEN_KEYS = ["seed", "randomize", "steps", "cfg", "width", "height", "sampler",
            "scheduler", "checkpoint", "lora_name", "lora_strength", "batch",
            "save_prefix"]
WEIGHT_RE = re.compile(r"\([^()]{1,120}:\s*-?\d+(?:\.\d+)?\)")

RESULTS = []
GENERATED = []
PORT = None
BASE = None
META = {}


# ---------------------------------------------------------------- 输出 ---
def say(text=""):
    print(text, flush=True)


def rec(status, name, detail=""):
    RESULTS.append([status, name, detail])
    tail = ("  · " + detail) if detail else ""
    say("[%-4s] %s%s" % (status, name, tail))
    return status == "PASS"


def ok(name, detail=""):
    return rec("PASS", name, detail)


def bad(name, detail=""):
    return rec("FAIL", name, detail)


def skip(name, detail=""):
    rec("SKIP", name, detail)
    return None


def head(s, n=110):
    s = "" if s is None else str(s)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- HTTP ---
def http(method, path, body=None, timeout=25):
    """→ (code, bytes, content_type)；网络错误 code=-1。"""
    url = BASE + path
    data = None
    headers = {"Accept": "application/json,*/*"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, payload, (e.headers.get("Content-Type") or "")
    except Exception as e:  # 连接层错误
        return -1, ("%s: %s" % (type(e).__name__, e)).encode("utf-8"), ""


def as_json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def q(path, **kw):
    return path + ("?" + urllib.parse.urlencode(kw) if kw else "")


# ---------------------------------------------------------------- 启动 ---
def pick_port(lo=8800, hi=8899):
    cands = list(range(lo, hi + 1))
    random.shuffle(cands)
    for p in cands:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    return None


def tail_file(path, nbytes=2500):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except Exception as e:
        return "<读不到日志: %s>" % e


def start_server(port):
    cmd = [PY, os.path.join("studio", "server.py"), "--port", str(port), "--no-browser"]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log = tempfile.NamedTemporaryFile(prefix="studio_e2e_", suffix=".log", delete=False)
    log.close()
    say("起服务：%s" % " ".join(cmd))
    say("  cwd=%s  log=%s" % (APP_DIR, log.name))
    lf = open(log.name, "ab")
    proc = subprocess.Popen(cmd, cwd=APP_DIR, stdout=lf, stderr=subprocess.STDOUT, env=env)
    return proc, lf, log.name


def wait_health(proc, budget=25.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < budget:
        if proc is not None and proc.poll() is not None:
            return False, "服务进程已退出（code=%s）" % proc.returncode
        code, raw, _ = http("GET", "/api/health", timeout=2)
        if code == 200:
            d = as_json(raw)
            if isinstance(d, dict) and d.get("ok"):
                return True, d
            last = "HTTP 200 但 ok=%r" % (d.get("ok") if isinstance(d, dict) else d)
        else:
            last = "HTTP %s" % code
        time.sleep(0.3)
    return False, "25s 超时（最后：%s）" % last


# ---------------------------------------------------------------- 用例 ---
def check_health():
    name = "GET /api/health"
    code, raw, ct = http("GET", "/api/health")
    if code != 200:
        return bad(name, "HTTP %s body=%s" % (code, head(raw)))
    d = as_json(raw) or {}
    missing = [k for k in ("ok", "version", "comfy_url", "comfy_reachable") if k not in d]
    if missing:
        return bad(name, "缺字段 %s；实际 %s" % (missing, head(raw)))
    if d.get("ok") is not True:
        return bad(name, "ok=%r" % d.get("ok"))
    if not isinstance(d.get("comfy_reachable"), bool):
        return bad(name, "comfy_reachable 非 bool：%r" % (d.get("comfy_reachable"),))
    return ok(name, "version=%s comfy_url=%s reachable=%s"
              % (d.get("version"), d.get("comfy_url"), d.get("comfy_reachable")))


def check_meta():
    name = "GET /api/meta"
    code, raw, _ = http("GET", "/api/meta")
    if code != 200:
        return bad(name, "HTTP %s body=%s" % (code, head(raw)))
    d = as_json(raw)
    if not isinstance(d, dict):
        return bad(name, "非 JSON 对象：%s" % head(raw))
    META.clear()
    META.update(d)
    for k in ("presets", "preset_names", "preset_modes", "ui_presets", "defaults", "enums", "vocab"):
        if k not in d:
            return bad(name, "缺字段 %s（有：%s）" % (k, sorted(d.keys())))
    presets = d.get("presets")
    if not isinstance(presets, dict):
        return bad(name, "presets 不是 dict：%r" % type(presets))
    if len(presets) != 11:
        return bad(name, "内置预设 %d 个（应为 11）：%s" % (len(presets), list(presets.keys())))
    names = d.get("preset_names")
    if not isinstance(names, list) or len(names) != 12:
        return bad(name, "preset_names 应为 12 项（自定义+11）：%r" % (names,))
    lost = [p for p in presets if p not in names]
    if lost:
        return bad(name, "preset_names 缺 %s" % lost)
    if names[0] != "自定义":
        return bad(name, "preset_names[0] 应为「自定义」，实际 %r" % (names[0],))
    defaults = d.get("defaults")
    if not isinstance(defaults, dict):
        return bad(name, "defaults 不是 dict：%r" % type(defaults))
    miss_def = [k for k in CAMERA_KEYS if k not in defaults]
    if miss_def:
        return bad(name, "defaults 缺相机字段 %s（实际 %s）" % (miss_def, sorted(defaults.keys())))
    miss_gen = [k for k in GEN_KEYS if k not in defaults]
    enums = d.get("enums") or {}
    if not isinstance(enums, dict):
        return bad(name, "enums 不是 dict：%r" % type(enums))
    for k in ("weight_mode", "azimuth_mode", "elevation_mode"):
        if not isinstance(enums.get(k), list) or not enums.get(k):
            return bad(name, "enums.%s 不是非空 list：%r" % (k, enums.get(k)))
    bad_vals = [x for x in ("tanh", "linear", "raw") if x not in enums["weight_mode"]]
    if bad_vals:
        return bad(name, "weight_mode 枚举缺 %s" % bad_vals)
    if not isinstance(d.get("ui_presets"), dict):
        return bad(name, "ui_presets 不是 dict")
    detail = ("11 预设 / names=%d / defaults=%d 键 / enums(weight_mode=%s,az=%s,el=%s) / vocab=%d 键"
              % (len(names), len(defaults), enums["weight_mode"], enums["azimuth_mode"],
                 enums["elevation_mode"], len(d.get("vocab") or {})))
    if miss_gen:
        detail += "  ⚠ defaults 缺生成字段 %s" % miss_gen
    return ok(name, detail)


def post_preview(label, over):
    body = dict(CAMERA_DEFAULT)
    body.update(over)
    code, raw, _ = http("POST", "/api/preview", body)
    if code != 200:
        return None, "HTTP %s body=%s" % (code, head(raw))
    d = as_json(raw)
    if not isinstance(d, dict):
        return None, "非 JSON：%s" % head(raw)
    for k in ("positive", "negative", "camera_info", "parts"):
        if k not in d:
            return None, "缺字段 %s（有 %s）" % (k, sorted(d.keys()))
    pos = d.get("positive")
    if not isinstance(pos, str) or not pos.strip():
        return None, "positive 空/非字符串：%r" % (pos,)
    if not isinstance(d.get("negative"), str):
        return None, "negative 非字符串：%r" % (d.get("negative"),)
    tok = d.get("tokens")
    if not isinstance(tok, dict) or not isinstance(tok.get("positive"), int):
        return None, "tokens.positive 缺失/非 int：%r" % (tok,)
    return d, "tokens=%d/%d" % (tok.get("positive"), tok.get("negative"))


def check_preview():
    name = "POST /api/preview（三组）"
    groups = [
        ("默认", {}),
        ("pos_x=1", {"pos_x": 1.0}),
        ("鸟瞰极限俯视", {"preset": "鸟瞰极限俯视"}),
    ]
    got = []
    for label, over in groups:
        d, info = post_preview(label, over)
        if d is None:
            return bad(name, "%s：%s" % (label, info))
        pos = d["positive"]
        if not WEIGHT_RE.search(pos):
            return bad(name, "%s：positive 无括号权重格式：%s" % (label, head(pos)))
        got.append((label, pos, info))
        say("       %-8s %s  │ %s" % (label, info, head(pos, 96)))
    pos_list = [g[1] for g in got]
    if len(set(pos_list)) != 3:
        return bad(name, "三组 positive 未互不相同")
    return ok(name, "三组互异 + 均含 (phrase:w) 权重；%s" % " / ".join(g[2] for g in got))


def check_models():
    name = "GET /api/comfy/models"
    code, raw, _ = http("GET", "/api/comfy/models", timeout=30)
    if code != 200:
        return bad(name, "HTTP %s body=%s" % (code, head(raw)))
    d = as_json(raw)
    if not isinstance(d, dict):
        return bad(name, "非 JSON：%s" % head(raw))
    for k in ("checkpoints", "loras", "samplers", "schedulers"):
        if not isinstance(d.get(k), list):
            return bad(name, "%s 不是 list：%r" % (k, d.get(k)))
    if not isinstance(d.get("comfy_reachable"), bool):
        return bad(name, "comfy_reachable 非 bool：%r" % (d.get("comfy_reachable"),))
    META["_models"] = d
    return ok(name, "ckpt=%d lora=%d sampler=%d scheduler=%d reachable=%s"
              % (len(d["checkpoints"]), len(d["loras"]), len(d["samplers"]),
                 len(d["schedulers"]), d["comfy_reachable"]))


def check_config():
    name = "config 读写回环"
    code, raw, _ = http("GET", "/api/config")
    if code != 200:
        return bad(name, "GET /api/config → HTTP %s body=%s" % (code, head(raw)))
    cur = as_json(raw)
    base = cur if isinstance(cur, dict) else {}
    probe_key = "_qa_probe"
    probe_val = {"ts": round(time.time(), 3), "vals": [0.1, 0.2, 0.3], "note": "e2e_test"}
    mode = None
    try:
        trial = dict(base)
        trial[probe_key] = probe_val
        code2, raw2, _ = http("POST", "/api/config", trial)
        if code2 not in (200, 201, 204):
            return bad(name, "POST /api/config → HTTP %s body=%s" % (code2, head(raw2)))
        code3, raw3, _ = http("GET", "/api/config")
        aft = as_json(raw3) or {}
        if isinstance(aft, dict) and aft.get(probe_key) == probe_val:
            mode = "新增键 %s" % probe_key
        else:
            # 后端可能只持久化白名单键：退一步改一个已存在的标量键
            scal = [k for k, v in base.items()
                    if isinstance(v, (int, float, str)) and not isinstance(v, bool)]
            if not scal:
                return bad(name, "GET 未回读 %s，且 config 里无标量键可试（实际 %s）"
                           % (probe_key, sorted(base.keys())))
            key = scal[0]
            old = base[key]
            new = (old + 1) if isinstance(old, (int, float)) else (str(old) + "-qa")
            trial2 = dict(base)
            trial2[key] = new
            code4, raw4, _ = http("POST", "/api/config", trial2)
            if code4 not in (200, 201, 204):
                return bad(name, "POST /api/config → HTTP %s body=%s" % (code4, head(raw4)))
            code5, raw5, _ = http("GET", "/api/config")
            aft2 = as_json(raw5) or {}
            if not isinstance(aft2, dict) or aft2.get(key) != new:
                return bad(name, "写 %s=%r 后读回 %r（期望 %r）"
                           % (key, new, (aft2 or {}).get(key), new))
            mode = "改 %s: %r → %r" % (key, old, new)
        return ok(name, "GET/POST 回环 OK（%s，键数 %d）" % (mode, len(aft) if isinstance(aft, dict) else -1))
    finally:
        if base:
            http("POST", "/api/config", base)


def check_ui_presets():
    name = "ui_presets 增删回环"
    pname = "_QA_%d_%d" % (os.getpid(), random.randint(1000, 9999))
    vals = [0.11, -0.22, 0.33, 0.44]
    try:
        code, raw, _ = http("POST", "/api/ui_presets", {"name": pname, "vals": vals})
        if code not in (200, 201):
            return bad(name, "POST → HTTP %s body=%s" % (code, head(raw)))
        code, raw, _ = http("GET", "/api/meta")
        d = as_json(raw) or {}
        got = (d.get("ui_presets") or {}).get(pname)
        if got is None:
            return bad(name, "POST 后在 /api/meta.ui_presets 找不到 %s（实际 %s）"
                       % (pname, list((d.get("ui_presets") or {}).keys())[:8]))
        try:
            near = [round(float(a), 3) for a in got] == [round(float(b), 3) for b in vals]
        except Exception:
            near = got == vals
        if not near:
            return bad(name, "回读值不符：%r ≠ %r" % (got, vals))
        code, raw, _ = http("DELETE", q("/api/ui_presets", name=pname))
        if code not in (200, 204):
            return bad(name, "DELETE → HTTP %s body=%s" % (code, head(raw)))
        code, raw, _ = http("GET", "/api/meta")
        d2 = as_json(raw) or {}
        if pname in (d2.get("ui_presets") or {}):
            return bad(name, "DELETE 后 %s 仍在 ui_presets" % pname)
        return ok(name, "POST 200 / meta 命中 %s / DELETE 200 / meta 已移除" % near)
    finally:
        http("DELETE", q("/api/ui_presets", name=pname))


def _gallery_items(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("items", "images", "files", "gallery"):
            if isinstance(d.get(k), list):
                return d[k]
        return None
    return None


def check_gallery(expect_filename=None):
    name = "GET /api/gallery" + ("（含新出图）" if expect_filename else "")
    code, raw, _ = http("GET", "/api/gallery")
    if code != 200:
        return bad(name, "HTTP %s body=%s" % (code, head(raw)))
    items = _gallery_items(as_json(raw))
    if items is None:
        return bad(name, "响应里找不到列表：%s" % head(raw))
    if len(items) > 200:
        return bad(name, "%d 项，超过契约上限 200" % len(items))
    for it in items[:5]:
        if not isinstance(it, dict):
            return bad(name, "条目不是对象：%r" % (it,))
        if "url" not in it or "filename" not in it:
            return bad(name, "条目缺 url/filename：%r" % (it,))
    if expect_filename:
        hit = [it for it in items
               if expect_filename in str(it.get("filename", "")) or expect_filename in str(it.get("url", ""))]
        if not hit:
            return bad(name, "新出图 %s 未出现在画廊（共 %d 项，首项 %s）"
                       % (expect_filename, len(items), head(json.dumps(items[:1], ensure_ascii=False), 120)))
        return ok(name, "新图已在画廊；共 %d 项，首项 filename=%s" % (len(items), hit[0].get("filename")))
    first = items[0] if items else {}
    return ok(name, "%d 项%s" % (len(items), ("，首项 " + str(first.get("filename"))) if items else "（空）"))


def check_gallery_clear():
    name = "POST /api/gallery/clear"
    code, raw, _ = http("GET", "/api/gallery")
    items = _gallery_items(as_json(raw)) or []
    if items:
        return skip(name, "画廊已有 %d 项，跳过清空以免删掉现有出图（真跑会 POST 一次）" % len(items))
    code, raw, _ = http("POST", "/api/gallery/clear", {})
    if code not in (200, 201, 204):
        return bad(name, "HTTP %s body=%s" % (code, head(raw)))
    return ok(name, "HTTP %s（画廊原为空）" % code)


def check_interrupt():
    name = "POST /api/interrupt"
    code, raw, _ = http("POST", "/api/interrupt", {})
    d = as_json(raw)
    if code == 200:
        return ok(name, "HTTP 200（ComfyUI 在线）body=%s" % head(raw, 80))
    if code == 503:
        return ok(name, "HTTP 503（ComfyUI 离线，契约允许）body=%s" % head(raw, 80))
    return bad(name, "HTTP %s（允许 200 或 503）body=%s" % (code, head(raw)))


def check_open_folder():
    name = "POST /api/open_folder"
    code, raw, _ = http("POST", "/api/open_folder", {})
    if 200 <= code < 300:
        return ok(name, "HTTP %s body=%s" % (code, head(raw, 80)))
    if 400 <= code < 500:
        return ok(name, "HTTP %s（无窗口环境可接受）body=%s" % (code, head(raw, 80)))
    return bad(name, "HTTP %s body=%s" % (code, head(raw)))


def check_static():
    name = "静态资源"
    problems = []
    code, raw, ct = http("GET", "/")
    if code != 200:
        problems.append("/ → HTTP %s" % code)
    elif "text/html" not in ct.lower():
        problems.append("/ Content-Type=%r" % ct)
    elif b'id="view3d"' not in raw and b"id='view3d'" not in raw:
        problems.append("/ 返回体里没有 id=view3d（%d 字节）" % len(raw))
    html_bytes = len(raw) if code == 200 else 0

    code2, raw2, ct2 = http("GET", "/vendor/three.module.js")
    if code2 != 200:
        problems.append("/vendor/three.module.js → HTTP %s" % code2)
    elif "javascript" not in ct2.lower():
        problems.append("/vendor/three.module.js Content-Type=%r（应 text/javascript）" % ct2)
    three_bytes = len(raw2) if code2 == 200 else 0

    code3, raw3, ct3 = http("GET", "/app.js")
    if code3 != 200:
        problems.append("/app.js → HTTP %s" % code3)
    elif "javascript" not in ct3.lower():
        problems.append("/app.js Content-Type=%r（应 text/javascript）" % ct3)

    code4, raw4, ct4 = http("GET", "/styles.css")
    if code4 != 200:
        problems.append("/styles.css → HTTP %s" % code4)
    elif "text/css" not in ct4.lower():
        problems.append("/styles.css Content-Type=%r" % ct4)

    if problems:
        return bad(name, "；".join(problems))
    return ok(name, "index.html %d B(text/html) / three.module.js %d B(%s) / app.js %d B(%s) / styles.css %d B(%s)"
              % (html_bytes, three_bytes, ct2.split(";")[0], len(raw3), ct3.split(";")[0],
                 len(raw4), ct4.split(";")[0]))


def check_traversal():
    name = "目录穿越 /api/view?local=../server.py"
    probes = ["/api/view?local=../server.py",
              "/api/view?local=" + urllib.parse.quote("../server.py", safe=""),
              "/api/view?local=..%2F..%2Fserver.py"]
    got = []
    for p in probes:
        code, raw, _ = http("GET", p)
        got.append((p, code, len(raw)))
        if code in (200, 206):
            leaked = b"ThreadingHTTPServer" in raw or b"import " in raw and b"def " in raw
            return bad(name, "HTTP 200 放行了穿越：%s（%d 字节，疑似泄露源码=%s）"
                       % (p, len(raw), leaked))
    codes = sorted(set(code for _, code, _ in got))
    if all(c in (400, 403) or c in (301, 302) for c in codes):
        return ok(name, " ".join("%s→%s" % (p.split("local=")[1], c) for p, c, _ in got))
    return bad(name, "返回码 %s（要求 400 或 403）：%s" % (codes, got))


# ---------------------------------------------------------- 真实出图 ---
def comfy_alive():
    for probe in ("/system_stats", "/queue", "/object_info"):
        try:
            with urllib.request.urlopen(COMFY + probe, timeout=3) as r:
                if r.status == 200:
                    return probe
        except Exception:
            continue
    return None


def pick_checkpoint(models):
    ck = [c for c in (models.get("checkpoints") or []) if isinstance(c, str) and c]
    if ck:
        return ck[0], "/api/comfy/models"
    if os.path.isdir(CKPT_DIR):
        cand = sorted(f for f in os.listdir(CKPT_DIR) if f.lower().endswith(".safetensors"))
        if cand:
            return cand[0], CKPT_DIR
    return None, None


def generated_abs_path(url):
    try:
        query = urllib.parse.urlparse(url).query
        local = urllib.parse.parse_qs(query).get("local", [""])[0]
        if not local:
            return None
        return os.path.normpath(os.path.join(OUT_DIR, local.replace("/", os.sep)))
    except Exception:
        return None


def check_generate():
    name = "真实出图（640x640 / steps=12）"
    probe = comfy_alive()
    if not probe:
        return skip(name, "ComfyUI %s 不可达（探测 /system_stats /queue /object_info 均失败）" % COMFY)
    models = META.get("_models") or {}
    ckpt, src = pick_checkpoint(models)
    if not ckpt:
        return bad(name, "ComfyUI 在线但拿不到 checkpoint（/api/comfy/models 空，%s 里也没有 .safetensors）" % CKPT_DIR)
    body = dict(CAMERA_DEFAULT)
    body.update({
        "pos_x": 0.35, "pos_y": -0.2, "pos_z": 0.15, "roll": 0.0,
        "seed": 20251117, "randomize": False, "steps": 12, "cfg": 5.0,
        "width": 640, "height": 640, "sampler": "euler", "scheduler": "normal",
        "checkpoint": ckpt, "lora_name": "", "lora_strength": 0.8,
        "batch": 1, "save_prefix": "qa_e2e",
    })
    say("       ComfyUI 在线（%s）；checkpoint=%s（来自 %s）" % (probe, ckpt, src))
    t0 = time.time()
    code, raw, _ = http("POST", "/api/generate", body, timeout=90)
    if code != 200:
        return bad(name, "POST /api/generate → HTTP %s body=%s" % (code, head(raw)))
    d = as_json(raw) or {}
    pid = d.get("prompt_id")
    if not pid:
        return bad(name, "响应无 prompt_id：%s" % head(raw))
    say("       prompt_id=%s seed_used=%s" % (pid, d.get("seed_used")))

    deadline = time.time() + 300.0
    last_note = 0.0
    state, prog = None, None
    while time.time() < deadline:
        code, praw, _ = http("GET", q("/api/progress", prompt_id=pid), timeout=20)
        pd = as_json(praw) or {}
        if code != 200:
            return bad(name, "GET /api/progress → HTTP %s body=%s" % (code, head(praw)))
        state = pd.get("state")
        prog = pd.get("progress")
        now = time.time()
        if now - last_note > 12:
            say("       [%5.1fs] state=%s progress=%s node=%s queue_pos=%s"
                % (now - t0, state, prog, pd.get("node"), pd.get("queue_pos")))
            last_note = now
        if state == "done":
            break
        if state == "error":
            return bad(name, "progress=error：%s（%.1fs）" % (pd.get("error"), time.time() - t0))
        time.sleep(1.0)
    else:
        return bad(name, "轮询 300s 未到 done（最后 state=%s progress=%s）" % (state, prog))

    pd = as_json(http("GET", q("/api/progress", prompt_id=pid))[1]) or {}
    images = pd.get("images") or []
    if not images:
        return bad(name, "done 但 images 为空：%s" % head(json.dumps(pd, ensure_ascii=False), 200))
    problems = []
    detail = []
    for img in images:
        if not isinstance(img, dict):
            problems.append("条目非对象 %r" % (img,))
            continue
        url = str(img.get("url") or "")
        if not url.startswith("/api/view?local="):
            problems.append("url 不是 /api/view?local=...：%r" % (url,))
            continue
        code, png, ct = http("GET", url, timeout=60)
        if code != 200:
            problems.append("GET %s → HTTP %s" % (url, code))
            continue
        abs_path = generated_abs_path(url)
        on_disk = os.path.isfile(abs_path) if abs_path else False
        if len(png) <= 20 * 1024:
            problems.append("%s 只有 %d 字节（要求 >20KB）" % (url, len(png)))
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            problems.append("%s PNG 魔数不对（前 8 字节 %r）" % (url, png[:8]))
        if not on_disk:
            problems.append("落盘路径不存在：%s" % abs_path)
        GENERATED.append((abs_path, len(png), on_disk))
        detail.append("%s | %d B | 落盘=%s | %s"
                      % (abs_path, len(png), on_disk, (ct.split(";")[0] or "?")))
    if problems:
        return bad(name, "；".join(problems))
    say("       出图：")
    for abs_path, nbytes, on_disk in GENERATED:
        say("         %s  (%d 字节, on_disk=%s)" % (abs_path, nbytes, on_disk))
    return ok(name, "完成耗时 %.1fs；图 %d 张；PNG 魔数/字节数/落盘全部正确；首图 %d 字节"
              % (time.time() - t0, len(images), GENERATED[0][1] if GENERATED else 0))


# ---------------------------------------------------------------- 主流程 ---
def main():
    global PORT, BASE
    ap = argparse.ArgumentParser(description="SDXL 相机工作台端到端契约测试")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-generate", action="store_true")
    ap.add_argument("--keep-server", action="store_true")
    args = ap.parse_args()

    say("=" * 78)
    say("SDXL 相机工作台 · e2e 契约测试")
    say("python   : %s" % sys.executable)
    say("studio   : %s" % STUDIO_DIR)
    say("=" * 78)

    if not os.path.isfile(SERVER_PY):
        say("[SKIP] studio/server.py 不存在（backend 还没交付）：%s" % SERVER_PY)
        say("[SKIP] web/ 存在=%s" % os.path.isdir(WEB_DIR))
        return 3
    if not os.path.isfile(PY):
        say("[FAIL] 解释器不存在：%s" % PY)
        return 1

    PORT = args.port or pick_port()
    if not PORT:
        say("[FAIL] 8800-8899 没找到空闲端口")
        return 1
    BASE = "http://127.0.0.1:%d" % PORT
    say("端口     : %d   (%s)" % (PORT, BASE))

    proc = lf = None
    logpath = None
    try:
        proc, lf, logpath = start_server(PORT)
        alive, info = wait_health(proc)
        if not alive:
            say("[FAIL] /api/health 未就绪：%s" % info)
            say("---- 服务日志尾部 ----")
            say(tail_file(logpath))
            return 1
        say("[PASS] 服务已就绪：%s" % json.dumps(info, ensure_ascii=False)[:150])
        say("-" * 78)

        check_health()
        check_meta()
        check_preview()
        check_models()
        check_config()
        check_ui_presets()
        check_gallery()
        check_gallery_clear()
        check_interrupt()
        check_open_folder()
        check_static()
        check_traversal()
        if args.no_generate:
            skip("真实出图", "--no-generate")
        else:
            check_generate()
            if GENERATED:
                check_gallery(expect_filename=os.path.basename(GENERATED[0][0] or ""))
    finally:
        if proc is not None and not args.keep_server:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    say("服务未在 6s 内退出，kill")
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            except Exception as e:
                say("结束服务出错：%s" % e)
        elif proc is not None:
            say("--keep-server：服务仍在 %s（pid=%s）" % (BASE, proc.pid))
        if lf is not None:
            try:
                lf.close()
            except Exception:
                pass

    say("")
    say("=" * 78)
    p = sum(1 for r in RESULTS if r[0] == "PASS")
    f = sum(1 for r in RESULTS if r[0] == "FAIL")
    s = sum(1 for r in RESULTS if r[0] == "SKIP")
    say("汇总：PASS %d / FAIL %d / SKIP %d   （共 %d 项）" % (p, f, s, len(RESULTS)))
    if GENERATED:
        say("真实出图：")
        for abs_path, nbytes, on_disk in GENERATED:
            say("  %s  (%d 字节, on_disk=%s)" % (abs_path, nbytes, on_disk))
    if f:
        say("失败明细：")
        for status, cname, cdetail in RESULTS:
            if status == "FAIL":
                say("  - %s：%s" % (cname, cdetail))
    if s:
        say("跳过：")
        for status, cname, cdetail in RESULTS:
            if status == "SKIP":
                say("  - %s：%s" % (cname, cdetail))
    say("=" * 78)
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
