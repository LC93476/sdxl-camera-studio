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
bird:        "from above, high angle, looking down at viewer, top-down view"
worm:        "from below, low angle, looking up at viewer"
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
