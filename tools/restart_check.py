
import json, time, urllib.request
B="http://127.0.0.1:8188"
def get(u, t=20):
    return urllib.request.urlopen(B+u, timeout=t)
try:
    urllib.request.urlopen(urllib.request.Request(B+"/interrupt", data=b"{}", headers={"Content-Type":"application/json"}), timeout=20)
except Exception as e:
    print("interrupt:", e)
try:
    get("/manager/reboot", 5)
except Exception as e:
    print("reboot sent (conn closed ok):", type(e).__name__)
for i in range(60):
    time.sleep(3)
    try:
        r = get("/system_stats", 5); r.read()
    except Exception:
        continue
    try:
        oi = json.load(get("/object_info/CameraAngleSDXL", 20))
        ok = "CameraAngleSDXL" in oi
    except Exception:
        ok = False
    if ok:
        print("node up after %ds" % (i*3))
        break
for u in ("/extensions/sdxl_camera_angle/sdxl_camera_widget.js",
          "/extensions/sdxl_camera_angle/three.module.js"):
    try:
        r = get(u, 30); data = r.read()
        print("%-58s %s %d bytes  head=%s" % (u, r.status, len(data), data[:40]))
    except Exception as e:
        print("%-58s FAIL %s" % (u, e))
