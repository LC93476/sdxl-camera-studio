/**
 * SDXL 相机控制 —— 共享算法（core/camera_core.py 的严格 JS 镜像）
 *
 * Python 是唯一权威：core/camera_core.py 改一处，这里必须同步改一处。
 * 本文件零依赖（不 import three、不碰 DOM），供：
 *   - js/sdxl_camera_widget.js（ComfyUI 节点内 3D UI）
 *   - studio/web/camera3d.js（Studio 前端）
 *   - tools/parity_check.py 与 tools/_tmp_js_check.mjs（一致性校验）
 *
 * 坐标约定（SPEC.md）：
 *   pos_x ∈ [-1,1]  方位：-1 左侧 / 0 正面 / +1 背面（θ = pos_x*180°）
 *   pos_y ∈ [-1,1]  仰俯：-1 正下方仰视 / +1 正上方俯视（elev = pos_y*90°）
 *   pos_z ∈ [-1,1]  距离：+1 特写 … -1 远景
 *   roll  ∈ [-1,1]  翻滚：±1 = ±45°（正=顺时针荷兰角）
 */

// ── 输出权重区间（SPEC 2.1） ─────────────────────────────
export const W_MIN_OUT = 0.05;
export const W_MAX_OUT = 2.0;
export const W_NEUTRAL = 1.0;
export const BARE_EPS = 0.005;
export const AZ_POLE = 0.9;

// 极点：|elev| > 75°（|pos_y| > 0.8333）时方位角几何意义消失（SPEC 第 8 节）
export const POLE_DEG = 75.0;
export const POLE_NORM = POLE_DEG / 90.0;
export const POLE_START = 0.75;
export const AZ_SUPPRESS = 0.12;

// ── 档位定义 ────────────────────────────────────────────
export const AZ_BINS = ["back", "back_left", "left", "front_left", "front", "front_right", "right", "back_right"];
export const EL_KEYS = ["bird", "worm", "high", "eye", "low"];
export const DIST_KEYS = ["ecu", "cu", "medium", "cowboy", "full", "wide"];

// 焦距（等效 36mm 全画幅）分档：左闭右开，超出取端档（SPEC 7.1）
export const LENS_KEYS = ["lens14", "lens24", "lens35", "lens50", "lens85", "lens135"];
export const LENS_BANDS = [[18.0, "lens14"], [29.0, "lens24"], [43.0, "lens35"],
  [65.0, "lens50"], [105.0, "lens85"], [1e9, "lens135"]];
export const LENS_WEIGHT = {
  lens14: 5.0, lens24: 3.2, lens35: 1.8,
  lens50: 1.0, lens85: 4.0, lens135: 5.0,
};

export const FRAME_KEYS = ["subject_left", "subject_right", "subject_center", "subject_low", "subject_high"];
export const SUBJECT_KEYS = ["subject_front", "subject_turn", "subject_away"];
export const GAZE_KEYS = ["gaze_viewer", "gaze_away", "gaze_over_shoulder"];

// ── 词表（SPEC 2.5 + 第 7 节；节点可用 _cfg_json 覆盖单条） ──
// 格式固定为 const V = {...};（tools/parity_check.py 用正则抓这两块，勿改结构）
const V = {
  // 方位 8 档
  front: "front view, straight-on shot, facing the camera",
  front_right: "three-quarter view from the right",
  right: "side view, profile view from the right",
  back_right: "three-quarter rear view from the right",
  back: "from behind, back view, seen from behind",
  back_left: "three-quarter rear view from the left",
  left: "side view, profile view from the left",
  front_left: "three-quarter view from the left",
  // 高度
  high: "high angle shot, from above",
  eye: "eye level shot",
  low: "low angle shot, from below, looking up",
  bird: "from above, high angle, looking down at viewer, top-down view",
  worm: "from below, low angle, looking up at viewer",
  // 距离
  ecu: "extreme close-up, macro shot",
  cu: "close-up, portrait shot",
  medium: "medium shot, waist up",
  cowboy: "cowboy shot, mid-thigh up",
  full: "full body shot",
  wide: "wide shot, long shot, full figure in frame",
  // 倾斜
  dutch: "dutch angle, canted framing, tilted horizon",
  dutch_neg: "dutch angle, reverse canted framing, tilted frame",
  // 焦距 / 光学
  lens14: "ultra wide angle lens, 14mm, strong perspective distortion, exaggerated foreshortening, deep depth of field",
  lens24: "wide angle lens, 24mm, wide perspective, deep depth of field, environmental context",
  lens35: "35mm lens, natural perspective, environmental portrait",
  lens50: "50mm lens, standard perspective, natural proportions",
  lens85: "85mm lens, telephoto compression, shallow depth of field, background blur, bokeh",
  lens135: "135mm telephoto lens, strong spatial compression, creamy bokeh, isolated subject",
  // 构图偏移
  subject_left: "subject placed on the left third, rule of thirds, off-center composition, negative space on the right",
  subject_right: "subject placed on the right third, rule of thirds, off-center composition, negative space on the left",
  subject_center: "centered composition, subject in the middle of the frame",
  subject_low: "extra headroom above, subject in the lower third, low horizon line",
  subject_high: "subject in the upper third, foreground leading space",
  // 主体朝向 / 视线（与机位解耦）
  subject_front: "front-facing body, shoulders square to the camera",
  subject_turn: "body turned three-quarters away from the camera",
  subject_away: "facing away, back turned to the camera",
  gaze_viewer: "looking at viewer, direct eye contact",
  gaze_away: "looking away, gaze averted",
  gaze_over_shoulder: "looking back over the shoulder at the viewer",
};

// 负向镜像（每个轴取相反方向）
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
  dutch_neg: "horizon level, straight horizon",
  lens14: "telephoto compression, compressed perspective",
  lens24: "telephoto lens, compressed perspective",
  lens35: "extreme wide angle distortion",
  lens50: "wide angle distortion, fisheye",
  lens85: "wide angle distortion, exaggerated perspective",
  lens135: "wide angle distortion, exaggerated perspective",
  subject_left: "centered composition, subject in the middle of the frame",
  subject_right: "centered composition, subject in the middle of the frame",
  subject_center: "off-center composition, subject pushed to the frame edge",
  subject_low: "subject centered vertically, no headroom",
  subject_high: "subject centered vertically, cropped composition",
  subject_front: "facing away, back turned to the camera",
  subject_turn: "front view, shoulders square to the camera",
  subject_away: "front view, facing the camera",
  gaze_viewer: "looking away, gaze averted",
  gaze_away: "looking at viewer, direct eye contact",
  gaze_over_shoulder: "back turned, not looking at the camera",
};

// 极点区的视线改写：竖直机位下 "looking at viewer" 会把人画成正面站立（= camera_core.py POLE_GAZE）
export const POLE_GAZE = {
  bird: "looking up at the viewer",
  worm: "looking down at the viewer",
};

// 极点互斥：竖直轴已给出强语义时，从方位 / 视线短语里剔除的 token（SPEC 第 8 节）
export const POLE_BLOCK_TOKENS = {
  bird: ["front view", "straight-on shot", "facing the camera", "eye level shot",
    "looking at viewer", "direct eye contact",
    "from behind", "seen from behind", "back view"],
  worm: ["eye level shot", "top-down view", "looking down at viewer",
    "from above", "high angle", "bird's-eye view"],
  high: ["eye level shot"],
  low: ["eye level shot"],
};

// ── 权重常量（= camera_core.py SDXL_TAGS.weights，节点 cfg 可覆盖） ──
export const AZ_WEIGHT = 10.0;      // 方位原始权重上限
export const WEIGHT_MIN = 0.1;      // 原始权重下限
export const WEIGHT_MAX = 10.0;     // 原始权重上限
export const NEG_WEIGHT = 1.15;     // 负向基础权重
export const EXTRA_MASTER = 1.0;

export const DEFAULT_WEIGHTS = {
  azimuth: { enabled: true, weight: 10.0, extra: 0.0, deadzone_ratio: 0.06 },
  elevation: { enabled: true, extra: 0.0, mult: 10.0, deadzone: 0.0 },
  distance: { enabled: true, extra: 0.0, slope: 0.5 },
  tilt: { enabled: true, extra: 0.0, slope: 6.0 },
  lens: { enabled: true, extra: 0.0 },
  framing: { enabled: true, extra: 0.0, slope: 2.0 },
  subject: { enabled: true, extra: 0.0, slope: 2.0 },
  extra_master: 1.0,
  weight_min: 0.1,
  weight_max: 10.0,
  neg_weight: 1.15,
};

// ── 预设（与 core/presets.py 逐字一致；名称 → [pos_x, pos_y, pos_z, roll]） ──
const PRESETS = [
  ["正面平视中景", [0.0, 0.0, 0.0, 0.0]],
  ["正面仰视全身", [0.0, -0.55, -0.5, 0.0]],
  ["正面俯视中景", [0.0, 0.55, 0.0, 0.0]],
  ["右侧90度中景", [0.5, 0.0, 0.0, 0.0]],
  ["背面全身", [1.0, 0.0, -0.5, 0.0]],
  ["左前方45度近景", [-0.25, 0.0, 0.35, 0.0]],
  ["右后方45度中景", [0.75, 0.0, 0.0, 0.0]],
  ["虫瞰极限仰视", [0.0, -1.0, -0.85, 0.0]],
  ["鸟瞰极限俯视", [0.0, 1.0, -0.85, 0.0]],
  ["荷兰角倾斜中景", [0.0, 0.0, 0.0, 0.45]],
  ["特写面部", [0.0, 0.0, 0.85, 0.0]],
  ["长焦人像85mm", [0.0, 0.0, 0.1, 0.0]],
  ["广角环境24mm", [0.0, 0.0, -0.5, 0.0]],
  ["三分法偏右", [0.0, 0.0, 0.0, 0.0]],
  ["回眸背面机位", [1.0, 0.0, 0.1, 0.0]],
  ["荷兰角负斜", [0.0, 0.0, 0.0, -0.45]],
];

const PRESET_EXTRAS = {
  "长焦人像85mm": { focal: 85.0, pan_x: 0.0, pan_y: 0.0 },
  "广角环境24mm": { focal: 24.0 },
  "三分法偏右": { focal: 35.0, pan_x: -0.35 },
  "回眸背面机位": { focal: 85.0, subject_yaw: -0.45, gaze_mode: "camera" },
  "荷兰角负斜": { focal: 50.0 },
};

const PRESET_MODES = {
  "虫瞰极限仰视": { elevation_mode: "5bin", strength: 0.7 },
  "鸟瞰极限俯视": { elevation_mode: "5bin", strength: 0.7 },
};

export const PRESET_NAMES = ["自定义"].concat(PRESETS.map(p => p[0]));

/** 预设 → 完整参数覆盖 dict（与 presets.preset_params 同义）。 */
export function presetParams(name) {
  const row = PRESETS.find(p => p[0] === name);
  if (!row) return { pos_x: 0.0, pos_y: 0.0, pos_z: 0.0, roll: 0.0 };
  const pose = row[1];
  const out = { pos_x: pose[0], pos_y: pose[1], pos_z: pose[2], roll: pose[3] };
  return Object.assign(out, PRESET_EXTRAS[name] || {}, PRESET_MODES[name] || {});
}

// ══════════════════════ 基础工具 ══════════════════════
const _dv = new DataView(new ArrayBuffer(8));

/** double → [尾数 BigInt, 指数]，值 = 尾数 * 2^指数；用于精确十进制舍入。 */
function _ratio(x) {
  _dv.setFloat64(0, x);
  const hi = _dv.getUint32(0);
  const lo = _dv.getUint32(4);
  const e = (hi >>> 20) & 0x7ff;
  let man = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
  let exp;
  if (e === 0) { exp = -1074; } else { man |= (1n << 52n); exp = e - 1075; }
  return [man, exp];
}

/**
 * Python round() 的等价实现：按 double 的精确二进制值做十进制舍入，
 * 半值取偶（JS 的 Math.round 是半值进位，会出现 ±0.01 的 parity 差异）。
 */
export function pyRound(x, nd) {
  const v = Number(x);
  const n = nd || 0;
  if (!isFinite(v) || v === 0) return v;
  const neg = v < 0;
  const [man, exp] = _ratio(Math.abs(v));
  let num = man;
  let den = 1n;
  if (exp >= 0) num = man << BigInt(exp); else den = 1n << BigInt(-exp);
  const p = Math.pow(10, n);
  num *= BigInt(p);
  const q = num / den;
  const r = num % den;
  const twice = r * 2n;
  let out = q;
  if (twice > den || (twice === den && (q % 2n === 1n))) out = q + 1n;
  const val = Number(out) / p;
  return (neg ? -val : val) || 0;
}

export const clamp = (v, lo, hi) => {
  let x = Number(v);
  if (!isFinite(x)) x = lo;
  if (x !== x) x = lo;
  if (x < lo) return lo;
  if (x > hi) return hi;
  return x;
};

export const r2 = v => pyRound(v, 2);
export const r3 = v => pyRound(v, 3);
export const r4 = v => pyRound(v, 4);

/** "%.2f" 等价（Python 的格式化永远是两位小数）。 */
export const fmt2 = v => (Number(v) || 0).toFixed(2);

export function splitTags(text) {
  return String(text == null ? "" : text).split(",").map(s => s.trim()).filter(Boolean);
}

/** 把 "(front view:1.20)" 还原成 "front view"。 */
export function stripWeight(token) {
  let text = String(token == null ? "" : token).trim();
  if (text.startsWith("(") && text.endsWith(")") && text.indexOf(":") >= 0) {
    text = text.slice(1, -1);
    const idx = text.lastIndexOf(":");
    const head = text.slice(0, idx);
    const tail = text.slice(idx + 1);
    if (isFinite(parseFloat(tail)) && tail.trim() !== "") return head.trim();
    return text;
  }
  return text;
}

function deepCopy(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function deepMerge(base, patch) {
  const out = deepCopy(base);
  if (!patch || typeof patch !== "object") return out;
  Object.keys(patch).forEach(k => {
    const v = patch[k];
    if (v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object") {
      out[k] = deepMerge(out[k], v);
    } else {
      out[k] = deepCopy(v);
    }
  });
  return out;
}

// ══════════════════════ 权重曲线 ══════════════════════
/** SPEC 2.1：raw → SDXL 友好的 [0.05, 2.0]；raw=1 时恒为 1.00（裸写）。 */
export function sdxlWeight(raw, strength, curve) {
  let r = clamp(raw, 1e-3, 1e6);
  r = Math.max(1e-3, r);
  let st = strength == null ? 1.0 : Number(strength);
  if (!isFinite(st)) st = 1.0;
  if (curve === "raw") return r2(clamp(r, W_MIN_OUT, W_MAX_OUT));
  if (curve === "linear") return r2(clamp(1 + 0.06 * (r - 1) * st, W_MIN_OUT, W_MAX_OUT));
  const s = 0.55 * st;
  const k = 3.3;
  const w = r >= 1 ? 1 + s * (r - 1) / ((r - 1) + k) : 1 - s * (1 - r) / ((1 - r) + k);
  return r2(clamp(w, W_MIN_OUT, W_MAX_OUT));
}

/** SPEC 2.2：按逗号拆 tag 逐个加权；w≈1 时裸写。 */
export function emit(phrase, weight) {
  return splitTags(phrase).map(t => (Math.abs(weight - 1) < BARE_EPS ? t : "(" + t + ":" + fmt2(weight) + ")"));
}

// ══════════════════════ 各维度映射 ══════════════════════
/** 俯仰对水平方位的压制（0.9 处 1，1.0 处 0）。 */
export function azGate(posY) {
  return clamp((1 - Math.abs(clamp(posY, -1, 1))) / (1 - AZ_POLE), 0.0, 1.0);
}

/** 极点平滑：|pos_y|>0.75 起线性衰减，1.0（正顶/正底）归零。 */
export function poleGate(posY) {
  return clamp((1 - Math.abs(clamp(posY, -1, 1))) / (1 - POLE_START), 0.0, 1.0);
}

/** 返回 [[key, ratio], ...]；8bin 线性混合，4bin 为原版 cos/sin 对照。 */
export function azimuthBins(posX, mode, deadzoneRatio) {
  const dz = Math.max(0.0, Number(deadzoneRatio == null ? 0.06 : deadzoneRatio) || 0.0);
  if (mode === "4bin") {
    const a = clamp(posX, -1, 1) * Math.PI;
    const raw = {
      front: Math.max(0.0, Math.cos(a)),
      back: Math.max(0.0, -Math.cos(a)),
      right: Math.max(0.0, Math.sin(a)),
      left: Math.max(0.0, -Math.sin(a)),
    };
    const total = raw.front + raw.back + raw.right + raw.left;
    if (total > 0) { raw.front /= total; raw.back /= total; raw.right /= total; raw.left /= total; }
    return ["front", "back", "left", "right"].filter(k => raw[k] >= dz).map(k => [k, raw[k]]);
  }
  const theta = clamp(posX, -1, 1) * Math.PI;
  const f = (theta + Math.PI) / (Math.PI / 4.0);
  const fl = Math.floor(f);
  const i0 = ((fl % 8) + 8) % 8;
  const frac = f - fl;
  const pairs = [[i0, 1.0 - frac], [(i0 + 1) % 8, frac]];
  return pairs.filter(p => p[1] >= dz).map(p => [AZ_BINS[p[0]], p[1]]);
}

export function elevationKey(posY, mode) {
  const y = clamp(posY, -1, 1);
  if (mode === "5bin" && Math.abs(y) > 0.75) return y > 0 ? "bird" : "worm";
  if (y > 0.2) return "high";
  if (y >= -0.2) return "eye";
  return "low";
}

export function distanceKey(posZ) {
  const z = clamp(posZ, -1, 1);
  if (z > 0.7) return "ecu";
  if (z > 0.2) return "cu";
  if (z >= 0.0) return "medium";
  if (z > -0.35) return "cowboy";
  if (z >= -0.7) return "full";
  return "wide";
}

export function lensKey(focal) {
  const f = clamp(focal, 4.0, 1200.0);
  for (let i = 0; i < LENS_BANDS.length; i++) {
    if (f <= LENS_BANDS[i][0]) return LENS_BANDS[i][1];
  }
  return "lens135";
}

/** 水平视场角（36mm 全画幅宽），单位度。 */
export function focalToFov(focal) {
  const f = clamp(focal, 4.0, 1200.0);
  return r2((2.0 * Math.atan(36.0 / (2.0 * f))) * 180.0 / Math.PI);
}

/** 构图偏移 → [[key, ratio], ...]；pan_x>0 = 相机右移（主体偏左）。 */
export function framingKeys(panX, panY) {
  const px = clamp(panX, -1, 1);
  const py = clamp(panY, -1, 1);
  const out = [];
  if (Math.abs(px) >= 0.12) out.push([px > 0 ? "subject_left" : "subject_right", Math.min(1.0, Math.abs(px))]);
  if (Math.abs(py) >= 0.12) out.push([py > 0 ? "subject_low" : "subject_high", Math.min(1.0, Math.abs(py))]);
  if (!out.length) out.push(["subject_center", 1.0]);
  return out;
}

/** 主体朝向 / 视线（与机位解耦）→ [subjectKey, gazeKey, ratio]。 */
export function subjectKeys(subjectYaw, posX, gazeMode) {
  const yaw = clamp(subjectYaw, -1, 1);
  const mag = Math.abs(yaw);
  let skey;
  let sratio;
  if (mag < 0.28) { skey = "subject_front"; sratio = 1.0; }
  else if (mag < 0.72) { skey = "subject_turn"; sratio = Math.min(1.0, mag); }
  else { skey = "subject_away"; sratio = Math.min(1.0, mag); }

  let behind = 0.0;
  azimuthBins(posX).forEach(([key, ratio]) => {
    if (key === "back" || key === "back_left" || key === "back_right") behind += ratio;
  });

  const mode = String(gazeMode || "auto");
  let gkey;
  if (mode === "camera") gkey = "gaze_viewer";
  else if (mode === "away") gkey = "gaze_away";
  else if (skey === "subject_front") gkey = "gaze_viewer";
  else if (behind > 0.45 && mag <= 0.72) gkey = "gaze_over_shoulder";
  else gkey = "gaze_away";
  return [skey, gkey, sratio];
}

// ══════════════════════ 词表 / 权重装载 ══════════════════════
/** 内置词表深拷贝（含 neg 与 weights）。 */
export function defaultTags() {
  return { tags: deepCopy(Object.assign({}, V, { neg: NEG, weights: DEFAULT_WEIGHTS })) };
}

function _baseTags() {
  const t = deepCopy(V);
  t.neg = deepCopy(NEG);
  t.weights = deepCopy(DEFAULT_WEIGHTS);
  return t;
}

/** 兼容 _cfg_json：字符串 JSON、{tags:{...}} 或扁平 {...}。 */
export function loadTags(cfg) {
  let c = cfg;
  if (typeof c === "string") {
    try { c = JSON.parse(c); } catch (e) { c = null; }
  }
  const tags = _baseTags();
  if (!c || typeof c !== "object") return tags;
  const patch = (c.tags && typeof c.tags === "object") ? c.tags : c;
  Object.keys(patch).forEach(k => {
    const v = patch[k];
    if (k in tags && v && typeof v === "object" && !Array.isArray(v) && tags[k] && typeof tags[k] === "object") {
      Object.assign(tags[k], v);
    } else if (typeof v === "string") {
      tags[k] = v;
    }
  });
  return tags;
}

export function resolveWeights(tags) {
  const w = (tags && tags.weights) || {};
  const merge = (base, patch) => Object.assign({}, base, patch || {});
  return {
    azimuth: merge(DEFAULT_WEIGHTS.azimuth, w.azimuth),
    elevation: merge(DEFAULT_WEIGHTS.elevation, w.elevation),
    distance: merge(DEFAULT_WEIGHTS.distance, w.distance),
    tilt: merge(DEFAULT_WEIGHTS.tilt, w.tilt),
    lens: merge(DEFAULT_WEIGHTS.lens, w.lens),
    framing: merge(DEFAULT_WEIGHTS.framing, w.framing),
    subject: merge(DEFAULT_WEIGHTS.subject, w.subject),
    extra_master: Number(w.extra_master == null ? 1.0 : (w.extra_master || 1.0)),
    weight_min: Number(w.weight_min == null ? 0.1 : (w.weight_min || 0.1)),
    weight_max: Number(w.weight_max == null ? 10.0 : (w.weight_max || 10.0)),
    neg_weight: Number(w.neg_weight == null ? 1.15 : (w.neg_weight || 1.15)),
  };
}

// ══════════════════════ 主入口 ══════════════════════
/**
 * 3D 机位 → SDXL 正向 / 负向提示词。
 * opts: preset, strength, weight_mode, azimuth_mode, elevation_mode, neg_strength,
 *       extra_positive, extra_negative, cfg, focal, pan_x, pan_y, subject_yaw, gaze_mode
 * 返回 {positive, negative, camera_info, info, parts, negative_parts, negativeParts, bins}
 */
export function computeCamera(posX, posY, posZ, roll, opts) {
  const o = opts || {};
  const pz = clamp(posZ, -1, 1);
  const py = clamp(posY, -1, 1);
  const px = clamp(posX, -1, 1);
  const rv = clamp(roll, -1, 1);
  const focal = clamp(o.focal == null ? 50.0 : o.focal, 4.0, 1200.0);
  const panX = clamp(o.pan_x == null ? 0.0 : o.pan_x, -1, 1);
  const panY = clamp(o.pan_y == null ? 0.0 : o.pan_y, -1, 1);
  const subjectYaw = clamp(o.subject_yaw == null ? 0.0 : o.subject_yaw, -1, 1);

  const tags = loadTags(o.cfg);
  const weights = resolveWeights(tags);
  const negMap = (tags.neg && typeof tags.neg === "object") ? tags.neg : {};

  const strength = Number(o.strength == null ? 1.0 : o.strength);
  const curve = String(o.weight_mode || o.weightMode || "tanh");
  const azMode = String(o.azimuth_mode || o.azimuthMode || "8bin");
  const elMode = String(o.elevation_mode || o.elevationMode || "3bin");
  // 与 camera_core.py 一致：显式传 None/缺省等价于 0（Python 里 absent → 0.0）
  const negStrength = Number(o.neg_strength == null ? 0.0 : o.neg_strength);
  const gazeMode = String(o.gaze_mode || o.gazeMode || "auto");

  let posTokens = [];
  let negTokens = [];
  let parts = [];
  const bins = [];

  const push = (kind, key, ratio, raw, weight, note, phraseOverride) => {
    const phrase = phraseOverride || tags[key];
    if (!phrase) return;
    const tokens = emit(phrase, weight);
    if (!tokens.length) return;
    posTokens = posTokens.concat(tokens);
    parts.push({
      kind: kind, key: key, ratio: r4(Number(ratio) || 0), raw: r4(Number(raw) || 0),
      weight: r3(Number(weight) || 0), phrase: phrase, tokens: tokens, note: note || "",
    });
    bins.push([key, r4(Number(ratio) || 0), kind]);
  };

  // ── 1. 方位（含极点衰减） ──
  const gate = azGate(py) * poleGate(py);
  const azCfg = weights.azimuth;
  let azSuppressed = false;
  if (azCfg.enabled !== false && gate >= AZ_SUPPRESS) {
    const dzr = azCfg.deadzone_ratio == null ? 0.06 : azCfg.deadzone_ratio;
    azimuthBins(px, azMode, dzr).forEach(([key, ratio]) => {
      const raw = ratio * Number(azCfg.weight == null ? 10.0 : azCfg.weight) * gate + Number(azCfg.extra || 0);
      if (raw < 0.05) return;
      push("az", key, ratio, raw, sdxlWeight(raw, strength, curve));
    });
  } else {
    azSuppressed = (azCfg.enabled === false) || gate < AZ_SUPPRESS;
  }

  // ── 2. 高度 ──
  const ekey = elevationKey(py, elMode);
  const elCfg = weights.elevation;
  if (elCfg.enabled !== false) {
    let eraw = ekey === "eye" ? 1.0 : Math.abs(py) * Number(elCfg.mult == null ? 10.0 : elCfg.mult);
    eraw += Number(elCfg.extra || 0);
    push("el", ekey, 1.0, eraw, sdxlWeight(eraw, strength, curve));
  }

  // ── 3. 距离 ──
  const dkey = distanceKey(pz);
  const dCfg = weights.distance;
  if (dCfg.enabled !== false) {
    const draw = 1 + Number(dCfg.slope == null ? 0.5 : dCfg.slope) * Math.abs(pz) + Number(dCfg.extra || 0);
    push("di", dkey, 1.0, draw, sdxlWeight(draw, strength, curve));
  }

  // ── 4. 焦距 / 光学 ──
  const lkey = lensKey(focal);
  const lensCfg = weights.lens;
  if (lensCfg.enabled !== false) {
    const lraw = (LENS_WEIGHT[lkey] == null ? 1.0 : LENS_WEIGHT[lkey]) + Number(lensCfg.extra || 0);
    push("lens", lkey, 1.0, lraw, sdxlWeight(lraw, strength, curve));
  }

  // ── 5. 倾斜（±45°，负值＝反向倾斜） ──
  const tiltCfg = weights.tilt;
  const rollDeg = r2(rv * 45.0);
  let dutch = false;
  if (tiltCfg.enabled !== false && Math.abs(rv) > 0.25) {
    dutch = true;
    const traw = 1 + Number(tiltCfg.slope == null ? 6.0 : tiltCfg.slope) * Math.abs(rv) + Number(tiltCfg.extra || 0);
    push("tg", rv > 0 ? "dutch" : "dutch_neg", 1.0, traw, sdxlWeight(traw, strength, curve));
  }

  // ── 6. 构图偏移（三分法 / 留白） ──
  const fCfg = weights.framing;
  const framing = [];
  if (fCfg.enabled !== false) {
    framingKeys(panX, panY).forEach(([key, ratio]) => {
      // 正中构图是默认态，除非 cfg 显式 emit_center:true，否则不烧 token
      if (key === "subject_center" && !fCfg.emit_center) return;
      framing.push(key);
      const fraw = key === "subject_center"
        ? 1.0
        : 1 + Number(fCfg.slope == null ? 2.0 : fCfg.slope) * ratio + Number(fCfg.extra || 0);
      push("frame", key, ratio, fraw, sdxlWeight(fraw, strength, curve));
    });
  }

  // ── 7. 主体朝向 / 视线 ──
  const sCfg = weights.subject;
  const sk = subjectKeys(subjectYaw, px, gazeMode);
  const skey = sk[0];
  const gkey = sk[1];
  const sratio = sk[2];
  let frontAz = 0.0;
  azimuthBins(px).forEach(([key, ratio]) => { if (key === "front") frontAz = ratio; });
  if (sCfg.enabled !== false) {
    // 机位在正面且主体也正对时，"front-facing body" 与方位词重复，跳过
    if (sratio > 0 && !(skey === "subject_front" && frontAz > 0.5)) {
      const sraw = 1 + Number(sCfg.slope == null ? 2.0 : sCfg.slope) * (skey === "subject_front" ? 0.0 : sratio);
      push("subject", skey, sratio, sraw, sdxlWeight(sraw, strength, curve));
    }
    const graw = gkey === "gaze_over_shoulder" ? 1.30 : 1.0;
    let gphrase = null;
    if (gkey === "gaze_viewer" && Math.abs(py) > 0.75) {
      gphrase = py > 0 ? POLE_GAZE.bird : POLE_GAZE.worm;
    }
    push("gaze", gkey, 1.0, graw, sdxlWeight(graw, strength, curve), "", gphrase);
  }

  // ── 8. 极点互斥：剔除与竖直轴冲突的方位 / 视线 token ──
  const blocked = (Math.abs(py) > 0.75 && POLE_BLOCK_TOKENS[ekey]) ? POLE_BLOCK_TOKENS[ekey] : [];
  if (blocked.length) {
    const kept = [];
    parts.forEach(part => {
      if (part.kind !== "az" && part.kind !== "gaze" && part.kind !== "subject") { kept.push(part); return; }
      const tokens = part.tokens.filter(t => blocked.indexOf(stripWeight(t)) < 0);
      if (tokens.length) { part.tokens = tokens; kept.push(part); }
    });
    parts = kept;
    posTokens = [];
    parts.forEach(part => { posTokens = posTokens.concat(part.tokens); });
  }

  // ── 9. 负向镜像 ──
  const negParts = [];
  if (negStrength > 0) {
    const nw = r2(clamp(1 + 0.15 * negStrength, W_MIN_OUT, W_MAX_OUT));
    const seen = {};
    parts.forEach(part => {
      const phrase = negMap[part.key];
      if (!phrase) return;
      splitTags(phrase).forEach(token => {
        if (seen[token]) return;
        seen[token] = true;
        negTokens.push("(" + token + ":" + fmt2(nw) + ")");
        negParts.push({ key: part.key, token: token, weight: nw });
      });
    });
  }

  const master = Number(weights.extra_master == null ? 1.0 : weights.extra_master) || 1.0;
  let extraPos = String(o.extra_positive == null ? "" : o.extra_positive).trim();
  const extraNeg = String(o.extra_negative == null ? "" : o.extra_negative).trim();
  if (extraPos && master !== 1.0) {
    extraPos = emit(extraPos, r2(clamp(master, W_MIN_OUT, W_MAX_OUT))).join(", ");
  }

  const posAll = posTokens.filter(Boolean);
  if (extraPos) posAll.push(extraPos);
  const positive = posAll.join(", ");
  const negAll = negTokens.slice();
  if (extraNeg) negAll.push(extraNeg);
  const negative = negAll.join(", ");

  let fanRatio = 0.0;
  azimuthBins(px).forEach(([key, ratio]) => {
    if (key === "back" || key === "back_left" || key === "back_right") fanRatio += ratio;
  });

  const info = {
    azimuth_deg: r2(px * 180.0),
    elev_deg: r2(py * 90.0),
    fov_deg: focalToFov(focal),
    focal: r2(focal),
    lens: lkey,
    bin: bins.length ? bins[0][0] : "-",
    bins: bins.map(b => [b[0], b[1], b[2]]),  // [[key, ratio, kind]]
    elevation: ekey,
    distance: dkey,
    dutch: !!dutch,
    roll_deg: rollDeg,
    az_gate: r4(gate),
    pole_gate: r4(poleGate(py)),
    azimuth_suppressed: !!azSuppressed,
    behind_ratio: r3(fanRatio),
    framing: framing,
    subject: skey,
    gaze: gkey,
    subject_yaw: r3(subjectYaw),
    pan: [r3(panX), r3(panY)],
    preset: String(o.preset || "自定义"),
    strength: r3(strength),
    weight_mode: curve,
    azimuth_mode: azMode,
    elevation_mode: elMode,
    neg_strength: r3(negStrength),
    parts: parts,
    negative_parts: negParts,
  };

  return {
    positive: positive,
    negative: negative,
    camera_info: info,
    info: info,
    parts: parts,
    negative_parts: negParts,
    negativeParts: negParts,
    bins: bins,
  };
}

export const SDXL_TAGS = V;
export { V, NEG };   // 供 js/sdxl_camera_widget.js 与标签面板直接引用

export default {
  V: V, NEG: NEG, PRESETS: PRESETS, PRESET_NAMES: PRESET_NAMES,
  computeCamera: computeCamera, sdxlWeight: sdxlWeight, focalToFov: focalToFov,
};
