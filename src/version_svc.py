#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/version_svc.py — relay 试点版本服务（纯标准库，DEV-33 sample-app 同构）。

路由：
  GET /health   → 200 "ok"（不触碰版本文件——健康即进程可服务）
  GET /version  → 200 JSON {release_id, source_sha, artifact_digest, built_at}
                  版本证据只读 payload 根 release-info.json，且每次请求对
                  payload 根重算 content-manifest 摘要自校验，不符 → 500
                  （版本被篡改/组装错误的可观测信号，solution §10）
  GET /buildinfo → 200 JSON {release_id, source_sha, built_at} 加运行时字段
                  {service_start_iso, uptime_seconds, python_version}；三元组
                  与 /version 共用同一 release_info() 读取/自校验路径（缺失/
                  不可解析/摘要不符同口径 → 500，其中缺失/不可解析按 api.md
                  返回约定错误对象，/version 仍回原详细错误对象），键按字母序
                  紧凑输出
  其他路径/方法 → 404

摘要口径与 tools/build_artifact.py、entry/relay_trusted_entry.py 一致：
排除 {MANIFEST.json, release-info.json} + 隐藏文件 + __pycache__，规范化
JSON（sort_keys + separators(",", ":")）后 sha256，前缀 "sha256:"。
build-note.txt 计入摘要（构建期版本间差异载体）。

服务只读上述文件，不落盘任何版本/状态文件；日志仅 stderr 一行/请求。

用法: python3 src/version_svc.py [port] [host]   # 缺省 18080 / 127.0.0.1
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MANIFEST_SCHEMA = "relay-artifact-manifest/1"
MANIFEST_EXCLUDE = {"MANIFEST.json", "release-info.json"}
DEFAULT_PORT = 18080
DEFAULT_HOST = "127.0.0.1"
# api.md：/buildinfo 对 release-info.json 缺失/不可解析返回的约定错误对象
BUILDINFO_UNAVAILABLE_ERROR = {"error": "release-info unavailable"}
# payload 根 = src/ 的上级目录（仓库内运行即仓库根；容器内即 /app）
PAYLOAD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 进程启动时刻（模块加载时一次采样，UTC）；/buildinfo 的 service_start_iso/
# uptime_seconds 以此为基准，仅存在于内存，不落盘
SERVICE_START = datetime.now(timezone.utc)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_manifest_digest(directory):
    """对 directory 重算 content-manifest 摘要（三处同算法，测试保证一致）。"""
    files = {}
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name in MANIFEST_EXCLUDE or name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, directory).replace(os.sep, "/")
            files[rel] = _sha256_file(full)
    payload = json.dumps({"schema": MANIFEST_SCHEMA, "files": files},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_release_info(payload_root):
    """读取并解析 release-info.json；缺失/不可解析由调用方按 500 处理。"""
    with open(os.path.join(payload_root, "release-info.json"), "r",
              encoding="utf-8") as handle:
        return json.load(handle)


class ReleaseInfoError(Exception):
    """release-info 读取/自校验失败；payload 即 500 响应的 JSON 对象。

    unavailable=True 标记缺失/不可解析一类：api.md 规定 /buildinfo 对此类
    返回约定错误对象 BUILDINFO_UNAVAILABLE_ERROR，/version 与其余失败类
    （非对象/摘要不符）仍回详细 payload，原行为不变。
    """

    def __init__(self, payload, unavailable=False):
        super().__init__(payload.get("error", "release-info 不可用"))
        self.payload = payload
        self.unavailable = unavailable


def release_info(payload_root):
    """读取 release-info.json 并对 payload 根重算摘要自校验。

    /version 与 /buildinfo 共用的唯一版本证据路径：缺失/不可解析/非对象/
    摘要不符 → 抛 ReleaseInfoError（由 handler 捕获按 500 返回）。
    """
    try:
        info = load_release_info(payload_root)
    except (OSError, ValueError) as exc:
        raise ReleaseInfoError(
            {"error": "release-info.json 不可用: " + str(exc)},
            unavailable=True) from exc
    if not isinstance(info, dict):
        raise ReleaseInfoError({"error": "release-info.json 不是 JSON 对象"})
    try:
        actual = compute_manifest_digest(payload_root)
    except OSError as exc:
        raise ReleaseInfoError(
            {"error": "payload 摘要计算失败: " + str(exc)}) from exc
    declared = info.get("artifact_digest")
    # 版本证据必须来自运行制品本身；不一致视为制品被篡改/组装错误
    if actual != declared:
        raise ReleaseInfoError({"error": "release-info 与实际制品不一致",
                                "declared": declared, "actual": actual})
    return info


def _uptime_seconds(now):
    """相对 SERVICE_START 的整数秒（每请求实算；时钟回拨钳 0）。"""
    return max(0, int((now - SERVICE_START).total_seconds()))


class VersionRequestHandler(BaseHTTPRequestHandler):
    server_version = "relay-pilot-version-svc/1.0"
    payload_root = PAYLOAD_ROOT  # build_payload_root 工厂按注入覆盖

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send_json_sorted(self, code, obj):
        # /buildinfo 200 口径：键字母序 + 紧凑分隔；/version 序列化保持不变
        self._send(code, json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _handle_version(self):
        try:
            info = release_info(self.payload_root)
        except ReleaseInfoError as exc:
            self._send_json(500, exc.payload)
            return
        self._send_json(200, {
            "release_id": info.get("release_id"),
            "source_sha": info.get("source_sha"),
            "artifact_digest": info.get("artifact_digest"),
            "built_at": info.get("built_at"),
        })

    def _handle_buildinfo(self):
        try:
            info = release_info(self.payload_root)
        except ReleaseInfoError as exc:
            # api.md 契约：缺失/不可解析 → 约定错误对象；其余失败类保持
            # 详细 payload；/version 的响应映射不受此分支影响
            if exc.unavailable:
                self._send_json_sorted(500, BUILDINFO_UNAVAILABLE_ERROR)
            else:
                self._send_json(500, exc.payload)
            return
        self._send_json_sorted(200, {
            "release_id": info.get("release_id"),
            "source_sha": info.get("source_sha"),
            "built_at": info.get("built_at"),
            "service_start_iso": SERVICE_START.isoformat(),
            "uptime_seconds": _uptime_seconds(datetime.now(timezone.utc)),
            "python_version": platform.python_version(),
        })

    def _not_found(self):
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        elif path == "/version":
            self._handle_version()
        elif path == "/buildinfo":
            self._handle_buildinfo()
        else:
            self._not_found()

    def __getattr__(self, name):
        # api.md：其他路径/方法 → 404——除 GET 外的任意方法（OPTIONS/TRACE/
        # CONNECT/自定义方法等）统一兜底 404，不落入 BaseHTTPRequestHandler
        # 未实现方法的默认 501 分支；仅在常规属性查找未命中时触发，
        # 已定义的 do_GET 等路由不受影响。
        if name.startswith("do_"):
            return self._not_found
        raise AttributeError(name)

    def log_message(self, fmt, *args):  # 一行/请求，仅 stderr，不落盘
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def build_payload_root(payload_root):
    """返回绑定指定 payload 根的 Handler 类（测试注入工厂）。"""
    return type("BoundVersionRequestHandler", (VersionRequestHandler,),
                {"payload_root": os.path.abspath(payload_root)})


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    port, host = DEFAULT_PORT, DEFAULT_HOST
    if len(args) >= 1:
        try:
            port = int(args[0])
        except ValueError:
            sys.stderr.write("端口必须是整数: %r\n" % (args[0],))
            return 2
    if len(args) >= 2:
        host = args[1]
    server = ThreadingHTTPServer((host, port), VersionRequestHandler)
    sys.stderr.write("version-svc listening on %s:%d (payload %s)\n"
                     % (host, port, VersionRequestHandler.payload_root))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # Ctrl-C 优雅退出
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
