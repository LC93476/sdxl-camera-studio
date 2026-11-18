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
| studio/web/vendor/three.module.js | frontend | 从 ../js/three.module.js 复制 |
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
13. GET / 与静态路径 → 从 studio/web 提供（.js/.mjs 必须是 text/javascript）；/vendor/three.module.js 必须可取。

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
