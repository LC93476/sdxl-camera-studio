# -*- coding: utf-8 -*-
"""SDXL 相机角度扫描：提交 -> 轮询 -> 下载 -> 拼对比图
用 ComfyUI 内嵌 python 跑：
  "D:/QUARK/ComfyUI-aki-v3.2/python/python.exe" tools/sweep_camera.py --mode azimuth
"""
import argparse, json, os, sys, time, uuid, urllib.request, urllib.parse

BASE = "http://127.0.0.1:8188"
CKPT = "WAI-Nsfw-Illustrious-17.safetensors"
LORA = "feiyue_xianxinglu_v2.safetensors"

BASE_POS = ("masterpiece, best quality, very aesthetic, absurdres, 1girl, solo, standing, "
            "arms behind back, long black hair, purple eyes, simple background, soft lighting")
BASE_NEG = ("lowres, worst quality, low quality, watermark, text, signature, bad anatomy, "
            "bad proportions, extra digits, fewer digits, missing fingers, extra limbs, blurry, out of frame")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))


def build(params, seed, w, h, steps, cfg, lora_strength, prefix):
    g = {
        "8": ("CheckpointLoaderSimple", {"ckpt_name": CKPT}),
        "9001": ("CameraAngleSDXL", dict(params)),
        "11": ("CLIPTextEncode", {"clip": ["10", 1], "text": ["9001", 0]}),
        "12": ("CLIPTextEncode", {"clip": ["10", 1], "text": ["9001", 1]}),
        "13": ("EmptyLatentImage", {"width": w, "height": h, "batch_size": 1}),
        "14": ("KSampler", {"model": ["10", 0], "positive": ["11", 0], "negative": ["12", 0],
                            "latent_image": ["13", 0], "seed": seed, "steps": steps, "cfg": cfg,
                            "sampler_name": "euler_ancestral", "scheduler": "karras", "denoise": 1.0}),
        "15": ("VAEDecode", {"samples": ["14", 0], "vae": ["8", 2]}),
        "110": ("SaveImage", {"images": ["15", 0], "filename_prefix": prefix}),
    }
    if lora_strength > 0:
        g["10"] = ("LoraLoader", {"model": ["8", 0], "clip": ["8", 1], "lora_name": LORA,
                                  "strength_model": lora_strength, "strength_clip": lora_strength})
    else:
        g["10"] = ("Identity", {})
        g.pop("10")
        g["11"][1]["clip"] = ["8", 1]
        g["12"][1]["clip"] = ["8", 1]
        g["14"][1]["model"] = ["8", 0]
    return {k: {"class_type": v[0], "inputs": v[1]} for k, v in g.items()}


def post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        print("HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:2000]), flush=True)
        raise


def get(path, timeout=60):
    return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))


def run_one(params, args, prefix, name):
    g = build(params, args.seed, args.width, args.height, args.steps, args.cfg, args.lora, prefix)
    r = post("/prompt", {"prompt": g, "client_id": str(uuid.uuid4())})
    pid = r["prompt_id"]
    t0 = time.time()
    while True:
        try:
            h = get("/history/" + pid)
        except Exception as e:
            print("  poll err", e, flush=True); time.sleep(3); continue
        if pid in h:
            e = h[pid]
            st = e.get("status", {})
            if st.get("status_str") != "success":
                print("  FAILED", name, json.dumps(st)[:600], flush=True)
                return None
            outs = []
            for nid, o in (e.get("outputs") or {}).items():
                for im in o.get("images", []):
                    outs.append(im)
            print("  done %-28s %5.1fs %s" % (name, time.time() - t0, [o["filename"] for o in outs]), flush=True)
            return outs
        time.sleep(2)


def fetch(im, outdir):
    q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                "type": im.get("type", "output")})
    data = urllib.request.urlopen(BASE + "/view?" + q, timeout=120).read()
    p = os.path.join(outdir, im["filename"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)
    return p


def grid(items, outpath, cols, cell_w=400, caption_h=64):
    from PIL import Image, ImageDraw, ImageFont
    def font(sz):
        for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\arial.ttf"):
            if os.path.exists(cand):
                try: return ImageFont.truetype(cand, sz)
                except Exception: pass
        return ImageFont.load_default()
    f = font(26); fs = font(19)
    ims = []
    for path, cap, sub in items:
        im = Image.open(path).convert("RGB")
        r = cell_w / im.width
        ims.append((im.resize((cell_w, max(1, int(im.height * r)))), cap, sub))
    cell_h = max(i[0].height for i in ims) + caption_h
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w + (cols + 1) * 8, rows * cell_h + (rows + 1) * 8), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    for i, (im, cap, sub) in enumerate(ims):
        cx = 8 + (i % cols) * (cell_w + 8)
        cy = 8 + (i // cols) * (cell_h + 8)
        sheet.paste(im, (cx, cy))
        d.text((cx + 6, cy + im.height + 4), cap, font=f, fill=(255, 235, 120))
        d.text((cx + 6, cy + im.height + 32), sub, font=fs, fill=(170, 200, 255))
    sheet.save(outpath, quality=92)
    print("GRID ->", outpath, sheet.size, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="azimuth")
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    ap.add_argument("--seed", type=int, default=20261117)
    ap.add_argument("--steps", type=int, default=24)
    ap.add_argument("--cfg", type=float, default=5.0)
    ap.add_argument("--width", type=int, default=832)
    ap.add_argument("--height", type=int, default=1216)
    ap.add_argument("--lora", type=float, default=0.8)
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=99)
    ap.add_argument("--tag", default="")
    ap.add_argument("--only", default="", help="只跑名字包含该子串的用例")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    base = dict(preset="自定义", strength=args.strength, weight_mode="tanh", azimuth_mode="8bin",
                elevation_mode="3bin", neg_strength=1.0, extra_positive=BASE_POS,
                extra_negative=BASE_NEG, _cfg_json="{}", pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0)
    cases = []
    if args.mode == "azimuth":
        for x in (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0):
            cases.append(("pos_x=%+.2f" % x, dict(base, pos_x=x)))
    elif args.mode == "elevation":
        for y in (-0.9, -0.6, -0.3, 0.0, 0.3, 0.6, 0.9):
            cases.append(("pos_y=%+.2f" % y, dict(base, pos_x=0.0, pos_y=y)))
    elif args.mode == "distance":
        for z in (0.9, 0.5, 0.1, -0.2, -0.5, -0.9):
            cases.append(("pos_z=%+.2f" % z, dict(base, pos_x=0.0, pos_z=z)))
    elif args.mode == "roll":
        for r in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            cases.append(("roll=%.1f" % r, dict(base, pos_x=0.0, roll=r)))
    elif args.mode == "weight":
        for s in (0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 2.0):
            cases.append(("strength=%.1f" % s, dict(base, pos_x=0.75, pos_y=0.35, strength=s)))
    elif args.mode == "preset":
        for p in ["正面平视中景", "正面仰视全身", "正面俯视中景", "右侧90度中景", "背面全身",
                  "左前方45度近景", "右后方45度中景", "虫瞰极限仰视", "鸟瞰极限俯视",
                  "荷兰角倾斜中景", "特写面部"]:
            cases.append((p, dict(base, preset=p)))
    elif args.mode == "ab":
        for nm, p in (("8bin_front", dict(base, pos_x=0.0)),
                      ("4bin_front", dict(base, pos_x=0.0, azimuth_mode="4bin")),
                      ("8bin_45", dict(base, pos_x=0.125)),
                      ("4bin_45", dict(base, pos_x=0.125, azimuth_mode="4bin")),
                      ("8bin_90", dict(base, pos_x=0.5)),
                      ("4bin_90", dict(base, pos_x=0.5, azimuth_mode="4bin"))):
            cases.append((nm, p))
    if args.only:
        cases = [c for c in cases if args.only in c[0]]
    cases = cases[: args.limit]

    try:
        from camera_core import compute_camera
        has_core = True
    except Exception as e:
        print("warn: 本地 core 不可导入 ->", e, flush=True)
        has_core = False

    items = []
    for name, params in cases:
        safe = name.replace("=", "").replace("+", "p").replace("-", "m").replace(".", "").replace("/", "_")
        prefix = "cam_sweep/%s_%s%s" % (args.mode, safe, ("_" + args.tag) if args.tag else "")
        cached = os.path.join(args.out, "raw", prefix.split("/")[-1] + "_00001_.png")
        if os.path.exists(cached):
            print("  cache %-28s %s" % (name, os.path.basename(cached)), flush=True)
            p = cached
        else:
            outs = run_one(params, args, prefix, name)
            if not outs:
                continue
            p = fetch(outs[0], os.path.join(args.out, "raw"))
        sub = ""
        if has_core:
            try:
                r = compute_camera(params["pos_x"], params["pos_y"], params["pos_z"], params["roll"],
                                   preset=params["preset"], strength=params["strength"],
                                   weight_mode=params["weight_mode"], azimuth_mode=params["azimuth_mode"],
                                   elevation_mode=params["elevation_mode"], neg_strength=params["neg_strength"],
                                   extra_positive="", extra_negative="")
                sub = r["camera_info"].get("bin_line", "") or ""
                with open(os.path.join(args.out, "prompts_%s.txt" % args.mode), "a", encoding="utf-8") as fh:
                    fh.write("### %s\nPOS: %s\nNEG: %s\n\n" % (name, r["positive"], r["negative"]))
            except Exception as e:
                sub = "core-err %s" % e
        items.append((p, name, sub))

    if items:
        grid(items, os.path.join(args.out, "grid_%s%s.jpg" % (args.mode, ("_" + args.tag) if args.tag else "")),
             9 if args.mode in ("azimuth", "preset") else 4)


if __name__ == "__main__":
    main()
