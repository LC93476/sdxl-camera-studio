#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把相机算法同步进 dsh-tagger 插件（D:/DSh tagger/lib/camera.js）。

为什么要有这个脚本：tagger 不能 import 外部绝对路径（插件要被 junction 进 profile，
web profile 里也要独立跑），所以 lib/camera.js 是 js/camera_shared.js 的逐字副本。
逐字副本最容易腐烂 —— 手抄一次就永久偏。这里用「拼装」替代手抄：

    header（派生说明，本脚本生成）
  + mirror（js/camera_shared.js 原文，一字不改）
  + adapter（tagger 自己的门面：cameraMeta / cameraPreview / CAMERA_* 常量，
             从现有 lib/camera.js 里原样摘出来，不用重写）

用法：
    python tools/sync_tagger_camera.py            # 同步并自检
    python tools/sync_tagger_camera.py --check    # 只比对，不写
    python tools/sync_tagger_camera.py --target "D:/DSh tagger/lib/camera.js"

自检：mirror 段与 js/camera_shared.js 一致；adapter 段仍在；computeCamera 只出现一次。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHARED = os.path.join(ROOT, "js", "camera_shared.js")
DEFAULT_TARGET = "D:/DSh tagger/lib/camera.js"

ADAPTER_MARKER = "// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550 tagger \u9002\u914d\u5c42"

HEADER = """/**
 * dsh-tagger · 相机机位算法（SDXL）
 *
 * 这是 js/camera_shared.js 的**逐字副本**，只在末尾挂了 tagger 适配层
 * （cameraMeta / cameraPreview / CAMERA_* 常量）。**不要在这里改算法** ——
 * 改 sdxl_camera_angle/js/camera_shared.js（单一真源），然后重跑：
 *
 *     cd /d/QUARK/sdxl_camera_angle
 *     python tools/parity_check.py
 *     python tools/sync_tagger_camera.py
 *
 * 为什么复制而不是 import：插件要被 junction 进桌面端与 web profile，两条路径
 * 深浅不同，任何 ../../QUARK/... 相对路径换目录就断。同步靠脚本，不靠人记。
 */
"""


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def split_existing(text):
    idx = text.find(ADAPTER_MARKER)
    if idx < 0:
        return None
    return text[idx:]


def compose(shared_text, adapter_text):
    return HEADER + "\n" + shared_text.rstrip("\n") + "\n\n" + adapter_text


def selfcheck(text, shared_text):
    problems = []
    if text.count("export function computeCamera(") != 1:
        problems.append("computeCamera 出现 %d 次（应为 1）" % text.count("export function computeCamera("))
    for name in ("export function cameraPreview(", "export function cameraMeta(",
                 "export const CAMERA_DEFAULT_PARAMS"):
        if name not in text:
            problems.append("缺 %s" % name)
    for line in shared_text.splitlines():
        if line.strip() and line not in text:
            problems.append("mirror 段缺行：%s" % line.strip()[:80])
            break
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="同步相机算法到 dsh-tagger")
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--check", action="store_true", help="只比对不写")
    args = ap.parse_args(argv)

    if not os.path.isfile(SHARED):
        print("[ERR] 找不到镜像源：%s" % SHARED)
        return 2
    if not os.path.isfile(args.target):
        print("[ERR] 找不到目标：%s" % args.target)
        return 2

    shared_text = read(SHARED)
    old = read(args.target)
    adapter = split_existing(old)
    if adapter is None:
        print("[ERR] 目标文件里找不到适配层标记 %s" % ADAPTER_MARKER)
        return 2

    new = compose(shared_text, adapter)
    problems = selfcheck(new, shared_text)
    if problems:
        print("[ERR] 自检未过：")
        for p in problems:
            print("      - " + p)
        return 1

    if new == old:
        print("[ok] 无需改动：%s 已与 js/camera_shared.js 一致" % args.target)
        return 0
    if args.check:
        print("[diff] 需要同步：%s 与 js/camera_shared.js 不一致" % args.target)
        return 1

    with open(args.target, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    print("[ok] 已同步 %s" % args.target)
    print("     mirror %d 行 + adapter %d 行" % (len(shared_text.splitlines()), len(adapter.splitlines())))
    print("     下一步：cd %s && ELECTRON_RUN_AS_NODE=1 <electron> --test tests/camera.test.mjs"
          % os.path.dirname(os.path.dirname(args.target)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
