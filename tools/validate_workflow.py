# -*- coding: utf-8 -*-
"""校验 litegraph 工作流 JSON：节点类型存在、连线索引合法、widgets_values 数量对得上。"""
import json, os, sys, urllib.request

BASE = "http://127.0.0.1:8188"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_specs():
    """本仓库节点（nodes/*.py）的 INPUT_TYPES —— 服务器的 /object_info 可能还是旧的，
    校验以源码为准，避免"改了 widget 顺序但校验照样过"。"""
    import importlib.util
    out = {}
    nodes_dir = os.path.join(ROOT, "nodes")
    core = os.path.join(ROOT, "core")
    if core not in sys.path:
        sys.path.insert(0, core)
    if not os.path.isdir(nodes_dir):
        return out
    for fn in os.listdir(nodes_dir):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location("_wfnode_" + fn[:-3], os.path.join(nodes_dir, fn))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print("warn: 载入 %s 失败 %s" % (fn, e))
            continue
        for attr in dir(mod):
            cls = getattr(mod, attr)
            if isinstance(cls, type) and hasattr(cls, "INPUT_TYPES"):
                try:
                    it = cls.INPUT_TYPES()
                except Exception:
                    continue
                out[attr] = {"input": it, "output": list(getattr(cls, "RETURN_TYPES", ()))}
    return out


def _is_api(wf):
    """API 格式（工作流 → 导出 API）：顶层是 {节点id: {class_type, inputs}}，没有 nodes/links 数组。"""
    if not isinstance(wf, dict) or "nodes" in wf:
        return False
    vals = [v for k, v in wf.items() if not str(k).startswith("_")]
    return bool(vals) and all(isinstance(v, dict) and "class_type" in v for v in vals)


def validate_api(wf, oi):
    """API 格式没有 widgets_values，改校验：类型已知 / 必填输入齐 / 连线目标存在且槽位不越界。"""
    nodes = {k: v for k, v in wf.items() if not str(k).startswith("_")}
    bad = []
    links = 0
    for nid, n in nodes.items():
        t = n.get("class_type")
        if not t:
            bad.append("node %s: 缺 class_type" % nid)
            continue
        inputs = n.get("inputs") or {}
        if t in oi:
            spec = oi[t]["input"]
            req = [k for k in (spec.get("required") or {})]
            miss = [k for k in req if k not in inputs]
            if miss:
                bad.append("node %s (%s): 缺必填输入 %s" % (nid, t, ", ".join(miss)))
        for key, val in inputs.items():
            if not (isinstance(val, list) and len(val) == 2 and isinstance(val[0], str)):
                continue
            src, slot = val[0], val[1]
            links += 1
            if src not in nodes:
                bad.append("node %s.%s: 连线指向不存在的节点 %s" % (nid, key, src))
                continue
            st = nodes[src].get("class_type")
            if st in oi:
                outs = oi[st].get("output") or []
                if not isinstance(slot, int) or slot >= len(outs):
                    bad.append("node %s.%s: 源 %s(%s) 槽位 %s 越界（它有 %d 个输出）"
                               % (nid, key, src, st, slot, len(outs)))
    print("API 格式 / 节点 %d 个 / 连线 %d 条" % (len(nodes), links))
    if bad:
        print("问题 %d 条：" % len(bad))
        for b in bad:
            print("  -", b)
        return 1
    print("OK 全部通过")
    return 0


def main(path):
    wf = json.load(open(path, encoding="utf-8"))
    local = local_specs()
    try:
        oi = json.load(urllib.request.urlopen(BASE + "/object_info", timeout=120))
    except Exception as e:
        print("warn: 服务器 /object_info 取不到（%s），只用本地节点定义" % e)
        oi = {}
    oi.update(local)   # 源码优先
    if _is_api(wf):
        return validate_api(wf, oi)
    nodes = {n["id"]: n for n in wf["nodes"]}
    links = {l[0]: l for l in wf["links"]}
    bad = []

    for nid, n in nodes.items():
        t = n["type"]
        if t not in oi:
            bad.append("node %s: 未知类型 %s" % (nid, t))
            continue
        spec = oi[t]["input"]
        req = list((spec.get("required") or {}).keys())
        opt = list((spec.get("optional") or {}).keys())
        # 该节点的输入槽里，被连线接管的 widget 名
        linked = {i["name"] for i in (n.get("inputs") or []) if i.get("link") is not None}
        n_widgets = len(n.get("widgets_values") or [])
        expect = len([x for x in req if x not in linked])
        # control_after_generate 之类的隐藏 widget 会多一个值，故允许 expect 或 expect+1
        if n_widgets not in (expect, expect + 1, len(req) + len(opt)):
            bad.append("node %s (%s): widgets_values=%d, 期望 %d（required %d, linked %d）"
                       % (nid, t, n_widgets, expect, len(req), len(linked)))
        # 输出类型
        outs = n.get("outputs") or []
        for i, o in enumerate(outs):
            if i < len(oi[t]["output"]) and o.get("type") != oi[t]["output"][i]:
                bad.append("node %s (%s): 输出槽 %d 类型 %s != %s"
                           % (nid, t, i, o.get("type"), oi[t]["output"][i]))

    for lid, l in links.items():
        _, src, sslot, dst, dslot, typ = l
        if src not in nodes:
            bad.append("link %s: 源节点 %s 不存在" % (lid, src)); continue
        if dst not in nodes:
            bad.append("link %s: 目标节点 %s 不存在" % (lid, dst)); continue
        so = nodes[src].get("outputs") or []
        di = nodes[dst].get("inputs") or []
        if sslot >= len(so):
            bad.append("link %s: 源 %s 槽 %d 越界" % (lid, src, sslot)); continue
        if dslot >= len(di):
            bad.append("link %s: 目标 %s 槽 %d 越界" % (lid, dst, dslot)); continue
        slot_links = so[sslot].get("links") or []
        if lid not in slot_links and so[sslot].get("link") != lid:
            bad.append("link %s: 源 %s 槽 %d 未登记该 link（实为 %s）" % (lid, src, sslot, slot_links))
        if di[dslot].get("link") != lid:
            bad.append("link %s: 目标 %s 槽 %d 未登记该 link（实为 %s）" % (lid, dst, dslot, di[dslot].get("link")))
        if typ not in ("*", so[sslot].get("type")):
            bad.append("link %s: 类型 %s 与源槽 %s 不符" % (lid, typ, so[sslot].get("type")))

    print("节点 %d 个 / 连线 %d 条" % (len(nodes), len(links)))
    if bad:
        print("问题 %d 条：" % len(bad))
        for b in bad:
            print("  -", b)
        return 1
    print("OK 全部通过")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        wfdir = os.path.join(ROOT, "workflows")
        args = sorted(os.path.join(wfdir, fn) for fn in os.listdir(wfdir) if fn.endswith(".json")) if os.path.isdir(wfdir) else []
        if not args:
            args = [os.path.join(ROOT, "workflows", "SDXL.CameraAngle.json")]
    rc = 0
    for p in args:
        if len(args) > 1:
            print("=== %s" % os.path.basename(p))
        rc |= main(p)
    sys.exit(rc)
