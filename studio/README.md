# SDXL 相机工作台 · 使用文档

本地 WebUI 桌面工作台：3D 里拖摄像机 → 实时生成 SDXL 正/负向提示词 → 一键提交本机 ComfyUI 出图 → 画廊回显。
它不是 LoRA、不是 ControlNet，是一台「相机参数 → 带权重的英文摄影词」的文本编译器。

提示词**由服务端 `core/camera_core.py` 的 `compute_camera` 计算**，`/api/preview` 与 `/api/generate` 走同一个函数，
所以预览看到的字符串就是实际送进 CLIPTextEncode 的字符串。工作台**不依赖 ComfyUI 是否装了插件节点**：
图结构由 `studio/comfy_bridge.py` 现场拼（CheckpointLoaderSimple → 可选 LoraLoader → 两个 CLIPTextEncode →
EmptyLatentImage → KSampler → VAEDecode → SaveImage），也不读 `SDXL.CameraAngle.json`。
`nodes/camera_sdxl_node.py` 那个 ComfyUI 节点壳仍可单独在 ComfyUI 里用，两者共用同一份核心算法。

## 1. 启动

### 1.1 桌面快捷方式（推荐，无控制台）

双击桌面 **「SDXL 相机工作台」**。快捷方式目标是 `wscript.exe`，参数指向 `launch.vbs`，VBS 以隐藏窗口调用
`launch.bat`，全程没有控制台。默认行为是**在系统默认浏览器里开一个标签页**（最稳，绝不会和系统里已有的
浏览器实例打架）；想用无浏览器边框的独立窗口就加 `--window`（见 1.2 与 FAQ「独立窗口没起来」）。快捷方式丢了就重新生成：

```powershell
powershell -ExecutionPolicy Bypass -File D:\QUARK\sdxl_camera_angle\studio\make_shortcut.ps1
```

### 1.2 launch.bat（有控制台，排错用）

```bat
D:\QUARK\sdxl_camera_angle\studio\launch.bat
```

控制台保留，能看到 `[launch]` / `[studio]` 全部输出；`launch.py` 返回非 0 时窗口 `pause`，不会一闪而过。
参数透传，例如 `launch.bat --port 8790 --no-window`。

### 1.3 手工起服务（最可控）

```bat
D:\QUARK\ComfyUI-aki-v3.2\python\python.exe D:\QUARK\sdxl_camera_angle\studio\server.py --port 8770 --no-browser
```

然后浏览器打开 http://127.0.0.1:8770/ 。去掉 `--no-browser` 时服务会自己调默认浏览器。
`server.py` 参数：`--port`（默认 8770）、`--host`（默认 127.0.0.1）、`--comfy`（默认 http://127.0.0.1:8188）、
`--no-browser`、`--quiet`。启动后**第一行固定**打印 `[studio] http://127.0.0.1:8770`，
后面还有 comfy 在线/离线、web / out / state 三个目录各一行。

### 1.4 端口规则

- `launch.py` 从 **8770** 起往上找空闲端口，最多试 40 个（8770–8809）；`--port N` 可指定起点。
- 实际端口看控制台首行 `[studio] http://127.0.0.1:PORT`。快捷方式看不到控制台时先试默认 8770
  （只有 8770 被占才会漂移，此时 `[launch] cmd:` 那行里也带着端口）。
- `launch.py` 会等 `/api/health` 变 ok（最多 45s），然后打开页面：**默认给默认浏览器开一个标签页**，
  `--window` 时才用 Edge/Chrome 的 `--app` 开独立窗口，找不到浏览器一律退回标签页。
  `--window` 下窗口关掉后它检测到浏览器进程消失会自动结束服务；标签页模式下服务一直跑到 Ctrl+C。

## 2. 前置条件

1. **ComfyUI 必须跑着**，默认 `http://127.0.0.1:8188`（用 `--comfy` 或 `state/config.json` 的 `comfy_url` 改）。
   离线时页面**照常可用**：能拖摄像机、看提示词、看 token 徽标，只是出不了图——右上角状态点变红显示
   「未连接 ComfyUI」，checkpoint 下拉只有一项「未连接 ComfyUI」，点「生成图片」得到 503 `{"error":"ComfyUI 未启动"}`。
2. **Python 用 ComfyUI 自带的那只**：`D:\QUARK\ComfyUI-aki-v3.2\python\python.exe`（3.13，带 PIL）。
   `launch.py` 先按相对路径猜 `ComfyUI-aki-v3.2/python/python.exe`，再退回 `sys.executable`。
3. 后端只用标准库，前端只用原生 ES module + 本地 three.js（`web/vendor/three.module.js`），
   **完全离线可用**，不装任何 pip 包，也不走 CDN。

## 3. 界面说明

### 3.1 左栏 · 3D 视图

canvas `#view3d`，球体 + 人偶参考，三个手柄：

| 操作 | 结果 |
|---|---|
| 左键拖空白处 | 旋转**场景**观察角（不改相机参数） |
| Shift+左键拖 / 右键拖 | 平移场景 |
| 拖**粉点** | 改方位 `pos_x`（绕人转） |
| 拖**青点** | 改高度 `pos_y`（升降） |
| 滚轮 | 改距离 `pos_z`（向**上**滚 = 靠近，`pos_z` 变大） |

3D 里 `pos_y` 的极值仰角是 78°（`pos_y = ±1` 时），`pos_z` 映射到 2.62–7.2 的相机距离。

### 3.2 左栏 · 相机参数

滑块与 3D 双向同步，任何改动都会 **200ms 防抖**后重算提示词（不刷新页面）：

- `方位 pos_x` / `高度 pos_y` / `距离 pos_z`：−1…1，步长 0.01；`倾斜 roll`：0…1，步长 0.01
- `强度 strength`：0…2 默认 1.00；`负向强度 neg_strength`：0…2 默认 1.00
- 三个枚举：`权重曲线 weight_mode`（tanh / linear / raw）、`方位精度 azimuth_mode`（8bin / 4bin）、
  `高度精度 elevation_mode`（3bin / 5bin）
- 两个文本框：`额外正向词`、`额外负向词`（原样拼到末尾，**不经过权重处理**）

拖动任意滑块会把预设下拉自动打回「自定义」。

### 3.3 右栏 · 提示词

- **正向 / 负向提示词**两块，各带「复制」按钮（`navigator.clipboard`，失败退回 `execCommand`）。
- 中间一行**相机信息**：「正面72% / 右前方28% · 平视 · 中景 · 方位角 12.3° · 仰角 0°」；方位被门控压制时补
  「方位闸门 0.8」，预览返回的 `warn` 以 `⚠ …` 追加在同一行。
- 右侧 **token 徽标**显示 `P 34 · N 26`，是**估算值** `round(len(text)/3.6)`，不是真实 tokenizer 结果
  （`warn` 的第一条永远在提醒这件事）。

### 3.4 右栏 · 手动附加

`手动正向` / `手动负向` 两个多行框，非空时以 `", "` 追加到对应提示词**最末尾**，与服务端算出的词分开管理
（适合常驻的 `masterpiece, best quality`）。

### 3.5 右栏 · 生成设置

`seed`（−1 = 随机）、`随机 seed` 勾选框、`steps`（28）、`cfg`（5.0）、`宽度` / `高度`（服务端默认 832×1216）、
`checkpoint` 下拉、`lora` 下拉（首项「（不使用 LoRA）」）、`lora 强度` 滑块。下拉内容来自
`GET /api/comfy/models`（读 ComfyUI 的 `/object_info`），离线时为空并显示「未连接 ComfyUI」；
卡片右上角徽标在线显示「ComfyUI 在线」，离线「未连接 ComfyUI」。

> `sampler` / `scheduler` / `batch` / `save_prefix` 界面上没有控件，但它们是 `/api/config` 的字段
> （默认 `euler_ancestral` / `karras` / 1 / `camera`），可直接 POST `/api/config` 改。

再加两个下拉 / 输入（第 5b 章详解）：

- **工作流模板**：首项「（内置最小图：checkpoint + LoRA 直出）」，下面是自动扫到的
  `~/.dsh-comfyui-workbench/workflows/*.json`（ComfyUI 的 API 格式工作流）。选中后**不再用内置最小图**，
  改为「把相机提示词注入这套工作流」出图 —— 也就是你自己的生产工作流（绯月三步 detailer、SAM3 部位重绘、
  姿势库标签……）可以直接吃到工作台的相机词。
- **姿势 id**：模板里 `pose` 变量的值（形如 `full_sexy_P001_front`），留空用模板自带默认值。

### 3.6 右栏 · 生成与画廊

- 「生成图片」：`POST /api/generate`，按钮变「生成中…」并禁用；提交成功后**每 700ms** 轮询 `/api/progress`。
  状态行依次「正在提交…」→「已提交 · seed N · 等待 ComfyUI…」→「排队中 · 前面 N 个任务…」→
  「生成中 42% · KSampler · 18s」→「完成 · 1 张 · seed N」。
- 「中断」：`POST /api/interrupt`，转发 `/interrupt` 给 ComfyUI，并把本地 running/queued 任务标成 error。
- 进度条 0–100，完成回填 100%。
- **画廊**：完成时把 `images[]` 插到最前，每张带 `seed N` 角标，徽标显示总张数；启动时用 `GET /api/gallery` 填充
  （out 目录 mtime 倒序）。
- **大图**：点缩略图 → lightbox。滚轮缩放 1×–6×、`←` / `→` 切换、`Esc` 或点背景关闭，底部显示「3 / 12 · seed 123456」。
- 「清空」：`POST /api/gallery/clear`，删 out 里的图片与 `.json` sidecar（其它文件不动）。
- 「输出目录」：`POST /api/open_folder`，用资源管理器打开 `studio/out`。

## 4. 参数语义

`pos_x` / `pos_y` / `pos_z` 钳制到 [−1,1]，`roll` 到 [0,1]，`strength` / `neg_strength` 到 [0,2]。

| 参数 | 语义 |
|---|---|
| `pos_x` | 方位。极角 `az = pos_x * π`：`-1` 正后方 → `-0.5` 左侧 → `0` 正面 → `+0.5` 右侧 → `+1` 正后方另一侧。8bin 在相邻两档间**线性双档插值**（两档权重和为 1），占比 < 6% 的档丢弃 |
| `pos_y` | 高度。`>0.2` 俯视 high、`[-0.2,0.2]` 平视 eye、`<-0.2` 仰视 low；5bin 时 `>0.75` 改 bird、`<-0.75` 改 worm。**`|pos_y|>0.9` 起方位词被门控衰减，±1 时完全关闭**（`az_gate=(1-|y|)/0.1`，钳到 [0,1]） |
| `pos_z` | 距离。`>0.7` 特写 ecu、`>0.2` 近景 cu、`>=0` 中景 medium、`>-0.35` 牛仔 cowboy、`>=-0.7` 全身 full、其余远景 wide。**档内权重还随 `|pos_z|` 连续变化**（`1+0.5*|z|`） |
| `roll` | 倾斜。**只有 `>0` 才输出** dutch angle，权重 `1+6*roll` |
| `strength` | 权重曲线强度，0 = 全部压成中性裸词，默认 1.0。tanh 下方位满权重（raw 10）约压到 **1.40**；`raw` 模式会被硬钳到 2.0。**>1.5 基本不用** |
| `neg_strength` | 负向强度，0 = 负向输出空串。负向权重恒为 `1+0.15*neg_strength`（与方向、大小无关） |
| `weight_mode` | `tanh`：`1 ± 0.55*strength*(x/(x+3.3))`，越极端越饱和，推荐；`linear`：`1+0.06*(raw-1)*strength`，线性但很快撞上限；`raw`：直通原值，**仍钳到 [0.05, 2.0]**，只作对比调试 |
| `azimuth_mode` | `8bin` 八方向双档插值；`4bin` 还原原始 cos/sin 四向分解（front/back/left/right） |
| `elevation_mode` | `3bin` high/eye/low；`5bin` 在 `|pos_y|>0.75` 额外插入 bird/worm |

短语按逗号拆成多个 tag **分别**加权，写成 `(from front:1.40), (eye level shot)` 这种 A1111 语法；
`|w-1| < 0.005` 时输出裸短语。平视 eye 的 raw 权重固定 1.0，所以正常情况下它就是个裸词。

`camera_info` 字段（v2）：`azimuth_deg`（`pos_x*180`）、`elev_deg`（`pos_y*90`）、`bin`、
`bins`（**三元组** `[key, ratio, kind]`，kind ∈ az/el/di/lens/tg/frame/subject/gaze）、`elevation`、`distance`、
`dutch`、`roll_deg`、`fov_deg`、`focal`、`lens`、`az_gate`、`pole_gate`、`azimuth_suppressed`、`behind_ratio`、
`framing`、`subject`、`gaze`、`subject_yaw`、`pan`、`preset`、`strength`、`weight_mode`、`azimuth_mode`、
`elevation_mode`、`neg_strength`、`parts`、`negative_parts`。`parts[]` 每项带 `kind/key/ratio/raw/weight/phrase/tokens`。

## 5. 预设

内置 **16 个**（`core/presets.py` 的 `PRESETS` + `PRESET_EXTRAS`），下拉首项「自定义」（`PRESET_NAMES` 共 17 项）：

| 预设 | (pos_x, pos_y, pos_z, roll) | 附加参数 | 实际档位 |
|---|---|---|---|
| 正面平视中景 | (0, 0, 0, 0) | — | front + eye + medium |
| 正面仰视全身 | (0, −0.55, −0.5, 0) | — | front + low + full |
| 正面俯视中景 | (0, 0.55, 0, 0) | — | front + high + medium |
| 右侧90度中景 | (0.5, 0, 0, 0) | — | right + eye + medium |
| 背面全身 | (1, 0, −0.5, 0) | — | back + eye + full |
| 左前方45度近景 | (−0.25, 0, 0.35, 0) | — | front_left + eye + cu |
| 右后方45度中景 | (0.75, 0, 0, 0) | — | back_right + eye + medium |
| 虫瞰极限仰视 | (0, −1, −0.85, 0) | `elevation_mode=5bin, strength=0.7` | worm + wide，方位关闭 |
| 鸟瞰极限俯视 | (0, 1, −0.85, 0) | `elevation_mode=5bin, strength=0.7` | bird + wide，方位关闭 |
| 荷兰角倾斜中景 | (0, 0, 0, 0.45) | — | front + eye + medium + dutch |
| 特写面部 | (0, 0, 0.85, 0) | — | front + eye + ecu |
| 长焦人像85mm | (0, 0, 0.1, 0) | `focal=85` | 85mm + 浅景深压缩 |
| 广角环境24mm | (0, 0, −0.5, 0) | `focal=24` | 24mm + 环境叙事 |
| 三分法偏右 | (0, 0, 0, 0) | `focal=35, pan_x=−0.35` | 主体落右三分线 |
| 回眸背面机位 | (1, 0, 0.1, 0) | `focal=85, subject_yaw=−0.45, gaze_mode=camera` | back + 3/4 侧身 + 回眸 |
| 荷兰角负斜 | (0, 0, 0, −0.45) | `focal=50` | 反向倾斜 −20° |

- **鸟瞰 / 虫瞰自动切档**：`PRESET_MODES` 把这两个预设的 `elevation_mode` 强制成 `5bin`、`strength` 压到 `0.7`
  （极端俯仰在 Illustrious 系上吃重权重会糊成贴图）。前端选预设时同步改滑块，后端 `compute_camera` 里再覆盖一次。
- 选预设会**覆盖机位与光学参数**（`pos_*/roll/focal/pan_*/subject_yaw/gaze_mode`）；再动任意滑块就回到「自定义」。
  覆盖逻辑在 `core/camera_core.py` 的 `compute_camera` 开头（先做 `preset_params` 成员判断再覆盖，未知预设名不动滑块）。
- **「保存预设」**把当前 17 键参数（位置 + 光学 + 档位 + extras）存进 `ui_presets`，落盘 `state/presets.json`；
  旧版 `[pos_x, pos_y, pos_z, roll]` 四元组仍能读入，其余键补默认值（`store._coerce_preset`）。
- 用户预设与 `PRESETS` 是两套东西：后端只认 `PRESETS` 里的名字，**不认识的 preset 名会把滑块值原样使用**（不报错、不换档）。
- 预设可 **导出 / 导入**（`/api/ui_presets/export` 下载 `ui_presets.json`、`/api/ui_presets/import` 合并导入），
  删除走 `DELETE /api/ui_presets?name=…`。

## 5b. 外部工作流模板（相机词 → 生产工作流）

内置那条链路只有「checkpoint → LoRA → 两个 CLIPTextEncode → KSampler → 存图」，是最干净的验证图。
要出成品（作者那套 1200×1800 + SAM3 脸/手/脚三段重绘、姿势库标签、细节修复 LoRA……），把
**工作流模板** 指过去就行：`studio/template_bridge.py` 会读模板 JSON，只改注入点，结构一行不动。

模板从哪来：

- ComfyUI 画布右上「工作流 → 导出（API）」得到的 JSON（**API 格式**，顶层是 `{"节点id": {"class_type": …, "inputs": …}}`）；
- 放到 `C:\Users\<你>\.dsh-comfyui-workbench\workflows\`（工作台默认扫描，和 comfyui-workbench 技能共用同一个目录），
  或放到项目下的 `workflows/`；
- 同名 `.schema.json` 可选，里面的 `vars` 用来告诉工作台「哪个节点是提示词 / 尺寸 / 种子 / 步数」，
  没有 schema 就按 class 类型猜（`CLIPTextEncode` → `text`，`EmptyLatentImage` → `width`，`KSampler` → `seed`）。

注入规则（保守，**绝不动连线与节点增删**）：

| 目标 | 规则 |
|---|---|
| 正向提示词 | 写进 `vars.prompt` 绑定的节点（`text` 整体替换）；没有该变量时退化到 `KSampler.positive` 指向的 `CLIPTextEncode`，并在返回里给 `warn` |
| 反向提示词 | 写进 `KSampler.negative` 指向节点的 `text`，**默认保留作者原文 + `", "` 追加工作台否定**；`negative_mode:"replace"` 则整体覆盖 |
| 尺寸 / 种子 / 步数 / CFG | 写进 schema 里 `width`/`height`/`seed`/`steps`/`cfg` 绑定的节点（模板没绑就不动，沿用模板自己的值） |
| 模板自定义变量 | 请求体 `template_vars` 里给同名键即覆盖（例如 `{"pose": "full_sexy_P026_side"}`）；界面上的「姿势 id」等价于 `template_vars.pose` |
| 存图前缀 | 每个 `SaveImage` 的 `filename_prefix` 前面加 `<save_prefix>/`，方便在 ComfyUI 输出目录里认领 |

工作台拿图仍然走 `/history` → 下载到 `studio/out` → 写 sidecar，所以「画廊 / 大图 / seed 角标」在多 detailer、
多 SaveImage 的模板上一样能用（多张全落进画廊）。

实测参考（本机 8GB 显存，WAI-Nsfw-Illustrious-17 + 绯月 LoRA，模板 `feiyue-sam3-posetags`）：

| 尺寸 / 步数 | 耗时 | 说明 |
|---|---|---|
| 832×1216 / 20 步 | 约 212 s | 含 SAM3 检脸手脚 + 3 段 detailer，出 `832×1216` 成品 |
| 1200×1800 / 30 步 | 约 7–9 min | 作者原尺寸；8GB 卡上会长时间占满显存，建议先小图确认提示词方向 |

> 模板里的姿势标签（`PoseTagsNode`）会往正向里补「这个白模是什么姿势」的描述，**和相机方位词会打架**：
> 想要纯相机控制就把 `pose` 选成与机位同向的角度（例如 `pos_x≈+0.5` 配 `full_sexy_P026_side`），
> 或者干脆用不带姿势库的模板（`feiyue-sam3-t2i`）。

### 5c. 链路实测记录（工作台 → 模板 → 出图）

| # | 模板 / 姿势 | 尺寸 · 步数 · seed | 机位 (x, y, z, roll) | 耗时 | 结果 |
|---|---|---|---|---|---|
| 1 | `feiyue-sam3-posetags` / `full_sexy_P001_front` | 832×1216 · 20 · 777001 | 0.30, −0.28, −0.20, 0 | 212 s | 彩色；右前 3/4 侧身 + 明显仰视 + 牛仔景，相机词生效 |
| 2 | `feiyue-sam3-posetags` / `full_sexy_P026_side` | 1200×1800 · 30 · 20261108 | 0.50, 0.12, −0.45, 0 | 313 s | **黑白线稿风**：该尺寸 + 该 seed 的偶发风格漂移，同参数降到 832 重跑恢复正常 |
| 3 | `feiyue-sam3-t2i` | 1200×1800 · 30 · 20261108 | 0.50, 0.10, −0.50, 0 | 150 s | 彩色；无姿势库模板，画面干净、无姿势词干扰 |
| 4 | `feiyue-sam3-posetags` / `full_sexy_P026_side` | 832×1216 · 20 · 777001 | 0.50, 0.10, −0.50, 0 | 246 s | 彩色；坐姿 + 背影 3/4 —— **姿势参考图的朝向压过了相机方位词** |

三条结论：

1. **注入链路是通的**：正/负提示词、尺寸、步数、seed、CFG、存图前缀全部按请求体落进模板，
   多 detailer 结构原样不动，成品回画廊、seed 角标正确。
2. **姿势库模板里，姿势参考图的朝向 > 相机方位词**：`PoseTagsNode` 从白模图反推的姿势描述
   （自带朝向）比 `(side view:1.40)` 更硬。要严格控机位就选朝向与目标一致的姿势，或用 t2i 模板。
3. **偶发风格漂移**：上表第 2 行那张黑白线稿是偶发（同参数换分辨率即恢复），遇到就换 seed
   （界面勾 `randomize`）重跑，不必改提示词。
### 5d. v2 参数实测（API 图直连，832×1216 · 26 步 · cfg 5.0 · euler_ancestral/karras）

v2 新参数（focal / roll / pan / subject_yaw / gaze）不走工作台模板，用 API 格式图直连 `/prompt` 验证；
同一个 `CameraAngleSDXL` 节点换成 api 图即可（工作流 JSON 是 UI 格式，直接 POST 会 500）。

| # | 参数 | 正向串关键段 | 出图 |
|---|---|---|---|
| 1 | pos_x=1.0 pos_z=0.1 focal=85 pan_x=0.35 subject_yaw=-0.45 gaze=camera | from behind ×3, 85mm/telephoto/bokeh, subject on the left third, body turned three-quarters away | 背影 3/4 回眸 + 左侧构图 + 强浅景深，全部对上 |
| 2 | pos_x=0.5 pos_y=0.35 pos_z=0.3 focal=14 | side view from the right, high angle, ultra wide angle / strong perspective distortion | 右前上方 + 明显广角透视收敛 |
| 3 | pos_y=-0.95 focal=24（虫瞰极点） | low angle shot, from below, looking up, looking down at the viewer | 丛林里贴地仰拍、透视冲天 —— 极点消歧生效 |
| 4 | pos_y=0.95 focal=50（鸟瞰极点） | high angle shot, from above, looking up at the viewer | 高机位俯拍 + 主体抬头（改 POLE_GAZE 前同参数出的是背面站姿） |
| 5 | pos_x=0.4 roll=0.3 pan_x=-0.4（荷兰角） | dutch angle / canted framing / tilted horizon, subject on the right third | 地平线倾斜 + 主体偏右三分线 |

> 极点两条最能说明问题：竖直机位下 `looking at viewer` 会把画面拉回「正面站立」，所以 |pos_y|>0.75 时视线短语
> 改写成 `looking up/down at the viewer`（`POLE_GAZE`），配合 `POLE_BLOCK_TOKENS` 剔掉的正面词，顶视/仰拍才真的出得来。

### 5e. v2.1 修正实测

v2.1 修的是**语义与坐标**，不是新功能；三处都影响出图，光看 UI 看不出来：

| # | 问题 | 修法 | 实测 |
|---|---|---|---|
| 1 | 极点区视线与高度词打架：py=0.95 时正向同时有 `top-down view` 和 `looking at viewer`，模型给「背面站立回眸」 | `POLE_GAZE` 按 py 正负改写视线短语 | 同参数改出「正上方俯拍 + 主体抬头」 |
| 2 | `subject_yaw` 原本按「相对相机」解释：背面机位 + yaw=0 会同时说「人正对镜头」和「相机在背后」 | 改世界系 `rel_yaw = wrap(subject_yaw - pos_x)`，另加两条去重（前方机位不发 subject_front、背后机位不发 subject_away） | pos_x=1.0 yaw=0 -> 只出 `from behind` 三条 + `looking away`，不再有 `facing the camera` |
| 3 | 3bin 拖到端点只给 `high angle shot`，出图是「稍高的平视」而不是顶视 | `POLE_LATCH=0.9`：`abs(pos_y)>0.9` 时 high/low 锁存成 bird/worm | pos_y=±1.0 出真顶视俯拍 / 贴地仰拍，±0.9 保持 high/low 作对照 |

3D 视口侧同期改的：roll 满量程 ±45 度、`lookAt` 目标按 pan 沿相机 right/up 偏移 2.0、取景框外环拖拽改 roll、
脚下箭头拖 subject_yaw、Alt+拖拽 pan（松手才 commit）。另外修了两个真 bug：单面拾取材质导致外环永远打不中；
roll 在已被自身旋转的坐标系里量角，导致拖 30 度只出 15 度。

> token 预算：相机正向段 >65 标黄、>75 标红（CLIP Chunk 0 上限 75）。工作台 `42 / 75 tok`、节点视口 `44 / 75 tok` 同一口径。

## 6. HTTP API

全部 JSON、UTF-8，根地址 `http://127.0.0.1:{PORT}`；错误统一 4xx/5xx + `{"error": "..."}`。
**除 `/api/generate` 外，任何路由都不会因为 ComfyUI 离线而 5xx。**

| # | 方法 | 路径 | 参数 | 返回要点 |
|---|---|---|---|---|
| 1 | GET | `/api/health` | — | `{ok, version, comfy_url, comfy_reachable, port, out_dir, web_dir}` |
| 2 | GET | `/api/meta` | — | `{version, presets, preset_names, preset_modes, ui_presets, defaults, enums, vocab, templates}`；`presets` 是 11 个内置，`ui_presets` 是用户存的，`templates` 同 `/api/templates` |
| 3 | POST | `/api/preview` | 12 个相机参数 + `manual_positive` / `manual_negative` | `{positive, negative, camera_info, parts, tokens:{positive,negative}, warn:[str], params}`；`camera_info` 是对象不是字符串 |
| 4 | POST | `/api/generate` | 相机参数 + `seed, randomize, steps, cfg, width, height, sampler, scheduler, checkpoint, lora_name, lora_strength, batch, save_prefix`；模板模式另加 `workflow_template, pose_id, template_vars, negative_mode` | `{prompt_id, template, warn:[str], seed_used, seed_source, steps, cfg, width, height, sampler, scheduler, checkpoint, lora_name, batch, positive, negative}` |
| 5 | GET | `/api/progress?prompt_id=…` | `prompt_id` 必填 | `{state, progress, node, queue_pos, images:[{filename,subfolder,type,url,local?,seed?,error?}], error, elapsed}`；离线时另有 `warn` |
| 6 | GET | `/api/view?local=…` | `local`（相对 out 的路径）**或** `filename` + `subfolder` + `type` | 图像字节；路径穿越 → 403，不存在 → 404 |
| 6b | GET | `/api/templates` | — | `{templates:[{id, name, path, dir, title, description, vars, valid, error, nodes}], dirs:[str]}`（**不会因 ComfyUI 离线 5xx**） |
| 7 | GET | `/api/comfy/models` | — | `{checkpoints, loras, samplers, schedulers, comfy_reachable, error}`；离线返回空数组 + `comfy_reachable:false`，不是 500 |
| 8 | GET | `/api/gallery?limit=200` | `limit` 1–200（默认 200） | `{items:[{url, filename, local, mtime, size, seed?, prompt_id?}], out_dir}`，mtime 倒序 |
| 8b | POST | `/api/gallery/clear` | — | `{ok:true, cleared:N}`（删图片与 `.json` sidecar） |
| 9 | GET | `/api/config` | — | 完整配置对象（键集同 `store.DEFAULT_CONFIG`） |
| 9b | POST | `/api/config` | 任意配置字段（部分更新） | `{ok:true, config:{…}}` |
| 10 | POST | `/api/ui_presets` | `{name, vals:[x,y,z,roll]}` | `{ok:true, name, ui_presets}`；名字空或 vals 不合法 → 400 |
| 10b | DELETE | `/api/ui_presets?name=…` | `name`（query 或 body） | `{ok:true, deleted:bool, name, ui_presets}` |
| 11 | POST | `/api/interrupt` | — | `{ok:true}`；离线时也是 **200** + `{ok:false, error}` |
| 12 | POST | `/api/open_folder` | — | `{ok:true, path}`；打不开 → 500 + `{ok:false, error, path}` |
| 13 | GET | `/` 与静态路径 | — | 从 `studio/web` 提供（`.js` / `.mjs` 为 `text/javascript`，`/vendor/three.module.js` 可取）；越界 → 403 |

- **模板模式**：`workflow_template` 非空时**不再要求 `checkpoint`**（模板自带 checkpoint/LoRA），
  返回里 `template` 是命中的文件名、`warn` 是注入过程中的提醒（例如「模板没有 prompt 变量，已写入 KSampler 正向节点 11」）；
  模板找不到或不是 API 格式 → 502 + `{"error":"工作流模板不可用: …"}`。
- **`/api/generate` 的三种失败**：`checkpoint` 为空（仅内置图模式）→ 400（body 附 `hint` 与当前 `checkpoints` 列表）；
  ComfyUI 不可达 → 503 `{"error":"ComfyUI 未启动","comfy_url":…}`；ComfyUI 拒绝建图 → 502。
- **`/api/progress` 的 state 四态**：`queued` 带 `queue_pos`（从 1 起，`progress` 为 0）；`running` 的 `node` 固定
  `KSampler`，`progress` 按 `elapsed/(steps*0.55)` 粗估、**上限 0.95**；`done` 时图已下载到 `studio/out`，
  `images[].url` 形如 `/api/view?local=camera_00001_ab12cd34.png`（下载失败则保留 ComfyUI 代理 `url` 与 `error`）；
  `error` 带文案（ComfyUI 报错、图丢了、被中断、20s 后既不在队列也不在历史里）。
- **seed 规则**：`randomize` 为真或 `seed<0` 用 `random.randrange(0, 2**31)`，否则 `seed & 0xFFFFFFFF`；
  `seed_source` 是 `random` / `fixed`。请求体上限 4 MB，`OPTIONS` 返 204 + CORS 头，不支持的方法 → 405。
  服务端只在内存里留最近 60 个任务（`MAX_TASKS`），重启后旧 prompt_id 查不到。

调用示例：

```bat
curl -s -X POST http://127.0.0.1:8770/api/preview -H "Content-Type: application/json" ^
  -d "{\"pos_x\":0.5,\"pos_y\":0.55,\"pos_z\":0.3,\"roll\":0,\"preset\":\"自定义\",\"strength\":1.0,\"weight_mode\":\"tanh\",\"azimuth_mode\":\"8bin\",\"elevation_mode\":\"3bin\",\"neg_strength\":1.0}"
```

## 7. 输出与文件布局

```
D:\QUARK\sdxl_camera_angle\
├─ studio\  server.py / comfy_bridge.py / store.py / template_bridge.py
│         launch.py, launch.bat, launch.vbs, make_shortcut.ps1
│         web\  index.html, app.js, camera3d.js, styles.css, vendor\three.module.js
│         tools\  e2e_test.py, check_ui.py, monitor.py
│         state\  config.json, presets.json
│         out\    camera_00001_ab12cd34.png (+ 同名 .json sidecar)
├─ core\  camera_core.py, presets.py
├─ nodes\ camera_sdxl_node.py
└─ tests\ test_core.py
```

- **出图文件名**：ComfyUI 的 `SaveImage` 用 `filename_prefix = save_prefix`（默认 `camera`）；工作台下载后统一改名
  `<原名去扩展>_<uuid8>.png`，非法字符替换成 `_`、主干截到 60 字符，**绝不覆盖已有文件**。
- **sidecar**：与图片同目录，名字是「图片名 + `.json`」，即 `camera_00001_ab12cd34.png.json`，内容
  `{seed, prompt_id, source_filename, steps, created}`；画廊靠它显示 seed 角标。
- **配置**：`state/config.json`，写盘走「临时文件 + `os.replace`」，进程被杀不会留半个 JSON；
  文件损坏时改名成 `config.json.broken` 并回退默认值。
- **预设**：`state/presets.json`，格式 `{"名字": [pos_x, pos_y, pos_z, roll]}`。
- **日志**：只有控制台 stdout（`[studio] …` 每行一个请求，未捕获异常带 traceback），不写日志文件。
- `/api/view?local=` 只允许读 `out/` 内的文件，resolve 后越界一律 403。

## 8. 常见问题

**ComfyUI 没启动 / 状态点灭 / 生成报 503** — 确认 http://127.0.0.1:8188/ 能开、ComfyUI 在跑，再看 `comfy_url` 是否指对。
离线时页面不是坏的：预览、复制提示词、3D 拖拽都正常，只是不能出图。

**出图空白 / 糊 / 颜色炸** — 先把 `strength` 拉回 **1.0**；权重过高（>1.5 或 `weight_mode=raw`）会把画面压成贴图。
再者确认模型吃摄影词：Illustrious/SDXL 系可以，纯二次元 danbooru 底座对 `three-quarter view` 反应弱。

**鸟瞰 / 虫瞰糊得厉害** — 这两个预设已自动设成 `elevation_mode=5bin` + `strength=0.7`；手改过就调回来。

**俯视几档看不出差别** — 是模型倾向，不是权重没生效（`high` 与 `bird` 主要差在词面）。想让人别被裁掉，
把 `pos_z` 往**负**方向调（保全身/远景），而不是继续加大 `pos_y`。

**提示词里冒出相机、三脚架之类的道具** — 词表用关系短语（`seen from behind` 而非 `back view camera`）来规避；
若仍出现就降低 `strength`，或把 `pos_y` 拉回平视（极端俯仰下 `bird`/`worm` 更容易被画成实物）。

**`|pos_y|` 接近 1 时方位词消失** — 设计如此：`az_gate=(1-|pos_y|)/0.1`，`>0.9` 开始衰减、±1 归零。

**弹出「Microsoft Edge 未响应，因为现有实例正在以提升的权限运行。是否要用普通权限重启现有实例？」** —
这是 Windows 上的一个老问题：系统里已经有一个**以管理员权限运行**的 Edge（典型来源：浏览器是被管理员身份
运行的终端 / 编辑器 / DSH 启动的），这时一个**普通权限**的进程再去启动 Edge，Edge 会要求「降权重启」，
弹框点「是」会把你现有所有窗口关掉。`launch.py` 的对策是**只试一次、绝不重试**，且默认改成开标签页；
要做到完全不弹框：先彻底退出所有 Edge 窗口（托盘里也退），再双击快捷方式，此时会以你的普通权限重新起一个
干净的浏览器进程，权限一致就不会有对话框。若一定要无边框窗口，用 `launch.bat --window`，并且同样先从
「没有其它 Edge 在跑」的状态开始。

**双击快捷方式后什么都没发生 / 没有窗口** — 服务是起来了（用 http://127.0.0.1:8770/ 手动访问验证），
只是浏览器那一步被上面那条权限问题挡住。用 `launch.bat` 能看到 `[launch]` 输出；或直接手动开服务
（1.3）再把地址粘进浏览器。

**端口被占用** — `launch.py` 会自己换端口，看首行 `[studio] http://127.0.0.1:PORT`。但**手工** `--port` 指定一个
已被占用的端口时，`ThreadingHTTPServer` 默认开着 `allow_reuse_address`，Windows 上会**静默复用**而不报错，
所以手工指定前先 `netstat -ano | findstr :8770` 确认。

**进度条卡 95%** — `running` 阶段按 `elapsed/(steps*0.55)` 估算，本来就会先停在 95% 等真正完成；
状态行还在跳秒数就说明在跑，图一落地立刻 100% 并插进画廊。

**进度查不到 / 突然 error** — `prompt_id` 既不在 `/queue` 也不在 `/history` 超过 20s 即判 error（ComfyUI 重启或任务被清）；
服务端只记最近 60 个任务，重启服务后旧 id 也查不到。

**保存的预设重启页面后不见了** — 它在 `state/presets.json` 里，只是下拉框在**页面加载时**从 `/api/meta` 读一次，
刷新页面即可；它也不是内置预设，后端不认这个名字，靠前端把四个数字写回滑块。

## 9. 二次开发入口

| 位置 | 职责 |
|---|---|
| `core/camera_core.py` | **算法唯一真源**。`compute_camera(pos_x=0, pos_y=0, pos_z=0, roll=0, preset="自定义", strength=1.0, weight_mode="tanh", azimuth_mode="8bin", elevation_mode="3bin", neg_strength=1.0, extra_positive="", extra_negative="", cfg=None)` → `{positive, negative, camera_info, parts}`，零 ComfyUI 依赖，改词表/公式只动这里 |
| `core/presets.py` | 加内置预设：`PRESETS` 加一条 `(x, y, z, roll)`；需换档就加到 `PRESET_MODES`（`elevation_mode` / `strength`）。`PRESET_NAMES` 自动带上，ComfyUI 节点 COMBO 共享 |
| `nodes/camera_sdxl_node.py` | ComfyUI 节点壳，只做参数转换，返回 `(positive, negative, camera_info)` |
| `studio/comfy_bridge.py` | 图结构在 `build_prompt`；要加高清修复、ControlNet 从这里长 |
| `studio/server.py` | 路由在 `StudioHandler._route`；新端点加一个 `if path == "/api/xxx"` 分支，注意「离线不 5xx」红线 |
| `studio/store.py` | `DEFAULT_CONFIG` 加字段后前端自动读（`/api/meta` 的 `defaults`） |
| `studio/web/app.js` | `cameraPayload()` / `genPayload()` 决定发给后端的字段；`pollProgress()` 是出图流程；`camera3d.js` 是 3D 交互 |
| `tests/test_core.py` | 纯 assert 自测：权重曲线、8 档插值、方位门控、距离 6 档边界、dutch、负向开关、预设覆盖 |
| `studio/tools/e2e_test.py` | 端到端契约测试：自带起服务、逐条打 API、ComfyUI 在线时真出一张 640×640 / 12 步的图 |

跑回归：

```bat
D:\QUARK\ComfyUI-aki-v3.2\python\python.exe D:\QUARK\sdxl_camera_angle\tests\test_core.py
D:\QUARK\ComfyUI-aki-v3.2\python\python.exe D:\QUARK\sdxl_camera_angle\studio\tools\e2e_test.py --no-generate
```

`e2e_test.py` 参数：`--port 8850`（固定端口，默认在 8800–8899 里挑）、`--no-generate`（跳过真实出图）、
`--keep-server`（结束后不杀服务）；退出码 0 全绿、1 有 FAIL、3 说明 `server.py` 不存在。
