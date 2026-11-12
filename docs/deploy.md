# relay 试点版本服务 运行说明（deploy）

适用对象：`relay-pilot-version-svc`（本仓库 `src/version_svc.py`）及其 M1 发布链
（构建 → 装运 → TG3 部署 → 线上核实）。文中所有路径与参数均按现网事实核对：
构建工具位于 `relay-server-entry` 仓，装运工具位于 `agent-relay` 仓，可信入口
安装在 TG3 目标机 `codexops` 用户 home 下。

---

## 1. 背景

本服务是 Codex × zCode 协作开发试点（V1.1 §10 构建部署与上线核实）的被部署物：
一个纯 Python 标准库的 HTTP 版本服务，不引入任何第三方依赖。

路由行为（`src/version_svc.py`）：

| 请求 | 响应 | 说明 |
| --- | --- | --- |
| `GET /health` | `200 ok`（纯文本） | 健康即进程可服务，不触碰版本文件 |
| `GET /version` | `200` JSON `{release_id, source_sha, artifact_digest, built_at}` | 版本三元组（`release_id` / `source_sha` / `artifact_digest`，另附构建时间）。数据只读 payload 根的 `release-info.json`，且每次请求对 payload 根重算 content-manifest 摘要自校验，与声明不符 → `500`（制品被篡改/组装错误的可观测信号） |
| 其他路径/方法 | `404` | 兜底 |

版本证据链（三处同算法，互为校验）：构建侧 `build_artifact.py` 计算
`artifact_digest` 并写入制品 `release-info.json`；装运侧 `pack_remote_image.py`
解包后重算摘要与构建登记值逐字比对（闸门）；运行中的服务 `/version` 每次请求
再次重算自校验。摘要口径：排除 `{MANIFEST.json, release-info.json}` + 隐藏文件
+ `__pycache__`，对剩余文件按相对路径建 `{路径: sha256}` 映射，规范化 JSON
（`sort_keys` + `separators(",", ":")`）后取 sha256，前缀 `"sha256:"`。

发布链总览（M1，环境 `pilot-tg3`）：

```
冻结 release SHA ──快照──▶ build_artifact.py ──▶ <release_id>.tar.gz（目录包）
    ──ssh stdin──▶ pack_remote_image.py（TG3 本机，摘要闸门 + docker build/save）
    ──▶ <store>/<release_id>.tar（镜像 tar）
    ──DeploymentRequest stdin──▶ ~/relay-pilot/bin/relay_trusted_entry.py
    ──▶ 容器 relay-pilot-version-svc-<release_id>（镜像 relay-pilot-version-svc:<release_id>）
    ──▶ 127.0.0.1:18080 /version + /health 核实 → VERIFIED
```

---

## 2. 本地运行

依赖：Python 3.9+（保持 3.9 兼容，检查在宿主 `/usr/bin/python3` 运行），无需 pip 包。

```bash
# 启动（可选参数：[port] [host]，缺省 18080 / 127.0.0.1）
python3 src/version_svc.py            # 监听 127.0.0.1:18080
python3 src/version_svc.py 18080 0.0.0.0

# 探测
curl -s http://127.0.0.1:18080/health   # → ok
curl -s http://127.0.0.1:18080/version  # → 200 JSON 或 500（见下）
```

payload 根 = `src/` 的上级目录（仓库内运行即仓库根；容器内即 `/app`）。
**裸仓库运行时 `/version` 返回 500 是预期行为**：`release-info.json` 是构建期
生成物（不入库），源码树里没有它；`/health` 恒为 `ok`。想在本地看到完整的
`/version`，用第 3 节的构建产物解包后运行：

```bash
python3 <relay-server-entry>/tools/build_artifact.py --release-id local-demo \
    --source-sha <40位十六进制> --source-dir . \
    --output /tmp/local-demo.tar.gz
mkdir -p /tmp/local-demo-payload && tar -xzf /tmp/local-demo.tar.gz -C /tmp/local-demo-payload
python3 /tmp/local-demo-payload/src/version_svc.py
```

测试（两套 unittest discover，即协调器 `unit` / `integration` 检查）：

```bash
python3 -m unittest discover -s tests              # 单测：路由分支 + 摘要算法等价性
python3 -m unittest discover -s tests/integration  # 集成冒烟：子进程真实起服验证 /health 与 /version
```

---

## 3. 构建参数

构建工具：`relay-server-entry/tools/build_artifact.py`（独立仓，dev/构建侧工具，
不部署）。从冻结的 release 生成 `<release_id>.tar.gz` 目录包（payload +
`build-note.txt` + `release-info.json` + `MANIFEST.json`）。

| 参数 | 必填 | 约束 / 语义 |
| --- | --- | --- |
| `--release-id` | 是 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`；贯穿制品、镜像 tag、容器名、账本 |
| `--source-sha` | 是 | 40 位小写十六进制；冻结的 release 源提交 SHA |
| `--output` | 是 | 输出路径 `<dir>/<release_id>.tar.gz` |
| `--source-dir` | 否（生产必传） | 打包源码目录——协调器从冻结 release SHA 导出的只读源码快照（ls-tree/cat-file 导出式，与工作区漂移无关）；未提供时回退本仓 `sample-app/`（仅开发侧默认，生产构建必须由协调器传 `--source-dir`） |
| `--build-note` | 否 | 写入 payload `build-note.txt` 的版本差异内容，**计入制品摘要**（不同 release 的摘要必不同的官方载体） |
| `--app-dir` | 否 | 显式指定应用目录（优先级低于 `--source-dir`） |

示例：

```bash
python3 tools/build_artifact.py --release-id RELAY-20261110-01 \
    --source-sha 0123456789abcdef0123456789abcdef01234567 \
    --source-dir /path/to/frozen-snapshot \
    --output /path/to/artifacts/RELAY-20261110-01.tar.gz \
    --build-note "r1"
```

成功后 stdout 恰好一行 JSON：`{"release_id":..., "artifact_digest":"sha256:...",
"source_sha":..., "path":...}`。其中 `artifact_digest` 即协调器填入
`DeploymentRequest.artifact_digest` 的值，也是装运闸门与线上 `/version`
回读的同一个值。

构建侧打包额外排除源码目录顶层的 `Dockerfile` / `build_artifact.py` /
`README.md`（它们不进制品，因此运行侧重算摘要时无需排除）；源码中的
`__pycache__`、隐藏文件同样排除。

---

## 4. TG3 部署链

TG3 环境：Ubuntu 24.04 x86_64，`codexops` 用户，docker（containerd 存储），
无 sudo。可信入口经 `relay-server-entry/install.sh` 安装在 `~/relay-pilot/`
（布局：`bin/`、`profiles/`、`state/{logs,releases,tmp}/`、`artifacts/`；
`state_dir=/home/codexops/relay-pilot/state`，
`artifact.store=/home/codexops/relay-pilot/artifacts`）。

### 4.1 构建（协调端）

协调器按里程碑冻结 release SHA，导出只读快照，调用
`relay-server-entry/tools/build_artifact.py --release-id … --source-sha …
--source-dir <快照> --output <artifacts>/<release_id>.tar.gz`（参数见第 3 节），
登记 stdout JSON 中的 `artifact_digest`。

### 4.2 装运（协调端 → TG3，ssh stdin）

装运工具：`agent-relay` 仓 `relay/tools/pack_remote_image.py`（在**目标机本机**
执行，Python 3.10+ 纯标准库）。协调器经 ssh 通道调用，**目录包字节经 stdin 传入**。

注意 ssh 命令经过**两层 shell 解析**：本地 shell 先解析一次；ssh 随后把远端命令
各参数用空格拼接成单个字符串，交由**远端 shell** 二次解析。因此含空格或引号的
参数（典型是 `--image-cmd` 的 JSON 数组）必须再做一层**远端引用**——若只在本地
层加单引号，该引号会被本地 shell 消耗，远端 shell 将剥掉 JSON 内的双引号，
`json.loads` 失败，装运脚本以 `image-cmd-invalid` 拒绝，根本进不了摘要闸门。
推荐用 `shlex.quote` 逐参数生成远端命令，引用由机器保证：

```bash
# 1) 生成远端命令（shlex.quote 保证每个参数经远端 shell 解析后逐字还原）
REMOTE_CMD=$(python3 - <<'PY'
import shlex
args = [
    "python3", "<TG3>/relay/tools/pack_remote_image.py",
    "--release-id", "RELAY-20261110-01",
    "--store", "/home/codexops/relay-pilot/artifacts",
    "--image", "relay-pilot-version-svc",
    "--image-cmd", '["python3","/app/src/version_svc.py","18080","0.0.0.0"]',
    "--expect-digest", "sha256:<构建登记的 artifact_digest>",
    "--base-image", "python:3.12-slim",
]
print(" ".join(shlex.quote(a) for a in args))
PY
)

# 2) 远端命令作为本地单个参数传入；目录包字节经 ssh stdin 传输（互不干扰）
ssh -o BatchMode=yes <transport> "$REMOTE_CMD" < <release_id>.tar.gz
```

不建议手写两层引用（例如把整条远端命令放进本地双引号、再对 JSON 参数内嵌
远端单引号）：引号嵌套、转义与换行都极易出错——双引号内的**裸换行**会被
本地 shell 保留、经 ssh 原样送达远端，而远端 shell 把换行当**命令分隔符**，
结果脚本被无参调用（argparse 直接拒绝），`--release-id` 等后续参数又各自
被当成命令执行，同样进不了摘要闸门。装运命令统一由 `shlex.quote` 生成
（如上），并保持远端命令为**单行**。

脚本在目标机依次执行：

1. 解包 stdin 目录包到暂存区 `<staging>/app/`；
2. **摘要闸门**：对解包内容按与 `build_artifact.py` / 入口 / 服务运行时同一
   算法重算 content-manifest 摘要，与 `--expect-digest` 逐字比对——不一致
   立即拒绝（结果 JSON `{"ok":false,"error":"digest-mismatch",...}`），
   绝不把身份不符的制品做成镜像；
3. `docker build -t relay-pilot-version-svc:<release_id>`（生成 Dockerfile：
   基础镜像 `--base-image`，`COPY app /app`，`EXPOSE 18080`，CMD 为
   `--image-cmd`）；
4. `docker save -o /home/codexops/relay-pilot/artifacts/<release_id>.tar`
   （可信入口 prepare 期望的路径与文件名，即 profile `artifact.docker_tar`
   模板 `{release_id}.tar`）；
5. stdout 打印恰好一行 JSON 结果（`ok` / `image_tag` / `image_tar` /
   `payload_digest` / `size_bytes`）；docker 全部输出被捕获进 stderr。

### 4.3 部署（TG3 可信入口）

调用形态：`DeploymentRequest` JSON 经 **stdin** 传入，stdout 返回恰好一个
`DeploymentResult` JSON；stderr 只记诊断，不得当成功依据。

```bash
python3 ~/relay-pilot/bin/relay_trusted_entry.py \
  --profile ~/relay-pilot/profiles/pilot-tg3-docker.json
```

- profile `~/relay-pilot/profiles/pilot-tg3-docker.json`（`profile_id:
  pilot-tg3-docker`，`environment_id: pilot-tg3`，`runtime: docker`）：服务名
  `relay-pilot-version-svc`，宿主绑定 `127.0.0.1:18080`（容器端口同为
  18080），探测 `http://127.0.0.1:18080/version` 与 `/health`，健康等待
  60s，观察窗口 120s（间隔 10s），`rollback_enabled: true`，
  `migration_policy: none`。
- `profile_hash` = profile 文件字节 sha256，用
  `python3 ~/relay-pilot/bin/relay_trusted_entry.py --profile \
  ~/relay-pilot/profiles/pilot-tg3-docker.json --print-profile-hash` 获取。
- 请求要点：`operation`（`status` / `deploy` / `verify` / `rollback`）；
  `expected_current_release_id` 首次部署为 `null`（CAS 防覆盖他人发布）；
  重试必须换新 `idempotency_key`；`deadline` 必须带时区。
- **容器名 / 镜像 tag 约定**：镜像 tag `relay-pilot-version-svc:<release_id>`
  （冒号连接，`pack_remote_image.py --image` 与 profile `docker.image` 共同
  决定）；容器名 `relay-pilot-version-svc-<release_id>`（连字符连接，入口按
  profile `service.name` + release_id 生成）。入口按不可变 image ID 运行容器，
  `external_deployment_id` 携带该 ID。
- deploy 语义：入口先解析镜像 tar 复核摘要与 `release-info.json` 并绑定镜像
  ID，再 load → 停掉非目标旧容器 → 启动新容器 → 健康检查 + 版本探测与请求
  身份一致才 CONFIRMED；失败自动恢复 previous 容器。
- 退出码：`0` 成功/幂等返回；`1` 业务失败（按 `retryable` 决定重试）；
  `2` 输入非法（修请求，不得盲目重试）；`3` 内部故障（人工介入，先 `status`
  对账）。

### 4.4 核实与收尾

deploy 返回 `DEPLOYED` 后，协调器发起 `operation: verify`（用同一 release
身份与 `release_sha`）：入口在 120 秒观察窗口内多轮探测
`http://127.0.0.1:18080/version`，每轮三元组与请求身份一致且 `/health` 通过
才记 `VERIFIED`。线上人工核实方法见第 5 节。

---

## 5. 验证方法

在 TG3 目标机（服务绑定宿主 `127.0.0.1:18080`）：

```bash
# 1) 健康：期望 200，正文恰好 "ok"
curl -s -w '\n%{http_code}\n' http://127.0.0.1:18080/health
# → ok
# → 200

# 2) 版本三元组：期望 200，JSON 的 release_id / source_sha / artifact_digest
#    与本次 release 登记逐字一致（built_at 为构建时间）
curl -s http://127.0.0.1:18080/version
# → {"release_id": "RELAY-20261110-01", "source_sha": "<40hex>",
#     "artifact_digest": "sha256:<64hex>", "built_at": "2026-11-10T..."}

# 3) 载体核对：容器名/镜像 tag 符合约定
docker ps --filter name=relay-pilot-version-svc-
docker images relay-pilot-version-svc
```

判定要点：

- `/health` 返回 `ok` 只证明进程活着；**发布成功以 `/version` 三元组与登记
  身份一致为准**（`source_sha` = 冻结 release SHA，`artifact_digest` = 构建
  stdout 登记值）。
- `/version` 返回 `500` = 运行制品自校验失败（见第 6 节），不得视为已部署。
- 协议级核实走入口 `verify` 操作（观察窗口 120 秒多轮一致才 `VERIFIED`），
  断线/异常后先用 `status` 对账再决定动作。

---

## 6. 故障排查

| 症状 | 常见原因 | 处置 |
| --- | --- | --- |
| 本地裸仓库 `/version` 500，正文 `release-info.json 不可用` | 预期行为：源码树无 `release-info.json`（构建期生成物） | 无需处置；需完整验证时按第 2 节解包构建产物运行 |
| 线上 `/version` 500，正文含 `declared`/`actual` | 运行制品与 `release-info.json` 声明摘要不符——payload 文件被改动/组装错误/混入未排除文件 | 核对镜像内 `/app` 内容与构建产物；重新构建并按链路重发，勿手改容器内文件 |
| 装运返回 `digest-mismatch` | ssh 传输包体损坏、`--expect-digest` 与制品不配、源码快照与构建登记漂移 | 以构建 stdout 登记的 `artifact_digest` 为准重装运；仍不符则回查快照来源 |
| 装运 rc=2 且结果 `image-cmd-invalid`（或远端参数被拆散、脚本被无参调用） | ssh 远端 shell 二次解析剥掉了参数引用——`--image-cmd` JSON 内双引号被去、含空格参数被拆分；双引号内裸换行还会被远端 shell 当命令分隔符 | 按 §4.2 用 `shlex.quote` 生成**单行**远端命令；复核时用 shell 函数替身执行远端命令串，确认脚本仅被调用一次且 `--image-cmd` 可被 `json.loads` 解析（`shlex.split` 把换行当普通空白，单独使用会漏检换行问题） |
| 装运 `docker-build-failed` / `docker-save-failed` | 目标机 docker 异常、基础镜像 `python:3.12-slim` 缺失、磁盘不足 | 看 ssh stderr 里捕获的 docker 输出末尾行；在 TG3 本机排查 docker 状态 |
| 入口 exit `2`（输入非法） | schema/环境 ID/profile_hash/release_id 字符集/deadline 无时区不通过 | 按报错修请求字段后重发（换新 `idempotency_key`）；profile 变更需重取 `--print-profile-hash` |
| 入口 exit `3` 或结果 `UNKNOWN` | 内部故障/断线，远端状态不可知 | **先发 `operation: status` 对账**；确认当前运行版本再决定续跑或回滚，绝不重复执行切换 |
| deploy 失败 `port already allocated` 类 | 旧容器占用宿主 18080 | 入口回滚路径已内置"先停失败版本容器再启 previous"；手动处置时先 `docker stop relay-pilot-version-svc-<失败release_id>` |
| `/health` 超时（健康等待 60s 内不通） | 容器启动失败、端口映射不符 | `docker logs relay-pilot-version-svc-<release_id>` 看服务 stderr（每请求一行日志）；核对容器 `18080->18080` 映射 |
| 重试 deploy 提示 request-hash-conflict | 同 `idempotency_key`/身份与账本既有记录不一致 | 属于防重复切换的保护；换新 `idempotency_key` 并确认 release 身份后再发 |

通用原则：任何情况下不要绕过入口直接 `docker run`/手改账本；所有判断以
结构化结果（`DeploymentResult` JSON、HTTP 探测）为准，stderr 文本不作成功
依据。
