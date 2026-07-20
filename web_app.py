#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import json
import os
import socket
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from pack_workflow import apply_parameter_overrides, load_config, parse_request, trigger_build


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CONFIG_PATH = BASE_DIR / "config.json"
BUILD_LOCK = threading.Lock()


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler: SimpleHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    return json.loads(raw.decode("utf-8"))


class JenkinsHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            config = load_config(CONFIG_PATH)
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "jenkins_url": config["jenkins"]["base_url"],
                    "jobs": config["jobs"],
                    "login_mode": "复用本地登录态",
                },
            )
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = read_json_body(self)
            if parsed.path == "/api/parse":
                self.handle_parse(payload)
                return
            if parsed.path == "/api/build":
                self.handle_build(payload)
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "message": "接口不存在"})
        except Exception as exc:
            json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    def handle_parse(self, payload: Dict[str, Any]) -> None:
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ValueError("请输入打包需求文案。")

        config = load_config(CONFIG_PATH)
        parsed = parse_request(text, config)
        json_response(self, HTTPStatus.OK, {"ok": True, "data": asdict(parsed)})

    def handle_build(self, payload: Dict[str, Any]) -> None:
        text = str(payload.get("text", "")).strip()
        headed = bool(payload.get("headed", False))

        if not text:
            raise ValueError("请输入打包需求文案。")

        if not BUILD_LOCK.acquire(blocking=False):
            raise RuntimeError("已有打包任务正在进行，请等待完成后再提交。")

        try:
            config = load_config(CONFIG_PATH)
            parsed = parse_request(text, config)
            overrides = payload.get("parameters")
            if isinstance(overrides, dict) and overrides:
                parsed = apply_parameter_overrides(parsed, overrides)

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                trigger_build(config=config, parsed=parsed, headed=headed)
        finally:
            BUILD_LOCK.release()

        json_response(
            self,
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Jenkins 构建已提交。",
                "data": asdict(parsed),
                "logs": output.getvalue(),
            },
        )


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return ""


def main() -> None:
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8899"))
    server = ThreadingHTTPServer((host, port), JenkinsHandler)
    print(f"页面已启动：http://127.0.0.1:{port}")
    lan_ip = get_lan_ip()
    if lan_ip:
        print(f"局域网访问：http://{lan_ip}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
