/**
 * camera3d.js —— Studio 薄壳（v2.3）。
 *
 * 场景核心（网格 / 人体 / 视线锥 / 朝向箭头 / 方位环 / 高度弧 / 距离管 / 动态视锥 /
 * 九宫格 / 地平线 / 参考图叠层 / 拖拽 / 滚轮 / setState / getState / 离屏导出）全部在
 * /js/camera_rig.js 的 CameraRig 里，与 ComfyUI 节点 (js/sdxl_camera_widget.js) 共用同一套实现。
 * 这里只保留 Studio 独有的壳：HUD 文本、document 级快捷键 1..6 / G / F。
 * 档位与 FOV 换算一律来自 /js/camera_shared.js（单一真源）。
 */
import { CameraRig as CameraRigCore } from "/js/camera_rig.js";
import { focalToFov } from "/js/camera_shared.js";

const SHOT_KEYS = ["1", "2", "3", "4", "5", "6"];
const HUD_LENS_ID = "hud_lens";
const HUD_POSE_ID = "hud_pose";

/** 输入框里敲数字不该触发机位切换 */
function isTypingTarget(el) {
  if (!el) return false;
  const tag = String(el.tagName || "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return el.isContentEditable === true;
}

export class CameraRig extends CameraRigCore {
  constructor(canvas, options) {
    super(canvas, options || {});
    this._hudLensEl = null;
    this._hudPoseEl = null;
    this._bindHud();
    this._paintHud();
    this._bindShellKeys();
  }

  /** 基类 _updateLens() 每帧可能被调用，HUD 跟着刷新 */
  _updateLens() {
    super._updateLens();
    this._paintHud();
  }

  _bindHud() {
    if (typeof document === "undefined") return;
    this._hudLensEl = document.getElementById(HUD_LENS_ID);
    this._hudPoseEl = document.getElementById(HUD_POSE_ID);
  }

  _paintHud() {
    if (!this._hudLensEl && !this._hudPoseEl) this._bindHud();
    const s = this.state || {};
    const focal = Number(s.focal);
    const fov = Number.isFinite(this.fovDeg) && this.fovDeg > 0
      ? this.fovDeg
      : focalToFov(Number.isFinite(focal) ? focal : 50);
    if (this._hudLensEl) {
      this._hudLensEl.textContent = Math.round(Number.isFinite(focal) ? focal : 50) + "mm · " + fov.toFixed(1) + "°";
    }
    if (this._hudPoseEl) {
      const info = this.info ? this.info() : null;
      this._hudPoseEl.textContent = info ? info.pose_text : "";
    }
  }

  /** document 级快捷键：1 正面 / 2 背面 / 3 左正侧 / 4 右正侧 / 5 俯拍 / 6 仰拍 / G 九宫格 / F 视锥 */
  _bindShellKeys() {
    if (typeof document === "undefined" || typeof document.addEventListener !== "function") return;
    this._onShellKey = (e) => this._handleShellKey(e);
    document.addEventListener("keydown", this._onShellKey);
  }

  _handleShellKey(e) {
    if (!e || e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;
    const key = String(e.key || "");
    if (SHOT_KEYS.indexOf(key) >= 0) {
      e.preventDefault();
      this.applyShot(Number(key));
      return;
    }
    const up = key.toUpperCase();
    if (up === "G") {
      e.preventDefault();
      this.toggleGrid();
      return;
    }
    if (up === "F") {
      e.preventDefault();
      this.toggleFrustum();
    }
  }

  dispose() {
    if (this._onShellKey && typeof document !== "undefined" && document.removeEventListener) {
      document.removeEventListener("keydown", this._onShellKey);
      this._onShellKey = null;
    }
    if (super.dispose) super.dispose();
  }
}

export default CameraRig;
