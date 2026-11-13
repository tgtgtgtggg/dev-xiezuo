#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_version_svc.py — 版本服务单元测试。

覆盖（对应 acceptance.md AC-T1）：
  * 摘要算法等价性：独立重写的参考实现对照、固定树金标向量、排除规则、
    与 relay-server-entry/tools/build_artifact.py 的跨仓同口径对照（可用时）。
  * 全部路由分支（临时 payload 目录 + 临时端口真实起服）：
    /health 200 ok（不依赖 release-info）、/version 200 三元组、
    篡改→500、缺 release-info→500、不可解析→500、非对象→500、
    其余路径→404、非 GET 方法→404、服务不落盘任何文件。
  * /buildinfo（TestBuildinfoEndpoint）：200 六字段完整且类型正确
    （service_start_iso 可解析 UTC、uptime_seconds 非负 int、
    python_version 非空）、键字母序紧凑输出、启动时刻一次采样且
    uptime 每请求实算、缺 release-info/坏 JSON → 500 且响应体为 api.md
    约定错误对象 {"error": "release-info unavailable"}、非对象/篡改→500、
    未知路径与非 GET 方法→404、不落盘。
"""
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import version_svc  # noqa: E402  (路径注入需先于导入)

# 显式禁用代理：宿主可能配置系统级 HTTP 代理（如 127.0.0.1:7890），
# urllib 默认会经代理访问 127.0.0.1 导致 502；本机回环直连不适用代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 跨仓对照对象：构建侧工具 build_artifact.py（存在时做同口径等价测试）
_BUILD_ARTIFACT_PATH = os.path.abspath(os.path.join(
    REPO_ROOT, os.pardir, os.pardir, os.pardir, os.pardir,
    "relay-server-entry", "tools", "build_artifact.py"))

# 固定 fixture 树：可见文件计入摘要，其余为排除项样本
FIXTURE_FILES = {
    "app.py": b"print('relay pilot')\n",
    "build-note.txt": "试点 r1 中文内容\n".encode("utf-8"),
    "lib/deep/util.py": b"# util\n",
    # 排除项——内容变更不应影响摘要
    "MANIFEST.json": b'{"schema":"relay-artifact-manifest/1","files":{}}\n',
    "release-info.json": b"{}",
    ".hidden": b"skip me",
    "__pycache__/app.cpython-39.pyc": b"\x00\x01binary",
}

# 上述 fixture 的金标摘要（按 logic.md 规格独立计算固化，防两套实现同时漂移）
GOLDEN_DIGEST = ("sha256:7fc45a7d53342750a90937508c21bf3"
                 "be64ce85dab86fd566802de6c5417f4c4")


def _reference_digest(payload_root):
    """独立重写的同规格算法（listdir 递归遍历，非 os.walk），用于等价性对照。"""
    collected = {}

    def _walk(dirpath, prefix):
        for entry in sorted(os.listdir(dirpath)):
            full = os.path.join(dirpath, entry)
            rel = prefix + entry
            if os.path.isdir(full):
                if entry == "__pycache__":
                    continue
                _walk(full, rel + "/")
            elif entry in ("MANIFEST.json", "release-info.json") or entry.startswith("."):
                continue
            else:
                with open(full, "rb") as fh:
                    collected[rel] = hashlib.sha256(fh.read()).hexdigest()

    _walk(payload_root, "")
    payload = json.dumps(
        {"schema": "relay-artifact-manifest/1", "files": collected},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _make_payload_tree(root):
    for rel, content in FIXTURE_FILES.items():
        full = os.path.join(root, rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content)


def _write_release_info(root, artifact_digest, release_id="RELAY-UNIT-20261111-01"):
    info = {
        "release_id": release_id,
        "source_sha": "0123456789abcdef0123456789abcdef01234567",
        "artifact_digest": artifact_digest,
        "built_at": "2026-11-11T08:00:00+00:00",
    }
    with open(os.path.join(root, "release-info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return info


def _list_files(root):
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


class DigestEquivalenceTests(unittest.TestCase):
    """摘要算法等价性（与 build_artifact.py 同口径）。"""

    def setUp(self):
        self.payload_root = tempfile.mkdtemp(prefix="version-svc-digest-")
        _make_payload_tree(self.payload_root)
        self.addCleanup(shutil.rmtree, self.payload_root, ignore_errors=True)

    def test_matches_reference_implementation(self):
        self.assertEqual(version_svc.compute_manifest_digest(self.payload_root),
                         _reference_digest(self.payload_root))

    def test_golden_vector(self):
        self.assertEqual(
            version_svc.compute_manifest_digest(self.payload_root), GOLDEN_DIGEST)

    def test_exclusion_rules(self):
        root = self.payload_root
        base = version_svc.compute_manifest_digest(root)
        # 排除集内容变更/新增不影响摘要
        for rel in ("MANIFEST.json", "release-info.json", ".hidden"):
            with open(os.path.join(root, rel), "ab") as fh:
                fh.write(b"changed")
        with open(os.path.join(root, ".gitignore"), "wb") as fh:
            fh.write(b"__pycache__/\n")
        with open(os.path.join(root, "__pycache__", "another.pyc"), "wb") as fh:
            fh.write(b"more bytecode")
        self.assertEqual(version_svc.compute_manifest_digest(root), base)
        # 计入集（含 build-note.txt——版本间差异载体）变更必须改变摘要
        with open(os.path.join(root, "build-note.txt"), "ab") as fh:
            fh.write("追加\n".encode("utf-8"))
        self.assertNotEqual(version_svc.compute_manifest_digest(root), base)

    @unittest.skipUnless(os.path.isfile(_BUILD_ARTIFACT_PATH),
                         "本机无 relay-server-entry/tools/build_artifact.py，"
                         "跨仓同口径对照跳过")
    def test_digest_matches_build_artifact_tool(self):
        """与构建侧 build_artifact.build_payload_manifest 摘要一致。

        fixture 不含 BUILD_EXCLUDE 名字（Dockerfile/build_artifact.py/
        README.md——构建期排除、运行时不存在），此时两侧排除集口径一致。
        """
        spec = importlib.util.spec_from_file_location(
            "relay_build_artifact", _BUILD_ARTIFACT_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _files, _payload, digest = module.build_payload_manifest(self.payload_root)
        self.assertEqual(
            digest, version_svc.compute_manifest_digest(self.payload_root))

    def test_build_payload_root_factory_binds_root(self):
        handler = version_svc.build_payload_root(self.payload_root)
        self.assertEqual(handler.payload_root,
                         os.path.abspath(self.payload_root))
        self.assertTrue(issubclass(handler, version_svc.VersionRequestHandler))

    def test_payload_root_default_is_parent_of_src(self):
        self.assertEqual(version_svc.PAYLOAD_ROOT, REPO_ROOT)


class RouteTests(unittest.TestCase):
    """全部路由分支（临时 payload 目录 + 临时端口起服）。"""

    def setUp(self):
        self.payload_root = tempfile.mkdtemp(prefix="version-svc-payload-")
        _make_payload_tree(self.payload_root)
        digest = _reference_digest(self.payload_root)
        self.release_info = _write_release_info(self.payload_root, digest)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), version_svc.build_payload_root(self.payload_root))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(shutil.rmtree, self.payload_root, ignore_errors=True)
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, path, method="GET"):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), method=method)
        try:
            with _OPENER.open(request, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_health_returns_ok(self):
        status, headers, body = self._request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual(headers.get_content_type(), "text/plain")

    def test_health_ignores_querystring(self):
        status, _headers, body = self._request("/health?probe=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")

    def test_health_works_without_release_info(self):
        os.remove(os.path.join(self.payload_root, "release-info.json"))
        status, _headers, body = self._request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")

    def test_version_returns_release_info_triple(self):
        status, headers, body = self._request("/version")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(json.loads(body.decode("utf-8")), self.release_info)

    def test_version_tampered_payload_returns_500(self):
        with open(os.path.join(self.payload_root, "app.py"), "ab") as fh:
            fh.write(b"# tampered\n")
        status, _headers, _body = self._request("/version")
        self.assertEqual(status, 500)

    def test_version_missing_release_info_returns_500(self):
        os.remove(os.path.join(self.payload_root, "release-info.json"))
        status, _headers, _body = self._request("/version")
        self.assertEqual(status, 500)

    def test_version_error_body_keeps_original_detail(self):
        # F-1 修复边界守护：仅 /buildinfo 归一为 api.md 约定错误对象，
        # /version 保留原有详细错误 payload（含底层异常信息）
        os.remove(os.path.join(self.payload_root, "release-info.json"))
        status, _headers, body = self._request("/version")
        self.assertEqual(status, 500)
        data = json.loads(body.decode("utf-8"))
        self.assertNotEqual(data, {"error": "release-info unavailable"})
        self.assertTrue(data["error"].startswith("release-info.json 不可用"))

    def test_version_unparseable_release_info_returns_500(self):
        target = os.path.join(self.payload_root, "release-info.json")
        with open(target, "wb") as fh:
            fh.write('{"release_id": 缺引号}'.encode("utf-8"))
        status, _headers, _body = self._request("/version")
        self.assertEqual(status, 500)

    def test_version_non_object_release_info_returns_500(self):
        target = os.path.join(self.payload_root, "release-info.json")
        with open(target, "wb") as fh:
            fh.write(b"[1, 2]")
        status, _headers, _body = self._request("/version")
        self.assertEqual(status, 500)

    def test_unknown_paths_return_404(self):
        for path in ("/nope", "/version/", "/health/extra", "/", "/versionx"):
            status, _headers, _body = self._request(path)
            self.assertEqual(status, 404, path)

    def test_non_get_methods_return_404(self):
        for method, path in (("POST", "/version"), ("PUT", "/health"),
                             ("DELETE", "/"), ("PATCH", "/version"),
                             ("HEAD", "/health"), ("OPTIONS", "/version"),
                             ("OPTIONS", "/health"), ("TRACE", "/health"),
                             ("CONNECT", "/version"), ("FOOBAR", "/health"),
                             ("RELAY-PILOT", "/version")):
            status, _headers, body = self._request(path, method=method)
            self.assertEqual(status, 404, "%s %s" % (method, path))
            if method != "HEAD":  # HEAD 按 RFC 不携带响应体
                self.assertEqual(body, b"not found",
                                 "%s %s" % (method, path))

    def test_service_writes_no_files(self):
        before = _list_files(self.payload_root)
        for path in ("/health", "/version", "/nope"):
            self._request(path)
        self.assertEqual(_list_files(self.payload_root), before)


class TestBuildinfoEndpoint(unittest.TestCase):
    """/buildinfo（api.md：200 六字段；失败与 /version 同口径 500）。"""

    EXPECTED_KEYS = ["built_at", "python_version", "release_id",
                     "service_start_iso", "source_sha", "uptime_seconds"]

    def setUp(self):
        self.payload_root = tempfile.mkdtemp(prefix="version-svc-buildinfo-")
        _make_payload_tree(self.payload_root)
        digest = _reference_digest(self.payload_root)
        self.release_info = _write_release_info(self.payload_root, digest)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), version_svc.build_payload_root(self.payload_root))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(shutil.rmtree, self.payload_root, ignore_errors=True)
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, path, method="GET"):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), method=method)
        try:
            with _OPENER.open(request, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def _get_buildinfo(self):
        status, headers, body = self._request("/buildinfo")
        self.assertEqual(status, 200)
        return headers, body

    def test_buildinfo_returns_full_typed_fields(self):
        headers, body = self._get_buildinfo()
        self.assertEqual(headers.get_content_type(), "application/json")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(sorted(data), self.EXPECTED_KEYS)
        # 三元组与 /version 同源同字段名（值直接来自 release-info.json）
        self.assertEqual(data["release_id"], self.release_info["release_id"])
        self.assertEqual(data["source_sha"], self.release_info["source_sha"])
        self.assertEqual(data["built_at"], self.release_info["built_at"])
        # service_start_iso 可解析且为 UTC，且不晚于当前时刻
        started = datetime.fromisoformat(data["service_start_iso"])
        self.assertEqual(started.utcoffset(), timedelta(0))
        self.assertLessEqual(started, datetime.now(timezone.utc))
        # uptime_seconds 非负 int（bool 是 int 子类，需显式排除）
        self.assertIsInstance(data["uptime_seconds"], int)
        self.assertNotIsInstance(data["uptime_seconds"], bool)
        self.assertGreaterEqual(data["uptime_seconds"], 0)
        # python_version 非空且与运行解释器一致
        self.assertEqual(data["python_version"], platform.python_version())
        self.assertTrue(data["python_version"])

    def test_buildinfo_body_is_sorted_compact_json(self):
        _headers, body = self._get_buildinfo()
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(list(data), sorted(data))
        self.assertEqual(
            body, json.dumps(data, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))

    def test_buildinfo_start_sampled_once_and_uptime_recomputed(self):
        _headers, first_body = self._get_buildinfo()
        first = json.loads(first_body.decode("utf-8"))
        time.sleep(1.1)  # 跨整数秒边界，验证 uptime 每请求实算而非缓存
        _headers, second_body = self._get_buildinfo()
        second = json.loads(second_body.decode("utf-8"))
        self.assertEqual(second["service_start_iso"],
                         first["service_start_iso"])
        self.assertGreaterEqual(second["uptime_seconds"],
                                first["uptime_seconds"] + 1)

    def test_buildinfo_missing_release_info_returns_500(self):
        os.remove(os.path.join(self.payload_root, "release-info.json"))
        status, headers, body = self._request("/buildinfo")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get_content_type(), "application/json")
        # api.md 契约：完整响应对象断言（dict 相等即同时排除多余键）
        self.assertEqual(json.loads(body.decode("utf-8")),
                         {"error": "release-info unavailable"})

    def test_buildinfo_unparseable_release_info_returns_500(self):
        target = os.path.join(self.payload_root, "release-info.json")
        with open(target, "wb") as fh:
            fh.write('{"release_id": 缺引号}'.encode("utf-8"))
        status, headers, body = self._request("/buildinfo")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(json.loads(body.decode("utf-8")),
                         {"error": "release-info unavailable"})

    def test_buildinfo_non_object_release_info_returns_500(self):
        target = os.path.join(self.payload_root, "release-info.json")
        with open(target, "wb") as fh:
            fh.write(b"[1, 2]")
        status, _headers, _body = self._request("/buildinfo")
        self.assertEqual(status, 500)

    def test_buildinfo_tampered_payload_returns_500(self):
        with open(os.path.join(self.payload_root, "app.py"), "ab") as fh:
            fh.write(b"# tampered\n")
        status, _headers, _body = self._request("/buildinfo")
        self.assertEqual(status, 500)

    def test_buildinfo_unknown_paths_return_404(self):
        for path in ("/buildinfo/", "/buildinfox", "/build/info",
                     "/buildinfo/extra"):
            status, _headers, _body = self._request(path)
            self.assertEqual(status, 404, path)

    def test_buildinfo_non_get_methods_return_404(self):
        for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS", "FOOBAR"):
            status, _headers, body = self._request("/buildinfo", method=method)
            self.assertEqual(status, 404, method)
            if method != "HEAD":  # HEAD 按 RFC 不携带响应体
                self.assertEqual(body, b"not found", method)

    def test_buildinfo_writes_no_files(self):
        before = _list_files(self.payload_root)
        self._request("/buildinfo")
        self.assertEqual(_list_files(self.payload_root), before)

    def test_uptime_helper_truncates_and_clamps_clock_rollback(self):
        start = version_svc.SERVICE_START
        self.assertEqual(version_svc._uptime_seconds(start), 0)
        # int 截断：1.9 秒 → 1
        self.assertEqual(version_svc._uptime_seconds(
            start + timedelta(seconds=1, microseconds=900000)), 1)
        # 时钟回拨钳 0
        self.assertEqual(version_svc._uptime_seconds(
            start - timedelta(seconds=30)), 0)


class MainArgvTests(unittest.TestCase):
    def test_main_rejects_non_integer_port(self):
        self.assertEqual(version_svc.main(["not-a-port"]), 2)

    def test_main_rejects_non_integer_port_with_host(self):
        self.assertEqual(version_svc.main(["not-a-port", "0.0.0.0"]), 2)


if __name__ == "__main__":
    unittest.main()
