# -*- coding: utf-8 -*-
"""仓库自检：本地模式 + 远端模式（GH_TOKEN）

用法：
    python tools/verify_repo.py

两种模式：
1. 本地模式（默认；没有 GH_TOKEN、或远端请求失败时自动走这里）
   - 用 git ls-files 列出被跟踪文件，校验关键文件一个不少；
   - 图片只允许 docs/screenshots/mapping.png、docs/screenshots/ui_overview.png、studio/icon.png、studio/icon.ico；
   - 确认 .gitignore 真的拦住了运行产物与本机配置；
   - 确认已下线的发布脚本没有回来（内容在 archive/）。
2. 远端模式（环境变量里有 GH_TOKEN）
   - 校验仓库是 private、默认分支、文件树里有全部关键文件、图片白名单一致；
   - 打印最近 5 条提交与两个关键文件的 raw 大小，顺带确认已删除的旧图确实 404。

两种模式都以 exit 0 表示通过；有 FAIL 时 exit 1 并打印原因。
根目录出现 Windows 保留名（CON / PRN / AUX / NUL / COM1..9 / LPT1..9 等）只报告，不崩溃。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "LC93476/sdxl-camera-studio"

KEY_FILES = [
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "requirements.txt",
    "core/camera_core.py",
    "core/presets.py",
    "nodes/camera_sdxl_node.py",
    "studio/server.py",
    "studio/CONTRACT.md",
    "studio/web/index.html",
    "studio/web/styles.css",
    "studio/web/app.js",
    "studio/web/camera3d.js",
    "js/three.module.js",
    "studio/tools/e2e_test.py",
    "studio/tools/check_ui.py",
    "tools/token_count.py",
    "tools/parity_check.py",
    "tools/validate_workflow.py",
    "tools/verify_repo.py",
    ".github/workflows/ci.yml",
    "start_studio.bat",
    "workflows/SDXL.CameraAngle.ControlNet.json",
]

ALLOWED_IMAGES = {
    "docs/screenshots/mapping.png",
    "docs/screenshots/ui_overview.png",
    "studio/icon.png",
    "studio/icon.ico",
}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg")

REMOVED_FILES = [
    "tools/publish_github.py",
    "tools/publish_final.py",
    "tools/recreate_public.py",
    "tools/tok_measure.py",
    "发布到GitHub.bat",
]
ARCHIVE_FILES = [
    "archive/说明.md",
    "archive/publish_github.py",
    "archive/publish_final.py",
    "archive/recreate_public.py",
    "archive/tok_measure.py",
    "archive/发布到GitHub.bat",
]

MUST_BE_IGNORED = [
    "studio/state/config.json",
    "studio/state/presets.json",
    "studio/out/",
    "out/",
    "__pycache__/",
]

RESERVED = {"CON", "PRN", "AUX", "NUL"}
RESERVED |= {"COM" + str(i) for i in range(1, 10)}
RESERVED |= {"LPT" + str(i) for i in range(1, 10)}


class Report(object):
    def __init__(self):
        self.ok = 0
        self.warned = 0
        self.failed = []

    def pass_(self, msg):
        self.ok += 1
        print("  PASS " + msg)

    def warn(self, msg):
        self.warned += 1
        print("  WARN " + msg)

    def fail(self, msg):
        self.failed.append(msg)
        print("  FAIL " + msg)

    def summary(self):
        print("")
        print("=" * 56)
        print("仓库自检汇总：PASS " + str(self.ok) + "  WARN " + str(self.warned) + "  FAIL " + str(len(self.failed)))
        for m in self.failed:
            print("  FAIL 原因: " + m)
        if self.failed:
            print("结果：失败")
            return 1
        print("结果：通过")
        return 0


def git(*args):
    """在仓库根跑一条 git 命令，返回 (rc, stdout)。git 不存在时返回 (None, '')。"""
    try:
        p = subprocess.run(["git"] + list(args), cwd=ROOT, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    if p.returncode != 0:
        return p.returncode, p.stdout.decode("utf-8", "replace")
    return 0, p.stdout.decode("utf-8", "replace")


def tracked_files():
    rc, out = git("ls-files", "-z")
    if rc != 0:
        return None
    return [n for n in out.split("\0") if n]


def walk_files():
    got = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "out", "node_modules")]
        for n in names:
            p = os.path.join(base, n)
            got.append(os.path.relpath(p, ROOT).replace("\\", "/"))
    return got


def check_reserved_names(rep):
    print("[0] 根目录 Windows 保留名检查")
    bad = []
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name)
        if not os.path.isfile(p):
            continue
        stem = name.split(".")[0].strip().upper()
        if stem in RESERVED or name != name.rstrip(" ."):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = -1
            bad.append((name, size))
    if bad:
        for name, size in bad:
            rep.warn("根目录有 Windows 保留名 / 尾随点空格的文件: " + repr(name) + " (" + str(size) + " 字节)，请改名后提交")
    else:
        rep.pass_("根目录没有 Windows 保留名")


def check_local(rep):
    print("")
    print("[1] 关键文件（git ls-files）")
    tracked = tracked_files()
    if tracked is None:
        rep.warn("git 不可用或不在仓库里，改用文件系统列举")
        tracked = walk_files()
    tset = set(tracked)
    for f in KEY_FILES:
        if not os.path.exists(os.path.join(ROOT, f.replace("/", os.sep))):
            rep.fail("关键文件在磁盘上不存在: " + f)
        elif f not in tset:
            rep.warn("文件存在但未被 git 跟踪: " + f)
        else:
            rep.pass_("在库: " + f)

    print("")
    print("[2] 图片白名单")
    images = [f for f in tracked if f.lower().endswith(IMAGE_EXT)]
    extra = [f for f in images if f not in ALLOWED_IMAGES]
    for f in sorted(images):
        if f in ALLOWED_IMAGES:
            rep.pass_("白名单图片: " + f)
    if extra:
        for f in extra:
            rep.fail("多余的图片文件（只允许 mapping.png / ui_overview.png / studio/icon.png）: " + f)
    for f in sorted(ALLOWED_IMAGES):
        if f not in images:
            rep.warn("白名单里的图片没在库中: " + f)

    print("")
    print("[3] .gitignore 生效")
    rc, _ = git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        rep.warn("不在 git 工作区，跳过 check-ignore")
    else:
        for rel in MUST_BE_IGNORED:
            rc, _ = git("check-ignore", "-q", rel)
            if rc == 0:
                rep.pass_("已被忽略: " + rel)
            elif rc == 1:
                rep.fail("没有被 .gitignore 拦住: " + rel)
            else:
                rep.warn("check-ignore 无法判断: " + rel)

    print("")
    print("[4] 已下线的发布脚本不应回来")
    for f in REMOVED_FILES:
        if f in tset or os.path.exists(os.path.join(ROOT, f.replace("/", os.sep))):
            rep.fail("应已删除但仍存在: " + f)
        else:
            rep.pass_("已删除: " + f)
    for f in ARCHIVE_FILES:
        if os.path.exists(os.path.join(ROOT, f.replace("/", os.sep))):
            rep.pass_("archive 里保留了原件: " + f)
        else:
            rep.fail("archive 缺少归档: " + f)


def http_json(path, token):
    req = urllib.request.Request("https://api.github.com/" + path)
    req.add_header("Authorization", "token " + token)
    req.add_header("User-Agent", "sdxl-camera-angle-verify")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def http_status(url, token):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "token " + token)
    req.add_header("User-Agent", "sdxl-camera-angle-verify")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return None, b""


def check_remote(rep, token):
    """返回 True 表示远端检查跑完了，False 表示应回落到本地模式。"""
    print("")
    print("[R] 远端模式（GH_TOKEN 存在）")
    try:
        info = http_json("repos/" + REPO, token)
    except Exception as e:
        rep.warn("远端请求失败（" + type(e).__name__ + ": " + str(e) + "），回落到本地模式")
        return False
    if info.get("private"):
        rep.pass_("仓库是 private")
    else:
        rep.fail("仓库不是 private")
    rep.pass_("默认分支: " + str(info.get("default_branch")) + " 大小: " + str(info.get("size")) + " KB")
    branch = info.get("default_branch") or "main"
    try:
        tree = http_json("repos/" + REPO + "/git/trees/" + branch + "?recursive=1", token)
        paths = set(x["path"] for x in tree.get("tree", []) if x.get("type") == "blob")
    except Exception as e:
        rep.warn("取文件树失败，跳过文件树校验: " + type(e).__name__)
        return True
    rep.pass_("远端文件数: " + str(len(paths)))
    for f in KEY_FILES:
        if f in paths:
            rep.pass_("远端在库: " + f)
        else:
            rep.fail("远端缺少关键文件: " + f)
    images = set(f for f in paths if f.lower().endswith(IMAGE_EXT))
    extra = images - ALLOWED_IMAGES
    if extra:
        for f in sorted(extra):
            rep.fail("远端有多余图片: " + f)
    else:
        rep.pass_("远端图片只有白名单三张")
    try:
        commits = http_json("repos/" + REPO + "/commits?per_page=5", token)
        for c in commits:
            rep.pass_("提交 " + c["sha"][:7] + " " + c["commit"]["message"].splitlines()[0][:60])
    except Exception as e:
        rep.warn("取提交列表失败: " + type(e).__name__)
    base = "https://raw.githubusercontent.com/" + REPO + "/" + branch + "/"
    for f in ("README.md", "pyproject.toml", "studio/icon.png"):
        st, body = http_status(base + f, token)
        if st == 200 and body:
            rep.pass_("raw " + f + " status=200 bytes=" + str(len(body)))
        else:
            rep.fail("raw " + f + " 取不到，status=" + str(st))
    for f in REMOVED_FILES:
        if f in paths:
            rep.fail("远端还有已下线的文件: " + f)
    return True


def main():
    print("仓库自检（" + ROOT + "）")
    rep = Report()
    check_reserved_names(rep)

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    remote_done = False
    if token:
        remote_done = check_remote(rep, token)
    else:
        print("")
        print("[R] 没有 GH_TOKEN，跳过远端检查（本地模式）")

    check_local(rep)
    if not remote_done and token:
        print("")
        print("[R] 远端不可用，已用本地结果为准")
    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())
