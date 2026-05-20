"""MiniCode 的最小 HTTP 网关。

刻意只用 Python 标准库实现，让 Docker 镜像与 console 入口点保持零依赖。
对外暴露：
    - GET /health  健康检查
    - POST /run    单次 headless 执行（请求体 {"prompt": "..."} → JSON 答复）

供平台桥接（Telegram / Discord / Web 等）二次封装使用。
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes]:
    """把 payload 序列化为 (status_code, utf-8 字节)。"""
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


class MiniCodeGatewayHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。仅支持 / 和 /health 的 GET，以及 /run 的 POST。"""

    server_version = "MiniCodeGateway/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """默认抑制访问日志，设置 MINI_CODE_GATEWAY_ACCESS_LOG=1 可开启。"""
        if os.environ.get("MINI_CODE_GATEWAY_ACCESS_LOG") == "1":
            super().log_message(format, *args)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        """发送 JSON 响应（自动设置 Content-Type / Content-Length）。"""
        status_code, body = _json_bytes(payload, status)
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """处理 GET 请求：仅支持 / 和 /health。"""
        if self.path in {"/", "/health"}:
            self._send_json({"ok": True, "service": "minicode-gateway"})
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        """处理 POST 请求：仅支持 /run。

        请求体格式：``{"prompt": "..."}``
        响应：成功 ``{"ok": true, "response": "..."}``，失败 ``{"ok": false, "error": "..."}``。
        """
        if self.path != "/run":
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw) if raw.strip() else {}
            prompt = str(data.get("prompt", "")).strip()
            if not prompt:
                self._send_json({"ok": False, "error": "prompt is required"}, status=400)
                return

            from minicode.headless import run_headless

            self._send_json({"ok": True, "response": run_headless(prompt)})
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            # headless 模块可能因为缺失配置抛 SystemExit，这里把它转成 500 的 JSON 错误返回
            if isinstance(exc, SystemExit):
                message = str(exc) or f"headless exited with status {exc.code}"
                print(f"MiniCode gateway headless exit: {message}", file=sys.stderr)
                self._send_json({"ok": False, "error": message}, status=500)
                return
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def run_gateway() -> None:
    """启动网关服务。监听地址/端口由环境变量控制：

    - MINI_CODE_GATEWAY_HOST（默认 127.0.0.1）
    - MINI_CODE_GATEWAY_PORT（默认 8080）
    """
    host = os.environ.get("MINI_CODE_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("MINI_CODE_GATEWAY_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), MiniCodeGatewayHandler)
    print(f"MiniCode gateway listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_gateway()
