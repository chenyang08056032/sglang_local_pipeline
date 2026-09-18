# -*- coding: utf-8 -*-
"""节点执行器 (自动检测本地/远程)。

执行链: (本地直执 或 SSH) → git checkout → docker run → 容器内跑用例文件 → 拉回日志
"""

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 各架构的标准卡数 (挂卡数量的唯一事实来源, run.py 的配置校验也依赖它)
_ARCH_NPUS = {"a3": 16, "a5": 8}

_MGMT_DEVICES = ["/dev/davinci_manager", "/dev/hisi_hdc"]

# A3 节点标准挂载 (宿主机:容器), 与运维手册的 docker run 命令保持一致
_NODE_MOUNTS = [
    "/usr/local/sbin:/usr/local/sbin",
    "/usr/local/Ascend/driver:/usr/local/Ascend/driver",
    "/usr/local/Ascend/firmware:/usr/local/Ascend/firmware",
    "/etc/ascend_install.info:/etc/ascend_install.info",
    "/var/queue_schedule:/var/queue_schedule",
    "$HOME/.cache:/root/.cache",  # 模型缓存复用, 避免每次容器重复下载
]


# 执行机 IP 集合缓存: 单次运行中不变, 避免 _is_local 每次都 fork hostname 子进程
_LOCAL_IPS_CACHE = None


def _local_ips():
    """收集本机所有 IP, 用于判断节点是否就是执行机本身。

    首选 hostname -I: 直接枚举所有网卡 IP, 不受 /etc/hosts 把主机名
    映射到 127.0.1.1 的影响; 拿不到时兜底解析主机名。
    结果在单次运行中缓存复用 (执行机 IP 不会变化)。
    """
    global _LOCAL_IPS_CACHE
    if _LOCAL_IPS_CACHE is not None:
        return _LOCAL_IPS_CACHE
    ips = {"127.0.0.1", "localhost", "::1"}
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True,
                           text=True, timeout=10)
        # 只收 IP 形态的 token (含 . 或 :), 过滤主机名等杂项输出
        ips.update(t for t in r.stdout.split() if "." in t or ":" in t)
    except Exception:
        pass
    if len(ips) == 3:  # hostname -I 未产出任何网卡 IP, 兜底解析主机名
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ips.add(info[4][0])
        except Exception:
            pass
    _LOCAL_IPS_CACHE = ips
    return ips


def _is_local(node):
    """节点 host 是否指向本机。"""
    return node.host in _local_ips()


def ssh_run(node, command, log_path=None, dry_run=False, prefix=None, quiet=False):
    """执行命令: 本机节点直接 subprocess, 远程走 SSH。实时回显 + 写日志。

    prefix: 多角色并发执行时给控制台每行加前缀 (如 "[prefill] "); 日志文件始终是原始输出。
    quiet: 不回显控制台, 只写日志文件 (多机用例的 PD/worker 角色, 控制台只留测试角色)。
    开始/结束打印 [ssh] 连接诊断 (执行方式/目标节点/rc/耗时), 便于排查多机连接问题。
    """
    local = _is_local(node)
    if dry_run:
        tag = "local" if local else f"{node.user}@{node.host}:{node.port}"
        head = f"[dry-run] {prefix}{tag}$ " if prefix else f"[dry-run] {tag}$ "
        print(head + command)
        return 0
    # [连接诊断] 执行方式 + 目标节点 + 命令概要 (压平换行并截断, 避免刷屏);
    # 多机并发时各角色的诊断行带 prefix, 便于区分是哪个节点的连接
    target = "本机" if local else f"ssh {node.user}@{node.host}:{node.port}"
    preview = " ".join(command.split())[:120]
    tic = time.perf_counter()
    _log(f"{prefix or ''}[ssh] 开始 {target}: {preview}")
    if local:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="ignore", bufsize=1,
        )
    else:
        # BatchMode=yes: 禁止交互式提示 (host key 未知/需 passphrase 时直接失败而非挂死);
        # StrictHostKeyChecking=accept-new: 首次连接自动接受 host key, 避免提示
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(node.port), f"{node.user}@{node.host}", command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="ignore", bufsize=1,
        )
    log_f = None
    if log_path:
        # 先建目录再打开文件 (调用方不保证父目录已存在)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_f = open(log_path, "a", encoding="utf-8", errors="ignore")
    try:
        for line in proc.stdout:
            if not quiet:
                print(f"{prefix}{line}", end="") if prefix else print(line, end="")
            if log_f:
                log_f.write(line)
                log_f.flush()
    finally:
        if log_f:
            log_f.close()
        proc.wait()
    # [连接诊断] rc + 耗时: SSH 失败 (rc=255)、BatchMode 拒绝、命令超时等在此一目了然
    dur = round(time.perf_counter() - tic, 1)
    _log(f"{prefix or ''}[ssh] 结束 {target} rc={proc.returncode} 耗时 {dur}s")
    return proc.returncode


def ssh_fetch_dir(node, remote_dir, local_dir, dry_run=False):
    """拉回节点产物: 本机直接 cp, 远程走 tar 管道。
    tmp/ (数据集+torch 编译缓存) 体积大且排查价值低, 不回传:
    远程节点的 runs/ 原件始终保留, 需要深度排查时可手动重拉。
    """
    local = _is_local(node)
    if dry_run:
        tag = "local" if local else node.host
        print(f"[dry-run] fetch {tag}:{remote_dir} -> {local_dir} (不含 tmp/)")
        return 0
    os.makedirs(local_dir, exist_ok=True)
    if local:
        rc = subprocess.run(
            ["cp", "-r", f"{remote_dir}/.", local_dir],
            stderr=subprocess.STDOUT,
        ).returncode
        if rc == 0:
            shutil.rmtree(os.path.join(local_dir, "tmp"), ignore_errors=True)
        return rc
    pull = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-p", str(node.port), f"{node.user}@{node.host}",
         f"tar czf - -C '{remote_dir}' --exclude=./tmp ."],
        stdout=subprocess.PIPE,
    )
    extract = subprocess.Popen(["tar", "xzf", "-", "-C", local_dir], stdin=pull.stdout)
    pull.stdout.close()
    rc = extract.wait()
    pull_rc = pull.wait()
    # 任一端失败都算拉回失败 (ssh 断开 / 远程 tar 出错 / 本地 tar 解压出错)
    return rc if rc != 0 else pull_rc


def _log(msg):
    """带时间戳的流水线日志; 时间戳插在首个 [tag] 之后, 保持 tag 在视觉左侧。"""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    # 消息都以 [tag] 开头 (如 [ssh], [execute], [decode-0] [ssh]);
    # 把时间戳插到第一个 ] 之后, 不带 tag 的消息前补 [time]
    idx = msg.find(']')
    if idx > 0:
        out = f"{msg[:idx+1]} [{ts}]{msg[idx+1:]}"
    else:
        out = f"[{ts}] {msg}"
    print(out, flush=True)


# ---------------------------------------------------------------------------
# 多机 (PD 分离) 用例支持
#
# 这类用例 (TestNpuPerfMultiNodePdSepTestCaseBase) 原本跑在 K8s 上:
#   - HOSTNAME 环境变量区分角色 (prefill/decode/router), POD_IP 为本节点地址
#   - K8s ConfigMap 做协调: PD 节点从 ConfigMap 发现 prefill-0/decode-0 地址,
#     router 从 ConfigMap 收集 PD 地址列表; 测试结束后 router 修改
#     active-test-class 通知 PD 节点退出
# 本地无 K8s, 用「执行机上的 HTTP 协调服务 + 容器内 sitecustomize 注入 fake
# kubernetes client」做等价替代, 不修改 sglang 代码:
#   - CoordService 模拟 ConfigMap 的 read/patch
#   - sitecustomize.py 把用例用到的 kubernetes 接口重定向到协调服务
# ---------------------------------------------------------------------------

# 协调服务默认端口 (被占用则自动换随机端口); 节点容器需可达执行机的该端口
_COORD_DEFAULT_PORT = 9377
# 与 sglang 侧 test_npu_multi_node_utils.ACTIVE_TEST_CLASS 保持一致
_ACTIVE_TEST_CLASS_KEY = "active-test-class"
# 结束信号哨兵值: 与任何测试类名都不同, PD 节点看到后正常退出
_RELEASE_VALUE = "__pipeline_released__"
# 结束信号写入后给 PD/worker 节点的 grace period (轮询周期 ~30s);
# 超时仍未退出的 (如 router 卡在 wait_for_all_ports_ready 不查 ConfigMap)
# 主动 docker rm -f 杀掉, 等价 CI 外层 runner 检测 pod 非 Running 后删 job
_GRACE_PERIOD_KILL_SEC = 60
# 多机用例的角色 (每个角色对应一个节点配置)
_MULTI_ROLES = ("prefill", "decode", "router")

# 注入容器的 sitecustomize.py: 拦截 kubernetes 导入, 重定向到协调服务。
# 仅实现用例实际用到的接口子集 (read/patch ConfigMap, list Pod 仅打日志用)。
_SITECUSTOMIZE = '''\
# sglang_local_pipeline 注入: 无 K8s 环境下, 把多机用例依赖的 kubernetes
# 客户端重定向到流水线协调服务 (SGLANG_COORD_URL)。仅当该变量设置时激活。
import json
import os
import sys
import types
import urllib.request

_COORD_URL = os.environ.get("SGLANG_COORD_URL", "").rstrip("/")

if _COORD_URL and "kubernetes" not in sys.modules:

    # 协调服务在内网执行机上, 请求必须直连: 容器若预置了 http_proxy,
    # urllib 默认经代理访问内网地址会超时; 空 ProxyHandler 强制绕过代理
    # (不影响用例自身走代理下载模型等需求)
    _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class _ApiException(Exception):
        def __init__(self, status=0, reason=""):
            super().__init__(f"({status}) Reason: {reason}")
            self.status = status

    class _ConfigMap:
        def __init__(self, data=None):
            self.data = data

    class _PodList:
        items = []

    class _CoreV1Api:
        def read_namespaced_config_map(self, name, namespace):
            with _OPENER.open(_COORD_URL + "/configmap", timeout=15) as r:
                return _ConfigMap(json.loads(r.read().decode())["data"])

        def patch_namespaced_config_map(self, name, namespace, body):
            req = urllib.request.Request(
                _COORD_URL + "/configmap",
                data=json.dumps(body or {}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with _OPENER.open(req, timeout=15):
                pass
            return _ConfigMap()

        def list_namespaced_pod(self, namespace, label_selector=None):
            return _PodList()

    def _load_kube_config(kube_config=None):
        pass

    _k8s = types.ModuleType("kubernetes")
    _client = types.ModuleType("kubernetes.client")
    _config = types.ModuleType("kubernetes.config")
    _rest = types.ModuleType("kubernetes.client.rest")
    _client.CoreV1Api = _CoreV1Api
    _config.load_kube_config = _load_kube_config
    _rest.ApiException = _ApiException
    _k8s.client = _client
    _k8s.config = _config
    _client.rest = _rest
    _client.config = _config
    sys.modules.update({
        "kubernetes": _k8s,
        "kubernetes.client": _client,
        "kubernetes.config": _config,
        "kubernetes.client.rest": _rest,
    })
'''


# 注入容器的 run_case.py 包装器: A5 节点单机用例适配。
# 用例脚本的 --tp-size 按 A3 节点卡数配置, A5 上需减半; 单机用例的
# --tp-size 均经 other_args 传入 sglang.test.test_utils.popen_launch_server,
# 在此替换该函数实现减半, 不修改 sglang 代码 (与 sitecustomize 同思路,
# 但仅作用于主测试进程, 不影响 server/基准测试子进程)。
_RUN_CASE_WRAPPER = '''\
import os
import runpy
import sys

import sglang.test.test_utils as _tu

_orig_popen_launch_server = _tu.popen_launch_server


def _halve_tp_size(other_args):
    """把 other_args 里的 --tp-size 值除以 2 (针对 A3 卡数配置的脚本)。

    未配置 --tp-size 或值为 1 时保持不变 (1 减半会得到非法的 0)。
    """
    args = list(other_args or [])
    for i, a in enumerate(args):
        if a != "--tp-size" or i + 1 >= len(args):
            continue
        try:
            tp = int(args[i + 1])
        except (TypeError, ValueError):
            continue
        if tp >= 2:
            print(f"[a5-适配] --tp-size {tp} -> {tp // 2}")
            args[i + 1] = str(tp // 2)
    return args


def _popen_launch_server(*a, **kw):
    # other_args 可能是第 5 个位置参数 (model, base_url, timeout, api_key,
    # other_args) 或关键字参数, 两种传法都处理
    if len(a) >= 5:
        a = list(a)
        a[4] = _halve_tp_size(a[4])
    if "other_args" in kw:
        kw["other_args"] = _halve_tp_size(kw["other_args"])
    return _orig_popen_launch_server(*a, **kw)


_tu.popen_launch_server = _popen_launch_server

# 等价于 python3 {用例文件} -f: 修正 sys.path[0] (用例可能 import 同目录
# 模块), 以 __main__ 身份执行原用例文件, 退出码原样透传
_file = os.path.abspath(sys.argv[1])
sys.path[0] = os.path.dirname(_file)
sys.argv = [_file, "-f"]
runpy.run_path(_file, run_name="__main__")
'''


def _port_occupants(port):
    """占用指定监听端口的进程列表 [(pid, name, cmdline)]; 无法解析时返回 []。"""
    try:
        r = subprocess.run(["ss", "-tlnpH", f"sport = :{port}"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    occupants = []
    for line in r.stdout.splitlines():
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if not m:
            continue
        pid = int(m.group(2))
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = " ".join(
                    f.read().decode(errors="ignore").split("\0")).strip()
        except OSError:
            cmdline = m.group(1)
        occupants.append((pid, m.group(1), cmdline))
    return occupants


def _reclaim_coord_port():
    """释放被残留流水线进程占用的协调端口, 供新一轮多机用例使用。

    只清理 cmdline 含 run.py 的进程 (上一轮未退干净的流水线); 被无关进程
    误占时不动它, 返回 False 由调用方报错。返回 True 表示端口已可用。
    """
    # 防御性排除自身 pid, 避免极端情况下误杀当前进程
    occupants = [o for o in _port_occupants(_COORD_DEFAULT_PORT)
                 if o[0] != os.getpid()]
    stale = [o for o in occupants if "run.py" in o[2]]
    if not stale:
        # 无可清理对象: 要么已空闲 (对端恰好退出), 要么被无关进程占用
        return not occupants
    for pid, _, cmdline in stale:
        _log(f"[coord] 清理占用 {_COORD_DEFAULT_PORT} 的残留流水线进程: "
             f"pid={pid} ({cmdline})")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):  # 最多等 10s 让 SIGTERM 生效
        if not _port_occupants(_COORD_DEFAULT_PORT):
            return True
        time.sleep(0.5)
    for pid, _, _ in stale:
        _log(f"[coord] SIGTERM 未退出, 强杀 pid={pid}")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(1)
    return not _port_occupants(_COORD_DEFAULT_PORT)


def _kill_container(node, container_name):
    """强制删除节点上的容器 (任一节点退出后, 清理残留的未退出容器)。

    本机直接执行, 远程走 SSH; 不回显输出 (快速清理, 日志由调用方打印)。
    """
    cmd = f"docker rm -f {container_name} >/dev/null 2>&1 || true"
    if _is_local(node):
        subprocess.Popen(cmd, shell=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL).wait()
    else:
        subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(node.port), f"{node.user}@{node.host}", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).wait()


class _CoordHandler(BaseHTTPRequestHandler):
    """协调服务的 HTTP 接口: GET/PATCH /configmap (与 K8s patch 语义一致, 按 key 合并)。"""

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/configmap":
            self.send_error(404)
            return
        self._send_json({"data": self.server.coord.get_data()})

    def do_PATCH(self):
        if self.path != "/configmap":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_error(400)
            return
        self._send_json({"data": self.server.coord.patch_data(payload.get("data") or {})})

    def log_message(self, fmt, *args):
        pass  # 用例会持续轮询, 关闭访问日志避免刷屏


class CoordService:
    """执行机上的轻量协调服务, 模拟 K8s ConfigMap 的 read/patch。

    每个多机用例一个实例 (fresh 状态, 无跨用例残留)。
    """

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()
        self._httpd = None

    def start(self):
        # 环境契约: 节点侧只放行 9377 (见配置头部前置说明), 不回退随机端口
        # (远程节点会静默连不上)。端口被占时先自动清理残留的流水线进程,
        # 清不掉 (被无关进程占用) 才报错
        try:
            self._httpd = ThreadingHTTPServer(
                ("0.0.0.0", _COORD_DEFAULT_PORT), _CoordHandler)
        except OSError:
            if not _reclaim_coord_port():
                leftovers = _port_occupants(_COORD_DEFAULT_PORT)
                detail = "; ".join(f"pid={p} ({c})" for p, _, c in leftovers)
                kills = "; ".join(f"kill -9 {p}" for p, _, _ in leftovers)
                raise RuntimeError(
                    f"协调端口 {_COORD_DEFAULT_PORT} 被 {detail or '未知进程'} 占用, "
                    f"且无法自动清理, 手动处理: {kills or 'ss -tlnp 检查后处理'}, "
                    f"完成后重跑")
            try:
                self._httpd = ThreadingHTTPServer(
                    ("0.0.0.0", _COORD_DEFAULT_PORT), _CoordHandler)
            except OSError as e:
                raise RuntimeError(f"清理残留进程后仍无法绑定协调端口: {e}")
        self._httpd.coord = self
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self):
        return self._httpd.server_address[1]

    def get_data(self):
        with self._lock:
            return dict(self._data)

    def patch_data(self, data):
        with self._lock:
            self._data.update(data)
            return dict(self._data)


def _local_ip_towards(host):
    """执行机与指定节点通信所用的本机源 IP (UDP connect, 不实际发包)。

    用于告知容器内协调服务地址; 节点需可达执行机的该 IP。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 22))
            return s.getsockname()[0]
    except OSError:
        return None


def _stage_sitecustomize(node, run_dir, dry_run=False):
    """把 fake kubernetes 桥接写到节点 run 目录 (容器内 /output, 经 PYTHONPATH 生效)。"""
    path = f"{run_dir}/sitecustomize.py"
    if dry_run:
        print(f"[dry-run] 写入 {path}: fake kubernetes 协调桥接 "
              f"({len(_SITECUSTOMIZE.splitlines())} 行, 内容见 pipeline.py)")
        return 0
    cmd = (f"mkdir -p {shlex.quote(run_dir)} && "
           f"cat > {shlex.quote(path)} <<'SGL_PIPELINE_EOF'\n"
           f"{_SITECUSTOMIZE}"
           f"SGL_PIPELINE_EOF\n")
    return ssh_run(node, cmd)


def _stage_run_wrapper(node, run_dir, dry_run=False):
    """把 A5 适配包装器写到节点 run 目录 (容器内 /output/run_case.py)。"""
    path = f"{run_dir}/run_case.py"
    if dry_run:
        print(f"[dry-run] 写入 {path}: A5 --tp-size 减半包装器 "
              f"({len(_RUN_CASE_WRAPPER.splitlines())} 行, 内容见 pipeline.py)")
        return 0
    cmd = (f"mkdir -p {shlex.quote(run_dir)} && "
           f"cat > {shlex.quote(path)} <<'SGL_PIPELINE_EOF'\n"
           f"{_RUN_CASE_WRAPPER}"
           f"SGL_PIPELINE_EOF\n")
    return ssh_run(node, cmd)


def prepare_node(cfg, node, dry_run=False):
    """执行前准备一个节点。online=true: 镜像/代码自动就位+切到目标 ref;
    false: 完全使用节点现状 (离线模式, 镜像/代码已手动准备好)。

    online 模式需节点网络可达 (docker registry / git remote), 代理由节点环境预置。
    """
    repo = cfg.run.repo
    where = "本机" if _is_local(node) else f"{node.user}@{node.host}:{node.port}"
    lines = ["set -e"]
    if not cfg.run.prepare.online:
        # 离线: 不 pull / 不 clone / 不 fetch / 不 checkout
        _log(f"[prepare] {where} 离线模式: 跳过镜像/代码准备, 直接使用节点现状")
        _log(f"[prepare] repo={repo} (请自行确认代码已就位)")
        rc = ssh_run(node, "\n".join(lines), dry_run=dry_run)
        _log(f"[prepare] {where} 就绪 (rc={rc})")
        return rc == 0
    remote = cfg.run.git_remote
    # 镜像不存在才 pull
    _log(f"[prepare] {where} 检查镜像 (不存在则 pull): {cfg.run.docker.image}")
    image = shlex.quote(cfg.run.docker.image)
    lines.append(f"docker image inspect {image} >/dev/null 2>&1 "
                 f"|| docker pull {image}")
    # remote 变更时改指向 (保留仓库对象, 免重新 clone); 首次则 clone
    _log(f"[prepare] {where} 确保仓库就位: {repo} (remote={remote})")
    q_repo = shlex.quote(repo)
    q_remote = shlex.quote(remote)
    lines.append(
        f"if [ -d {q_repo}/.git ]; then\n"
        f"  cd {q_repo}\n"
        f"  [ \"$(git remote get-url origin)\" = {q_remote} ] || "
        f"git remote set-url origin {q_remote}\n"
        f"  cd - >/dev/null\n"
        f"else\n"
        f"  git clone {q_remote} {q_repo}\n"
        f"fi"
    )
    # fetch 只更新 origin/* 指针; checkout 切版本; 分支还需 reset --hard 对齐远端
    # (否则本地分支停在旧提交, checkout 到的是旧代码)。tag/commit 不 reset。
    _log(f"[prepare] {where} git fetch + checkout {cfg.run.ref}")
    q_ref = shlex.quote(cfg.run.ref)
    lines.append(f"cd {q_repo} && git fetch origin --tags --force")
    lines.append(f"git checkout --force {q_ref}")
    lines.append(f"if git show-ref --verify --quiet refs/remotes/origin/{q_ref}; then "
                 f"git reset --hard origin/{q_ref}; fi")
    rc = ssh_run(node, "\n".join(lines), dry_run=dry_run)
    if rc == 0:
        _log(f"[prepare] {where} 就绪: 镜像+代码已对齐 ref={cfg.run.ref}")
    else:
        _log(f"[prepare] {where} 失败 (rc={rc}), 详见上方节点输出")
    return rc == 0


def _build_cmd(cfg, suite, node, node_run_dir, role=None, extra_env=None,
               timeout_minutes=None, tp_halving=False):
    """构造节点上执行的完整命令。

    role: 多机用例的角色名 (prefill/decode/router), 决定容器名后缀;
    extra_env: 角色专属环境变量 (多机用例的 HOSTNAME/POD_IP/协调地址等);
    timeout_minutes: 覆盖 suite.timeout_minutes (多机用例 PD 角色加余量用);
    tp_halving: A5 单机用例, 经 /output/run_case.py 包装启动 (--tp-size 减半)。
    构造完成后打印 [cmd] 关键路径诊断 (容器名/节点/repo/输出目录/超时)。
    """
    repo = cfg.run.repo
    d = cfg.run.docker
    container = f"sgl-pipeline-{suite.name}" + (f"-{role}" if role else "")

    # 容器内命令
    parts = ["set -euo pipefail"]
    q_repo = shlex.quote(repo)
    # 覆盖镜像内 ascend 工具 (学 CI nightly 做法)
    # 注意: 对完整路径做 shlex.quote, 不能只引用前缀 (否则 /python/... 落在引号外,
    # 路径含空格时会被 shell 拆断); glob * 留在引号外以便 shell 展开
    ascend_src = shlex.quote(f"{repo}/python/sglang/test/ascend")
    parts.append(
        f"cp -r {ascend_src}/* "
        f"$(python3 -c 'import sglang, os; print(os.path.dirname(sglang.__file__))')/test/ascend/"
    )
    # 预置 gsm8k / ShareGPT 数据集 (与 CI 一致): /tmp 每次全新挂载, 不预置则会
    # 重复下载 (gsm8k) 或 perf 套件找不到数据; 缓存缺失时忽略, 走在线下载
    for f in ("tmp/test.jsonl",
              "otavia/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json"):
        parts.append(f"cp '/root/.cache/modelscope/hub/datasets/{f}' /tmp/ 2>/dev/null || true")
    if tp_halving:
        # A5 适配: 经包装器启动, 自动把 other_args 里的 --tp-size 减半
        cmd = (f"cd {q_repo} && python3 -u /output/run_case.py "
               f"{shlex.quote(suite.file)} -f")
    else:
        cmd = f"cd {q_repo} && python3 -u {shlex.quote(suite.file)} -f"
    parts.append(f"{cmd} 2>&1 | tee /output/case.log")

    inner = "\n".join(parts)

    # docker run 参数 (devices=auto 时卡数由 arch 决定, 显式列表以配置为准)
    ids = range(_ARCH_NPUS[node.arch]) if d.devices == "auto" else d.devices
    device_args = []
    for i in ids:
        device_args += ["--device", f"/dev/davinci{i}"]
    if device_args:
        # 挂了 davinci 卡才挂管理设备 (devices 显式配空列表时不挂)
        for dev in _MGMT_DEVICES:
            device_args += ["--device", dev]

    mount_args = [
        "-v", shlex.quote(f"{repo}:{repo}"),
        "-v", shlex.quote(f"{node_run_dir}:/output"),
        "-v", shlex.quote(f"{node_run_dir}/tmp:/tmp"),
        "-v", shlex.quote(f"{node_run_dir}/plog:/root/ascend/log"),
    ] + [x for m in _NODE_MOUNTS for x in ("-v", m)]
    # 用户自定义挂载 (追加在默认挂载之后), 路径含空格等特殊字符时整体 quote 防止拆断;
    # 格式同 docker -v, 支持只读等选项 (如 "/data:/data:ro")
    if d.extra_mounts:
        mount_args += [x for m in d.extra_mounts for x in ("-v", shlex.quote(m))]

    # shlex.quote: 值含空格等 shell 特殊字符时自动加引号, 保证 " ".join 后不被拆断
    envs = dict(cfg.run.env)
    if extra_env:
        envs.update(extra_env)
    # 容器默认 UTC, 流水线日志用本机时区 (Asia/Shanghai); 统一为 +8 避免同一
    # 输出里两个时区造成误判 (如 prefill/decode 跨节点时序分析)
    envs.setdefault("TZ", "Asia/Shanghai")
    # 内网直连: 节点 /root/.docker/config.json 的 proxies 配置会向容器注入
    # http_proxy, requests/urllib 访问内网地址 (协调服务 / server 健康检查)
    # 经代理会超时; 对所有节点 + 协调地址设置 no_proxy, 外网下载仍走代理。
    # 显式 -e 优先于 docker config 注入, 不被节点侧配置覆盖
    no_proxy = {n.host for n in cfg.nodes} | {"localhost", "127.0.0.1", "::1"}
    if str(envs.get("SGLANG_COORD_URL", "")).startswith("http"):
        no_proxy.add(urlparse(envs["SGLANG_COORD_URL"]).hostname)
    combined = ",".join(sorted(no_proxy))
    for k in ("no_proxy", "NO_PROXY"):
        envs[k] = f"{envs[k]},{combined}" if envs.get(k) else combined
    env_args = [v for k, val in envs.items()
                for v in ("-e", f"{k}={shlex.quote(str(val))}")]

    inner_escaped = inner.replace("'", "'\"'\"'")
    docker_args = (["docker", "run", "--rm", "--privileged",
                    "--net", d.net, "--ipc", "host",
                    "--shm-size", d.shm_size,
                    "--name", container]
                   + device_args + mount_args + env_args
                   + [shlex.quote(d.image), "bash", "-c", f"'{inner_escaped}'"])

    # 宿主机命令 (镜像/代码/版本由 prepare_node 提前保证)
    lines = [
        "set -e",
        f"mkdir -p {shlex.quote(f'{node_run_dir}/tmp')} {shlex.quote(f'{node_run_dir}/plog')}",
        # 清理残留同名容器: 上次 timeout 杀掉 docker 客户端后容器不会自停,
        # 不清理则本次 docker run 因 --name 冲突直接失败
        f"docker rm -f {container} >/dev/null 2>&1 || true",
    ]
    tmo = timeout_minutes if timeout_minutes is not None else suite.timeout_minutes
    if tmo:
        # -k: SIGTERM 后 60s 仍未退出则 SIGKILL, 防止容器内进程卡死挂住整个 run
        lines.append(f"timeout -k 60 {int(tmo) * 60} " + " ".join(docker_args))
    else:
        lines.append(" ".join(docker_args))
    # [路径诊断] 打印构造出的容器关键路径, 便于排查多机部署时的挂载/路径问题
    # (多机并发时各线程同时构造命令, 带节点 host 才能区分是哪个节点的容器)
    _log(f"[cmd] 容器={container} 节点={node.host} repo={repo} "
         f"输出目录={node_run_dir} 超时={tmo or '无'}分钟")
    return "\n".join(lines)


def _fetch_artifacts(node, node_run_dir, local_dir, label):
    """拉回一个执行单元的产物 (case.log + plog/, 不含 tmp/)。

    本机节点: 拷贝成功后清理 runs/ 侧副本省磁盘 (results/ 已有一份);
    远程节点: runs/ 是节点侧唯一原始数据, 始终保留。
    """
    frc = ssh_fetch_dir(node, node_run_dir, local_dir)
    if frc != 0:
        _log(f"[fetch] {label} 拉回失败 (rc={frc}), 保留节点侧原件: {node_run_dir}")
        return
    _log(f"[fetch] {label} 拉回完成 (case.log + plog/, 未回传 tmp/)")
    if not _is_local(node):
        return
    try:
        shutil.rmtree(node_run_dir)
        _log(f"[fetch] {label} 已清理本机节点侧目录: {node_run_dir}")
    except OSError as e:
        _log(f"[fetch] {label} 清理失败 ({e}), 保留: {node_run_dir}")
    # 上层目录空了则顺手清掉 (其他用例/角色还在用时 rmdir 自然失败)
    for d in (os.path.dirname(node_run_dir),
              os.path.dirname(os.path.dirname(node_run_dir))):
        try:
            os.rmdir(d)
        except OSError:
            pass


def execute_suite(cfg, suite, node, run_id, local_run_dir, dry_run=False):
    """执行一个单机用例, 返回结果 dict。"""
    result = {"name": suite.name, "node": node.host,
              "status": "fail", "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "duration_sec": 0, "error": None}
    node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}"
    local_suite_dir = os.path.join(local_run_dir, suite.name)
    where = "本机" if _is_local(node) else f"{node.user}@{node.host}:{node.port}"

    tic = time.perf_counter()
    try:
        # A5 节点: 脚本 --tp-size 按 A3 卡数配置, 注入包装器减半 (仅单机用例)
        tp_halving = node.arch == "a5"
        if tp_halving:
            _log(f"[execute] {suite.name} A5 适配: 注入 --tp-size 减半包装器 "
                 f"({node_run_dir}/run_case.py)")
            if _stage_run_wrapper(node, node_run_dir, dry_run) != 0:
                raise RuntimeError("写入 run_case.py 失败")
        cmd = _build_cmd(cfg, suite, node, node_run_dir, tp_halving=tp_halving)
        _log(f"[execute] {suite.name} @ {where} 启动容器 "
             f"(超时={suite.timeout_minutes or '无'}分钟, 节点输出目录={node_run_dir})")
        rc = ssh_run(node, cmd, log_path=os.path.join(local_suite_dir, "case.log"), dry_run=dry_run)
        result["status"] = "pass" if rc == 0 else "fail"
        result["error"] = None if rc == 0 else f"exit code {rc}"
        dur = round(time.perf_counter() - tic, 1)
        if rc == 0:
            _log(f"[execute] {suite.name} 容器正常结束 (rc=0, 耗时 {dur}s)")
        elif dry_run:
            _log(f"[execute] {suite.name} dry-run 完成命令打印")
        else:
            _log(f"[execute] {suite.name} 容器异常结束 (rc={rc}, 耗时 {dur}s), "
                 f"日志: {local_suite_dir}/case.log")
        if not dry_run:
            _log(f"[fetch] {suite.name} 拉回节点产物: {node_run_dir} -> {local_suite_dir}")
            _fetch_artifacts(node, node_run_dir, local_suite_dir, suite.name)
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] {suite.name} 流水线异常: {type(e).__name__}: {e}")
    result["duration_sec"] = round(time.perf_counter() - tic, 1)
    return result


def execute_multinode_suite(cfg, suite, run_id, local_run_dir, dry_run=False):
    """执行一个多机 PD 分离用例, 返回结果 dict。

    流程 (等价替代 CI/K8s 的多机协调, 详见文件头部说明):
      1. 执行机起协调服务 (模拟 ConfigMap), 预置所有 pod 注册信息
         (sglang-prefill-0/1.., sglang-decode-0/1..)
      2. 各节点并发 docker run 同一用例文件, 以环境变量区分角色和序号:
         - prefill/decode: 拉起 PD 服务, 轮询等待结束信号
         - router: 等 PD 端口就绪后拉起 router, /health 就绪后执行基准测试
      3. router 结束 (无论成败) 或任一 PD 提前退出 (服务崩溃) 后,
         向协调服务写结束信号, 通知剩余 PD 节点退出, 避免空等超时
      4. 全部节点 rc==0 才算通过 (正常结束时 PD 角色不跑测试, 收到信号后以 0 退出)

    支持多节点 PD: prefill/decode 各可配多个节点 (如 2p2d),
    router 固定单节点 (sglang 框架限制)。
    """
    # 展开每个角色为 (role, idx, node) 列表; idx 为该角色内的节点序号
    units = []
    for role in _MULTI_ROLES:
        for idx, host in enumerate(suite.roles[role]):
            node = cfg.find_node(host)
            if node is None:
                raise RuntimeError(
                    f"角色 {role}-{idx} 的节点 {host} 未在 nodes 中定义")
            units.append((role, idx, node))

    # unit key: "role-idx" (如 "prefill-0", "decode-1", "router-0")
    keys = [f"{r}-{i}" for r, i, _ in units]
    where_desc = " ".join(f"{k}@{n.host}" for (r, i, n), k in zip(units, keys))
    result = {"name": suite.name, "node": where_desc,
              "status": "fail", "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "duration_sec": 0, "error": None}

    # router 固定 key="router-0" (load_config 已保证 router 只有 1 个节点)
    router_key = "router-0"
    pd_keys = [k for k in keys if not k.startswith("router")]

    tic = time.perf_counter()
    coord = CoordService()
    try:
        # 预置所有 pod 注册信息 (K8s 里由各 pod 自注册; 用例据此发现各节点地址)
        coord_data = {f"sglang-{k}": n.host for (r, i, n), k in zip(units, keys)}
        if dry_run:
            # dry_run: 不启动协调服务, 不做网络探测 (否则不可达节点会 RuntimeError,
            # 失去 dry_run 仅校验命令的目的); 用占位 URL 让打印的 docker 命令完整可读
            coord_url = "<dry-run: coord not started>"
        else:
            coord.start()
            # 协调地址须用「节点可达执行机」的那个本机 IP
            coord_host = _local_ip_towards(units[0][2].host)
            if not coord_host:
                raise RuntimeError(
                    f"无法确定执行机与节点 {units[0][2].host} 互通的本机 IP, "
                    "多机用例的协调服务将不可达")
            coord_url = f"http://{coord_host}:{coord.port}"
            coord.patch_data(coord_data)
        _log(f"[execute] {suite.name} 多机用例 @ {where_desc} "
             f"(超时={suite.timeout_minutes or '无'}分钟/每节点)")
        _log(f"[execute] {suite.name} 协调服务: {coord_url} "
             f"(已预置 {len(coord_data)} 个 pod 注册: {', '.join(coord_data.keys())})")

        rc, errs, durs = {}, {}, {}

        def _run_unit(role, idx, node, key):
            node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}/{key}"
            unit_env = {
                # 用例框架按 HOSTNAME 识别角色 (须含角色名且以 -数字 结尾)
                "HOSTNAME": f"sglang-{key}",
                # 服务绑定/互访地址 = 节点 host (容器 host 网络)
                "POD_IP": node.host,
                "NAMESPACE": "sglang",
                "KUBE_CONFIG_MAP": "sglang-coord",
                "SGLANG_COORD_URL": coord_url,
                # /output/sitecustomize.py 注入 fake kubernetes (重定向到协调服务)
                "PYTHONPATH": "/output",
            }
            unit_tic = time.perf_counter()
            try:
                if _stage_sitecustomize(node, node_run_dir, dry_run) != 0:
                    rc[key] = -1
                    errs[key] = "写入 sitecustomize.py 失败"
                    return
                # PD 节点比 router 多留 2 分钟: router 结束后它们
                # 还需一个轮询周期 (~30s) 才收到结束信号退出,
                # 不留余量会被 timeout 误杀造成假失败
                tmo = (suite.timeout_minutes + 2
                       if suite.timeout_minutes and role != "router"
                       else suite.timeout_minutes)
                cmd = _build_cmd(cfg, suite, node, node_run_dir,
                                 role=key, extra_env=unit_env,
                                 timeout_minutes=tmo)
                log_path = os.path.join(local_run_dir, suite.name, key, "case.log")
                # PD 角色日志量大且与 router 交错, 只写文件不回显 (对齐 CI 各 pod
                # 日志隔离的观感, 控制台只看 router 的测试输出)
                rc[key] = ssh_run(node, cmd, log_path=log_path,
                                  dry_run=dry_run, prefix=f"[{key}] ",
                                  quiet=(role != "router"))
            except Exception as e:
                rc[key] = -1
                errs[key] = f"{type(e).__name__}: {e}"
            finally:
                durs[key] = round(time.perf_counter() - unit_tic, 1)

        threads = {k: threading.Thread(target=_run_unit,
                                      args=(r, i, n, k), daemon=True)
                   for (r, i, n), k in zip(units, keys)}
        # 先启 router, 等它向 ConfigMap 写入 active-test-class 后再启 PD;
        # 保证 PD 的首次 ConfigMap 查询能看到该 key (对齐 CI 的时序):
        # CI 的 router pod 不挂 NPU, 框架比 PD 快很多必先写; 本地 router 也挂
        # 满 NPU, 启动竞争无偏向, 需显式控制顺序。router 崩溃未写入时超时兜底
        if not dry_run:
            threads[router_key].start()
            _log(f"[execute] {suite.name} 先启动 router, 等待写入 active-test-class ...")
            router_seq_tic = time.perf_counter()
            while True:
                if _ACTIVE_TEST_CLASS_KEY in coord.get_data():
                    _log(f"[execute] {suite.name} active-test-class 已写入, 启动 PD 节点")
                    break
                if not threads[router_key].is_alive():
                    _log(f"[execute] {suite.name} router 提前退出 (未写入 active-test-class), "
                         "直接启动 PD 节点")
                    break
                if time.perf_counter() - router_seq_tic > 120:
                    _log(f"[execute] {suite.name} 等待 active-test-class 超时 (120s), "
                         "直接启动 PD 节点")
                    break
                time.sleep(2)
            for k in pd_keys:
                threads[k].start()
        else:
            for t in threads.values():
                t.start()
        # 监控: router 结束 (正常路径) 或任一 PD 提前退出 (崩溃) → 广播结束信号;
        # 信号写入后给 PD 节点 _GRACE_PERIOD_KILL_SEC 收到信号退出, 超时仍未退出的
        # (如 router 卡在 wait_for_all_ports_ready 不查 ConfigMap) 主动 docker rm -f
        released = False
        released_at = None
        killed = False
        while any(t.is_alive() for t in threads.values()):
            if (not released and not dry_run
                    and (not threads[router_key].is_alive()
                         or any(not threads[k].is_alive() for k in pd_keys))):
                coord.patch_data({_ACTIVE_TEST_CLASS_KEY: _RELEASE_VALUE})
                released = True
                released_at = time.perf_counter()
                # 区分释放原因便于排查: router 退出 (正常/异常) vs PD 提前退出 (崩溃)
                # 注意: router 异常退出时 PD 可能仍在服务, 此信号会让 PD 提前退出,
                # 测试结果应以 router 的 case.log 为准
                cause = ("router 退出" if not threads[router_key].is_alive()
                         else "PD 节点提前退出")
                _log(f"[execute] {suite.name} 结束信号已写入协调服务 ({cause}), "
                     "等待 prefill/decode 节点退出")
            # grace period 超时后, 仍有容器未退出 → 强杀 (等价 CI 外层 runner
            # 检测 pod 非 Running 后删 job; 常见于 PD 崩溃后 router 卡在端口等待)
            if (released and not killed and not dry_run
                    and time.perf_counter() - released_at > _GRACE_PERIOD_KILL_SEC):
                for (r, i, n), k in zip(units, keys):
                    if threads[k].is_alive():
                        container = f"sgl-pipeline-{suite.name}-{k}"
                        _log(f"[execute] {suite.name}/{k} 超过 {_GRACE_PERIOD_KILL_SEC}s "
                             f"未退出, 强制删除容器 {container}")
                        _kill_container(n, container)
                killed = True
            time.sleep(5)
        for t in threads.values():
            t.join()

        for key in keys:
            dur = durs.get(key, "?")
            if errs.get(key):
                _log(f"[execute] {suite.name}/{key} 异常 (耗时 {dur}s): {errs[key]}")
            elif rc.get(key) == 0:
                _log(f"[execute] {suite.name}/{key} 正常结束 (rc=0, 耗时 {dur}s)")
            else:
                # PD/worker 角色不回显控制台, 失败时指明日志位置便于排查
                _log(f"[execute] {suite.name}/{key} 异常结束 (rc={rc.get(key)}, 耗时 {dur}s), "
                     f"日志: {os.path.join(local_run_dir, suite.name, key, 'case.log')}")

        # rc.get(k) 为 None 时 (线程在赋值前异常退出, 如 KeyboardInterrupt) 归为
        # error 而非误导性的 "exit code None"
        failed = {k: (errs.get(k)
                      or (None if rc.get(k) == 0
                          else (f"exit code {rc.get(k)}"
                                if rc.get(k) is not None
                                else "未执行 (线程异常退出)")))
                  for k in keys}
        if all(v is None for v in failed.values()):
            result["status"] = "pass"
        else:
            result["error"] = "; ".join(f"{k}: {v}" for k, v in failed.items() if v)

        if not dry_run:
            for (role, idx, node), key in zip(units, keys):
                node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}/{key}"
                local_unit_dir = os.path.join(local_run_dir, suite.name, key)
                _log(f"[fetch] {suite.name}/{key} 拉回节点产物: "
                     f"{node_run_dir} -> {local_unit_dir}")
                _fetch_artifacts(node, node_run_dir, local_unit_dir, f"{suite.name}/{key}")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] {suite.name} 流水线异常: {type(e).__name__}: {e}")
    finally:
        coord.stop()
    result["duration_sec"] = round(time.perf_counter() - tic, 1)
    return result


def execute_multinode_tp_suite(cfg, suite, run_id, local_run_dir, dry_run=False):
    """执行一个多机混布 TP 用例, 返回结果 dict。

    与 PD 分离的区别:
      - 无 prefill/decode/router 角色, 所有节点组成一个 TP 实例
      - HOSTNAME = sglang-node-{idx} (用例框架据此区分 master/worker)
      - master (node-0) 启动 sglang server 并执行测试; worker 只起 server
      - master 测试结束后广播结束信号, worker 收到后正常退出

    协调机制与 PD 分离相同: CoordService + sitecustomize.py, 不修改 sglang 代码。
    """
    # 展开节点列表为 (idx, node); idx=0 为 master
    units = []
    for idx, host in enumerate(suite.multinode):
        node = cfg.find_node(host)
        if node is None:
            raise RuntimeError(
                f"节点 {host} 未在 nodes 中定义")
        units.append((idx, node))

    keys = [f"node-{i}" for i, _ in units]
    where_desc = " ".join(f"{k}@{n.host}" for (_, n), k in zip(units, keys))
    result = {"name": suite.name, "node": where_desc,
              "status": "fail", "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "duration_sec": 0, "error": None}

    master_key = "node-0"
    worker_keys = [k for k in keys if k != master_key]

    tic = time.perf_counter()
    coord = CoordService()
    try:
        # 预置所有 pod 注册信息: sglang-node-0, sglang-node-1, ...
        # 用例的 launch_pd_mix_node 据此发现 master IP, 拼 --dist-init-addr
        coord_data = {f"sglang-{k}": n.host for (_, n), k in zip(units, keys)}
        if dry_run:
            # dry_run: 不启动协调服务, 不做网络探测 (否则不可达节点会 RuntimeError,
            # 失去 dry_run 仅校验命令的目的); 用占位 URL 让打印的 docker 命令完整可读
            coord_url = "<dry-run: coord not started>"
        else:
            coord.start()
            coord_host = _local_ip_towards(units[0][1].host)
            if not coord_host:
                raise RuntimeError(
                    f"无法确定执行机与节点 {units[0][1].host} 互通的本机 IP, "
                    "多机用例的协调服务将不可达")
            coord_url = f"http://{coord_host}:{coord.port}"
            coord.patch_data(coord_data)
        _log(f"[execute] {suite.name} 多机混布 @ {where_desc} "
             f"(超时={suite.timeout_minutes or '无'}分钟/每节点)")
        _log(f"[execute] {suite.name} 协调服务: {coord_url} "
             f"(已预置 {len(coord_data)} 个 pod 注册: {', '.join(coord_data.keys())})")

        rc, errs, durs = {}, {}, {}

        def _run_unit(idx, node, key):
            node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}/{key}"
            unit_env = {
                # 用例框架按 HOSTNAME 识别 master/worker (须以 -数字 结尾)
                "HOSTNAME": f"sglang-{key}",
                "POD_IP": node.host,
                "NAMESPACE": "sglang",
                "KUBE_CONFIG_MAP": "sglang-coord",
                "SGLANG_COORD_URL": coord_url,
                "PYTHONPATH": "/output",
            }
            unit_tic = time.perf_counter()
            try:
                if _stage_sitecustomize(node, node_run_dir, dry_run) != 0:
                    rc[key] = -1
                    errs[key] = "写入 sitecustomize.py 失败"
                    return
                # worker 比 master 多留 2 分钟: master 结束后 worker
                # 还需一个轮询周期才收到结束信号退出
                is_master = key == master_key
                tmo = (suite.timeout_minutes
                       if is_master or not suite.timeout_minutes
                       else suite.timeout_minutes + 2)
                cmd = _build_cmd(cfg, suite, node, node_run_dir,
                                 role=key, extra_env=unit_env,
                                 timeout_minutes=tmo)
                log_path = os.path.join(local_run_dir, suite.name, key, "case.log")
                # worker 角色只写文件不回显, 控制台只保留 master 的测试输出
                rc[key] = ssh_run(node, cmd, log_path=log_path,
                                  dry_run=dry_run, prefix=f"[{key}] ",
                                  quiet=(key != master_key))
            except Exception as e:
                rc[key] = -1
                errs[key] = f"{type(e).__name__}: {e}"
            finally:
                durs[key] = round(time.perf_counter() - unit_tic, 1)

        threads = {k: threading.Thread(target=_run_unit,
                                      args=(i, n, k), daemon=True)
                   for (i, n), k in zip(units, keys)}
        for t in threads.values():
            t.start()
        # 监控: master 结束 (正常路径) 或任一 worker 提前退出 (崩溃) → 广播结束信号;
        # 信号写入后给 worker 节点 _GRACE_PERIOD_KILL_SEC 收到信号退出, 超时仍未退出的
        # 主动 docker rm -f (等价 CI 外层 runner 检测 pod 非 Running 后删 job)
        released = False
        released_at = None
        killed = False
        while any(t.is_alive() for t in threads.values()):
            if (not released and not dry_run
                    and (not threads[master_key].is_alive()
                         or any(not threads[k].is_alive() for k in worker_keys))):
                coord.patch_data({_ACTIVE_TEST_CLASS_KEY: _RELEASE_VALUE})
                released = True
                released_at = time.perf_counter()
                # 区分释放原因便于排查: master 退出 (正常/异常) vs worker 提前退出 (崩溃)
                # 注意: master 异常退出时 worker 可能仍在服务, 此信号会让 worker 提前退出,
                # 测试结果应以 master 的 case.log 为准
                cause = ("master 退出" if not threads[master_key].is_alive()
                         else "worker 节点提前退出")
                _log(f"[execute] {suite.name} 结束信号已写入协调服务 ({cause}), "
                     "等待 worker 节点退出")
            # grace period 超时后, 仍有容器未退出 → 强杀
            if (released and not killed and not dry_run
                    and time.perf_counter() - released_at > _GRACE_PERIOD_KILL_SEC):
                for (i, n), k in zip(units, keys):
                    if threads[k].is_alive():
                        container = f"sgl-pipeline-{suite.name}-{k}"
                        _log(f"[execute] {suite.name}/{k} 超过 {_GRACE_PERIOD_KILL_SEC}s "
                             f"未退出, 强制删除容器 {container}")
                        _kill_container(n, container)
                killed = True
            time.sleep(5)
        for t in threads.values():
            t.join()

        for key in keys:
            dur = durs.get(key, "?")
            if errs.get(key):
                _log(f"[execute] {suite.name}/{key} 异常 (耗时 {dur}s): {errs[key]}")
            elif rc.get(key) == 0:
                _log(f"[execute] {suite.name}/{key} 正常结束 (rc=0, 耗时 {dur}s)")
            else:
                # PD/worker 角色不回显控制台, 失败时指明日志位置便于排查
                _log(f"[execute] {suite.name}/{key} 异常结束 (rc={rc.get(key)}, 耗时 {dur}s), "
                     f"日志: {os.path.join(local_run_dir, suite.name, key, 'case.log')}")
        # rc.get(k) 为 None 时 (线程在赋值前异常退出, 如 KeyboardInterrupt) 归为
        # error 而非误导性的 "exit code None"
        failed = {k: (errs.get(k)
                      or (None if rc.get(k) == 0
                          else (f"exit code {rc.get(k)}"
                                if rc.get(k) is not None
                                else "未执行 (线程异常退出)")))
                  for k in keys}
        if all(v is None for v in failed.values()):
            result["status"] = "pass"
        else:
            result["error"] = "; ".join(f"{k}: {v}" for k, v in failed.items() if v)

        if not dry_run:
            for (idx, node), key in zip(units, keys):
                node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}/{key}"
                local_unit_dir = os.path.join(local_run_dir, suite.name, key)
                _log(f"[fetch] {suite.name}/{key} 拉回节点产物: "
                     f"{node_run_dir} -> {local_unit_dir}")
                _fetch_artifacts(node, node_run_dir, local_unit_dir, f"{suite.name}/{key}")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] {suite.name} 流水线异常: {type(e).__name__}: {e}")
    finally:
        coord.stop()
    result["duration_sec"] = round(time.perf_counter() - tic, 1)
    return result
