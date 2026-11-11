#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/integration/test_version_integration.py — 版本服务集成冒烟。

以子进程真实起服验证 GET /health 与 GET /version：
payload 布局与真实制品一致（payload 根 = src/ 上级，release-info.json
在 payload 根上，artifact_digest 为构建侧口径的摘要），argv 传端口。
本目录是 required 检查 integration 的 discover 起点。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

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

    def test_health_and_version_over_real_subprocess(self):
        payload_root = tempfile.mkdtemp(prefix="version-svc-it-")
        self.addCleanup(shutil.rmtree, payload_root, ignore_errors=True)

        # 真实制品布局：payload 根含 src/version_svc.py 与 release-info.json
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

        with _OPENER.open(base_url + "/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"ok")

        with _OPENER.open(base_url + "/version", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"),
                             "application/json; charset=utf-8")
            self.assertEqual(json.loads(resp.read().decode("utf-8")),
                             release_info)

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
