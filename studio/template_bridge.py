# -*- coding: utf-8 -*-
"""外部工作流模板（ComfyUI API 格式 JSON）接入层。

工作台默认自己拼一张最小图（checkpoint → lora → 两个 CLIPTextEncode → KSampler → 存图）。
想把相机提示词塞进「已经调好的生产工作流」（绯月三步 detailer、SAM3 部位重绘、姿势库标签……）
就用这里：读模板 JSON，只改几个输入点，其余原样提交。

模板来源：任何 ComfyUI「Export (API)」出来的 JSON。工作台目录默认扫描
~/.dsh-comfyui-workbench/workflows/*.json（同名 .schema.json 里的 vars 会被用来
判断「正向提示词该写进哪个节点」等）。

注入规则（保守，不动结构）：
  正向 → vars.prompt 绑定的节点（通常是主 CLIPTextEncode）的 text，整体替换；
  反向 → 模板里 KSampler.negative 指向的节点的 text，**保留作者原文并在末尾追加**
        工作台的正向否定词（可用 negative_mode="replace" 改成覆盖）；
  尺寸 / 种子 / 步数 / CFG → vars 里绑定到 EmptyLatentImage / KSampler 的节点；
  其它 vars（比如 pose）由 gen 里的同名键覆盖，没有就用模板默认值。
"""
import json
import os
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIRS = [
    os.path.expanduser(r"~\.dsh-comfyui-workbench\workflows"),
    os.path.join(os.path.dirname(HERE), "workflows"),
]


class TemplateError(Exception):
    pass


# ══════════════════════ 发现与加载 ══════════════════════
def template_dirs():
    return [d for d in TEMPLATE_DIRS if d and os.path.isdir(d)]


def list_templates():
    out = []
    for d in template_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json") or name.endswith(".schema.json"):
                continue
            path = os.path.join(d, name)
            tid = name[:-5]
            item = {"id": tid, "name": tid, "path": path, "dir": d,
                    "title": tid, "description": "", "vars": [], "valid": False, "error": ""}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not data:
                    raise TemplateError("不是 API 格式的对象")
                first = next(iter(data.values()))
                if not isinstance(first, dict) or "class_type" not in first:
                    raise TemplateError("不是 API 格式（可能是 UI 工程文件）")
                item["valid"] = True
                item["nodes"] = len(data)
                schema = _load_schema(path)
                if schema:
                    item["title"] = schema.get("title") or tid
                    item["description"] = schema.get("description") or ""
                    item["vars"] = [v.get("name") for v in schema.get("vars") or []]
            except Exception as e:  # 坏模板不该让整个列表崩
                item["error"] = str(e)
            out.append(item)
    return out


def _load_schema(path):
    sp = path[:-5] + ".schema.json"
    if not os.path.isfile(sp):
        return {}
    try:
        with open(sp, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve(ref):
    """ref 可以是模板 id、绝对路径、或 http(s) 什么都行——只接受前两种。"""
    ref = str(ref or "").strip()
    if not ref:
        raise TemplateError("模板名为空")
    if os.path.isfile(ref):
        return ref
    for d in template_dirs():
        p = os.path.join(d, ref if ref.endswith(".json") else ref + ".json")
        if os.path.isfile(p):
            return p
    raise TemplateError("找不到工作流模板: %s" % ref)


def load(ref):
    path = resolve(ref)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise TemplateError("模板读取失败: %s" % e)
    if not isinstance(data, dict) or not data:
        raise TemplateError("模板不是 API 格式的节点对象: %s" % path)
    return path, data, _load_schema(path)


# ══════════════════════ 注入 ══════════════════════
def _nodes_by_class(prompt, cls):
    return [nid for nid, n in prompt.items()
            if isinstance(n, dict) and n.get("class_type") == cls]


def _find_sampler(prompt):
    ids = _nodes_by_class(prompt, "KSampler") or _nodes_by_class(prompt, "KSamplerAdvanced")
    if not ids:
        raise TemplateError("模板里没有 KSampler")
    return ids[0]


def _link_node(prompt, link):
    """["12", 0] → ("12", 0)；不是链接就返回 (None, None)。"""
    if isinstance(link, list) and len(link) == 2:
        return str(link[0]), link[1]
    return None, None


_CLASS_DEFAULT_INPUT = {
    "CLIPTextEncode": "text",
    "CLIPTextEncodeSDXL": "text",
    "CLIPTextEncodeFlux": "text",
    "EmptyLatentImage": "width",
    "EmptySD3LatentImage": "width",
    "KSampler": "seed",
    "KSamplerAdvanced": "seed",
}


def _guess_input(prompt, node_id, var_name):
    """schema 里只写了 node 没写 input 时，按节点 class 与已有输入推断该写哪个字段。"""
    node = prompt.get(node_id) or {}
    inputs = node.get("inputs") or {}
    if var_name in inputs:
        return var_name
    guess = _CLASS_DEFAULT_INPUT.get(str(node.get("class_type") or ""))
    if guess and guess in inputs:
        return guess
    return None


def _var_targets(schema, prompt=None):
    """vars 名 → [(node_id, input_name), ...]（input 缺省时按节点类型推断）"""
    out = {}
    for v in (schema.get("vars") or []):
        name = v.get("name")
        if not name:
            continue
        targets = []
        if v.get("node"):
            nid = str(v["node"])
            inp = v.get("input") or (_guess_input(prompt, nid, name) if prompt is not None else None)
            if inp:
                targets.append((nid, inp))
        for b in (v.get("bindings") or []):
            if b.get("node") and b.get("input"):
                targets.append((str(b["node"]), b["input"]))
        if targets:
            out[name] = targets
    return out


def _set_text(prompt, node_id, text):
    n = prompt.get(node_id)
    if not n:
        return False
    n.setdefault("inputs", {})["text"] = text
    return True


def build(ref, gen, positive, negative):
    """返回可直接 POST /prompt 的节点字典。gen 是 /api/generate 那套参数。"""
    path, prompt, schema = load(ref)
    prompt = json.loads(json.dumps(prompt))  # 深拷贝，别改缓存
    targets = _var_targets(schema, prompt)
    warn = []

    sampler = _find_sampler(prompt)
    pos_link, _ = _link_node(prompt, prompt[sampler]["inputs"].get("positive"))
    neg_link, _ = _link_node(prompt, prompt[sampler]["inputs"].get("negative"))

    # ---- 正向：优先用 vars.prompt 绑定，退化到 KSampler.positive 指向的 CLIPTextEncode
    pos_set = False
    for nid, inp in targets.get("prompt", []):
        if _set_text(prompt, nid, positive or ""):
            pos_set = True
    if not pos_set and pos_link and prompt.get(pos_link, {}).get("class_type") == "CLIPTextEncode":
        pos_set = _set_text(prompt, pos_link, positive or "")
        warn.append("模板没有 prompt 变量，已写入 KSampler 的正向节点 %s" % pos_link)

    # ---- 反向：默认保留作者原文，追加工作台否定
    mode = str(gen.get("negative_mode") or "append").lower()
    if neg_link:
        cur = str(prompt.get(neg_link, {}).get("inputs", {}).get("text") or "")
        if mode == "replace":
            new = negative or ""
        elif negative:
            new = (cur.rstrip().rstrip(",") + ", " + negative).strip(", ")
        else:
            new = cur
        _set_text(prompt, neg_link, new)

    # ---- 尺寸 / 种子 / 步数 / CFG / 其它变量
    def apply_var(name, value):
        for nid, inp in targets.get(name, []):
            node = prompt.get(nid)
            if not node:
                continue
            node.setdefault("inputs", {})[inp] = value

    if gen.get("width"):
        apply_var("width", int(gen["width"]))
    if gen.get("height"):
        apply_var("height", int(gen["height"]))
    if gen.get("seed") is not None and int(gen.get("seed") or 0) >= 0:
        apply_var("seed", int(gen["seed"]))
    if gen.get("steps"):
        apply_var("steps", int(gen["steps"]))
    if gen.get("cfg"):
        apply_var("cfg", round(float(gen["cfg"]), 2))

    # 模板自定义变量（pose 之类）：gen["template_vars"] 里给就用，否则沿用模板默认
    extra = gen.get("template_vars") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            apply_var(k, v)

    # 存图前缀带上工作台前缀，方便在 ComfyUI 输出目录里认出来
    prefix = str(gen.get("save_prefix") or "").strip()
    if prefix:
        for nid in _nodes_by_class(prompt, "SaveImage"):
            cur = str(prompt[nid]["inputs"].get("filename_prefix") or "")
            if cur and not cur.startswith(prefix + "/"):
                prompt[nid]["inputs"]["filename_prefix"] = urllib.parse.quote(prefix, safe="") + "/" + cur

    return {"prompt": prompt, "template": os.path.basename(path),
            "template_path": path, "warn": warn,
            "positive_node": pos_link, "negative_node": neg_link, "sampler": sampler}
