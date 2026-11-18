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


def main(path):
    wf = json.load(open(path, encoding="utf-8"))
    local = local_specs()
    try:
        oi = json.load(urllib.request.urlopen(BASE + "/object_info", timeout=120))
    except Exception as e:
        print("warn: 服务器 /object_info 取不到（%s），只用本地节点定义" % e)
        oi = {}
    oi.update(local)   # 源码优先
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
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else r"D:/QUARK/SDXL.CameraAngle.json"))
