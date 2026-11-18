# -*- coding: utf-8 -*-
"""template_bridge 单测：纯 assert，python tests/test_template_bridge.py 直跑（本机没有 pytest）。

覆盖 studio/template_bridge.py 的公开面：
  sniff(prompt)                        —— 正负文本回溯 + ControlNet 槽位分配
  inspect(ref)                         —— 槽位报告（ok / nodes / node_list / sniff）
  build(ref, gen, positive, negative)  —— 注入后的 API 图（文本 / 尺寸 / 种子 / 控制图）
  _trace_text(...)                     —— 穿过 ConditioningConcat / ControlNetApply 等中间节点
  load / resolve                       —— 坏输入只抛 TemplateError，不崩

断言用的都是仓库自带的三个工作流：
  workflows/SDXL.CameraAngle.ControlNet.json  API 格式 13 节点（10=depth、13=pose 两个 ControlNet 槽位）
  workflows/SDXL.CameraAngle.api.json         API 格式 9 节点（3=CameraAngleSDXL，4/5 的 text 是它的连线）
  workflows/SDXL.CameraAngle.json             UI 工程文件（必须被拒）
"""
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.realpath(__file__))
_ROOT = os.path.dirname(_HERE)
_STUDIO = os.path.join(_ROOT, "studio")
if _STUDIO not in sys.path:
    sys.path.insert(0, _STUDIO)

import template_bridge as tb  # noqa: E402
from template_bridge import TemplateError  # noqa: E402

WF_DIR = os.path.join(_ROOT, "workflows")
CN_TMPL = os.path.join(WF_DIR, "SDXL.CameraAngle.ControlNet.json")
API_TMPL = os.path.join(WF_DIR, "SDXL.CameraAngle.api.json")
UI_TMPL = os.path.join(WF_DIR, "SDXL.CameraAngle.json")

POS = "(front view:1.30), eye level shot, medium shot"
NEG = "(blurry:1.10), (bad anatomy:1.05)"


def need(path):
    assert os.path.isfile(path), "模板缺失: " + path


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build(path, gen=None, positive=POS, negative=NEG):
    return tb.build(path, gen or {}, positive, negative)


def nodes_of(slots):
    return [s["node"] for s in (slots or [])]


# ══════════════════════ 1. ControlNet 模板：嗅探 ══════════════════════
def test_controlnet_sniff():
    need(CN_TMPL)
    prompt = read_json(CN_TMPL)
    assert len(prompt) == 13, "ControlNet 模板应有 13 个节点，实际 %d" % len(prompt)

    s = tb.sniff(prompt)
    assert s["sampler"] == "6", s["sampler"]
    assert s["latent"] == "5", s["latent"]
    assert s["save"] == "8", s["save"]
    assert nodes_of(s["positive"]) == ["3"], s["positive"]
    assert nodes_of(s["negative"]) == ["4"], s["negative"]
    assert s["positive"][0]["class_type"] == "CLIPTextEncode"
    assert s["camera"] == [], "纯 ControlNet 模板没有相机节点"

    depth = s["controlnet"]["depth"]
    pose = s["controlnet"]["pose"]
    assert len(depth) == 1 and depth[0]["node"] == "10", depth
    assert depth[0]["field"] == "image" and depth[0]["class_type"] == "ControlNetApplyAdvanced"
    assert len(pose) == 1 and pose[0]["node"] == "13", pose
    assert pose[0]["class_type"] == "ControlNetApplyAdvanced"
    assert s["warn"] == [], s["warn"]

    info = tb.inspect(CN_TMPL)
    assert info["ok"] is True
    assert info["nodes"] == 13 and len(info["node_list"]) == 13, info["nodes"]
    assert os.path.basename(info["template_path"]) == "SDXL.CameraAngle.ControlNet.json"
    types = dict((it["id"], it["class_type"]) for it in info["node_list"])
    assert types["9"] == "LoadImage" and types["12"] == "LoadImage", types
    assert types["11"] == "ControlNetLoader"
    assert info["sniff"]["controlnet"]["depth"][0]["node"] == "10"
    assert info["sniff"]["controlnet"]["pose"][0]["node"] == "13"
    assert info["vars"] == [], info["vars"]
    print("[ok] ControlNet 模板：pos=3 neg=4 sampler=6 latent=5 save=8；"
          "depth 槽位=10 pose 槽位=13；inspect nodes=13 vars=[]")


# ══════════════════════ 2. ControlNet 模板：注入控制图 ══════════════════════
def test_controlnet_build_inject():
    need(CN_TMPL)
    before = read_json(CN_TMPL)
    out = build(CN_TMPL, {
        "width": 640, "height": 640, "seed": 12345, "steps": 12, "cfg": 6.5,
        "negative_mode": "replace", "save_prefix": "qa_e2e",
        "template_vars": {
            "controlnet_depth": "qa_depth.png",
            "controlnet_pose": "qa_pose.png",
            "controlnet_strength": 0.33,
        },
    })
    prompt = out["prompt"]
    assert len(prompt) == 13, "只换图不该插节点，实际 %d 个节点" % len(prompt)

    # 两个 LoadImage 的 image 被换成传入的文件名（复用作者的加载节点）
    assert prompt["9"]["inputs"]["image"] == "qa_depth.png", prompt["9"]["inputs"]
    assert prompt["12"]["inputs"]["image"] == "qa_pose.png", prompt["12"]["inputs"]
    # 槽位仍然连着 9 / 12，没有被改链
    assert prompt["10"]["inputs"]["image"] == ["9", 0], prompt["10"]["inputs"]["image"]
    assert prompt["13"]["inputs"]["image"] == ["12", 0], prompt["13"]["inputs"]["image"]
    assert prompt["10"]["inputs"]["strength"] == 0.33
    assert prompt["10"]["inputs"]["start_percent"] == 0.0
    assert prompt["10"]["inputs"]["end_percent"] == 0.85

    assert out["injected"]["depth"]["node"] == "10"
    assert out["injected"]["pose"]["node"] == "13"
    assert out["injected"]["depth"]["image"] == "qa_depth.png"

    # 文本 / 尺寸 / 种子 / 步数 / CFG / 存图前缀
    assert prompt["3"]["inputs"]["text"] == POS
    assert prompt["4"]["inputs"]["text"] == NEG
    assert prompt["5"]["inputs"]["width"] == 640 and prompt["5"]["inputs"]["height"] == 640
    assert prompt["6"]["inputs"]["seed"] == 12345
    assert prompt["6"]["inputs"]["steps"] == 12
    assert prompt["6"]["inputs"]["cfg"] == 6.5
    assert prompt["8"]["inputs"]["filename_prefix"].startswith("qa_e2e/"), prompt["8"]["inputs"]

    assert out["positive_node"] == "3" and out["negative_node"] == "4"
    assert out["sampler"] == "6"
    assert out["template"] == "SDXL.CameraAngle.ControlNet.json"

    # 深拷贝：模板文件本身不能被改
    assert read_json(CN_TMPL) == before, "build() 改到了磁盘上的模板文件"
    assert read_json(CN_TMPL)["9"]["inputs"]["image"] != "qa_depth.png"
    print("[ok] build 注入 ControlNet：9.image=qa_depth.png 12.image=qa_pose.png；"
          "640x640 seed=12345 steps=12 cfg=6.5 strength=0.33；节点数仍 13；模板文件未被改")


# ══════════════════════ 3. 相机模板：文本槽位被相机节点占着 ══════════════════════
def test_api_template_camera_slot():
    need(API_TMPL)
    prompt = read_json(API_TMPL)
    assert prompt["3"]["class_type"] == "CameraAngleSDXL"
    assert prompt["4"]["inputs"]["text"] == ["3", 0]
    assert prompt["5"]["inputs"]["text"] == ["3", 1]

    s = tb.sniff(prompt)
    assert s["camera"] == ["3"], s["camera"]
    assert nodes_of(s["positive"]) == ["4"], s["positive"]
    assert nodes_of(s["negative"]) == ["5"], s["negative"]
    assert s["sampler"] == "7" and s["latent"] == "6" and s["save"] == "9"
    assert s["controlnet"]["depth"] == [] and s["controlnet"]["pose"] == []
    assert s["warn"] == [], s["warn"]

    out = build(API_TMPL, {"negative_mode": "replace"}, POS, NEG)
    p = out["prompt"]
    # 文字写进相机节点的 extra_*，不打断 4/5 到 3 的连线
    assert p["3"]["inputs"]["extra_positive"] == POS, p["3"]["inputs"]["extra_positive"]
    assert p["3"]["inputs"]["extra_negative"] == NEG
    assert p["4"]["inputs"]["text"] == ["3", 0], p["4"]["inputs"]["text"]
    assert p["5"]["inputs"]["text"] == ["3", 1], p["5"]["inputs"]["text"]
    assert out["positive_node"] == "4" and out["negative_node"] == "5"
    assert any("extra_positive" in w for w in out["warn"]), out["warn"]
    assert any("extra_negative" in w for w in out["warn"]), out["warn"]

    # 模板里没有 ControlNet 槽位时只 warn，不改结构
    out2 = build(API_TMPL, {"template_vars": {"controlnet_depth": "x.png"}})
    assert len(out2["prompt"]) == 9, "没有槽位时不该插节点，实际 %d" % len(out2["prompt"])
    assert out2["injected"]["depth"] is None
    assert any("没有 ControlNet 槽位" in w for w in out2["warn"]), out2["warn"]
    print("[ok] api 相机模板：pos 槽位=4 但文字写进 3.extra_positive，4.text 仍是 [3,0]；"
          "无 ControlNet 槽位时只 warn 不插节点")


# ══════════════════════ 4. 自定义链：_trace_text 穿过中间节点 ══════════════════════
def _write_tmp(prompt, name="custom.json"):
    d = tempfile.mkdtemp(prefix="tb_test_")
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(prompt, fh, ensure_ascii=False)
    return d, path


def test_custom_trace_chain():
    prompt = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "author text", "clip": ["20", 1]}},
        "2": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["1", 0], "negative": ["1", 0], "image": "x.png",
                         "control_net": ["8", 0], "strength": 0.5,
                         "start_percent": 0.0, "end_percent": 0.85}},
        "4": {"class_type": "ConditioningConcat",
              "inputs": {"conditioning_to": ["2", 0], "conditioning_from": ["2", 0]}},
        "5": {"class_type": "KSampler",
              "inputs": {"positive": ["4", 0], "negative": ["1", 0], "latent_image": ["6", 0],
                         "seed": 1, "steps": 20, "cfg": 7.0, "denoise": 1.0,
                         "sampler_name": "euler", "scheduler": "normal", "model": ["20", 0]}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "out", "images": ["9", 0]}},
        "8": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "cn.safetensors"}},
    }
    d, path = _write_tmp(prompt)
    try:
        nid, field = tb._trace_text(prompt, ["4", 0], prefer="positive")
        assert (nid, field) == ("1", "text"), "穿过 ConditioningConcat 应追到 1.text，实际 %r" % ((nid, field),)

        s = tb.sniff(prompt)
        assert s["camera"] == [], s["camera"]
        assert nodes_of(s["positive"]) == ["1"], s["positive"]
        assert s["sampler"] == "5" and s["latent"] == "6" and s["save"] == "7"
        assert s["controlnet"]["depth"][0]["node"] == "2", s["controlnet"]
        # 只有一个 ControlNet 槽位：pose 空着，不报错也不重复分配
        assert s["controlnet"]["pose"] == [], s["controlnet"]["pose"]
        assert s["warn"] == [], s["warn"]

        out = build(path, {"width": 768, "height": 512, "seed": 42}, "my prompt", "my neg")
        p = out["prompt"]
        assert p["1"]["inputs"]["text"].startswith("my prompt"), p["1"]["inputs"]["text"]
        assert "my neg" in p["1"]["inputs"]["text"], p["1"]["inputs"]["text"]
        assert p["6"]["inputs"]["width"] == 768 and p["6"]["inputs"]["height"] == 512
        assert p["5"]["inputs"]["seed"] == 42
        assert out["positive_node"] == "1"
        print("[ok] 自定义链：_trace_text 穿过 ControlNetApply + ConditioningConcat 追到 1.text；"
              "sniff depth=2 pose=空；build 尺寸/种子/文本都落地")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_degenerate_warn():
    prompt = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "aaa", "clip": ["9", 1]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "bbb", "clip": ["9", 1]}},
    }
    d, path = _write_tmp(prompt, "no_sampler.json")
    try:
        s = tb.sniff(prompt)
        assert s["sampler"] == "", s["sampler"]
        assert nodes_of(s["positive"]) == ["1"] and nodes_of(s["negative"]) == ["2"], s
        joined = " | ".join(s["warn"])
        assert "退化" in joined, s["warn"]
        assert "没从 KSampler" in joined, s["warn"]

        out = build(path, {}, "p", "n")
        assert out["prompt"]["1"]["inputs"]["text"] == "p"
        assert "n" in out["prompt"]["2"]["inputs"]["text"]
        print("[ok] 没有 KSampler 的模板：正负都退化到文本节点并 warn（%d 条）；build 仍写出文本"
              % len(s["warn"]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ══════════════════════ 5. 坏输入 ══════════════════════
def test_bad_inputs():
    need(UI_TMPL)

    raised = None
    try:
        tb.inspect(UI_TMPL)
    except TemplateError as exc:
        raised = str(exc)
    assert raised is not None, "UI 工程文件应抛 TemplateError"
    assert "Export (API)" in raised, raised
    assert "UI" in raised, raised

    for ref in ("definitely-not-a-template.json", "SDXL.no_such_template",
                os.path.join(WF_DIR, "no_such.json")):
        try:
            tb.load(ref)
        except TemplateError as exc:
            assert "找不到" in str(exc), str(exc)
        else:
            raise AssertionError("不存在的模板应抛 TemplateError: %r" % (ref,))

    try:
        tb.load("")
    except TemplateError as exc:
        assert "为空" in str(exc), str(exc)
    else:
        raise AssertionError("空 ref 应抛 TemplateError")

    try:
        build(UI_TMPL)
    except TemplateError as exc:
        assert "Export (API)" in str(exc), str(exc)
    else:
        raise AssertionError("UI 工程文件不该能 build")

    try:
        tb.inspect(os.path.join(WF_DIR, "no_such_template.json"))
    except TemplateError:
        pass
    else:
        raise AssertionError("inspect 不存在的模板应抛 TemplateError")

    # 非 dict / None 喂给 sniff 不炸（server 端坏 body 就是这样进来）
    for bad in (None, [], "x", 0):
        s = tb.sniff(bad)
        assert s["positive"] == [] and s["sampler"] == "" and s["warn"] == [], s
    print("[ok] 坏输入：UI 工程文件抛 TemplateError 且提到 Export (API)；"
          "不存在/空 ref 都抛 TemplateError；sniff(None/[]/str) 返回空报告不崩")


# ══════════════════════ 6. 模板列表 ══════════════════════
def test_list_templates():
    items = tb.list_templates()
    assert isinstance(items, list) and items, "list_templates 返回空"
    by_id = dict((it["id"], it) for it in items)
    for tid in ("SDXL.CameraAngle.ControlNet", "SDXL.CameraAngle.api"):
        assert tid in by_id, "模板列表缺 %s（现有 %s）" % (tid, sorted(by_id))
        assert by_id[tid]["valid"] is True, by_id[tid].get("error")
        assert by_id[tid]["nodes"] >= 9, by_id[tid]

    ui = by_id.get("SDXL.CameraAngle")
    if ui is not None:
        assert ui["valid"] is False, "UI 工程文件不该被标成 valid"
        assert "API" in str(ui.get("error") or ""), ui.get("error")

    assert all("path" in it and "name" in it for it in items), items[0]
    print("[ok] list_templates：%d 个模板，ControlNet/api 两个 valid，UI 工程文件被标 invalid"
          % len(items))


def main():
    tests = [
        test_controlnet_sniff,
        test_controlnet_build_inject,
        test_api_template_camera_slot,
        test_custom_trace_chain,
        test_degenerate_warn,
        test_bad_inputs,
        test_list_templates,
    ]
    for t in tests:
        t()
    print("\nALL %d TESTS PASSED" % len(tests))


if __name__ == "__main__":
    main()
