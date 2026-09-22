# -*- coding: utf-8 -*-
"""节点执行器 (自动检测本地/远程)。

执行链: 执行机准备代码仓 (git, 唯一代码准备点) → 清理各节点旧仓并从执行机复制
→ (本地直执 或 SSH) docker run → 容器内跑用例文件 → 拉回日志
"""

import json
import os
import re
import shlex
import shutil
import signal
import uuid
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 各架构的标准卡数 (挂卡数量的唯一事实来源, run.py 的配置校验也依赖它)
_ARCH_NPUS = {"a3": 16, "a5": 8}

_MGMT_DEVICES = ["/dev/davinci_manager", "/dev/hisi_hdc"]

# evalscope 官方源码仓 (精度框架; prepare 阶段浅克隆到执行机 {workspace}/evalscope)
_EVALSCOPE_REPO = "https://github.com/modelscope/evalscope.git"

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


def _log_filename(suite):
    """用例脚本名去 .py 加 .log, 作为容器内落盘日志和本地 log_path 的统一文件名。
    比固定的 'case.log' 更直观: 文件名即用例名, 一眼可知是哪个用例的日志。
    """
    return os.path.splitext(os.path.basename(suite.file))[0] + ".log"


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


def _workspace_shared_with_exec(node, workspace, dry_run=False):
    """检测节点的 {workspace} 是否与执行机同一物理目录 (共享存储如 NFS)。

    用于跳过 sync_repo_to_node / evalscope 分发的 rm+push, 防止共享目录下
    自毁执行机代码仓。原理: 执行机写唯一标记文件, SSH 到节点检查能否看到,
    看到即共享 (本机节点调用方已过滤, 此处必为远程)。dry_run 不做探测,
    返回 False 走默认打印路径。
    """
    if dry_run:
        return False
    probe = os.path.join(workspace, f".sglang-share-probe-{uuid.uuid4().hex[:8]}")
    try:
        open(probe, "w").close()
    except OSError:
        return False  # workspace 不存在或不可写, 走 sync 让它正常报错
    try:
        rc = ssh_run(node, f"test -f {shlex.quote(probe)}", quiet=True)
        return rc == 0
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


# 容器内 Python logging 行 (asctime,mmm - LEVEL - msg): 控制台改写为与 _log
# 一致的 [ts] LEVEL - msg, 两种来源的日志格式统一; 日志文件始终保留原始行
_PY_LOG_LINE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - ([A-Z]+) - (.*)'
)


def ssh_run(node, command, log_path=None, diag_path=None,
            dry_run=False, prefix=None, quiet=False):
    """执行命令: 本机节点直接 subprocess, 远程走 SSH。实时回显 + 写日志。

    prefix: 多角色并发执行时给控制台每行加前缀 (如 "[prefill] "); 日志文件始终是原始输出。
    quiet: 不回显控制台, 只写日志文件 (多机用例的 PD/worker 角色, 控制台只留测试角色);
        [ssh] 连接诊断同样只落日志, 失败 (rc!=0) 时强制上控制台, 不静默失败。
    log_path: 容器实时输出 (stdout 流) 写入此文件; fetch 阶段会被容器内落盘版本覆盖
        (容器内重定向直写 /output, 更完整: 不受 SSH 断开/timeout 截断影响)。
    diag_path: [ssh] 连接诊断 (开始/结束/rc/耗时) 写入此独立文件; fetch 不覆盖,
        保证用例日志被覆盖后仍可查连接记录。无 diag_path 时回退写 log_path。
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
    # 实时流文件 (用例名.log): fetch 阶段会被容器内落盘版本覆盖
    # 诊断文件 (ssh.log): 独立保留, fetch 不覆盖, 保证连接记录可查
    log_f = None
    diag_f = None
    for p in (log_path, diag_path):
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
    if log_path:
        log_f = open(log_path, "a", encoding="utf-8", errors="ignore")
    if diag_path:
        diag_f = open(diag_path, "a", encoding="utf-8", errors="ignore")

    def _diag(msg, force=False):
        # 诊断优先落 diag_f (独立文件, fetch 不覆盖);
        # 无 diag_f 时回退落 log_f (会被 fetch 覆盖, 但好过完全不记录)
        for f in (diag_f, log_f):
            if f:
                f.write(msg + "\n")
                f.flush()
        if not quiet or force:  # quiet 角色失败时 (force) 仍上控制台, 不静默失败
            _log(msg)

    _diag(f"{prefix or ''}[ssh] 开始 {target}: {preview}")
    if local:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="ignore", bufsize=1,
        )
    else:
        # BatchMode=yes: 禁止交互式提示 (host key 未知/需 passphrase 时直接失败而非挂死);
        # StrictHostKeyChecking=accept-new: 首次连接自动接受 host key, 避免提示;
        # LogLevel=ERROR: 抑制登录 banner ("Authorized users only..."), 纯噪音
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "LogLevel=ERROR",
             "-p", str(node.port), f"{node.user}@{node.host}", command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="ignore", bufsize=1,
        )
    try:
        for line in proc.stdout:
            if not quiet:
                # 容器 logging 行改写为与 _log 一致的 [ts] LEVEL - msg (仅控制台)
                m = _PY_LOG_LINE.match(line)
                shown = (f"[{m.group(1)}] {m.group(2)} - {m.group(3)}\n"
                         if m else line)
                print(f"{prefix}{shown}", end="") if prefix else print(shown, end="")
            if log_f:
                log_f.write(line)
                log_f.flush()
    finally:
        proc.wait()
        # [连接诊断] rc + 耗时: SSH 失败 (rc=255)、BatchMode 拒绝、命令超时等在此一目了然;
        # 必须在关闭文件之前写入: 对已关闭的文件 write 会抛
        # "ValueError: I/O operation on closed file" (角色线程直接崩溃, 容器不会启动)
        dur = round(time.perf_counter() - tic, 1)
        try:
            _diag(f"{prefix or ''}[ssh] 结束 {target} rc={proc.returncode} 耗时 {dur}s",
                  force=(proc.returncode != 0))
        finally:
            if log_f:
                log_f.close()
            if diag_f:
                diag_f.close()
    return proc.returncode


# 不回传的产物: tmp/ (数据集+torch 编译缓存) 体积大且排查价值低;
# run_case.py / sitecustomize.py 是流水线注入的固定脚本 (内容为
# _RUN_CASE_WRAPPER / _SITECUSTOMIZE 常量), 连同其编译缓存 __pycache__/
# 均无回传价值, 均不进 results (节点 runs/ 原件保留, 需要时可手动重拉)
_FETCH_EXCLUDES = ("tmp", "__pycache__", "run_case.py", "sitecustomize.py")


def _rm_fetch_excluded(local_dir):
    """删除不回传产物 (本机 cp 无法排除, 拷完再删)。"""
    for name in _FETCH_EXCLUDES:
        p = os.path.join(local_dir, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


def ssh_fetch_dir(node, remote_dir, local_dir, dry_run=False):
    """拉回节点产物: 本机直接 cp, 远程走 tar 管道。
    _FETCH_EXCLUDES 中的内容不回传; 远程节点的 runs/ 原件始终保留,
    需要深度排查时可手动重拉。
    """
    local = _is_local(node)
    if dry_run:
        tag = "local" if local else node.host
        print(f"[dry-run] fetch {tag}:{remote_dir} -> {local_dir} "
              f"(不含 {'/'.join(_FETCH_EXCLUDES)})")
        return 0
    os.makedirs(local_dir, exist_ok=True)
    if local:
        rc = subprocess.run(
            ["cp", "-r", f"{remote_dir}/.", local_dir],
            stderr=subprocess.STDOUT,
        ).returncode
        if rc == 0:
            _rm_fetch_excluded(local_dir)
        return rc
    excludes = "".join(f" --exclude=./{n}" for n in _FETCH_EXCLUDES)
    pull = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "LogLevel=ERROR",
         "-p", str(node.port), f"{node.user}@{node.host}",
         f"tar czf - -C '{remote_dir}'{excludes} ."],
        stdout=subprocess.PIPE,
    )
    # tar 的 "time stamp in the future" 告警是节点时钟偏差的免费探测器: 逐条刷屏
    # 但完全丢弃会丢失该信号, 故压成一行汇总; 其余 tar 错误原样透出
    extract = subprocess.Popen(
        ["tar", "xzf", "-", "-C", local_dir],
        stdin=pull.stdout, stderr=subprocess.PIPE, text=True, errors="ignore")
    pull.stdout.close()
    _, tar_err = extract.communicate()
    rc = extract.returncode
    pull_rc = pull.wait()
    if tar_err:
        future = [l for l in tar_err.splitlines() if "in the future" in l]
        for l in tar_err.splitlines():
            if "in the future" not in l:
                _log(f"[fetch] tar: {l}")
        if future:
            _log(f"[fetch] 警告: {node.host} 时钟快于本机 "
                 f"(检出 {len(future)} 个未来时间戳, 明细略), 建议校时")
    # 任一端失败都算拉回失败 (ssh 断开 / 远程 tar 出错 / 本地 tar 解压出错)
    return rc if rc != 0 else pull_rc


def ssh_push_dir(node, local_dir, remote_dir, dry_run=False):
    """推送执行机目录到远程节点: tar 打包经 ssh 管道, 远程解压 (fetch 的反方向)。

    目标目录由调用方先清理 (sync_repo_to_node 先 rm -rf 旧仓), 此处只负责
    传输; 远程 mkdir -p 兜底建目录。tar 用 "." 打包含隐藏文件 (.git 等),
    整仓复制保证节点与执行机代码严格一致。
    """
    if dry_run:
        print(f"[dry-run] push {local_dir} -> "
              f"{node.user}@{node.host}:{remote_dir}")
        return 0
    push = subprocess.Popen(
        ["tar", "czf", "-", "-C", local_dir, "."],
        stdout=subprocess.PIPE,
    )
    q_dir = shlex.quote(remote_dir)
    extract = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "LogLevel=ERROR",
         "-p", str(node.port), f"{node.user}@{node.host}",
         f"mkdir -p {q_dir} && tar xzf - -C {q_dir}"],
        stdin=push.stdout, stderr=subprocess.PIPE, text=True, errors="ignore")
    push.stdout.close()
    _, err = extract.communicate()
    rc = extract.returncode
    push_rc = push.wait()
    if err:
        # 与 fetch 同规则: 未来时间戳告警 (执行机时钟快于节点) 汇总为一行,
        # 其余 tar 错误原样透出
        future = [l for l in err.splitlines() if "in the future" in l]
        for l in err.splitlines():
            if "in the future" not in l:
                _log(f"[push] tar: {l}")
        if future:
            _log(f"[push] 警告: 执行机时钟快于 {node.host} "
                 f"(检出 {len(future)} 个未来时间戳, 明细略), 建议校时")
    # 任一端失败都算推送失败 (本地 tar 出错 / ssh 断开 / 远程解压出错)
    return rc if rc != 0 else push_rc


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

# 协调服务固定端口 (不回退随机端口: 环境只放行 9377, 换端口远程节点会静默
# 连不上; 被占时先清理残留流水线进程, 清不掉才报错, 见 CoordService.start)
_COORD_DEFAULT_PORT = 9377
# 与 sglang 侧 test_npu_multi_node_utils.ACTIVE_TEST_CLASS 保持一致
_ACTIVE_TEST_CLASS_KEY = "active-test-class"
# 结束信号哨兵值: 与任何测试类名都不同, PD 节点看到后正常退出
_RELEASE_VALUE = "__pipeline_released__"
# 结束信号写入后给 PD/worker 节点的 grace period (轮询周期 ~30s);
# 超时仍未退出的 (如 router 卡在 wait_for_all_ports_ready 不查 ConfigMap)
# 主动 docker rm -f 杀掉, 等价 CI 外层 runner 检测 pod 非 Running 后删 job
_GRACE_PERIOD_KILL_SEC = 60

# 用例未配 timeout_minutes 时的兜底超时 (分钟), 防止容器内进程 hang 导致整个 run 卡死
_DEFAULT_TIMEOUT_MINUTES = 60
# 多机用例的角色 (每个角色对应一个节点配置)
_MULTI_ROLES = ("prefill", "decode", "router")

# 注入容器的 sitecustomize.py (经 PYTHONPATH=/output 加载, 单机/多机统一):
#   - 多机用例: 拦截 kubernetes 导入, 重定向到协调服务。仅实现用例实际用到
#     的接口子集 (read/patch ConfigMap, list Pod 仅打日志用);
#   - 精度用例: evalscope venv 进程关闭 requests SSL 校验 (见文件内注释)。
_SITECUSTOMIZE = '''\
# sglang_local_pipeline 注入 (经 PYTHONPATH=/output 加载):
#   - 无 K8s 环境下, 把多机用例依赖的 kubernetes 客户端重定向到流水线协调
#     服务 (SGLANG_COORD_URL)。仅当该变量设置时激活。
#   - evalscope venv (test_env_evalscope) 的 python 进程: 关闭 requests SSL
#     校验, 规避企业代理自签证书导致数据集下载 CERTIFICATE_VERIFY_FAILED。
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

# evalscope venv (test_env_evalscope) 的 python 进程: 关闭 requests SSL 校验。
# 企业代理以自签证书劫持外网 HTTPS, venv 内 requests 下载 modelscope 数据集
# 报 CERTIFICATE_VERIFY_FAILED; 补丁放在 merge_environment_settings 层, 覆盖
# 会话默认值与调用方显式传入的 verify (含 verify=True)。其余 python 进程
# (sglang server / 用例主进程 / 系统 python) 前缀不匹配, 完全不受影响。
if "test_env_evalscope" in sys.prefix:
    try:
        import requests
        import urllib3

        _orig_merge = requests.Session.merge_environment_settings

        def _merge_no_verify(self, *args, **kwargs):
            # 原方法返回 dict (非上下文管理器), 包装成普通函数:
            # 在合并结果上强制 verify=False, 覆盖默认值与调用方显式传参
            settings = _orig_merge(self, *args, **kwargs)
            settings["verify"] = False
            return settings

        requests.Session.merge_environment_settings = _merge_no_verify
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print("[py_hook] requests SSL verification disabled (evalscope venv)")
    except Exception:
        pass
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


def _stage_sitecustomize(node, run_dir, dry_run=False, prefix=None, quiet=False,
                         diag_path=None):
    """把 sitecustomize.py 写到节点 run 目录 (容器内 /output, 经 PYTHONPATH 生效):
    fake kubernetes 协调桥接 (多机用例) + evalscope venv 的 requests SSL 补丁。
    prefix/quiet/diag_path 透传 ssh_run: 与主命令同规则, quiet 角色的 [ssh] 诊断
    只落 ssh.log 不刷控制台 (失败时仍上控制台), 保证连接记录完整。
    staging 只记诊断不记实时流 (一条 cat 命令, 无容器 stdout)。
    """
    path = f"{run_dir}/sitecustomize.py"
    if dry_run:
        print(f"[dry-run] 写入 {path}: fake k8s 桥接 + evalscope venv SSL 补丁 "
              f"({len(_SITECUSTOMIZE.splitlines())} 行, 内容见 pipeline.py)")
        return 0
    cmd = (f"mkdir -p {shlex.quote(run_dir)} && "
           f"cat > {shlex.quote(path)} <<'SGL_PIPELINE_EOF'\n"
           f"{_SITECUSTOMIZE}"
           f"SGL_PIPELINE_EOF\n")
    return ssh_run(node, cmd, prefix=prefix, quiet=quiet, diag_path=diag_path)


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


class _ExecMachine:
    """执行机自身的伪节点: host=localhost 恒在 _local_ips() 集合内,
    ssh_run 对其走本地 subprocess 分支——供执行机本地命令 (git 等) 复用
    同一套实时回显 / [ssh] 诊断逻辑。
    """

    host = "localhost"
    user = "root"
    port = 22


def prepare_local_repo(cfg, dry_run=False):
    """在执行机上准备 sglang 代码仓 (唯一代码准备点, 各节点代码均由此复制)。

    无 prepare 开关, 由 {workspace}/sglang 是否存在且非空决定行为 (与
    evalscope 同语义; os.listdir 不滤隐藏文件, .git 也算非空):
      - 存在且非空: 直接使用现状, 不做任何 git 操作 (用户对版本有完全
        控制, 自行 clone/checkout/分支切换; 流水线不干预)
      - 不存在/空目录: 用 run.env 配置的代理 (http_proxy/https_proxy)
        执行原子 clone (先落 {repo}.cloning 临时目录, 成功后清主目录
        再同分区 rename 发布); 配了 ref 用 git clone -b {ref} (只
        clone, 不 fetch/checkout/reset); 无 git_remote 时报错提示
        手动准备。clone 中断只留残骸在 .cloning (下次无条件清理),
        主目录只有 "缺失/空" 与 "完整" 两种状态, 非空检查不会被
        半成品误判 (失败重跑即恢复)
    """
    repo = cfg.run.repo
    # 无条件清理上次中断 clone 留下的临时目录 (原子 clone 残骸; dry-run 不动磁盘)
    if not dry_run:
        shutil.rmtree(f"{repo}.cloning", ignore_errors=True)
    if os.path.isdir(repo) and os.listdir(repo):
        if dry_run:
            print(f"[dry-run] 执行机代码仓 {repo} 已存在且非空, 使用现状 (不做 git 操作)")
            return True
        _log(f"[prepare] 执行机代码仓就绪 (使用现状, 不做 git 操作): {repo}")
        return True

    remote = cfg.run.git_remote
    if not remote:
        state = "不存在" if not os.path.isdir(repo) else "为空目录"
        if dry_run:
            print(f"[dry-run] 执行机代码仓 {repo} {state} 且未配 run.code.git_remote, "
                  f"将报错")
            return False
        _log(f"[错误] 执行机代码仓{state}: {repo}  且未配 run.code.git_remote, "
             f"无法 clone; 手动准备如: git clone <remote> -b <ref> {repo}")
        return False

    # 代理显式取 run.env (http_proxy 等): 该配置只注入容器, 执行机 shell 拿不到
    proxy_env = " ".join(
        f"{k}={shlex.quote(str(cfg.run.env[k]))}"
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
        if cfg.run.env.get(k))
    proxy_prefix = f"env {proxy_env} " if proxy_env else ""

    q_repo = shlex.quote(repo)
    q_tmp = shlex.quote(f"{repo}.cloning")  # 临时目录与主目录同父, mv 必为同分区 rename
    q_remote = shlex.quote(remote)
    ref = cfg.run.ref
    # 只 clone, 不做 fetch/checkout/reset: 用户对版本有完全控制, 流水线
    # 仅在仓不存在时帮忙拉一份默认/指定 ref 的代码; 后续版本切换由用户做。
    # 原子 clone: 成功后才清主目录 (此时必为空/缺失) 并 rename 发布,
    # 中断只留残骸在 .cloning, 主目录不会出现非空半成品
    ref_desc = f", ref={ref}" if ref else ""
    _log(f"[prepare] 执行机 clone 代码仓: {repo} (remote={remote}{ref_desc})")
    ref_opt = f" -b {shlex.quote(ref)}" if ref else ""
    # 清主目录 + mv 单独成段: clone 失败时 rm/mv 不执行, 主目录保持缺失/空
    cmd = (f"{proxy_prefix}git clone{ref_opt} {q_remote} {q_tmp} "
           f"&& rm -rf {q_repo} && mv {q_tmp} {q_repo}")
    if dry_run:
        print(f"[dry-run] {cmd}")
        return True
    rc = ssh_run(_ExecMachine(), cmd)
    if rc == 0:
        _log(f"[prepare] 执行机代码仓就绪: {repo}")
    else:
        _log(f"[prepare] 执行机代码仓 clone 失败 (rc={rc}), 详见上方输出; "
             f"或手动准备: git clone {remote} -b {ref or '<branch>'} {repo}")
    return rc == 0


def sync_repo_to_node(cfg, node, dry_run=False):
    """把执行机的 sglang 代码仓同步到远程节点: 先清理节点旧仓, 再整仓复制。

    各节点代码不再各自 git 操作 (节点无需外网/git), 统一以执行机为唯一来源,
    保证多节点用例的代码版本严格一致。执行机自身节点无需同步。
    """
    repo = cfg.run.repo
    where = f"{node.user}@{node.host}:{node.port}"
    _log(f"[prepare] {where} 清理旧代码仓: {repo}")
    if ssh_run(node, f"rm -rf {shlex.quote(repo)}", dry_run=dry_run) != 0:
        _log(f"[prepare] {where} 清理旧代码仓失败, 手动排查: "
             f"ssh {node.user}@{node.host} 'ls {repo}'")
        return False
    _log(f"[prepare] {where} 从执行机复制代码仓 -> {repo}")
    if ssh_push_dir(node, repo, repo, dry_run=dry_run) != 0:
        _log(f"[prepare] {where} 复制代码仓失败, 手动排查: "
             f"ssh {node.user}@{node.host} 'ls {repo}'")
        return False
    return True


def prepare_evalscope(cfg, master_hosts, dry_run=False):
    """准备 evalscope 源码 (固定 {workspace}/evalscope, 与 sglang 仓同约定)。

    与 sglang 仓同模式 (执行机唯一 git 点 + 同路径内网分发): 各节点不 clone
    (慢且需外网); 分发与 sync_repo_to_node 一致——先清理节点旧目录再整目录
    复制, 节点内容与执行机严格一致。自定义版本直接放执行机 {workspace}/evalscope
    (非空即跳过 clone, 同样被分发)。master_hosts 只含跑测试的节点 (单机=node,
    PD 分离=router, 混布 TP=第一个节点); worker/PD 节点只起 server 不装精度
    框架 (与 run.datasets 仅测试节点需要同理)。
    staging 幂等: 非空跳过 clone (跨 run 复用, rm -rf 后重跑可强制刷新)。
    clone 为原子式 (先 .cloning 后 rename), 中断不留半成品, 下次重跑自愈。
    clone 失败整体跳过分发 (离线执行机无外网时的预期降级; 节点无源码 →
    容器内软链不生效 → run_evalscope.sh 回退在线安装); 单节点清理/复制失败
    不阻断, 该节点同样回退在线安装。
    """
    if not master_hosts:
        return
    staging = f"{cfg.run.workspace}/evalscope"
    q_staging = shlex.quote(staging)
    # ---- 执行机 staging ----
    # 代理显式取 run.env (http_proxy 等): 该配置只注入容器, 执行机 shell 拿不到
    proxy_env = " ".join(
        f"{k}={shlex.quote(str(cfg.run.env[k]))}"
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
        if cfg.run.env.get(k))
    proxy_prefix = f"env {proxy_env} " if proxy_env else ""
    # 原子 clone 临时目录 (与主目录同父, mv 必为同分区 rename)
    q_tmp = shlex.quote(f"{staging}.cloning")
    # 原子 clone: 先落 .cloning, 成功后清主目录 (此时必为空/缺失) 再 rename
    # 发布——主目录只有 "缺失/空" 与 "完整" 两种状态, clone 中断只留残骸在
    # .cloning (下次无条件清理), 不会被非空检查误判为就绪
    lines = ["set -e",
             f"rm -rf {q_tmp}",
             f'if [ -d {q_staging} ] && [ -n "$(ls -A {q_staging})" ]; then',
             '  echo "evalscope staging 已存在, 跳过 clone"',
             "else",
             f"  {proxy_prefix}git clone --depth 1 {_EVALSCOPE_REPO} {q_tmp}",
             f"  rm -rf {q_staging}",
             f"  mv {q_tmp} {q_staging}",
             "fi"]
    _log(f"[prepare] 执行机准备 evalscope 源码 (staging): {staging}")
    if ssh_run(_ExecMachine(), "\n".join(lines), dry_run=dry_run) != 0:
        _log(f"[prepare] evalscope clone 失败, 跳过分发 (各节点回退在线安装); "
             f"手动命令: git clone --depth 1 {_EVALSCOPE_REPO} {staging}")
        return
    # ---- 分发到各测试节点: 清理旧目录 + 整目录复制 (与 sync_repo_to_node 一致) ----
    for host in master_hosts:
        node = cfg.find_node(host)
        # 本机节点: staging 即节点路径 (同为 {workspace}/evalscope), 天然就绪
        if _is_local(node):
            continue
        where = f"{node.user}@{node.host}:{node.port}"
        # workspace 与执行机共享时 (NFS/共享卷), rm+push 会删执行机的 staging;
        # 共享即同一物理目录, evalscope 源码天然可见, 跳过分发
        if _workspace_shared_with_exec(node, cfg.run.workspace, dry_run):
            _log(f"[prepare] {where} workspace 与执行机共享, 跳过 evalscope 分发 "
                 f"(源码已在执行机 staging, 共享目录天然可见)")
            continue
        _log(f"[prepare] {where} 清理旧 evalscope 源码: {staging}")
        if ssh_run(node, f"rm -rf {q_staging}", dry_run=dry_run) != 0:
            _log(f"[prepare] {where} 清理失败, 跳过分发, 该节点回退在线安装; "
                 f"手动检查: ssh {node.user}@{node.host} 'ls {staging}'")
            continue
        _log(f"[prepare] {where} 从执行机分发 evalscope 源码 -> {staging}")
        if ssh_push_dir(node, staging, staging, dry_run=dry_run) != 0:
            _log(f"[prepare] {where} evalscope 源码分发失败, 该节点回退在线安装; "
                 f"手动检查: ssh {node.user}@{node.host} 'ls {staging}'")


def prepare_node(cfg, node, dry_run=False):
    """执行前准备一个节点。

    代码仓统一在执行机准备 (prepare_local_repo) 并复制到各节点
    (sync_repo_to_node), 节点侧不做 git 操作 (节点无需外网):
      - 镜像: 不存在则尝试 docker pull; 失败不阻断 (节点可能离线,
        镜像应手动 docker pull 就位; 执行时若仍缺, docker run 自然报错);
      - 远程节点: 清理旧代码仓 + 从执行机整仓复制;
      - 执行机自身: 代码已在 prepare_local_repo 就位, 无需复制。
    """
    where = "本机" if _is_local(node) else f"{node.user}@{node.host}:{node.port}"
    # 镜像不存在则尝试 pull; 失败不阻断 (节点可能离线, 镜像应手动准备):
    # 用 if + || true 让 pull 失败也不影响整体 rc, 只打日志提示手动准备
    _log(f"[prepare] {where} 检查镜像 (不存在则尝试 pull): {cfg.run.docker.image}")
    image = shlex.quote(cfg.run.docker.image)
    lines = [
        f"if ! docker image inspect {image} >/dev/null 2>&1; then",
        f"  echo '[prepare] 镜像不存在, 尝试 pull: {cfg.run.docker.image}'",
        f"  if ! docker pull {image}; then",
        f"    echo '[prepare] 镜像 pull 失败, 不阻断; 如节点离线请手动准备: docker pull {cfg.run.docker.image}'",
        f"  fi",
        f"fi",
    ]
    rc = ssh_run(node, "\n".join(lines), dry_run=dry_run)
    # pull 失败不阻断 (rc 由 if/|| true 兜底为 0, 这里仅 ssh 自身失败才 != 0);
    # 但 ssh 失败意味着节点不可达, sync_repo_to_node 也会失败, 让它去报错
    if not _is_local(node):
        # workspace 与执行机共享时 (NFS/共享卷), rm+push 会删执行机自己的仓;
        # 共享即节点与执行机同一物理目录, 代码天然一致, 跳过 sync
        if _workspace_shared_with_exec(node, cfg.run.workspace, dry_run):
            _log(f"[prepare] {where} workspace 与执行机共享, 跳过代码同步 "
                 f"(sglang 仓已在执行机准备, 共享目录天然可见)")
        elif not sync_repo_to_node(cfg, node, dry_run):
            return False
    _log(f"[prepare] {where} 就绪 (代码来自执行机 {cfg.run.repo})")
    return True


def cleanup_node_sglang(cfg, node, dry_run=False):
    """清理节点上的 sglang 残留 (宿主机进程 + 本流水线容器), 释放 NPU 卡。

    prepare 阶段对每个就绪节点执行一次 (紧跟 prepare_node), 覆盖两类残留:
      - 宿主机 sglang 进程 (手动调试残留, 形态见下)
      - 本流水线容器 (name=sgl-pipeline-*): 上轮 timeout 只杀掉节点上的
        docker 客户端, 容器不会自停, 不清理则占卡 / --name 冲突
    容器只删自己创建的 sgl-pipeline-*; 同镜像的其他容器 (CI / 手动
    docker run 残留) 属他人创建, 不动 (占卡时按失败提示手动处理)。

    模式覆盖四种 cmdline 形态: python -m sglang.* (经典启动) /
    sglang serve (新版 CLI 启动) / sglang::* (scheduler 等 worker 被
    setproctitle 改名后的 cmdline, 占 NPU 卡的正是它们) / sglang_router。
    模式中的 [.:] / [ ] / [_-] 防止 pgrep/pkill 匹配到清理命令自身 (其
    cmdline 含模式原文), 也不会误杀 run.py (路径是 sglang_local_pipeline)。
    """
    pattern = "'sglang[.:]|sglang[ ]serve|sglang[_-]router'"
    lines = [
        # 先列后杀, 输出确认清掉了什么; pgrep 无匹配 (rc=1) 时打印 (无)
        "echo '[cleanup] 宿主机 sglang 进程:'",
        f"pgrep -af {pattern} || echo '  (无)'",
        f"pkill -9 -f {pattern} 2>/dev/null || true",
        "echo '[cleanup] 残留容器 (sgl-pipeline-*):'",
        "docker ps -a --filter name=sgl-pipeline- --format '  {.Names} ({.Status})'",
        # xargs -r: 无输入时不执行 (避免空参数报错); rm 失败 stderr 可见, 不阻断
        "docker ps -aq --filter name=sgl-pipeline- "
        "| xargs -r docker rm -f >/dev/null || true",
    ]
    where = "本机" if _is_local(node) else f"{node.user}@{node.host}:{node.port}"
    _log(f"[cleanup] {where} 清理 sglang 残留 (宿主机进程 + 容器)")
    rc = ssh_run(node, "\n".join(lines), dry_run=dry_run)
    if not dry_run and rc != 0:
        _log(f"[cleanup] {where} 清理失败 (rc={rc}), 不阻断执行; "
             f"如用例报 NPU 卡被占用, 手动检查: pgrep -af sglang; docker ps")
    return rc


def _build_cmd(cfg, suite, node, node_run_dir, role=None, extra_env=None,
               timeout_minutes=None, tp_halving=False):
    """构造节点上执行的完整命令。

    role: 多机用例的角色名 (prefill/decode/router), 决定容器名后缀;
    extra_env: 角色专属环境变量 (多机用例的 HOSTNAME/POD_IP/协调地址等);
    timeout_minutes: 覆盖 suite.timeout_minutes (多机用例 PD 角色加余量用);
        suite/参数均为空时兜底 _DEFAULT_TIMEOUT_MINUTES, 杜绝无超时 hang 死;
    tp_halving: A5 单机用例, 经 /output/run_case.py 包装启动 (--tp-size 减半)。
    构造完成后打印 [cmd] 关键路径诊断 (容器名/节点/repo/输出目录/超时)。
    """
    repo = cfg.run.repo
    d = cfg.run.docker
    container = f"sgl-pipeline-{suite.name}" + (f"-{role}" if role else "")

    # 容器内命令
    parts = ["set -euo pipefail"]
    # 自定义算子包环境 (DeepSeek-V4-Flash 等用例需要; 镜像未装时忽略, 与 CI 一致)。
    # vendor 脚本可能引用未定义变量 (如 ZSH_VERSION), source 前临时关 -u
    parts += [
        "set +u",
        "source /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/"
        "customize/bin/set_env.bash || true",
        "source /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/"
        "custom_transformer/bin/set_env.bash || true",
        "set -u",
    ]
    q_repo = shlex.quote(repo)
    # 覆盖镜像内 ascend 工具 (学 CI nightly 做法)
    # 注意: 对完整路径做 shlex.quote, 不能只引用前缀 (否则 /python/... 落在引号外,
    # 路径含空格时会被 shell 拆断); glob * 留在引号外以便 shell 展开
    ascend_src = shlex.quote(f"{repo}/python/sglang/test/ascend")
    parts.append(
        f"cp -r {ascend_src}/* "
        f"$(python3 -c 'import sglang, os; print(os.path.dirname(sglang.__file__))')/test/ascend/"
    )
    # 精度框架 (run_evalscope) 硬编码 /root/sglang 调用 run_evalscope.sh, 与 CI
    # K8s 把代码卷挂在 /root/sglang 对齐; 本地 repo 在 {workspace}/sglang, 建软链
    # 让硬编码路径落到实际 repo (ln -sfn 覆盖既有目录/链接, 可重复执行)
    parts.append(f"ln -sfn {q_repo} /root/sglang")
    # evalscope 本地源 (固定 {workspace}/evalscope, 已同路径挂载见下方 mount_args):
    # run_evalscope.sh 硬编码检查 /root/.cache/.cache/evalscope, 软链过去实现本地
    # pip install -e。条件软链 (非空才链): 节点缺源码时 docker 对挂载源建空目录,
    # 无条件软链会让 [ -d ] 误命中 → pip install -e 空目录报错; 不链则回退在线安装
    q_ev = shlex.quote(f"{cfg.run.workspace}/evalscope")
    parts.append(
        f'if [ -d {q_ev} ] && [ -n "$(ls -A {q_ev})" ]; then '
        f"mkdir -p /root/.cache/.cache && "
        f"ln -sfn {q_ev} /root/.cache/.cache/evalscope; fi")
    # 预置数据集到 /tmp (与 CI 一致): /tmp 每次全新挂载, 不预置则会重复下载
    # (gsm8k) 或 perf 套件找不到数据; datasets 为节点本地绝对路径, 所在目录
    # 已自动挂载进容器 (见下方 mount_args); 未配置时跳过, 由用例在线下载或
    # 自行读取; cp -r 兼容文件/目录, 缓存缺失时忽略, 回退在线下载
    for f in cfg.run.datasets:
        parts.append(f"cp -r {shlex.quote(f)} /tmp/ 2>/dev/null || true")
    if tp_halving:
        # A5 适配: 经包装器启动, 自动把 other_args 里的 --tp-size 减半
        cmd = (f"cd {q_repo} && python3 -u /output/run_case.py "
               f"{shlex.quote(suite.file)} -f")
    else:
        cmd = f"cd {q_repo} && python3 -u {shlex.quote(suite.file)} -f"
    # 不用 "| tee": 用例泄漏的子进程 (如 PD 分离 router 的 sglang_router) 会继承
    # 管道写端, python 退出后 tee 等 EOF 永不退出 → 容器挂死。改为后台执行 +
    # 重定向落盘 + tail --pid 跟踪屏显: python 退出 → wait 返回 → 脚本结束,
    # 容器随 PID 1 退出被 Docker 整体回收 (对齐 CI: 主进程退出即杀 cgroup 全部
    # 进程, 泄漏的 router 子进程一并清理); 退出码经 wait 透传 (与 pipefail 下
    # tee 等价)
    log_f = shlex.quote(f"/output/{_log_filename(suite)}")
    parts.append(f"{cmd} > {log_f} 2>&1 & _case_pid=$!")
    parts.append(f"tail -f {log_f} --pid=$_case_pid")
    parts.append("wait $_case_pid")

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
        # evalscope 源码 (与 repo 同模式: 同路径挂载; 节点缺目录时 docker 建空
        # 目录, 由上方条件软链兜底回退在线安装)
        "-v", shlex.quote(f"{cfg.run.workspace}/evalscope:"
                          f"{cfg.run.workspace}/evalscope"),
        "-v", shlex.quote(f"{node_run_dir}:/output"),
        "-v", shlex.quote(f"{node_run_dir}/tmp:/tmp"),
        "-v", shlex.quote(f"{node_run_dir}/plog:/root/ascend/log"),
    ] + [x for m in _NODE_MOUNTS for x in ("-v", m)]
    # datasets 所在目录自动挂载 (同路径映射, 去重), 容器内路径与配置一致,
    # cp 命令直接使用原路径; 与默认挂载重叠时 (如 ~/.cache 下) docker 后挂载
    # 遮蔽前者, 同一宿主机目录内容一致, 无害
    _auto_dirs = [f.rsplit("/", 1)[0] for f in cfg.run.datasets]
    for m in dict.fromkeys(_auto_dirs):
        mount_args += ["-v", shlex.quote(f"{m}:{m}")]
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
    if not tmo:
        # 未配 timeout_minutes 时兜底, 防止容器内进程 hang 导致整个 run 卡死
        tmo = _DEFAULT_TIMEOUT_MINUTES
        _log(f"[cmd] {role or '单机用例'} @{node.host} 未配 timeout, "
             f"使用默认 {tmo} 分钟")
    # -k: SIGTERM 后 60s 仍未退出则 SIGKILL, 防止容器内进程卡死挂住整个 run
    lines.append(f"timeout -k 60 {int(tmo) * 60} " + " ".join(docker_args))
    # [cmd] 每容器一行: 角色@节点+超时 (容器名/输出目录按固定规则可推导,
    # 完整命令看 dry-run 或各角色 ssh.log; 多机并发时靠 host 区分归属)
    _log(f"[cmd] {role or '单机用例'} @{node.host} 超时={tmo}分钟")
    return "\n".join(lines)


def _fetch_artifacts(node, node_run_dir, local_dir, label, runs_root):
    """拉回一个执行单元的产物 (用例名.log + ssh.log + plog/, 排除项见
    _FETCH_EXCLUDES)。

    本机节点: 拷贝成功后清理 runs/ 侧副本省磁盘 (results/ 已有一份), 并逐级
    清掉因此变空的上层目录直到 runs/ 根 (含; 之上的 workspace 不动);
    远程节点: runs/ 是节点侧唯一原始数据, 始终保留。
    """
    frc = ssh_fetch_dir(node, node_run_dir, local_dir)
    # label 为空时 (单机用例, 段落上下文已明确) 不占位
    who = f"{label} " if label else ""
    if frc != 0:
        # 失败保留完整路径便于手动重拉
        _log(f"[fetch] {who}拉回失败 (rc={frc}), 保留节点侧原件: {node_run_dir}")
        return
    note = "日志 + plog"
    if _is_local(node):
        try:
            shutil.rmtree(node_run_dir)
        except OSError as e:
            _log(f"[fetch] {who}拉回完成 ({note}); 清理本机 runs/ 副本失败 "
                 f"({e}), 保留: {node_run_dir}")
            return
        note += ", 本机 runs/ 副本已清理"
        # 逐级向上清掉变空的目录直到 runs/ 根 (含); rmdir 只删空目录, 其他用例/
        # 角色还在用时自然失败即止 (下级非空时上级必非空, 无需再往上试)
        root = runs_root.rstrip("/")
        d = os.path.dirname(node_run_dir)
        while d == root or d.startswith(root + "/"):
            try:
                os.rmdir(d)
            except OSError:
                break
            if d == root:
                break
            d = os.path.dirname(d)
    _log(f"[fetch] {who}拉回完成 ({note})")


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
            _log(f"[execute] A5 适配: 注入 --tp-size 减半包装器 "
                 f"({node_run_dir}/run_case.py)")
            if _stage_run_wrapper(node, node_run_dir, dry_run) != 0:
                raise RuntimeError("写入 run_case.py 失败")
        # 注入 sitecustomize.py (容器内 /output, 经 PYTHONPATH 生效): evalscope
        # venv 的 requests SSL 校验关闭; fake k8s 桥接仅在 SGLANG_COORD_URL
        # 设置 (多机用例) 时激活, 单机用例为空操作
        if _stage_sitecustomize(node, node_run_dir, dry_run) != 0:
            raise RuntimeError("写入 sitecustomize.py 失败")
        # A5 单机用例专属环境变量 (run.a5_env, 如灵衢 FIA 互联 ASCEND_USE_FIA);
        # 未配置时为空, 不注入; 多机用例不注入
        extra_env = dict(cfg.run.a5_env) if node.arch == "a5" else {}
        # /output/sitecustomize.py 的加载路径 (与多机用例的注入方式一致)
        extra_env["PYTHONPATH"] = "/output"
        cmd = _build_cmd(cfg, suite, node, node_run_dir, tp_halving=tp_halving,
                         extra_env=extra_env)
        _log(f"[execute] @ {where} 启动容器 (超时={suite.timeout_minutes or _DEFAULT_TIMEOUT_MINUTES}分钟)")
        rc = ssh_run(node, cmd,
                     log_path=os.path.join(local_suite_dir, _log_filename(suite)),
                     diag_path=os.path.join(local_suite_dir, "ssh.log"),
                     dry_run=dry_run)
        result["status"] = "pass" if rc == 0 else "fail"
        result["error"] = None if rc == 0 else f"exit code {rc}"
        dur = round(time.perf_counter() - tic, 1)
        if rc == 0:
            _log(f"[execute] 容器正常结束 (rc=0, 耗时 {dur}s)")
        elif dry_run:
            _log(f"[execute] dry-run 完成命令打印")
        else:
            # 失败保留完整日志路径便于排查
            _log(f"[execute] 容器异常结束 (rc={rc}, 耗时 {dur}s), "
                 f"日志: {os.path.join(local_suite_dir, _log_filename(suite))}")
        if not dry_run:
            _fetch_artifacts(node, node_run_dir, local_suite_dir, "",
                             f"{cfg.run.workspace}/runs")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] 流水线异常: {type(e).__name__}: {e}")
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
      4. 判定对齐 CI (CI 只监控 router pod 日志的 OK/FAILED, 判定后删 job):
         router rc==0 即通过; PD 节点 rc==0 (收到信号退出) 或被强杀的 137
         不判失败, 其余非 0 退出码视为 PD 崩溃 (等价 CI pod 非 Running 判失败)

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
        _log(f"[execute] 多机用例 @ {where_desc} "
             f"(超时={suite.timeout_minutes or _DEFAULT_TIMEOUT_MINUTES}分钟/每节点)")
        _log(f"[execute] 协调服务: {coord_url} (已预置 {len(coord_data)} 个 pod 注册)")

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
                # /output/sitecustomize.py: fake kubernetes 桥接 + evalscope venv
                # 的 requests SSL 补丁 (与单机用例注入内容一致)
                "PYTHONPATH": "/output",
            }
            unit_tic = time.perf_counter()
            log_path = os.path.join(local_run_dir, suite.name, key, _log_filename(suite))
            diag_path = os.path.join(local_run_dir, suite.name, key, "ssh.log")
            try:
                if _stage_sitecustomize(node, node_run_dir, dry_run,
                                        prefix=f"[{key}] ",
                                        quiet=(role != "router"),
                                        diag_path=diag_path) != 0:
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
                # PD 角色日志量大且与 router 交错, 只写文件不回显 (对齐 CI 各 pod
                # 日志隔离的观感, 控制台只看 router 的测试输出)
                rc[key] = ssh_run(node, cmd, log_path=log_path, diag_path=diag_path,
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
            _log(f"[execute] 先启动 router, 等待写入 active-test-class ...")
            router_seq_tic = time.perf_counter()
            while True:
                if _ACTIVE_TEST_CLASS_KEY in coord.get_data():
                    _log(f"[execute] active-test-class 已写入, 启动 PD 节点")
                    break
                if not threads[router_key].is_alive():
                    _log(f"[execute] router 提前退出 (未写入 active-test-class), "
                         "直接启动 PD 节点")
                    break
                if time.perf_counter() - router_seq_tic > 120:
                    _log(f"[execute] 等待 active-test-class 超时 (120s), "
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
                # 测试结果应以 router 的用例日志为准
                cause = ("router 退出" if not threads[router_key].is_alive()
                         else "PD 节点提前退出")
                _log(f"[execute] 结束信号已写入协调服务 ({cause}), "
                     "等待 prefill/decode 节点退出")
            # grace period 超时后, 仍有容器未退出 → 强杀 (等价 CI 外层 runner
            # 检测 pod 非 Running 后删 job; 常见于 PD 崩溃后 router 卡在端口等待)
            if (released and not killed and not dry_run
                    and time.perf_counter() - released_at > _GRACE_PERIOD_KILL_SEC):
                for (r, i, n), k in zip(units, keys):
                    if threads[k].is_alive():
                        container = f"sgl-pipeline-{suite.name}-{k}"
                        _log(f"[execute] {k} 超过 {_GRACE_PERIOD_KILL_SEC}s "
                             f"未退出, 强制删除容器 {container}")
                        _kill_container(n, container)
                killed = True
            time.sleep(5)
        for t in threads.values():
            t.join()

        for key in keys:
            dur = durs.get(key, "?")
            if errs.get(key):
                _log(f"[execute] {key} 异常 (耗时 {dur}s): {errs[key]}")
            elif rc.get(key) == 0:
                _log(f"[execute] {key} 正常结束 (rc=0, 耗时 {dur}s)")
            else:
                # PD/worker 角色不回显控制台, 失败时指明日志位置便于排查
                _log(f"[execute] {key} 异常结束 (rc={rc.get(key)}, 耗时 {dur}s), "
                     f"日志: {os.path.join(local_run_dir, suite.name, key, _log_filename(suite))}")

        # 判定对齐 CI (run_npu_e2e_test.py 只监控 router pod 日志的 OK/FAILED,
        # 判定后直接删 job, PD pod 是被连带杀掉的): 以 router 的 rc 为准;
        # PD 节点正常收到结束信号以 0 退出, 超过宽限期被强杀的 137 (轮询周期
        # 较长等) 不判失败; 其余非 0 退出码视为 PD 崩溃, 判失败。
        # rc.get(k) 为 None 时 (线程在赋值前异常退出, 如 KeyboardInterrupt) 归为
        # error 而非误导性的 "exit code None"
        failed = {}
        if errs.get(router_key) or rc.get(router_key) != 0:
            failed[router_key] = (errs.get(router_key)
                                  or (f"exit code {rc.get(router_key)}"
                                      if rc.get(router_key) is not None
                                      else "未执行 (线程异常退出)"))
        for k in pd_keys:
            if errs.get(k):
                failed[k] = errs[k]
            elif rc.get(k) not in (0, 137):
                failed[k] = (f"exit code {rc.get(k)}"
                             if rc.get(k) is not None
                             else "未执行 (线程异常退出)")
        if not failed:
            result["status"] = "pass"
            tolerated = [k for k in pd_keys
                         if not errs.get(k) and rc.get(k) == 137]
            if tolerated:
                _log(f"[execute] {', '.join(tolerated)} 被强杀 (rc=137) "
                     "不判失败: router 已成功 (对齐 CI)")
        else:
            result["error"] = "; ".join(f"{k}: {v}" for k, v in failed.items())

        if not dry_run:
            for (role, idx, node), key in zip(units, keys):
                node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}/{key}"
                local_unit_dir = os.path.join(local_run_dir, suite.name, key)
                _fetch_artifacts(node, node_run_dir, local_unit_dir, key,
                                 f"{cfg.run.workspace}/runs")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] 流水线异常: {type(e).__name__}: {e}")
    finally:
        coord.stop()
    result["duration_sec"] = round(time.perf_counter() - tic, 1)
    return result


def execute_multinode_tp_suite(cfg, suite, run_id, local_run_dir, dry_run=False):
    """执行一个多机混布 TP 用例, 返回结果 dict。

    与 PD 分离的区别:
      - 无 prefill/decode/router 角色, 所有节点组成一个 TP 实例
      - HOSTNAME = sglang-node-{idx} (用例框架据此区分 master/worker)
      - master (node-0) 启动 sglang server 并执行测试; worker 只起 server,
        随后 sleep MAX_SERVER_KEEP_ALIVE_TIME(3600s) 保活, 不轮询结束信号,
        master 结束后只能由流水线强制清理 (rc=137)
      - 判定对齐 CI: master rc==0 即通过, worker 被强杀的 137 不判失败

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
        _log(f"[execute] 多机混布 @ {where_desc} "
             f"(超时={suite.timeout_minutes or _DEFAULT_TIMEOUT_MINUTES}分钟/每节点)")
        _log(f"[execute] 协调服务: {coord_url} (已预置 {len(coord_data)} 个 pod 注册)")

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
            log_path = os.path.join(local_run_dir, suite.name, key, _log_filename(suite))
            diag_path = os.path.join(local_run_dir, suite.name, key, "ssh.log")
            try:
                if _stage_sitecustomize(node, node_run_dir, dry_run,
                                        prefix=f"[{key}] ",
                                        quiet=(key != master_key),
                                        diag_path=diag_path) != 0:
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
                # worker 角色只写文件不回显, 控制台只保留 master 的测试输出
                rc[key] = ssh_run(node, cmd, log_path=log_path, diag_path=diag_path,
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
                # 测试结果应以 master 的用例日志为准
                cause = ("master 退出" if not threads[master_key].is_alive()
                         else "worker 节点提前退出")
                _log(f"[execute] 结束信号已写入协调服务 ({cause}), "
                     "等待 worker 节点退出")
            # grace period 超时后, 仍有容器未退出 → 强杀
            if (released and not killed and not dry_run
                    and time.perf_counter() - released_at > _GRACE_PERIOD_KILL_SEC):
                for (i, n), k in zip(units, keys):
                    if threads[k].is_alive():
                        container = f"sgl-pipeline-{suite.name}-{k}"
                        _log(f"[execute] {k} 超过 {_GRACE_PERIOD_KILL_SEC}s "
                             f"未退出, 强制删除容器 {container}")
                        _kill_container(n, container)
                killed = True
            time.sleep(5)
        for t in threads.values():
            t.join()

        for key in keys:
            dur = durs.get(key, "?")
            if errs.get(key):
                _log(f"[execute] {key} 异常 (耗时 {dur}s): {errs[key]}")
            elif rc.get(key) == 0:
                _log(f"[execute] {key} 正常结束 (rc=0, 耗时 {dur}s)")
            else:
                # PD/worker 角色不回显控制台, 失败时指明日志位置便于排查
                _log(f"[execute] {key} 异常结束 (rc={rc.get(key)}, 耗时 {dur}s), "
                     f"日志: {os.path.join(local_run_dir, suite.name, key, _log_filename(suite))}")
        # 判定对齐 CI (run_npu_e2e_test.py 只监控 master pod 日志的 OK/FAILED,
        # 判定后直接删 job, worker 是被连带杀掉的): master rc==0 即通过;
        # worker 只起 server 且 sleep 保活不轮询结束信号, 被强杀的 137 属
        # 预期收尾不判失败; 其余非 0 退出码 (worker 崩溃 / master 失败被杀) 判失败
        # rc.get(k) 为 None 时 (线程在赋值前异常退出, 如 KeyboardInterrupt) 归为
        # error 而非误导性的 "exit code None"
        failed = {}
        if errs.get(master_key) or rc.get(master_key) != 0:
            failed[master_key] = (errs.get(master_key)
                                  or (f"exit code {rc.get(master_key)}"
                                      if rc.get(master_key) is not None
                                      else "未执行 (线程异常退出)"))
        for k in worker_keys:
            if errs.get(k):
                failed[k] = errs[k]
            elif rc.get(k) not in (0, 137):
                failed[k] = (f"exit code {rc.get(k)}"
                             if rc.get(k) is not None
                             else "未执行 (线程异常退出)")
        if not failed:
            result["status"] = "pass"
            if any(not errs.get(k) and rc.get(k) == 137 for k in worker_keys):
                _log("[execute] worker 被强杀 (rc=137) 不判失败: "
                     "master 已成功 (对齐 CI)")
        else:
            result["error"] = "; ".join(f"{k}: {v}" for k, v in failed.items())

        if not dry_run:
            for (idx, node), key in zip(units, keys):
                node_run_dir = f"{cfg.run.workspace}/runs/{run_id}/{suite.name}/{key}"
                local_unit_dir = os.path.join(local_run_dir, suite.name, key)
                _fetch_artifacts(node, node_run_dir, local_unit_dir, key,
                                 f"{cfg.run.workspace}/runs")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] 流水线异常: {type(e).__name__}: {e}")
    finally:
        coord.stop()
    result["duration_sec"] = round(time.perf_counter() - tic, 1)
    return result
