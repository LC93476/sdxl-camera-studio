# -*- coding: utf-8 -*-
"""外部工作流模板（ComfyUI API 格式 JSON）接入层。

工作台默认自己拼一张最小图（checkpoint → lora → 两个 CLIPTextEncode → KSampler → 存图）。
想把相机提示词塞进「已经调好的生产工作流」（绯月三步 detailer、SAM3 部位重绘、姿势库标签……）
就用这里：读模板 JSON，只改几个输入点，其余原样提交。

模板来源：任何 ComfyUI「Export (API)」出来的 JSON。工作台目录默认扫描
~/.dsh-comfyui-workbench/workflows/*.json（同名 .schema.json 里的 vars 会被用来
判断「正向提示词该写进哪个节点」等）；工作流目录里的 UI 工程文件（nodes/links）会被明确拒绝。

注入规则（保守，只改 inputs，不动结构）：
  正向 → vars.prompt 绑定的节点，否则沿着 KSampler.positive 一路回溯（会穿过
         ControlNetApply* / CameraAngleSDXL / 各种 conditioning 透传节点）到 CLIPTextEncode*.text；
  反向 → 同上（KSampler.negative 链），**保留作者原文并在末尾追加**工作台的否定词
         （可用 negative_mode="replace" 改成覆盖）；
  尺寸 / 种子 / 步数 / CFG → schema 变量优先，没有 schema 就按节点类型推断（latent / sampler）；
  其它 vars（比如 pose）由 gen["template_vars"] 里的同名键覆盖，没有就用模板默认值；
  ControlNet → gen["template_vars"]["controlnet_depth" | "controlnet_pose"] 是「已经上传到
         ComfyUI input/ 的文件名」，写进模板里 ControlNetApply* 的 image 输入：原本连着
         LoadImage 就只换图（不加节点），原本连着别的节点就插一个 LoadImage 并改链；
         模板里没有 ControlNet 槽位时只 warn，不改结构。
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


# ══════════════════════ 节点分类（class_type 大小写不敏感，按前缀归类） ══════════════════════
SAMPLER_PREFIXES = ("ksampler", "samplerselect", "samplercustom")
TEXT_PREFIXES = ("cliptextencode", "t5textencode", "smztextencode", "textencode")
CONTROLNET_APPLY_PREFIXES = ("controlnetapply",)
CAMERA_CLASSES = (
    "cameraanglesdxl", "cameraangle", "sdxlcameraangle", "cameraanglesdxladvanced",
)
LATENT_CLASSES = (
    "emptylatentimage", "emptylatentimagesdxl", "emptysd3latentimage",
    "emptylatentimageflux", "emptylatentimagevideo",
)
SAVE_PREFIXES = ("saveimage", "saveanimated", "previewimage")
LOADIMAGE_PREFIXES = ("loadimage",)
# 回溯正向/负向文本时会穿过的输入名（不跟 model / clip / vae / latent 这类线）
PASSTHROUGH_FIELDS = (
    "positive", "negative", "conditioning", "cond",
    "conditioning_1", "conditioning_2", "conditioning_to", "conditioning_from",
)


def _cls(node):
    return str((node or {}).get("class_type") or "").strip()


def _cls_low(node):
    return _cls(node).lower()


def _match(node, prefixes=(), classes=()):
    low = _cls_low(node)
    if not low:
        return False
    if low in classes:
        return True
    for pref in prefixes:
        if low.startswith(pref):
            return True
    return False


def _is_sampler(node):
    return _match(node, prefixes=SAMPLER_PREFIXES)


def _is_text(node):
    return _match(node, prefixes=TEXT_PREFIXES)


def _is_controlnet_apply(node):
    return _match(node, prefixes=CONTROLNET_APPLY_PREFIXES)


def _is_camera(node):
    return _match(node, classes=CAMERA_CLASSES)


def _is_save(node):
    return _match(node, prefixes=SAVE_PREFIXES)


def _is_loadimage(node):
    return _match(node, prefixes=LOADIMAGE_PREFIXES)


def _is_latent(node):
    return _match(node, classes=LATENT_CLASSES)


def _id_key(node_id):
    text = str(node_id)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _nodes_where(prompt, pred):
    """按谓词取节点 id（按 id 自然序排序，保证「第一个 ControlNet」是稳定的）。"""
    ids = []
    for nid, node in (prompt or {}).items():
        if isinstance(node, dict) and pred(node):
            ids.append(str(nid))
    ids.sort(key=_id_key)
    return ids


def _nodes_by_class(prompt, cls):
    """精确匹配 class_type（大小写不敏感）；给 `前缀*` 时按前缀匹配。"""
    want = str(cls or "").strip().lower()
    if not want:
        return []
    ids = []
    for nid, node in (prompt or {}).items():
        if not isinstance(node, dict):
            continue
        got = _cls_low(node)
        if got == want or (want.endswith("*") and got.startswith(want[:-1])):
            ids.append(str(nid))
    ids.sort(key=_id_key)
    return ids


# ══════════════════════ 发现与加载 ══════════════════════
def template_dirs():
    return [d for d in TEMPLATE_DIRS if d and os.path.isdir(d)]


def _ui_format_hint(data):
    """litegraph 的 UI 工程文件（nodes/links/last_node_id）不是 API 格式。"""
    if not isinstance(data, dict):
        return False
    if "nodes" not in data or "links" not in data:
        return False
    sample = data.get("nodes") or []
    for node in sample:
        if isinstance(node, dict) and "type" in node and "inputs" in node:
            return True
    return True


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
                if _ui_format_hint(data):
                    raise TemplateError("不是 API 格式（这是 UI 工程文件，请 Export (API) 后重存）")
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
    if _ui_format_hint(data):
        raise TemplateError("模板是 ComfyUI UI 格式（nodes/links），不是 API 格式："
                            "请在 ComfyUI 里 Workflow → Export (API) 重新导出")
    if not isinstance(data, dict) or not data:
        raise TemplateError("模板不是 API 格式的节点对象: %s" % path)
    return path, data, _load_schema(path)


# ══════════════════════ 嗅探 ══════════════════════
def _find_sampler(prompt, required=True):
    ids = _nodes_where(prompt, _is_sampler)
    if not ids:
        if required:
            raise TemplateError("模板里没有 KSampler")
        return ""
    return ids[0]


def _link_node(prompt, link):
    """["12", 0] → ("12", 0)；不是链接就返回 (None, None)。"""
    if isinstance(link, (list, tuple)) and len(link) == 2:
        return str(link[0]), link[1]
    return None, None


def _is_link(value):
    return isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], (str, int))


def _trace_text(prompt, link, prefer="", seen=None, depth=0):
    """顺着条件链找真正的文本节点，返回 (node_id, input_field)；找不到 (None, None)。

    会穿过 ControlNetApply* / CameraAngleSDXL / conditioning 透传节点，
    但只跟 positive / negative / conditioning 这几条线，不会走进加载器或 latent 分支。
    """
    nid, _slot = _link_node(prompt, link)
    if not nid or depth > 16:
        return None, None
    seen = seen if seen is not None else set()
    if nid in seen:
        return None, None
    seen.add(nid)
    node = prompt.get(nid)
    if not isinstance(node, dict):
        return None, None
    if _is_text(node):
        return nid, "text"
    inputs = node.get("inputs") or {}
    if not isinstance(inputs, dict):
        return None, None
    fields = [k for k, v in inputs.items()
              if str(k).lower() in PASSTHROUGH_FIELDS and _is_link(v)]
    if prefer:
        fields.sort(key=lambda k: 0 if str(k).lower() == str(prefer).lower() else 1)
        if str(prefer).lower() == "negative" and not any(
                str(k).lower() == "negative" for k in fields):
            fields.reverse()  # ConditioningConcat 这类没有 positive/negative 名，负向取后一路
    for field in fields:
        found, ffield = _trace_text(prompt, inputs[field], prefer=prefer, seen=seen, depth=depth + 1)
        if found:
            return found, ffield
    return None, None


def _cn_slots(prompt):
    """所有 ControlNetApply* 节点里的控制图槽位：[{node, field, class_type}]（按 id 排序）。"""
    slots = []
    for nid in _nodes_where(prompt, _is_controlnet_apply):
        inputs = prompt[nid].get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        field = ""
        for cand in ("image", "control_image", "controlnet_image", "pixels"):
            if cand in inputs:
                field = cand
                break
        if not field:
            for key, value in inputs.items():
                low = str(key).lower()
                if low in ("positive", "negative", "control_net", "vae", "strength",
                           "start_percent", "end_percent"):
                    continue
                if _is_link(value):
                    field = str(key)
                    break
        if field:
            slots.append({"node": nid, "field": field,
                          "class_type": _cls(prompt.get(nid))})
    return slots


def sniff(prompt):
    """槽位报告：正负提示词会被写进哪个节点、Sampler / latent / 存图 / ControlNet 在哪。

    返回 {camera, positive, negative, sampler, latent, save,
          controlnet:{depth:[{node,field,class_type}], pose:[…]}, warn}
    """
    warn = []
    prompt = prompt if isinstance(prompt, dict) else {}
    sampler = _find_sampler(prompt, required=False)
    camera = _nodes_where(prompt, _is_camera)

    positive, negative = [], []
    if sampler:
        inputs = prompt[sampler].get("inputs") or {}
        for link, prefer, bucket in ((inputs.get("positive"), "positive", positive),
                                     (inputs.get("negative"), "negative", negative)):
            nid, field = _trace_text(prompt, link, prefer=prefer)
            if nid:
                bucket.append({"node": nid, "field": field,
                               "class_type": _cls(prompt.get(nid))})

    text_nodes = _nodes_where(prompt, _is_text)
    if not positive and text_nodes:
        positive.append({"node": text_nodes[0], "field": "text",
                         "class_type": _cls(prompt.get(text_nodes[0]))})
        warn.append("没从 KSampler.positive 追到文本节点，退化用第一个文本节点 %s" % text_nodes[0])
    if not negative:
        spare = [nid for nid in text_nodes if not positive or nid != positive[0]["node"]]
        if spare:
            negative.append({"node": spare[0], "field": "text",
                             "class_type": _cls(prompt.get(spare[0]))})
            warn.append("没从 KSampler.negative 追到文本节点，退化用另一个文本节点 %s" % spare[0])
    if camera and not text_nodes:
        warn.append("模板有 CameraAngleSDXL 节点，但没找到 CLIPTextEncode* 文本节点")

    latent = ""
    for nid in _nodes_where(prompt, _is_latent):
        latent = nid
        break
    if not latent:
        for nid, node in prompt.items():
            inputs = (node or {}).get("inputs") if isinstance(node, dict) else None
            if isinstance(inputs, dict) and "width" in inputs and "height" in inputs:
                latent = str(nid)
                break

    saves = _nodes_where(prompt, _is_save)
    slots = _cn_slots(prompt)
    controlnet = {"depth": slots[:1], "pose": slots[1:2]}
    if len(slots) > 2:
        warn.append("模板里有 %d 个 ControlNet 槽位，只自动分配前两个（depth/pose）" % len(slots))
    if slots and not camera:
        pass

    return {
        "camera": camera,
        "positive": positive,
        "negative": negative,
        "sampler": sampler,
        "latent": latent,
        "save": saves[0] if saves else "",
        "controlnet": controlnet,
        "warn": warn,
    }


def inspect(ref):
    """POST /api/templates/inspect：模板 id / 路径 → 槽位报告（不提交任何东西）。"""
    path, prompt, schema = load(ref)
    info = sniff(prompt)
    node_list = []
    for nid in sorted((str(k) for k in prompt.keys()), key=_id_key):
        node = prompt.get(nid) or {}
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        node_list.append({
            "id": nid,
            "class_type": _cls(node),
            "title": str(meta.get("title") or ""),
        })
    info["sniff"] = {k: info[k] for k in ("camera", "positive", "negative", "sampler", "latent", "save", "controlnet", "warn") if k in info}
    info.update({
        "ok": True,
        "template": os.path.basename(path),
        "template_path": path,
        "nodes": len(prompt),
        "node_list": node_list,
        "vars": [v.get("name") for v in (schema.get("vars") or []) if isinstance(v, dict) and v.get("name")],
        "schema_title": str(schema.get("title") or ""),
    })
    return info


def empty_sniff():
    return {"camera": [], "positive": [], "negative": [], "sampler": "", "latent": "",
            "save": "", "controlnet": {"depth": [], "pose": []}, "warn": []}


# ══════════════════════ 变量定位 ══════════════════════
_CLASS_DEFAULT_INPUT = {
    "cliptextencode": "text",
    "cliptextencodesdxl": "text",
    "cliptextencodeflux": "text",
    "cliptextencodesd3": "text",
    "emptylatentimage": "width",
    "emptylatentimagesdxl": "width",
    "emptysd3latentimage": "width",
    "ksampler": "seed",
    "ksampleradvanced": "seed",
    "controlnetloader": "control_net_name",
    "controlnetapply": "strength",
    "controlnetapplyadvanced": "strength",
    "checkpointloadersimple": "ckpt_name",
    "loraLoader": "lora_name",
    "loadimage": "image",
    "saveimage": "filename_prefix",
}


def _guess_input(prompt, node_id, var_name):
    """schema 里只写了 node 没写 input 时，按节点 class 与已有输入推断该写哪个字段。"""
    node = prompt.get(str(node_id)) or {}
    inputs = node.get("inputs") or {}
    if var_name in inputs:
        return var_name
    guess = _CLASS_DEFAULT_INPUT.get(_cls_low(node))
    if guess and guess in inputs:
        return guess
    return None


def _var_targets(schema, prompt=None):
    """vars 名 → [(node_id, input_name), ...]（input 缺省时按节点类型推断）"""
    out = {}
    for v in (schema.get("vars") or []):
        if not isinstance(v, dict):
            continue
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
            if isinstance(b, dict) and b.get("node") and b.get("input"):
                targets.append((str(b["node"]), b["input"]))
        if targets:
            out[name] = targets
    return out


def _infer_targets(prompt):
    """没有 schema（或 schema 没写全）时，按节点类型推断 width/height/seed/steps/cfg 写哪儿。"""
    out = {}
    latents = _nodes_where(prompt, _is_latent)
    if not latents:
        for nid, node in prompt.items():
            inputs = (node or {}).get("inputs") if isinstance(node, dict) else None
            if isinstance(inputs, dict) and "width" in inputs and "height" in inputs:
                latents.append(str(nid))
                break
    for nid in latents[:1]:
        inputs = prompt[nid].get("inputs") or {}
        for var in ("width", "height"):
            if var in inputs:
                out.setdefault(var, []).append((nid, var))
    for nid in _nodes_where(prompt, _is_sampler)[:1]:
        inputs = prompt[nid].get("inputs") or {}
        for var in ("seed", "noise_seed"):
            if var in inputs:
                out.setdefault("seed", []).append((nid, var))
                break
        for var in ("steps", "cfg"):
            if var in inputs:
                out.setdefault(var, []).append((nid, var))
    return out


def _merged_targets(schema, prompt):
    out = _infer_targets(prompt)
    for name, lst in _var_targets(schema, prompt).items():
        out[name] = list(lst)  # schema 明确写了就以 schema 为准
    return out


def _get_input_text(prompt, node_id, field):
    node = prompt.get(str(node_id)) or {}
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if isinstance(inputs, dict):
        return str(inputs.get(field or "text") or "")
    return ""


def _text_slot_target(prompt, node_id, field):
    """文本槽位若「连到相机节点输出」，改写成相机节点的 extra_positive/extra_negative。

    例：CameraAngleSDXL(3) → CLIPTextEncode(4).text = ["3", 0] 时，直接写 4.text 会把
    机位词整段覆盖掉；正确做法是把工作台的提示词写进 3.extra_positive，保持连线不变。
    """
    node = prompt.get(str(node_id)) or {}
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        return str(node_id), (field or "text"), False
    key = field or "text"
    cur = inputs.get(key)
    src_id, _idx = _link_node(prompt, cur)
    if src_id and _is_camera(prompt.get(src_id) or {}):
        return src_id, "", True
    return str(node_id), key, False


def _set_input_text(prompt, node_id, field, text):
    node = prompt.get(str(node_id))
    if not isinstance(node, dict):
        return False
    inputs = node.setdefault("inputs", {})
    if not isinstance(inputs, dict):
        return False
    key = field or "text"
    if key not in inputs and "text" in inputs:
        key = "text"
    inputs[key] = text
    return True


# ══════════════════════ ControlNet 注入 ══════════════════════
def _new_node_id(prompt, base="LoadImage"):
    """给插入的 LoadImage 找一个不撞车的 id（数字 id 就 +1，否则加后缀）。"""
    nums = [int(str(k)) for k in prompt.keys() if str(k).isdigit()]
    nid = str(max(nums) + 1 if nums else 9001)
    while nid in prompt:
        nid = str(int(nid) + 1)
    return nid


def _apply_control_image(prompt, slot, filename, warn):
    """把一个已上传的文件名写进 ControlNet 槽位；只改 inputs，必要时插一个 LoadImage。"""
    nid = str(slot.get("node") or "")
    node = prompt.get(nid)
    if not isinstance(node, dict):
        warn.append("ControlNet 槽位节点 %s 不存在，跳过" % nid)
        return False
    inputs = node.setdefault("inputs", {})
    if not isinstance(inputs, dict):
        warn.append("ControlNet 节点 %s 的 inputs 异常，跳过" % nid)
        return False
    field = slot.get("field") or "image"
    cur = inputs.get(field)
    if isinstance(cur, str) or cur is None:
        inputs[field] = filename
        return True
    src_id, _slot = _link_node(prompt, cur)
    if not src_id:
        inputs[field] = filename
        return True
    src = prompt.get(src_id)
    if isinstance(src, dict) and _is_loadimage(src):
        src.setdefault("inputs", {})["image"] = filename  # 复用作者的 LoadImage：只换图
        return True
    new_id = _new_node_id(prompt)
    prompt[new_id] = {"class_type": "LoadImage", "inputs": {"image": filename}}
    inputs[field] = [new_id, 0]
    warn.append("ControlNet 槽位 %s.%s 原本连到 %s(%s)，已插入 LoadImage %s 并改链"
                % (nid, field, src_id, _cls(src), new_id))
    return True


def _inject_controlnet(prompt, controlnet, extra, warn):
    """controlnet_depth / controlnet_pose 注入；返回每个 kind 的落点（没注入就是 None）。"""
    out = {"depth": None, "pose": None}
    if not isinstance(extra, dict):
        return out
    strength = extra.get("controlnet_strength")
    for kind in ("depth", "pose"):
        filename = str(extra.get("controlnet_" + kind) or "").strip()
        if not filename:
            continue
        slots = (controlnet or {}).get(kind) or []
        if not slots:
            warn.append("模板里没有 ControlNet 槽位，controlnet_%s 未注入（%s）" % (kind, filename))
            continue
        slot = slots[0]
        if not _apply_control_image(prompt, slot, filename, warn):
            continue
        node = prompt.get(str(slot["node"])) or {}
        inputs = node.setdefault("inputs", {})
        if strength is not None and isinstance(inputs, dict) and "strength" in inputs:
            try:
                inputs["strength"] = float(strength)
            except (TypeError, ValueError):
                warn.append("controlnet_strength=%r 不是数字，忽略" % (strength,))
        out[kind] = {"node": str(slot["node"]), "field": slot["field"], "image": filename}
    return out


# ══════════════════════ 注入 ══════════════════════
def build(ref, gen, positive, negative):
    """返回可直接 POST /prompt 的节点字典。gen 是 /api/generate 那套参数。"""
    path, prompt, schema = load(ref)
    prompt = json.loads(json.dumps(prompt))  # 深拷贝，别改缓存
    gen = gen if isinstance(gen, dict) else {}

    info = sniff(prompt)
    warn = list(info.get("warn") or [])
    targets = _merged_targets(schema, prompt)

    # ---- 正向 / 反向文本槽位：schema 变量优先，否则用嗅探结果（大小写 / 透传节点都已处理）
    pos_slots = list(targets.get("prompt") or [])
    if not pos_slots:
        pos_slots = [(t["node"], t["field"]) for t in (info.get("positive") or [])]
    neg_slots = list(targets.get("negative") or [])
    if not neg_slots:
        neg_slots = [(t["node"], t["field"]) for t in (info.get("negative") or [])]

    pos_written = []
    for nid, inp in pos_slots:
        tnid, tinp, is_camera = _text_slot_target(prompt, nid, inp)
        if is_camera:
            tinp = "extra_positive"
            if prompt[tnid].setdefault("inputs", {}).get(tinp) is None:
                prompt[tnid]["inputs"][tinp] = ""
            _set_input_text(prompt, tnid, tinp, positive or "")
            pos_written.append(nid)
            warn.append("正向提示词写进相机节点 %s 的 extra_positive（槽位 %s.%s 是它的连线，未打断）"
                        % (tnid, nid, inp))
            continue
        if _set_input_text(prompt, tnid, tinp, positive or ""):
            pos_written.append(nid)
    if positive and not pos_written:
        warn.append("模板里找不到正向文本槽位，正向提示词未注入")

    mode = str(gen.get("negative_mode") or "append").lower()
    neg_written = []
    for nid, inp in neg_slots:
        tnid, tinp, is_camera = _text_slot_target(prompt, nid, inp)
        if is_camera:
            tinp = "extra_negative"
            cur = str((prompt[tnid].setdefault("inputs", {}) or {}).get(tinp) or "")
            if mode == "replace":
                new = negative or ""
            elif negative:
                new = (cur.rstrip().rstrip(",") + ", " + negative).strip(", ")
            else:
                new = cur
            _set_input_text(prompt, tnid, tinp, new)
            neg_written.append(nid)
            warn.append("负向提示词写进相机节点 %s 的 extra_negative（槽位 %s.%s 是它的连线，未打断）"
                        % (tnid, nid, inp))
            continue
        cur = _get_input_text(prompt, nid, inp)
        if mode == "replace":
            new = negative or ""
        elif negative:
            new = (cur.rstrip().rstrip(",") + ", " + negative).strip(", ")
        else:
            new = cur
        if _set_input_text(prompt, nid, inp, new):
            neg_written.append(nid)
    if negative and not neg_written:
        warn.append("模板里找不到负向文本槽位，负向提示词未注入")

    # ---- 尺寸 / 种子 / 步数 / CFG / 其它变量
    def apply_var(name, value):
        for nid, inp in targets.get(name, []):
            node = prompt.get(str(nid))
            if not isinstance(node, dict):
                continue
            node.setdefault("inputs", {})[inp] = value

    if gen.get("width"):
        apply_var("width", int(gen["width"]))
    if gen.get("height"):
        apply_var("height", int(gen["height"]))
    try:
        seed_val = int(gen.get("seed"))
    except (TypeError, ValueError):
        seed_val = -1
    if seed_val >= 0:
        apply_var("seed", seed_val)
    if gen.get("steps"):
        apply_var("steps", int(gen["steps"]))
    if gen.get("cfg"):
        apply_var("cfg", round(float(gen["cfg"]), 2))

    # 模板自定义变量（pose 之类）：gen["template_vars"] 里给就用，否则沿用模板默认
    extra = gen.get("template_vars") if isinstance(gen.get("template_vars"), dict) else {}
    for key, value in extra.items():
        if str(key).startswith("controlnet_"):
            continue
        apply_var(key, value)

    # ---- ControlNet 控制图（已上传到 ComfyUI input/ 的文件名）
    injected = _inject_controlnet(prompt, info.get("controlnet") or {}, extra, warn)

    # 存图前缀带上工作台前缀，方便在 ComfyUI 输出目录里认出来
    prefix = str(gen.get("save_prefix") or "").strip()
    if prefix:
        for nid in _nodes_where(prompt, _is_save):
            inputs = prompt[nid].setdefault("inputs", {})
            cur = str(inputs.get("filename_prefix") or "")
            if cur and not cur.startswith(prefix + "/"):
                inputs["filename_prefix"] = urllib.parse.quote(prefix, safe="") + "/" + cur

    positive_node = pos_written[0] if pos_written else ""
    negative_node = neg_written[0] if neg_written else ""
    if not positive_node and info.get("positive"):
        positive_node = str(info["positive"][0].get("node") or "")
    if not negative_node and info.get("negative"):
        negative_node = str(info["negative"][0].get("node") or "")

    info["warn"] = []  # 已经并进 warn，避免重复
    return {
        "prompt": prompt,
        "template": os.path.basename(path),
        "template_path": path,
        "warn": warn,
        "positive_node": positive_node,
        "negative_node": negative_node,
        "sampler": info.get("sampler") or "",
        "sniff": info,
        "injected": injected,
    }
