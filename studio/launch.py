# -*- coding: utf-8 -*-
"""SDXL 相机工作台 · 桌面启动器

做四件事：
1. 找一个空闲端口（默认 8770 起）；
2. 用 ComfyUI 自带的 python 拉起 studio/server.py（--no-browser）；
3. 等 /api/health 通了，用 Edge 的 app 模式开一个无浏览器边框的窗口；
4. 窗口关掉后（或 Ctrl+C）一并结束服务进程。

用法：
    python launch.py                # 正常启动
    python launch.py --port 8790    # 指定端口
    python launch.py --no-window    # 只起服务，不开窗口（调试用）
    python launch.py --browser chrome
"""
import argparse
import json
import os
import socket
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PY_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(HERE)), "ComfyUI-aki-v3.2", "python", "python.exe"),
    r"D:\QUARK\ComfyUI-aki-v3.2\python\python.exe",
    sys.executable,
]

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def pick_python():
    for p in PY_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return sys.executable


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(start=8770):
    for p in range(start, start + 40):
        if port_free(p):
            return p
    raise SystemExit("no free port in range %d..%d" % (start, start + 40))


def wait_health(port, timeout=40.0):
    url = "http://127.0.0.1:%d/api/health" % port
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8"))
                if data.get("ok"):
                    return data
        except Exception:
            time.sleep(0.35)
    return None


def find_browser(kind):
    cands = CHROME_CANDIDATES if kind == "chrome" else EDGE_CANDIDATES
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def open_app_window(url, kind="edge"):
    exe = find_browser(kind)
    profile = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "SDXLCameraStudio")
    if not exe:
        print("[launch] 没找到 Edge/Chrome，改用默认浏览器")
        webbrowser.open(url)
        return None
    args = [
        exe,
        "--app=%s" % url,
        "--window-size=1504,950",
        "--user-data-dir=%s" % profile,
        "--no-first-run",
        "--no-default-browser-check",
        # 关键：有些机器上已经有一个"以管理员权限运行"的 Edge 实例（比如浏览器是从
        # 提升权限的终端/编辑器里起来的）。这时普通权限启动会弹
        # 「Microsoft Edge 未响应，因为现有实例正在以提升的权限运行。是否要用普通权限重启现有实例？」
        # ——点"是"会把用户现有窗口全关掉。加上这个开关就不做降权重启，直接以当前权限起新实例。
        "--do-not-de-elevate",
        "--disable-features=Translate,MediaRouter",
    ]
    print("[launch] 打开应用窗口:", os.path.basename(exe))
    # Edge/Chrome 的启动进程会立刻退出、由真正的浏览器进程接管，所以不能拿 Popen 句柄当窗口存活判断。
    # 返回 profile 目录，交给 _count_browser() 轮询命令行里的这个标记。
    #
    # 两种实测坑（都在这台机器上复现过）：
    #   1) profile 目录里残留 lockfile / Singleton* —— 上次用 Stop-Process -Force 杀掉浏览器后留下的，
    #      下次启动会直接报 "Lock file can not be created! Error code: 5" 并 exit 21，一个窗口都不出现。
    #      对策：动手前先清掉这些残留文件（_clean_profile）。
    #   2) 某些情况下 Popen 的进程创建路径拿不到窗口 —— 兜底用 PowerShell Start-Process 再试一次。
    # 只尝试一次。以前这里会在失败后再用 PowerShell Start-Process 重试，结果在
    # 「已有以管理员权限运行的 Edge」的机器上连续触发系统的
    # 「现有实例正在以提升的权限运行，是否用普通权限重启」对话框，
    # 用户点"是"会关掉自己所有窗口。宁可退回标签页，也不反复弹窗。
    try:
        _clean_profile(profile)
        subprocess.Popen(args, close_fds=True)
        if _wait_browser(profile, kind, 10):
            return profile
    except Exception as e:
        print("[launch] 启动浏览器失败:", e)
    print("[launch] 独立应用窗口没起来（常见原因：系统里已有一个以管理员权限运行的")
    print("         Edge 实例，普通权限的窗口会被系统拦下）。改成在默认浏览器的新")
    print("         标签页里打开，功能完全一样；想用独立窗口，请用普通权限双击启动。")
    _kill_our_browser(profile, kind)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return None


def _kill_our_browser(profile, kind="edge"):
    """只清掉用本应用 profile 起的浏览器进程，不碰用户自己的窗口。"""
    name = {"edge": "msedge.exe", "chrome": "chrome.exe"}.get(kind, "msedge.exe")
    tag = os.path.basename(profile)
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
        "Where-Object { $_.CommandLine -like '*%s*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        % (name, tag)
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _clean_profile(profile):
    """清掉 profile 里可能导致 "Lock file can not be created" 的残留锁（只在没有活进程时调用）。"""
    for fn in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        p = os.path.join(profile, fn)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _wait_browser(profile, kind="edge", seconds=8):
    """轮询到浏览器进程出现（返回 True），超时返回 False。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _count_browser(profile, kind) > 0:
            return True
        time.sleep(0.8)
    return False


def _launch_via_powershell(exe, args):
    """用 PowerShell Start-Process 拉起浏览器；成功返回 True。"""
    def q(s):
        return "'" + str(s).replace("'", "''") + "'"

    arglist = ",".join(q(a) for a in args)
    ps = (
        "$ErrorActionPreference='Stop'; "
        "Start-Process -FilePath %s -ArgumentList %s; 'OK'"
        % (q(exe), arglist)
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = "OK" in (out.stdout or "")
        if not ok:
            print("[launch] Start-Process 失败:", (out.stderr or "").strip()[:200])
        return ok
    except Exception as e:
        print("[launch] Start-Process 异常:", e)
        return False


def _count_browser(profile, kind="edge"):
    """数有多少浏览器进程带着我们的 user-data-dir，返回数量（取不到就返回 -1）。"""
    name = "chrome.exe" if kind == "chrome" else "msedge.exe"
    key = profile.replace("\\", "\\")
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
        "ForEach-Object { $_.CommandLine } | "
        "Where-Object { $_ -and $_.Contains('%s') } | Measure-Object | "
        "Select-Object -ExpandProperty Count" % (name, key)
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        txt = (out.stdout or "").strip()
        return int(txt) if txt.isdigit() else -1
    except Exception:
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-window", action="store_true")
    # 默认在浏览器里开一个标签页（永不弹窗、永远可用）；想要无边框独立窗口用 --window。
    ap.add_argument("--window", action="store_true",
                    help="尝试用 Edge 的 --app 独立窗口打开（需要当前会话与已运行的浏览器权限一致）")
    ap.add_argument("--browser", default="edge", choices=["edge", "chrome"])
    ap.add_argument("--comfy", default="http://127.0.0.1:8188")
    a = ap.parse_args()

    py = pick_python()
    port = a.port or pick_port()
    cmd = [py, os.path.join(HERE, "server.py"), "--port", str(port), "--comfy", a.comfy, "--no-browser"]
    print("[launch] python:", py)
    print("[launch] cmd:", " ".join(cmd))

    server = subprocess.Popen(cmd, cwd=HERE)
    proc = None  # 浏览器 profile 路径（字符串）或旧式 Popen
    try:
        info = wait_health(port, timeout=45)
        if not info:
            print("[launch] 服务没起来（45s 超时），检查上面 server.py 的输出")
            return 2
        url = "http://127.0.0.1:%d/" % port
        print("[launch] 服务已就绪:", url, "| ComfyUI:", "在线" if info.get("comfy_reachable") else "离线")
        if a.no_window:
            print("[launch] --no-window：只跑服务，Ctrl+C 结束")
        elif a.window:
            proc = open_app_window(url, a.browser)
            if proc is None:
                print("[launch] 浏览器已交出去，服务继续在后台运行，Ctrl+C 结束")
        else:
            # 默认路径：交给系统默认浏览器开一个标签页。
            # 这样不会和"已以管理员权限运行的浏览器实例"打架，也不会弹
            # 「现有实例正在以提升的权限运行，是否用普通权限重启」的对话框。
            try:
                webbrowser.open(url)
            except Exception as e:
                print("[launch] 打开浏览器失败:", e, "→ 手动访问", url)
            print("[launch] 已在浏览器打开:", url, "（Ctrl+C 结束服务）")
        seen_window = False
        gone_streak = 0
        while True:
            if server.poll() is not None:
                print("[launch] server 已退出，码", server.returncode)
                break
            if isinstance(proc, str):
                n = _count_browser(proc, a.browser)
                if n > 0:
                    if not seen_window:
                        print("[launch] 应用窗口已就绪（浏览器进程 %d 个）" % n)
                    seen_window = True
                    gone_streak = 0
                elif seen_window and n == 0:
                    gone_streak += 1
                    if gone_streak >= 2:
                        print("[launch] 应用窗口已关闭，正在停止服务")
                        break
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("[launch] Ctrl+C，停止")
    finally:
        # proc 可能是 profile 字符串（浏览器已交给系统接管），只有 Popen 才需要 terminate
        for p in (proc, server):
            if p is None or isinstance(p, str):
                continue
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        print("[launch] 结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
