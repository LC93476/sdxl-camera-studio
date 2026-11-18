/**
 * camera3d.js —— 3D 相机机位编辑器（原生 three.js，无 OrbitControls / 无 CSS2DRenderer）
 *
 * 场景约定：人形面朝 +Z。
 *   pos_x ∈ [-1,1]  → 方位角 az = pos_x * π（0 = 正面 +Z，+1 = 背面 -Z，右侧为正）
 *   pos_y ∈ [-1,1]  → 仰角 el = pos_y * 78°（+ 俯视 / - 仰视）
 *   pos_z ∈ [-1,1]  → 距离（+ 近 / - 远）
 *   roll  ∈ [0,1]   → 画面线框倾斜 roll*30°
 *
 * 对外 API：new CameraRig(canvasEl, { onChange })、setState({...})、resize()、getState()、dispose()
 */
import * as THREE from 'three';

const DEG = Math.PI / 180;
const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
const r3 = (v) => Math.round(v * 1000) / 1000;

const LEAN = 78 * DEG;        // pos_y = ±1 时的极值仰角
const AZ_R = 2.05;            // 方位环半径
const EL_R = 1.52;            // 高度弧半径
const ANCHOR_Y = 0.98;        // 人形中心（相机注视点）
const CAM_NEAR = 2.62;        // pos_z = +1
const CAM_FAR = 7.2;          // pos_z = -1
const DOT_Y = 0.08;           // 粉点离地高度

const COLOR = {
  bg: 0x16171b,
  violet: 0xa78bfa,
  pink: 0xff7ab6,
  cyan: 0x67e8f9,
  accent: 0xffd75e,
  body: 0x4b5261,
  bodyDark: 0x353b47,
};

export class CameraRig {
  constructor(canvasEl, options = {}) {
    if (!canvasEl) throw new Error('CameraRig: 需要 canvas 元素');

    this.canvas = canvasEl;
    this.onChange = typeof options.onChange === 'function' ? options.onChange : null;
    this.state = { pos_x: 0, pos_y: 0, pos_z: 0, roll: 0 };

    this.target = new THREE.Vector3(0, ANCHOR_Y, 0);
    this.orbit = { theta: 0.62, phi: 1.10, radius: 8.6 };
    this.drag = null;
    this.hover = null;
    this._raf = 0;
    this._ndc = new THREE.Vector2();
    this._ray = new THREE.Raycaster();

    this._initRenderer();
    this._initScene();
    this._initHuman();
    this._initRings();
    this._initCameraMarker();
    this._bindEvents();

    this._sync();
    this.resize();
  }

  // ────────────────────────────── 初始化 ──────────────────────────────

  _initRenderer() {
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      antialias: true,
      alpha: false,
    });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.renderer.setClearColor(COLOR.bg, 1);
  }

  _initScene() {
    this.scene = new THREE.Scene();
    this.view = new THREE.PerspectiveCamera(42, 1, 0.1, 120);

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.55));

    const key = new THREE.DirectionalLight(0xffffff, 0.85);
    key.position.set(4, 8, 6);
    this.scene.add(key);

    const rim = new THREE.DirectionalLight(0xa78bfa, 0.45);
    rim.position.set(-5, 4, -6);
    this.scene.add(rim);

    const fill = new THREE.DirectionalLight(0x67e8f9, 0.18);
    fill.position.set(-3, 2, 5);
    this.scene.add(fill);

    const grid = new THREE.GridHelper(9, 30, 0x2b2f38, 0x20232a);
    grid.material.transparent = true;
    grid.material.opacity = 0.75;
    grid.position.y = 0;
    this.scene.add(grid);

    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(0.62, 48),
      new THREE.MeshBasicMaterial({ color: 0x1b1d23, transparent: true, opacity: 0.85 })
    );
    disc.rotation.x = -Math.PI / 2;
    disc.position.y = 0.004;
    this.scene.add(disc);
  }

  _initHuman() {
    const g = new THREE.Group();
    const skin = new THREE.MeshStandardMaterial({ color: COLOR.body, roughness: 0.68, metalness: 0.14 });
    const dark = new THREE.MeshStandardMaterial({ color: COLOR.bodyDark, roughness: 0.82, metalness: 0.1 });
    const ink = new THREE.MeshBasicMaterial({ color: 0x111317 });

    const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.21, 0.42, 8, 24), skin);
    torso.position.y = 1.06;
    g.add(torso);

    const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.07, 0.1, 16), dark);
    neck.position.y = 1.48;
    g.add(neck);

    const head = new THREE.Mesh(new THREE.SphereGeometry(0.155, 28, 22), skin);
    head.position.y = 1.66;
    g.add(head);

    // 面朝 +Z：两只眼睛给方向感
    for (const sx of [-1, 1]) {
      const eye = new THREE.Mesh(new THREE.SphereGeometry(0.021, 10, 10), ink);
      eye.position.set(sx * 0.055, 1.69, 0.135);
      g.add(eye);
    }

    for (const sx of [-1, 1]) {
      const leg = new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.52, 6, 16), dark);
      leg.position.set(sx * 0.105, 0.38, 0);
      g.add(leg);

      const foot = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.06, 0.23), dark);
      foot.position.set(sx * 0.105, 0.035, 0.045);
      g.add(foot);

      const arm = new THREE.Mesh(new THREE.CapsuleGeometry(0.055, 0.46, 6, 14), skin);
      arm.position.set(sx * 0.295, 1.06, 0);
      arm.rotation.z = sx * 0.09;
      g.add(arm);
    }

    this.human = g;
    this.scene.add(g);
  }

  _initRings() {
    // 方位环（紫）：水平圆环
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(AZ_R, 0.016, 8, 128),
      new THREE.MeshBasicMaterial({ color: COLOR.violet, transparent: true, opacity: 0.75 })
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = DOT_Y;
    this.scene.add(ring);
    this.azRing = ring;

    // 环上的小刻度（每 45° 一根）
    const ticks = new THREE.Group();
    const tickMat = new THREE.MeshBasicMaterial({ color: COLOR.violet, transparent: true, opacity: 0.4 });
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2;
      const tick = new THREE.Mesh(new THREE.BoxGeometry(0.015, 0.015, 0.14), tickMat);
      tick.position.set(Math.sin(a) * AZ_R, DOT_Y, Math.cos(a) * AZ_R);
      tick.rotation.y = a;
      ticks.add(tick);
    }
    this.scene.add(ticks);

    // 粉点：拖它改 pos_x
    this.azDot = new THREE.Mesh(
      new THREE.SphereGeometry(0.085, 24, 18),
      new THREE.MeshStandardMaterial({
        color: COLOR.pink,
        emissive: COLOR.pink,
        emissiveIntensity: 0.75,
        roughness: 0.35,
        metalness: 0.1,
      })
    );
    this.scene.add(this.azDot);

    this.azHit = new THREE.Mesh(new THREE.SphereGeometry(0.26, 12, 10), this._hitMaterial());
    this.azHit.userData.pick = 'az';
    this.scene.add(this.azHit);

    // 高度弧（青）：竖直半圆，随方位角旋转，始终落在"人形→相机"的径向平面上
    this.arcGroup = new THREE.Group();
    this.arcGroup.position.set(0, ANCHOR_Y, 0);

    const pts = [];
    for (let i = 0; i <= 64; i++) {
      const a = -Math.PI / 2 + (Math.PI * i) / 64;
      pts.push(new THREE.Vector3(0, Math.sin(a) * EL_R, Math.cos(a) * EL_R));
    }
    const curve = new THREE.CatmullRomCurve3(pts);
    const arc = new THREE.Mesh(
      new THREE.TubeGeometry(curve, 64, 0.014, 8, false),
      new THREE.MeshBasicMaterial({ color: COLOR.cyan, transparent: true, opacity: 0.8 })
    );
    this.arcGroup.add(arc);

    this.elDot = new THREE.Mesh(
      new THREE.SphereGeometry(0.085, 24, 18),
      new THREE.MeshStandardMaterial({
        color: COLOR.cyan,
        emissive: COLOR.cyan,
        emissiveIntensity: 0.75,
        roughness: 0.35,
        metalness: 0.1,
      })
    );
    this.arcGroup.add(this.elDot);

    this.elHit = new THREE.Mesh(new THREE.SphereGeometry(0.28, 12, 10), this._hitMaterial());
    this.elHit.userData.pick = 'el';
    this.arcGroup.add(this.elHit);

    this.scene.add(this.arcGroup);
  }

  _hitMaterial() {
    return new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0,
      depthWrite: false,
    });
  }

  _initCameraMarker() {
    const g = new THREE.Group();

    // 机身（黄 #ffd75e）+ 镜头，+Z 为取景方向
    const body = new THREE.Mesh(
      new THREE.BoxGeometry(0.24, 0.17, 0.14),
      new THREE.MeshStandardMaterial({
        color: COLOR.accent,
        emissive: COLOR.accent,
        emissiveIntensity: 0.32,
        roughness: 0.42,
        metalness: 0.35,
      })
    );
    g.add(body);

    const lens = new THREE.Mesh(
      new THREE.CylinderGeometry(0.055, 0.075, 0.1, 20),
      new THREE.MeshStandardMaterial({ color: 0x2b2d34, roughness: 0.4, metalness: 0.6 })
    );
    lens.rotation.x = Math.PI / 2;
    lens.position.z = 0.1;
    g.add(lens);

    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(0.2, 16, 12),
      new THREE.MeshBasicMaterial({ color: COLOR.accent, transparent: true, opacity: 0.12, depthWrite: false })
    );
    g.add(halo);

    // roll 的视觉反馈：取景框线框，roll>0 时倾斜 roll*30°
    this.frame = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.PlaneGeometry(0.94, 0.64)),
      new THREE.LineBasicMaterial({ color: COLOR.accent, transparent: true, opacity: 0.9 })
    );
    this.frame.position.set(0, 0, 0.62);
    g.add(this.frame);

    this.frameFill = new THREE.Mesh(
      new THREE.PlaneGeometry(0.94, 0.64),
      new THREE.MeshBasicMaterial({
        color: COLOR.accent,
        transparent: true,
        opacity: 0.05,
        side: THREE.DoubleSide,
        depthWrite: false,
      })
    );
    this.frameFill.position.copy(this.frame.position);
    g.add(this.frameFill);

    this.camGroup = g;
    this.scene.add(g);

    // 相机 → 人形 的连线
    const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
    this.link = new THREE.Line(
      geo,
      new THREE.LineBasicMaterial({ color: COLOR.accent, transparent: true, opacity: 0.22 })
    );
    this.link.frustumCulled = false;
    this.scene.add(this.link);
  }

  // ────────────────────────────── 事件 ──────────────────────────────

  _bindEvents() {
    this._onDown = (e) => this._pointerDown(e);
    this._onMove = (e) => this._pointerMove(e);
    this._onUp = (e) => this._pointerUp(e);
    this._onWheel = (e) => this._wheel(e);
    this._onContext = (e) => e.preventDefault();
    this._onResize = () => this.resize();

    this.canvas.addEventListener('pointerdown', this._onDown);
    this.canvas.addEventListener('pointermove', this._onMove);
    this.canvas.addEventListener('pointerup', this._onUp);
    this.canvas.addEventListener('pointercancel', this._onUp);
    this.canvas.addEventListener('pointerleave', () => this._setHover(null));
    this.canvas.addEventListener('wheel', this._onWheel, { passive: false });
    this.canvas.addEventListener('contextmenu', this._onContext);

    window.addEventListener('resize', this._onResize);
    if (typeof ResizeObserver === 'function') {
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this.canvas);
    }
  }

  _toNDC(e) {
    const rect = this.canvas.getBoundingClientRect();
    const w = rect.width || 1;
    const h = rect.height || 1;
    this._ndc.set(((e.clientX - rect.left) / w) * 2 - 1, -((e.clientY - rect.top) / h) * 2 + 1);
    this._ray.setFromCamera(this._ndc, this.view);
    return this._ray;
  }

  _pick(e) {
    const ray = this._toNDC(e);
    const hits = ray.intersectObjects([this.azHit, this.elHit], false);
    if (!hits.length) return null;
    return hits[0].object.userData.pick || null;
  }

  _pointerDown(e) {
    let mode = null;

    const hit = this._pick(e);
    if (hit) {
      mode = hit;
    } else if (e.button === 2 || e.shiftKey) {
      mode = 'pan';
    } else if (e.button === 0) {
      mode = 'orbit';
    }
    if (!mode) return;

    this.drag = { mode, x: e.clientX, y: e.clientY };
    this.canvas.classList.add('dragging');
    try {
      this.canvas.setPointerCapture(e.pointerId);
    } catch (_) {
      /* 捕获失败不影响拖拽 */
    }
    e.preventDefault();

    if (mode === 'az' || mode === 'el') this._dragDot(mode, e);
  }

  _pointerMove(e) {
    if (!this.drag) {
      this._setHover(this._pick(e));
      return;
    }

    const mode = this.drag.mode;
    if (mode === 'az' || mode === 'el') {
      this._dragDot(mode, e);
      return;
    }

    const dx = e.clientX - this.drag.x;
    const dy = e.clientY - this.drag.y;
    this.drag.x = e.clientX;
    this.drag.y = e.clientY;

    if (mode === 'orbit') {
      this.orbit.theta -= dx * 0.0075;
      this.orbit.phi = clamp(this.orbit.phi - dy * 0.0065, 0.2, Math.PI - 0.2);
    } else {
      const k = this.orbit.radius * 0.0015;
      const right = new THREE.Vector3(1, 0, 0).applyQuaternion(this.view.quaternion);
      const up = new THREE.Vector3(0, 1, 0).applyQuaternion(this.view.quaternion);
      this.target.addScaledVector(right, -dx * k);
      this.target.addScaledVector(up, dy * k);
      this.target.y = clamp(this.target.y, 0.25, 2.2);
    }
    this._invalidate();
  }

  _pointerUp(e) {
    if (!this.drag) return;
    this.drag = null;
    this.canvas.classList.remove('dragging');
    try {
      if (e && e.pointerId !== undefined) this.canvas.releasePointerCapture(e.pointerId);
    } catch (_) {
      /* 已释放 */
    }
  }

  _setHover(name) {
    if (this.hover === name) return;
    this.hover = name;
    this.canvas.classList.toggle('pickable', !!name);
    this.azDot.scale.setScalar(name === 'az' ? 1.25 : 1);
    this.elDot.scale.setScalar(name === 'el' ? 1.25 : 1);
    this._invalidate();
  }

  _wheel(e) {
    e.preventDefault();
    // 向上滚 = 靠近（pos_z 变大）
    const step = e.deltaY > 0 ? -0.06 : 0.06;
    this.state.pos_z = clamp(this.state.pos_z + step, -1, 1);
    this._sync();
    this._emit();
    this._invalidate();
  }

  /** 拖点：把射线投到对应平面，反解 pos_x / pos_y */
  _dragDot(mode, e) {
    const ray = this._toNDC(e);
    const p = new THREE.Vector3();

    if (mode === 'az') {
      const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -DOT_Y);
      if (!ray.ray.intersectPlane(plane, p)) return;
      const az = Math.atan2(p.x, p.z);
      this.state.pos_x = clamp(az / Math.PI, -1, 1);
    } else {
      const az = this.state.pos_x * Math.PI;
      const normal = new THREE.Vector3(Math.cos(az), 0, -Math.sin(az));
      const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, new THREE.Vector3(0, ANCHOR_Y, 0));
      if (!ray.ray.intersectPlane(plane, p)) return;
      const ly = p.y - ANCHOR_Y;
      const lz = p.x * Math.sin(az) + p.z * Math.cos(az);
      const a = Math.atan2(ly, lz);
      this.state.pos_y = clamp(a / LEAN, -1, 1);
    }

    this._sync();
    this._emit();
    this._invalidate();
  }

  // ────────────────────────────── 状态 → 场景 ──────────────────────────────

  _sync() {
    const s = this.state;
    const az = clamp(s.pos_x, -1, 1) * Math.PI;
    const el = clamp(s.pos_y, -1, 1) * LEAN;
    const pz = clamp(s.pos_z, -1, 1);
    const dist = CAM_NEAR + ((1 - pz) / 2) * (CAM_FAR - CAM_NEAR);

    const sinAz = Math.sin(az);
    const cosAz = Math.cos(az);
    const cosEl = Math.cos(el);
    const sinEl = Math.sin(el);

    // 粉色方位点
    const ax = AZ_R * sinAz;
    const azZ = AZ_R * cosAz;
    this.azDot.position.set(ax, DOT_Y, azZ);
    this.azHit.position.copy(this.azDot.position);

    // 青色高度弧 / 点
    this.arcGroup.rotation.y = az;
    this.elDot.position.set(0, EL_R * sinEl, EL_R * cosEl);
    this.elHit.position.copy(this.elDot.position);

    // 黄色相机标记
    const cx = dist * cosEl * sinAz;
    const cy = ANCHOR_Y + dist * sinEl;
    const cz = dist * cosEl * cosAz;
    this.camGroup.position.set(cx, cy, cz);
    this.camGroup.lookAt(this.target);

    const tilt = clamp(s.roll, 0, 1) * 30 * DEG;
    this.frame.rotation.z = tilt;
    this.frameFill.rotation.z = tilt;

    // 相机 → 人形连线
    const arr = this.link.geometry.attributes.position.array;
    arr[0] = cx; arr[1] = cy; arr[2] = cz;
    arr[3] = this.target.x; arr[4] = ANCHOR_Y; arr[5] = this.target.z;
    this.link.geometry.attributes.position.needsUpdate = true;
    this.link.geometry.computeBoundingSphere();
  }

  _emit() {
    if (!this.onChange) return;
    const s = this.state;
    this.onChange({
      pos_x: r3(s.pos_x),
      pos_y: r3(s.pos_y),
      pos_z: r3(s.pos_z),
      roll: r3(s.roll),
    });
  }

  _applyView() {
    const { theta, phi, radius } = this.orbit;
    const sp = Math.sin(phi);
    this.view.position.set(
      this.target.x + radius * sp * Math.sin(theta),
      this.target.y + radius * Math.cos(phi),
      this.target.z + radius * sp * Math.cos(theta)
    );
    this.view.lookAt(this.target);
  }

  _invalidate() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this._render();
    });
  }

  _render() {
    this._applyView();
    this.renderer.render(this.scene, this.view);
  }

  // ────────────────────────────── 对外 API ──────────────────────────────

  setState(next) {
    if (!next) return;
    const s = this.state;
    if (Number.isFinite(next.pos_x)) s.pos_x = clamp(next.pos_x, -1, 1);
    if (Number.isFinite(next.pos_y)) s.pos_y = clamp(next.pos_y, -1, 1);
    if (Number.isFinite(next.pos_z)) s.pos_z = clamp(next.pos_z, -1, 1);
    if (Number.isFinite(next.roll)) s.roll = clamp(next.roll, 0, 1);
    this._sync();
    this._invalidate();
  }

  getState() {
    const s = this.state;
    return { pos_x: r3(s.pos_x), pos_y: r3(s.pos_y), pos_z: r3(s.pos_z), roll: r3(s.roll) };
  }

  resize() {
    const w = Math.max(1, Math.round(this.canvas.clientWidth || 1));
    const h = Math.max(1, Math.round(this.canvas.clientHeight || 1));
    this.renderer.setSize(w, h, false);
    this.view.aspect = w / h;
    this.view.updateProjectionMatrix();
    this._render();
  }

  dispose() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    this.canvas.removeEventListener('pointerdown', this._onDown);
    this.canvas.removeEventListener('pointermove', this._onMove);
    this.canvas.removeEventListener('pointerup', this._onUp);
    this.canvas.removeEventListener('pointercancel', this._onUp);
    this.canvas.removeEventListener('wheel', this._onWheel);
    this.canvas.removeEventListener('contextmenu', this._onContext);
    window.removeEventListener('resize', this._onResize);
    if (this._ro) this._ro.disconnect();
    this.renderer.dispose();
  }
}
