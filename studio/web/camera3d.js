/**
 * camera3d.js —— 3D 相机机位编辑器（v2，原生 three.js，无 OrbitControls / 无 CSS2DRenderer）
 *
 * 场景约定（SPEC.md 第 7/8 节）：人形面朝 +Z。
 *   pos_x ∈ [-1,1]  → 方位角 az = pos_x * π（0 = 正面 +Z，+1 = 背面 -Z，右侧为正）
 *   pos_y ∈ [-1,1]  → 仰角 el = pos_y * 78°（+ 俯视 / - 仰视）
 *   pos_z ∈ [-1,1]  → 距离（+ 近 / - 远）
 *   roll  ∈ [-1,1]  → 画面线框倾斜 roll*45°（负值 = 反向荷兰角）
 *   focal ∈ [4,1200] → 水平视场角 fov = 2*atan(36/(2*focal))（36mm 全画幅宽；单一真源 js/camera_shared.js）
 *   pan_x/pan_y ∈ [-1,1] → 构图偏移（pan_x>0 = 相机右移 → 主体偏左；pan_y>0 = 主体偏下）
 *   subject_yaw ∈ [-1,1] → 主体朝向（0 正面 / ±0.28 起四分之三 / ±0.72 起背身）
 *   gaze_mode ∈ {auto,camera,away} → 视线（auto 走 subjectKeys 的自动判定）
 *
 * 对外 API：new CameraRig(canvasEl, { onChange, onCommit }) / setState({...}) / getState() /
 *           resize() / dispose() / setRefImage(url) / setRefOpacity(v) /
 *           toggleGrid(force) / toggleFrustum(force) / applyShot(n)
 *
 * 直接操纵（task-7）：脚下黄色箭头拖 subject_yaw / 取景框外环拖 roll /
 *           Alt + 拖画布调 pan_x·pan_y。拖动过程 onChange(v, {live:true}) 只刷预览，
 *           pointerup 才发一次不带 live 的回调 → app.js 在那里做持久化提交。
 */
import * as THREE from "/js/three.module.js";
import { focalToFov, azimuthBins, elevationKey, subjectKeys } from "/js/camera_shared.js";

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
const HEAD_Y = 1.66;          // 头部中心（视线锥起点）

// 取景框（相机标记前方的画面比例框，3:2）
const FRAME_Z = 0.62;
const FRAME_W = 0.94;
const FRAME_H = 0.64;
const HALF_W = FRAME_W / 2;
const HALF_H = FRAME_H / 2;
const FRAME_ASPECT = FRAME_W / FRAME_H;

// 视锥：从相机标记沿 +Z 张开的四棱锥
const FRUSTUM_D = 1.55;

// 视口相机 fov 直接取 focal 换算的值，但夹在这个区间里，否则 200mm 会把导航压成针孔
const VIEW_FOV_MIN = 26;
const VIEW_FOV_MAX = 74;

// roll：±1 → ±45°，负值就是反向荷兰角
const ROLL_MAX = 45 * DEG;
// pan_x/pan_y 是构图系数，乘这个常量换成相机空间的注视点偏移（量级≈人体宽度 1 单位）
const PAN_SCALE = 2.0;
// 取景框外环热区半径 = 角点半径 × 1.08（即框外 8%），命中即进 roll 拖拽
const FRAME_RING_K = 1.08;
// Alt + 拖拽：每像素换多少 pan（1000px 画布上拖 200px ≈ 1.0）
const PAN_DRAG_K = 0.005;

const WORLD_UP = new THREE.Vector3(0, 1, 0);

const COLOR = {
  bg: 0x16171b,
  violet: 0xa78bfa,
  pink: 0xff7ab6,
  cyan: 0x67e8f9,
  accent: 0xffd75e,
  body: 0x4b5261,
  bodyDark: 0x353b47,
  grid: 0xffd75e,
};

const BIN_CN = {
  front: "正面", front_right: "右前方", right: "右侧", back_right: "右后方",
  back: "背面", back_left: "左后方", left: "左侧", front_left: "左前方",
};
const EL_CN = { high: "俯视", eye: "平视", low: "仰视", bird: "鸟瞰", worm: "虫瞰" };

// 快捷键 1..6 → 标准机位（pos_x / pos_y）
const SHOTS = {
  1: { name: "正面", pos_x: 0, pos_y: 0 },
  2: { name: "背面", pos_x: 1, pos_y: 0 },
  3: { name: "左正侧", pos_x: -0.5, pos_y: 0 },
  4: { name: "右正侧", pos_x: 0.5, pos_y: 0 },
  5: { name: "高角度俯拍", pos_x: 0, pos_y: 0.62 },
  6: { name: "低角度仰拍", pos_x: 0, pos_y: -0.62 },
};

export class CameraRig {
  constructor(canvasEl, options = {}) {
    if (!canvasEl) throw new Error("CameraRig: 需要 canvas 元素");

    this.canvas = canvasEl;
    this.onChange = typeof options.onChange === "function" ? options.onChange : null;
    this.onCommit = typeof options.onCommit === "function" ? options.onCommit : null;
    this.state = {
      pos_x: 0, pos_y: 0, pos_z: 0, roll: 0,
      focal: 50, pan_x: 0, pan_y: 0, subject_yaw: 0, gaze_mode: "auto",
    };

    this.fovDeg = focalToFov(50);
    this.gridOn = true;
    this.frustumOn = true;

    this.target = new THREE.Vector3(0, ANCHOR_Y, 0);
    this.orbit = { theta: 0.62, phi: 1.10, radius: 8.6 };
    this.drag = null;
    this.hover = null;
    this._raf = 0;
    this._ndc = new THREE.Vector2();
    this._ray = new THREE.Raycaster();
    this._refUrl = "";

    this._initRenderer();
    this._initScene();
    this._initHuman();
    this._initSubjectMarks();
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
    this.scene.fog = new THREE.Fog(COLOR.bg, 12, 26);
    this.view = new THREE.PerspectiveCamera(42, 1, 0.1, 200);

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.55));

    const key = new THREE.DirectionalLight(0xffffff, 0.85);
    key.position.set(4, 8, 6);
    this.scene.add(key);

    const rim = new THREE.DirectionalLight(COLOR.violet, 0.45);
    rim.position.set(-5, 4, -6);
    this.scene.add(rim);

    const fill = new THREE.DirectionalLight(COLOR.cyan, 0.18);
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
    // 主体整体挂在 subjectRig 上：subject_yaw 一转，身体 / 脚下箭头一起转
    this.subjectRig = new THREE.Group();

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
    head.position.y = HEAD_Y;
    g.add(head);

    // 面朝 +Z：两只眼睛给方向感（背面看着是后脑勺）
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
    this.subjectRig.add(g);
    this.scene.add(this.subjectRig);
  }

  /** 脚下正面箭头 + 视线锥（两者都随 subject_yaw / gaze_mode 变） */
  _initSubjectMarks() {
    const arrow = new THREE.Group();

    const mat = new THREE.MeshBasicMaterial({ color: COLOR.accent, transparent: true, opacity: 0.9 });
    const shaft = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.012, 0.24), mat);
    shaft.position.set(0, 0, 0.2);
    arrow.add(shaft);

    const head = new THREE.Mesh(new THREE.ConeGeometry(0.085, 0.16, 16), mat);
    head.rotation.x = Math.PI / 2;
    head.position.set(0, 0, 0.4);
    arrow.add(head);

    arrow.position.y = 0.02;

    // 可拖热区：箭头本身太细，套一个不可见盒体，raycast 才好命中
    this.footHit = new THREE.Mesh(new THREE.BoxGeometry(0.52, 0.3, 0.9), this._hitMaterial());
    this.footHit.position.set(0, 0.1, 0.2);
    this.footHit.userData.pick = "yaw";
    arrow.add(this.footHit);

    this.footArrow = arrow;
    this.subjectRig.add(arrow);

    // 视线锥：锥心在头部，锥底朝视线方向张开（+Z 是张开方向，lookAt 让它对准目标）
    const coneLen = 0.62;
    const coneGeo = new THREE.ConeGeometry(0.12, coneLen, 18, 1, true);
    coneGeo.rotateX(-Math.PI / 2);          // 顶点转到 -Z
    coneGeo.translate(0, 0, coneLen / 2);   // 顶点回到原点，锥底朝 +Z
    this.gaze = new THREE.Group();
    const cone = new THREE.Mesh(
      coneGeo,
      new THREE.MeshBasicMaterial({
        color: COLOR.cyan,
        transparent: true,
        opacity: 0.22,
        side: THREE.DoubleSide,
        depthWrite: false,
      })
    );
    this.gaze.add(cone);
    this.gaze.position.set(0, HEAD_Y, 0);
    this.scene.add(this.gaze);
  }

  _initRings() {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(AZ_R, 0.016, 8, 128),
      new THREE.MeshBasicMaterial({ color: COLOR.violet, transparent: true, opacity: 0.75 })
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = DOT_Y;
    this.scene.add(ring);
    this.azRing = ring;

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
    this.azHit.userData.pick = "az";
    this.scene.add(this.azHit);

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
    this.elHit.userData.pick = "el";
    this.arcGroup.add(this.elHit);

    this.scene.add(this.arcGroup);
  }

  // 不可见拾取材质。必须 DoubleSide：取景框外环（frameRingHit）的平面朝向与视线相反，
  // 单面材质会让 raycaster 永远打不中，拖拽转 roll 会退化成 orbit。
  _hitMaterial() {
    return new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
  }

  _initCameraMarker() {
    const g = new THREE.Group();

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

    // 视锥线框：8 条棱（顶点 → 4 角，4 角成框），几何体在 _updateFrustum 里按 fov 重写
    const fgeo = new THREE.BufferGeometry();
    fgeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(16 * 3), 3));
    this.frustum = new THREE.LineSegments(
      fgeo,
      new THREE.LineBasicMaterial({ color: COLOR.cyan, transparent: true, opacity: 0.55 })
    );
    this.frustum.frustumCulled = false;
    g.add(this.frustum);

    // 取景框（roll 视觉反馈 + 三分法 + 地平线 + 构图点）
    this.frameGrp = new THREE.Group();
    this.frameGrp.position.set(0, 0, FRAME_Z);

    this.frame = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.PlaneGeometry(FRAME_W, FRAME_H)),
      new THREE.LineBasicMaterial({ color: COLOR.accent, transparent: true, opacity: 0.9 })
    );
    this.frameGrp.add(this.frame);

    this.frameFill = new THREE.Mesh(
      new THREE.PlaneGeometry(FRAME_W, FRAME_H),
      new THREE.MeshBasicMaterial({
        color: COLOR.accent,
        transparent: true,
        opacity: 0.05,
        side: THREE.DoubleSide,
        depthWrite: false,
      })
    );
    this.frameGrp.add(this.frameFill);

    // 三分法九宫格（两条竖 + 两条横）
    const gv = FRAME_W / 6;
    const gh = FRAME_H / 6;
    const gpts = [
      -gv, -HALF_H, 0, -gv, HALF_H, 0,
      gv, -HALF_H, 0, gv, HALF_H, 0,
      -HALF_W, -gh, 0, HALF_W, -gh, 0,
      -HALF_W, gh, 0, HALF_W, gh, 0,
    ];
    const ggeo = new THREE.BufferGeometry();
    ggeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(gpts), 3));
    this.thirds = new THREE.LineSegments(
      ggeo,
      new THREE.LineBasicMaterial({ color: COLOR.grid, transparent: true, opacity: 0.34 })
    );
    this.frameGrp.add(this.thirds);

    // 地平线参考线（随仰角上下移动；出了取景框就隐藏）
    const hgeo = new THREE.BufferGeometry();
    hgeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array([-HALF_W, 0, 0, HALF_W, 0, 0]), 3));
    this.horizon = new THREE.Line(
      hgeo,
      new THREE.LineBasicMaterial({ color: 0x8ee6ff, transparent: true, opacity: 0.5 })
    );
    this.frameGrp.add(this.horizon);

    // 构图点：pan_x / pan_y 把主体推到哪个三分位，一眼可见
    this.subjectDot = new THREE.Mesh(
      new THREE.RingGeometry(0.048, 0.066, 28),
      new THREE.MeshBasicMaterial({ color: COLOR.pink, transparent: true, opacity: 0.85, side: THREE.DoubleSide })
    );
    this.frameGrp.add(this.subjectDot);

    // 取景框外环热区（不可见）：命中即进入 roll 旋转拖拽，半径 = 角点半径 × 1.08
    const ringR = 0.5 * Math.hypot(FRAME_W, FRAME_H) * FRAME_RING_K;
    this.frameRingHit = new THREE.Mesh(
      new THREE.RingGeometry(ringR * 0.86, ringR, 56),
      this._hitMaterial()
    );
    this.frameRingHit.userData.pick = "roll";
    this.frameGrp.add(this.frameRingHit);

    g.add(this.frameGrp);
    this.camGroup = g;
    this.scene.add(g);

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
    this._onKey = (e) => this._key(e);

    this.canvas.addEventListener("pointerdown", this._onDown);
    this.canvas.addEventListener("pointermove", this._onMove);
    this.canvas.addEventListener("pointerup", this._onUp);
    this.canvas.addEventListener("pointercancel", this._onUp);
    this.canvas.addEventListener("pointerleave", () => this._setHover(null));
    this.canvas.addEventListener("wheel", this._onWheel, { passive: false });
    this.canvas.addEventListener("contextmenu", this._onContext);
    document.addEventListener("keydown", this._onKey);

    window.addEventListener("resize", this._onResize);
    if (typeof ResizeObserver === "function") {
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this.canvas);
    }
  }

  _typing(e) {
    const t = e.target;
    if (!t || !t.tagName) return false;
    const tag = String(t.tagName).toUpperCase();
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable === true;
  }

  /** 快捷键：1..6 标准机位 / G 九宫格 / F 视锥（R 由 app.js 统一复位全部参数） */
  _key(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (this._typing(e)) return;

    const k = String(e.key || "");
    if (SHOTS[k]) {
      const shot = SHOTS[k];
      this.state.pos_x = shot.pos_x;
      this.state.pos_y = shot.pos_y;
      this._sync();
      this._emit();
      this._invalidate();
      e.preventDefault();
      return;
    }
    if (k === "g" || k === "G") {
      this.toggleGrid();
      e.preventDefault();
      return;
    }
    if (k === "f" || k === "F") {
      this.toggleFrustum();
      e.preventDefault();
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
    const hits = ray.intersectObjects([this.azHit, this.elHit, this.footHit, this.frameRingHit], false);
    if (!hits.length) return null;
    return hits[0].object.userData.pick || null;
  }

  _pointerDown(e) {
    let mode = null;

    const hit = this._pick(e);
    if (hit) {
      mode = hit;
    } else if (e.altKey) {
      mode = "framing";        // Alt + 拖画布：调构图 pan_x / pan_y
    } else if (e.button === 2 || e.shiftKey) {
      mode = "pan";
    } else if (e.button === 0) {
      mode = "orbit";
    }
    if (!mode) return;

    this.drag = { mode, x: e.clientX, y: e.clientY, mutated: false };
    if (mode === "roll") {
      // 抓取点的框内角度 + 当前 roll：拖动按角度差换算，抓哪儿都不会跳
      this.drag.a0 = this._frameAngle(e);
      this.drag.r0 = clamp(this.state.roll, -1, 1);
    }
    this.canvas.classList.add("dragging");
    try {
      this.canvas.setPointerCapture(e.pointerId);
    } catch (_) {
      /* 捕获失败不影响拖拽 */
    }
    e.preventDefault();

    if (mode === "az" || mode === "el") this._dragDot(mode, e);
    else if (mode === "yaw") this._dragYaw(e);
  }

  _pointerMove(e) {
    if (!this.drag) {
      this._setHover(this._pick(e));
      return;
    }

    const mode = this.drag.mode;
    if (mode === "az" || mode === "el") {
      this._dragDot(mode, e);
      return;
    }
    if (mode === "yaw") {
      this._dragYaw(e);
      return;
    }
    if (mode === "roll") {
      this._dragRoll(e);
      return;
    }

    const dx = e.clientX - this.drag.x;
    const dy = e.clientY - this.drag.y;
    this.drag.x = e.clientX;
    this.drag.y = e.clientY;

    if (mode === "orbit") {
      this.orbit.theta -= dx * 0.0075;
      this.orbit.phi = clamp(this.orbit.phi - dy * 0.0065, 0.2, Math.PI - 0.2);
    } else if (mode === "framing") {
      // 向右拖 = 主体右移（pan_x 减小）；向下拖 = 主体下移（pan_y 增大）
      this.state.pan_x = clamp(this.state.pan_x - dx * PAN_DRAG_K, -1, 1);
      this.state.pan_y = clamp(this.state.pan_y + dy * PAN_DRAG_K, -1, 1);
      this.drag.mutated = true;
      this._sync();
      this._emit({ live: true });
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
    const drag = this.drag;
    if (!drag) return;
    this.drag = null;
    this.canvas.classList.remove("dragging");
    try {
      if (e && e.pointerId !== undefined) this.canvas.releasePointerCapture(e.pointerId);
    } catch (_) {
      /* 已释放 */
    }
    // 拖动过程只发 live 回调（只刷预览）；松手才发不带 live 的一次 → 由 app.js 提交持久化
    if (drag.mutated) {
      this._emit();
      if (this.onCommit) this.onCommit(this.getState());
    }
  }

  _setHover(name) {
    if (this.hover === name) return;
    this.hover = name;
    this.canvas.classList.toggle("pickable", !!name);
    this.azDot.scale.setScalar(name === "az" ? 1.25 : 1);
    this.elDot.scale.setScalar(name === "el" ? 1.25 : 1);
    this._invalidate();
  }

  _wheel(e) {
    e.preventDefault();
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

    if (mode === "az") {
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

    if (this.drag) this.drag.mutated = true;
    this._sync();
    this._emit({ live: true });
    this._invalidate();
  }

  /** 取景框平面上的指针角度（roll 旋转拖拽用；拿不到交点返回 null） */
  _frameAngle(e) {
    const ray = this._toNDC(e);
    const normal = new THREE.Vector3(0, 0, 1).applyQuaternion(this.camGroup.quaternion);
    const origin = new THREE.Vector3();
    this.frameGrp.getWorldPosition(origin);
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin);
    const p = new THREE.Vector3();
    if (!ray.ray.intersectPlane(plane, p)) return null;
    // 在 camGroup 空间量角度，不要用 frameGrp.worldToLocal：frameGrp 自身被 roll 带着转
    // (rotation.z = roll * 45°)，在它内部量角度会把「框转过去了、指针没动」也算成角度变化，
    // 形成负反馈（拖 30° 只出 15°）。camGroup 不含 roll，角度只跟指针走。
    const local = p.sub(origin).applyQuaternion(this.camGroup.quaternion.clone().invert());
    return Math.atan2(local.y, local.x);
  }

  /** 拖取景框外环：框内角度差 → roll ∈ [-1,1]（45° 满刻度） */
  _dragRoll(e) {
    const a = this._frameAngle(e);
    if (a == null || this.drag.a0 == null) return;
    let d = a - this.drag.a0;
    while (d > Math.PI) d -= Math.PI * 2;
    while (d < -Math.PI) d += Math.PI * 2;
    this.state.roll = clamp(this.drag.r0 + d / ROLL_MAX, -1, 1);
    this.drag.mutated = true;
    this._sync();
    this._emit({ live: true });
    this._invalidate();
  }

  /** 拖脚下箭头：地面射线 → 绕人体竖直轴的角度 → subject_yaw ∈ [-1,1] */
  _dragYaw(e) {
    const ray = this._toNDC(e);
    const plane = new THREE.Plane(WORLD_UP.clone(), -DOT_Y);
    const p = new THREE.Vector3();
    if (!ray.ray.intersectPlane(plane, p)) return;
    this.state.subject_yaw = clamp(Math.atan2(p.x, p.z) / Math.PI, -1, 1);
    this.drag.mutated = true;
    this._sync();
    this._emit({ live: true });
    this._invalidate();
  }

  // ────────────────────────────── 光学 / 视锥 ──────────────────────────────

  /** focal → 视口 fov + 视锥口径 + HUD 读数 */
  _updateLens() {
    const focal = clamp(this.state.focal, 4, 1200);
    const fov = focalToFov(focal);          // 水平视场角（36mm 宽），度
    this.fovDeg = fov;

    this.view.fov = clamp(fov, VIEW_FOV_MIN, VIEW_FOV_MAX);
    this.view.updateProjectionMatrix();

    this._updateFrustum(fov);
    this._updateHud();
  }

  _updateFrustum(fovDeg) {
    const halfW = FRUSTUM_D * Math.tan((fovDeg * DEG) / 2);
    const halfH = halfW / FRAME_ASPECT;
    const d = FRUSTUM_D;
    const c = [
      [halfW, halfH, d], [-halfW, halfH, d], [-halfW, -halfH, d], [halfW, -halfH, d],
    ];
    const seg = [];
    for (let i = 0; i < 4; i++) {
      seg.push([0, 0, 0], c[i]);
      seg.push(c[i], c[(i + 1) % 4]);
    }
    const arr = this.frustum.geometry.attributes.position.array;
    let o = 0;
    for (let i = 0; i < seg.length; i++) {
      arr[o++] = seg[i][0];
      arr[o++] = seg[i][1];
      arr[o++] = seg[i][2];
    }
    this.frustum.geometry.attributes.position.needsUpdate = true;
    this.frustum.geometry.computeBoundingSphere();
  }

  _updateHud() {
    const el = document.getElementById("hud_lens");
    if (el) el.textContent = Math.round(this.state.focal) + "mm · " + this.fovDeg.toFixed(1) + "°";

    const pose = document.getElementById("hud_pose");
    if (!pose) return;
    const bins = azimuthBins(this.state.pos_x, "8bin");
    const top = bins.length ? bins[0][0] : "front";
    const el2 = elevationKey(this.state.pos_y, "3bin");
    pose.textContent = (BIN_CN[top] || top) + " · " + (EL_CN[el2] || el2);
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

    this.azDot.position.set(AZ_R * sinAz, DOT_Y, AZ_R * cosAz);
    this.azHit.position.copy(this.azDot.position);

    this.arcGroup.rotation.y = az;
    this.elDot.position.set(0, EL_R * sinEl, EL_R * cosEl);
    this.elHit.position.copy(this.elDot.position);

    const cx = dist * cosEl * sinAz;
    const cy = ANCHOR_Y + dist * sinEl;
    const cz = dist * cosEl * cosAz;
    this.camGroup.position.set(cx, cy, cz);

    // 注视点按 pan_x / pan_y 沿相机局部 right/up 偏移 —— 视锥与取景框才真实反映三分法
    const eye = new THREE.Vector3(cx, cy, cz);
    const base = this.target.clone();
    const fwd = base.clone().sub(eye).normalize();
    const right = new THREE.Vector3().crossVectors(fwd, WORLD_UP).normalize();
    const up = new THREE.Vector3().crossVectors(right, fwd).normalize();
    const aim = base.clone()
      .addScaledVector(right, clamp(s.pan_x, -1, 1) * PAN_SCALE)
      .addScaledVector(up, clamp(s.pan_y, -1, 1) * PAN_SCALE);
    this.camGroup.lookAt(aim);

    const tilt = clamp(s.roll, -1, 1) * ROLL_MAX;
    this.frameGrp.rotation.z = tilt;

    const arr = this.link.geometry.attributes.position.array;
    arr[0] = cx; arr[1] = cy; arr[2] = cz;
    arr[3] = this.target.x; arr[4] = ANCHOR_Y; arr[5] = this.target.z;
    this.link.geometry.attributes.position.needsUpdate = true;
    this.link.geometry.computeBoundingSphere();

    this._syncSubject();
    this._syncGaze();
    this._syncFraming(el);
    this._updateLens();
  }

  /** subject_yaw：主体整体转身；脚下箭头跟着转 */
  _syncSubject() {
    const yaw = clamp(this.state.subject_yaw, -1, 1);
    this.subjectRig.rotation.y = yaw * Math.PI;
  }

  /** 视线锥：auto 走 subjectKeys 判定，camera 看镜头，away 看身体正前方 */
  _syncGaze() {
    const yaw = clamp(this.state.subject_yaw, -1, 1);
    const res = subjectKeys(yaw, clamp(this.state.pos_x, -1, 1), this.state.gaze_mode);
    const gkey = res[1];
    const head = new THREE.Vector3(0, HEAD_Y, 0);
    const fwd = new THREE.Vector3(Math.sin(yaw * Math.PI), 0, Math.cos(yaw * Math.PI));

    let target;
    if (gkey === "gaze_viewer" || gkey === "gaze_over_shoulder") {
      target = this.camGroup.position.clone();
      if (gkey === "gaze_over_shoulder") {
        const side = new THREE.Vector3(Math.cos(yaw * Math.PI), 0, -Math.sin(yaw * Math.PI));
        target.addScaledVector(side, (yaw >= 0 ? 1 : -1) * 0.85);
      }
    } else {
      target = head.clone().addScaledVector(fwd, 2.4);
    }

    this.gaze.position.copy(head);
    this.gaze.lookAt(target);
    this.gaze.visible = gkey !== "gaze_away" || Math.abs(yaw) < 0.999;
  }

  /** 构图点 + 地平线：pan_x/pan_y 推到哪个三分位、仰角把地平线顶到哪 */
  _syncFraming(el) {
    const px = clamp(this.state.pan_x, -1, 1);
    const py = clamp(this.state.pan_y, -1, 1);
    this.subjectDot.position.set(-px * HALF_W * 0.9, -py * HALF_H * 0.9, 0.001);
    this.subjectDot.material.opacity = Math.max(Math.abs(px), Math.abs(py)) < 0.02 ? 0.4 : 0.85;

    const tanH = Math.tan((this.fovDeg * DEG) / 2);
    const tanV = Math.max(1e-6, tanH / FRAME_ASPECT);
    const off = (HALF_H * Math.tan(el)) / tanV;
    const pos = this.horizon.geometry.attributes.position.array;
    pos[1] = off;
    pos[4] = off;
    this.horizon.geometry.attributes.position.needsUpdate = true;
    this.horizon.visible = Math.abs(off) <= HALF_H * 1.35;
  }

  _emit(meta) {
    if (!this.onChange) return;
    const s = this.state;
    this.onChange({
      pos_x: r3(s.pos_x),
      pos_y: r3(s.pos_y),
      pos_z: r3(s.pos_z),
      roll: r3(s.roll),
      focal: Math.round(clamp(s.focal, 4, 1200)),
      pan_x: r3(s.pan_x),
      pan_y: r3(s.pan_y),
      subject_yaw: r3(s.subject_yaw),
      gaze_mode: String(s.gaze_mode || "auto"),
    }, meta || null);
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

  // ────────────────────────────── 参考图叠层 / 开关 ──────────────────────────────

  /** 本地图 → objectURL → 半透明叠层（不写 localStorage，换图时回收上一张） */
  setRefImage(url) {
    const el = document.getElementById("ref_overlay");
    if (this._refUrl && this._refUrl !== url && this._refUrl.indexOf("blob:") === 0) {
      try { URL.revokeObjectURL(this._refUrl); } catch (_) { /* 已失效 */ }
    }
    this._refUrl = url || "";
    if (!el) return;
    if (!url) {
      el.hidden = true;
      el.removeAttribute("src");
      return;
    }
    el.src = url;
    el.hidden = false;
  }

  setRefOpacity(v) {
    const el = document.getElementById("ref_overlay");
    if (el) el.style.opacity = String(clamp(Number(v) || 0, 0, 1));
  }

  toggleGrid(force) {
    this.gridOn = typeof force === "boolean" ? force : !this.gridOn;
    if (this.thirds) this.thirds.visible = this.gridOn;
    const overlay = document.getElementById("grid_overlay");
    if (overlay) overlay.classList.toggle("off", !this.gridOn);
    return this.gridOn;
  }

  toggleFrustum(force) {
    this.frustumOn = typeof force === "boolean" ? force : !this.frustumOn;
    if (this.frustum) this.frustum.visible = this.frustumOn;
    return this.frustumOn;
  }

  /** 快捷键 / 按钮共用：1..6 标准机位 */
  applyShot(n) {
    const shot = SHOTS[String(n)];
    if (!shot) return false;
    this.state.pos_x = shot.pos_x;
    this.state.pos_y = shot.pos_y;
    this._sync();
    this._emit();
    this._invalidate();
    return true;
  }

  // ────────────────────────────── 对外 API ──────────────────────────────

  setState(next) {
    if (!next) return;
    const s = this.state;
    if (Number.isFinite(next.pos_x)) s.pos_x = clamp(next.pos_x, -1, 1);
    if (Number.isFinite(next.pos_y)) s.pos_y = clamp(next.pos_y, -1, 1);
    if (Number.isFinite(next.pos_z)) s.pos_z = clamp(next.pos_z, -1, 1);
    if (Number.isFinite(next.roll)) s.roll = clamp(next.roll, -1, 1);
    if (Number.isFinite(next.focal)) s.focal = clamp(next.focal, 4, 1200);
    if (Number.isFinite(next.pan_x)) s.pan_x = clamp(next.pan_x, -1, 1);
    if (Number.isFinite(next.pan_y)) s.pan_y = clamp(next.pan_y, -1, 1);
    if (Number.isFinite(next.subject_yaw)) s.subject_yaw = clamp(next.subject_yaw, -1, 1);
    if (typeof next.gaze_mode === "string" && next.gaze_mode) s.gaze_mode = next.gaze_mode;
    this._sync();
    this._invalidate();
  }

  getState() {
    const s = this.state;
    return {
      pos_x: r3(s.pos_x),
      pos_y: r3(s.pos_y),
      pos_z: r3(s.pos_z),
      roll: r3(s.roll),
      focal: Math.round(clamp(s.focal, 4, 1200)),
      pan_x: r3(s.pan_x),
      pan_y: r3(s.pan_y),
      subject_yaw: r3(s.subject_yaw),
      gaze_mode: String(s.gaze_mode || "auto"),
    };
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
    this.canvas.removeEventListener("pointerdown", this._onDown);
    this.canvas.removeEventListener("pointermove", this._onMove);
    this.canvas.removeEventListener("pointerup", this._onUp);
    this.canvas.removeEventListener("pointercancel", this._onUp);
    this.canvas.removeEventListener("wheel", this._onWheel);
    this.canvas.removeEventListener("contextmenu", this._onContext);
    document.removeEventListener("keydown", this._onKey);
    window.removeEventListener("resize", this._onResize);
    if (this._ro) this._ro.disconnect();
    this.setRefImage("");
    this.renderer.dispose();
  }
}
