/*
 * SDXL 相机控制 —— ComfyUI 节点 3D 面板（v2.3 薄壳）
 * 3D 场景（网格 / 人体 / 视线锥 / 朝向箭头 / 方位环 / 高度弧 / 距离管 / 动态视锥 / 九宫格 /
 * 地平线 / 参考图叠层 / 拖拽与滚轮）全部复用 js/camera_rig.js 的 CameraRig —— 与 Studio 视口
 * (studio/web/camera3d.js) 同一份实现，两处不再各自维护 scene。
 * camera_rig.js 用裸说明符 import "three"：Studio 由 index.html 的 importmap 提供映射；ComfyUI
 * 页面没有 importmap，故这里在 import rig 之前动态注入一份（需要多 importmap，Chrome/Edge 133+）。
 * 本文件只保留节点侧接线：widget 读写、预览文本、token 预算、词表面板、预设面板、快捷键。
 */
import { app } from "../../scripts/app.js";
import { computeCamera, V, NEG, PRESET_NAMES, presetParams } from "./camera_shared.js";
import * as SHARED from "./camera_shared.js";
// 显示用中文短名（算法侧无中文，纯 UI 文案）
const CN = {
  front: "正面", front_right: "右前方", right: "右侧", back_right: "右后方", back: "背面",
  back_left: "左后方", left: "左侧", front_left: "左前方", high: "俯视", eye: "平视", low: "仰视",
  bird: "鸟瞰", worm: "虫瞰", ecu: "特写", cu: "近景", medium: "中景", cowboy: "牛仔",
  full: "全身", wide: "远景", dutch: "倾斜", dutch_neg: "反倾斜",
  subject_left: "主体偏左", subject_right: "主体偏右", subject_center: "主体居中",
  subject_low: "主体偏下", subject_high: "主体偏上", subject_front: "正对身体",
  subject_turn: "转身", subject_away: "背对", gaze_viewer: "看镜头", gaze_away: "移开视线",
  gaze_over_shoulder: "回眸",
};
// token 预算：优先用 camera_shared.js，缺导出时按逗号切 tag 近似（与 Studio 兜底口径一致）
const _clipApprox = (text) => {
  const s = String(text == null ? "" : text).trim();
  if (!s) return 0;
  let n = 0;
  s.split(",").forEach((tag) => { tag.trim().split(/\s+/).forEach((w) => { if (w) n += w.length > 6 ? 2 : 1; }); });
  return n;
};
const clipTokens = SHARED.clipTokens || _clipApprox;
const budgetOf = SHARED.budgetOf || ((p, n) => ({ positive: clipTokens(p), negative: clipTokens(n), total: clipTokens(p) + clipTokens(n), limit: 75, warn: 65 }));
// pos_z → 距离档位中心（滚轮 / 松手吸附，与 camera_core.distance_key 阈值对应）
const DIST_GEARS = [0.85, 0.45, 0.10, -0.17, -0.52, -0.85];
const POSE_KEYS = ["pos_x", "pos_y", "pos_z", "roll", "focal", "pan_x", "pan_y", "subject_yaw"];
const HOTKEYS = {
  "1": { pos_x: 0, pos_y: 0, pos_z: 0, roll: 0 }, "2": { pos_x: 1, pos_y: 0, pos_z: 0, roll: 0 },
  "3": { pos_x: -0.5, pos_y: 0, pos_z: 0, roll: 0 }, "4": { pos_x: 0.5, pos_y: 0, pos_z: 0, roll: 0 },
  "5": { pos_x: 0, pos_y: 1, pos_z: -0.35, roll: 0 }, "6": { pos_x: 0, pos_y: -1, pos_z: -0.35, roll: 0 },
};
// 标签面板分组（键名与 camera_shared 的 V 一致）
const TAG_GROUPS = [
  ["方位", ["front", "front_right", "right", "back_right", "back", "back_left", "left", "front_left"]],
  ["高度", ["high", "eye", "low", "bird", "worm"]],
  ["距离", ["ecu", "cu", "medium", "cowboy", "full", "wide"]],
  ["倾斜", ["dutch", "dutch_neg"]],
  ["焦距 / 光学", ["lens14", "lens24", "lens35", "lens50", "lens85", "lens135"]],
  ["构图偏移", ["subject_left", "subject_right", "subject_center", "subject_low", "subject_high"]],
  ["主体 / 视线", ["subject_front", "subject_turn", "subject_away", "gaze_viewer", "gaze_away", "gaze_over_shoulder"]],
];
// ---- three 裸说明符：注入 importmap 后再动态加载 camera_rig.js ----
function ensureThreeImportMap() {
  const url = new URL("./three.module.js", import.meta.url).href;
  if (typeof document === "undefined") return url;
  const has = Array.prototype.slice.call(document.querySelectorAll("script[type=\"importmap\"]")).some((s) => {
    try { const m = JSON.parse(s.textContent || "{}"); return !!(m && m.imports && m.imports.three); } catch (_) { return false; }
  });
  if (!has) {
    const el = document.createElement("script");
    el.type = "importmap";
    el.textContent = JSON.stringify({ imports: { three: url } });
    document.head.appendChild(el);
  }
  return url;
}
const THREE_URL = ensureThreeImportMap();
let CameraRig = null;
try {
  const rigMod = await import("./camera_rig.js");
  CameraRig = rigMod.CameraRig || rigMod.default || null;
} catch (err) {
  console.warn("[SDXLCam] camera_rig.js 加载失败（需要支持多 importmap 的浏览器）:", err);
}
console.log("[SDXLCam] widget v2.3 · rig:", CameraRig ? "ok" : "missing", "· three:", THREE_URL);
// ---- CSS ----
(function () {
  if (document.getElementById("sdxlcam-style")) return;
  const el = document.createElement("style");
  el.id = "sdxlcam-style";
  el.textContent = [
    ".sc-w{display:flex;flex-direction:column;gap:4px;padding:4px;box-sizing:border-box;min-height:400px}" ".sc-w .sc-cw{position:relative;width:100%;flex-shrink:0;overflow:hidden;border-radius:8px;background:#0a0a14}",
    ".sc-w .sc-cw canvas{display:block;width:100% !important;height:auto !important;aspect-ratio:560/500;border-radius:8px;cursor:grab;touch-action:none}" ".sc-w .sc-ol{position:absolute;left:0;right:0;top:0;padding:6px 8px;display:flex;justify-content:space-between;pointer-events:none;font-size:10px}",
    ".sc-w .sc-ol .h{color:rgba(255,255,255,0.5)}", ".sc-w .sc-ol .i{text-align:right}" ".sc-w .sc-ol .d{color:#ff8a4c;font-family:monospace}", ".sc-w .sc-ol .r{color:#c792ea;font-family:monospace}",
    ".sc-w .sc-ol .l{color:#8fd6c0;font-family:monospace}" ".sc-w .btn-r{display:flex;gap:6px;align-items:center;flex-wrap:wrap}",
    ".sc-w .btn-r button{background:#333;border:1px solid #555;color:#ddd;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:10px;line-height:1.6}" ".sc-w .btn-r button:hover{background:#444}", ".sc-w .btn-r button.on{background:#4a3a6a;border-color:#8f7fe0;color:#fff}",
    ".sc-w .sc-bin{font-size:10px;font-family:monospace;color:#8fd6c0;background:rgba(0,0,0,0.28);border:1px solid #3a4a48;border-radius:4px;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" ".sc-w .sc-tok{font-size:9px;font-family:monospace;padding:0 6px;color:rgba(143,214,192,0.75);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".sc-w .sc-bin2{font-size:9px;font-family:monospace;color:rgba(143,214,192,0.6);padding:0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" ".sc-w .pr{width:100%;min-height:26px;max-height:60px;resize:vertical;font-size:10px;background:rgba(0,0,0,0.25);border:1px solid #444;color:#fff3b7;border-radius:4px;padding:3px 5px;font-family:monospace;box-sizing:border-box;overflow-x:hidden;word-break:break-all}",
    ".sc-w .pr.neg{color:#b7c6ff;border-color:#39404f;min-height:22px}" ".sc-w .ph{font-size:9px;color:rgba(255,255,255,0.3);text-align:center}",
    ".sc-w .sc-panel{display:none;border:1px solid #444;border-radius:6px;padding:6px;background:rgba(0,0,0,0.3);margin-top:4px;max-height:620px;overflow-y:auto}" ".sc-w .sc-panel.open{display:block}",
    ".sc-w .sc-gp{font-size:10px;color:#667eea;margin:6px 0 2px 0;font-weight:bold}" ".sc-w .sc-row{display:flex;align-items:center;gap:4px;margin:1px 0}",
    ".sc-w .sc-row span.k{width:70px;font-size:9px;color:rgba(255,255,255,0.6);flex-shrink:0}" ".sc-w .sc-row input{flex:1;min-width:0;font-size:9px;background:#222;border:1px solid #444;color:#ddd;border-radius:3px;padding:1px 4px}",
    ".sc-w .sc-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}" ".sc-w .sc-grid button{background:#2c2c3a;border:1px solid #4a4a5e;color:#cfd3e0;border-radius:4px;padding:4px 2px;cursor:pointer;font-size:10px;text-align:center}",
    ".sc-w .sc-grid button:hover{background:#3c3c52;border-color:#8f7fe0}", ".sc-w .sc-grid button.on{background:#4a3a6a;border-color:#8f7fe0;color:#fff}",
  ].join("\n");
  document.head.appendChild(el);
})();
// ============================================================
// Extension
// ============================================================
app.registerExtension({
  name: "ComfyUI.SDXL.CameraAngle",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "CameraAngleSDXL") return;
    const orig = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      orig?.apply(this, arguments);
      const node = this;
      const gw = (n) => node.widgets?.find((w) => w.name === n);
      const num = (n) => { const w = gw(n); if (!w) return 0; const v = parseFloat(w.value); return isNaN(v) ? 0 : v; };
      const str = (n) => String(gw(n)?.value ?? "");
      // 额外提示词是字符串 widget；脏数据一律当空，避免拼出 "..., 0"
      const extraStr = (n) => { const v = gw(n)?.value; return typeof v === "string" ? v : ""; };
      const hasW = (n) => !!gw(n);
      // ---- 隐藏 _cfg_json（词表覆盖，DOM 隐藏但保留序列化） ----
      setTimeout(() => {
        const w = gw("_cfg_json");
        if (!w || !w.element) return;
        w.computeSize = () => [0, 0];
        w.element.style.cssText = "display:none!important;height:0!important;min-height:0!important;padding:0!important;margin:0!important;border:none!important;overflow:hidden!important";
        if (w.element.parentNode) w.element.parentNode.style.display = "none";
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
      const wrap = document.createElement("div");
      wrap.className = "sc-cw";
      container.prepend(wrap);
      const ol = document.createElement("div");
      ol.className = "sc-ol";
      ol.innerHTML = '<span class="h">拖拽 · 1-6 机位 · R 重置 · G 九宫格 · F 视锥</span>'
        + '<div class="i"><div class="d">正面 · 0°</div><div class="r"></div><div class="l"></div></div>';
      wrap.appendChild(ol);
      const hHint = ol.querySelector(".h"), hDir = ol.querySelector(".d"),
        hRoll = ol.querySelector(".r"), hLens = ol.querySelector(".l");
      hHint.title = "紫环粉点=方位 · 青弧青点=高度 · 黄点=距离 · 滚轮=距离档位 · Alt+拖=构图偏移";
      // 词表（面板 / _cfg_json 可覆盖；默认值来自 camera_shared 的 V / NEG）
      const VOCAB = Object.assign({}, V);
      const VOCAB_NEG = Object.assign({}, NEG);
      let tagVer = 0;
      let rig = null, inRig = false, hoverCanvas = false;
      if (!CameraRig) {
        const miss = document.createElement("div");
        miss.className = "ph"; miss.style.padding = "18px 10px";
        miss.textContent = "3D 视口需要支持多 importmap 的浏览器（Chrome / Edge 133+）";
        wrap.appendChild(miss);
      } else {
        // compact：不依赖外部 DOM id；交互（拖拽手柄 / 滚轮 / Alt+拖构图）与 Studio 视口一致
        rig = new CameraRig(wrap, { compact: true, interactive: true, onChange: onChange, onCommit: onCommit });
        wrap.addEventListener("pointerenter", () => { hoverCanvas = true; });
        wrap.addEventListener("pointerleave", () => { hoverCanvas = false; });
      }
      // ---- widget → rig ----
      function readW() {
        if (!rig) return;
        inRig = true;
        rig.setState({
          pos_x: num("pos_x"), pos_y: num("pos_y"), pos_z: num("pos_z"), roll: num("roll"),
          focal: num("focal") || 50, pan_x: num("pan_x"), pan_y: num("pan_y"),
          subject_yaw: num("subject_yaw"), gaze_mode: str("gaze_mode") || "auto",
        });
        inRig = false;
        paintHud();
      }
      // ---- rig → widget ----
      function syncN() {
        if (!rig) return;
        const s = rig.getState();
        const put = (k, v) => { const w = gw(k); if (w) w.value = v; };
        put("pos_x", s.pos_x); put("pos_y", s.pos_y); put("pos_z", s.pos_z); put("roll", s.roll);
        put("focal", s.focal); put("pan_x", s.pan_x); put("pan_y", s.pan_y);
        put("subject_yaw", s.subject_yaw); put("gaze_mode", s.gaze_mode);
        if (node.graph) node.graph.setDirtyCanvas(true, true);
      }
      /** 手动改姿态即视为自定义：清掉预设，避免 Python 端用预设覆盖手调值 */
      function markCustom() {
        const wp = gw("preset");
        if (!wp || !wp.value || wp.value === "自定义") return;
        wp.value = "自定义";
        if (node.graph) node.graph.setDirtyCanvas(true, true);
      }
      /** 距离档位吸附：只吸 0.12 以内的档位，不破坏自由值 */
      function snapDist() {
        if (!rig) return;
        const z = rig.getState().pos_z;
        let best = DIST_GEARS[0];
        for (let i = 1; i < DIST_GEARS.length; i++) {
          if (Math.abs(z - DIST_GEARS[i]) < Math.abs(z - best)) best = DIST_GEARS[i];
        }
        if (Math.abs(z - best) < 0.12) rig.setState({ pos_z: best });
      }
      function onChange() {
        if (inRig) return;   // readW 引起的回灌，不回写 widget
        markCustom(); syncN(); updatePreview();
      }
      function onCommit() {
        markCustom(); snapDist(); syncN(); updatePreview();
        if (node.graph) node.graph.setDirtyCanvas(true, true);
      }
      // ---- 覆盖层 HUD ----
      function paintHud() {
        if (!rig) return;
        const s = rig.getState();
        const info = rig.info ? rig.info() : null;
        const ad = s.pos_x * 180;
        hDir.textContent = Math.abs(s.pos_x) > 0.85 ? "背面 · 180°"
          : s.pos_x < -0.05 ? "左 " + Math.abs(ad).toFixed(0) + "°"
            : s.pos_x > 0.05 ? "右 " + ad.toFixed(0) + "°" : "正面 · 0°";
        hRoll.textContent = Math.abs(s.roll) > 0.01 ? "倾斜 " + s.roll.toFixed(2) + " (" + (s.roll * 45).toFixed(0) + "°)" : "";
        hLens.textContent = info ? info.lens_text : "";
      }
      // ---- _cfg_json：{"tags": {正向词..., "neg": {...}}} ----
      function cfgObj() { return { tags: Object.assign({}, VOCAB, { neg: VOCAB_NEG }) }; }
      function loadCfg() {
        const w = gw("_cfg_json");
        if (!w || !w.value || w.value === "{}") return;
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
      function opts() {
        const s = rig ? rig.getState() : { pos_x: 0, pos_y: 0, pos_z: 0, roll: 0, focal: 50, pan_x: 0, pan_y: 0, subject_yaw: 0, gaze_mode: "auto" };
        return {
          preset: str("preset") || "自定义",
          strength: hasW("strength") ? num("strength") : 1,
          weight_mode: str("weight_mode") || "tanh",
          azimuth_mode: str("azimuth_mode") || "8bin",
          elevation_mode: str("elevation_mode") || "3bin",
          neg_strength: hasW("neg_strength") ? num("neg_strength") : 0,
          focal: s.focal, pan_x: s.pan_x, pan_y: s.pan_y, subject_yaw: s.subject_yaw, gaze_mode: s.gaze_mode,
          extra_positive: extraStr("extra_positive"), extra_negative: extraStr("extra_negative"),
          cfg: cfgObj(),
        };
      }
      /** 档位行：right / eye / medium → 中文注解 + 焦距 / 构图 / 主体 */
      function ChineseLine(ib) {
        const parts = [];
        parts.push("方位 " + (CN[ib.bin] || ib.bin) + " " + ib.azimuth_deg.toFixed(0) + "°");
        parts.push("高度 " + (CN[ib.elevation] || ib.elevation));
        parts.push("距离 " + (CN[ib.distance] || ib.distance));
        parts.push(ib.focal.toFixed(0) + "mm / FOV " + ib.fov_deg.toFixed(1) + "°");
        if (ib.dutch) parts.push("倾斜 " + ib.roll_deg.toFixed(0) + "°");
        if (ib.framing && ib.framing.length) parts.push.apply(parts, ib.framing.map((k) => CN[k] || k));
        if (ib.subject) parts.push(CN[ib.subject] || ib.subject);
        if (ib.gaze) parts.push(CN[ib.gaze] || ib.gaze);
        return parts.join(" · ");
      }
      // ---- 预览（只在参数变化时重算） ----
      let lastSig = "";
      function updatePreview() {
        if (!rig) return;
        const s = rig.getState();
        const sig = [s.pos_x, s.pos_y, s.pos_z, s.roll, s.focal, s.pan_x, s.pan_y, s.subject_yaw, s.gaze_mode,
          num("strength"), str("weight_mode"), str("azimuth_mode"), str("elevation_mode"),
          num("neg_strength"), str("extra_positive"), str("extra_negative"), tagVer].join("|");
        if (sig === lastSig) return;
        lastSig = sig;
        const r = computeCamera(s.pos_x, s.pos_y, s.pos_z, s.roll, opts());
        prevEl.value = r.positive;
        negEl.value = r.negative;
        // token 预算：Chunk 0 看图的是正向段，阈值只卡 positive（> 65 橙 / > 75 红），与 Studio 一致
        const bg = budgetOf(r.positive, r.negative) || {};
        const pt = Math.round(Number(bg.positive) || 0), nt = Math.round(Number(bg.negative) || 0);
        const lim = Math.round(Number(bg.limit) || 75), wn = Math.round(Number(bg.warn) || 65);
        tokEl.textContent = pt + " / " + lim + " tok · N " + nt
          + (pt > lim ? " · 超 Chunk 0 上限" : (pt > wn ? " · 接近上限" : ""));
        tokEl.style.color = pt > lim ? "#d0453a" : (pt > wn ? "#d08b23" : "rgba(143,214,192,0.75)");
        const ib = r.info;
        binEl.textContent = (ib.bin || "—") + " / " + ib.elevation + " / " + ib.distance + (ib.dutch ? " / dutch" : "");
        binEl2.textContent = ChineseLine(ib);
        paintHud();
      }
      // ---- 机位 / 复位 / 开关 ----
      function setPose(obj) {
        if (!rig) return;
        const patch = {};
        POSE_KEYS.forEach((k) => { if (obj[k] != null) patch[k] = obj[k]; });
        rig.setState(patch); markCustom(); syncN(); updatePreview();
      }
      function resetAll() {
        setPose({ pos_x: 0, pos_y: 0, pos_z: 0, roll: 0, focal: 50, pan_x: 0, pan_y: 0, subject_yaw: 0 });
        const wp = gw("preset"); if (wp) wp.value = "自定义";
        paintPreset();
      }
      function toggleGrid() { if (rig && gridBtn) gridBtn.classList.toggle("on", !!rig.toggleGrid()); }
      function toggleFrustum() { if (rig && frBtn) frBtn.classList.toggle("on", !!rig.toggleFrustum()); }
      // ---- 按钮行 ----
      const bRow = document.createElement("div"); bRow.className = "btn-r"; container.appendChild(bRow);
      const mkBtn = (txt, title, fn) => {
        const b = document.createElement("button");
        b.textContent = txt; if (title) b.title = title;
        b.onclick = fn; bRow.appendChild(b); return b;
      };
      mkBtn("归位", "姿态清零（R）", resetAll);
      const tagBtn = mkBtn("标签", "编辑词表", () => togglePanel(tagPanel, tagBtn, "标签", "收起标签"));
      const preBtn = mkBtn("预设", "16 个预设", () => togglePanel(prePanel, preBtn, "预设", "收起预设"));
      const gridBtn = mkBtn("九宫格", "三分法参考线（G）", toggleGrid);
      const frBtn = mkBtn("视锥", "相机视锥线框（F）", toggleFrustum);
      // ---- 标签面板（innerHTML + 事件委托，比逐行建 DOM 短） ----
      const tagPanel = document.createElement("div"); tagPanel.className = "sc-panel";
      container.appendChild(tagPanel);
      tagPanel.innerHTML = TAG_GROUPS.map((pair) => {
        const rows = pair[1].map((k) => '<div class="sc-row"><span class="k">'
          + (CN[k] || k) + " " + k + '</span><input type="text" data-k="' + k + '" /></div>').join("");
        return '<div class="sc-gp">' + pair[0] + "</div>" + rows;
      }).join("") + '<div class="sc-row" style="margin-top:6px"><button data-act="reset">恢复默认词表</button></div>';
      const tagInputs = {};
      Array.prototype.slice.call(tagPanel.querySelectorAll("input[data-k]")).forEach((inp) => {
        tagInputs[inp.getAttribute("data-k")] = inp;
      });
      tagPanel.addEventListener("input", (e) => {
        const k = e.target && e.target.getAttribute && e.target.getAttribute("data-k");
        if (!k) return;
        VOCAB[k] = e.target.value; saveCfg(); updatePreview();
      });
      tagPanel.addEventListener("click", (e) => {
        const act = e.target && e.target.getAttribute && e.target.getAttribute("data-act");
        if (act !== "reset") return;
        for (const k of Object.keys(V)) VOCAB[k] = V[k];
        for (const k of Object.keys(NEG)) VOCAB_NEG[k] = NEG[k];
        refreshTags(); saveCfg(); updatePreview();
      });
      function refreshTags() {
        for (const k of Object.keys(tagInputs)) tagInputs[k].value = VOCAB[k] || "";
      }
      refreshTags();
      // ---- 预设面板（PRESET_NAMES 与 core/presets.py 同源） ----
      const prePanel = document.createElement("div"); prePanel.className = "sc-panel";
      container.appendChild(prePanel);
      prePanel.innerHTML = '<div class="sc-grid">'
        + PRESET_NAMES.slice(1).map((n) => '<button data-preset="' + n + '">' + n + "</button>").join("")
        + '<button data-preset="自定义" style="grid-column:1 / -1">自定义（不改姿态）</button></div>'
        + '<div class="ph" style="margin-top:4px">预设参数由 Python 端权威计算，此处只同步 3D 姿态与焦距</div>';
      function paintPreset() {
        const cur = gw("preset")?.value;
        Array.prototype.slice.call(prePanel.querySelectorAll("button[data-preset]")).forEach((b) => {
          b.classList.toggle("on", b.getAttribute("data-preset") === cur);
        });
      }
      prePanel.addEventListener("click", (e) => {
        const name = e.target && e.target.getAttribute && e.target.getAttribute("data-preset");
        if (!name) return;
        if (name === "自定义") {
          const wp = gw("preset"); if (wp) wp.value = "自定义";
          paintPreset();
          return;
        }
        const wp = gw("preset"); if (wp) wp.value = name;
        const pr = presetParams(name) || {};
        const patch = {};
        ["pos_x", "pos_y", "pos_z", "roll", "focal", "pan_x", "pan_y", "subject_yaw", "gaze_mode"]
          .forEach((k) => { if (pr[k] != null) patch[k] = pr[k]; });
        if (rig) rig.setState(patch);
        ["elevation_mode", "strength"].forEach((k) => {
          const w = gw(k); if (w && pr[k] != null) w.value = pr[k];
        });
        readW(); syncN(); updatePreview(); paintPreset();
      });
      paintPreset();
      function togglePanel(p, btn, openTxt, closeTxt) {
        const show = !p.classList.contains("open");
        if (p === tagPanel) { prePanel.classList.remove("open"); preBtn.textContent = "预设"; }
        if (p === prePanel) { tagPanel.classList.remove("open"); tagBtn.textContent = "标签"; }
        p.classList.toggle("open", show);
        btn.textContent = show ? closeTxt : openTxt;
      }
      // ---- 底部：档位 + 预览 ----
      const binEl = document.createElement("div"); binEl.className = "sc-bin";
      binEl.textContent = "front / eye / medium";
      const binEl2 = document.createElement("div"); binEl2.className = "sc-bin2";
      const tokEl = document.createElement("div"); tokEl.className = "sc-tok"; tokEl.textContent = "— / 75 tok";
      const prevEl = document.createElement("textarea"); prevEl.className = "pr"; prevEl.readOnly = true; prevEl.rows = 2;
      const negEl = document.createElement("textarea"); negEl.className = "pr neg"; negEl.readOnly = true; negEl.rows = 2;
      const ph = document.createElement("div"); ph.className = "ph";
      container.appendChild(binEl);
      container.appendChild(binEl2);
      container.appendChild(tokEl);
      container.appendChild(prevEl);
      container.appendChild(negEl);
      ph.textContent = "预览 → 正向 / 负向（Python 端为准）";
      container.appendChild(ph);
      // ---- 任意 widget 变化 → 灌进 rig + 刷预览 ----
      (node.widgets || []).forEach((w) => {
        if (w._scHooked) return;
        const oc = w.callback;
        w.callback = function () {
          if (oc) oc.apply(this, arguments);
          readW(); updatePreview();
        };
        w._scHooked = true;
      });
      // ---- 滚轮：距离档位（新版前端在捕获阶段 stopPropagation，故监听 window 捕获） ----
      const onWheel = (e) => {
        if (!rig) return;
        const cv = rig.canvas || wrap.querySelector("canvas");
        if (!cv) return;
        const rc = cv.getBoundingClientRect();
        if (e.clientX < rc.left || e.clientX > rc.right || e.clientY < rc.top || e.clientY > rc.bottom) return;
        e.preventDefault(); e.stopPropagation();
        const z = rig.getState().pos_z;
        let idx = 0;
        for (let i = 1; i < DIST_GEARS.length; i++) {
          if (Math.abs(z - DIST_GEARS[i]) < Math.abs(z - DIST_GEARS[idx])) idx = i;
        }
        const ni = Math.max(0, Math.min(DIST_GEARS.length - 1, idx + (e.deltaY > 0 ? 1 : -1)));
        rig.setState({ pos_z: DIST_GEARS[ni] });
        markCustom(); syncN(); updatePreview();
      };
      window.addEventListener("wheel", onWheel, { capture: true, passive: false });
      // ---- 快捷键：1..6 机位 / R 重置 / G 九宫格 / F 视锥 ----
      const onKey = (e) => {
        const tag = ((e.target && e.target.tagName) || "").toUpperCase();
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (!node.selected && !hoverCanvas) return;
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        const k = e.key;
        if (HOTKEYS[k]) { e.preventDefault(); setPose(HOTKEYS[k]); }
        else if (k === "r" || k === "R") { e.preventDefault(); resetAll(); }
        else if (k === "g" || k === "G") { e.preventDefault(); toggleGrid(); }
        else if (k === "f" || k === "F") { e.preventDefault(); toggleFrustum(); }
      };
      window.addEventListener("keydown", onKey);
      // ---- 初始化 ----
      readW();
      updatePreview();
      setTimeout(() => {
        readW(); loadCfg(); refreshTags(); updatePreview();
        if (rig) {
          gridBtn.classList.toggle("on", !!rig.gridOn);
          frBtn.classList.toggle("on", !!rig.frustumOn);
        }
      }, 220);
      node.onConfigure = (function (o) {
        return function () {
          if (o) o.apply(this, arguments);
          setTimeout(() => { readW(); loadCfg(); refreshTags(); updatePreview(); }, 60);
        };
      })(node.onConfigure);
      // ---- 清理 ----
      const domWidget = node.widgets?.find((w) => w.name === "cam3d");
      if (domWidget) {
        const baseRO = domWidget.onRemove;
        domWidget.onRemove = function () {
          try { window.removeEventListener("wheel", onWheel, { capture: true }); } catch (_) {}
          try { window.removeEventListener("keydown", onKey); } catch (_) {}
          try { if (rig) rig.dispose(); } catch (_) {}
          rig = null;
          if (baseRO) baseRO.call(this);
        };
      }
    };
  },
});
