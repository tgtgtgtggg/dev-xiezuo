#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/integration/test_version_integration.py — 版本服务集成冒烟。

以子进程真实起服验证 GET /health、GET /version 与 GET /buildinfo：
payload 布局与真实制品一致（payload 根 = src/ 上级，release-info.json
在 payload 根上，artifact_digest 为构建侧口径的摘要），argv 传端口。
本目录是 required 检查 integration 的 discover 起点。
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))            # tests/integration
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_DIR = os.path.join(REPO_ROOT, "src")
sys.path.insert(0, SRC_DIR)

import version_svc  # noqa: E402  (路径注入需先于导入)

# 显式禁用代理：宿主可能配置系统级 HTTP 代理（如 127.0.0.1:7890），
# urllib 默认会经代理访问 127.0.0.1 导致 502；本机回环直连不适用代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

READY_TIMEOUT_SECONDS = 10.0


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class VersionSvcIntegrationSmoke(unittest.TestCase):

    def _make_payload(self):
        """真实制品布局的临时 payload 根：src/version_svc.py + release-info.json。"""
        payload_root = tempfile.mkdtemp(prefix="version-svc-it-")
        self.addCleanup(shutil.rmtree, payload_root, ignore_errors=True)
        os.makedirs(os.path.join(payload_root, "src"))
        shutil.copy2(os.path.join(SRC_DIR, "version_svc.py"),
                     os.path.join(payload_root, "src", "version_svc.py"))
        release_info = {
            "release_id": "RELAY-IT-20261111-01",
            "source_sha": "0123456789abcdef0123456789abcdef01234567",
            "artifact_digest": version_svc.compute_manifest_digest(payload_root),
            "built_at": "2026-11-11T08:00:00+00:00",
        }
        with open(os.path.join(payload_root, "release-info.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(release_info, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
            fh.write("\n")
        return payload_root, release_info

    def _start_subprocess(self, payload_root):
        """argv 起服并等待就绪，返回 base_url（退出清理已注册）。"""
        port = _free_port()
        base_url = "http://127.0.0.1:%d" % port
        log_file = tempfile.TemporaryFile(mode="w+b")  # 子进程 stderr 诊断用
        self.addCleanup(log_file.close)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen(
            [sys.executable,
             os.path.join(payload_root, "src", "version_svc.py"), str(port)],
            stdout=subprocess.DEVNULL, stderr=log_file, env=env)
        self.addCleanup(self._terminate, proc)
        self._wait_ready(base_url + "/health", proc, log_file)
        return base_url

    def test_health_and_version_over_real_subprocess(self):
        _payload_root, release_info = self._make_payload()
        base_url = self._start_subprocess(_payload_root)

        with _OPENER.open(base_url + "/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"ok")

        with _OPENER.open(base_url + "/version", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"),
                             "application/json; charset=utf-8")
            self.assertEqual(json.loads(resp.read().decode("utf-8")),
                             release_info)

    def test_buildinfo_over_real_subprocess(self):
        _payload_root, release_info = self._make_payload()
        base_url = self._start_subprocess(_payload_root)

        with _OPENER.open(base_url + "/buildinfo", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"),
                             "application/json; charset=utf-8")
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        # 键字母序 + 紧凑分隔（logic.md 序列化口径）
        self.assertEqual(list(data), sorted(data))
        self.assertEqual(raw, json.dumps(data, ensure_ascii=False,
                                         sort_keys=True,
                                         separators=(",", ":")).encode("utf-8"))
        # 三元组与 release-info 同源，python_version 与子进程解释器一致
        self.assertEqual(data["release_id"], release_info["release_id"])
        self.assertEqual(data["source_sha"], release_info["source_sha"])
        self.assertEqual(data["built_at"], release_info["built_at"])
        self.assertEqual(data["python_version"], platform.python_version())
        self.assertTrue(data["python_version"])
        # service_start_iso 可解析 UTC；子进程刚起步，启动时刻贴近当前时间
        started = datetime.fromisoformat(data["service_start_iso"])
        self.assertEqual(started.utcoffset(), timedelta(0))
        skew = (datetime.now(timezone.utc) - started).total_seconds()
        self.assertGreaterEqual(skew, 0.0)
        self.assertLessEqual(skew, 60.0)
        # uptime_seconds 非负 int（bool 是 int 子类，需显式排除）
        self.assertIsInstance(data["uptime_seconds"], int)
        self.assertNotIsInstance(data["uptime_seconds"], bool)
        self.assertGreaterEqual(data["uptime_seconds"], 0)
        self.assertLess(data["uptime_seconds"], 60)

        # 启动时刻一次采样：再次请求 service_start_iso 不变，uptime 不减
        with _OPENER.open(base_url + "/buildinfo", timeout=5) as resp:
            again = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(again["service_start_iso"], data["service_start_iso"])
        self.assertGreaterEqual(again["uptime_seconds"], data["uptime_seconds"])

    def _wait_ready(self, health_url, proc, log_file):
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail("服务子进程提前退出(code %s): %s"
                          % (proc.returncode, self._log_tail(log_file)))
            try:
                with _OPENER.open(health_url, timeout=1) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.1)
        self.fail("服务 %.1fs 内未就绪: %s"
                  % (READY_TIMEOUT_SECONDS, self._log_tail(log_file)))

    @staticmethod
    def _terminate(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    @staticmethod
    def _log_tail(log_file):
        try:
            log_file.seek(0)
            return log_file.read().decode("utf-8", "replace")[-2000:]
        except OSError:
            return "<日志不可读>"


if __name__ == "__main__":
    unittest.main()
