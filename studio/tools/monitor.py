# -*- coding: utf-8 -*-
"""轮询工作台 /api/progress，直到 done/error。用法: monitor.py <base> <prompt_id> [max_seconds]"""
import json
import sys
import time
import urllib.request

base = sys.argv[1].rstrip("/")
pid = sys.argv[2]
limit = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
t0 = time.time()
last = ""
while True:
    try:
        with urllib.request.urlopen(base + "/api/progress?prompt_id=" + pid, timeout=10) as r:
            d = json.load(r)
    except Exception as exc:
        d = {"state": "unknown", "error": str(exc)}
    # 只在状态/进度/节点真的变化时打印一行，避免每秒刷屏
    s = "%s %s %s" % (d.get("state"), d.get("progress"), d.get("node") or "")
    if s != last:
        print("%s (%.0fs)" % (s, float(d.get("elapsed") or 0)), flush=True)
        last = s
    if d.get("state") in ("done", "error"):
        print("FINAL " + json.dumps({k: d.get(k) for k in ("state", "images", "error", "elapsed")}, ensure_ascii=False))
        sys.exit(0)
    if time.time() - t0 > limit:
        print("TIMEOUT")
        sys.exit(2)
    time.sleep(3)
