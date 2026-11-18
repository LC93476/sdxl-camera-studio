# -*- coding: utf-8 -*-
"""SDXL 相机工作台 · ComfyUI 桥

只做两件事：**建图** 与 **收发**。

建图（不读 SDXL.CameraAngle.json、不依赖任何插件节点是否安装）：
    1 CheckpointLoaderSimple
    2 LoraLoader（仅在 lora_name 非空时插，接 1.model / 1.clip）
    3 CLIPTextEncode 正向     4 CLIPTextEncode 负向
    5 EmptyLatentImage
    6 KSampler  ->  7 VAEDecode  ->  8 SaveImage

收发：POST /prompt、GET /history/{id}、GET /queue、GET /view、
GET /object_info/*、POST /interrupt。

所有请求都有超时：连接 3s、读 15s。失败统一抛 ComfyError（上层决定降级还是 503），
绝不让 ComfyUI 离线把进程带到异常里。
"""

import http.client
import json
import os
import socket
import urllib.parse

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 15.0
CLIENT_ID = "camera-studio"

# 离线兜底（/object_info 拿不到时给前端一个能用的下拉框）
FALLBACK_SAMPLERS = [
    "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde",
    "dpmpp_sde", "dpmpp_2s_ancestral", "uni_pc", "ddim", "lms", "heun",
]
FALLBACK_SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"]


class ComfyError(Exception):
    """ComfyUI 通讯/协议错误（离线、超时、4xx/5xx、非 JSON）。"""


# 兼容别名：外部文档里也叫 ComfyBridgeError，指向同一个类
ComfyBridgeError = ComfyError


# ══════════════════════ 连接（分离的连接/读取超时） ══════════════════════
class _HTTPConn(http.client.HTTPConnection):
    def __init__(self, host, port):
        http.client.HTTPConnection.__init__(self, host, port, timeout=READ_TIMEOUT)

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), CONNECT_TIMEOUT)
        self.sock.settimeout(READ_TIMEOUT)
        tunnel = getattr(self, "_tunnel_host", None)
        if tunnel:
            self._tunnel()


class _HTTPSConn(http.client.HTTPSConnection):
    def __init__(self, host, port):
        http.client.HTTPSConnection.__init__(self, host, port, timeout=READ_TIMEOUT)

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), CONNECT_TIMEOUT)
        self.sock.settimeout(READ_TIMEOUT)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        tunnel = getattr(self, "_tunnel_host", None)
        if tunnel:
            self._tunnel()


# ══════════════════════ 桥 ══════════════════════
class ComfyBridge(object):
    def __init__(self, base_url="http://127.0.0.1:8188"):
        self.base = ""
        self.scheme = "http"
        self.host = "127.0.0.1"
        self.port = 8188
        self.prefix = ""
        self.set_base(base_url)

    # ---------- 地址 ----------
    def set_base(self, base_url):
        base = str(base_url or "http://127.0.0.1:8188").strip().rstrip("/")
        if "://" not in base:
            base = "http://" + base
        parsed = urllib.parse.urlparse(base)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https"):
            scheme = "http"
        self.base = base
        self.scheme = scheme
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if scheme == "https" else 80)
        self.prefix = (parsed.path or "").rstrip("/")
        return self.base

    # ---------- 原始请求 ----------
    def _request(self, path, payload=None, method=None):
        """JSON 收发：返回 response body bytes；任何失败抛 ComfyError。"""
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if method is None:
                method = "POST"
        return self._raw(path, body, headers, method)

    def _raw(self, path, body=None, headers=None, method=None):
        """原样发字节（multipart 上传走这里）：返回 body bytes；失败抛 ComfyError。"""
        conn_cls = _HTTPSConn if self.scheme == "https" else _HTTPConn
        conn = None
        try:
            conn = conn_cls(self.host, self.port)
            hdrs = {
                "Accept": "application/json",
                "User-Agent": "sdxl-camera-studio/1.0",
            }
            hdrs.update(headers or {})
            if method is None:
                method = "POST" if body is not None else "GET"
            conn.request(method, self.prefix + path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                snippet = data.decode("utf-8", "replace").strip().replace(chr(10), " ")[:300]
                raise ComfyError("ComfyUI HTTP %d: %s" % (resp.status, snippet or path))
            return data
        except ComfyError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise ComfyError("ComfyUI 超时(%ss): %s %s" % (READ_TIMEOUT, self.base, exc))
        except (OSError, http.client.HTTPException) as exc:
            raise ComfyError("ComfyUI 连不上 %s: %s" % (self.base, exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _json(self, path, payload=None, method=None, allow_empty=True):
        raw = self._request(path, payload, method)
        if not raw:
            return {} if allow_empty else None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ComfyError("ComfyUI 返回了非 JSON: %s" % path)

    # ---------- 探测 ----------
    def reachable(self):
        try:
            self._request("/queue")
            return True
        except ComfyError:
            return False

    def system_stats(self):
        try:
            return self._json("/system_stats")
        except ComfyError:
            return {}

    # ---------- 模型清单 ----------
    @staticmethod
    def empty_models(reason=""):
        return {
            "checkpoints": [],
            "loras": [],
            "samplers": [],
            "schedulers": [],
            "comfy_reachable": False,
            "error": reason or "",
        }

    def models(self):
        """在线：拉 /object_info；离线：空数组 + comfy_reachable False。"""
        if not self.reachable():
            return self.empty_models("ComfyUI 未启动")
        out = {
            "checkpoints": [],
            "loras": [],
            "samplers": list(FALLBACK_SAMPLERS),
            "schedulers": list(FALLBACK_SCHEDULERS),
            "comfy_reachable": True,
            "error": "",
        }
        try:
            info = self._json("/object_info/CheckpointLoaderSimple")
            req = (info.get("CheckpointLoaderSimple") or {}).get("input", {}).get("required", {})
            names = (req.get("ckpt_name") or [[]])[0]
            if isinstance(names, list):
                out["checkpoints"] = [str(n) for n in names]
        except ComfyError as exc:
            out["error"] = "checkpoints: %s" % exc
        try:
            info = self._json("/object_info/LoraLoader")
            req = (info.get("LoraLoader") or {}).get("input", {}).get("required", {})
            names = (req.get("lora_name") or [[]])[0]
            if isinstance(names, list):
                out["loras"] = [str(n) for n in names if n != "None"]
        except ComfyError:
            pass
        try:
            info = self._json("/object_info/KSampler")
            req = (info.get("KSampler") or {}).get("input", {}).get("required", {})
            samplers = (req.get("sampler_name") or [None])[0]
            schedulers = (req.get("scheduler") or [None])[0]
            if isinstance(samplers, list) and samplers:
                out["samplers"] = [str(s) for s in samplers]
            if isinstance(schedulers, list) and schedulers:
                out["schedulers"] = [str(s) for s in schedulers]
        except ComfyError:
            pass
        return out

    # ---------- 建图 ----------
    def build_prompt(self, gen, positive, negative):
        """gen = {checkpoint, lora_name, lora_strength, seed, steps, cfg,
        width, height, sampler, scheduler, batch, save_prefix}"""
        ckpt = str(gen.get("checkpoint") or "").strip()
        if not ckpt:
            raise ComfyError("checkpoint 为空")
        lora = str(gen.get("lora_name") or "").strip()
        lora_strength = _num(gen.get("lora_strength"), 0.8)
        seed = int(_num(gen.get("seed"), 0))
        steps = max(1, int(_num(gen.get("steps"), 28)))
        cfg = _num(gen.get("cfg"), 5.0)
        width = max(64, int(_num(gen.get("width"), 832)))
        height = max(64, int(_num(gen.get("height"), 1216)))
        batch = max(1, min(16, int(_num(gen.get("batch"), 1))))
        sampler = str(gen.get("sampler") or "euler_ancestral")
        scheduler = str(gen.get("scheduler") or "karras")
        prefix = str(gen.get("save_prefix") or "camera") or "camera"

        nodes = {}
        nodes["1"] = {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt},
        }
        model_src = ["1", 0]
        clip_src = ["1", 1]
        if lora:
            nodes["2"] = {
                "class_type": "LoraLoader",
                "inputs": {
                    "model": ["1", 0],
                    "clip": ["1", 1],
                    "lora_name": lora,
                    "strength_model": float(lora_strength),
                    "strength_clip": float(lora_strength),
                },
            }
            model_src = ["2", 0]
            clip_src = ["2", 1]
        nodes["3"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive or "", "clip": clip_src},
        }
        nodes["4"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative or "", "clip": clip_src},
        }
        nodes["5"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": batch},
        }
        nodes["6"] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
                "model": model_src,
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["5", 0],
            },
        }
        nodes["7"] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["6", 0], "vae": ["1", 2]},
        }
        nodes["8"] = {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": prefix, "images": ["7", 0]},
        }
        return nodes

    def build_graph(self, gen, positive, negative):
        """返回 (graph, meta)。

        gen["workflow_template"] 非空 → 走外部工作流模板（studio/template_bridge.py），
        只把相机提示词/尺寸/采样参数注入模板，其余节点原样提交；
        为空 → 用下面这张内置最小图。
        """
        tpl = str((gen or {}).get("workflow_template") or "").strip()
        if tpl:
            try:
                import template_bridge
            except ImportError:
                raise ComfyError("template_bridge 模块缺失（studio/template_bridge.py）")
            try:
                res = template_bridge.build(tpl, gen, positive, negative)
            except template_bridge.TemplateError as exc:
                raise ComfyError("工作流模板不可用: %s" % exc)
            meta = {
                "template": res.get("template") or tpl,
                "template_path": res.get("template_path") or "",
                "warn": list(res.get("warn") or []),
                "positive_node": res.get("positive_node"),
                "negative_node": res.get("negative_node"),
                "sampler": res.get("sampler"),
            }
            return res["prompt"], meta
        return self.build_prompt(gen, positive, negative), {"template": "", "warn": []}

    # ---------- 提交 ----------
    def submit(self, prompt):
        payload = {
            "prompt": prompt,
            "client_id": CLIENT_ID,
            "extra_data": {"source": "sdxl-camera-studio"},
        }
        resp = self._json("/prompt", payload)
        if not isinstance(resp, dict):
            raise ComfyError("ComfyUI /prompt 返回格式异常")
        if resp.get("error") or resp.get("node_errors"):
            detail = resp.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
            raise ComfyError("ComfyUI 拒绝了这个图: %s" % str(detail)[:400])
        prompt_id = resp.get("prompt_id")
        if not prompt_id:
            raise ComfyError("ComfyUI 没有返回 prompt_id: %s" % json.dumps(resp, ensure_ascii=False)[:300])
        return str(prompt_id)

    # ---------- 轮询 ----------
    def history(self, prompt_id):
        """返回该 prompt 的 history 条目；还没有/未知返回 None。"""
        data = self._json("/history/" + urllib.parse.quote(str(prompt_id)))
        if not isinstance(data, dict):
            return None
        entry = data.get(prompt_id)
        if entry is None:
            # 某些版本会把 id 原样返回在别的键上，逐个找
            for key, val in data.items():
                if isinstance(val, dict) and ("outputs" in val or "status" in val):
                    return val
            return None
        return entry if isinstance(entry, dict) else None

    def queue_state(self, prompt_id):
        """返回 (state, position)：('running', 0) / ('queued', n) / (None, 0)。"""
        data = self._json("/queue")
        if not isinstance(data, dict):
            return (None, 0)
        for row in data.get("queue_running") or []:
            if _queue_id(row) == str(prompt_id):
                return ("running", 0)
        for idx, row in enumerate(data.get("queue_pending") or []):
            if _queue_id(row) == str(prompt_id):
                return ("queued", idx + 1)
        return (None, 0)

    # ---------- 取图 ----------
    def view_bytes(self, filename, subfolder="", image_type="output"):
        query = urllib.parse.urlencode({
            "filename": filename,
            "subfolder": subfolder or "",
            "type": image_type or "output",
        })
        return self._request("/view?" + query)

    # ---------- 上传（ControlNet 控制图进 ComfyUI 的 input/） ----------
    def upload_image(self, blob, filename="image.png", subfolder="", image_type="input"):
        """POST /upload/image（multipart/form-data，手工拼，纯标准库）。

        返回 {"name": 实际文件名, "subfolder": ..., "type": ...}；
        离线 / 被拒抛 ComfyError（上层转 503）。
        """
        blob = blob if isinstance(blob, (bytes, bytearray)) else b""
        raw_name = str(filename or "image.png").replace("\\", "/")
        name = os.path.basename(raw_name) or "image.png"
        boundary = "----CameraStudio" + os.urandom(10).hex()
        chunks = []
        form = [("type", image_type or "input")]
        if subfolder:
            form.append(("subfolder", subfolder))
        for key, value in form:
            chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
            chunks.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode("utf-8"))
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
        chunks.append(
            ('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % name).encode("utf-8")
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(bytes(blob))
        chunks.append(b"\r\n")
        chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
        body = b"".join(chunks)
        raw = self._raw("/upload/image", body, {
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(len(body)),
        })
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            raise ComfyError("ComfyUI /upload/image 返回了非 JSON")
        if not isinstance(data, dict):
            raise ComfyError("ComfyUI /upload/image 返回格式异常")
        return {
            "name": str(data.get("name") or name),
            "subfolder": str(data.get("subfolder") or ""),
            "type": str(data.get("type") or image_type or "input"),
        }

    def interrupt(self):
        self._request("/interrupt", {})
        return True

    def queue_clear(self):
        self._request("/queue", {"clear": True})
        return True


# ══════════════════════ 小工具 ══════════════════════
def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _queue_id(row):
    """ComfyUI 队列行：[number, prompt_id, prompt, extra, outputs] 或 dict。"""
    if isinstance(row, (list, tuple)) and len(row) > 1:
        return str(row[1])
    if isinstance(row, dict):
        for key in ("prompt_id", "id"):
            if key in row:
                return str(row[key])
    return ""


def history_images(entry):
    """从 history 条目里抠出 [{filename, subfolder, type}, …]。"""
    out = []
    if not isinstance(entry, dict):
        return out
    outputs = entry.get("outputs") or {}
    if not isinstance(outputs, dict):
        return out
    for _node_id, node_out in outputs.items():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs"):
            for img in node_out.get(key) or []:
                if not isinstance(img, dict):
                    continue
                filename = img.get("filename") or ""
                if not filename:
                    continue
                out.append({
                    "filename": filename,
                    "subfolder": img.get("subfolder") or "",
                    "type": img.get("type") or "output",
                })
    return out


def history_error(entry):
    """history 条目里 status_str == error 时把消息拼出来。"""
    if not isinstance(entry, dict):
        return ""
    status = entry.get("status")
    if not isinstance(status, dict):
        return ""
    messages = []
    for msg in status.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        kind = str(msg[0])
        detail = msg[1]
        if kind.endswith("error"):
            text = ""
            if isinstance(detail, dict):
                text = str(detail.get("exception_message") or detail.get("message") or "")
            messages.append(text.strip()[:300] or kind)
    return " / ".join([m for m in messages if m])


__all__ = [
    "ComfyError", "ComfyBridge", "CONNECT_TIMEOUT", "READ_TIMEOUT", "CLIENT_ID",
    "FALLBACK_SAMPLERS", "FALLBACK_SCHEDULERS", "history_images", "history_error",
]
