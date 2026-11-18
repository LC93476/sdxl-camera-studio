# SDXL 相机控制 —— 设计契约（Lead 定稿，所有人以此为准）

## 0. 任务
把 https://github.com/xunying7860/Comfyui-Anima_camera_angle 的"相机角度控制"原理移植到 SDXL，
并且支持手动调摄像机直接出图。原始仓库已 clone 到 `D:/QUARK/_repo2/Comfyui-Anima_camera_angle/`。

## 1. 原节点原理（已分析完，作为事实）
它不是 LoRA、不是 ControlNet，**纯粹是提示词权重生成器**：
3D 参数 → 自然语言相机标签 + 权重，拼成 `(tag:weight), (tag:weight)` 丢给 CLIPTextEncode。

- `pos_x` 方位角：`az = pos_x * pi`；四向球面分解
  `front=max(0,cos az)`, `back=max(0,-cos az)`, `right=max(0,sin az)`, `left=max(0,-sin az)`，
  除以总和归一化。权重 = `ratio * (az_weight + az_extra*extra_master) * az_gate`。
- `az_gate = clamp((1-|pos_y|)/(1-0.9))`：|pos_y|>0.9（虫瞰/鸟瞰）时方位权重衰减到 0。
- `pos_y` 高度：3 档阈值 >0.2 俯视 / >=-0.2 平视 / 否则仰视；
  权重 = `|pos_y| * mult + extra_master*el_extra`，`mult = 15 if 平视 else 10`（eye 档故意给底噪权重）。
- `pos_z` 距离：6 档阈值 `>0.7 ecu / >0.2 cu / >0 medium / >-0.35 cowboy / >=-0.7 full / else wide`，
  权重 = `1 + extra_master*di_extra`。**注意：距离档位之间权重是平的，只有档位跳变**。
- `roll`：>0 才输出 dutch angle。
- 权重钳制 `[weight_min=0.1, weight_max] `，xy_mult_enabled 时上限抬到 `max(wmax, xy_mult)`（默认 10）。
- 输出格式 `(from front:10.00), (medium shot:1.00)`。

**致命限制（就是移植到 SDXL 必须解决的）**：
1. 权重直接给到 10（A1111 语法），Anima/Illustrious 这种 danbooru 原生模型还能吃，
   SDXL 系（含 Illustrious SDXL）在 >1.6 就出噪点/贴图/畸形。必须做权重压缩曲线。
2. 词表是 danbooru 惯用语（from front / facing left），SDXL 对 `from front` 理解弱、
   且 `facing left` 在 SDXL 里会被读成"人物朝向左边"，语义容易反。要换成摄影术语。
3. 方位只有 4 个离散方向，没有 45°/135° 斜侧，SDXL 上换角度只能跳档。
4. 没有负向提示词。SDXL 上相机控制"听话"的关键恰恰是**正向 + 反向镜像压制**。

## 2. SDXL 适配决定（必须照此实现）

### 2.1 权重曲线 `sdxl_weight`
原始 raw 权重范围 [0.1, 10]，1.0 = 中性。压缩到 SDXL 友好区间：

```python
def sdxl_weight(raw, strength=1.0, curve="tanh"):
    raw = max(1e-3, float(raw))
    if curve == "raw":                    # 直通，调试用
        return round(max(0.05, min(2.0, raw)), 2)
    if curve == "linear":                 # 线性截断
        return round(max(0.05, min(2.0, 1.0 + 0.06 * (raw - 1.0) * strength)), 2)
    S = 0.55 * float(strength)
    K = 3.3
    if raw >= 1.0:
        w = 1.0 + S * (raw - 1.0) / (raw - 1.0 + K)
    else:
        w = 1.0 - S * (1.0 - raw) / ((1.0 - raw) + K)
    return round(max(0.05, min(2.0, w)), 2)
```
校验值（strength=1, tanh）：raw=10→1.40，5→1.30，2→1.13，1→1.00，0.5→0.92，0.1→0.88。

### 2.2 输出格式
- `|w-1.00| < 0.005` → 输出裸短语（不加括号权重），保持 CLIP 干净。
- 否则 `(phrase:{w:.2f})`。
- 短语内部可以用逗号拆成多个 tag（沿用原仓库 `_split_tags` 行为，逐个加权重）。

### 2.3 方位：8 方向 + 线性双档混合（默认）
8 个方向中心角 φk = k*45°，k=0..7，标签顺序（相机方位，逆时针，pos_x>0 = 相机移向角色右侧）：
```
k=0  front        (pos_x=0)
k=1  front_right  (pos_x=+0.125 附近)  # 相机在右前方
k=2  right        (pos_x=+0.25)        # 正右侧
k=3  back_right
k=4  back         (pos_x=±0.5 → 180°)
k=5  back_left
k=6  left
k=7  front_left
```
等等——**修正**：原仓库 pos_x 步长 0.05，0.5 = 90°=右侧，±1 = 背面。
即 `θ = pos_x * π`，pos_x=0.5 → θ=90°。所以 pos_x 与角度是 2 倍关系，pos_x∈[-1,1] → θ∈[-π,π]。
**精确分档公式（照抄）**：`θ = clamp(pos_x,-1,1) * π`（∈[-π,π]），
`f = (θ + π) / (π/4)` → 0..8；`i0 = int(f) % 8`；`frac = f - i0`；
权重 `(1-frac)` 给 bin i0、`frac` 给 bin `(i0+1) % 8`。
bin j 的中心角 `θ_j = -π + j*π/4`：j=0→-180(back) 1→-135(back_left) 2→-90(left) 3→-45(front_left)
4→0(front) 5→+45(front_right) 6→+90(right) 7→+135(back_right)。
校验：pos_x=0 → i0=4 front；0.5 → f=6 → right；-0.5 → left；1 → f=8 → i0=0 back（接缝连续）。
单向贡献 < `deadzone_ratio`(默认 0.06) 的丢弃。
`azimuth_mode = "8bin" | "4bin"`（4bin = 完全沿用原仓库 cos/sin 分解，用于对照）。

### 2.4 方位门控与高度
- `az_gate = clamp((1-|pos_y|)/(1-0.9), 0, 1)`（沿用）。
- 高度 3 档 + 可选 5 档（`elevation_mode="3bin"|"5bin"`，5 档在 |pos_y|>0.75 时启用 bird/worm）。
  两种模式在 `|pos_y| > POLE_LATCH(0.9)` 都锁存成 bird / worm——拖到端点必须出真顶视/贴地，否则 3bin 只给
  `high angle shot`，出图是「稍高的平视」。锁存用严格大于，0.9 本身仍是 high/low。
- 高度 raw 权重：`|pos_y| * 10 + extra`；**平视档 raw 固定 1.0**（原仓库给 10 是 danbooru 时代的 hack，SDXL 不要）。
- 距离 6 档沿用阈值，但**档内做连续插值**：`raw = 1 + 9*|pos_z - z_center(bin)| ...` 不需要，保持档位跳变，
  默认 raw = 1.0 + 0.5*|pos_z| 让极端档稍强，strength 可调。
- roll>0 → dutch angle，raw = 1 + 6*roll。

### 2.5 词表（SDXL 摄影术语）——见 docs/sdxl_camera_mapping.md，core 里必须内置同一份默认值
正向（示例，research 可补充但键名不许变）：
```
front:       "front view, straight-on shot, looking at viewer"
front_right: "three-quarter view from the right"
right:       "side view, profile view from the right"
back_right:  "three-quarter rear view from the right"
back:        "from behind, back view, seen from behind"
back_left:   "three-quarter rear view from the left"
left:        "side view, profile view from the left"
front_left:  "three-quarter view from the left"
high:        "high angle shot, from above"
eye:         "eye level shot"
low:         "low angle shot, from below, looking up"
bird:        "bird's-eye view, top-down view, aerial shot, from above"
worm:        "worm's-eye view, extreme low angle, from below"
# 注意：极点词只描述**机位**，不含 looking down/up at viewer —— 视线由 POLE_GAZE 单独改写，
# 否则同一段里 "looking down at viewer"（机位）与 "looking up at the viewer"（视线）互相打架。
ecu:         "extreme close-up, macro shot"
cu:          "close-up, portrait shot"
medium:      "medium shot, waist up"
cowboy:      "cowboy shot, mid-thigh up"
full:        "full body shot"
wide:        "wide shot, long shot, full figure in frame"
dutch:       "dutch angle, canted framing, tilted horizon"
```
负向镜像（每个轴取**相反方向**1~2 个短语，权重固定 `(-:1.15)` 或裸写，由 `neg_strength` 控制 0=关闭）：
```
front ↔ "from behind, back view, seen from behind"
back  ↔ "front view, straight-on shot"
right ↔ "side view from the left, profile from the left"
left  ↔ "side view from the right"
high  ↔ "low angle shot, from below, looking up"
low   ↔ "high angle shot, from above"
eye   ↔ "high angle, low angle"
ecu   ↔ "wide shot, full body"
cu    ↔ "wide shot, full body, distant"
medium↔ "close-up, extreme close-up"
cowboy↔ "close-up, extreme close-up"
full  ↔ "close-up, portrait shot"
wide  ↔ "close-up, portrait shot"
dutch ↔ "horizon level, straight horizon"
```

### 2.6 节点契约（UI 与后端必须一致）
```
CLASS = "CameraAngleSDXL"      DISPLAY = "SDXL 相机控制"
CATEGORY = "Anima/相机"
RETURN_TYPES = ("STRING", "STRING", "STRING")
RETURN_NAMES = ("positive", "negative", "camera_info")

widget order (INPUT_TYPES required 里的顺序):
 pos_x  FLOAT 0.0 [-1,1] step .05   "方位角"
 pos_y  FLOAT 0.0 [-1,1] step .01   "高度"
 pos_z  FLOAT 0.0 [-1,1] step .05   "距离"
 roll   FLOAT 0.0 [0,1] step .10    "倾斜"
 preset COMBO ["自定义", "正面平视中景", "正面仰视全身", "正面俯视中景", "右侧90度中景",
               "背面全身", "左前方45度近景", "右后方45度中景", "虫瞰极限仰视", "鸟瞰极限俯视",
               "荷兰角倾斜中景", "特写面部"] 默认 "自定义"
 strength FLOAT 1.0 [0,2] step .05  "强度"
 weight_mode COMBO ["tanh","linear","raw"] 默认 "tanh"
 azimuth_mode COMBO ["8bin","4bin"] 默认 "8bin"
 elevation_mode COMBO ["3bin","5bin"] 默认 "3bin"
 neg_strength FLOAT 1.0 [0,2] step .05 "负向强度"
 extra_positive STRING "" (multiline)
 extra_negative STRING "" (multiline)
 _cfg_json STRING "{}"  (隐藏，兼容标签覆盖)
```
preset != "自定义" 时用预设姿态覆盖 pos_*，滑块值忽略（但保留在 UI 上）。
`camera_info` 输出 JSON：`{"azimuth_deg":..,"elev_deg":..,"bin":"right","parts":[[phrase,w],...],"preset":..}`。

### 2.7 目录
```
D:/QUARK/sdxl_camera_angle/
  __init__.py
  core/camera_core.py        # 纯函数，零 ComfyUI 依赖，可单测
  core/presets.py
  nodes/camera_sdxl_node.py  # ComfyUI 节点壳
  js/sdxl_camera_widget.js   # 3D 手动调摄像机 UI
  docs/sdxl_camera_mapping.md
  tests/test_core.py
  README.md
```
`camera_core.py` 必须能在无 ComfyUI 环境下 `python -c "import" ` 通过（sys.path 加 core 目录即可）。

## 3. 验收
- `python -c "from camera_core import compute_camera; print(compute_camera(0,0,0,0))"` → front + eye + medium，权重全部在 [0.05,1.4]。
- 方位扫描 pos_x ∈ {-1,-0.75,...,1} 时正/负向短语按 45° 正确切换，斜侧出现双档。
- 同一 seed 下不同方位出图，方向必须肉眼可见地变（Lead 负责出图验证）。

## 7. v2 扩展：光学 / 构图 / 主体朝向

四个新维度全部在 core/camera_core.py 里映射，js/camera_shared.js 是镜像实现（parity_check 校验）。

### 7.1 焦距 focal（等效 36mm 全画幅，mm）
fov_deg = 2*atan(36/(2*focal)) * 180/pi。分档（左闭右开，超出取端档）：

| focal | key | 原始权重 raw | tanh 后 |
|---|---|---|---|
| <=18 | lens14 | 5.0 | 1.30 |
| <=29 | lens24 | 3.2 | 1.22 |
| <=43 | lens35 | 1.8 | 1.11 |
| <=65 | lens50 | 1.0 | 1.00（裸写） |
| <=105 | lens85 | 4.0 | 1.26 |
| >105 | lens135 | 5.0 | 1.30 |

### 7.2 翻滚 roll in [-1,1]（±1 = ±45°）
|roll| > 0.25 才出词：raw = 1 + 6*|roll|；正值 → dutch（顺时针倾斜），负值 → dutch_neg（反向倾斜）。
camera_info.roll_deg = roll*45。

### 7.3 构图偏移 pan_x / pan_y in [-1,1]（取景中心解耦）
- pan_x > 0 = 相机右移 → 主体落到画面左侧三分线 → subject_left；< 0 → subject_right。
- pan_y > 0 = 机位上抬 → 主体压到画面下方（上方留白）→ subject_low；< 0 → subject_high。
- 两者 |v| < 0.12 → subject_center（裸写）。
- 权重 raw = 1 + 2.0*|v|（slope 可 cfg 覆盖）。

### 7.4 主体朝向 subject_yaw in [-1,1] + gaze_mode in {auto,camera,away}
- `subject_yaw` 是**世界坐标系**朝向（0 = 面向世界 +Z，±1 = 背向），与 `studio/web/camera3d.js`
  的朝向箭头同一套角度；`pos_x` 是相机方位（0 正面、±1 背面）。判定前先求相对角：
  `rel_yaw = wrap(subject_yaw - pos_x)`，`wrap(v) = ((v + 1) mod 2) - 1`。
- |rel_yaw| < 0.28 → subject_front（面向镜头）；< 0.72 → subject_turn（三分之四背转）；>= 0.72 → subject_away。
- 去重：`subject_front` 且机位正面扇区 > 0.5 → 不发（与 front view 重复）；
  `subject_away` 且机位背面扇区 > 0.45 → 不发（与 from behind / back view 重复）。
- 视线：gaze_mode=camera → gaze_viewer；away → gaze_away；auto 时
  subject_front → viewer；机位在背面（back 系 bins 权重和 > 0.45）且 |rel_yaw| <= 0.72 →
  gaze_over_shoulder（回眸）；其余 → away。|pos_y| > 0.75 时 gaze_viewer 改写为 POLE_GAZE。
- 视线词裸写（权重 1.00，回眸 raw 1.30），朝向词 raw = 1 + 2.0*|rel_yaw|。
- 例：pos_x=1.0 + subject_yaw=0.0 → rel=-1 → subject_away（去重不发词）+ gaze_away，
  **不再出现 front-facing body / looking at viewer**；pos_x=1.0 + subject_yaw=0.4 → rel=-0.6 → turn + 回眸。

### 7.5 CLIP token 预算（相机段）

- `clip_tokens(text)`：零依赖估算。剥掉 `(tag:1.40)` 权重语法 → 按逗号切段 → 按空白切词 →
  每词 `ceil(len*2/9)`（≈4.5 字符/token）。误差 ±10%，用于实时提示，不参与出图。
- `token_budget(positive, negative)` → `{positive, negative, total, limit:75, warn:65, level}`；
  `level = ok | warn(>65) | danger(>75)`。75 是 Chunk 0 的内容 token 上限（77 − 起止符）。
- 同步契约：`js/camera_shared.js` 导出 `CLIP_TOK_LIMIT/CLIP_TOK_WARN/plainTags/clipTokens/budgetOf`，
  `tools/parity_check.py` 比对阈值与函数存在性。

## 8. v2 极点消歧

POLE_START = 0.75、POLE_DEG = 75、POLE_NORM = 75/90 = 0.8333、AZ_SUPPRESS = 0.12。

- pole_gate(y) = clamp((1-|y|)/(1-0.75), 0, 1)（0.75 处 1，1.0 处 0）。
- 有效方位系数 gate = az_gate(y) * pole_gate(y)；gate < 0.12 时**整段方位词被丢弃**，
  camera_info.azimuth_suppressed = true（|pos_y| 约 0.94 以上必触发）。
- `|pos_y| > 0.9` 时高度档位锁存成 bird/worm（`POLE_LATCH`，3bin/5bin 都适用）；
- |pos_y| > 0.75 时按高度档位剔除方位/视线里的冲突 token（POLE_BLOCK_TOKENS）：
  - bird: front view / straight-on shot / facing the camera / eye level shot / looking at viewer / direct eye contact /
    from behind / seen from behind / back view
  - worm: eye level shot / top-down view / looking down at viewer / from above / high angle / bird's-eye view
  - high / low: eye level shot
- 极点视线改写 `POLE_GAZE`（按 **py 正负**判定，不按 ekey）：
  py > 0.75 → "looking up at the viewer"；py < -0.75 → "looking down at the viewer"。
  理由：竖直机位下发 "looking at viewer" 会画成正面站立，实测 py=0.95 出的是背面回眸站姿。
- 结果：pos_y=1, elevation_mode=5bin → bird's-eye view, top-down view, aerial shot, from above + 距离 + 光学 + 构图
  + looking up at the viewer，**不再出现 from behind**。

## 9. v2 camera_info 增补字段

focal, fov_deg, lens, roll_deg, pole_gate, azimuth_suppressed, behind_ratio, framing[], subject, gaze,
subject_yaw（世界系）, rel_yaw（= wrap(subject_yaw - pos_x)）, pan[2], tokens{positive,negative,total,limit,warn,level}；
parts[] 每项 {kind, key, raw, weight, phrase, tokens[], note}，kind in {az, el, di, lens, tg, frame, subject, gaze}。

## 10. v2.3 工程化与 ControlNet 联动

### 10.1 双端共享 3D 核心 `js/camera_rig.js`

Studio 的 `studio/web/camera3d.js` 与 ComfyUI 节点卡片里的 `js/sdxl_camera_widget.js` 曾经各维护一套 THREE 场景，
任何几何改动都得改两遍。v2.3 起场景只剩一份：`CameraRig`。

```js
const rig = new CameraRig(canvas, { compact: false, interactive: true,
                                    onChange: s => {}, onCommit: s => {} });
```

| 方法 | 作用 |
|---|---|
| `setState(dict)` / `getState()` | 与 compute_camera 同名的 8+ 个参数（pos_x/pos_y/pos_z/roll/focal/pan_x/pan_y/subject_yaw/gaze_mode） |
| `applyShot(n)` | 1..6 标准机位（正/背/左正侧/右正侧/高/低） |
| `toggleGrid(bool)` / `toggleFrustum(bool)` | 三分法九宫格 / 动态视锥线框 |
| `setRefImage(url)` / `setRefOpacity(v)` | 参考图叠层 |
| `resize()` / `dispose()` | 尺寸同步 / 释放 geometry、material、renderer |
| `renderDepthDataURL(w, h, {invert})` | 离屏深度图（`MeshDepthMaterial` + `BasicDepthPacking`；该 packing 原生是**近 0 黑 / 远 1 白**，`invert:true`（默认）在 2D 画布上取反后才是**白=近、黑=远**；背景填远端 `0xffffff`，不会被当成贴脸点） |
| `renderPoseDataURL(w, h)` | 离屏 OpenPose 18 点骨架（黑底、右红左蓝中黄） |

`compact: true` 时只用 canvas 自身尺寸（缺省 320×300）、不挂 document 级键盘监听、不读任何 DOM id，
因此可以直接塞进 ComfyUI 节点卡片；Studio 侧把快捷键与滑块绑定留在 `camera3d.js` 薄壳里。
渲染仍是按需的：状态变化 `invalidate()` 才画一帧，静止时 0 帧，离屏导出不会改动当前 viewport。

### 10.2 ControlNet 深度 / 骨架注入

3D 视口本身就是「这张图应该长什么样」的几何真值，所以可以直接拿它当 ControlNet 的控制图：

1. 前端 `renderDepthDataURL(512, 768)` → `data:image/png;base64,…`；
2. `POST /api/controlnet/upload {kind, data_url}` → 后端解码后经 ComfyUI `POST /upload/image` 上传到 `input/`；
3. 生成时把返回的 `filename` 放进 `template_vars.controlnet_depth`（或 `controlnet_pose`）；
4. `template_bridge.build` 找到模板里的 `ControlNetApplyAdvanced.image` 输入，改写成该 filename（若是链接则插入 `LoadImage` 再改链，不动其它结构）。

深度图口径：**近=白、远=黑**（`MeshDepthMaterial` + `BasicDepthPacking` 原生是近 0 黑 / 远 1 白，导出时在 2D 画布上取反；背景填远端 `0xffffff`，否则空景会被标成贴脸最近点），与 `control_v11f1p_sd15_depth` / `controlnet-union-sdxl` 的输入一致；
导出相机按主体包围盒**自动贴框**（`_cameraFor(aspect, fit=true)`：主体盒 8 角投到相机屏幕轴取 `halfW/halfH`，`dist = max(halfH/tan(vHalf), halfW/tan(hHalf)) / EXPORT_FIT_MARGIN(0.92)`，两轴取大值所以横幅也不裁切），depth 与 pose 复用同一台 cam 实例，轮廓天然对齐；
本机可用的控制模型是 `controlnet-union-sdxl-1.0.safetensors`（union 同时覆盖 depth 与 openpose），示例模板见 `workflows/SDXL.CameraAngle.ControlNet.json`：
该模板有**两个** `ControlNetApplyAdvanced` 槽位——节点 10 吃 depth（`strength 0.65` / `end_percent 0.85`），节点 13 吃 pose（`strength 0.45` / `end_percent 0.6`，串在 10 的下游），sniff 按节点 id 顺序分配 `controlnet_depth` → 10、`controlnet_pose` → 13；只给一个变量时另一个槽位保持作者填的占位图。
骨架图口径：18 点（0 鼻 1 颈 2 右肩 3 右肘 4 右腕 5 左肩 6 左肘 7 左腕 8 右髋 9 右膝 10 右踝 11 左髋 12 左膝 13 左踝 14 右眼 15 左眼 16 右耳 17 左耳），
配色沿用 OpenPose 官方（右 `#ff0000`、左 `#0000ff`、中 `#ffff00`），ControlNet openpose 直接可吃。

### 10.3 任意工作流嗅探与多机位批量

`template_bridge.inspect(ref)` 只读地报出「这条工作流会被注入哪几个节点」：

| 槽位 | 嗅探规则 |
|---|---|
| `camera` | 存在 `CameraAngleSDXL` 节点时优先，直接写它的 positive/negative 输出后的 CLIPTextEncode |
| `positive` / `negative` | `vars.prompt` 绑定节点；否则从 `KSampler.positive/negative` 连线回溯 `CLIPTextEncode*` |
| `sampler` / `latent` / `save` | 按 class_type 找 `KSampler*` / `Empty*LatentImage` / `SaveImage` |
| `controlnet.depth` / `.pose` | `ControlNetApplyAdvanced.image` 的上游（`LoadImage` 或直接值） |

注入永远是「只改 inputs」：不新增/删除节点（除非 ControlNet 槽位是链接，需要补一个 `LoadImage`），不改 class_type，不覆盖作者写的负向原文（`negative_mode="replace"` 才覆盖）。
无法识别 UI 格式工程文件时返回明确错误，而不是猜。

批量队列 `POST /api/batch` 用同一份 base 参数与同一 seed 依次提交多个机位（三视图 / 环绕 8 机位 / 推拉 / 俯仰），
`GET /api/batch?batch_id=` 聚合每张的 state/progress/images，Studio 用网格并排对比；ComfyUI 离线时整单 503，不会半提交。

