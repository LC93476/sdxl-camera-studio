/**
 * SDXL 相机控制 —— three.js 3D 手动调摄像机 UI
 * 绑定节点 CameraAngleSDXL（widget 契约见 SPEC.md 2.6）。
 * 预览用纯 JS 复刻 SPEC 2.1 的 tanh 权重曲线 + 2.3 的 8bin 方位 + 2.4 高度 + 2.5 词表；
 * Python 端 core/camera_core.py 是唯一权威，这里只做拖动时的即时预览（可能差 ±0.01）。
 */
console.log("[SDXLCam] loading sdxl_camera_widget.js");
import * as THREE from "./three.module.js";
import { app } from "../../scripts/app.js";

// ============================================================
// 0. 词表（与 SPEC.md 2.5 一致，键名不许改；标签面板可覆盖）
// ============================================================
const V = {
  front: "front view, straight-on shot, looking at viewer",
  front_right: "three-quarter view from the right",
  right: "side view, profile view from the right",
  back_right: "three-quarter rear view from the right",
  back: "from behind, back view, seen from behind",
  back_left: "three-quarter rear view from the left",
  left: "side view, profile view from the left",
  front_left: "three-quarter view from the left",
  high: "high angle shot, from above",
  eye: "eye level shot",
  low: "low angle shot, from below, looking up",
  bird: "from above, high angle, looking down at viewer, top-down view",
  worm: "from below, low angle, looking up at viewer",
  ecu: "extreme close-up, macro shot",
  cu: "close-up, portrait shot",
  medium: "medium shot, waist up",
  cowboy: "cowboy shot, mid-thigh up",
  full: "full body shot",
  wide: "wide shot, long shot, full figure in frame",
  dutch: "dutch angle, canted framing, tilted horizon",
};

// 中文短名（面板与档位显示）
const CN = {
  front: "正面", front_right: "右前方", right: "右侧", back_right: "右后方",
  back: "背面", back_left: "左后方", left: "左侧", front_left: "左前方",
  high: "俯视", eye: "平视", low: "仰视", bird: "鸟瞰", worm: "虫瞰",
  ecu: "特写", cu: "近景", medium: "中景", cowboy: "牛仔", full: "全身", wide: "远景",
  dutch: "倾斜",
};

// 负向镜像（SPEC 2.5 负向镜像表）
const NEG = {
  front: "from behind, back view, seen from behind",
  front_right: "three-quarter rear view from the left",
  right: "side view from the left, profile from the left",
  back_right: "three-quarter view from the left",
  back: "front view, facing the camera",
  back_left: "three-quarter view from the right",
  left: "side view from the right",
  front_left: "three-quarter rear view from the right",
  high: "low angle shot, from below, looking up",
  low: "high angle shot, from above",
  eye: "high angle, low angle",
  bird: "worm's-eye view, extreme low angle",
  worm: "bird's-eye view, top-down shot",
  ecu: "wide shot, full body",
  cu: "wide shot, full body, distant",
  medium: "close-up, extreme close-up",
  cowboy: "close-up, extreme close-up",
  full: "close-up, portrait shot",
  wide: "close-up, portrait shot",
  dutch: "horizon level, straight horizon",
};

// 8 个方位 bin：j=0..7 的中心角 θ_j = -π + j*π/4（SPEC 2.3）
const AZ_KEYS = ["back", "back_left", "left", "front_left", "front", "front_right", "right", "back_right"];

// pos_z → 距离档位中心（UI 吸附用，与 SPEC 2.4 阈值对应）
const DIST_GEARS = [0.85, 0.45, 0.10, -0.17, -0.52, -0.85]; // ecu cu medium cowboy full wide
// 方位原始权重上限（= SDXL_TAGS.weights.azimuth.weight，camera_core.py 权威）
const AZ_WEIGHT = 10.0;

// 12 个预设（Python 端 presets.py 为准；这里只负责把 pose 写进 widget 并重绘）
const PRESETS = [
  ["正面平视中景", [0, 0, 0, 0]],
  ["正面仰视全身", [0, -0.55, -0.5, 0]],
  ["正面俯视中景", [0, 0.55, 0, 0]],
  ["右侧90度中景", [0.5, 0, 0, 0]],
  ["背面全身", [1, 0, -0.5, 0]],
  ["左前方45度近景", [-0.25, 0, 0.35, 0]],
  ["右后方45度中景", [0.75, 0, 0, 0]],
  ["虫瞰极限仰视", [0, -1, -0.85, 0], { elevation_mode: "5bin" }],
  ["鸟瞰极限俯视", [0, 1, -0.85, 0], { elevation_mode: "5bin" }],
  ["荷兰角倾斜中景", [0, 0, 0, 0.45]],
  ["特写面部", [0, 0, 0.85, 0]],
];

// ============================================================
// 1. 纯函数：SDXL 权重曲线 / 8bin / 提示词
// ============================================================
const r2 = v => Math.round(v * 100) / 100;
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

/** SPEC 2.1：raw∈[0.1,10] 压缩到 SDXL 友好区间 */
function sdxlWeight(raw, strength, curve) {
  raw = Math.max(1e-3, Number(raw) || 0);
  const st = Number(strength);
  strength = isNaN(st) ? 1 : st;
  if (curve === "raw") return r2(clamp(raw, 0.05, 2.0));
  if (curve === "linear") return r2(clamp(1 + 0.06 * (raw - 1) * strength, 0.05, 2.0));
  const S = 0.55 * strength, K = 3.3;
  const w = raw >= 1
    ? 1 + S * (raw - 1) / ((raw - 1) + K)
    : 1 - S * (1 - raw) / ((1 - raw) + K);
  return r2(clamp(w, 0.05, 2.0));
}

/** SPEC 2.2：短语内按逗号拆 tag，逐个加权重；w≈1 时裸写 */
function emit(phrase, w) {
  return String(phrase || "").split(",").map(s => s.trim()).filter(Boolean)
    .map(t => (Math.abs(w - 1) < 0.005 ? t : "(" + t + ":" + w.toFixed(2) + ")"));
}

/**
 * 一次算出正向 / 负向 / 档位信息。
 * o = { strength, curve, azimuthMode, elevationMode, negStrength, extraPos, extraNeg, vocab }
 */
function computeCamera(px, py, pz, roll, o) {
  const voc = o.vocab || V;
  const pos = [], neg = [], bins = [];
  const strength = o.strength, curve = o.curve || "tanh";
  const negS = Number(o.negStrength) || 0;

  // ---- 方位：8bin 双档线性混合（4bin = 原版 cos/sin 对照） ----
  const gate = clamp((1 - Math.abs(py)) / (1 - 0.9), 0, 1);
  const nat = [];
  if (o.azimuthMode === "4bin") {
    const a = clamp(px, -1, 1) * Math.PI;
    const d = {
      front: Math.max(0, Math.cos(a)), back: Math.max(0, -Math.cos(a)),
      right: Math.max(0, Math.sin(a)), left: Math.max(0, -Math.sin(a)),
    };
    const s = d.front + d.back + d.right + d.left;
    if (s > 0) for (const k in d) d[k] /= s;
    for (const k of ["front", "back", "left", "right"]) if (d[k] >= 0.06) nat.push([k, d[k]]);
  } else {
    const th = clamp(px, -1, 1) * Math.PI;
    const f = (th + Math.PI) / (Math.PI / 4);
    const fl = Math.floor(f);
    let i0 = fl % 8; if (i0 < 0) i0 += 8;
    const frac = f - fl;
    const pair = [[i0, 1 - frac], [(i0 + 1) % 8, frac]];
    for (const [j, nv] of pair) if (nv >= 0.06) nat.push([AZ_KEYS[j], nv]);
  }
  if (gate > 0) {
    for (const [k, nv] of nat) {
      const raw = nv * AZ_WEIGHT * gate;
      if (raw < 0.05) continue;
      const w = sdxlWeight(raw, strength, curve);
      pos.push(...emit(voc[k], w));
      bins.push([k, w, "az"]);
    }
  }

  // ---- 高度：3 档，5 档在 |py|>0.75 时启用 bird/worm ----
  let ek;
  if (o.elevationMode === "5bin" && Math.abs(py) > 0.75) ek = py > 0 ? "bird" : "worm";
  else ek = py > 0.2 ? "high" : py >= -0.2 ? "eye" : "low";
  const eraw = ek === "eye" ? 1.0 : Math.abs(py) * 10;
  pos.push(...emit(voc[ek], sdxlWeight(eraw, strength, curve)));
  bins.push([ek, sdxlWeight(eraw, strength, curve), "el"]);

  // ---- 距离：6 档跳变，raw = 1 + 0.5*|pz| ----
  // SPEC 2.4 阈值写的是 pz>0 才 medium，但 §3 验收要求 (0,0,0,0) 出 medium，
  // 故 medium 下界取 -0.2（相对原版 6 档的唯一改动，Python 端需一致）
  const dk = pz > 0.7 ? "ecu" : pz > 0.2 ? "cu" : pz >= 0 ? "medium"
    : pz > -0.35 ? "cowboy" : pz >= -0.7 ? "full" : "wide";
  const draw = 1 + 0.5 * Math.abs(pz);
  pos.push(...emit(voc[dk], sdxlWeight(draw, strength, curve)));
  bins.push([dk, sdxlWeight(draw, strength, curve), "di"]);

  // ---- 倾斜：roll>0 才输出 dutch angle，raw = 1 + 6*roll ----
  const dutch = roll > 0;
  if (dutch) {
    const traw = 1 + 6 * roll;
    pos.push(...emit(voc.dutch, sdxlWeight(traw, strength, curve)));
    bins.push(["dutch", sdxlWeight(traw, strength, curve), "tg"]);
  }

  // ---- 负向镜像：取相反方向短语，权重 1 + 0.15*neg_strength ----
  if (negS > 0) {
    const nw = r2(clamp(1 + 0.15 * negS, 0.05, 2.0));
    const seen = new Set();
    for (const [k] of bins) {
      const ph = NEG[k]; if (!ph) continue;
      for (const t of String(ph).split(",").map(s => s.trim()).filter(Boolean)) {
        if (seen.has(t)) continue; seen.add(t);
        neg.push("(" + t + ":" + nw.toFixed(2) + ")");
      }
    }
  }

  // ---- 额外提示词（原样拼接） ----
  const exP = String(o.extraPos || "").trim();
  const exN = String(o.extraNeg || "").trim();

  return {
    positive: [pos.join(", "), exP].filter(Boolean).join(", "),
    negative: [neg.join(", "), exN].filter(Boolean).join(", "),
    bins,
    info: { az: bins.find(b => b[2] === "az")?.[0] || "-", el: ek, di: dk, dutch },
  };
}

// ============================================================
// 2. CSS
// ============================================================
(function () {
  if (document.getElementById("sdxlcam-style")) return;
  const el = document.createElement("style"); el.id = "sdxlcam-style";
  el.textContent = [
    ".sc-w{display:flex;flex-direction:column;gap:4px;padding:4px;box-sizing:border-box;min-height:380px}",
    ".sc-w .sc-cw{position:relative;width:100%;flex-shrink:0;overflow:hidden;border-radius:8px}",
    ".sc-w .sc-cw canvas{display:block;width:100% !important;height:auto !important;aspect-ratio:560/500;border-radius:8px;cursor:grab;touch-action:none}",
    ".sc-w .sc-cw canvas:active{cursor:grabbing}",
    ".sc-w .sc-ol{position:absolute;left:0;right:0;top:0;padding:6px 8px;display:flex;justify-content:space-between;pointer-events:none;font-size:10px}",
    ".sc-w .sc-ol .h{color:rgba(255,255,255,0.5)}",
    ".sc-w .sc-ol .i{text-align:right}",
    ".sc-w .sc-ol .d{color:#ff8a4c;font-family:monospace}",
    ".sc-w .sc-ol .r{color:#c792ea;font-family:monospace}",
    ".sc-w .btn-r{display:flex;gap:6px;align-items:center;flex-wrap:wrap}",
    ".sc-w .btn-r button{background:#333;border:1px solid #555;color:#ddd;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:10px;line-height:1.6}",
    ".sc-w .btn-r button:hover{background:#444}",
    ".sc-w .sc-bin{font-size:10px;font-family:monospace;color:#8fd6c0;background:rgba(0,0,0,0.28);border:1px solid #3a4a48;border-radius:4px;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".sc-w .pr{width:100%;min-height:26px;max-height:60px;resize:vertical;font-size:10px;background:rgba(0,0,0,0.25);border:1px solid #444;color:#fff3b7;border-radius:4px;padding:3px 5px;font-family:monospace;box-sizing:border-box;overflow-x:hidden;word-break:break-all}",
    ".sc-w .pr.neg{color:#b7c6ff;border-color:#39404f;min-height:22px}",
    ".sc-w .ph{font-size:9px;color:rgba(255,255,255,0.3);text-align:center}",
    ".sc-w .sc-panel{display:none;border:1px solid #444;border-radius:6px;padding:6px;background:rgba(0,0,0,0.3);margin-top:4px;max-height:620px;overflow-y:auto}",
    ".sc-w .sc-panel.open{display:block}",
    ".sc-w .sc-gp{font-size:10px;color:#667eea;margin:6px 0 2px 0;font-weight:bold}",
    ".sc-w .sc-row{display:flex;align-items:center;gap:4px;margin:1px 0}",
    ".sc-w .sc-row span.k{width:60px;font-size:9px;color:rgba(255,255,255,0.6);flex-shrink:0}",
    ".sc-w .sc-row input{flex:1;min-width:0;font-size:9px;background:#222;border:1px solid #444;color:#ddd;border-radius:3px;padding:1px 4px}",
    ".sc-w .sc-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}",
    ".sc-w .sc-grid button{background:#2c2c3a;border:1px solid #4a4a5e;color:#cfd3e0;border-radius:4px;padding:4px 2px;cursor:pointer;font-size:10px;text-align:center}",
    ".sc-w .sc-grid button:hover{background:#3c3c52;border-color:#8f7fe0}",
    ".sc-w .sc-grid button.on{background:#4a3a6a;border-color:#8f7fe0;color:#fff}",
  ].join("\n");
  document.head.appendChild(el);
})();

// ============================================================
// 3. Extension
// ============================================================
app.registerExtension({
  name: "ComfyUI.SDXL.CameraAngle",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "CameraAngleSDXL") return;

    const orig = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      orig?.apply(this, arguments);
      const node = this;

      // ---- 隐藏 _cfg_json（标签覆盖，DOM 层隐藏但保留序列化） ----
      setTimeout(() => {
        const w = gw("_cfg_json");
        if (w && w.element) {
          w.computeSize = () => [0, 0];
          w.element.style.cssText = "display:none!important;height:0!important;min-height:0!important;padding:0!important;margin:0!important;border:none!important;overflow:hidden!important";
          if (w.element.parentNode) w.element.parentNode.style.display = "none";
        }
      }, 200);

      function gw(n) { return node.widgets?.find(w => w.name === n); }
      const num = n => parseFloat(gw(n)?.value ?? 0) || 0;
      const str = n => String(gw(n)?.value ?? "");

      const [ow, oh] = node.size;
      node.setSize([Math.max(ow, 380), Math.max(oh, 600)]);

      // ---- 容器 + DOM widget ----
      const container = document.createElement("div");
      container.className = "sc-w"; container.style.width = "100%";
      try { node.addDOMWidget("cam3d", "sdxl-cam-3d", container, { getMinHeight: () => 400, getMaxHeight: () => 1400, hideOnZoom: false, serialize: false }); }
      catch (e) { console.warn("[SDXLCam] addDOMWidget fail:", e); return; }

      // ============= three.js 场景 =============
      let CW = Math.max(container.clientWidth || 360, 280);
      let CH = Math.round(CW * 500 / 560);
      const scene = new THREE.Scene(); scene.background = new THREE.Color(0x0a0a14);
      const cam3d = new THREE.PerspectiveCamera(45, CW / CH, 0.1, 100);
      cam3d.position.set(4, 3.5, 4); cam3d.lookAt(0, 0.3, 0);
      const renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.setSize(CW, CH, false);
      renderer.outputColorSpace = THREE.SRGBColorSpace;

      const wrap = document.createElement("div"); wrap.className = "sc-cw"; container.prepend(wrap);
      wrap.appendChild(renderer.domElement);
      const ro = new ResizeObserver(() => {
        const nw = Math.max(container.clientWidth || 280, 280);
        if (nw !== CW) {
          CW = nw; CH = Math.round(CW * 500 / 560);
          renderer.setSize(CW, CH, false); cam3d.aspect = CW / CH; cam3d.updateProjectionMatrix();
        }
      });
      ro.observe(container);

      const ol = document.createElement("div"); ol.className = "sc-ol";
      ol.innerHTML = '<span class="h">拖拽</span><div class="i"><div class="d">正面 · 0°</div><div class="r"></div></div>';
      wrap.appendChild(ol);
      const hHint = ol.querySelector(".h"), hDir = ol.querySelector(".d"), hRoll = ol.querySelector(".r");
      hHint.title = "紫环粉点=方位 · 青弧青点=高度 · 黄点=距离";

      scene.add(new THREE.AmbientLight(0xffffff, 0.4));
      const ml = new THREE.DirectionalLight(0xffffff, 0.8); ml.position.set(5, 10, 5); scene.add(ml);
      const fl = new THREE.DirectionalLight(0xE93D82, 0.3); fl.position.set(-5, 5, -5); scene.add(fl);

      const CENTER = new THREE.Vector3(0, 0.5, 0), AZ_R = 1.8, EL_R = 1.4;
      scene.add(new THREE.GridHelper(5, 20, 0x1a1a2e, 0x12121a));

      // 人物卡
      const card = new THREE.Mesh(new THREE.BoxGeometry(1.2, 1.2, 0.02), new THREE.MeshBasicMaterial({ color: 0x3a3a4a }));
      card.position.copy(CENTER); scene.add(card);
      const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1.2, 1.2, 0.02)), new THREE.LineBasicMaterial({ color: 0xE93D82 }));
      frame.position.copy(CENTER); scene.add(frame);
      const gr = new THREE.Mesh(new THREE.RingGeometry(0.55, 0.58, 64), new THREE.MeshBasicMaterial({ color: 0xE93D82, transparent: true, opacity: 0.4, side: THREE.DoubleSide, depthWrite: false }));
      gr.position.set(0, 0.01, 0); gr.rotation.x = -Math.PI / 2; scene.add(gr);

      // 相机本体（紫粉）
      const camCone = new THREE.Mesh(new THREE.ConeGeometry(0.15, 0.4, 4), new THREE.MeshStandardMaterial({ color: 0xE93D82, emissive: 0xE93D82, emissiveIntensity: 0.5 }));
      scene.add(camCone);
      const camGlow = new THREE.Mesh(new THREE.SphereGeometry(0.08, 16, 16), new THREE.MeshBasicMaterial({ color: 0xff6ba8, transparent: true, opacity: 0.8 }));
      scene.add(camGlow);

      // 手柄 1：紫环 + 粉点 = 方位
      const azRing = new THREE.Mesh(new THREE.TorusGeometry(AZ_R, 0.04, 16, 100), new THREE.MeshBasicMaterial({ color: 0xE93D82, transparent: true, opacity: 0.7 }));
      azRing.rotation.x = Math.PI / 2; azRing.position.y = 0.02; scene.add(azRing);
      const azH = new THREE.Mesh(new THREE.SphereGeometry(0.16, 32, 32), new THREE.MeshStandardMaterial({ color: 0xE93D82, emissive: 0xE93D82, emissiveIntensity: 0.6 }));
      scene.add(azH);
      const azG = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 16), new THREE.MeshBasicMaterial({ color: 0xE93D82, transparent: true, opacity: 0.2 }));
      scene.add(azG);

      // 手柄 2：青弧 + 青点 = 高度
      const arcPts = [];
      for (let i = 0; i <= 32; i++) {
        const a = (-30 + 90 * i / 32) * Math.PI / 180;
        arcPts.push(new THREE.Vector3(-0.8, EL_R * Math.sin(a) + CENTER.y, EL_R * Math.cos(a)));
      }
      const arc = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(arcPts), 32, 0.04, 8, false), new THREE.MeshBasicMaterial({ color: 0x00FFD0, transparent: true, opacity: 0.8 }));
      scene.add(arc);
      const elH = new THREE.Mesh(new THREE.SphereGeometry(0.16, 32, 32), new THREE.MeshStandardMaterial({ color: 0x00FFD0, emissive: 0x00FFD0, emissiveIntensity: 0.6 }));
      scene.add(elH);
      const elG = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 16), new THREE.MeshBasicMaterial({ color: 0x00FFD0, transparent: true, opacity: 0.2 }));
      scene.add(elG);

      // 手柄 3：黄点 + 黄线 = 距离
      const distH = new THREE.Mesh(new THREE.SphereGeometry(0.15, 32, 32), new THREE.MeshStandardMaterial({ color: 0xFFB800, emissive: 0xFFB800, emissiveIntensity: 0.7 }));
      scene.add(distH);
      const distG = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 16), new THREE.MeshBasicMaterial({ color: 0xFFB800, transparent: true, opacity: 0.25 }));
      scene.add(distG);
      let distTube = null;

      // ---- 状态 ----
      const S = {
        px: 0, py: 0, pz: 0, rv: 0,
        azimuth: 0, elevation: 15, dist: 5.5,
        dragging: false, tgt: null, hovered: null, animId: null,
      };
      const VOCAB = JSON.parse(JSON.stringify(V)); // 运行时可被标签面板覆盖
      let cfgJson = null;

      function snapDist() {
        let idx = 0;
        for (let i = 1; i < DIST_GEARS.length; i++) if (Math.abs(S.pz - DIST_GEARS[i]) < Math.abs(S.pz - DIST_GEARS[idx])) idx = i;
        S.pz = DIST_GEARS[idx];
        S.dist = S.pz * 4.5 + 5.5;
      }

      // ---- _cfg_json 读取 / 写入（Python 侧契约：{"tags": {...}}） ----
      function loadCfg() {
        const w = gw("_cfg_json"); if (!w || !w.value || w.value === "{}") return;
        try {
          const d = JSON.parse(w.value);
          const t = d && d.tags;
          if (t && typeof t === "object") for (const k of Object.keys(t)) if (typeof t[k] === "string" && k in VOCAB) VOCAB[k] = t[k];
        } catch (_) {}
      }
      function saveCfg() {
        const w = gw("_cfg_json"); if (w) { w.value = JSON.stringify({ tags: VOCAB }); cfgJson = w.value; }
      }

      // ---- pos_z 吸附档位 ----
      (function hookZ() {
        const wz = gw("pos_z");
        if (!wz || wz._zHooked) return;
        let oldVal = parseFloat(wz.value) || 0;
        const origCb = wz.callback;
        wz.callback = function (v) {
          if (origCb) origCb.call(this, v);
          const dir = parseFloat(v) > oldVal ? 1 : -1;
          let idx = DIST_GEARS.indexOf(oldVal);
          if (idx < 0) {
            let best = 0;
            for (let i = 1; i < DIST_GEARS.length; i++) if (Math.abs(v - DIST_GEARS[i]) < Math.abs(v - DIST_GEARS[best])) best = i;
            wz.value = DIST_GEARS[best];
          } else {
            wz.value = DIST_GEARS[Math.max(0, Math.min(DIST_GEARS.length - 1, idx + dir))];
          }
          oldVal = parseFloat(wz.value) || 0;
        };
        wz._zHooked = true;
      })();

      function readW() {
        S.px = r2(num("pos_x")) || 0; S.py = r2(num("pos_y")) || 0;
        S.pz = r2(num("pos_z")) || 0; S.rv = r2(num("roll")) || 0;
        S.azimuth = (S.px * 180 + 360) % 360; S.elevation = S.py * 45 + 15; S.dist = S.pz * 4.5 + 5.5;
        // 注意：这里不做档位吸附，否则 pos_z=0 会被改成 0.10，与 Python 端预览不一致
      }
      function syncN() {
        const wx = gw("pos_x"); if (wx) wx.value = S.px;
        const wy = gw("pos_y"); if (wy) wy.value = S.py;
        const wz = gw("pos_z"); if (wz) wz.value = S.pz;
        const wr = gw("roll"); if (wr) wr.value = S.rv;
        if (node.graph) node.graph.setDirtyCanvas(true, true);
      }
      /** 手动改姿态即视为自定义：清掉预设，避免 Python 用预设覆盖 */
      function markCustom() {
        const wp = gw("preset");
        if (wp && wp.value && wp.value !== "自定义") { wp.value = "自定义"; if (node.graph) node.graph.setDirtyCanvas(true, true); }
      }

      readW(); setTimeout(() => { readW(); loadCfg(); refreshTags(); }, 220);
      node.onConfigure = (function (o) { return function () { if (o) o.apply(this, arguments); setTimeout(() => { readW(); loadCfg(); refreshTags(); }, 60); }; })(node.onConfigure);

      // ---- 预览元素（先建，upd() 用） ----
      const binEl = document.createElement("div"); binEl.className = "sc-bin"; binEl.textContent = "front / eye / medium";
      const prevEl = document.createElement("textarea"); prevEl.className = "pr"; prevEl.readOnly = true; prevEl.rows = 2;
      const negEl = document.createElement("textarea"); negEl.className = "pr neg"; negEl.readOnly = true; negEl.rows = 2;
      const ph = document.createElement("div"); ph.className = "ph";

      function opts() {
        return {
          strength: num("strength") || 1,
          curve: str("weight_mode") || "tanh",
          azimuthMode: str("azimuth_mode") || "8bin",
          elevationMode: str("elevation_mode") || "3bin",
          negStrength: num("neg_strength"),
          extraPos: str("extra_positive"),
          extraNeg: str("extra_negative"),
          vocab: VOCAB,
        };
      }

      // ---- 每帧更新 ----
      function upd() {
        if (!S.dragging) {
          S.px = r2(num("pos_x")); S.py = r2(num("pos_y"));
          S.pz = r2(num("pos_z")); S.rv = num("roll");
          S.azimuth = (S.px * 180 + 360) % 360; S.elevation = S.py * 45 + 15; S.dist = S.pz * 4.5 + 5.5;
        }
        const ar = S.azimuth * Math.PI / 180, er = S.elevation * Math.PI / 180;
        const vd = 2.6 - (S.dist / 10) * 2.0;
        const cx = vd * Math.sin(ar) * Math.cos(er), cy = CENTER.y + vd * Math.sin(er), cz = vd * Math.cos(ar) * Math.cos(er);
        camCone.position.set(cx, cy, cz); camCone.lookAt(CENTER); camCone.rotateX(Math.PI / 2);
        camGlow.position.copy(camCone.position);

        azH.position.set(AZ_R * Math.sin(ar), 0.16, AZ_R * Math.cos(ar)); azG.position.copy(azH.position);
        elH.position.set(-0.8, CENTER.y + EL_R * Math.sin(er), EL_R * Math.cos(er)); elG.position.copy(elH.position);
        const dt = 0.15 + (10 - S.dist) / 10 * 0.7;
        distH.position.lerpVectors(CENTER, camCone.position, dt); distG.position.copy(distH.position);

        if (distTube) { scene.remove(distTube); distTube.geometry.dispose(); distTube.material.dispose(); }
        distTube = new THREE.Mesh(
          new THREE.TubeGeometry(new THREE.LineCurve3(CENTER.clone(), camCone.position.clone()), 1, 0.025, 8, false),
          new THREE.MeshBasicMaterial({ color: 0xFFB800, transparent: true, opacity: 0.8 }));
        scene.add(distTube);

        // 覆盖层文字
        const ad = S.px * 180;
        hDir.textContent = Math.abs(S.px) > 0.85 ? "背面 · 180°" : S.px < -0.05 ? "左 " + Math.abs(ad).toFixed(0) + "°" : S.px > 0.05 ? "右 " + ad.toFixed(0) + "°" : "正面 · 0°";
        hRoll.textContent = S.rv > 0 ? "倾斜 " + (S.rv * 10).toFixed(2) + "°" : "";

        // 预览：正/负提示词 + 档位
        const r = computeCamera(S.px, S.py, S.pz, S.rv, opts());
        prevEl.value = r.positive;
        negEl.value = r.negative;
        const ib = r.info;
        binEl.textContent =
          (ib.az !== "-" ? ib.az : "—") + " / " + ib.el + " / " + ib.di + (ib.dutch ? " / dutch" : "") +
          "    " + (S.px * 90).toFixed(0) + "°  " + (S.py * 60).toFixed(0) + "°";
      }

      let alive = true;
      function anim() {
        if (!alive) return;
        S.animId = requestAnimationFrame(anim);
        gr.rotation.z += 0.005;
        try { renderer.render(scene, cam3d); } catch (_) {}
        upd();
      }
      requestAnimationFrame(anim);

      // ---- 鼠标交互 ----
      const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
      const hList = [{ m: azH, g: azG, n: "az" }, { m: elH, g: elG, n: "el" }, { m: distH, g: distG, n: "dist" }];
      const setScale = (h, s) => { h.m.scale.setScalar(s); if (h.g) h.g.scale.setScalar(s); };
      const canvas = renderer.domElement;

      canvas.addEventListener("pointerdown", e => {
        const r = canvas.getBoundingClientRect();
        mouse.x = (e.clientX - r.left) / r.width * 2 - 1;
        mouse.y = -(e.clientY - r.top) / r.height * 2 + 1;
        ray.setFromCamera(mouse, cam3d);
        for (const h of hList) {
          if (ray.intersectObject(h.m).length > 0) { S.dragging = true; S.tgt = h.n; setScale(h, 1.3); e.preventDefault(); return; }
        }
      });
      canvas.addEventListener("pointermove", e => {
        const r = canvas.getBoundingClientRect();
        mouse.x = (e.clientX - r.left) / r.width * 2 - 1;
        mouse.y = -(e.clientY - r.top) / r.height * 2 + 1;
        ray.setFromCamera(mouse, cam3d);
        if (!S.dragging) {
          let f = null;
          for (const h of hList) if (ray.intersectObject(h.m).length > 0) { f = h; break; }
          if (S.hovered && S.hovered !== f) setScale(S.hovered, 1.0);
          if (f) { setScale(f, 1.15); S.hovered = f; } else S.hovered = null;
          return;
        }
        const plane = new THREE.Plane(), pt = new THREE.Vector3();
        if (S.tgt === "az") {
          plane.setFromNormalAndCoplanarPoint(new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, 0));
          if (ray.ray.intersectPlane(plane, pt)) {
            let a = Math.atan2(pt.x, pt.z) * 180 / Math.PI; if (a < 0) a += 360;
            S.azimuth = Math.max(0, Math.min(360, a));
            S.px = r2(((S.azimuth + 180) % 360 - 180) / 180);
            markCustom(); syncN();
          }
        } else if (S.tgt === "el") {
          plane.setFromNormalAndCoplanarPoint(new THREE.Vector3(1, 0, 0), new THREE.Vector3(-0.8, 0, 0));
          if (ray.ray.intersectPlane(plane, pt)) {
            let a = Math.atan2(pt.y - CENTER.y, pt.z) * 180 / Math.PI;
            a = Math.max(-30, Math.min(60, a));
            S.elevation = a; S.py = r2((a - 15) / 45);
            markCustom(); syncN();
          }
        } else if (S.tgt === "dist") {
          S.dist = Math.max(1, Math.min(10, S.dist + (e.movementY || 0) * 0.05));
          S.pz = r2((S.dist - 5.5) / 4.5);
          markCustom(); syncN();
        }
      });
      canvas.addEventListener("pointerup", () => {
        if (!S.dragging) return;
        if (S.tgt === "dist") snapDist();
        S.dragging = false; S.tgt = null;
        if (S.hovered) setScale(S.hovered, 1.0);
        markCustom(); syncN();
      });
      canvas.addEventListener("pointercancel", () => { S.dragging = false; S.tgt = null; });
      canvas.addEventListener("contextmenu", e => e.preventDefault());

      // 滚轮：新版前端在捕获阶段 stopPropagation，改在 window 捕获阶段监听
      const onWheel = e => {
        const rc = canvas.getBoundingClientRect();
        if (e.clientX < rc.left || e.clientX > rc.right || e.clientY < rc.top || e.clientY > rc.bottom) return;
        e.preventDefault(); e.stopPropagation();
        let idx = DIST_GEARS.indexOf(S.pz);
        if (idx < 0) { snapDist(); idx = DIST_GEARS.indexOf(S.pz); }
        const dir = e.deltaY > 0 ? 1 : -1;
        const ni = Math.max(0, Math.min(DIST_GEARS.length - 1, (idx >= 0 ? idx : 3) + dir));
        S.pz = DIST_GEARS[ni]; S.dist = S.pz * 4.5 + 5.5;
        markCustom(); syncN();
      };
      window.addEventListener("wheel", onWheel, { capture: true, passive: false });

      // ---- 按钮行：归位 / 标签 / 预设 ----
      const bRow = document.createElement("div"); bRow.className = "btn-r"; container.appendChild(bRow);

      const rst = document.createElement("button"); rst.textContent = "归位"; rst.title = "姿态清零并回到自定义";
      rst.onclick = () => {
        S.azimuth = 0; S.elevation = 15; S.dist = 5.5;
        S.px = S.py = S.pz = S.rv = 0;
        const wp = gw("preset"); if (wp) wp.value = "自定义";
        syncN(); paintPreset();
      };
      bRow.appendChild(rst);

      const tagBtn = document.createElement("button"); tagBtn.textContent = "标签";
      bRow.appendChild(tagBtn);

      const preBtn = document.createElement("button"); preBtn.textContent = "预设";
      bRow.appendChild(preBtn);

      // ---- 标签面板 ----
      const tagPanel = document.createElement("div"); tagPanel.className = "sc-panel";
      container.appendChild(tagPanel);

      const TAG_GROUPS = [
        ["方位", ["front", "front_right", "right", "back_right", "back", "back_left", "left", "front_left"]],
        ["高度", ["high", "eye", "low", "bird", "worm"]],
        ["距离", ["ecu", "cu", "medium", "cowboy", "full", "wide"]],
        ["倾斜", ["dutch"]],
      ];
      const tagInputs = {};
      TAG_GROUPS.forEach(([gname, keys]) => {
        const g = document.createElement("div"); g.className = "sc-gp"; g.textContent = gname; tagPanel.appendChild(g);
        keys.forEach(k => {
          const row = document.createElement("div"); row.className = "sc-row";
          const lbl = document.createElement("span"); lbl.className = "k"; lbl.textContent = CN[k] + " " + k; row.appendChild(lbl);
          const inp = document.createElement("input"); inp.type = "text"; inp.value = VOCAB[k];
          inp.oninput = function () { VOCAB[k] = this.value; saveCfg(); };
          tagInputs[k] = inp; row.appendChild(inp); tagPanel.appendChild(row);
        });
      });
      const tagFoot = document.createElement("div"); tagFoot.className = "sc-row"; tagFoot.style.marginTop = "6px";
      const tagReset = document.createElement("button"); tagReset.textContent = "恢复默认词表";
      tagReset.style.cssText = "background:#333;border:1px solid #555;color:#ddd;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px";
      tagReset.onclick = () => {
        for (const k of Object.keys(V)) VOCAB[k] = V[k];
        refreshTags(); saveCfg();
      };
      tagFoot.appendChild(tagReset); tagPanel.appendChild(tagFoot);

      function refreshTags() {
        for (const k of Object.keys(tagInputs)) tagInputs[k].value = VOCAB[k];
      }

      // ---- 预设面板 ----
      const prePanel = document.createElement("div"); prePanel.className = "sc-panel";
      container.appendChild(prePanel);
      const preGrid = document.createElement("div"); preGrid.className = "sc-grid"; prePanel.appendChild(preGrid);
      const preNote = document.createElement("div"); preNote.className = "ph";
      preNote.style.marginTop = "4px";
      preNote.textContent = "预设由 Python 端权威计算，此处仅同步 3D 姿态";
      prePanel.appendChild(preNote);

      const preButtons = [];
      function applyPreset(name, pose, over) {
        const wp = gw("preset");
        if (wp) wp.value = name;
        if (over) for (const k of Object.keys(over)) { const w = gw(k); if (w) w.value = over[k]; }
        if (pose) {
          const [px, py, pz, rl] = pose;
          S.px = px; S.py = py; S.pz = pz; S.rv = rl;
          S.azimuth = (px * 180 + 360) % 360; S.elevation = py * 45 + 15; S.dist = pz * 4.5 + 5.5;
          syncN();
        }
        paintPreset();
      }
      function paintPreset() {
        const cur = gw("preset")?.value;
        preButtons.forEach(([name, el]) => el.classList.toggle("on", name === cur));
      }
      PRESETS.forEach(([name, pose, over]) => {
        const b = document.createElement("button"); b.textContent = name;
        b.onclick = () => applyPreset(name, pose, over);
        preGrid.appendChild(b); preButtons.push([name, b]);
      });
      const customBtn = document.createElement("button"); customBtn.textContent = "自定义（不改姿态）";
      customBtn.style.gridColumn = "1 / -1";
      customBtn.onclick = () => applyPreset("自定义", null);
      preGrid.appendChild(customBtn);
      preButtons.push(["自定义", customBtn]);
      paintPreset();

      function togglePanel(p, btn, openTxt, closeTxt) {
        const show = !p.classList.contains("open");
        if (p === tagPanel) { prePanel.classList.remove("open"); preBtn.textContent = "预设"; }
        if (p === prePanel) { tagPanel.classList.remove("open"); tagBtn.textContent = "标签"; }
        p.classList.toggle("open", show);
        btn.textContent = show ? closeTxt : openTxt;
      }
      tagBtn.onclick = () => togglePanel(tagPanel, tagBtn, "标签", "收起标签");
      preBtn.onclick = () => togglePanel(prePanel, preBtn, "预设", "收起预设");

      // ---- 底部：档位 + 预览 ----
      container.appendChild(binEl);
      container.appendChild(prevEl);
      container.appendChild(negEl);
      ph.textContent = "预览 → 正向 / 负向（Python 端为准）";
      container.appendChild(ph);

      // ---- 清理 ----
      const domWidget = node.widgets?.find(w => w.name === "cam3d");
      if (domWidget) {
        const baseRO = domWidget.onRemove;
        domWidget.onRemove = function () {
          alive = false;
          if (S.animId) cancelAnimationFrame(S.animId);
          try { ro.disconnect(); } catch (_) {}
          try { window.removeEventListener("wheel", onWheel, { capture: true }); } catch (_) {}
          try { renderer.dispose(); } catch (_) {}
          if (baseRO) baseRO.call(this);
        };
      }
    };
  },
});
