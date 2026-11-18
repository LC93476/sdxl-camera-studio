# SDXL 相机词表与权重实证调研

> 任务：task-2。产出对象：task-1（core/nodes 实现）与 Lead（出图验证）。
> 契约来源：`D:/QUARK/sdxl_camera_angle/SPEC.md`（下称 SPEC）。
> 本文所有"源码行号"均指本机 `D:/QUARK/ComfyUI-aki-v3.2/ComfyUI/` 下的文件；所有"实测"均指用该目录自带 CLIP tokenizer + 逐行复刻的权重解析器跑出的真实输出（脚本见 §9，可复跑）。
> 词表键名严格等于 SPEC 2.5，一个都没改。

---

## 0. 先给结论（TL;DR）

| # | 结论 | 依据 |
|---|---|---|
| 1 | SDXL 每块 77 槽 = 1 BOS + **75 个可用内容 token** + 1 EOS；不是 77 可用，也不是 74。 | `comfy/sd1_clip.py:643` + 实测 |
| 2 | 相机段必须落在**第 0 块**：pooled 只取第 0 块，且 chunk0 的 cross-attn 话语权最大。 | `comfy/sdxl_clip.py:62`、`comfy/sd1_clip.py:50` |
| 3 | `(text:1.4)` 是**绝对覆盖**，不是叠加：`((x:1.4))` 结果仍是 1.4，不是 1.54；裸 `(x)` 才是 ×1.1。 | `comfy/sd1_clip.py:354-364` |
| 4 | **方括号 `[x]` 在 ComfyUI 里没有任何降权语义**，只多 2 个 token。A1111 里 `[x]`=÷1.1，ComfyUI 不支持。 | `comfy/sd1_clip.py:369-377` + 实测 |
| 5 | 权重真实数学是**沿"空 prompt 方向"外插**：`z = (z - z_empty)*w + z_empty`。>1 是把 embedding 往 CLIP 流形外推，所以 1.5 以上必崩。 | `comfy/sd1_clip.py:63` |
| 6 | 16 条词表全量输出 = **152 token**，直接吃两块（2×75）。必须按轴只发命中档。 | 实测 §1.5 |
| 7 | SPEC 2.1 校验值有一处笔误：`raw=0.5` 复算是 **0.93**，不是 0.92。task-1 若写成断言会挂。 | §9.2 复算 |

---

## 1. 事实基础：SDXL 双 text encoder、77 槽位、75 有效 token

### 1.1 双编码器结构

| 组件 | 隐藏维 | tokenizer | 池化输出 |
|---|---|---|---|
| CLIP-L | 768 | `SDTokenizer`（`pad_with_end=True`，pad=49407） | 不使用 |
| CLIP-G | 1280 | `SDXLClipGTokenizer`（`pad_with_end=False`，pad=**0**） | **只用 G 的 pooled** |

证据：

- `comfy/sdxl_clip.py:24-33`：`SDXLTokenizer.tokenize_with_weights` 把**同一段文本**分别喂给 `clip_g` 与 `clip_l`，返回 `{"g": [...], "l": [...]}`。
- `comfy/sdxl_clip.py:56-62`：`cut_to = min(l_out.shape[1], g_out.shape[1])`，`torch.cat([l_out[:,:cut_to], g_out[:,:cut_to]], dim=-1)` → cross-attn 上下文 **768+1280 = 2048 维**，两编码器块数被强制对齐。
- `comfy/sdxl_clip.py:62` 返回 `g_pooled`；`comfy/sd1_clip.py:50` 里 `first_pooled = pooled[0:1]` —— **只取第 0 块**。
- `comfy/sdxl_clip.py:14,21`：G 的 `special_tokens={"start":49406,"end":49407,"pad":0}`。
- 论文口径：arXiv:2307.01952 摘要 "a larger cross-attention context as SDXL uses a second text encoder"。

**含义**：`cut_to` 说明两个编码器按同一 77/75 规则各自分块且块数一致；pooled 只看第 0 块 → 第 0 块对整体构图/风格话语权最大。相机词放第 0 块 = 用最高权重通道喊话。

### 1.2 77 与 75：把数字钉死

`comfy/sd1_clip.py:488` 的 `SDTokenizer.__init__` 默认 `max_length=77`（SDXL 没改）。

分块在 `comfy/sd1_clip.py:628-674`，关键三行：

```python
# comfy/sd1_clip.py:643
if len(t_group) + len(batch) > self.max_length - has_end_token:
# comfy/sd1_clip.py:651-654
if self.end_token is not None:
    batch.append((self.end_token, 1.0, 0))
# comfy/sd1_clip.py:671-672
if self.pad_to_max_length and len(batch) < self.max_length:
    self.pad_tokens(batch, self.max_length - len(batch))
```

推演：`batch` 起始就有 1 个 BOS（`:631-633`），判据是 `len(batch)+len(content) > 77-1`，故内容位 = 77 − 1(BOS) − 1(EOS) = **75**。

- **74 是哪来的**：社区教程常写"75 可用、算上权重文本/逗号 74"。解析器在 tokenize **之前**就把括号和 `:1.40` 剥掉了（`:589-593` 先 `token_weights`，`:597-626` 才 tokenize 纯短语），所以**括号与数字不占 token，逗号占 1 个**。`(front view:1.10), (eye level shot:1.05)` 实际消耗 = 11 + 1 + 3 + 1 = 16 token。
- **填充不是零成本**：CLIP-L 用 49407 填（`pad_with_end=True`，`comfy/sd1_clip.py:530-531`），CLIP-G 用 0 填；SDXL 两个编码器**都没开 attention mask**（`comfy/sdxl_clip.py:14` 未传 `enable_attention_masks`），填充 token 仍参与注意力。

### 1.3 短语会不会被切断

会，但只对超长单词。`comfy/sd1_clip.py:634-664` 以 word 为单位塞入，`is_large = len(t_group) >= self.max_word_length`（`:636`，`max_word_length = 8`，`:541`）：

- 组内 < 8 token：整组要么进、要么整组顺延（`:652-661`）。
- 组内 ≥ 8 token：**被劈开**，前半留本块、后半进下一块（`:646-650`）。

所以正常短语不会被切，但"一个括号塞整句"的做法一旦跨块，权重仍作用于整句，语义却被拆到两块。

### 1.4 相机段该放第几块 —— 结论与理由

**放第 0 块，紧跟质量前缀之后。**

| 理由 | 机制 | 出处 |
|---|---|---|
| pooled 只读第 0 块 | `first_pooled = pooled[0:1]`，SDXL 的 `g_pooled` 直接进 `pooled_output` 参与 y 条件 | `comfy/sd1_clip.py:50` + `comfy/sdxl_clip.py:62` |
| 块间无跨块注意力 | 每块独立 BOS+EOS | `comfy/sd1_clip.py:628-674` |
| CLIP 文本塔是**因果**注意力 | `causal_mask = torch.full(...).triu_(1)`，后 token 能看前 token，前看不到后 | `comfy/clip_model.py:174-179` |
| 跨块即稀释 | 152 token 全量词表 = 2 块起，第 1 块只剩 75 位给主体/场景 | 实测 §1.5 |

因果掩码给出排布策略：主体/服装在前、相机词在其后 → 相机 token 在注意力里"看见了"主体，构图描述与主体更一致。推荐顺序：

```
质量前缀 → 主体/身份/服装 → 相机段（方位+高度+距离+dutch） → 场景/光照/风格 → 镜头/景深
```

不要放到第 60 token 之后：实测 C 组 66/77 已贴边，再加 rich 相机段立刻溢出。

### 1.5 token 预算实测（CLIP tokenizer，BOS/EOS 已扣除）

| 键名 | 短语（SPEC 2.5） | token | 占一块(75) |
|---|---|---|---|
| front | front view, facing the camera, straight-on shot | **11** | 15% |
| front_right | three-quarter view from the right | 7 | 9% |
| right | side view, profile view from the right | 8 | 11% |
| back | from behind, back view, seen from behind | 9 | 12% |
| high | high angle shot, from above | 6 | 8% |
| eye | eye level shot | 3 | 4% |
| low | low angle shot, from below, looking up | 9 | 12% |
| bird | bird's-eye view, top-down view, aerial shot, from above | 11 | 14% |
| worm | worm's-eye view, extreme low angle, from below | 9 | 12% |
| ecu | extreme close-up, macro shot | 7 | 9% |
| cu | close-up, portrait shot | 6 | 8% |
| medium | medium shot, waist up | 5 | 7% |
| cowboy | cowboy shot, mid-thigh up | 7 | 9% |
| full | full body shot | 3 | 4% |
| wide | wide shot, long shot, full figure in frame | 10 | 13% |
| dutch | dutch angle, canted camera, tilted horizon | 10 | 13% |
| — | 16 条全量拼接 | **152** | **2.03 块** |

对照原仓库 danbooru 词：`from front` 2 token、`facing right` 2 token、裸 `front view` 2 token、裸 `three-quarter view` 4 token。

**"照抄原仓库"的隐藏代价**：原词 2 token/条，我们 3~11 token/条，同样 4 方位输出从 8 token 涨到 30+。所以必须**按轴只发当前命中档**（8bin 最多 2 个方位 + 1 高度 + 1 距离 + 可选 dutch），典型输出 20~45 token；严禁把 16 条词表整拼。

实测典型输出：

```
compact  (front_right:1.15), (eye level:1.00), (close-up:1.10)                -> 29 token
rich     (three-quarter view from the right:1.15), (eye level shot:1.05),
         (close-up, portrait shot:1.10), (dutch angle, tilted horizon:1.20)   -> 50 token
```

溢出实证（前缀 72 token + rich 相机段）：

```
prefix_tokens=72 cam_tokens=53 total=125 chunks=2
 chunk0 slots=77 content=75
 chunk1 slots=29 content=27
```

该统计按原始字符串计，含被解析器剥掉的 `:1.15` 数字，属**保守高估**；扣除 4 组数字约 10~12 token 后仍 ≈113 > 75，结论不变：**长前缀 + rich 相机段 = 必溢出到第 1 块**。

---

## 2. ComfyUI 的 prompt weighting 到底怎么解析

### 2.1 调用链

```
CLIPTextEncode.encode(text) -> CLIPTextEncode.encode
  -> self.tokenize(text)                                # comfy/sd.py:439-441
    -> SDXLTokenizer.tokenize_with_weights              # comfy/sdxl_clip.py:29-33
      -> SDTokenizer.tokenize_with_weights              # comfy/sd1_clip.py:577
           text = escape_important(text)                # :589
           parsed_weights = token_weights(text, 1.0)    # :593
           ... 逐 word tokenize                          # :597-626
           ... 分块                                      # :628-674
  -> SDXLClipModel.encode_token_weights                 # comfy/sdxl_clip.py:56
    -> ClipTokenWeightEncoder.encode_token_weights      # comfy/sd1_clip.py:29-80
         z[i][j] = (z[i][j] - z_empty[j]) * weight + z_empty[j]   # :63
```

### 2.2 解析器逐行语义

```python
# comfy/sd1_clip.py:349-367（原文）
def token_weights(string, current_weight):
    a = parse_parentheses(string)
    out = []
    for x in a:
        weight = current_weight
        if len(x) >= 2 and x[-1] == ')' and x[0] == '(':
            x = x[1:-1]
            xx = x.rfind(":")
            weight *= 1.1
            if xx > 0:
                try:
                    weight = float(x[xx+1:])
                    x = x[:xx]
                except:
                    pass
            out += token_weights(x, weight)
        else:
            out += [(x, current_weight)]
    return out
```

由此 19 行直接推出的语义（每条都被复刻脚本验证，见 §9.1）：

| 写法 | 解析结果 (片段, 权重) | 说明 |
|---|---|---|
| `front view` | `("front view", 1.0)` | 裸词 |
| `(front view)` | `("front view", 1.1)` | 无数字 → 当前权重 ×1.1 |
| `((front view))` | `("front view", 1.21)` | 递归相乘 |
| `(front view:1.40)` | `("front view", 1.40)` | **绝对值覆盖**，不再乘 1.1 |
| `((front view:1.30))` | `("front view", 1.30)` | 外层 1.1 被内层绝对值覆盖 |
| `(a, b:1.25)` | `("a, b", 1.25)` | 一个括号可多词共享末尾权重 |
| `(a:1.2, b:1.3)` | `("a:1.2, b", 1.3)` | **危险**：`rfind(":")` 取最后一个冒号，`1.2` 残留成 token |
| `[front view]` | `("[front view]", 1.0)` | **方括号零语义**，多 2 token |
| `\\(x\\)` | 走 `escape_important` → 字面括号 | 短语内要写括号必须转义 |
| `(:1.2)` | 不解析（`xx > 0` 不成立） | 冒号必须在位置 >0 |

三条**硬约束**（决定我们输出格式对不对）：

1. **一个括号组只能有一个末尾冒号权重**。词表内禁止冒号，每个短语单独包一层括号 —— SPEC 2.2 的 `_split_tags` 逐个加权重必须坚持。
2. **不要用 `[]`**。不降权、多耗 token，还把括号字符塞进 CLIP 词表。
3. **权重 = 1.0 时不要加括号**。除"干净"外还有机制理由：`comfy/sd1_clip.py:36` 的 `has_weights` 为 False 时，不会走 `:40-44` 追加空 prompt 编码、也不会走 `:57-64` 的外插数学。裸词 = 原始 CLIP 语义 + 少一次编码。SPEC 2.2 的 `|w-1.00| < 0.005 → 裸短语` 正确。

### 2.3 权重到底改了什么：外插，不是缩放

```python
# comfy/sd1_clip.py:57-64
if has_weights:
    z_empty = out[-1]
    for i in range(len(z)):
        for j in range(len(z[i])):
            weight = token_weight_pairs[k][j][1]
            if weight != 1.0:
                z[i][j] = (z[i][j] - z_empty[j]) * weight + z_empty[j]
```

- 数学：`z' = z_empty + w·(z − z_empty)` —— 以空 prompt 为原点线性外推。
- 单位是**每 token**，组内所有 token（含逗号）乘同一 w。
- 后果：`w=1.4` 不是"更强调"，而是把 embedding 沿 `(z−z_empty)` 拉出 40%。1.3~1.5 已明显脱离 CLIP 训练分布 → 注意力尖峰 → 纹理崩坏/贴图块/面部结构错乱。这就是"SDXL >1.6 出噪点"的机制。
- 反向：`w=0.9` 是软化，不是删除。要删除请放**负向 prompt**（CFG 下 = 远离该方向），别给 0.3。
- 上游自证：`D:/QUARK/_repo2/Comfyui-Anima_camera_angle/README.md:86` 原话"使用 illustrious 模型时，正前/后方视角时 **XY 乘数不要超过 2**，其余时候 **不要超过 3**（待验证），否则容易出噪点图"。原仓库 `az_weight=10`、`xy_mult=10`（`camera_angle_node.py:14,37-39`）远超阈值，靠 danbooru 系模型容忍；SDXL 无此容忍度。
- **tanh 曲线自带安全带**：解析上限 `1+0.55=1.55`，实际 `raw=10` 只到 1.40（§9.2）。即 **tanh 模式物理上不可能输出 >1.4**。只有 `weight_mode="raw"` 才到 2.0，那模式只该给实验用。

---

## 3. SDXL 对相机/构图词的实际响应

### 3.1 吃得进去的

| 类型 | 例子 | 为什么有效 |
|---|---|---|
| 摄影机位名词 | `front view`, `side view`, `profile view`, `three-quarter view` | SDXL captioner 用自然语言描述图像，机位是高频项 |
| 镜头距离 | `close-up`, `extreme close-up`, `medium shot`, `full body shot`, `wide shot` | 与 caption 尺寸描述对齐 |
| 高度 | `high angle shot`, `low angle shot`, `eye level`, `bird's-eye view` | 同上 |
| 器材/构图附加 | `dutch angle`, `85mm lens`, `shallow depth of field`, `rule of thirds` | 纯语料词（原仓库 `camera_angle_node.py:42-48` 同一套） |
| 自然语言整句 | `the camera looks up at her from below` | 论文强调更大 cross-attn 上下文 + 多宽高比训练 |

### 3.2 吃不进 / 会反的（必须换掉的 danbooru 词）

| danbooru 词 | 在 SDXL 上的问题 | 换成 |
|---|---|---|
| `from front` | 2 token 介词短语，SDXL 语料里几乎不作为机位标签；强绑定只存在于 danbooru 系（anima/illustrious） | `front view, facing the camera, straight-on shot` |
| `facing right` / `facing left` | 原仓库把 `left` 映射成 `facing right`（`camera_angle_node.py:12`），语义是"角色朝右"；SDXL 会读成**主体朝向**，于是"相机在左"变"人看左" —— 方向常反 | `side view, profile view from the left`（明确 from 是机位） |
| `back view` 单词 | `back` 有"背部/背景"歧义 | `from behind, back view, seen from behind`（三重冗余压歧义） |
| `eye-level` | 连字符被 BPE 切成 `eye` `-` `level`，多 1 token 无收益 | `eye level shot`（3 token） |
| `cowboy shot` | SDXL captioner 少这么写，响应弱 | 保留但**必须**带 `mid-thigh up`；或降级 `medium shot` |
| 单 token `wide`/`full` | 太泛，常被忽略 | `wide shot, long shot, full figure in frame` |

实测：`[dutch angle]` 比 `dutch angle` 多 2 token 且权重不变；`canted` 被切成 `can`+`ted`（2 token 换 1 个词的信息量）→ 建议词表里 `canted camera` 改 `tilted camera`（省 1~2 token）。**但 SPEC 2.5 是定稿，core 默认值照抄 SPEC，改动只作为 `_cfg_json` 覆盖示例。**

### 3.3 为什么 >1.5 会出噪点（三因叠加）

1. **外插越界**：`z' = z_empty + 1.6·(z−z_empty)`，cross-attn 的 QK 分布出现尖峰。
2. **多 token 同向叠加**：一个括号组内所有 token 同乘 w。`(front view, facing the camera, straight-on shot:1.6)` 是 11 个 token **同时**外推 60%。
3. **CFG 再放大**：`uncond + scale·(cond − uncond)` 会二次放大，`cfg=7` 时 1.4 的有效偏移≈1.6~1.8。所以**上限要按 cfg 定**：cfg 7~8 相机权重别超 1.3；cfg 4~5 可到 1.4。

社区口径：A1111 wiki 的 emphasis 表（`(word)`=×1.1、`(word:1.5)`=×1.5、`[word]`=÷1.1）是 A1111 语义；ComfyUI 只实现前半，**没实现方括号**。链接见 §8。

---

## 4. 完整词表（键名 = SPEC 2.5）+ 推荐权重区间

### 4.1 权重刻度（tanh 曲线实际可取值）

| raw | 1.0 | 1.1 | 1.2 | 1.5 | 2 | 3 | 4 | 5 | 6 | 8 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 输出 w | 1.00 | 1.02 | 1.03 | 1.07 | 1.13 | 1.21 | 1.26 | 1.30 | 1.33 | 1.37 | 1.40 |

| raw | 0.9 | 0.7 | 0.5 | 0.3 | 0.1 |
|---|---|---|---|---|---|
| 输出 w | 0.98 | 0.95 | 0.93 | 0.90 | 0.88 |

**读法**：`raw ∈ [1,3]` 覆盖 `w ∈ [1.00,1.21]`，是"有感觉但不出事"的整个工作区间；`raw=5` → 1.30（强）；`raw≥8` → 1.37~1.40（上限）。`raw<0.9` 只到 0.88~0.98，属轻微软化，别当删除用。

**权重语义刻度（判读用）**：

| w | 观感 |
|---|---|
| 0.85~0.95 | 该词几乎不起作用 |
| 1.00 | 中性（裸词） |
| 1.05~1.15 | 可感知，构图开始听话 |
| 1.20~1.30 | 明确服从，细节正常 |
| 1.35~1.40 | 强服从，偶发局部纹理劣化 |
| ≥1.50 | 噪点/贴图/畸形概率显著上升（不要默认输出） |

### 4.2 正向词表（8 方位）

| 键名 | 短语（照 SPEC 2.5） | token | 推荐 w 区间 | 默认 raw | 理由 |
|---|---|---|---|---|---|
| front | front view, facing the camera, straight-on shot | 11 | 1.00 ~ 1.15 | 2.0 → 1.13 | 语义最重，11 token 同时外推，>1.15 易把脸压平、丢立体感 |
| front_right | three-quarter view from the right | 7 | 1.05 ~ 1.20 | 2.0 → 1.13 | 3/4 侧是 SDXL 最熟机位，响应强，不需大权重 |
| right | side view, profile view from the right | 8 | 1.05 ~ 1.20 | 2.0 → 1.13 | `profile` 明确；输出时不要夹任何 facing 词，否则打架 |
| back_right | three-quarter rear view from the right | 7 | 1.10 ~ 1.25 | 2.5 → 1.17 | `rear` 在 SDXL 语料偏弱，需比正面高一点才稳 |
| back | from behind, back view, seen from behind | 9 | 1.10 ~ 1.25 | 2.5 → 1.17 | 三重冗余压歧义；9 token 同时外推，别超 1.25 |
| back_left | three-quarter rear view from the left | 7 | 1.10 ~ 1.25 | 2.5 → 1.17 | 同上 |
| left | side view, profile view from the left | 8 | 1.05 ~ 1.20 | 2.0 → 1.13 | 同 right |
| front_left | three-quarter view from the left | 7 | 1.05 ~ 1.20 | 2.0 → 1.13 | 同上 |

**8bin 双档混合规则**（SPEC 2.3：`(1-frac)` 给 i0、`frac` 给 i0+1）：两档同发时**预算守恒**很重要。建议主档 `raw = 1 + 2·(1-frac)`、次档 `raw = 1 + 2·frac` —— frac=0.5 时两档都 1.13，frac=0.1 时 1.19 / 1.07。理由：两个方位词在 CLIP 里竞争，同时高位会互相抵消并抬高整体扰动；守恒让"斜侧"看起来真是 45°，而不是"正面+右侧"两张脸叠着。次档 `frac < 0.06`（SPEC `deadzone_ratio`）已被丢弃，不会输出 `(x:1.00)` 空转词。

### 4.3 正向词表（高度）

| 键名 | 短语 | token | 推荐 w | 默认 raw | 理由 |
|---|---|---|---|---|---|
| high | high angle shot, from above | 6 | 1.05 ~ 1.25 | 曲线后的 `|y|·10` | 俯视要同时改"看得到头顶"和"背景在地面"，低了不起作用 |
| eye | eye level shot | 3 | **1.00（裸词）** | 1.0 | 平视是 SDXL 默认视角，加权只会推向证件照。SPEC 2.4"平视 raw 固定 1.0"正确 |
| low | low angle shot, from below, looking up | 9 | 1.05 ~ 1.25 | 同上 | 仰视在 SDXL 上比俯视弱，需略高；`looking up` 是关键锚 |
| bird | bird's-eye view, top-down view, aerial shot, from above | 11 | 1.20 ~ 1.35 | 2.5 → 1.17 | 仅 `elevation_mode="5bin"` 且 `|y|>0.75` 出现；此时方位已被 `az_gate` 压到 0，可放心给高 |
| worm | worm's-eye view, extreme low angle, from below | 9 | 1.20 ~ 1.35 | 2.5 → 1.17 | 同上；`extreme` 与 bird/worm 互斥，别和 low/high 同现 |

**门控提醒**：`az_gate = clamp((1-|pos_y|)/0.1)`（SPEC 2.4）在 `|pos_y|>0.9` 时为 0，虫瞰/鸟瞰档方位词整段消失。这是设计意图，但出图验证时要单独确认"方位词确实没了"，否则会误判成 bug。

### 4.4 正向词表（距离 6 档）

| 键名 | 短语 | token | 推荐 w | 默认 raw | 理由 |
|---|---|---|---|---|---|
| ecu | extreme close-up, macro shot | 7 | 1.05 ~ 1.15 | `1 + 0.5·|z|` | 极特写易"只剩一只眼"，宁低勿高；`macro` 会引入景深/失焦副作用 |
| cu | close-up, portrait shot | 6 | 1.05 ~ 1.15 | 同上 | 与 `portrait` 语料强绑定，低权重即可 |
| medium | medium shot, waist up | 5 | **1.00（裸词）** | 1.0 | 中景是默认构图，加权会往"半身像"漂移 |
| cowboy | cowboy shot, mid-thigh up | 7 | 1.05 ~ 1.15 | 同上 | `cowboy shot` 存在但不强，靠 `mid-thigh up` 兜底 |
| full | full body shot | 3 | 1.05 ~ 1.20 | 同上 | 3 token 性价比最高；SDXL 默认裁切，1.15 明显改善"全身出画" |
| wide | wide shot, long shot, full figure in frame | 10 | 1.10 ~ 1.25 | 同上 | 远景要压"想画大脸"的倾向，权重略高；10 token 会与 subject 抢 |

**判读提醒**：SPEC 2.4 保留"档位跳变、档内不平滑"，`pos_z=0.21` 与 `0.69` 都输出同一个 `cu:1.05`。这是**故意**的（原仓库行为），按 6 档看，别指望连续推拉。

### 4.5 倾斜

| 键名 | 短语 | token | 推荐 w | 默认 raw | 理由 |
|---|---|---|---|---|---|
| dutch | dutch angle, canted camera, tilted horizon | 10 | 1.05 ~ 1.30 | `1 + 6·roll`（SPEC 2.4） | 荷兰角是整幅画面旋转，10 token 外推易带裁切/畸变；1.30 已明显倾斜 |

### 4.6 负向镜像词表

| 键名 | 负向短语（SPEC 2.5） | token | 推荐 w | 理由 |
|---|---|---|---|---|
| front | from behind, back view, seen from behind | 9 | 1.10 ~ 1.20 | 用完整三重冗余，单个 `from behind` 在负向里太弱 |
| front_right | three-quarter rear view from the left | 7 | 1.05 ~ 1.15 | 斜前 ↔ 斜后另一侧；**不要**用 `side view from the left`（会连"左侧"一起压掉，斜前 45° 也需要一点左） |
| right | side view from the left, profile from the left | 9 | 1.10 ~ 1.20 | 正侧 ↔ 反侧，必须明确 `from the left` |
| back_right | three-quarter view from the left | 7 | 1.05 ~ 1.15 | 镜像 |
| back | front view, facing the camera | 6 | 1.10 ~ 1.20 | 背面 ↔ 正面 |
| back_left | three-quarter view from the right | 7 | 1.05 ~ 1.15 | 镜像 |
| left | side view from the right | 6 | 1.10 ~ 1.20 | 镜像（SPEC 只给这一条，够用） |
| front_left | three-quarter view from the right | 7 | 1.05 ~ 1.15 | 镜像 |
| high | low angle shot, from below, looking up | 9 | 1.05 ~ 1.15 | 高度镜像要给足，否则俯视常被读成平视 |
| low | high angle shot, from above | 6 | 1.05 ~ 1.15 | 镜像 |
| eye | high angle, low angle | 4 | 1.00 ~ 1.05 | 双向压制两个极角会让构图变平（SDXL 退化成无透视），故低 |
| ecu | wide shot, full body | 5 | 1.05 ~ 1.15 | 距离镜像 |
| cu | wide shot, full body, distant | 7 | 1.05 ~ 1.15 | 同上 |
| medium | close-up, extreme close-up | 6 | 1.05 ~ 1.15 | 中景 ↔ 特写 |
| cowboy | close-up, extreme close-up | 6 | 1.05 ~ 1.15 | 同上（与 medium 共用一组镜像合理：都不是面部大特写） |
| full | close-up, portrait shot | 6 | 1.10 ~ 1.20 | 全身 ↔ 特写，距离轴最容易被违反的一对 |
| wide | close-up, portrait shot | 6 | 1.10 ~ 1.20 | 同上 |
| dutch | horizon level, straight horizon | 5 | 1.05 ~ 1.15 | 荷兰角镜像；`horizon level` 是关键锚 |

**负向总则**：

1. 只压"当前轴的对立面"，一次最多 3 条（方位 1 + 高度 1 + 距离 1）+ 可选 dutch。条数一多，负向 token 也吃上下文（负向同样 77/75 分块）。
2. 负向上限应比正向低 0.05~0.10：CFG 下 `cond − uncond` 差值被放大，负向 1.2 的推力≈正向 1.35。SPEC 2.5 里 `(-:1.15)` 的直觉内核正确，实现写成 `(phrase:1.15)`。
3. `neg_strength=0` 时输出**空串**（不带空格），让 CLIPTextEncode 走"无权重"路径（`comfy/sd1_clip.py:36`），不触发外插。
4. 负向里**不要**写 `bad anatomy, worst quality` 这类通用词——那是工作流自身负向模板的事，本节点只负责相机镜像；SPEC 2.6 的 `extra_negative` 就是留给用户拼的。

### 4.7 词表设计五条硬规则（写死在 core）

1. **短语内不含冒号**（`rfind(":")` 会误判，见 §2.2）。
2. **短语内不含括号**（否则要转义，用户复制到别处就崩）。
3. **每条短语自带"语义锚 + 解释"**（`side view` 后必跟 `profile view from the right`）：SDXL 对单词响应不稳、对短语稳。
4. **机位用 `from X` / `view`，不用 `facing X`**（后者是主体朝向，会反）。
5. **单条不超 11 token**（front 已是 11，到上限）。

---

## 5. 可直接贴进 SDXL 工作流的推荐 prompt 骨架

### 5.1 结构

```
[质量/风格前缀] , [主体与身份] , <相机段> , [场景/光照/风格] , [镜头/景深附加]
```

`<相机段>` 就是本节点 `positive` 输出，**原样插入，不要再包一层括号**（节点已输出 `(phrase:w)`；再包会变 `((phrase:1.1))`，手抖写成 `((phrase))` 就成 1.21）。

### 5.2 模板 A：人像 / 中景（最常用）

正向：
```
masterpiece, best quality, 1girl, solo, silver hair, red eyes, black jacket, <相机段>, city street at night, neon signs, cinematic lighting, depth of field, 85mm lens
```
负向：
```
<相机镜像段>, lowres, bad anatomy, bad hands, extra digits, watermark, text, jpeg artifacts
```
相机段示例（`pos_x=0, pos_y=0.3, pos_z=0.1`）：
```
(front view:1.10), (high angle shot, from above:1.13), (medium shot:1.00)
```
注意 `medium` 打出来是裸词（w=1.00 无括号），这是 SPEC 2.2 的正确行为，不是漏了权重。

### 5.3 模板 B：全身 / 远景

```
正向: masterpiece, best quality, 1girl, full figure, silver hair, black coat, <相机段>, empty street, dramatic sky, wide composition, cinematic
负向: <相机镜像段>, cropped, out of frame, bad proportions, extra limbs, watermark
相机段: (three-quarter view from the right:1.13), (eye level shot:1.00), (full body shot:1.17)
```

### 5.4 模板 C：特写 / 情绪

```
正向: masterpiece, best quality, 1girl, face focus, detailed eyes, <相机段>, soft rim light, bokeh, film grain
负向: <相机镜像段>, full body, wide shot, bad face, deformed eyes, watermark
相机段: (front view:1.15), (low angle shot, from below, looking up:1.20), (close-up, portrait shot:1.10)
```

### 5.5 贴之前自查清单

- [ ] 相机段落在**前 40 token 内**（§1.4）；超了就把场景词往后挪。
- [ ] 正向整体不超 **75 token**（一块）；超了就确认第 1 块里没有半截相机短语。
- [ ] 一个括号组里**只有一个**末尾冒号。
- [ ] 没有 `[...]`。
- [ ] 没有无权重括号 `(word)`（那是 ×1.1，会悄悄改语义；要中性就写裸词）。
- [ ] 负向里没有重复正向的词（重复会让 CFG 打架，表现为构图抖动）。

---

## 6. 实验设计（Lead 该跑的对照 + 判读标准）

> 目标：(a) 方位词真换方向；(b) 权重曲线在哪个点开始劣化；(c) 负向镜像是否必要。
> 固定：同 checkpoint、同 seed、同采样器/步数/cfg、同分辨率、同非相机 prompt 部分。**一次只动一个变量。**

### 6.1 矩阵总表

| 组 | 变量 | 取值 | 图数 | 目的 |
|---|---|---|---|---|
| E1 | 方位 8 档（同 seed） | `pos_x` = −1, −0.75, −0.5, −0.25, 0, 0.25, 0.5, 0.75 | 8 | 8bin 每档方向肉眼可见地变（SPEC 验收 3） |
| E2 | 斜侧双档 | `pos_x` = 0.125, 0.375（frac=0.5 接缝） | 2 | 双档混合不产生"双脸/双身" |
| E3 | 高度 3 档 | `pos_y` = +0.6, 0, −0.6 | 3 | high/eye/low |
| E4 | 高度极角 5 档 | `pos_y` = +0.85, −0.85（`5bin`） | 2 | bird/worm + `az_gate` 归零 |
| E5 | 距离 6 档 | `pos_z` = 0.85, 0.45, 0.1, −0.15, −0.5, −0.85 | 6 | 6 档跳变而非连续推拉 |
| E6 | 权重梯度 | `raw` 模块，raw = 0.8/1.0/1.2/1.4/1.6，固定 front+eye+medium | 5 | 找劣化阈值（预期 1.4 起劣化，1.6 明显噪） |
| E7 | 曲线对比 | 同 raw（raw=5）下 tanh vs linear vs raw | 3 | tanh 是否"温和但够用" |
| E8 | 负向开关 | `neg_strength` = 0 / 1.0（`pos_x=0.5`） | 2 | 侧面最容易跑回正脸，验证镜像必要性 |
| E9 | 4bin vs 8bin | 两模式，`pos_x=0.25` | 2 | 4bin 是否出现方向反（原仓库 left/right 映射） |
| E10 | token 溢出 | 长前缀(72) + rich vs 短前缀 + compact | 2 | §1.5 的溢出在画面上是否可辨 |

合计 35 张。**先跑 E1+E6+E8（15 张）**，这三组决定设计成不成立。

### 6.2 单组参数

```
E1: pos_x∈{-1,-0.75,-0.5,-0.25,0,0.25,0.5,0.75}, pos_y=0, pos_z=0, roll=0,
    strength=1.0, weight_mode=tanh, azimuth_mode=8bin, elevation_mode=3bin, neg_strength=1.0
E3: pos_y∈{+0.6,0,-0.6}, pos_x=0, pos_z=-0.15, 其余同 E1
E5: pos_z∈{0.85,0.45,0.1,-0.15,-0.5,-0.85}, pos_x=0, pos_y=0, 其余同 E1
E6: 固定字符串灌入 CLIPTextEncode：0.8→(front view:0.80) ... 1.6→(front view:1.60)，其余轴固定裸词
E8: pos_x=0.5, pos_y=0, pos_z=-0.15, neg_strength=0 与 1.0，其余相同
```

> E6 的可执行性注意：SPEC 的 `raw` 曲线是"raw 直通并 clamp 到 [0.05,2.0]"，而 raw 来自轴公式，要精确得到 1.4/1.6 最省事的是**直接在测试里灌固定字符串**绕过节点。别为了实验去改节点语义。

### 6.3 判读标准（可勾选）

| 维度 | 通过 | 存疑 | 失败 |
|---|---|---|---|
| 方位 | 8 张能按 45° 顺序看出鼻尖/肩线朝向变化，左右不反 | 相邻两档几乎一样（>4 档才变） | 任一档方向与预期相反 |
| 斜侧双档 | 0.125/0.375 呈现"介于两档之间" | 出现两张脸/两个身体融合 | 退化成纯正面或纯侧面 |
| 高度 | 俯视看到头顶/地面，仰视看到下颌/天空 | 只有轻微透视变化 | 无变化 |
| 距离 | 6 档 6 个不同取景范围 | 相邻档重叠（阈值写错） | 某档缺失或重复 |
| 权重 | 1.2 明显强于 1.0，1.4 仍干净 | 1.4 轻微纹理劣化 | 1.2 就出噪点 |
| 负向 | 开负向后侧面更"侧"（不是 3/4） | 差异不明显 | 开负向后构图跑偏（词选错） |
| token 溢出 | 长前缀+rich 与 短前缀+compact 差异可辨 | 差异很小 | rich 版明显失控（溢出把主体切了） |

**噪点判定（不靠感觉）**：满足任一条即"劣化"——
1. 皮肤/布料出现非语义块状纹理（贴图感）；
2. 面部结构错乱（多眼、塌鼻、五官错位）而 prompt 未变；
3. 高频彩色杂点（不是 film grain 的均匀颗粒）；
4. 同 seed 下 1.0 干净、1.4 出现上述任一条 → 记为该词表的劣化阈值。

### 6.4 记录表模板（建 csv）

| 图 id | 组 | pos_x | pos_y | pos_z | roll | strength | weight_mode | az_mode | elev_mode | neg_strength | seed | cfg | 方位 | 高度 | 距离 | 噪点 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

---

## 7. 风险与未验证项

| 项 | 状态 | 影响 | 建议 |
|---|---|---|---|
| `from front` 在 SDXL 上"弱" | 推理性结论 + 上游作者间接佐证，未做 A/B 出图 | 若差异不大，词表改动收益变小 | E1 加一组 `front` vs `from front`（+2 张） |
| 劣化阈值 1.4/1.5 | 机制清楚，具体阈值依赖 checkpoint 与 cfg | 某些微调模型可能 1.3 就崩 | E6 若 1.4 已崩，把默认 `weight_max` 降到 1.3 |
| `cut_to` 双编码器块数对齐 | 源码确认（`comfy/sdxl_clip.py:61`），未测"L 比 G 多一块"的实际后果 | 极端长 prompt 下 L 最后一块可能被丢 | 不属本任务；提醒 Lead 别在相机段外写超长 prompt |
| 距离轴"档内不平滑" | SPEC 明确保留跳变 | 用户觉得滑块后半段无响应 | 文档已说明，README 可补一句 |
| SPEC 2.1 校验值 0.92 | **判定为笔误，应为 0.93**（§9.2） | task-1 的断言测试会挂 | 把 SPEC 51 行 `0.5→0.92` 改 `0.5→0.93`，或测试用 `abs(diff)<0.005` 容差 |
| `canted camera` 的 BPE 浪费 | 实测 10 token（`can`+`ted`） | 每条 dutch 多 1~2 token | 默认值照 SPEC；文档给 `_cfg_json` 覆盖示例 |
| `cowboy shot` 是否被 SDXL 吃进 | 社区常用但无权威语料证据 | 牛仔镜头可能弱响应 | 已用 `mid-thigh up` 兜底 |

---

## 8. 参考来源

**本机源码（最强证据）**

- `D:/QUARK/ComfyUI-aki-v3.2/ComfyUI/comfy/sd1_clip.py` —— `:29-80` 权重外插数学；`:321-347` `parse_parentheses`；`:349-367` `token_weights`；`:369-377` `escape_important`；`:488-546` tokenizer 默认参数；`:577-679` `tokenize_with_weights`（77/75 分块）
- `D:/QUARK/ComfyUI-aki-v3.2/ComfyUI/comfy/sdxl_clip.py` —— `:5-33` 双 tokenizer；`:41-62` 双编码器拼接与 pooled
- `D:/QUARK/ComfyUI-aki-v3.2/ComfyUI/comfy/clip_model.py` —— `:174-179` 因果掩码
- `D:/QUARK/ComfyUI-aki-v3.2/ComfyUI/comfy/sd.py` —— `:410-441` `encode_from_tokens` / `encode`
- `D:/QUARK/_repo2/Comfyui-Anima_camera_angle/camera_angle_node.py` —— 原节点权重公式与 danbooru 词表（`:8-40`、`:145-199`）
- `D:/QUARK/_repo2/Comfyui-Anima_camera_angle/README.md:86` —— 上游作者自述"权重过高出噪点"

**外部资料**

- SDXL 论文 arXiv:2307.01952 —— "a larger cross-attention context as SDXL uses a second text encoder"：https://arxiv.org/abs/2307.01952
- HF Diffusers《Prompting / Prompt weighting》—— `(cat)`=1.1、`((cat))`=1.21、`(cat:1.5)`=1.5 的官方口径，并指出 diffusers 靠 sd_embed/compel 做权重：https://huggingface.co/docs/diffusers/using-diffusers/weighted_prompts
- `xhinker/sd_embed` —— 长 prompt 加权 embedding，明确支持 SD1.5/SDXL 无限长度 + A1111 兼容语法：https://github.com/xhinker/sd_embed
- AUTOMATIC1111 wiki《Features → Attention/emphasis》—— `[word]`=÷1.1、`(word:0.25)`=÷4、`\(\)` 转义（对照 ComfyUI 缺 `[]` 语义）：https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/Features
- PixAI 社区《SDXL Prompt Weights & Tag Syntax》—— 社区经验口径（非权威，仅佐证）：https://blog.pixai.art/en/pixai-sdxl-prompting-guide-essential-tips-tricks/

---

## 9. 附录：可复跑脚本与复算

### 9.1 解析器复刻（与 `comfy/sd1_clip.py:321-367` 逐行一致）

```python
import re
from transformers import CLIPTokenizer
tok = CLIPTokenizer.from_pretrained(r"D:/QUARK/ComfyUI-aki-v3.2/ComfyUI/comfy/sd1_tokenizer")

def parse_parentheses(string):          # comfy/sd1_clip.py:321
    result, current_item, nesting_level = [], "", 0
    for char in string:
        if char == "(":
            if nesting_level == 0:
                if current_item: result.append(current_item); current_item = "("
                else: current_item = "("
            else: current_item += char
            nesting_level += 1
        elif char == ")":
            nesting_level -= 1
            if nesting_level == 0: result.append(current_item + ")"); current_item = ""
            else: current_item += char
        else: current_item += char
    if current_item: result.append(current_item)
    return result

def token_weights(string, current_weight):   # comfy/sd1_clip.py:349
    a = parse_parentheses(string); out = []
    for x in a:
        weight = current_weight
        if len(x) >= 2 and x[-1] == ')' and x[0] == '(':
            x = x[1:-1]; xx = x.rfind(":"); weight *= 1.1
            if xx > 0:
                try: weight = float(x[xx+1:]); x = x[:xx]
                except: pass
            out += token_weights(x, weight)
        else: out += [(x, current_weight)]
    return out
```

真实输出（可复现）：

```
=== token_weights behaviour ===
'(from behind:1.20), (medium shot:1.00)' -> [('from behind', 1.2), (',', 1.0), ('medium shot', 1.0)]
'(dutch angle)'                          -> [('dutch angle', 1.1)]
'((dutch angle))'                        -> [('dutch angle', 1.21)]
'((dutch angle:1.30))'                   -> [('dutch angle', 1.3)]
'(a:1.2, b:1.3)'                         -> [('a:1.2, b', 1.3)]        <- 坏例：数字残留
'(side view, profile view:1.25)'         -> [('side view, profile view', 1.25)]
'[dutch angle]'                          -> [('[dutch angle]', 1.0)]   <- 方括号无权重
'(front view:1.05) (eye level shot)'     -> [('front view', 1.05), ('', 1.0), ('eye level shot', 1.1)]
'(high angle shot, from above:1.30)'     -> [('high angle shot, from above', 1.3)]

=== bracket token cost ===
dutch angle                       2 tokens ['dutch','angle']
[dutch angle]                     4 tokens ['[','dutch','angle',']']
dutch angle, canted camera        6 tokens ['dutch','angle',',','can','ted','camera']
```

### 9.2 `sdxl_weight` 复算（SPEC 2.1 tanh）

```python
def sdxl_weight(raw, strength=1.0):     # curve="tanh"
    raw = max(1e-3, float(raw)); S = 0.55 * float(strength); K = 3.3
    if raw >= 1.0: w = 1.0 + S * (raw - 1.0) / (raw - 1.0 + K)
    else:          w = 1.0 - S * (1.0 - raw) / ((1.0 - raw) + K)
    return round(max(0.05, min(2.0, w)), 2)
```

| raw | 0.1 | 0.3 | 0.5 | 0.7 | 0.9 | 1.0 | 1.5 | 2 | 3 | 4 | 5 | 6 | 8 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 复算 w | 0.88 | 0.90 | **0.93** | 0.95 | 0.98 | 1.00 | 1.07 | 1.13 | 1.21 | 1.26 | 1.30 | 1.33 | 1.37 | 1.40 |

与 SPEC 51 行校验值对照：`10→1.40` ✓、`5→1.30` ✓、`2→1.13` ✓、`1→1.00` ✓、`0.1→0.88` ✓、**`0.5→0.92` ✗（应为 0.93）**。

### 9.3 分块模拟（同 `comfy/sd1_clip.py:628-674` 逻辑，按逗号切组）

```python
def chunks(text):
    groups = []
    for piece in text.split(","):
        piece = piece.strip()
        if piece: groups.append(list(tok(piece)["input_ids"][1:-1]))
    b = [["<bos>"]]
    for g in groups:
        while g:
            if len(g) + len(b[-1]) > 76:          # 77 - 1(has_end_token)
                rem = 77 - len(b[-1]) - 1
                b[-1].extend(map(str, g[:rem])); g = g[rem:]
                b[-1].append("<eos>"); b.append(["<bos>"])
            else:
                b[-1].extend(map(str, g)); g = []
    b[-1].append("<eos>")
    return b
```

实测：

```
A 短前缀 (5 主体词 + 3 段相机)   -> 1 chunk, 38/77 slots
B 中前缀 (10 词 + 3 段相机)      -> 1 chunk, 45/77 slots
C 长前缀 (16 词 + 3 段相机)      -> 1 chunk, 66/77 slots   <- 已贴边
D 长前缀(72 tok) + rich 相机(53) -> 2 chunks: 75 + 27
```

---

## 10. v2 增补：光学、倾斜、构图、主体朝向、极点消歧

> 本节对应 SPEC 第 7–9 节与 `core/camera_core.py` v2 实现。所有数值均由 `compute_camera()` 实跑得出（脚本见文末），不是推演。

### 10.1 焦距 → FOV → 词条（`focal`，8–400mm）

水平 FOV 用 36mm 画幅算：`fov = 2·atan(36/(2f))`。档位由 `LENS_BANDS` 决定，**区间左闭右开**，超出 135 归 135 档：

| focal 输入 | 命中档位 | tanh 权重 | 水平 FOV | 正向词条 |
|---|---|---|---|---|
| 14 / 18 | `lens14` | 1.30 | 104.25° / 90.00° | ultra wide angle lens, 14mm, strong perspective distortion, exaggerated foreshortening, deep depth of field |
| 24 | `lens24` | 1.22 | 73.74° | wide angle lens, 24mm, wide perspective, deep depth of field, environmental context |
| 35 | `lens35` | 1.11 | 54.43° | 35mm lens, natural perspective, environmental portrait |
| 50 | `lens50` | **1.00（裸写）** | 39.60° | 50mm lens, standard perspective, natural proportions |
| 85 | `lens85` | 1.26 | 23.91° | 85mm lens, telephoto compression, shallow depth of field, background blur, bokeh |
| 135+ | `lens135` | 1.30 | 15.19° | 135mm telephoto lens, strong spatial compression, creamy bokeh, isolated subject |

三点必须记住：

1. **50mm 是中性档，权重恰好 1.0，所以输出裸短语不带括号**。任何"权重必须带括号"的断言都会在这里挂 —— 这不是 bug。
2. 超广角/长焦用 1.30 而不是 1.5+：>1.5 沿空 prompt 方向外插，必出噪点（§3.3）。
3. 焦距**不改**机位（pos_x/y/z），只改透视与景深语义 —— 想真的"退后拉近"要同时动 `pos_z`。`LENS_WEIGHT` 与 `LENS_BANDS` 在 `core/camera_core.py` 与 `js/camera_shared.js` 里必须逐字一致（`tools/parity_check.py` 会查）。

### 10.2 倾斜 roll（±45°）

`roll_deg = roll × 45`，阈值 `|roll| > 0.25` 才发声（11.25°，低于此值 SDXL 基本看不出来，白占 token）：

| roll | 角度 | 正向词条 | 权重 |
|---|---|---|---|
| 0.26 | 11.7° | dutch angle, canted framing, tilted horizon | 1.03 |
| 0.5 | 22.5° | 同上 | 1.13 |
| 1.0 | 45.0° | 同上 | 1.30 |
| −0.5 | −22.5° | dutch angle, reverse canted framing, tilted frame | 1.13 |
| −1.0 | −45.0° | 同上 | 1.30 |

负值是**反向倾斜**，不是"不倾斜"。`roll` 只进正向词与负向镜像（`tilted horizon` 在负向里做对照），不影响方位/高度档位。

### 10.3 构图偏移 pan（三分法）

`pan_x / pan_y` 描述的是**相机相对主体的平移**，不是主体自己动：

| 参数 | 语义 | 正向词条 |
|---|---|---|
| `pan_x = +0.5` | 相机右移 → 主体落到**左**三分线，右侧留白 | subject placed on the left third, rule of thirds, off-center composition, negative space on the right |
| `pan_x = −0.5` | 相机左移 → 主体落到**右**三分线 | subject placed on the right third, rule of thirds, off-center composition, negative space on the left |
| `pan_y = +0.4` | 主体压低，上方留白，地平线偏低 | extra headroom above, subject in the lower third, low horizon line |
| `pan_y = −0.4` | 主体抬高，前景引线 | subject in the upper third, foreground leading space |
| 两项都 `|v| < 0.12` | 居中构图 | **默认不发词**（`subject_center` 只在 `framing.emit_center` 打开时输出） |

居中之所以默认静默：SDXL 对"centered composition"的响应是把主体缩小到画面中央 40% 区域，正常中景会因此变弱；默认不发等于"不做额外干扰"，需要时再开。

### 10.4 主体朝向 / 视线（`subject_yaw` + `gaze_mode`）—— 世界坐标系

`subject_yaw` 是主体在**世界坐标系**里的朝向，与 `studio/web/camera3d.js` 的黄色箭头**同一套角度**（箭头朝 +Z 时 = 0，转 180° = ±1）。`pos_x` 是相机方位（0 正面、±1 背面），两者单位都是「1.0 = 180°」。判定前先求**相对相机的偏航角**：

```
rel_yaw = wrap(subject_yaw - pos_x)        wrap(v) = ((v + 1) % 2 + 2) % 2 - 1
```

| `|rel_yaw|` | 档位 | 词条 | 实测权重 |
|---|---|---|---|
| < 0.28 | `subject_front` | front-facing body, shoulders square to the camera | 裸写（去重时整条不发） |
| 0.28 – 0.72 | `subject_turn` | body turned three-quarters away from the camera | 1.12（0.45）/ 1.15（0.6） |
| > 0.72 | `subject_away` | facing away, back turned to the camera | 1.19（0.9） |

同一组参数在不同机位下的结论（`tests/test_core.py::test_world_yaw_geometry`）：

| pos_x | subject_yaw | rel_yaw | subject | gaze（auto） |
|---|---|---|---|---|
| 0.0 | 0.0 | 0.0 | front（正面机位去重，不发词） | viewer |
| 0.5 | 0.5 | 0.0 | front（去重不发） | viewer |
| **1.0** | **0.0** | **−1.0** | **away（与 from behind 重复，不发 facing away）** | **away** |
| **1.0** | **0.4** | **−0.6** | **turn** | **over_shoulder** |
| 1.0 | −0.45 | 0.55 | turn | over_shoulder |
| 0.0 | 0.9 | 0.9 | away（1.19） | away |

两条去重规则（都是在 `compute_camera` 第 7 段里判的）：

- `dup_front = subject == subject_front and front_az > 0.5` —— 正面机位再说 "front-facing body" 与 `front view` 重复；
- `dup_back = subject == subject_away and behind_az > 0.45` —— 背面机位再说 "facing away" 与 `from behind / back view` 重复。

视线 `gaze_mode`（默认 `auto`，用 `|rel_yaw|` 判定）：

| 条件 | 档位 | 词条 | 权重 |
|---|---|---|---|
| `auto` + `|rel_yaw| < 0.28` | `gaze_viewer` | looking at viewer, direct eye contact | 1.00 裸写 |
| `auto` + `0.28 <= |rel_yaw| <= 0.72` 且 `behind_ratio > 0.45` | `gaze_over_shoulder` | looking back over the shoulder at the viewer | 1.30 raw → **1.05** |
| `auto` + `|rel_yaw| > 0.72` | `gaze_away` | looking away, gaze averted | 1.00 裸写 |
| `camera`（强制） | `gaze_viewer` | 同上 | 1.00 |
| `away`（强制） | `gaze_away` | 同上 | 1.00 |

`behind_ratio` 是机位落在背面扇区的比例：只有"相机在身后 + 人半转身"才成立"回眸"，正面机位下不管 yaw 多大都不该发 over the shoulder。这条规则是被实测逼出来的 —— 正面机位硬发回眸，SDXL 会把人连同背景一起扭头。

**v2.1 修正**：早期实现把 `subject_yaw` 当成"相对相机的偏航角"，于是背面机位（`pos_x=1`）+ `subject_yaw=0` 会输出 `front-facing body` + `looking at viewer` —— 文字说"人正对镜头"，机位词说"相机在背后"，出图变成一张自相矛盾的半身像。现在统一到世界系，3D 视口箭头指哪、词条就是哪。老参数想保持原效果：相对角 `r` 换算成世界角 `wrap(r + pos_x)`。

### 10.5 极点消歧（|pos_y| > 0.75）

两个门控：

```
az_gate(y)   = clamp((1 - |y|) / 0.10, 0, 1)     # 方位词随高度衰减
pole_gate(y) = clamp((1 - |y|) / 0.25, 0, 1)     # 极点区内整体衰减
有效门 = az_gate × pole_gate；< 0.12 直接抑制（azimuth_suppressed=True，连 negative 一起关）
```

实测（`azimuth_mode=8bin`）：

| pos_y | pos_x | pole_gate | 发方位词 | 说明 |
|---|---|---|---|---|
| 0.75 | 0 | 1.00 | front | 极点边界，尚未衰减 |
| 0.80 | ±1.0 | 0.80 | back | 衰减到 0.8，词还在但要屏蔽互斥词 |
| 0.95 | 0 | 0.20 | **无**（suppressed） | 有效门 0.2×0.2=0.04 < 0.12 |
| 1.00 | 0 | 0.00 | **无** | 纯垂直机位 |
| −0.8 | ±1.0 | 0.80 | back | 虫瞰同规则 |

高度词（`EL_KEYS`）**不受极点抑制**，py=0.95 仍然发 `high`：极点处"从上往下"这件事本身就是要表达的，被压掉的只是"从哪个方向"。

互斥词屏蔽表 `POLE_BLOCK_TOKENS`（`core/camera_core.py:153-159`）：

| 极点 | 从方位词里剔除 | 原因 |
|---|---|---|
| 鸟瞰（py > 0.75） | from behind, seen from behind, back view, front view, straight-on shot, facing the camera, eye level shot, looking at viewer, direct eye contact | 实测 py=0.8 且 |px|=1.0 时会出现 "from behind" + "top-down view" 并存 → 出图变成半背半俯的空镜 |
| 虫瞰（py < −0.75） | eye level shot, top-down view, looking down at viewer, from above, high angle, bird's-eye view | 同上，反方向 |

这张表是 v2 里唯一"靠实测反复修出来"的常量，改它必须跑 `tools/sweep_camera.py --mode elevation --tag v2` 重扫。

**极点锁存 `POLE_LATCH`（v2.1）**

`elevation_mode="3bin"` 只有 high / eye / low 三档，`pos_y=0.95` 也只给 `high angle shot` —— 出图是「稍高的平视」，
不是真顶视。所以 v2.1 在 `|pos_y| > 0.9`（`POLE_LATCH`，严格大于）时把 high/low 升级成 bird/worm：

| pos_y | 3bin 改前 | 3bin 改后（v2.1） | 5bin |
|---|---|---|---|
| 0.80 | high angle shot | high angle shot | high angle shot |
| 0.90 | high angle shot | high angle shot（阈值本身不锁） | bird's-eye view |
| 0.95 | high angle shot | bird's-eye view / top-down view / aerial shot / from above | bird |
| 1.00 | high angle shot | 同上 + pole_gate=0 | 同上 |

锁存后 `POLE_BLOCK_TOKENS["bird"]` 生效，方位段的 front view / facing the camera 会被剔除，视线走 `POLE_GAZE`。

**视线改写 `POLE_GAZE`（`core/camera_core.py`，同步在 `js/camera_shared.js`）**

屏蔽表把 "looking at viewer / direct eye contact" 从**方位**词里剔掉了，但视线段是独立生成的，py=0.95 仍会补上一句 "looking at viewer"，于是模型照样画一个正面站立的人像（实测 v2_pole_top：py=0.95 出的是背面回眸站姿，不是顶视）。修法是给极点区换视线短语，而不是删掉视线：

| 极点 | 原短语 | 改写为 |
|---|---|---|
| 鸟瞰（py > 0.75） | looking at viewer, direct eye contact | looking up at the viewer |
| 虫瞰（py < -0.75） | 同上 | looking down at the viewer |

判定按 **py 的正负**，不按 `ekey`：`elevation_mode` 默认 3bin 时 `ekey` 是 `high`/`low`，只有 5bin 才是 `bird`/`worm`，用 `ekey` 查表在默认档会静默失效（踩过一次）。

改完重跑：`python tests/test_core.py`（`test_pole_conflicts` 断言 py=0.95 -> looking up at the viewer、py=-0.95 -> looking down at the viewer、py=0.5 保持原短语）、`python tools/parity_check.py`（比对 `POLE_GAZE` 2 条），然后**重启 ComfyUI** 才生效 —— 节点进程里 `camera_core` 已被 import 缓存，改文件不重启不会变。

### 10.6 `camera_info` 新增字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `fov_deg` | float | 水平 FOV，由 focal 算 |
| `focal` | float | 输入焦距 |
| `lens` | str | 命中档位（lens14 … lens135） |
| `roll_deg` | float | roll × 45 |
| `pole_gate` | float | 极点门控值 0–1 |
| `azimuth_suppressed` | bool | 是否因极点压掉方位词 |
| `behind_ratio` | float | 机位在背面扇区的比例（回眸判据） |
| `framing` | list[str] | 命中的构图档位 |
| `subject` / `gaze` | str | 命中的朝向/视线档位 |
| `subject_yaw` | float | 回显输入（世界坐标系朝向） |
| `rel_yaw` | float | `wrap(subject_yaw - pos_x)`，朝向/视线判定的真正依据 |
| `pan` | list[float] | 回显输入 `[pan_x, pan_y]` |
| `tokens` | dict | `{positive, negative, total, limit:75, warn:65, level}`，相机段 CLIP token 预算（`clip_tokens` 估算，≈4.5 字符/token） |
| `bins` | list | `[[key, ratio, kind]]`，kind ∈ az/el/di/lens/tg/frame/subject/gaze |

`bins` 带 kind 是 v2 的破坏性变更：老调用方按 `bins[i][0]` 取 key 仍然对，但"`bins` 只有方位"的假设不再成立（测试里用 `az_bins()` 过滤 `kind == "az"`）。

### 10.7 v2 的 token 预算（65 标黄 / 75 截断）

相机段是**一段独立文字**，塞进用户的 prompt 头部，所以必须自己看着别超 Chunk 0 的 75 个内容 token（77 减去两个起止符）。三个地方用同一个口径：

| 位置 | 实现 | 显示 |
|---|---|---|
| 节点内 3D 视口 | `js/sdxl_camera_widget.js:454` → `budgetOf(positive, negative)` | `44 / 75 tok · N 35`，>65 橙 `#d08b23`，>75 红 `#d0453a` |
| Studio 前端 | `studio/web/app.js` 的 token 面板（优先用后端 `tokens.positive`） | 同上 |
| Python / 后端 | `core/camera_core.py::clip_tokens` + `token_budget`；`studio/server.py` 在 `/api/preview` 的 `tokens` 里补 `limit/warn/level` | `tokens.level ∈ ok/warn/danger` |

`clip_tokens` 是**零依赖估算**（不加载 CLIP 分词器）：先剥掉 `(tag:1.40)` 的权重语法，再按逗号切段、按空白切词，每词 `ceil(len*2/9)` 个 token。误差约 ±10%（"front view" 真分词 2、估算 3;整段 40 实测 vs 44 估算），用来判断"要不要收手"够用。要精确值：`studio/tools/e2e_test.py` 或后端在 ComfyUI python 下会切到真 `SDTokenizer`（`method="tokenizer"`）。

实测参考（真分词器）：



| 场景 | 正向 / 负向 token | 说明 |
|---|---|---|
| 默认（正面平视中景 50mm） | 40 / 38 | 光学 50mm 裸写省 token |
| `pos_x=1`（背面） | 49 / 44 | 背面词组更长（from behind / back view / seen from behind） |
| 鸟瞰极限俯视 | 37 / 23 | 方位词被极点抑制 + 互斥词剔除，负向同时变短 |

结论不变（§1.4）：相机段整体仍在 75 token 以内，一块够用；但**极点 + 长焦 + 构图 + 朝向同时开**会到 ~70，逼近边界，此时应关掉不用的轴（`strength` 不动，直接让对应参数归零）。

**为什么相机段要盯着 75**：它和用户的 `extra_positive`、LoRA 触发词、画风词共用 Chunk 0。超了不是报错，是 CLIP 静默截断 —— 被截掉的往往正好是排在后面的 `gaze` / `lens` 这些"贵但不显眼"的词，于是"我明明设了 85mm 却没有虚化"。看到橙色就删词，别加词。

### 10.8 复跑

```bash
cd D:/QUARK/sdxl_camera_angle
PYTHONIOENCODING=utf-8 python tests/test_core.py          # 17 项单测（含世界系 rel_yaw / token 预算）
PYTHONIOENCODING=utf-8 python tools/parity_check.py       # Python ↔ JS 常量逐字一致（含 CLIP 阈值 65/75）
PYTHONIOENCODING=utf-8 python tools/validate_workflow.py  # 工作流 widgets 与节点 INPUT_TYPES 对齐
PYTHONIOENCODING=utf-8 python tools/sweep_camera.py --mode elevation --tag v2_fix   # 极点重扫（要 ComfyUI 在线）
```
