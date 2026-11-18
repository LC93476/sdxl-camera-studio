r"""检查工作目录能不能被当前 python 写进去（低完整性标签 / 只读盘体检）。

用法（用真正启动服务的那个 python 跑）：
    "D:/QUARK/ComfyUI-aki-v3.2/python/python.exe" tools\check_env.py

ComfyUI 便携版的 python.exe 是低完整性进程（S-1-16-4096），只能写带低完整性
标签的目录。搬项目、换盘之后就检查一下，比等服务端报 PermissionError 好。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [os.path.join(ROOT, "studio", "state"), os.path.join(ROOT, "studio", "out")]


def probe(directory):
    path = os.path.join(directory, ".write_probe")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(path)
        return True, ""
    except OSError as err:
        return False, "%s: %s" % (type(err).__name__, err)


def main():
    print("python: %s (%s)" % (sys.version.split()[0], sys.executable))
    bad = []
    for directory in TARGETS:
        ok, why = probe(directory)
        print(("  [ok]   " if ok else "  [FAIL] ") + directory + ("" if ok else "  " + why))
        if not ok:
            bad.append(directory)
    if bad:
        print("")
        print("目录不可写。若用的是 ComfyUI 便携版 python（低完整性进程），执行：")
        print('  icacls "%s" /setintegritylevel (OI)(CI)L' % ROOT)
        print("然后重开服务。只想放在别的盘也行，但那个盘上的目录要同样处理一次。")
        return 1
    print("")
    print("OK：state / out 都可写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
