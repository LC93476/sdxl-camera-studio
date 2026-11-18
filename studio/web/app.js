/**
 * app.js —— SDXL 相机工作台前端主控（v2）
 *
 * 职责：
 *   1. 组装 CameraRig（3D）+ 滑块，双向同步，200ms 防抖调 /api/preview
 *   2. 生成流程：/api/generate → 700ms 轮询 /api/progress → 进度条 / 状态 / 画廊插顶
 *   3. v2：focal/pan_x/pan_y/subject_yaw/gaze_mode、token 预算面板、序列并排对比、预设导入导出
 *   4. 启动引导：/api/meta、/api/config、/api/comfy/models、/api/health、/api/gallery、/api/sequences
 *
 * 后端离线时页面照常可用：所有请求都走 try/catch，只在 #status 里说真话。
 */
import { CameraRig } from './camera3d.js';
import { focalToFov, framingKeys, subjectKeys } from '/js/camera_shared.js';
// task-7 契约：js/camera_shared.js 提供 clipTokens / budgetOf（单一真源）。
// 用命名空间导入 + 存在性检查，共享模块还没落地时页面照常跑；一落地就自动切过去。
import * as SHARED from '/js/camera_shared.js';

const API = {
  health: '/api/health',
  meta: '/api/meta',
  preview: '/api/preview',
  generate: '/api/generate',
  gallery: '/api/gallery',
  galleryClear: '/api/gallery/clear',
  models: '/api/comfy/models',
  config: '/api/config',
  uiPresets: '/api/ui_presets',
  presetsExport: '/api/ui_presets/export',
  presetsImport: '/api/ui_presets/import',
  sequences: '/api/sequences',
  sequence: '/api/sequence',
  interrupt: '/api/interrupt',
  openFolder: '/api/open_folder',
  progress: (promptId) => '/api/progress?prompt_id=' + encodeURIComponent(promptId),
  controlnetUpload: '/api/controlnet/upload',
  templates: '/api/templates',
  templatesInspect: '/api/templates/inspect',
  batch: '/api/batch',
  batchGet: (batchId) => '/api/batch?batch_id=' + encodeURIComponent(batchId),
};

const $ = (id) => document.getElementById(id);

// token 预算阈值（相机正向段）：> 65 标黄，> 75 标红（Chunk 0 上限）
const TOKEN_WARN = 65;
const TOKEN_LIMIT = 75;

/**
 * clipTokens 兜底：按逗号切 tag、每个词 1 token、长词（>6 字符）算 2。
 * js/camera_shared.js 一旦导出 clipTokens / budgetOf，就用共享实现（下面两行常量选择）。
 */
function fallbackClipTokens(text) {
  const s = String(text == null ? '' : text).trim();
  if (!s) return 0;
  let n = 0;
  s.split(',').forEach((tag) => {
    tag.trim().split(/\s+/).forEach((w) => {
      if (w) n += w.length > 6 ? 2 : 1;
    });
  });
  return n;
}

function fallbackBudgetOf(positive, negative) {
  const p = fallbackClipTokens(positive);
  const n = fallbackClipTokens(negative);
  return { positive: p, negative: n, total: p + n, limit: TOKEN_LIMIT, warn: TOKEN_WARN };
}

const clipTokens = typeof SHARED.clipTokens === 'function' ? SHARED.clipTokens : fallbackClipTokens;
const budgetOf = typeof SHARED.budgetOf === 'function' ? SHARED.budgetOf : fallbackBudgetOf;

// focal 快捷档（mm）
const FOCAL_STOPS = [14, 24, 35, 50, 85, 135];

const DEFAULTS = {
  pos_x: 0, pos_y: 0, pos_z: 0, roll: 0,
  focal: 50, pan_x: 0, pan_y: 0, subject_yaw: 0, gaze_mode: 'auto',
  ref_opacity: 0.35,
  preset: '自定义', strength: 1, weight_mode: 'tanh',
  azimuth_mode: '8bin', elevation_mode: '3bin', neg_strength: 1,
  extra_positive: '', extra_negative: '',
  manual_positive: '', manual_negative: '',
  seed: -1, randomize: true, steps: 28, cfg: 5, width: 1024, height: 1024,
  checkpoint: '', lora: '', lora_strength: 1,
  workflow_template: '', pose_id: '',
  // v2.3：ControlNet 导出 / 模板嗅探 / 批量队列
  cn_auto_inject: true, cn_kind: 'depth', cn_strength: 1,
  cn_depth_file: '', cn_pose_file: '',
  template: '',
  batch_name: '三视图', batch_seed: 12345, batch_same_seed: true,
};

const ENUM_FALLBACK = {
  weight_mode: ['tanh', 'linear', 'raw'],
  azimuth_mode: ['8bin', '4bin'],
  elevation_mode: ['3bin', '5bin'],
  gaze_mode: ['auto', 'camera', 'away'],
};

const CN_BIN = {
  front: '正面', front_right: '右前方', right: '右侧', back_right: '右后方',
  back: '背面', back_left: '左后方', left: '左侧', front_left: '左前方',
};
const CN_EL = { high: '俯视', eye: '平视', low: '仰视', bird: '鸟瞰', worm: '虫瞰' };
const CN_DIST = { ecu: '特写', cu: '近景', medium: '中景', cowboy: '牛仔', full: '全身', wide: '远景' };
const CN_LENS = { lens14: '14mm', lens24: '24mm', lens35: '35mm', lens50: '50mm', lens85: '85mm', lens135: '135mm' };
const CN_FRAME = {
  subject_left: '主体偏左', subject_right: '主体偏右', subject_center: '居中',
  subject_low: '主体偏低', subject_high: '主体偏高',
};
const CN_SUBJECT = { subject_front: '正面朝向', subject_turn: '四分之三侧身', subject_away: '背身' };
const CN_GAZE = { gaze_viewer: '看镜头', gaze_away: '视线离镜', gaze_over_shoulder: '回眸看镜头' };
// token 分段 kind 码 → 中文（后端 kind 就是 az/el/di/lens/gaze 这些短码）
const TK_KIND = {
  az: '方位', el: '高度', di: '距离', lens: '镜头', gaze: '视线',
  subject: '朝向', framing: '构图', extra: '附加', manual: '手动', weight: '权重',
};

// 数值型相机滑块（行为一致的走同一套绑定）
const CAM_KEYS = ['pos_x', 'pos_y', 'pos_z', 'roll', 'pan_x', 'pan_y', 'subject_yaw'];
const CAM_TEXT_KEYS = ['extra_positive', 'extra_negative', 'manual_positive', 'manual_negative'];

const S = Object.assign({}, DEFAULTS);

let rig = null;
let meta = null;
let metaPresets = {};
let uiPresets = {};
let presetModes = {};
let seqDefs = {};

let previewSeq = 0;
let previewTimer = 0;
let configTimer = 0;
let syncing = false;

let promptId = null;
let pollTimer = 0;
let genBusy = false;
let lastSeed = null;

let seqTimers = [];
let seqRunning = false;

let lbList = [];
let lbIndex = 0;
let lbZoom = 1;

// ─────────────────────────── 基础工具 ───────────────────────────

function num(v, fallback = 0) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

async function api(path, options) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_) {
      data = { error: text.slice(0, 300) };
    }
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || 'HTTP ' + res.status + ' ' + path;
    throw new Error(msg);
  }
  return data;
}

function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body === undefined ? {} : body),
  });
}

function setStatus(text, kind) {
  const el = $('status');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('err', kind === 'err');
  el.classList.toggle('ok', kind === 'ok');
}

function setProgress(p) {
  const el = $('progressbar');
  if (!el) return;
  el.value = Math.round(clamp(p, 0, 1) * 100);
}

function setBadge(name, text) {
  const el = document.querySelector('[data-badge="' + name + '"]');
  if (el) el.textContent = text;
}

function readout(kind) {
  return document.querySelector('[data-val="' + kind + '"]');
}

function refreshReadouts() {
  CAM_KEYS.forEach((k) => {
    const el = readout(k);
    if (el) el.textContent = num(S[k]).toFixed(3);
  });
  ['strength', 'neg_strength', 'lora_strength'].forEach((k) => {
    const el = readout(k);
    if (el) el.textContent = num(S[k]).toFixed(2);
  });
  const ro = readout('ref_opacity');
  if (ro) ro.textContent = num(S.ref_opacity, 0.35).toFixed(2);
  updateFocalUI();
  updateFramingHint();
}

// ─────────────────────────── 焦段 / 构图提示 ───────────────────────────

function focalValue() {
  return Math.round(clamp(num(S.focal, 50), 14, 200));
}

function updateFocalUI() {
  const f = focalValue();
  const label = $('focal_label');
  if (label) label.textContent = f + 'mm · ' + focalToFov(f).toFixed(1) + '°';
  const row = $('btn_focal_preset');
  if (!row) return;
  Array.prototype.slice.call(row.querySelectorAll('.chip')).forEach((b) => {
    b.classList.toggle('on', Math.round(num(b.dataset.focal)) === f);
  });
}

/** 三分法/构图提示：pan_x、pan_y、subject_yaw、gaze_mode 一句话说明 */
function framingWords() {
  const out = [];
  let keys = [];
  try {
    keys = framingKeys(num(S.pan_x), num(S.pan_y)) || [];
  } catch (_) {
    keys = [];
  }
  if (keys.length) out.push(keys.map((k) => CN_FRAME[k[0]] || k[0]).join(' + '));
  let sk = null;
  try {
    sk = subjectKeys(num(S.subject_yaw), num(S.pos_x), S.gaze_mode);
  } catch (_) {
    sk = null;
  }
  if (sk) {
    out.push(CN_SUBJECT[sk[0]] || sk[0]);
    out.push(CN_GAZE[sk[1]] || sk[1]);
  }
  return out.length ? out.join(' · ') : '居中构图';
}

function updateFramingHint() {
  const el = $('framing_hint');
  if (el) el.textContent = framingWords();
}

// ─────────────────────────── 载荷 ───────────────────────────

function cameraPayload() {
  return {
    pos_x: num(S.pos_x),
    pos_y: num(S.pos_y),
    pos_z: num(S.pos_z),
    roll: num(S.roll),
    focal: focalValue(),
    pan_x: num(S.pan_x),
    pan_y: num(S.pan_y),
    subject_yaw: num(S.subject_yaw),
    gaze_mode: S.gaze_mode || 'auto',
    preset: S.preset || '自定义',
    strength: num(S.strength, 1),
    weight_mode: S.weight_mode || 'tanh',
    azimuth_mode: S.azimuth_mode || '8bin',
    elevation_mode: S.elevation_mode || '3bin',
    neg_strength: num(S.neg_strength, 1),
    extra_positive: S.extra_positive || '',
    extra_negative: S.extra_negative || '',
    manual_positive: S.manual_positive || '',
    manual_negative: S.manual_negative || '',
  };
}

function genPayload() {
  return {
    seed: Math.trunc(num(S.seed, -1)),
    randomize: !!S.randomize,
    steps: Math.trunc(num(S.steps, 28)),
    cfg: num(S.cfg, 5),
    width: Math.trunc(num(S.width, 1024)),
    height: Math.trunc(num(S.height, 1024)),
    checkpoint: S.checkpoint || '',
    lora_name: S.lora || '',
    lora_strength: num(S.lora_strength, 1),
    workflow_template: S.workflow_template || '',
    pose_id: (S.pose_id || '').trim(),
    // v2.3：可选工作流模板（template_select）+ 已上传的 ControlNet 图
    template: S.template || '',
    template_vars: controlnetVars(),
  };
}

// ─────────────────────────── 预览 ───────────────────────────

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    previewTimer = 0;
    runPreview();
  }, 200);
}

async function runPreview() {
  const seq = ++previewSeq;
  try {
    const data = await postJSON(API.preview, cameraPayload());
    if (seq !== previewSeq) return;
    $('positive_out').textContent = data && typeof data.positive === 'string' ? data.positive : '';
    $('negative_out').textContent = data && typeof data.negative === 'string' ? data.negative : '';
    renderTokens(data);
    let info = infoLine(data && data.camera_info);
    const warn = data && Array.isArray(data.warn) ? data.warn.filter(Boolean) : [];
    if (warn.length) info = info + ' · ⚠ ' + warn.join('；');
    $('info_out').textContent = info;
  } catch (err) {
    if (seq !== previewSeq) return;
    setStatus('预览失败：' + err.message, 'err');
  }
}

function infoLine(info) {
  if (!info || typeof info !== 'object') return '—';
  const parts = [];
  if (Array.isArray(info.bins) && info.bins.length) {
    parts.push(info.bins
      .map((b) => (CN_BIN[b[0]] || b[0]) + Math.round(num(b[1]) * 100) + '%')
      .join(' / '));
  } else if (info.bin) {
    parts.push(CN_BIN[info.bin] || info.bin);
  }
  if (info.elevation) parts.push(CN_EL[info.elevation] || info.elevation);
  if (info.distance) parts.push(CN_DIST[info.distance] || info.distance);
  if (info.dutch) parts.push('荷兰角');
  if (Number.isFinite(info.azimuth_deg)) parts.push('方位角 ' + info.azimuth_deg + '°');
  if (Number.isFinite(info.elev_deg)) parts.push('仰角 ' + info.elev_deg + '°');
  if (Number.isFinite(info.az_gate) && info.az_gate < 1) parts.push('方位闸门 ' + info.az_gate);
  if (Number.isFinite(info.focal)) parts.push(Math.round(info.focal) + 'mm');
  if (Number.isFinite(info.fov_deg)) parts.push('FOV ' + info.fov_deg + '°');
  if (info.lens) parts.push(CN_LENS[info.lens] || String(info.lens));
  if (Array.isArray(info.framing) && info.framing.length) {
    parts.push(info.framing.map((k) => CN_FRAME[k[0] || k] || k).join(' + '));
  }
  if (Array.isArray(info.subject) && info.subject.length) {
    parts.push(info.subject.map((k) => CN_SUBJECT[k] || CN_GAZE[k] || k).join(' / '));
  }
  return parts.length ? parts.join(' · ') : '—';
}

// ─────────────────────────── Token 预算 ───────────────────────────

function tokenLevel(pos, warn, limit) {
  if (pos > limit) return 'danger';
  if (pos > warn) return 'warn';
  return '';
}

function paintTokenBadges(pos, warn, limit) {
  const level = tokenLevel(pos, warn, limit);
  ['token_badge', 'token_total'].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.classList.toggle('tok-warn', level === 'warn');
    el.classList.toggle('tok-danger', level === 'danger');
    // styles.css 不在本任务文件范围，颜色内联兜底：标黄 / 标红一定看得见
    el.style.color = level === 'danger' ? '#ff6b6b' : level === 'warn' ? '#ffcf5c' : '';
  });
}

// 超上限提示行（动态建节点，不新增 DOM id）
let tokenHintEl = null;

function paintTokenHint(text) {
  if (!tokenHintEl) {
    const host = $('token_total');
    const parent = host ? host.parentNode : null;
    if (!parent) return;
    tokenHintEl = document.createElement('div');
    tokenHintEl.className = 'tk-warnline';
    tokenHintEl.style.marginTop = '4px';
    tokenHintEl.style.fontSize = '11px';
    tokenHintEl.style.lineHeight = '1.4';
    const parts = $('token_parts');
    if (parts && parts.parentNode === parent) parent.insertBefore(tokenHintEl, parts);
    else parent.appendChild(tokenHintEl);
  }
  tokenHintEl.textContent = text || '';
  tokenHintEl.hidden = !text;
  if (text) tokenHintEl.style.color = '#ff6b6b';
}

function renderTokens(data) {
  const tk = (data && data.tokens) || {};
  const positiveText = data && typeof data.positive === 'string' ? data.positive : '';
  const negativeText = data && typeof data.negative === 'string' ? data.negative : '';

  // 预算：limit / warn 由共享模块给（budgetOf），拿不到就用本地常量
  const budget = budgetOf(positiveText, negativeText) || {};
  const limit = Math.round(num(budget.limit, TOKEN_LIMIT));
  const warn = Math.round(num(budget.warn, TOKEN_WARN));

  // 数值优先用后端 /api/preview 的 tokens.positive，拿不到用 clipTokens 兜底
  const pos = Number.isFinite(Number(tk.positive))
    ? Math.round(Number(tk.positive))
    : Math.round(num(budget.positive, clipTokens(positiveText)));
  const neg = Number.isFinite(Number(tk.negative))
    ? Math.round(Number(tk.negative))
    : Math.round(num(budget.negative, clipTokens(negativeText)));

  const badge = $('token_badge');
  if (badge) badge.textContent = 'P ' + pos + ' · N ' + neg;
  const tot = $('token_total');
  if (tot) tot.textContent = pos + ' / ' + limit + ' tok';
  paintTokenBadges(pos, warn, limit);
  paintTokenHint(pos > limit ? '已超 Chunk 0 上限，相机词会被截断' : '');

  const list = $('token_parts');
  if (!list) return;
  list.textContent = '';
  const parts = (data && Array.isArray(data.token_parts)) ? data.token_parts : [];
  if (!parts.length) {
    const ph = document.createElement('div');
    ph.className = 'tk-empty';
    ph.textContent = '（后端未返回 token_parts）';
    list.appendChild(ph);
    return;
  }
  parts.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'tk-row';

    const kind = document.createElement('span');
    kind.className = 'tk-kind';
    const code = String((p && p.kind) || 'part');
    kind.textContent = TK_KIND[code] ? code + ' · ' + TK_KIND[code] : code;

    const text = document.createElement('span');
    text.className = 'tk-text';
    text.textContent = String((p && p.text) || '');

    const n = document.createElement('span');
    n.className = 'tk-num';
    const cnt = Math.round(num(p && p.tokens, 0));
    n.textContent = String(cnt);
    n.classList.toggle('hot', cnt >= 12);

    row.appendChild(kind);
    row.appendChild(text);
    row.appendChild(n);
    list.appendChild(row);
  });
}

// ─────────────────────────── 双向同步 ───────────────────────────

function pushRig() {
  if (!rig || syncing) return;
  rig.setState({
    pos_x: num(S.pos_x),
    pos_y: num(S.pos_y),
    pos_z: num(S.pos_z),
    roll: num(S.roll),
    focal: focalValue(),
    pan_x: num(S.pan_x),
    pan_y: num(S.pan_y),
    subject_yaw: num(S.subject_yaw),
    gaze_mode: S.gaze_mode || 'auto',
  });
}

function writeCameraUI() {
  syncing = true;
  CAM_KEYS.forEach((k) => {
    const el = $(k);
    if (el) el.value = String(num(S[k]));
  });
  const fo = $('focal');
  if (fo) fo.value = String(focalValue());
  const ro = $('ref_opacity');
  if (ro) ro.value = String(num(S.ref_opacity, 0.35));
  ['strength', 'neg_strength'].forEach((k) => {
    const el = $(k);
    if (el) el.value = String(num(S[k]));
  });
  ['weight_mode', 'azimuth_mode', 'elevation_mode', 'gaze_mode'].forEach((k) => {
    const el = $(k);
    if (el && S[k] != null) el.value = String(S[k]);
  });
  if ($('preset')) $('preset').value = S.preset || '自定义';
  refreshReadouts();
  syncing = false;
}

function markCustom() {
  if (S.preset === '自定义') return;
  S.preset = '自定义';
  const el = $('preset');
  if (el) el.value = '自定义';
}

function onRigChange(v, meta) {
  if (syncing) return;
  CAM_KEYS.forEach((k) => {
    if (Number.isFinite(v[k])) S[k] = v[k];
  });
  if (Number.isFinite(v.focal)) S.focal = Math.round(clamp(v.focal, 14, 200));
  if (typeof v.gaze_mode === 'string' && v.gaze_mode) S.gaze_mode = v.gaze_mode;
  writeCameraUI();
  markCustom();
  schedulePreview();
  // 拖拽中（meta.live）只刷预览；松手那次不带 live，才提交持久化
  if (!(meta && meta.live)) scheduleConfigSave();
}

// ─────────────────────────── 配置持久化 ───────────────────────────

function scheduleConfigSave() {
  if (configTimer) clearTimeout(configTimer);
  configTimer = setTimeout(() => {
    configTimer = 0;
    saveConfig();
  }, 700);
}

async function saveConfig() {
  const body = Object.assign({}, cameraPayload(), genPayload(), {
    // v2.3 面板状态：模板选择 + ControlNet + 批量队列默认值（都只是 UI 偏好）
    template: S.template || '',
    cn_auto_inject: !!S.cn_auto_inject,
    cn_kind: S.cn_kind || 'depth',
    cn_strength: clamp(num(S.cn_strength, 1), 0, 2),
    cn_depth_file: S.cn_depth_file || '',
    cn_pose_file: S.cn_pose_file || '',
    batch_name: S.batch_name || '三视图',
    batch_seed: Math.trunc(num(S.batch_seed, 12345)),
    batch_same_seed: S.batch_same_seed !== false,
  });
  try {
    await postJSON(API.config, body);
  } catch (_) {
    /* 配置保存失败不打断操作，值仍在内存里 */
  }
}

function flattenConfig(raw) {
  const out = {};
  const take = (obj) => {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
    Object.keys(obj).forEach((k) => {
      const v = obj[k];
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        if (k === 'params' || k === 'camera' || k === 'gen' || k === 'generate' || k === 'config' || k === 'defaults' || k === 'state') {
          take(v);
        }
      } else {
        out[k] = v;
      }
    });
  };
  take(raw);
  return out;
}

function applyConfig(raw) {
  const flat = flattenConfig(raw);
  Object.keys(flat).forEach((k) => {
    const key = k === 'lora_name' ? 'lora' : k;
    if (!(key in DEFAULTS)) return;
    const v = flat[k];
    if (key === 'randomize' || key === 'cn_auto_inject' || key === 'batch_same_seed') S[key] = !!v;
    else if (typeof DEFAULTS[key] === 'number') S[key] = num(v, DEFAULTS[key]);
    else S[key] = v == null ? DEFAULTS[key] : String(v);
  });
}

// ─────────────────────────── 生成 ───────────────────────────

function setGenBusy(busy) {
  genBusy = busy;
  const btn = $('btn_generate');
  if (!btn) return;
  btn.disabled = busy;
  btn.textContent = busy ? '生成中…' : '生成图片';
}

async function onGenerate() {
  if (genBusy) return;
  setGenBusy(true);
  setProgress(0);
  setStatus('正在提交…');
  lastSeed = null;
  try {
    const data = await postJSON(API.generate, Object.assign({}, cameraPayload(), genPayload()));
    promptId = data && data.prompt_id;
    if (!promptId) throw new Error('后端未返回 prompt_id');
    if (data && data.seed_used != null) {
      lastSeed = data.seed_used;
      const tplName = data && data.template ? ' · 模板 ' + String(data.template).replace(/\.json$/, '') : '';
      const warn = (data && Array.isArray(data.warn) && data.warn.length) ? ' · ' + data.warn.join('；') : '';
      setStatus('已提交 · seed ' + data.seed_used + tplName + warn + ' · 等待 ComfyUI…');
    } else {
      setStatus('已提交 · 等待 ComfyUI…');
    }
    pollTimer = setTimeout(pollProgress, 700);
  } catch (err) {
    setGenBusy(false);
    setStatus('生成失败：' + err.message, 'err');
  }
}

async function pollProgress() {
  pollTimer = 0;
  if (!promptId) return;
  try {
    const p = (await api(API.progress(promptId))) || {};
    const state = String(p.state || '');
    setProgress(num(p.progress));

    if (state === 'done') {
      setGenBusy(false);
      setProgress(1);
      const images = Array.isArray(p.images) ? p.images : [];
      images.forEach((img) => addTile(normalizeTile(img, lastSeed), true));
      updateGalleryCount();
      setStatus('完成 · ' + images.length + ' 张' + (lastSeed != null ? ' · seed ' + lastSeed : ''), 'ok');
      return;
    }
    if (state === 'error') {
      setGenBusy(false);
      setStatus('生成失败：' + (p.error || 'ComfyUI 返回未知错误'), 'err');
      return;
    }
    if (state === 'queued') {
      const pos = p.queue_pos;
      setStatus('排队中' + (pos != null ? ' · 前面 ' + pos + ' 个任务' : '') + '…');
    } else {
      const pct = Math.round(num(p.progress) * 100);
      setStatus('生成中 ' + pct + '%' + (p.node ? ' · ' + p.node : '') + (p.elapsed != null ? ' · ' + Math.round(num(p.elapsed)) + 's' : ''));
    }
    pollTimer = setTimeout(pollProgress, 700);
  } catch (err) {
    setGenBusy(false);
    setStatus('进度查询失败：' + err.message, 'err');
  }
}

// ─────────────────────────── 画廊 ───────────────────────────

function normalizeTile(item, seed) {
  if (typeof item === 'string') return { url: item, seed: seed };
  if (!item) return { url: '', seed: seed };
  let url = item.url || '';
  if (!url && item.filename) {
    url = '/api/view?filename=' + encodeURIComponent(item.filename)
      + '&subfolder=' + encodeURIComponent(item.subfolder || '')
      + '&type=' + encodeURIComponent(item.type || 'output');
  }
  const s = item.seed != null ? item.seed : seed;
  return {
    url: url,
    seed: s,
    filename: item.filename || '',
    mtime: item.mtime != null ? item.mtime : null,
  };
}

function addTile(tile, prepend) {
  if (!tile || !tile.url) return null;
  const gallery = $('gallery');
  if (!gallery) return null;

  const fig = document.createElement('figure');
  fig.className = 'tile';
  fig.dataset.url = tile.url;
  fig.dataset.seed = tile.seed != null ? String(tile.seed) : '';
  fig.title = tile.filename || '点击放大';

  const img = document.createElement('img');
  img.loading = 'lazy';
  img.alt = tile.filename || '生成图';
  img.src = tile.url;
  fig.appendChild(img);

  if (tile.seed != null && tile.seed !== '') {
    const tag = document.createElement('span');
    tag.className = 'tile-seed';
    tag.textContent = 'seed ' + tile.seed;
    fig.appendChild(tag);
  }

  if (prepend) gallery.insertBefore(fig, gallery.firstChild);
  else gallery.appendChild(fig);
  return fig;
}

function updateGalleryCount() {
  const gallery = $('gallery');
  const n = gallery ? gallery.querySelectorAll('.tile').length : 0;
  setBadge('gallery-count', n + ' 张');
  return n;
}

function tileList() {
  const gallery = $('gallery');
  if (!gallery) return [];
  return Array.prototype.slice.call(gallery.querySelectorAll('.tile')).map((t) => ({
    url: t.dataset.url,
    seed: t.dataset.seed,
    cap: t.title,
  }));
}

function loadGallery(raw) {
  const list = Array.isArray(raw) ? raw : (raw && (raw.items || raw.gallery || raw.images)) || [];
  const gallery = $('gallery');
  if (gallery) gallery.textContent = '';
  list.forEach((item) => addTile(normalizeTile(item), false));
  updateGalleryCount();
}

// ─────────────────────────── lightbox ───────────────────────────

function openLightbox(index, list) {
  lbList = Array.isArray(list) && list.length ? list : tileList();
  if (!lbList.length) return;
  lbIndex = clamp(index, 0, lbList.length - 1);
  lbZoom = 1;
  const box = $('lightbox');
  box.hidden = false;
  renderLightbox();
}

function renderLightbox() {
  const item = lbList[lbIndex];
  if (!item) return;
  const img = document.querySelector('.lb-img');
  const cap = document.querySelector('.lb-cap');
  if (img) {
    img.src = item.url;
    img.style.transform = 'scale(' + lbZoom + ')';
  }
  if (cap) {
    const seedTxt = item.seed ? ' · seed ' + item.seed : '';
    cap.textContent = (lbIndex + 1) + ' / ' + lbList.length + seedTxt;
  }
}

function closeLightbox() {
  const box = $('lightbox');
  if (!box) return;
  box.hidden = true;
  const img = document.querySelector('.lb-img');
  if (img) img.removeAttribute('src');
  lbZoom = 1;
}

function stepLightbox(delta) {
  if (!lbList.length) return;
  lbIndex = (lbIndex + delta + lbList.length) % lbList.length;
  lbZoom = 1;
  renderLightbox();
}

function bindLightbox() {
  const box = $('lightbox');
  if (!box) return;

  box.addEventListener('click', (e) => {
    const t = e.target;
    if (t === box || (t.classList && t.classList.contains('lb-stage'))) closeLightbox();
  });

  const closeBtn = document.querySelector('.lb-close');
  if (closeBtn) closeBtn.addEventListener('click', closeLightbox);

  const prev = document.querySelector('.lb-prev');
  if (prev) prev.addEventListener('click', (e) => { e.stopPropagation(); stepLightbox(-1); });

  const next = document.querySelector('.lb-next');
  if (next) next.addEventListener('click', (e) => { e.stopPropagation(); stepLightbox(1); });

  box.addEventListener('wheel', (e) => {
    e.preventDefault();
    lbZoom = clamp(lbZoom * (e.deltaY < 0 ? 1.12 : 0.89), 1, 6);
    const img = document.querySelector('.lb-img');
    if (img) img.style.transform = 'scale(' + lbZoom + ')';
  }, { passive: false });

  document.addEventListener('keydown', (e) => {
    if (box.hidden) return;
    if (e.key === 'Escape') { closeLightbox(); e.preventDefault(); return; }
    if (e.key === 'ArrowLeft') { stepLightbox(-1); e.preventDefault(); return; }
    if (e.key === 'ArrowRight') { stepLightbox(1); e.preventDefault(); }
  });
}

// ─────────────────────────── 复制 ───────────────────────────

async function copyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    /* 落到 execCommand 兜底 */
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', 'readonly');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (_) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

function flashCopy(btn, ok) {
  if (!btn) return;
  const old = btn.dataset.label || btn.textContent;
  btn.dataset.label = old;
  btn.textContent = ok ? '已复制' : '失败';
  setTimeout(() => { btn.textContent = old; }, 1100);
}

// ─────────────────────────── 预设 / 启动数据 ───────────────────────────

function presetDict(raw) {
  if (Array.isArray(raw)) {
    if (raw.length < 4) return null;
    return { pos_x: num(raw[0]), pos_y: num(raw[1]), pos_z: num(raw[2]), roll: num(raw[3]) };
  }
  if (raw && typeof raw === 'object') return raw;
  return null;
}

function fillPresetSelect(names) {
  const sel = $('preset');
  if (!sel) return;

  const builtin = [];
  const seen = {};
  (names && names.length ? names : Object.keys(metaPresets)).forEach((n) => {
    if (!n || n === '自定义' || seen[n]) return;
    seen[n] = 1;
    builtin.push(n);
  });

  sel.textContent = '';
  const custom = document.createElement('option');
  custom.value = '自定义';
  custom.textContent = '自定义';
  sel.appendChild(custom);

  if (builtin.length) {
    const g1 = document.createElement('optgroup');
    g1.label = '内置预设';
    builtin.forEach((n) => {
      const o = document.createElement('option');
      o.value = n;
      o.textContent = n;
      g1.appendChild(o);
    });
    sel.appendChild(g1);
  }

  const mine = Object.keys(uiPresets).filter((n) => !seen[n] && n !== '自定义');
  if (mine.length) {
    const g2 = document.createElement('optgroup');
    g2.label = '我的预设';
    mine.forEach((n) => {
      const o = document.createElement('option');
      o.value = n;
      o.textContent = '★ ' + n;
      g2.appendChild(o);
    });
    sel.appendChild(g2);
  }

  sel.value = S.preset || '自定义';
}

function fillEnums(enums) {
  Object.keys(ENUM_FALLBACK).forEach((key) => {
    const el = $(key);
    if (!el) return;
    let values = ENUM_FALLBACK[key];
    if (enums && Array.isArray(enums[key]) && enums[key].length) values = enums[key];
    else if (key === 'gaze_mode' && meta && Array.isArray(meta.gaze_modes) && meta.gaze_modes.length) values = meta.gaze_modes;
    el.textContent = '';
    values.forEach((v) => {
      const o = document.createElement('option');
      o.value = String(v);
      o.textContent = String(v);
      el.appendChild(o);
    });
    el.value = String(S[key] != null ? S[key] : values[0]);
  });
}

function applyMeta(data) {
  meta = data || {};
  metaPresets = (meta.presets && typeof meta.presets === 'object') ? meta.presets : {};
  uiPresets = (meta.ui_presets && typeof meta.ui_presets === 'object') ? meta.ui_presets : {};
  presetModes = (meta.preset_modes && typeof meta.preset_modes === 'object') ? meta.preset_modes : {};

  const defs = (meta.defaults && typeof meta.defaults === 'object') ? meta.defaults : {};
  Object.keys(DEFAULTS).forEach((k) => { S[k] = DEFAULTS[k]; });
  Object.keys(defs).forEach((k) => {
    const key = k === 'lora_name' ? 'lora' : k;
    if (!(key in DEFAULTS)) return;
    if (key === 'randomize') S[key] = !!defs[k];
    else if (typeof DEFAULTS[key] === 'number') S[key] = num(defs[k], DEFAULTS[key]);
    else S[key] = defs[k] == null ? DEFAULTS[key] : String(defs[k]);
  });

  fillPresetSelect(meta.preset_names);
  fillEnums(meta.enums);
  fillTemplateSelect(meta.templates);
}

function fillTemplateSelect(items) {
  const sel = $('wf_template');
  if (!sel) return;
  const list = Array.isArray(items) ? items : [];
  sel.textContent = '';
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '（内置最小图：checkpoint + LoRA 直出）';
  sel.appendChild(none);
  list.forEach((t) => {
    if (!t || t.valid === false) return;
    const id = String(t.id || t.name || '');
    if (!id) return;
    const o = document.createElement('option');
    o.value = id;
    o.textContent = (t.title ? t.title + ' · ' : '') + id;
    if (t.description) o.title = String(t.description);
    sel.appendChild(o);
  });
  sel.value = (S.workflow_template && list.some((t) => String(t.id) === S.workflow_template)) ? S.workflow_template : '';
  S.workflow_template = sel.value;
}

function applyModels(data) {
  const d = data || {};
  const cps = Array.isArray(d.checkpoints) ? d.checkpoints : [];
  const loras = Array.isArray(d.loras) ? d.loras : [];
  const reachable = d.comfy_reachable !== false && cps.length > 0;

  const cpSel = $('checkpoint');
  if (cpSel) {
    cpSel.textContent = '';
    if (cps.length) {
      cps.forEach((name) => {
        const o = document.createElement('option');
        o.value = name;
        o.textContent = name;
        cpSel.appendChild(o);
      });
      setBadge('models', 'ComfyUI 在线');
    } else {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = '未连接 ComfyUI';
      cpSel.appendChild(o);
      setBadge('models', '未连接 ComfyUI');
    }
    if (S.checkpoint && cps.indexOf(S.checkpoint) >= 0) cpSel.value = S.checkpoint;
    else S.checkpoint = cpSel.value || '';
  }

  const loraSel = $('lora');
  if (loraSel) {
    loraSel.textContent = '';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = '（不使用 LoRA）';
    loraSel.appendChild(none);
    loras.forEach((name) => {
      const o = document.createElement('option');
      o.value = name;
      o.textContent = name;
      loraSel.appendChild(o);
    });
    if (S.lora && loras.indexOf(S.lora) >= 0) loraSel.value = S.lora;
    else S.lora = '';
  }

  if (reachable) setStatus('就绪 · ComfyUI 已连接', 'ok');
}

function applyHealth(data) {
  const conn = document.querySelector('.conn');
  const text = document.querySelector('.conn-text');
  if (!conn) return;
  const ok = !!(data && data.comfy_reachable);
  conn.classList.toggle('ok', ok);
  conn.classList.toggle('bad', !ok);
  if (text) text.textContent = ok ? 'ComfyUI 已连接' : '未连接 ComfyUI';
  const tip = data && data.comfy_url ? 'ComfyUI: ' + data.comfy_url : 'ComfyUI 未连接';
  conn.title = tip;
}

// ─────────────────────────── 用户操作 ───────────────────────────

/** 选中预设：dict 里所有键都写回 UI（旧式 4 元组按 pos/roll 处理） */
function applyPreset(name) {
  S.preset = name === undefined ? '自定义' : name;
  const dict = presetDict(metaPresets[S.preset] || uiPresets[S.preset]);
  if (S.preset !== '自定义' && dict) {
    Object.keys(dict).forEach((k) => {
      if (k === 'preset' || k === 'lora_name') return;
      if (!(k in DEFAULTS)) return;
      const v = dict[k];
      if (k === 'randomize') { S[k] = !!v; return; }
      if (typeof DEFAULTS[k] === 'number') S[k] = num(v, DEFAULTS[k]);
      else S[k] = v == null ? DEFAULTS[k] : String(v);
    });
    const mode = presetModes[S.preset];
    if (mode && typeof mode === 'object') {
      if (mode.elevation_mode != null) S.elevation_mode = String(mode.elevation_mode);
      if (mode.strength != null) S.strength = num(mode.strength, S.strength);
    }
  }
  writeCameraUI();
  pushRig();
  syncGenUI();
  schedulePreview();
  scheduleConfigSave();
}

function resetAll() {
  Object.keys(DEFAULTS).forEach((k) => { S[k] = DEFAULTS[k]; });
  S.preset = '自定义';
  writeCameraUI();
  pushRig();
  syncGenUI();
  schedulePreview();
  scheduleConfigSave();
  setProgress(0);
  setStatus('已复位');
}

function syncGenUI() {
  const map = { seed: 'seed', steps: 'steps', cfg: 'cfg', width: 'width', height: 'height', lora_strength: 'lora_strength' };
  Object.keys(map).forEach((k) => {
    const el = $(map[k]);
    if (el) el.value = String(num(S[k], DEFAULTS[k]));
  });
  if ($('randomize')) $('randomize').checked = !!S.randomize;
  if ($('wf_template')) $('wf_template').value = S.workflow_template || '';
  if ($('pose_id')) $('pose_id').value = S.pose_id || '';
  refreshReadouts();
}

async function onSavePreset() {
  const name = window.prompt('预设名称', '我的机位');
  if (!name) return;
  try {
    const res = await postJSON(API.uiPresets, { name: name, vals: cameraPayload() });
    if (res && res.ui_presets && typeof res.ui_presets === 'object') uiPresets = res.ui_presets;
    S.preset = name;
    const names = (meta && Array.isArray(meta.preset_names)) ? meta.preset_names : Object.keys(metaPresets);
    fillPresetSelect(names);
    const sel = $('preset');
    if (sel) sel.value = name;
    setStatus('已保存预设「' + name + '」（含 focal/pan/subject_yaw/gaze_mode）', 'ok');
  } catch (err) {
    setStatus('保存预设失败：' + err.message, 'err');
  }
}

function exportPresets() {
  const a = document.createElement('a');
  a.href = API.presetsExport;
  a.rel = 'noopener';
  a.download = 'ui_presets.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setStatus('已请求导出预设（若无下载，确认后端 /api/ui_presets/export）', 'ok');
}

async function importPresets(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const parsed = JSON.parse(text);
    const presets = (parsed && parsed.presets && typeof parsed.presets === 'object') ? parsed.presets : parsed;
    const res = await postJSON(API.presetsImport, { presets: presets });
    if (res && res.ui_presets && typeof res.ui_presets === 'object') uiPresets = res.ui_presets;
    const names = (meta && Array.isArray(meta.preset_names)) ? meta.preset_names : Object.keys(metaPresets);
    fillPresetSelect(names);
    setStatus('已导入预设 · 新增 ' + Math.round(num(res && res.added, 0)) + ' 个', 'ok');
  } catch (err) {
    setStatus('导入预设失败：' + err.message, 'err');
  }
}

// ─────────────────────────── 序列 ───────────────────────────

function clearSeqTimers() {
  seqTimers.forEach((t) => clearTimeout(t));
  seqTimers = [];
}

function fillSeqSelect(data) {
  const sel = $('seq_select');
  if (!sel) return;
  const seqs = (data && data.sequences && typeof data.sequences === 'object') ? data.sequences : {};
  seqDefs = seqs;
  const names = (data && Array.isArray(data.names) && data.names.length) ? data.names : Object.keys(seqs);
  sel.textContent = '';
  if (!names.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '（后端未提供序列）';
    sel.appendChild(o);
    return;
  }
  names.forEach((n) => {
    const o = document.createElement('option');
    o.value = n;
    o.textContent = n;
    sel.appendChild(o);
  });
  updateSeqDesc();
}

function updateSeqDesc() {
  const sel = $('seq_select');
  const el = $('seq_desc');
  if (!el) return;
  const name = sel ? sel.value : '';
  const def = seqDefs[name];
  if (!def) {
    el.textContent = '';
    return;
  }
  const shots = Array.isArray(def.shots) ? def.shots : [];
  const labels = shots.map((s) => String((s && s.label) || '机位')).join(' · ');
  el.textContent = (def.desc ? def.desc + ' · ' : '') + shots.length + ' 张：' + labels;
}

function updateSeqCount() {
  const strip = $('seq_strip');
  const n = strip ? strip.querySelectorAll('.seq-card img').length : 0;
  setBadge('seq-count', n + ' 张');
}

function addSeqCard(shot) {
  const strip = $('seq_strip');
  if (!strip) return null;

  const card = document.createElement('figure');
  card.className = 'seq-card';
  card.dataset.url = '';

  const cap = document.createElement('figcaption');
  cap.className = 'seq-cap';
  cap.textContent = String((shot && shot.label) || '机位');

  const body = document.createElement('div');
  body.className = 'seq-body';
  const ph = document.createElement('div');
  ph.className = 'seq-ph';
  ph.textContent = '排队…';
  body.appendChild(ph);

  const meta = document.createElement('div');
  meta.className = 'seq-meta';
  meta.textContent = 'seed ' + (shot && shot.seed != null ? shot.seed : '—');

  card.appendChild(cap);
  card.appendChild(body);
  card.appendChild(meta);
  strip.appendChild(card);

  return { card: card, ph: ph, meta: meta };
}

function seqUrls() {
  const strip = $('seq_strip');
  if (!strip) return [];
  return Array.prototype.slice.call(strip.querySelectorAll('.seq-card')).map((c) => ({
    url: c.dataset.url,
    seed: '',
    cap: (c.querySelector('.seq-cap') || {}).textContent || '',
  })).filter((x) => !!x.url);
}

function pollSeqShot(shot, ui) {
  if (!shot || !shot.prompt_id || !ui) return;
  const tick = async () => {
    try {
      const p = (await api(API.progress(shot.prompt_id))) || {};
      const state = String(p.state || '');
      const pct = Math.round(num(p.progress) * 100);
      if (state === 'done') {
        const images = Array.isArray(p.images) ? p.images : [];
        const tile = images.length ? normalizeTile(images[0], shot.seed) : null;
        if (tile && tile.url) {
          const img = document.createElement('img');
          img.loading = 'lazy';
          img.alt = String(shot.label || '序列出图');
          img.src = tile.url;
          if (ui.ph.parentNode) ui.ph.parentNode.replaceChild(img, ui.ph);
          ui.card.dataset.url = tile.url;
          if (!ui.card.dataset.seed) ui.card.dataset.seed = String(shot.seed != null ? shot.seed : '');
          updateSeqCount();
          ui.meta.textContent = 'seed ' + (shot.seed != null ? shot.seed : '—') + ' · 完成';
        } else {
          ui.ph.textContent = '完成但无图';
        }
        return;
      }
      if (state === 'error') {
        ui.ph.textContent = '失败：' + (p.error || '未知错误');
        return;
      }
      ui.ph.textContent = state === 'queued' ? '排队中…' : '生成中 ' + pct + '%';
      ui.meta.textContent = 'seed ' + (shot.seed != null ? shot.seed : '—') + ' · ' + pct + '%';
      seqTimers.push(setTimeout(tick, 700));
    } catch (err) {
      ui.meta.textContent = '轮询失败：' + err.message;
      seqTimers.push(setTimeout(tick, 1600));
    }
  };
  seqTimers.push(setTimeout(tick, 700));
}

async function runSequence() {
  if (seqRunning) return;
  const sel = $('seq_select');
  const name = sel ? sel.value : '';
  if (!name) {
    setStatus('还没有可选序列（GET /api/sequences）', 'err');
    return;
  }
  seqRunning = true;
  const btn = $('seq_run');
  if (btn) { btn.disabled = true; btn.textContent = '提交中…'; }
  clearSeqTimers();
  const strip = $('seq_strip');
  if (strip) strip.textContent = '';
  updateSeqCount();

  try {
    const sameSeedEl = $('seq_same_seed');
    const body = {
      name: name,
      base: Object.assign({}, cameraPayload(), genPayload()),
      same_seed: sameSeedEl ? !!sameSeedEl.checked : true,
      seed: Math.trunc(num(S.seed, -1)),
      randomize: !!S.randomize,
    };
    const data = await postJSON(API.sequence, body);
    const shots = (data && Array.isArray(data.shots)) ? data.shots : [];
    shots.forEach((shot) => {
      const ui = addSeqCard(shot);
      pollSeqShot(shot, ui);
    });
    const failed = data && data.failed_label ? ' · 中断于 ' + data.failed_label : '';
    setStatus('序列「' + name + '」已提交 ' + shots.length + ' 张' + failed, shots.length ? 'ok' : 'err');
  } catch (err) {
    setStatus('序列提交失败：' + err.message, 'err');
  } finally {
    seqRunning = false;
    if (btn) { btn.disabled = false; btn.textContent = '运行序列'; }
  }
}

// ─────────────── v2.3：ControlNet 导出 / 模板嗅探 / 批量队列 ───────────────
//
// 三条线共用一套原则：控件永远可用，后端离线只在对应面板里说中文错误，不打断页面。
//   · ControlNet：3D 场景离屏导出 depth / pose → POST /api/controlnet/upload → filename 进 template_vars
//   · 模板嗅探：GET /api/templates 填下拉 → POST /api/templates/inspect → 结构化报告
//   · 批量队列：POST /api/batch → 900ms GET /api/batch → batch_grid 并排卡片

const CN_W = 512;
const CN_H = 768;

let cnFiles = { depth: '', pose: '' };
let tplItems = [];
let batchTimer = 0;
let batchId = '';
let batchLabel = '';
let batchRunning = false;

/** 取 3D 核心实例：camera3d.js 可能挂 window.__rig，本文件也把同一实例挂上去 */
function currentRig() {
  if (window.__rig && typeof window.__rig === 'object') return window.__rig;
  return rig || null;
}

function setCnStatus(text, kind) {
  const el = $('cn_status');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('err', kind === 'err');
  el.classList.toggle('ok', kind === 'ok');
}

function setBatchStatus(text, kind) {
  const el = $('batch_status');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('err', kind === 'err');
  el.classList.toggle('ok', kind === 'ok');
}

/** 导出 + 上传；只走共享 3D 核心的离屏导出，前端不再自建渲染路径（两条口径会不一致） */
async function exportControlNet(kind) {
  const btn = $(kind === 'depth' ? 'btn_depth_export' : 'btn_pose_export');
  const img = $(kind === 'depth' ? 'depth_preview' : 'pose_preview');
  const r = currentRig();
  if (!r) {
    setCnStatus('3D 未就绪：左侧视口没起来（刷新页面重试）', 'err');
    return;
  }
  const native = kind === 'depth' ? r.renderDepthDataURL : r.renderPoseDataURL;
  if (typeof native !== 'function') {
    setCnStatus('3D 未就绪：当前 CameraRig 没有 ' + (kind === 'depth' ? 'renderDepthDataURL' : 'renderPoseDataURL') + '，换带离屏导出的版本', 'err');
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = '导出中…';
  }
  try {
    const dataUrl = kind === 'depth'
      ? native.call(r, CN_W, CN_H, { invert: true })
      : native.call(r, CN_W, CN_H);
    if (!dataUrl || String(dataUrl).indexOf('data:image') !== 0) {
      throw new Error('导出结果不是 data:image（3D 核心返回 ' + (dataUrl ? typeof dataUrl : '空') + '）');
    }
    if (img) {
      img.src = dataUrl;
      img.hidden = false;
    }
    setCnStatus('已生成 ' + CN_W + '×' + CN_H + ' ' + (kind === 'depth' ? '深度图' : '骨架图') + '，上传中…');

    const res = await postJSON(API.controlnetUpload, { kind: kind, data_url: dataUrl });
    const name = res && res.filename ? String(res.filename) : '';
    if (!name) throw new Error('后端未返回 filename');
    cnFiles[kind] = name;
    if (kind === 'depth') S.cn_depth_file = name;
    else S.cn_pose_file = name;

    const bytes = res && res.bytes != null ? num(res.bytes, 0) : 0;
    const sizeText = bytes >= 1024 ? (bytes / 1024).toFixed(1) + ' KB' : bytes + ' B';
    setCnStatus('已上传 ' + name + ' · ' + sizeText + (S.cn_auto_inject ? ' · 下次生成自动注入' : ' · 自动注入已关闭'), 'ok');
    setBadge('cn', kind === 'depth' ? '深度就绪' : '骨架就绪');
    scheduleConfigSave();
  } catch (err) {
    setCnStatus('导出失败：' + err.message, 'err');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = kind === 'depth' ? '导出深度' : '导出骨架';
    }
  }
}

/** 生成时要带的 ControlNet 变量（自动注入关闭时是空对象） */
function controlnetVars() {
  const out = {};
  if (!S.cn_auto_inject) return out;
  const kind = S.cn_kind || 'depth';
  const wantDepth = kind === 'depth' || kind === 'both';
  const wantPose = kind === 'pose' || kind === 'both';
  if (wantDepth && cnFiles.depth) out.controlnet_depth = cnFiles.depth;
  if (wantPose && cnFiles.pose) out.controlnet_pose = cnFiles.pose;
  if (out.controlnet_depth || out.controlnet_pose) {
    out.controlnet_strength = clamp(num(S.cn_strength, 1), 0, 2);
  }
  return out;
}

// ── 模板嗅探 ──

function nodeBrief(v) {
  if (v == null || v === '') return '—';
  if (Array.isArray(v)) {
    if (!v.length) return '—';
    return v.map(nodeBrief).join(' / ');
  }
  if (typeof v === 'string' || typeof v === 'number') return String(v);
  if (typeof v === 'object') {
    const id = v.node != null ? v.node : (v.id != null ? v.id : '');
    const field = v.field != null ? v.field : (v.input != null ? v.input : '');
    return (id === '' ? '—' : '#' + id) + (field ? ' · ' + field : '');
  }
  return String(v);
}

function tplListFrom(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.templates)) return data.templates;
  if (data && data.templates && typeof data.templates === 'object') {
    return Object.keys(data.templates).map((k) => {
      const t = data.templates[k];
      return Object.assign({ id: k }, (t && typeof t === 'object') ? t : {});
    });
  }
  return [];
}

function tplIdOf(t) {
  if (!t) return '';
  return String(t.id || t.name || t.path || '');
}

function fillTemplatePicker(items) {
  const sel = $('template_select');
  if (!sel) return;
  const list = Array.isArray(items) ? items : [];
  sel.textContent = '';
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '（不了模板：走内置最小图）';
  sel.appendChild(none);
  list.forEach((t) => {
    if (!t) return;
    const id = tplIdOf(t);
    if (!id) return;
    // 后端 list_templates 的 nodes 是数字；也有 node_list 清单的实现，两种都认
    const nodes = Array.isArray(t.node_list) ? t.node_list.length : (Math.trunc(num(t.nodes, 0)) || Math.trunc(num(t.node_count, 0)));
    const o = document.createElement('option');
    o.value = id;
    o.textContent = (t.title ? String(t.title) : id) + (nodes ? ' · ' + nodes + ' 节点' : '') + (t.valid === false ? ' · 不可用' : '');
    if (t.path) o.title = String(t.path);
    sel.appendChild(o);
  });
  const want = String(S.template || '');
  const hasWant = list.some((t) => tplIdOf(t) === want);
  sel.value = hasWant ? want : '';
  S.template = sel.value;
  setBadge('tpl', sel.value ? '已选模板' : '未选模板');
}

async function loadTemplates() {
  let items = [];
  try {
    const data = await api(API.templates);
    items = tplListFrom(data);
  } catch (_) {
    items = tplListFrom(meta && meta.templates);
  }
  tplItems = items;
  fillTemplatePicker(items);
  return items;
}

function appendRepLine(box, text, kind) {
  const p = document.createElement('div');
  p.className = 'rep-warn' + (kind === 'err' ? ' err' : '');
  p.textContent = text;
  box.appendChild(p);
}

function renderTemplateReport(data, ref) {
  const box = $('template_report');
  if (!box) return;
  const d = data || {};
  const sniff = (d.sniff && typeof d.sniff === 'object') ? d.sniff : {};
  // 后端 nodes 是数字（节点总数），node_list 才是清单；两种都兜住
  const nodeCount = Math.trunc(num(d.nodes, 0)) ||
    (Array.isArray(d.node_list) ? d.node_list.length : 0) ||
    (Array.isArray(d.nodes) ? d.nodes.length : 0);
  const cn = (sniff.controlnet && typeof sniff.controlnet === 'object') ? sniff.controlnet : {};
  const depth = Array.isArray(cn.depth) ? cn.depth : [];
  const pose = Array.isArray(cn.pose) ? cn.pose : [];
  const warn = Array.isArray(d.warn) ? d.warn : [];

  box.textContent = '';
  // ok:false 是 200 里的软失败（模板读不了 / 解析炸了），直接把后端原话放出来
  if (d.ok === false) {
    appendRepLine(box, '嗅探失败：' + String(d.error || '后端没说原因'), 'err');
    warn.forEach((w) => appendRepLine(box, String(w), 'err'));
    setBadge('tpl', '嗅探失败');
    return;
  }

  const camList = Array.isArray(sniff.camera) ? sniff.camera : (sniff.camera ? [sniff.camera] : []);
  const rows = [
    ['模板', String(d.template || ref || '—') + (nodeCount ? ' · ' + nodeCount + ' 个节点' : ' · 节点数未知')],
    ['CameraAngleSDXL', camList.length ? '有 · ' + nodeBrief(camList) : '无（用 KSampler 的 CLIPTextEncode）'],
    ['正向提示词节点', nodeBrief(sniff.positive)],
    ['负向提示词节点', nodeBrief(sniff.negative)],
    ['采样器', nodeBrief(sniff.sampler)],
    ['Latent', nodeBrief(sniff.latent)],
    ['保存节点', nodeBrief(sniff.save)],
    ['ControlNet 深度槽', depth.length ? depth.map(nodeBrief).join(' / ') : '无'],
    ['ControlNet 骨架槽', pose.length ? pose.map(nodeBrief).join(' / ') : '无'],
  ];

  box.textContent = '';
  const dl = document.createElement('dl');
  dl.className = 'rep-list';
  rows.forEach((pair) => {
    const dt = document.createElement('dt');
    dt.textContent = pair[0];
    const dd = document.createElement('dd');
    dd.textContent = pair[1];
    dl.appendChild(dt);
    dl.appendChild(dd);
  });
  box.appendChild(dl);

  if (warn.length) {
    const ul = document.createElement('ul');
    ul.className = 'rep-warn';
    warn.forEach((w) => {
      const li = document.createElement('li');
      li.textContent = String(w);
      ul.appendChild(li);
    });
    box.appendChild(ul);
  } else {
    const p = document.createElement('div');
    p.className = 'rep-ok';
    p.textContent = '没有警告：注入点齐全。';
    box.appendChild(p);
  }
  setBadge('tpl', '已嗅探');
}

async function inspectTemplate() {
  const sel = $('template_select');
  const ref = sel ? sel.value : '';
  const box = $('template_report');
  if (!ref) {
    if (box) box.textContent = '先在上面下拉里选一条工作流，再点「嗅探节点」。';
    return;
  }
  const btn = $('btn_template_inspect');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '嗅探中…';
  }
  if (box) box.textContent = '正在嗅探 ' + ref + ' …';
  try {
    const data = await postJSON(API.templatesInspect, { ref: ref });
    renderTemplateReport(data, ref);
  } catch (err) {
    if (box) {
      box.textContent = '';
      appendRepLine(box, '嗅探失败：' + err.message, 'err');
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '嗅探节点';
    }
  }
}

// ── 批量队列 ──

function clearBatchTimer() {
  if (batchTimer) clearTimeout(batchTimer);
  batchTimer = 0;
}

function batchUrls() {
  const grid = $('batch_grid');
  if (!grid) return [];
  return Array.prototype.slice.call(grid.querySelectorAll('.batch-card'))
    .map((c) => ({ url: c.dataset.url || '', seed: (c.querySelector('.batch-seed') || {}).textContent || '' }))
    .filter((x) => !!x.url);
}

function jobStateOf(job, data) {
  const s = String((job && job.state) || '');
  if (s) return s;
  return String((data && data.state) || 'queued');
}

function renderBatchGrid(data) {
  const grid = $('batch_grid');
  if (!grid) return;
  const jobs = Array.isArray(data && data.jobs) ? data.jobs : [];
  grid.textContent = '';
  jobs.forEach((job, i) => {
    const state = jobStateOf(job, data);
    const pct = Math.round(clamp(num(job && job.progress, 0), 0, 1) * 100);

    const card = document.createElement('figure');
    card.className = 'batch-card ' + (state === 'done' ? 'is-done' : state === 'error' ? 'is-error' : 'is-run');

    const cap = document.createElement('figcaption');
    cap.className = 'batch-cap';
    const label = document.createElement('span');
    label.className = 'batch-label';
    label.textContent = String((job && job.label) || '机位 ' + (i + 1));
    const seedEl = document.createElement('span');
    seedEl.className = 'batch-seed';
    seedEl.textContent = 'seed ' + ((job && job.seed != null) ? job.seed : '—');
    cap.appendChild(label);
    cap.appendChild(seedEl);

    const body = document.createElement('div');
    body.className = 'batch-body';
    const imgs = Array.isArray(job && job.images) ? job.images : [];
    const tile = imgs.length ? normalizeTile(imgs[0], (job && job.seed)) : null;
    if (tile && tile.url) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = label.textContent;
      img.src = tile.url;
      body.appendChild(img);
      card.dataset.url = tile.url;
    } else {
      const ph = document.createElement('div');
      ph.className = 'batch-ph';
      ph.textContent = state === 'error' ? '失败' : state === 'done' ? '完成但无图' : state === 'queued' ? '排队…' : pct + '%';
      body.appendChild(ph);
      if (state !== 'error') {
        const bar = document.createElement('div');
        bar.className = 'batch-bar';
        const fill = document.createElement('span');
        fill.style.width = pct + '%';
        bar.appendChild(fill);
        body.appendChild(bar);
      }
    }

    const meta = document.createElement('div');
    meta.className = 'batch-meta';
    if (state === 'error') {
      meta.textContent = '失败：' + String((job && job.error) || (data && data.error) || '未知错误');
    } else if (state === 'done') {
      meta.textContent = '完成';
    } else if (state === 'queued') {
      meta.textContent = '排队中';
    } else {
      meta.textContent = '生成中 ' + pct + '%';
    }

    card.appendChild(cap);
    card.appendChild(body);
    card.appendChild(meta);
    grid.appendChild(card);
  });

  if (!jobs.length) {
    const ph = document.createElement('div');
    ph.className = 'batch-ph empty';
    ph.textContent = '还没有批量任务：选一个序列后点「批量出图」。';
    grid.appendChild(ph);
  }
}

function setBatchBusy(busy) {
  batchRunning = busy;
  const btn = $('btn_batch_run');
  if (!btn) return;
  btn.disabled = busy;
  btn.textContent = busy ? '批量中…' : '批量出图';
}

async function refreshGallery() {
  try {
    loadGallery(await api(API.gallery));
  } catch (_) {
    /* 画廊刷新失败静默：批量成品已经在 batch_grid 里 */
  }
}

async function runBatch() {
  if (batchRunning) return;
  const sel = $('seq_select');
  const name = sel ? sel.value : '';
  if (!seqDefs[name]) await loadSequences();
  const def = seqDefs[name];
  const shots = def && Array.isArray(def.shots) ? def.shots : [];
  if (!shots.length) {
    setBatchStatus('没有可用序列：GET /api/sequences 没返回机位，先跑序列页', 'err');
    return;
  }

  const nameEl = $('batch_name');
  batchLabel = (nameEl && String(nameEl.value || '').trim()) || S.batch_name || '三视图';
  const seedEl = $('batch_seed');
  const seed = Math.trunc(num(seedEl ? seedEl.value : S.batch_seed, 12345));
  const sameEl = $('batch_same_seed');
  const sameSeed = sameEl ? !!sameEl.checked : S.batch_same_seed !== false;

  clearBatchTimer();
  setBatchBusy(true);
  const grid = $('batch_grid');
  if (grid) grid.textContent = '';
  setBadge('batch', '0 / ' + shots.length);
  setBatchStatus('正在提交「' + batchLabel + '」' + shots.length + ' 张…');

  try {
    const body = {
      name: batchLabel,
      base: Object.assign({}, cameraPayload(), genPayload()),
      shots: shots.map((s) => {
        const params = (s && s.params && typeof s.params === 'object') ? s.params : {};
        return { label: String((s && s.label) || '机位'), params: params };
      }),
      same_seed: sameSeed,
      seed: seed,
    };
    const data = await postJSON(API.batch, body);
    batchId = String((data && (data.batch_id || data.id)) || '');
    if (!batchId) throw new Error('后端未返回 batch_id');
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    renderBatchGrid({ state: 'queued', done: 0, total: jobs.length, jobs: jobs });
    setBadge('batch', '0 / ' + (jobs.length || shots.length));
    setBatchStatus('批次 ' + batchId + ' 已提交 · 排队中');
    batchTimer = setTimeout(pollBatch, 900);
  } catch (err) {
    setBatchBusy(false);
    setBatchStatus('批量提交失败：' + err.message, 'err');
  }
}

async function pollBatch() {
  batchTimer = 0;
  if (!batchId) return;
  try {
    const data = (await api(API.batchGet(batchId))) || {};
    renderBatchGrid(data);
    const state = String(data.state || '');
    const done = Math.trunc(num(data.done, 0));
    const total = Math.trunc(num(data.total, 0));
    setBadge('batch', done + ' / ' + total);
    if (state === 'done') {
      setBatchBusy(false);
      setBatchStatus('批次「' + (data.name || batchLabel) + '」完成 · ' + done + '/' + total, 'ok');
      refreshGallery();
      return;
    }
    if (state === 'error') {
      setBatchBusy(false);
      setBatchStatus('批次失败：' + (data.error || '后端没给原因'), 'err');
      return;
    }
    setBatchStatus('批次 ' + batchId + ' · ' + (state === 'queued' ? '排队中' : '生成中') + ' ' + done + '/' + total);
    batchTimer = setTimeout(pollBatch, 900);
  } catch (err) {
    setBatchBusy(false);
    setBatchStatus('批次轮询失败：' + err.message, 'err');
  }
}

// ── v2.3 绑定与回填 ──

function syncWfTemplate() {
  const wf = $('wf_template');
  if (!wf) return;
  const want = String(S.template || '');
  if (!want) return;
  const has = Array.prototype.some.call(wf.options, (o) => o.value === want);
  if (has) {
    wf.value = want;
    S.workflow_template = want;
  }
}

function applyV23UI() {
  const auto = $('cn_auto_inject');
  if (auto) auto.checked = !!S.cn_auto_inject;
  const kindSel = $('cn_kind_select');
  if (kindSel) kindSel.value = ['depth', 'pose', 'both'].indexOf(String(S.cn_kind)) >= 0 ? String(S.cn_kind) : 'depth';
  const cnStr = $('cn_strength');
  if (cnStr) cnStr.value = String(clamp(num(S.cn_strength, 1), 0, 2));
  const cnOut = document.querySelector('[data-val="cn_strength"]');
  if (cnOut) cnOut.textContent = clamp(num(S.cn_strength, 1), 0, 2).toFixed(2);

  const bName = $('batch_name');
  if (bName) bName.value = S.batch_name || '三视图';
  const bSeed = $('batch_seed');
  if (bSeed) bSeed.value = String(Math.trunc(num(S.batch_seed, 12345)));
  const bSame = $('batch_same_seed');
  if (bSame) bSame.checked = S.batch_same_seed !== false;

  cnFiles.depth = S.cn_depth_file || '';
  cnFiles.pose = S.cn_pose_file || '';
  if (cnFiles.depth || cnFiles.pose) {
    const parts = [];
    if (cnFiles.depth) parts.push('深度 ' + cnFiles.depth);
    if (cnFiles.pose) parts.push('骨架 ' + cnFiles.pose);
    setBadge('cn', '已就绪');
    setCnStatus('已记录的 ControlNet 文件：' + parts.join(' · ') + '（' + (S.cn_auto_inject ? '会自动注入' : '自动注入已关闭') + '）');
  }
  setBatchStatus('未开始 · 选序列后点「批量出图」');
  renderBatchGrid({ state: 'queued', jobs: [] });
}

function bindV23() {
  const depthBtn = $('btn_depth_export');
  if (depthBtn) depthBtn.addEventListener('click', () => exportControlNet('depth'));

  const poseBtn = $('btn_pose_export');
  if (poseBtn) poseBtn.addEventListener('click', () => exportControlNet('pose'));

  const auto = $('cn_auto_inject');
  if (auto) {
    auto.addEventListener('change', () => {
      S.cn_auto_inject = !!auto.checked;
      setCnStatus(auto.checked ? '已开启自动注入：导出的 filename 会进 template_vars。' : '已关闭自动注入：导出仍会上传，但生成不带 ControlNet。');
      scheduleConfigSave();
    });
  }

  const kindSel = $('cn_kind_select');
  if (kindSel) {
    kindSel.addEventListener('change', () => {
      S.cn_kind = kindSel.value;
      scheduleConfigSave();
    });
  }

  const cnStr = $('cn_strength');
  if (cnStr) {
    cnStr.addEventListener('input', () => {
      S.cn_strength = clamp(num(cnStr.value, 1), 0, 2);
      const out = document.querySelector('[data-val="cn_strength"]');
      if (out) out.textContent = S.cn_strength.toFixed(2);
      scheduleConfigSave();
    });
  }

  const tplSel = $('template_select');
  if (tplSel) {
    tplSel.addEventListener('change', () => {
      S.template = tplSel.value;
      syncWfTemplate();
      setBadge('tpl', tplSel.value ? '已选模板' : '未选模板');
      scheduleConfigSave();
    });
  }

  const tplBtn = $('btn_template_inspect');
  if (tplBtn) tplBtn.addEventListener('click', inspectTemplate);

  const bName = $('batch_name');
  if (bName) {
    bName.addEventListener('input', () => {
      S.batch_name = bName.value;
      scheduleConfigSave();
    });
  }
  const bSeed = $('batch_seed');
  if (bSeed) {
    bSeed.addEventListener('input', () => {
      S.batch_seed = Math.trunc(num(bSeed.value, 12345));
      scheduleConfigSave();
    });
  }
  const bSame = $('batch_same_seed');
  if (bSame) {
    bSame.addEventListener('change', () => {
      S.batch_same_seed = !!bSame.checked;
      scheduleConfigSave();
    });
  }

  const runBtn = $('btn_batch_run');
  if (runBtn) runBtn.addEventListener('click', runBatch);

  const grid = $('batch_grid');
  if (grid) {
    grid.addEventListener('click', (e) => {
      const card = e.target.closest ? e.target.closest('.batch-card') : null;
      if (!card || !card.dataset.url) return;
      const list = batchUrls();
      const idx = list.findIndex((x) => x.url === card.dataset.url);
      openLightbox(idx < 0 ? 0 : idx, list);
    });
  }
}

// ─────────────────────────── 绑定 ───────────────────────────

function bindCameraInputs() {
  CAM_KEYS.forEach((k) => {
    const el = $(k);
    if (!el) return;
    el.addEventListener('input', () => {
      if (syncing) return;
      S[k] = num(el.value);
      refreshReadouts();
      markCustom();
      pushRig();
      schedulePreview();
      scheduleConfigSave();
    });
  });

  const focalEl = $('focal');
  if (focalEl) {
    focalEl.addEventListener('input', () => {
      if (syncing) return;
      S.focal = clamp(num(focalEl.value, 50), 14, 200);
      refreshReadouts();
      markCustom();
      pushRig();
      schedulePreview();
      scheduleConfigSave();
    });
  }

  const chipRow = $('btn_focal_preset');
  if (chipRow) {
    chipRow.addEventListener('click', (e) => {
      const b = e.target.closest ? e.target.closest('.chip') : null;
      if (!b) return;
      const f = Math.round(num(b.dataset.focal, 50));
      if (FOCAL_STOPS.indexOf(f) < 0) return;
      S.focal = f;
      writeCameraUI();
      pushRig();
      markCustom();
      schedulePreview();
      scheduleConfigSave();
    });
  }

  ['strength', 'neg_strength'].forEach((k) => {
    const el = $(k);
    if (!el) return;
    el.addEventListener('input', () => {
      if (syncing) return;
      S[k] = num(el.value);
      refreshReadouts();
      markCustom();
      schedulePreview();
      scheduleConfigSave();
    });
  });

  ['weight_mode', 'azimuth_mode', 'elevation_mode', 'gaze_mode'].forEach((k) => {
    const el = $(k);
    if (!el) return;
    el.addEventListener('change', () => {
      if (syncing) return;
      S[k] = el.value;
      refreshReadouts();
      markCustom();
      pushRig();
      schedulePreview();
      scheduleConfigSave();
    });
  });

  const refFile = $('ref_file');
  if (refFile) {
    refFile.addEventListener('change', () => {
      const file = refFile.files && refFile.files[0];
      if (!file) {
        if (rig) rig.setRefImage('');
        return;
      }
      const url = URL.createObjectURL(file);
      if (rig) {
        rig.setRefImage(url);
        rig.setRefOpacity(S.ref_opacity);
      }
      setStatus('参考图叠层已载入：' + file.name + '（仅内存，不写 localStorage）', 'ok');
    });
  }

  const refOp = $('ref_opacity');
  if (refOp) {
    refOp.addEventListener('input', () => {
      S.ref_opacity = clamp(num(refOp.value, 0.35), 0, 1);
      refreshReadouts();
      if (rig) rig.setRefOpacity(S.ref_opacity);
    });
  }
}

function bindTextInputs() {
  CAM_TEXT_KEYS.forEach((k) => {
    const el = $(k);
    if (!el) return;
    el.addEventListener('input', () => {
      S[k] = el.value;
      schedulePreview();
      scheduleConfigSave();
    });
  });
}

function bindGenInputs() {
  const numberKeys = { seed: 'seed', steps: 'steps', cfg: 'cfg', width: 'width', height: 'height' };
  Object.keys(numberKeys).forEach((k) => {
    const el = $(k);
    if (!el) return;
    el.addEventListener('input', () => {
      S[k] = num(el.value, DEFAULTS[k]);
      scheduleConfigSave();
    });
  });

  const loraEl = $('lora_strength');
  if (loraEl) {
    loraEl.addEventListener('input', () => {
      S.lora_strength = num(loraEl.value, 1);
      refreshReadouts();
      scheduleConfigSave();
    });
  }

  const rnd = $('randomize');
  if (rnd) rnd.addEventListener('change', () => { S.randomize = rnd.checked; scheduleConfigSave(); });

  const cp = $('checkpoint');
  if (cp) cp.addEventListener('change', () => { S.checkpoint = cp.value; scheduleConfigSave(); });

  const lora = $('lora');
  if (lora) lora.addEventListener('change', () => { S.lora = lora.value; scheduleConfigSave(); });

  const tpl = $('wf_template');
  if (tpl) tpl.addEventListener('change', () => { S.workflow_template = tpl.value; scheduleConfigSave(); });

  const pose = $('pose_id');
  if (pose) pose.addEventListener('input', () => { S.pose_id = pose.value; scheduleConfigSave(); });
}

function bindButtons() {
  const presetSel = $('preset');
  if (presetSel) presetSel.addEventListener('change', () => applyPreset(presetSel.value));

  const save = $('btn_save_preset');
  if (save) save.addEventListener('click', onSavePreset);

  const exp = $('btn_export_presets');
  if (exp) exp.addEventListener('click', exportPresets);

  const imp = $('btn_import_presets');
  if (imp) {
    imp.addEventListener('click', () => {
      const file = $('import_file');
      if (file) file.click();
    });
  }

  const impFile = $('import_file');
  if (impFile) {
    impFile.addEventListener('change', () => {
      const file = impFile.files && impFile.files[0];
      importPresets(file);
      impFile.value = '';
    });
  }

  const reset = $('btn_reset');
  if (reset) reset.addEventListener('click', resetAll);

  const hud = $('btn_hud_toggle');
  if (hud) {
    hud.addEventListener('click', () => {
      const vp = $('viewport');
      if (!vp) return;
      const off = vp.classList.toggle('hud-off');
      hud.classList.toggle('on', !off);
      if (rig) rig.toggleGrid(!off);
    });
  }

  const seqBtn = $('btn_seq');
  if (seqBtn) {
    seqBtn.addEventListener('click', () => {
      const panel = $('seq_panel');
      if (!panel) return;
      const open = panel.classList.toggle('open');
      seqBtn.classList.toggle('on', open);
    });
  }

  const seqSel = $('seq_select');
  if (seqSel) seqSel.addEventListener('change', updateSeqDesc);

  const seqRun = $('seq_run');
  if (seqRun) seqRun.addEventListener('click', runSequence);

  const strip = $('seq_strip');
  if (strip) {
    strip.addEventListener('click', (e) => {
      const card = e.target.closest ? e.target.closest('.seq-card') : null;
      if (!card || !card.dataset.url) return;
      const list = seqUrls();
      const idx = list.findIndex((x) => x.url === card.dataset.url);
      openLightbox(idx < 0 ? 0 : idx, list);
    });
  }

  const folder = $('btn_open_folder');
  if (folder) {
    folder.addEventListener('click', async () => {
      try {
        await postJSON(API.openFolder, {});
        setStatus('已打开输出目录', 'ok');
      } catch (err) {
        setStatus('打开目录失败：' + err.message, 'err');
      }
    });
  }

  const gen = $('btn_generate');
  if (gen) gen.addEventListener('click', onGenerate);

  const intr = $('btn_interrupt');
  if (intr) {
    intr.addEventListener('click', async () => {
      try {
        await postJSON(API.interrupt, {});
        setStatus('已请求中断', 'ok');
      } catch (err) {
        setStatus('中断失败：' + err.message, 'err');
      }
    });
  }

  const cpPos = $('btn_copy_pos');
  if (cpPos) {
    cpPos.addEventListener('click', async () => {
      const txt = ($('positive_out') || {}).textContent || '';
      const ok = await copyText(txt);
      flashCopy(cpPos, ok);
    });
  }

  const cpNeg = $('btn_copy_neg');
  if (cpNeg) {
    cpNeg.addEventListener('click', async () => {
      const txt = ($('negative_out') || {}).textContent || '';
      const ok = await copyText(txt);
      flashCopy(cpNeg, ok);
    });
  }

  const clear = document.querySelector('.gallery-clear');
  if (clear) {
    clear.addEventListener('click', async () => {
      if (!window.confirm('清空画廊记录？（输出目录里的图片不会删除）')) return;
      try {
        await postJSON(API.galleryClear, {});
        loadGallery([]);
        setStatus('画廊已清空', 'ok');
      } catch (err) {
        setStatus('清空失败：' + err.message, 'err');
      }
    });
  }

  const gallery = $('gallery');
  if (gallery) {
    gallery.addEventListener('click', (e) => {
      const tile = e.target.closest ? e.target.closest('.tile') : null;
      if (!tile) return;
      const list = tileList();
      const idx = list.findIndex((t) => t.url === tile.dataset.url);
      openLightbox(idx < 0 ? 0 : idx);
    });
  }
}

/** R 键复位（1..6/G/F 在 camera3d.js 里） */
function bindShortcutReset() {
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    const tag = t && t.tagName ? String(t.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    if (e.key === 'r' || e.key === 'R') {
      resetAll();
      e.preventDefault();
    }
  });
}

// ─────────────────────────── 启动 ───────────────────────────

async function loadSequences() {
  try {
    const data = await api(API.sequences);
    fillSeqSelect(data);
  } catch (_) {
    fillSeqSelect(null);
  }
}

async function init() {
  const canvas = $('view3d');
  if (canvas) {
    try {
      rig = new CameraRig(canvas, {
        onChange: onRigChange,
        // 松手提交：立刻刷一次预览并写盘（拖动过程不会每个 pointermove 都 POST）
        onCommit: () => {
          refreshReadouts();
          schedulePreview();
          saveConfig();
        },
      });
    } catch (err) {
      rig = null;
      setStatus('3D 视图初始化失败：' + err.message, 'err');
    }
  }

  // v2.3：把实例挂到 window 上，ControlNet 导出与其它模块都从这里取
  window.__rig = rig || null;

  // index.html 的 #roll 还是 min=0（不在本任务文件范围）：放宽到 -1，
  // 否则拖外环拖出来的负向荷兰角在滑块上拨不回来
  const rollEl = $('roll');
  if (rollEl) rollEl.min = '-1';

  bindCameraInputs();
  bindTextInputs();
  bindGenInputs();
  bindButtons();
  bindLightbox();
  bindShortcutReset();
  bindV23();

  const results = await Promise.allSettled([
    api(API.meta),
    api(API.config),
    api(API.models),
    api(API.health),
    api(API.gallery),
  ]);

  const [metaRes, cfgRes, modelsRes, healthRes, galleryRes] = results;

  if (metaRes.status === 'fulfilled') {
    applyMeta(metaRes.value);
  } else {
    fillPresetSelect([]);
    fillEnums(null);
  }

  if (cfgRes.status === 'fulfilled') applyConfig(cfgRes.value);

  writeCameraUI();
  syncGenUI();
  applyV23UI();
  pushRig();
  if (rig) {
    rig.setRefOpacity(S.ref_opacity);
    rig.toggleGrid(true);
  }

  if (modelsRes.status === 'fulfilled') {
    applyModels(modelsRes.value);
  } else {
    setBadge('models', '未连接 ComfyUI');
    const cpSel = $('checkpoint');
    if (cpSel && !cpSel.options.length) {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = '未连接 ComfyUI';
      cpSel.appendChild(o);
    }
    const loraSel = $('lora');
    if (loraSel && !loraSel.options.length) {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = '（不使用 LoRA）';
      loraSel.appendChild(o);
    }
  }

  if (healthRes.status === 'fulfilled') {
    applyHealth(healthRes.value);
  } else {
    const conn = document.querySelector('.conn');
    const text = document.querySelector('.conn-text');
    if (conn) conn.classList.add('bad');
    if (text) text.textContent = '未连接 ComfyUI';
  }

  if (galleryRes.status === 'fulfilled') {
    loadGallery(galleryRes.value);
  } else {
    loadGallery([]);
  }

  loadSequences();
  loadTemplates();

  if (rig) rig.resize();

  const bootErr = [metaRes, cfgRes].filter((r) => r.status === 'rejected');
  if (bootErr.length && !genBusy) {
    setStatus('后端未就绪：' + bootErr[0].reason.message + '（页面仍可使用）', 'err');
  } else if (!genBusy) {
    setStatus('就绪');
  }

  runPreview();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
