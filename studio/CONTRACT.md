# SDXL 相机工作台 · 团队契约 v1

目标：把 sdxl_camera_angle 做成**独立桌面应用**——本地启动 WebUI（Edge --app 窗口，无浏览器边框），
3D 场景里手动拖摄像机 → 实时生成正/负提示词 → 一键提交 ComfyUI 出图 → 画廊回显。

## 目录与归属（严格，勿越界写）

| 路径 | 归属 | 说明 |
|---|---|---|
| studio/CONTRACT.md | Lead | 本文件，只读 |
| studio/server.py | backend | HTTP 服务 + 路由（stdlib ThreadingHTTPServer） |
| studio/comfy_bridge.py | backend | ComfyUI 图构建 / 提交 / 轮询 / 取图 |
| studio/store.py | backend | config.json / presets.json、输出目录、画廊索引 |
| studio/web/index.html, styles.css, app.js, camera3d.js | frontend | 全部前端 |
| js/three.module.js, js/camera_shared.js | Lead | 仓库根唯一一份，studio 通过 `/js/<file>` 静态路由复用（studio/web/vendor/ 已删除，勿再引用） |
| studio/launch.bat, launch.vbs, launch.ps1, 桌面快捷方式 | Lead | 启动器/桌面壳 |
| studio/tools/e2e_test.py, check_ui.py | qa | 端到端与 UI 契约测试 |
| studio/state/, studio/out/ | runtime | 运行时数据 |

后端**只**依赖：python 标准库 + ../core/camera_core.py、../core/presets.py。不得新增第三方依赖。
前端**只**用原生 ES module + 本地 three.js，不得用 CDN（离线可用）。

## 核心算法（唯一真源）

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))
from camera_core import compute_camera, default_tags
from presets import PRESETS, PRESET_NAMES, PRESET_MODES, get_preset

res = compute_camera(pos_x=0.0, pos_y=0.0, pos_z=0.0, roll=0.0,
                     preset="自定义", strength=1.0, weight_mode="tanh",
                     azimuth_mode="8bin", elevation_mode="3bin",
                     neg_strength=1.0, extra_positive="", extra_negative="")
# res = {"positive": str, "negative": str, "camera_info": str, "parts": dict}
```

参数范围：pos_x/pos_y/pos_z ∈ [-1,1]（pos_z 负=远、正=近），roll ∈ [0,1]，strength ∈ [0,2]，
neg_strength ∈ [0,2]，weight_mode ∈ {tanh, linear, raw}，azimuth_mode ∈ {8bin, 4bin}，elevation_mode ∈ {3bin, 5bin}。

## HTTP API（http://127.0.0.1:{PORT}，默认 8770；全部 JSON，UTF-8）

1. GET /api/health → {ok:true, version, comfy_url, comfy_reachable:bool}
2. GET /api/meta → {presets:{名:[x,y,z,roll]}, preset_names:[…], preset_modes:{…}, ui_presets:{名:[x,y,z,roll]},
   defaults:{所有参数字段:默认值}, enums:{weight_mode:[…],azimuth_mode:[…],elevation_mode:[…]}, vocab:{…}}
   presets=内置 11 个，ui_presets=用户保存（presets.json）
3. POST /api/preview body=相机参数（含 preset/manual_positive/manual_negative）→
   {positive, negative, camera_info, parts, tokens:{positive:int,negative:int}, warn:[str]}
   manual_positive 非空时 positive 末尾追加 ", "+manual_positive；manual_negative 同理。
4. POST /api/generate body=相机参数 + {seed, randomize, steps, cfg, width, height, sampler, scheduler,
   checkpoint, lora_name, lora_strength, batch, save_prefix} → {prompt_id, seed_used}
   图结构自己建（不读 SDXL.CameraAngle.json、不依赖插件节点是否安装）：
   CheckpointLoaderSimple(1) → 可选 LoraLoader(2) → CLIPTextEncode(3 正/4 负)、EmptyLatentImage(5)、
   KSampler(6) → VAEDecode(7) → SaveImage(8)。提示词用**服务端 compute_camera 结果**（与 preview 一致，所见即所得）。
5. GET /api/progress?prompt_id=… → {state:"queued"|"running"|"done"|"error", progress:0..1, node, queue_pos,
   images:[{filename,subfolder,type,url}], error, elapsed}
   实现：查 /history/{id} 判完成/出错；未出现则查 /queue 判断 queued/running；done 时把图下载到 studio/out/，
   images[].url 指向 /api/view?local=<相对 out 的路径>。
6. GET /api/view?local=<相对路径> → 图像字节（防目录穿越）；也支持 ?filename=&subfolder=&type= 代理 ComfyUI。
7. GET /api/comfy/models → {checkpoints:[…], loras:[…], samplers:[…], schedulers:[…], comfy_reachable}
   ComfyUI 不可达时返回空数组而非 500。
8. GET /api/gallery → 画廊列表（out 目录 mtime 倒序，≤200，字段 {url, filename, mtime, seed?}）；POST /api/gallery/clear
9. GET /api/config / POST /api/config → 配置持久化（studio/state/config.json）
10. POST /api/ui_presets {name, vals:[x,y,z,roll]} / DELETE /api/ui_presets?name=…
11. POST /api/interrupt → 转发 /interrupt 给 ComfyUI
12. POST /api/open_folder → explorer 打开 out 目录
13. GET / 与静态路径 → 从 studio/web 提供（.js/.mjs 必须是 text/javascript）；/js/three.module.js 与 /js/camera_shared.js 必须经 `/js/<file>` 可取。

错误统一 HTTP 4xx/5xx + {error:str}。除 /api/generate 外任何路由不得因 ComfyUI 离线而 500。

## 前端契约

- **必须存在的 DOM id**：view3d, vx, vy, vz, vr, pos_x, pos_y, pos_z, roll, preset, strength, weight_mode,
  azimuth_mode, elevation_mode, neg_strength, extra_positive, extra_negative, manual_positive, manual_negative,
  positive_out, negative_out, info_out, token_badge, seed, randomize, steps, cfg, width, height, checkpoint,
  lora, lora_strength, btn_generate, btn_interrupt, btn_reset, btn_save_preset, btn_open_folder,
  btn_copy_pos, btn_copy_neg, gallery, status, progressbar, lightbox
- 交互：3D 场景拖点/环改 pos_x/pos_y/pos_z（滚轮改 pos_z），滑块与 3D 双向同步；预览 200ms 防抖，
  参数变化即 POST /api/preview 并刷新三行输出。
- 生成：POST /api/generate → 每 700ms 轮询 /api/progress，进度条+状态；完成把图插到画廊顶部（seed 角标），
  点击出 lightbox（滚轮缩放、←→ 切换）。
- 视觉：深色 #16171b 底 + #ffd75e 强调，无外部字体/图标（内联 SVG 或 unicode）。
- 不得用 localStorage（配置走 /api/config）。

## v2 增补（视角系统升级）

### 归属（v2 新增）

| 路径 | 归属 |
|---|---|
| core/camera_core.py, core/presets.py | Lead（唯一真源；backend/frontend 只读） |
| js/camera_shared.js | Lead（Py→JS 镜像算法 + 坐标系常量，双端共用） |
| js/three.module.js | Lead（唯一一份，studio 通过 /js/ 静态路由复用） |
| tools/token_count.py | backend（纯标准库 token 统计） |
| studio/web/* | frontend |
| tools/*.py, tests/*, docs/*, SPEC.md | Lead |

### 参数集合（v2）

CAM_KEYS += focal, pan_x, pan_y, subject_yaw, gaze_mode。
默认值 {"focal":50,"pan_x":0,"pan_y":0,"subject_yaw":0,"gaze_mode":"auto"}。
预设不再只是名字：presets.preset_params(name) 返回完整覆盖 dict
（pos_*/roll/focal/pan_*/subject_yaw/gaze_mode/elevation_mode/strength），后端与节点都必须用它。
compute_camera 返回 parts[]，每项 {kind,key,raw,weight,phrase,tokens[]}，可直接逐段数 token。

### 后端新增端点（全部 JSON）

1. GET /api/meta 追加：focals:[14,24,35,50,85,135]、gaze_modes:["auto","camera","away"]、sequences:{name:desc}、vendored:[...]。
2. POST /api/preview 返回追加：token_parts:[{kind,key,text,tokens}]、tokens:{positive,negative,total,method}（method=tokenizer|estimate）。
3. GET /api/ui_presets/export → 附件下载 JSON（Content-Disposition: attachment），体为 {"version":2,"presets":{name:vals}}。
4. POST /api/ui_presets/import body {"presets":{...}} → 合并写入 {ok, added, ui_presets}。
5. GET /api/sequences → {names:[...], sequences:{name:{desc,shots:[{label,params}]}}}。
6. POST /api/sequence body {"name":"三视图…","base":{…相机与生成参数…},"same_seed":true,"seed":123}
   → {ok, name, shots:[{label, prompt_id, seed, positive, negative, params}]}；逐张 submit；
   ComfyUI 离线时整单 503 且不得半提交。
7. GET /js/<file> → 从仓库根 js/ 出静态文件（three.module.js、camera_shared.js），resolve 后必须仍在 js 根内。
8. DELETE /api/ui_presets?name= 保持不变。

### 前端新增 DOM id（必须存在）

focal, focal_label, pan_x, pan_y, subject_yaw, gaze_mode, btn_focal_preset,
token_total, token_parts, btn_seq, seq_select, seq_run, seq_strip, grid_overlay, ref_overlay, ref_opacity, ref_file,
btn_hud_toggle, btn_export_presets, btn_import_presets, import_file

### frontend 交互要求（v2）

- 3D：透视相机 fov 跟随 focal 实时变化；相机视锥（frustum）随 FOV 收缩/张开；取景框内叠加三分法九宫格 + 地平线；
  主体脚下画正面箭头 + 视线锥（跟着 subject_yaw 转）；支持参考图叠层（本地 file → objectURL → 半透明背景，ref_opacity 调）。
- 快捷键：1..6 切换标准机位（正 / 背 / 左正侧 / 右正侧 / 高角度 / 低角度），R 重置，G 切换九宫格，F 切换视锥。
- 序列：seq_select 选内置序列 → seq_run 一次提交，seq_strip 并排展示每张进度与成品（复用 /api/progress）。
- Token 预算：token_badge/token_total 高亮当前相机提示词占用（>60 转黄、>90 转红），token_parts 列出每个片段与 token 数。
- 预设：btn_save_preset 保存当前全部 v2 参数；btn_export_presets 走 /api/ui_presets/export；btn_import_presets + import_file 走 import。
- 3D 复用：studio/web/camera3d.js 必须 import "/js/three.module.js" 与 "/js/camera_shared.js"；
  studio/web/vendor/three.module.js 已删除，不要再引用。
## v2.3 增补（共享 3D 核心 / ControlNet 导出 / 任意工作流注入 / 批量队列）

### 归属（v2.3）

| 路径 | 归属 | 说明 |
|---|---|---|
| js/camera_rig.js | studio-fe | 唯一一份 Three.js 场景核心（场景/人体/视线锥/动态视锥/九宫格/按需渲染/离屏导出） |
| studio/web/camera3d.js | studio-fe | 变成薄壳：import CameraRig → 绑定 DOM 滑块 + 事件，不得再自建 scene |
| js/sdxl_camera_widget.js | studio-fe | 用 CameraRig({compact:true}) 替掉自建 scene / 手柄 / 渲染循环 |
| studio/template_bridge.py, studio/comfy_bridge.py, studio/server.py | studio-be | 工作流嗅探、注入、上传、批量队列后端 |
| studio/web/app.js, index.html, styles.css | studio-frontend | 预览/导出/批量网格 UI |
| pyproject.toml, requirements.txt, .github/workflows/ci.yml, tools/token_count.py, tools/verify_repo.py, start_studio.bat | studio-qa | 打包与 CI 门禁 |
| core/*, js/camera_shared.js, tools/parity_check.py, tests/*, docs/*, README.md, SPEC.md | Lead | 真源与文档 |

### js/camera_rig.js（双端共享 3D 核心）

```js
import { CameraRig } from "/js/camera_rig.js";
const rig = new CameraRig(canvasEl, { compact: true, interactive: true,
                                     onChange: s => {}, onCommit: s => {} });
rig.setState({ pos_x, pos_y, pos_z, roll, focal, pan_x, pan_y, subject_yaw, gaze_mode });
rig.getState();            // 同 setState 的键，数值已 clamp
rig.applyShot(n);          // 1..6 标准机位
rig.toggleGrid(true); rig.toggleFrustum(true);
rig.setRefImage(url); rig.setRefOpacity(0.4);
rig.resize(); rig.dispose();
// v2.3 新增离屏导出（返回 data URL，image/png）：
rig.renderDepthDataURL(512, 768, { invert: true });  // 白=近 黑=远，ControlNet depth 口径
rig.renderPoseDataURL(512, 768);                     // OpenPose 18 点标准配色骨架，黑底
```

要求：
1. `compact:true` 时不依赖任何外部 DOM id、不注册 document 级键盘监听、尺寸取 canvas 自身或 320×300；`interactive:false` 时不绑指针事件。
2. 场景内容必须与现 studio/web/camera3d.js 等价：网格 + 人体标定模型 + 视线锥 + 主体朝向箭头 + 方位环/高度弧/距离管 + 动态视锥线框 + 三分法九宫格 + 地平线 + 参考图叠层。
3. 按需渲染：状态变化 `invalidate()` → 单帧 raf；静止 0 帧；`dispose()` 释放 geometry/material/renderer。
4. 离屏导出复用同一份场景与相机（不改当前 viewport 尺寸），渲完必须恢复 renderer 状态；导出 canvas 用完 dispose。
5. 深度导出用 `THREE.MeshDepthMaterial`（`depthPacking: THREE.BasicDepthPacking`），近裁剪面对应白色；输出 `background = 0x000000`。
6. 骨架导出：18 点 keypoint（0 鼻 1 颈 2 右肩 3 右肘 4 右腕 5 左肩 6 左肘 7 左腕 8 右髋 9 右膝 10 右踝 11 左髋 12 左膝 13 左踝 14 右眼 15 左眼 16 右耳 17 左耳），
   连接对 (1,2)(1,5)(2,3)(3,4)(5,6)(6,7)(1,8)(8,9)(9,10)(1,11)(11,12)(12,13)(1,0)(0,14)(14,16)(0,15)(15,17)，
   投影用当前相机 `project()`，线与点按 OpenPose 官方配色（右=红 #ff0000 / 左=蓝 #0000ff / 中=黄 #ffff00），黑底。

### 后端新增端点（v2.3，全部 JSON）

1. `POST /api/controlnet/upload` body `{kind:"depth"|"pose", data_url, name?}` → 把 data URL 解码后经 ComfyUI `POST /upload/image`(multipart, type=input) 上传，
   返回 `{ok, kind, filename, subfolder:"", type:"input", bytes}`；ComfyUI 离线 503 `{error}`；data_url 非法 400。
2. `POST /api/templates/inspect` body `{ref}`（模板 id 或路径）→ `{ok, template, nodes, sniff:{camera, positive, negative, sampler, latent, save, controlnet:{depth:[{node,field}],pose:[...]}}, warn:[...]}`，
   用于前端显示「这条工作流会被注入哪几个节点」。嗅探规则：优先 `CameraAngleSDXL` 节点（直接用其 positive/negative 输出），否则用 KSampler.positive/negative 指向的 `CLIPTextEncode*`。
3. `POST /api/batch` body `{name?:"三视图", base:{相机+生成参数}, shots:[{label, params}], same_seed:true, seed}` → `{ok, batch_id, seed, jobs:[{label, prompt_id, seed, positive, negative}]}`；逐张 submit（同一 seed 时共用），ComfyUI 离线整单 503 不半提交。
4. `GET /api/batch?batch_id=…` → `{ok, batch_id, state:"queued"|"running"|"done"|"error", done, total, jobs:[{label, prompt_id, state, progress, images:[{filename,url}]}]}`，内部复用 progress 逻辑。
5. `POST /api/preview` 不变；`POST /api/generate` 追加可选 `template` 与 `template_vars`（含 `controlnet_depth` / `controlnet_pose` 已上传的 filename），注入失败只 warn 不改结构。

### 前端新增 DOM id（v2.3，必须存在）

`btn_depth_export, btn_pose_export, depth_preview, pose_preview, cn_auto_inject, cn_kind_select, cn_strength,`
`template_select, btn_template_inspect, template_report, batch_name, batch_seed, batch_same_seed, btn_batch_run, batch_grid, batch_status`

### 前端交互（v2.3）

- 导出：点 `btn_depth_export` / `btn_pose_export` → `rig.renderDepthDataURL(512,768)` / `renderPoseDataURL(512,768)` → 填入 `depth_preview` / `pose_preview`（`<img>`）并 POST `/api/controlnet/upload`；
  勾了 `cn_auto_inject` 时把返回 filename 记进生成参数（`template_vars.controlnet_depth|pose`）。
- 批量：`batch_seed` 锁定同一 seed，`btn_batch_run` 读内置序列（或当前 N 个机位）→ POST `/api/batch` → 轮询 `GET /api/batch`，`batch_grid` 网格并排显示进度与成品（含 label 与 seed）。
- 模板：`template_select` 列出 /api/templates → `btn_template_inspect` 显示 `template_report`（哪条被注入、有无 CameraAngleSDXL、ControlNet 槽位）。
- 所有新控件断线（后端 503）时只显示错误文案，不得抛异常中断页面。

