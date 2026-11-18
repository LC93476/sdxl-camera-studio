#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_ui.py — SDXL 相机工作台前端静态契约检查（只依赖标准库）

检查项：
  1. index.html 的 id 集合 ⊇ CONTRACT.md「必须存在的 DOM id」清单；
  2. app.js / camera3d.js 里 getElementById('x')、querySelector('#x')、querySelectorAll('#x')
     引用的 x 必须出现在 index.html 的 id 里；
  3. app.js 里所有 fetch('/api/...') 路径都在契约 HTTP API 清单里（允许 ?query 与模板拼接）；
  4. index.html 有 importmap 指向 /js/three.module.js，且该文件存在且 > 1MB（v2 复用仓库根）；
  5. app.js / camera3d.js 不出现 OrbitControls、不出现 CDN 外链（允许 import 'three'），
     并做一次括号配平（跳过字符串与注释）检查。

用法：
    D:/QUARK/ComfyUI-aki-v3.2/python/python.exe D:/QUARK/sdxl_camera_angle/studio/tools/check_ui.py
退出码：FAIL>0 → 1，否则 0。
"""
from __future__ import print_function

import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
STUDIO_DIR = os.path.dirname(TOOLS_DIR)
WEB_DIR = os.path.join(STUDIO_DIR, "web")
CONTRACT = os.path.join(STUDIO_DIR, "CONTRACT.md")
INDEX = os.path.join(WEB_DIR, "index.html")
APP_JS = os.path.join(WEB_DIR, "app.js")
CAM_JS = os.path.join(WEB_DIR, "camera3d.js")
ROOT = os.path.dirname(STUDIO_DIR)                       # 仓库根
THREE = os.path.join(ROOT, "js", "three.module.js")     # v2：复用仓库根的三件套
CAMSHARED = os.path.join(ROOT, "js", "camera_shared.js")
STYLES = os.path.join(WEB_DIR, "styles.css")

RESULTS = []


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


def head(s, n=160):
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


# ------------------------------------------------- JS 词法小工具 ---
def strip_js_comments(src):
    """去掉 // 与 /* */ 注释，保留字符串内容（选择器/fetch 都在字符串里）。"""
    out = []
    i, n = 0, len(src)
    state = None  # None / 单引号 / 双引号 / 反引号 / 行注释 / 块注释
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state in ("'", '\"', "`"):
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if ch == state:
                state = None
            i += 1
            continue
        if state == "//":
            if ch == "\n":
                state = None
                out.append(ch)
            i += 1
            continue
        if state == "/*":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
                continue
            if ch == "\n":
                out.append("\n")
            i += 1
            continue
        if ch in "'\"`":
            state = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            state = "//"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "/*"
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def bracket_problem(src):
    """简单的括号配平检查（跳过字符串与注释）。返回 None 或问题描述。"""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    i, n = 0, len(src)
    line = 1
    state = None
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
        if state in ("'", '\"', "`"):
            if ch == "\\":
                i += 2
                continue
            if ch == state:
                state = None
            i += 1
            continue
        if state == "//":
            if ch == "\n":
                state = None
            i += 1
            continue
        if state == "/*":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
                continue
            i += 1
            continue
        if ch in "'\"`":
            state = ch
            i += 1
            continue
        if ch == "/" and nxt == "/":
            state = "//"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "/*"
            i += 2
            continue
        if ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                return "第 %d 行多余的 %r" % (line, ch)
            op, oline = stack.pop()
            if op != pairs[ch]:
                return "第 %d 行 %r 与第 %d 行 %r 不匹配" % (line, ch, oline, op)
        i += 1
    if stack:
        op, oline = stack[-1]
        return "第 %d 行起的 %r 未闭合（共 %d 个未闭合）" % (oline, op, len(stack))
    return None


# ------------------------------------------------------- 契约解析 ---
def contract_text():
    if not os.path.isfile(CONTRACT):
        return None
    return read_text(CONTRACT)


def contract_dom_ids(text):
    m = re.search(r"必须存在的 DOM id[^\n：:]*[：:](.*?)(?=\n\s*-\s)", text, re.S)
    if not m:
        return None
    chunk = m.group(1)
    ids = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", chunk):
        if tok not in ids:
            ids.append(tok)
    return ids


def contract_api_set(text):
    """契约里所有 /api/... 路径（v2 起扫全文，不限 ## HTTP API 段）。

    CONTRACT.md 的「v2 增补」段新增了 /api/sequences、/api/ui_presets/* 等，
    只扫首段会漏，前端一写就误报「契约外路径」。
    """
    if not re.search(r"##\s*HTTP API", text):
        return None
    paths = set(re.findall(r"/api/[A-Za-z0-9_/\-]+", text))
    return paths or None


# ------------------------------------------------------------ 检查 ---
def check_files():
    name = "文件齐备"
    need = [(INDEX, "index.html"), (APP_JS, "app.js"), (CAM_JS, "camera3d.js"),
            (STYLES, "styles.css"), (THREE, "js/three.module.js"),
            (CAMSHARED, "js/camera_shared.js")]
    missing = [label + "(" + p + ")" for p, label in need if not os.path.isfile(p)]
    if missing:
        return bad(name, "缺 " + "、".join(missing)), {}
    sizes = {}
    for p, label in need:
        sizes[label] = os.path.getsize(p)
    return ok(name, "、".join("%s %d B" % (k, v) for k, v in sizes.items())), sizes


def check_dom_ids(ids_in_html, contract_ids):
    name = "index.html ⊇ 契约 DOM id"
    missing = [i for i in contract_ids if i not in ids_in_html]
    if missing:
        return bad(name, "缺 %d 个 id：%s（index.html 共 %d 个 id）"
                   % (len(missing), missing, len(ids_in_html)))
    extra = sorted(ids_in_html - set(contract_ids))
    return ok(name, "契约 %d 个 id 全部存在；index.html 共 %d 个 id%s"
              % (len(contract_ids), len(ids_in_html),
                 ("，额外：" + ",".join(extra[:10]) + ("…" if len(extra) > 10 else "")) if extra else ""))


def js_refs(src):
    """→ (selectors:list[(line, kind, ident)], fetches:list[(line, path)])"""
    code = strip_js_comments(src)
    sels = []
    for m in re.finditer("getElementById\\(\\s*['\"]([^'\"]+)['\"]", code):
        sels.append((code.count("\n", 0, m.start()) + 1, "getElementById", m.group(1)))
    for m in re.finditer("querySelector(?:All)?\\(\\s*['\"]#([A-Za-z_][A-Za-z0-9_-]*)", code):
        sels.append((code.count("\n", 0, m.start()) + 1, "querySelector", m.group(1)))
    # 前端常写 const $ = (id) => document.getElementById(id)，再 $('pos_x')。
    # 这种间接写法也要算进 id 引用，否则 3D 工作台的 id 引用会漏检成 0 处。
    helpers = set(re.findall(
        r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\([^)]{0,40}\)|[A-Za-z_$][\w$]*)\s*=>\s*(?:document\.)?getElementById""",
        code))
    helpers.update(re.findall(
        r"""function\s+([A-Za-z_$][\w$]*)\s*\([^)]{0,40}\)\s*\{\s*return\s+(?:document\.)?getElementById""",
        code))
    for name in sorted(helpers):
        pat = re.escape(name) + r"""\(\s*['"]([#A-Za-z_][^'"]{0,60})['"]\s*\)"""
        for m in re.finditer(pat, code):
            raw = m.group(1).lstrip("#")
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", raw):
                continue  # 类名 / 属性选择器，不属于 id 检查范围
            sels.append((code.count("\n", 0, m.start()) + 1,
                         name + "()", raw))
    fetches = []
    for m in re.finditer("fetch\\(\\s*['\"`](/api/[^'\"`\\s)]*)", code):
        fetches.append((code.count("\n", 0, m.start()) + 1, m.group(1)))
    return sels, fetches


def normalize_api(path):
    p = path.split("#")[0].split("?")[0].strip()
    if p.endswith("/") and len(p) > 1:
        p = p.rstrip("/")
    return p


def check_js_selectors(files, ids_in_html):
    name = "app.js/camera3d.js 选择器都能在 index.html 里找到"
    missing = []
    total = 0
    for path in files:
        if not os.path.isfile(path):
            continue
        src = read_text(path)
        sels, _ = js_refs(src)
        total += len(sels)
        for line, kind, ident in sels:
            if ident not in ids_in_html:
                missing.append("%s:%d %s('%s')" % (os.path.basename(path), line, kind, ident))
    if missing:
        return bad(name, "引用不存在的 id %d 处：%s" % (len(missing), missing[:20]))
    return ok(name, "共 %d 处引用，全部命中 index.html 的 id" % total)


API_LITERAL_RE = re.compile(r'''["'\x60](/api/[^"'\x60\s]*)''')
API_FETCH_RE = re.compile(r'''fetch\(\s*["'\x60](/api/[^"'\x60\s)]*)''')


def collect_api_paths(src):
    """→ [(line, raw, via)]，via 是 fetch（fetch('/api/...') 字面量）
    或 literal（API 常量表里的 '/api/xxx'，再经 api(path) 包装调用）。
    只看真实代码：注释先剥掉（注释里的 /api/preview 之类不算调用）。"""
    code = strip_js_comments(src)
    hits = []
    for m in API_FETCH_RE.finditer(code):
        hits.append((code.count("\n", 0, m.start()) + 1, m.group(1), "fetch"))
    for m in API_LITERAL_RE.finditer(code):
        hits.append((code.count("\n", 0, m.start()) + 1, m.group(1), "literal"))
    return hits


def check_fetch_paths(js_files, apis):
    name = "app.js 的 /api 路径 ⊂ 契约 API"
    unknown, seen = [], []
    n_fetch = n_lit = 0
    for path in js_files:
        if not os.path.isfile(path):
            continue
        for line, raw, via in collect_api_paths(read_text(path)):
            if via == "fetch":
                n_fetch += 1
            else:
                n_lit += 1
            norm = normalize_api(raw)
            seen.append(norm)
            if norm not in apis:
                unknown.append("%s:%d %s('%s') → %s"
                               % (os.path.basename(path), line, via, raw, norm))
    if n_fetch + n_lit == 0:
        return bad(name, "一个 /api 路径都没扫到（前端可能没接 API）")
    if unknown:
        return bad(name, "契约外路径 %d 处：%s" % (len(unknown), unknown[:12]))
    uniq = sorted(set(seen))
    missing = sorted(a for a in apis if a not in uniq)
    detail = "%d 处 fetch 字面量 + %d 处 API 常量路径 → 去重 %d 条：%s" % (
        n_fetch, n_lit, len(uniq), ", ".join(uniq))
    if missing:
        detail += "；未直接出现（可能由画廊 url 间接使用）：" + ", ".join(missing)
    return ok(name, detail)


def check_importmap(html):
    name = "importmap → /js/three.module.js（>1MB）"
    m = re.search(r"<script[^>]*type\s*=\s*['\"]importmap['\"][^>]*>(.*?)</script>", html, re.S | re.I)
    if not m:
        return bad(name, "index.html 里没有 script type=importmap")
    try:
        spec = json.loads(m.group(1).strip())
    except Exception as e:
        return bad(name, "importmap JSON 解析失败：%s；原文 %s" % (e, head(m.group(1), 120)))
    imports = spec.get("imports") or {}
    if not isinstance(imports, dict):
        return bad(name, "importmap.imports 不是对象：%r" % (imports,))
    three = imports.get("three")
    if not three:
        return bad(name, "importmap 没有 three 映射 → 实际 %s" % list(imports.keys()))
    if not str(three).endswith("/js/three.module.js"):
        return bad(name, "three 指向 %r（应指向 /js/three.module.js）" % (three,))
    if not os.path.isfile(THREE):
        return bad(name, "js/three.module.js 不存在：%s" % THREE)
    size = os.path.getsize(THREE)
    if size <= 1024 * 1024:
        return bad(name, "js/three.module.js 只有 %d 字节（要求 >1MB）" % size)
    others = [k for k in imports if k != "three"]
    return ok(name, "three → %s，文件 %d 字节%s"
              % (three, size, ("，其它映射：" + ",".join(others)) if others else ""))


CDN_HOST_RE = re.compile(
    r"(?:https?:)?//(?:cdn|unpkg|jsdelivr|cdnjs|esm\.sh|skypack|esm\.run|jspm|"
    r"fonts\.googleapis|fonts\.gstatic|code\.jquery|ajax\.googleapis|bootcdn|staticfile)"
    r"[^\s'\"`)<]*", re.I)
REMOTE_IMPORT_RE = re.compile(
    r"(?:from|import|fetch|src)\s*[=(]?\s*['\"`](https?://|//)[^'\"`]*", re.I)


def check_no_orbit_no_cdn(js_files):
    name = "无 OrbitControls / 无 CDN 外链"
    problems = []
    remote_hits = []
    for path in js_files:
        if not os.path.isfile(path):
            continue
        src = read_text(path)
        base = os.path.basename(path)
        # 注释里提到 OrbitControls 不算违规（前端常写"无 OrbitControls"说明），
        # 只看真正的代码：去掉注释后扫描，行号仍然对得上（注释保留换行）。
        code = strip_js_comments(src)
        low = code.lower()
        if "orbitcontrols" in low:
            line = low.count("\n", 0, low.index("orbitcontrols")) + 1
            problems.append("%s:%d 出现 OrbitControls（代码里不只是注释）" % (base, line))
        for m in CDN_HOST_RE.finditer(code):
            line = code.count("\n", 0, m.start()) + 1
            problems.append("%s:%d CDN 外链 %s" % (base, line, head(m.group(0), 60)))
        for m in REMOTE_IMPORT_RE.finditer(code):
            line = code.count("\n", 0, m.start()) + 1
            remote_hits.append("%s:%d %s" % (base, line, head(m.group(0), 80)))
    if remote_hits:
        problems.extend("远程 import/fetch/src → " + h for h in remote_hits[:8])
    if problems:
        return bad(name, "；".join(problems[:10]))
    return ok(name, "两个 js 都没有 OrbitControls，也没有 CDN/远程 import")


def check_brackets(files):
    name = "括号配平（js）"
    problems = []
    for path in files:
        if not os.path.isfile(path):
            continue
        prob = bracket_problem(read_text(path))
        if prob:
            problems.append("%s：%s" % (os.path.basename(path), prob))
    if problems:
        return bad(name, "；".join(problems))
    return ok(name, "app.js / camera3d.js 括号配平")


def check_three_imports(js_files):
    name = "只从本地模块 import（three / 相对路径）"
    detail = []
    for path in js_files:
        if not os.path.isfile(path):
            continue
        src = read_text(path)
        code = strip_js_comments(src)
        specs = re.findall(r"from\s*['\"]([^'\"]+)['\"]", code)
        specs += re.findall(r"import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", code)
        rel = [s for s in specs if s.startswith(".") or s.startswith("/") or s == "three"]
        other = [s for s in specs if s not in rel]
        detail.append("%s: %s" % (os.path.basename(path), sorted(set(specs)) or "无 import"))
        if other:
            return bad(name, "%s 有非本地模块 import：%s" % (os.path.basename(path), other))
    return ok(name, "；".join(detail))


def main():
    say("=" * 78)
    say("SDXL 相机工作台 · 前端静态契约检查")
    say("web : %s" % WEB_DIR)
    say("=" * 78)

    check_files()
    text = contract_text()
    contract_ids, apis = [], set()
    if text is None:
        bad("读 CONTRACT.md", "不存在：%s" % CONTRACT)
    else:
        contract_ids = contract_dom_ids(text) or []
        apis = contract_api_set(text) or set()
        if not contract_ids:
            bad("解析契约 DOM id", "没从 CONTRACT.md 里抓到「必须存在的 DOM id」清单")
        else:
            ok("解析契约 DOM id", "%d 个 id" % len(contract_ids))
        if not apis:
            bad("解析契约 API", "没从 CONTRACT.md 的 HTTP API 段抓到 /api/... 清单")
        else:
            ok("解析契约 API", "%d 条：%s" % (len(apis), ", ".join(sorted(apis))))

    html = read_text(INDEX) if os.path.isfile(INDEX) else ""
    ids_in_html = set()
    if not html:
        bad("读 index.html", "不存在或为空：%s" % INDEX)
    else:
        ids_in_html = set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", html))
        ok("index.html 解析", "%d 字节，%d 个 id" % (len(html), len(ids_in_html)))

    if contract_ids and html:
        check_dom_ids(ids_in_html, contract_ids)

    js_files = [APP_JS, CAM_JS]
    if any(os.path.isfile(p) for p in js_files):
        check_js_selectors(js_files, ids_in_html)
    if os.path.isfile(APP_JS) and apis:
        check_fetch_paths([APP_JS], apis)
    if html:
        check_importmap(html)
    check_no_orbit_no_cdn(js_files)
    check_brackets(js_files)
    if any(os.path.isfile(p) for p in js_files):
        check_three_imports(js_files)

    say("")
    say("=" * 78)
    p = sum(1 for r in RESULTS if r[0] == "PASS")
    f = sum(1 for r in RESULTS if r[0] == "FAIL")
    say("汇总：PASS %d / FAIL %d   （共 %d 项）" % (p, f, len(RESULTS)))
    if f:
        say("失败明细：")
        for status, cname, cdetail in RESULTS:
            if status == "FAIL":
                say("  - %s：%s" % (cname, cdetail))
    say("=" * 78)
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
