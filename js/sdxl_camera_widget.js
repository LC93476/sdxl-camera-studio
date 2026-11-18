/**
 * SDXL 相机控制 —— three.js 3D 手动调摄像机 UI（v2）
 * 绑定节点 CameraAngleSDXL（widget 契约见 SPEC.md 2.6 / 第 7 节）。
 * 提示词算法全部来自 ./camera_shared.js（core/camera_core.py 的严格 JS 镜像），
 * Python 端是唯一权威；本文件只负责 3D 渲染、拖拽交互与即时预览。
 * 渲染是按需的：只有状态或相机变化才画一帧（无 60FPS 常驻自循环）。
 */
console.log("[SDXLCam] loading sdxl_camera_widget.js (v2 shared-algorithm)");
import * as THREE from "./three.module.js";
import { app } from "../../scripts/app.js";
import {
  computeCamera, V, NEG, PRESET_NAMES, presetParams,
  focalToFov, lensKey, r2, clamp,
} from "./camera_shared.js";
// 命名空间导入：budgetOf / clipTokens 若尚未导出，也不会让整个模块加载失败
import * as SHARED from "./camera_shared.js";

// ============================================================
// 0. 显示用中文短名（算法侧无中文，纯 UI 文案）
// ============================================================
const CN = {
  front: "正面", front_right: "右前方", right: "右侧", back_right: "右后方",
  back: "背面", back_left: "左后方", left: "左侧", front_left: "左前方",
  high: "俯视", eye: "平视", low: "仰视", bird: "鸟瞰", worm: "虫瞰",
  ecu: "特写", cu: "近景", medium: "中景", cowboy: "牛仔", full: "全身", wide: "远景",
  dutch: "倾斜", dutch_neg: "反倾斜",
  lens14: "14mm", lens24: "24mm", lens35: "35mm", lens50: "50mm", lens85: "85mm", lens135: "135mm",
  subject_left: "主体偏左", subject_right: "主体偏右", subject_center: "主体居中",
  subject_low: "主体偏下", subject_high: "主体偏上",
  subject_front: "正对身体", subject_turn: "转身", subject_away: "背对",
  gaze_viewer: "看镜头", gaze_away: "移开视线", gaze_over_shoulder: "回眸",
};

// ---- token 预算：优先用 camera_shared.js 的实现；该导出尚未落地时用本地近似兜底 ----
// 兜底口径与 studio/web/app.js 的 fallbackClipTokens 一致：按逗号切 tag、每词 1 token、长词(>6)记 2
const _clipApprox = text => {
  const s = String(text == null ? "" : text).trim();
  if (!s) return 0;
  let n = 0;
  s.split(",").forEach(tag => {
    tag.trim().split(/\s+/).forEach(w => { if (w) n += w.length > 6 ? 2 : 1; });
  });
  return n;
};
const clipTokens = SHARED.clipTokens || _clipApprox;
const budgetOf = SHARED.budgetOf || ((positive, negative) => {
  const p = clipTokens(positive), n = clipTokens(negative);
  return { positive: p, negative: n, total: p + n, limit: 75, warn: 65 };
});

// pos_z → 距离档位中心（拖拽 / 滚轮吸附用，与 camera_core.distance_key 阈值对应）
const DIST_GEARS = [0.85, 0.45, 0.10, -0.17, -0.52, -0.85]; // ecu cu medium cowboy full wide

// 姿态坐标（拖拽 / 快捷键 / 预设都只改这几个）
const POSE_KEYS = ["pos_x", "pos_y", "pos_z", "roll", "focal", "pan_x", "pan_y", "subject_yaw"];

// 快捷键 1..6：标准机位（px, py, pz, roll）
const HOTKEYS = {
  "1": { pos_x: 0, pos_y: 0, pos_z: 0, roll: 0 },          // 正面平视
  "2": { pos_x: 1, pos_y: 0, pos_z: 0, roll: 0 },          // 背面
  "3": { pos_x: -0.5, pos_y: 0, pos_z: 0, roll: 0 },       // 左正侧
  "4": { pos_x: 0.5, pos_y: 0, pos_z: 0, roll: 0 },        // 右正侧
  "5": { pos_x: 0, pos_y: 1, pos_z: -0.35, roll: 0 },      // 高俯
  "6": { pos_x: 0, pos_y: -1, pos_z: -0.35, roll: 0 },     // 低仰
};

// ============================================================
// 1. CSS
// ============================================================
(function () {
  if (document.getElementById("sdxlcam-style")) return;
  const el = document.createElement("style"); el.id = "sdxlcam-style";
  el.textContent = [
    ".sc-w{display:flex;flex-direction:column;gap:4px;padding:4px;box-sizing:border-box;min-height:400px}",
    ".sc-w .sc-cw{position:relative;width:100%;flex-shrink:0;overflow:hidden;border-radius:8px}",
    ".sc-w .sc-cw canvas{display:block;width:100% !important;height:auto !important;aspect-ratio:560/500;border-radius:8px;cursor:grab;touch-action:none}",
    ".sc-w .sc-cw canvas:active{cursor:grabbing}",
    ".sc-w .sc-ol{position:absolute;left:0;right:0;top:0;padding:6px 8px;display:flex;justify-content:space-between;pointer-events:none;font-size:10px}",
    ".sc-w .sc-ol .h{color:rgba(255,255,255,0.5)}",
    ".sc-w .sc-ol .i{text-align:right}",
    ".sc-w .sc-ol .d{color:#ff8a4c;font-family:monospace}",
    ".sc-w .sc-ol .r{color:#c792ea;font-family:monospace}",
    ".sc-w .sc-ol .l{color:#8fd6c0;font-family:monospace}",
    ".sc-w .sc-vf{position:absolute;left:0;right:0;top:0;bottom:0;pointer-events:none;display:none}",
    ".sc-w .sc-vf.on{display:block}",
    ".sc-w .sc-vf i{position:absolute;background:rgba(255,255,255,0.18)}",
    ".sc-w .sc-vf i.v{top:0;bottom:0;width:1px}",
    ".sc-w .sc-vf i.h{left:0;right:0;height:1px}",
    ".sc-w .sc-vf i.hz{left:0;right:0;height:0;border-top:1px dashed rgba(143,214,192,0.45);background:none}",
    ".sc-w .sc-vf b{position:absolute;inset:6px;border:1px solid rgba(255,255,255,0.14);border-radius:4px;display:block}",
    ".sc-w .btn-r{display:flex;gap:6px;align-items:center;flex-wrap:wrap}",
    ".sc-w .btn-r button{background:#333;border:1px solid #555;color:#ddd;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:10px;line-height:1.6}",
    ".sc-w .btn-r button:hover{background:#444}",
    ".sc-w .btn-r button.on{background:#4a3a6a;border-color:#8f7fe0;color:#fff}",
    ".sc-w .sc-bin{font-size:10px;font-family:monospace;color:#8fd6c0;background:rgba(0,0,0,0.28);border:1px solid #3a4a48;border-radius:4px;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".sc-w .sc-tok{font-size:9px;font-family:monospace;padding:0 6px;color:rgba(143,214,192,0.75);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".sc-w .sc-bin2{font-size:9px;font-family:monospace;color:rgba(143,214,192,0.6);padding:0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".sc-w .pr{width:100%;min-height:26px;max-height:60px;resize:vertical;font-size:10px;background:rgba(0,0,0,0.25);border:1px solid #444;color:#fff3b7;border-radius:4px;padding:3px 5px;font-family:monospace;box-sizing:border-box;overflow-x:hidden;word-break:break-all}",
    ".sc-w .pr.neg{color:#b7c6ff;border-color:#39404f;min-height:22px}",
    ".sc-w .ph{font-size:9px;color:rgba(255,255,255,0.3);text-align:center}",
    ".sc-w .sc-panel{display:none;border:1px solid #444;border-radius:6px;padding:6px;background:rgba(0,0,0,0.3);margin-top:4px;max-height:620px;overflow-y:auto}",
    ".sc-w .sc-panel.open{display:block}",
    ".sc-w .sc-gp{font-size:10px;color:#667eea;margin:6px 0 2px 0;font-weight:bold}",
    ".sc-w .sc-row{display:flex;align-items:center;gap:4px;margin:1px 0}",
    ".sc-w .sc-row span.k{width:70px;font-size:9px;color:rgba(255,255,255,0.6);flex-shrink:0}",
    ".sc-w .sc-row input{flex:1;min-width:0;font-size:9px;background:#222;border:1px solid #444;color:#ddd;border-radius:3px;padding:1px 4px}",
    ".sc-w .sc-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}",
    ".sc-w .sc-grid button{background:#2c2c3a;border:1px solid #4a4a5e;color:#cfd3e0;border-radius:4px;padding:4px 2px;cursor:pointer;font-size:10px;text-align:center}",
    ".sc-w .sc-grid button:hover{background:#3c3c52;border-color:#8f7fe0}",
    ".sc-w .sc-grid button.on{background:#4a3a6a;border-color:#8f7fe0;color:#fff}",
  ].join("\n");
  document.head.appendChild(el);
})();

// ============================================================
// 2. Extension
// ============================================================
app.registerExtension({
  name: "ComfyUI.SDXL.CameraAngle",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "CameraAngleSDXL") return;

    const orig = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      orig?.apply(this, arguments);
      const node = this;

      const gw = n => node.widgets?.find(w => w.name === n);
      const num = n => {
        const w = gw(n); if (!w) return 0;
        const v = parseFloat(w.value); return isNaN(v) ? 0 : v;
      };
      const str = n => String(gw(n)?.value ?? "");
      // 额外提示词是字符串 widget；非字符串（脏数据）一律当空，避免拼出 "..., 0"
      const extraStr = n => { const v = gw(n)?.value; return typeof v === "string" ? v : ""; };
      const hasW = n => !!gw(n);

      // ---- 隐藏 _cfg_json（标签覆盖，DOM 层隐藏但保留序列化） ----
      setTimeout(() => {
        const w = gw("_cfg_json");
        if (w && w.element) {
          w.computeSize = () => [0, 0];
          w.element.style.cssText = "display:none!important;height:0!important;min-height:0!important;padding:0!important;margin:0!important;border:none!important;overflow:hidden!important";
          if (w.element.parentNode) w.element.parentNode.style.display = "none";
        }
      }, 200);

      const [ow, oh] = node.size;
      node.setSize([Math.max(ow, 400), Math.max(oh, 640)]);

      // ---- 容器 + DOM widget ----
      const container = document.createElement("div");
      container.className = "sc-w"; container.style.width = "100%";
      try {
        node.addDOMWidget("cam3d", "sdxl-cam-3d", container, {
          getMinHeight: () => 400, getMaxHeight: () => 1400, hideOnZoom: false, serialize: false,
        });
      } catch (e) { console.warn("[SDXLCam] addDOMWidget fail:", e); return; }

      // ============= three.js 场景 =============
      let CW = Math.max(container.clientWidth || 360, 280);
      let CH = Math.round(CW * 500 / 560);
      const scene = new THREE.Scene(); scene.background = new THREE.Color(0x0a0a14);
      const cam3d = new THREE.PerspectiveCamera(45, CW / CH, 0.1, 100);
      cam3d.position.set(4, 3.5, 4); cam3d.lookAt(0, 0.3, 0);
      const renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(CW, CH, false);
      renderer.outputColorSpace = THREE.SRGBColorSpace;

      const wrap = document.createElement("div"); wrap.className = "sc-cw"; container.prepend(wrap);
      wrap.appendChild(renderer.domElement);
      const ro = new ResizeObserver(() => {
        const nw = Math.max(container.clientWidth || 280, 280);
        if (nw !== CW) {
          CW = nw; CH = Math.round(CW * 500 / 560);
          renderer.setSize(CW, CH, false); cam3d.aspect = CW / CH; cam3d.updateProjectionMatrix();
          scheduleRender();
        }
      });
      ro.observe(container);

      const ol = document.createElement("div"); ol.className = "sc-ol";
      ol.innerHTML = '<span class="h">拖拽 · 1-6 机位 · R 重置 · G 九宫格 · F 视锥</span>'
        + '<div class="i"><div class="d">正面 · 0°</div><div class="r"></div><div class="l"></div></div>';
      wrap.appendChild(ol);
      const hHint = ol.querySelector(".h"), hDir = ol.querySelector(".d"),
        hRoll = ol.querySelector(".r"), hLens = ol.querySelector(".l");
      hHint.title = "紫环粉点=方位 · 青弧青点=高度 · 黄点=距离 · 滚轮=距离档位";

      // 取景框：三分法九宫格 + 地平线（按钮 / G 开关）
      const vf = document.createElement("div"); vf.className = "sc-vf";
      vf.innerHTML = '<b></b>'
        + '<i class="v" style="left:33.33%"></i><i class="v" style="left:66.66%"></i>'
        + '<i class="h" style="top:33.33%"></i><i class="h" style="top:66.66%"></i>'
        + '<i class="hz" style="top:50%"></i>';
      wrap.appendChild(vf);

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

      // 主体朝向箭头（脚下，跟 subject_yaw 转）+ 视线锥
      const subjArrow = new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 0.03, 0), 0.95, 0x7fe0a8, 0.24, 0.14);
      scene.add(subjArrow);
      const gazeCone = new THREE.Mesh(
        new THREE.ConeGeometry(0.2, 0.5, 16, 1, true),
        new THREE.MeshBasicMaterial({ color: 0x7fe0ff, transparent: true, opacity: 0.32, side: THREE.DoubleSide, depthWrite: false }));
      scene.add(gazeCone);

      // 相机本体（紫粉）
      const camCone = new THREE.Mesh(new THREE.ConeGeometry(0.15, 0.4, 4), new THREE.MeshStandardMaterial({ color: 0xE93D82, emissive: 0xE93D82, emissiveIntensity: 0.5 }));
      scene.add(camCone);
      const camGlow = new THREE.Mesh(new THREE.SphereGeometry(0.08, 16, 16), new THREE.MeshBasicMaterial({ color: 0xff6ba8, transparent: true, opacity: 0.8 }));
      scene.add(camGlow);

      // 视锥线框（随 focal 的 FOV 收放）
      const FR_LEN = 2.4;
      const frGeo = new THREE.BufferGeometry();
      frGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(16 * 3), 3));
      const frustumLines = new THREE.LineSegments(frGeo, new THREE.LineBasicMaterial({ color: 0x8f7fe0, transparent: true, opacity: 0.45 }));
      scene.add(frustumLines);

      // 手柄 1：紫环 + 粉点 = 方位
      const azRing = new THREE.Mesh(new THREE.TorusGeometry(AZ_R, 0.04, 16, 100), new THREE.MeshBasicMaterial({ color: 0xE93D82, transparent: true, opacity: 0.7 }));
      azRing.rotation.x = Math.PI / 2; azRing.position.y = 0.02; scene.add(azRing);
      const mkHandle = (color, emis) => new THREE.Mesh(new THREE.SphereGeometry(0.16, 32, 32), new THREE.MeshStandardMaterial({ color: color, emissive: emis, emissiveIntensity: 0.6 }));
      const mkGlow = color => new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 16), new THREE.MeshBasicMaterial({ color: color, transparent: true, opacity: 0.2 }));
      const azH = mkHandle(0xE93D82, 0xE93D82); scene.add(azH);
      const azG = mkGlow(0xE93D82); scene.add(azG);

      // 手柄 2：青弧 + 青点 = 高度
      const arcPts = [];
      for (let i = 0; i <= 32; i++) {
        const a = (-78 + 156 * i / 32) * Math.PI / 180;
        arcPts.push(new THREE.Vector3(-0.8, EL_R * Math.sin(a) + CENTER.y, EL_R * Math.cos(a)));
      }
      const arc = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(arcPts), 32, 0.04, 8, false), new THREE.MeshBasicMaterial({ color: 0x00FFD0, transparent: true, opacity: 0.8 }));
      scene.add(arc);
      const elH = mkHandle(0x00FFD0, 0x00FFD0); scene.add(elH);
      const elG = mkGlow(0x00FFD0); scene.add(elG);

      // 手柄 3：黄点 + 黄线 = 距离
      const distH = mkHandle(0xFFB800, 0xFFB800); scene.add(distH);
      const distG = mkGlow(0xFFB800); distG.material.opacity = 0.25; scene.add(distG);
      // 距离连线：单位圆柱只建一次，之后只改 position / quaternion / scale.y（不再每帧 new + dispose）
      const distTube = new THREE.Mesh(
        new THREE.CylinderGeometry(0.025, 0.025, 1, 8, 1, true),
        new THREE.MeshBasicMaterial({ color: 0xFFB800, transparent: true, opacity: 0.8 }));
      scene.add(distTube);
      const _tubeUp = new THREE.Vector3(0, 1, 0);
      const _tubeDir = new THREE.Vector3();

      // ---- 状态 ----
      const S = {
        px: 0, py: 0, pz: 0, rv: 0,
        focal: 50, panx: 0, pany: 0, yaw: 0, gaze: "auto",
        azimuth: 0, elevation: 0, dist: 5.5,
        dragging: false, tgt: null, hovered: null, rafId: null, rafPending: false,
        gazeKey: "gaze_viewer", hoverCanvas: false, showGrid: true, showFrustum: true,
      };
      const VOCAB = Object.assign({}, V);       // 正向词表（标签面板 / _cfg_json 可覆盖）
      const VOCAB_NEG = Object.assign({}, NEG); // 负向镜像
      let tagVer = 0;

      const azDeg = px => (px * 180 + 360) % 360;
      // py=-1..1 → -78..78°（0 = 平视），与 core 的 pos_y 对称语义一致
      const elDeg = py => py * 78;
      const distV = pz => pz * 4.5 + 5.5;

      function snapDist() {
        let idx = 0;
        for (let i = 1; i < DIST_GEARS.length; i++) if (Math.abs(S.pz - DIST_GEARS[i]) < Math.abs(S.pz - DIST_GEARS[idx])) idx = i;
        S.pz = DIST_GEARS[idx];
        S.dist = distV(S.pz);
      }

      // ---- _cfg_json：{"tags": {正向词..., "neg": {...}}} ----
      function cfgObj() {
        return { tags: Object.assign({}, VOCAB, { neg: VOCAB_NEG }) };
      }
      function loadCfg() {
        const w = gw("_cfg_json"); if (!w || !w.value || w.value === "{}") return;
        let d = null;
        try { d = JSON.parse(w.value); } catch (_) { return; }
        const t = d && d.tags;
        if (!t || typeof t !== "object") return;
        for (const k of Object.keys(t)) {
          if (k === "neg" && t.neg && typeof t.neg === "object") { Object.assign(VOCAB_NEG, t.neg); continue; }
          if (typeof t[k] === "string" && k in VOCAB) VOCAB[k] = t[k];
        }
        tagVer++;
      }
      function saveCfg() {
        const w = gw("_cfg_json");
        if (w) w.value = JSON.stringify(cfgObj());
        tagVer++;
      }

      // ---- pos_z 吸附档位（数字 widget 步进） ----
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
        S.px = r2(num("pos_x")); S.py = r2(num("pos_y"));
        S.pz = r2(num("pos_z")); S.rv = r2(num("roll"));
        S.focal = num("focal") || 50; S.panx = num("pan_x"); S.pany = num("pan_y");
        S.yaw = num("subject_yaw"); S.gaze = str("gaze_mode") || "auto";
        S.azimuth = azDeg(S.px); S.elevation = elDeg(S.py); S.dist = distV(S.pz);
        // 注意：读回时不做档位吸附，否则 pos_z=0 会被改成 0.10，与 Python 预览不一致
      }
      function syncN() {
        const put = (k, v) => { const w = gw(k); if (w) w.value = v; };
        put("pos_x", S.px); put("pos_y", S.py); put("pos_z", S.pz); put("roll", S.rv);
        if (node.graph) node.graph.setDirtyCanvas(true, true);
        markDirty();
      }
      /** 手动改姿态即视为自定义：清掉预设，避免 Python 用预设覆盖 */
      function markCustom() {
        const wp = gw("preset");
        if (wp && wp.value && wp.value !== "自定义") {
          wp.value = "自定义";
          if (node.graph) node.graph.setDirtyCanvas(true, true);
        }
      }

      // ---- 任意 widget 变化都标记重绘：按需渲染下 HUD 也不会滞后 ----
      (function hookWidgets() {
        (node.widgets || []).forEach(w => {
          if (w._scHooked) return;
          const oc = w.callback;
          w.callback = function (v) {
            if (oc) oc.apply(this, arguments);
            readW(); markDirty();
          };
          w._scHooked = true;
        });
      })();

      readW();
      setTimeout(() => { readW(); loadCfg(); refreshTags(); markDirty(); }, 220);
      node.onConfigure = (function (o) {
        return function () {
          if (o) o.apply(this, arguments);
          setTimeout(() => { readW(); loadCfg(); refreshTags(); scheduleRender(); }, 60);
        };
      })(node.onConfigure);

      // ---- 底部元素 ----
      const binEl = document.createElement("div"); binEl.className = "sc-bin";
      binEl.textContent = "front / eye / medium";
      const binEl2 = document.createElement("div"); binEl2.className = "sc-bin2";
      const tokEl = document.createElement("div"); tokEl.className = "sc-tok"; tokEl.textContent = "— / 75 tok";
      const prevEl = document.createElement("textarea"); prevEl.className = "pr"; prevEl.readOnly = true; prevEl.rows = 2;
      const negEl = document.createElement("textarea"); negEl.className = "pr neg"; negEl.readOnly = true; negEl.rows = 2;
      const ph = document.createElement("div"); ph.className = "ph";

      function opts() {
        return {
          preset: str("preset") || "自定义",
          strength: hasW("strength") ? num("strength") : 1,
          weight_mode: str("weight_mode") || "tanh",
          azimuth_mode: str("azimuth_mode") || "8bin",
          elevation_mode: str("elevation_mode") || "3bin",
          neg_strength: hasW("neg_strength") ? num("neg_strength") : 0,
          focal: S.focal, pan_x: S.panx, pan_y: S.pany,
          subject_yaw: S.yaw, gaze_mode: S.gaze,
          extra_positive: extraStr("extra_positive"),
          extra_negative: extraStr("extra_negative"),
          cfg: cfgObj(),
        };
      }

      // ---- 相机 FOV 跟随 focal（水平 36mm 全画幅 → 画布纵横比换算成竖直 FOV） ----
      let lastFocal = -1;
      function applyFocal(focal) {
        if (focal === lastFocal) return;
        lastFocal = focal;
        const hf = focalToFov(focal) * Math.PI / 180;
        const vf = 2 * Math.atan(Math.tan(hf / 2) / (CW / CH)) * 180 / Math.PI;
        cam3d.fov = clamp(vf, 1, 170);
        cam3d.updateProjectionMatrix();
      }

      // ---- 视锥线框 ----
      const _v3a = new THREE.Vector3(), _v3b = new THREE.Vector3();
      function updateFrustum(pos) {
        if (!S.showFrustum) return;
        const dir = _v3a.copy(CENTER).sub(pos).normalize();
        const right = _v3b.copy(dir).cross(new THREE.Vector3(0, 1, 0));
        if (right.lengthSq() < 1e-6) right.set(1, 0, 0);
        right.normalize();
        const up = new THREE.Vector3().crossVectors(right, dir).normalize();
        const hf = focalToFov(S.focal) * Math.PI / 180;
        const hw = Math.tan(hf / 2) * FR_LEN, hh = hw / (CW / CH);
        const c = pos.clone().addScaledVector(dir, FR_LEN);
        const p1 = c.clone().addScaledVector(right, -hw).addScaledVector(up, -hh);
        const p2 = c.clone().addScaledVector(right, hw).addScaledVector(up, -hh);
        const p3 = c.clone().addScaledVector(right, hw).addScaledVector(up, hh);
        const p4 = c.clone().addScaledVector(right, -hw).addScaledVector(up, hh);
        const segs = [[pos, p1], [pos, p2], [pos, p3], [pos, p4],
          [p1, p2], [p2, p3], [p3, p4], [p4, p1]];
        const arr = frGeo.attributes.position.array;
        let i = 0;
        for (const [a, b] of segs) {
          arr[i++] = a.x; arr[i++] = a.y; arr[i++] = a.z;
          arr[i++] = b.x; arr[i++] = b.y; arr[i++] = b.z;
        }
        frGeo.attributes.position.needsUpdate = true;
      }

      // ---- 预览（只在参数变化时重算） ----
      let lastSig = "";
      function updatePreview() {
        const sig = [S.px, S.py, S.pz, S.rv, S.focal, S.panx, S.pany, S.yaw, S.gaze,
          num("strength"), str("weight_mode"), str("azimuth_mode"), str("elevation_mode"),
          num("neg_strength"), str("extra_positive"), str("extra_negative"), tagVer].join("|");
        if (sig === lastSig) return;
        lastSig = sig;
        const r = computeCamera(S.px, S.py, S.pz, S.rv, opts());
        prevEl.value = r.positive;
        negEl.value = r.negative;
        // token 预算：Chunk 0 看图的是正向段，故阈值只卡 positive（> 65 橙 / > 75 红），与 studio 一致
        const bg = budgetOf(r.positive, r.negative) || {};
        const pt = Math.round(Number(bg.positive) || 0), nt = Math.round(Number(bg.negative) || 0);
        const lim = Math.round(Number(bg.limit) || 75), wn = Math.round(Number(bg.warn) || 65);
        tokEl.textContent = pt + " / " + lim + " tok · N " + nt
          + (pt > lim ? " · 超 Chunk 0 上限" : (pt > wn ? " · 接近上限" : ""));
        tokEl.style.color = pt > lim ? "#d0453a" : (pt > wn ? "#d08b23" : "rgba(143,214,192,0.75)");
        const ib = r.info;
        binEl.textContent = (ib.bin || "—") + " / " + ib.elevation + " / " + ib.distance
          + (ib.dutch ? " / dutch" : "");
        binEl2.textContent = ChineseLine(ib);
        S.gazeKey = ib.gaze;   // 视线锥朝向复用同一次计算，避免每帧算两遍
      }

      /** 档位行：right / eye / medium → 中文注解 + 焦距 / 构图 / 主体 */
      function ChineseLine(ib) {
        const parts = [];
        parts.push("方位 " + (CN[ib.bin] || ib.bin) + " " + ib.azimuth_deg.toFixed(0) + "°");
        parts.push("高度 " + (CN[ib.elevation] || ib.elevation));
        parts.push("距离 " + (CN[ib.distance] || ib.distance));
        parts.push(ib.focal.toFixed(0) + "mm / FOV " + ib.fov_deg.toFixed(1) + "°");
        if (ib.dutch) parts.push("倾斜 " + ib.roll_deg.toFixed(0) + "°");
        if (ib.framing && ib.framing.length) parts.push(...ib.framing.map(k => CN[k] || k));
        if (ib.subject) parts.push(CN[ib.subject] || ib.subject);
        if (ib.gaze) parts.push(CN[ib.gaze] || ib.gaze);
        return parts.join(" · ");
      }

      // ---- 每帧更新 ----
      function upd() {
        if (!S.dragging) readW();
        applyFocal(S.focal);

        const ar = S.azimuth * Math.PI / 180, er = S.elevation * Math.PI / 180;
        const vd = 2.6 - (S.dist / 10) * 2.0;
        const cx = vd * Math.sin(ar) * Math.cos(er), cy = CENTER.y + vd * Math.sin(er), cz = vd * Math.cos(ar) * Math.cos(er);
        camCone.position.set(cx, cy, cz); camCone.lookAt(CENTER); camCone.rotateX(Math.PI / 2);
        camGlow.position.copy(camCone.position);

        azH.position.set(AZ_R * Math.sin(ar), 0.16, AZ_R * Math.cos(ar)); azG.position.copy(azH.position);
        elH.position.set(-0.8, CENTER.y + EL_R * Math.sin(er), EL_R * Math.cos(er)); elG.position.copy(elH.position);
        const dt = 0.15 + (10 - S.dist) / 10 * 0.7;
        distH.position.lerpVectors(CENTER, camCone.position, dt); distG.position.copy(distH.position);

        // 距离连线：只更新变换（position/quaternion/scale），几何体复用
        _tubeDir.copy(camCone.position).sub(CENTER);
        const tlen = _tubeDir.length();
        distTube.visible = tlen > 1e-4;
        if (distTube.visible) {
          distTube.position.copy(CENTER).addScaledVector(_tubeDir, 0.5);
          distTube.quaternion.setFromUnitVectors(_tubeUp, _tubeDir.normalize());
          distTube.scale.set(1, tlen, 1);
        }

        updateFrustum(new THREE.Vector3(cx, cy, cz));

        // 主体朝向箭头 + 视线锥（跟 subject_yaw / gaze_mode 转）
        const yawTh = S.yaw * Math.PI;   // subject_yaw ±1 → ±180°，与 core 世界系一致
        const fwd = new THREE.Vector3(Math.sin(yawTh), 0, Math.cos(yawTh));
        subjArrow.position.set(0, 0.03, 0); subjArrow.setDirection(fwd);
        const head = new THREE.Vector3(0, CENTER.y + 0.45, 0);
        const toCam = camCone.position.clone().sub(head).normalize();
        const gz = S.gazeKey;
        let gdir;
        if (gz === "gaze_viewer") gdir = toCam.clone();
        else if (gz === "gaze_over_shoulder") gdir = toCam.clone().add(fwd.clone().multiplyScalar(0.6)).normalize();
        else gdir = toCam.clone().negate().normalize();
        gdir.y = clamp(gdir.y, -0.5, 0.5); gdir.normalize();
        gazeCone.position.copy(head).addScaledVector(gdir, 0.3);
        gazeCone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), gdir);

        // 覆盖层文字
        const ad = S.px * 180;
        hDir.textContent = Math.abs(S.px) > 0.85 ? "背面 · 180°"
          : S.px < -0.05 ? "左 " + Math.abs(ad).toFixed(0) + "°"
            : S.px > 0.05 ? "右 " + ad.toFixed(0) + "°" : "正面 · 0°";
        hRoll.textContent = Math.abs(S.rv) > 0.01 ? "倾斜 " + S.rv.toFixed(2) + " (" + (S.rv * 45).toFixed(0) + "°)" : "";
        hLens.textContent = S.focal.toFixed(0) + "mm · " + lensKey(S.focal);

        updatePreview();
      }

      // ---- 按需渲染：状态或相机变化才画一帧，没有常驻自循环 ----
      let alive = true;
      function renderOnce() {
        if (!alive) return;
        try { upd(); renderer.render(scene, cam3d); } catch (_) {}
      }
      function scheduleRender() {
        if (!alive || S.rafPending) return;
        S.rafPending = true;
        S.rafId = requestAnimationFrame(() => { S.rafPending = false; S.rafId = null; renderOnce(); });
      }
      function markDirty() { scheduleRender(); }

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
          if (ray.intersectObject(h.m).length > 0) { S.dragging = true; S.tgt = h.n; setScale(h, 1.3); scheduleRender(); e.preventDefault(); return; }
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
          scheduleRender();
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
            a = Math.max(-78, Math.min(78, a));
            S.elevation = a; S.py = r2(a / 78);
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
      canvas.addEventListener("pointerenter", () => { S.hoverCanvas = true; });
      canvas.addEventListener("pointerleave", () => { S.hoverCanvas = false; });

      // 滚轮：新版前端在捕获阶段 stopPropagation，改在 window 捕获阶段监听
      const onWheel = e => {
        const rc = canvas.getBoundingClientRect();
        if (e.clientX < rc.left || e.clientX > rc.right || e.clientY < rc.top || e.clientY > rc.bottom) return;
        e.preventDefault(); e.stopPropagation();
        let idx = DIST_GEARS.indexOf(S.pz);
        if (idx < 0) { snapDist(); idx = DIST_GEARS.indexOf(S.pz); }
        const dir = e.deltaY > 0 ? 1 : -1;
        const ni = Math.max(0, Math.min(DIST_GEARS.length - 1, (idx >= 0 ? idx : 3) + dir));
        S.pz = DIST_GEARS[ni]; S.dist = distV(S.pz);
        markCustom(); syncN();
      };
      window.addEventListener("wheel", onWheel, { capture: true, passive: false });

      // ---- 快捷键：1..6 标准机位 / R 重置 / G 九宫格 / F 视锥 ----
      function setPose(obj) {
        for (const k of Object.keys(obj)) {
          if (POSE_KEYS.indexOf(k) < 0) continue;
          const w = gw(k); if (w) w.value = obj[k];
        }
        const [a, b, c] = [obj.pos_x || 0, obj.pos_y || 0, obj.pos_z || 0];
        S.px = a; S.py = b; S.pz = c; S.rv = obj.roll || 0;
        S.azimuth = azDeg(S.px); S.elevation = elDeg(S.py); S.dist = distV(S.pz);
        markCustom(); syncN();
      }
      function resetAll() {
        setPose({ pos_x: 0, pos_y: 0, pos_z: 0, roll: 0, focal: 50, pan_x: 0, pan_y: 0, subject_yaw: 0 });
        const wp = gw("preset"); if (wp) wp.value = "自定义";
        paintPreset();
      }
      function toggleGrid() {
        S.showGrid = !S.showGrid; vf.classList.toggle("on", S.showGrid);
        gridBtn.classList.toggle("on", S.showGrid); scheduleRender();
      }
      function toggleFrustum() {
        S.showFrustum = !S.showFrustum; frustumLines.visible = S.showFrustum;
        frBtn.classList.toggle("on", S.showFrustum); scheduleRender();
      }
      const onKey = e => {
        const tag = (e.target && e.target.tagName || "").toUpperCase();
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (!node.selected && !S.hoverCanvas) return;
        const k = e.key;
        if (HOTKEYS[k]) { e.preventDefault(); setPose(HOTKEYS[k]); }
        else if (k === "r" || k === "R") { e.preventDefault(); resetAll(); }
        else if (k === "g" || k === "G") { e.preventDefault(); toggleGrid(); }
        else if (k === "f" || k === "F") { e.preventDefault(); toggleFrustum(); }
      };
      window.addEventListener("keydown", onKey);

      // ---- 按钮行 ----
      const bRow = document.createElement("div"); bRow.className = "btn-r"; container.appendChild(bRow);
      const mkBtn = (txt, title, fn) => {
        const b = document.createElement("button"); b.textContent = txt; if (title) b.title = title;
        b.onclick = fn; bRow.appendChild(b); return b;
      };
      mkBtn("归位", "姿态清零（R）", resetAll);
      const tagBtn = mkBtn("标签", "编辑词表", () => togglePanel(tagPanel, tagBtn, "标签", "收起标签"));
      const preBtn = mkBtn("预设", "16 个预设", () => togglePanel(prePanel, preBtn, "预设", "收起预设"));
      const gridBtn = mkBtn("九宫格", "三分法参考线（G）", toggleGrid);
      const frBtn = mkBtn("视锥", "相机视锥线框（F）", toggleFrustum);
      gridBtn.classList.toggle("on", S.showGrid);
      frBtn.classList.toggle("on", S.showFrustum);
      vf.classList.toggle("on", S.showGrid);

      // ---- 标签面板 ----
      const tagPanel = document.createElement("div"); tagPanel.className = "sc-panel";
      container.appendChild(tagPanel);
      const TAG_GROUPS = [
        ["方位", ["front", "front_right", "right", "back_right", "back", "back_left", "left", "front_left"]],
        ["高度", ["high", "eye", "low", "bird", "worm"]],
        ["距离", ["ecu", "cu", "medium", "cowboy", "full", "wide"]],
        ["倾斜", ["dutch", "dutch_neg"]],
        ["焦距 / 光学", ["lens14", "lens24", "lens35", "lens50", "lens85", "lens135"]],
        ["构图偏移", ["subject_left", "subject_right", "subject_center", "subject_low", "subject_high"]],
        ["主体 / 视线", ["subject_front", "subject_turn", "subject_away", "gaze_viewer", "gaze_away", "gaze_over_shoulder"]],
      ];
      const tagInputs = {};
      TAG_GROUPS.forEach(([gname, keys]) => {
        const g = document.createElement("div"); g.className = "sc-gp"; g.textContent = gname; tagPanel.appendChild(g);
        keys.forEach(k => {
          const row = document.createElement("div"); row.className = "sc-row";
          const lbl = document.createElement("span"); lbl.className = "k"; lbl.textContent = (CN[k] || k) + " " + k;
          row.appendChild(lbl);
          const inp = document.createElement("input"); inp.type = "text"; inp.value = VOCAB[k] || "";
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
        for (const k of Object.keys(tagInputs)) tagInputs[k].value = VOCAB[k] || "";
      }

      // ---- 预设面板（PRESET_NAMES 与 core/presets.py 同源） ----
      const prePanel = document.createElement("div"); prePanel.className = "sc-panel";
      container.appendChild(prePanel);
      const preGrid = document.createElement("div"); preGrid.className = "sc-grid"; prePanel.appendChild(preGrid);
      const preNote = document.createElement("div"); preNote.className = "ph";
      preNote.style.marginTop = "4px";
      preNote.textContent = "预设参数由 Python 端权威计算，此处只同步 3D 姿态与焦距";
      prePanel.appendChild(preNote);

      const preButtons = [];
      function applyPreset(name) {
        const wp = gw("preset"); if (wp) wp.value = name;
        const pr = presetParams(name);
        const put = (k, v) => { const w = gw(k); if (w && v != null) w.value = v; };
        put("pos_x", pr.pos_x); put("pos_y", pr.pos_y); put("pos_z", pr.pos_z); put("roll", pr.roll);
        ["focal", "pan_x", "pan_y", "subject_yaw", "gaze_mode", "elevation_mode", "strength"]
          .forEach(k => { if (pr[k] != null) put(k, pr[k]); });
        readW();
        syncN(); paintPreset();
      }
      function paintPreset() {
        const cur = gw("preset")?.value;
        preButtons.forEach(([name, el]) => el.classList.toggle("on", name === cur));
      }
      PRESET_NAMES.slice(1).forEach(name => {
        const b = document.createElement("button"); b.textContent = name;
        b.onclick = () => applyPreset(name);
        preGrid.appendChild(b); preButtons.push([name, b]);
      });
      const customBtn = document.createElement("button"); customBtn.textContent = "自定义（不改姿态）";
      customBtn.style.gridColumn = "1 / -1";
      customBtn.onclick = () => { const wp = gw("preset"); if (wp) wp.value = "自定义"; paintPreset(); };
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

      // ---- 底部：档位 + 预览 ----
      container.appendChild(binEl);
      container.appendChild(binEl2);
      container.appendChild(tokEl);
      container.appendChild(prevEl);
      container.appendChild(negEl);
      ph.textContent = "预览 → 正向 / 负向（Python 端为准）";
      container.appendChild(ph);

      scheduleRender();   // 首帧

      // ---- 清理 ----
      const domWidget = node.widgets?.find(w => w.name === "cam3d");
      if (domWidget) {
        const baseRO = domWidget.onRemove;
        domWidget.onRemove = function () {
          alive = false;
          if (S.rafId) cancelAnimationFrame(S.rafId);
          S.rafPending = false; S.rafId = null;
          try { ro.disconnect(); } catch (_) {}
          try { distTube.geometry.dispose(); distTube.material.dispose(); } catch (_) {}
          try { window.removeEventListener("wheel", onWheel, { capture: true }); } catch (_) {}
          try { window.removeEventListener("keydown", onKey); } catch (_) {}
          try { renderer.dispose(); } catch (_) {}
          if (baseRO) baseRO.call(this);
        };
      }
    };
  },
});
