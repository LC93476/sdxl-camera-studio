# -*- coding: utf-8 -*-
"""把本项目发布到 GitHub（只用标准库）。

用法（三种情况都支持）：

  1) 仓库已经在网页上建好了（最简单，拿到地址直接推）：
     python tools/publish_github.py --repo https://github.com/<你>/<仓库>.git --token <TOKEN>

  2) 连仓库一起建（token 有 repo 权限即可，不用先去网页点）：
     python tools/publish_github.py --name sdxl-camera-studio --token <TOKEN> --public

  3) token 放环境变量，避免出现在命令行历史里：
     set GITHUB_TOKEN=ghp_xxx  &&  python tools/publish_github.py --name sdxl-camera-studio

  只想在本地把仓库整理好、先不推：
     python tools/publish_github.py --local-only

token 需要什么权限：classic token 勾 repo（要建仓库就勾 repo 全权），
fine-grained token 给目标仓库 Contents: Read and write（建仓库还要 Administration: Read and write）。

推完之后 origin 保存的是不带 token 的干净地址，token 不会留在 .git/config 里。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"
DEFAULT_MSG = "feat: SDXL 相机控制节点 + 桌面相机工作台"


def run(args, check=True, quiet=False):
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not quiet and proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        if check:
            print("[!] 命令失败: %s" % " ".join(args))
            print((proc.stderr or proc.stdout).strip()[:2000])
            sys.exit(1)
        return proc
    return proc


def git(*args, **kw):
    return run(["git", *args], **kw)


def have_git_repo():
    return os.path.isdir(os.path.join(ROOT, ".git"))


def api(path, token, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "sdxl-camera-studio-publisher")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body or "{}")
        except Exception:
            return exc.code, {"message": body[:500]}
    except Exception as exc:  # 网络问题
        return 0, {"message": str(exc)}


def parse_repo(value):
    """接受 owner/name、https://github.com/o/n(.git)、git@github.com:o/n.git"""
    value = value.strip()
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", value)
    if m:
        return m.group(1), m.group(2)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        return value.split("/")[0], value.split("/")[1]
    print("[!] 认不出仓库地址: %s（要 owner/name 或 https://github.com/owner/name）" % value)
    sys.exit(1)


def ensure_identity(token):
    """本地仓库没有身份时补一个；有 token 就用 GitHub 账号信息。"""
    name = subprocess.run(["git", "config", "user.name"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    email = subprocess.run(["git", "config", "user.email"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    login = email_ = None
    if token:
        status, me = api("/user", token)
        if status == 200 and me.get("login"):
            login = me["login"]
            email_ = me.get("email") or ("%s@users.noreply.github.com" % login)
    if not name:
        git("config", "--local", "user.name", login or "SDXL Camera Studio")
        print("[*] git user.name -> %s" % (login or "SDXL Camera Studio"))
    if not email:
        git("config", "--local", "user.email", email_ or "sdxl-camera-studio@users.noreply.github.com")
        print("[*] git user.email -> %s" % (email_ or "sdxl-camera-studio@users.noreply.github.com"))
    return login


def main():
    ap = argparse.ArgumentParser(description="把 SDXL 相机工作台发布到 GitHub")
    ap.add_argument("--repo", help="目标仓库：owner/name 或 https://github.com/owner/name.git")
    ap.add_argument("--name", help="要新建的仓库名（配合 --token 使用）")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
                    help="GitHub token（也可用环境变量 GITHUB_TOKEN）")
    ap.add_argument("--public", action="store_true", help="建仓库时设为公开（默认私有）")
    ap.add_argument("--desc", default="SDXL camera control: 3D 摄像机拖拽 → 提示词权重 → ComfyUI 出图",
                    help="新建仓库时的描述")
    ap.add_argument("--branch", default="main", help="分支名，默认 main")
    ap.add_argument("--message", default=DEFAULT_MSG, help="提交信息")
    ap.add_argument("--local-only", action="store_true", help="只做本地仓库整理与提交，不联网")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    args = ap.parse_args()

    def step(cmd_desc):
        print("\n==> " + cmd_desc)

    # 1) 本地仓库
    if not have_git_repo():
        step("初始化本地仓库（分支 %s）" % args.branch)
        git("init", "-b", args.branch)
    else:
        step("已有 .git，跳过 init")

    login = ensure_identity(args.token if not args.local_only else None)

    step("暂存全部文件")
    git("add", "-A")
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    files = [x for x in staged.splitlines() if x.strip()]
    print("[*] 待提交 %d 个文件" % len(files))
    if files:
        step("提交")
        git("commit", "-m", args.message)
    else:
        print("[*] 没有新的改动，跳过 commit")

    if args.local_only:
        print("\n[完成] 本地仓库已就绪：%s" % ROOT)
        print("下一步：带上 token 再跑一次本脚本即可推送。")
        return

    if not args.token and not args.repo:
        print("\n[!] 没有 token 也没有 --repo：先在网页上建好空仓库，然后")
        print("    python tools/publish_github.py --repo https://github.com/<你>/<仓库>.git --token <TOKEN>")
        print("    或者直接给 token 让脚本连仓库一起建：--name sdxl-camera-studio --token <TOKEN>")
        sys.exit(2)

    owner = name = None
    if args.repo:
        owner, name = parse_repo(args.repo)
    elif not args.token:
        print("[!] 建仓库需要 --token")
        sys.exit(2)
    else:
        status, me = api("/user", args.token)
        if status != 200:
            print("[!] token 校验失败（%s）：%s" % (status, me.get("message")))
            sys.exit(1)
        owner = me["login"]
        name = args.name or "sdxl-camera-studio"
        print("[*] 已登录 GitHub：%s" % owner)
        step("建仓库 %s/%s（%s）" % (owner, name, "公开" if args.public else "私有"))
        status, body = api("/user/repos", args.token, "POST", {
            "name": name, "description": args.desc, "private": not args.public,
            "has_issues": True, "has_wiki": False, "auto_init": False,
        })
        if status == 201:
            print("[*] 建好了：%s" % body.get("html_url"))
        elif status == 422:
            print("[*] 仓库已存在，直接推")
        else:
            print("[!] 建仓库失败（%s）：%s" % (status, body.get("message")))
            sys.exit(1)

    clean_url = "https://github.com/%s/%s.git" % (owner, name)
    step("推送 %s/%s" % (owner, name))
    if args.dry_run:
        print("[dry-run] git push https://***@github.com/%s/%s.git HEAD:refs/heads/%s" % (owner, name, args.branch))
        return
    push_url = "https://x-access-token:%s@github.com/%s/%s.git" % (args.token, owner, name)
    # 不用 -u：给的是显式 URL，upstream 由后面手动写进 config
    proc = run(["git", "push", push_url, "HEAD:refs/heads/%s" % args.branch], check=False)
    if proc.returncode != 0:
        proc = run(["git", "push", push_url, "HEAD:refs/heads/%s" % args.branch], check=False)
    if proc.returncode != 0:
        print("\n[!] 推送失败。常见原因：token 没有 repo/Contents 写权限，或仓库名拼错。")
        sys.exit(1)

    # origin 保存干净地址，token 不落盘
    git("remote", "remove", "origin", check=False, quiet=True)
    git("remote", "add", "origin", clean_url)
    git("config", "branch.%s.remote" % args.branch, "origin")
    git("config", "branch.%s.merge" % args.branch, "refs/heads/%s" % args.branch)

    print("\n[完成] https://github.com/%s/%s" % (owner, name))
    print("        origin = %s（不含 token）" % clean_url)


if __name__ == "__main__":
    main()
