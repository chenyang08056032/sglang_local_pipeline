# -*- coding: utf-8 -*-
"""节点执行器 (自动检测本地/远程)。

执行链: (本地直执 或 SSH) → git checkout → docker run → 容器内跑用例文件 → 拉回日志
"""

import os
import shlex
import shutil
import socket
import subprocess
import time

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


def _local_ips():
    """收集本机所有 IP, 用于判断节点是否就是执行机本身。

    首选 hostname -I: 直接枚举所有网卡 IP, 不受 /etc/hosts 把主机名
    映射到 127.0.1.1 的影响; 拿不到时兜底解析主机名。
    """
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
    return ips


def _is_local(node):
    """节点 host 是否指向本机。"""
    return node.host in _local_ips()


def ssh_run(node, command, log_path=None, dry_run=False):
    """执行命令: 本机节点直接 subprocess, 远程走 SSH。实时回显 + 写日志。"""
    local = _is_local(node)
    if dry_run:
        tag = "local" if local else f"{node.user}@{node.host}:{node.port}"
        print(f"[dry-run] {tag}$ {command}")
        return 0
    if local:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="ignore", bufsize=1,
        )
    else:
        proc = subprocess.Popen(
            ["ssh", "-p", str(node.port), f"{node.user}@{node.host}", command],
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
            print(line, end="")
            if log_f:
                log_f.write(line)
                log_f.flush()
    finally:
        if log_f:
            log_f.close()
        proc.wait()
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
        ["ssh", "-p", str(node.port), f"{node.user}@{node.host}",
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
    """带时间戳的流水线日志。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def _build_cmd(cfg, suite, node, node_run_dir):
    """构造节点上执行的完整命令。"""
    repo = cfg.run.repo
    d = cfg.run.docker

    # 容器内命令
    parts = ["set -euo pipefail"]
    # 覆盖镜像内 ascend 工具 (学 CI nightly 做法)
    q_repo = shlex.quote(repo)
    parts.append(
        f"cp -r {q_repo}/python/sglang/test/ascend/* "
        f"$(python3 -c 'import sglang, os; print(os.path.dirname(sglang.__file__))')/test/ascend/"
    )
    # 预置 gsm8k / ShareGPT 数据集 (与 CI 一致): /tmp 每次全新挂载, 不预置则会
    # 重复下载 (gsm8k) 或 perf 套件找不到数据; 缓存缺失时忽略, 走在线下载
    for f in ("tmp/test.jsonl",
              "otavia/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json"):
        parts.append(f"cp '/root/.cache/modelscope/hub/datasets/{f}' /tmp/ 2>/dev/null || true")
    cmd = f"cd {q_repo} && python3 -u {shlex.quote(suite.file)} -f"
    parts.append(f"{cmd} 2>&1 | tee /output/case.log")

    inner = "\n".join(parts)

    # docker run 参数
    ids = range(node.npus) if d.devices == "auto" else d.devices
    device_args = []
    for i in ids:
        device_args += ["--device", f"/dev/davinci{i}"]
    for dev in _MGMT_DEVICES:
        device_args += ["--device", dev]

    mount_args = [
        "-v", shlex.quote(f"{repo}:{repo}"),
        "-v", shlex.quote(f"{node_run_dir}:/output"),
        "-v", shlex.quote(f"{node_run_dir}/tmp:/tmp"),
        "-v", shlex.quote(f"{node_run_dir}/plog:/root/ascend/log"),
    ] + [x for m in _NODE_MOUNTS for x in ("-v", m)]

    # shlex.quote: 值含空格等 shell 特殊字符时自动加引号, 保证 " ".join 后不被拆断
    env_args = [v for k, val in cfg.run.env.items()
                for v in ("-e", f"{k}={shlex.quote(str(val))}")]

    inner_escaped = inner.replace("'", "'\"'\"'")
    docker_args = (["docker", "run", "--rm", "--privileged",
                    "--net", d.net, "--ipc", "host",
                    "--shm-size", d.shm_size,
                    "--name", f"sgl-pipeline-{suite.name}"]
                   + device_args + mount_args + env_args
                   + [shlex.quote(d.image), "bash", "-c", f"'{inner_escaped}'"])

    # 宿主机命令 (镜像/代码/版本由 prepare_node 提前保证)
    lines = [
        "set -e",
        f"mkdir -p {shlex.quote(node_run_dir)}/tmp {shlex.quote(node_run_dir)}/plog",
        # 清理残留同名容器: 上次 timeout 杀掉 docker 客户端后容器不会自停,
        # 不清理则本次 docker run 因 --name 冲突直接失败
        f"docker rm -f sgl-pipeline-{suite.name} >/dev/null 2>&1 || true",
    ]
    if suite.timeout_minutes:
        # -k: SIGTERM 后 60s 仍未退出则 SIGKILL, 防止容器内进程卡死挂住整个 run
        lines.append(f"timeout -k 60 {int(suite.timeout_minutes) * 60} " + " ".join(docker_args))
    else:
        lines.append(" ".join(docker_args))
    return "\n".join(lines)


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
        cmd = _build_cmd(cfg, suite, node, node_run_dir)
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
            frc = ssh_fetch_dir(node, node_run_dir, local_suite_dir)
            if frc == 0:
                _log(f"[fetch] {suite.name} 拉回完成 (case.log + plog/, 未回传 tmp/)")
                if _is_local(node):
                    # 本机节点: 产物已在 results/ 有一份, 清理 runs/ 侧副本省磁盘;
                    # 远程节点的 runs/ 是节点侧唯一原始数据, 不删
                    try:
                        shutil.rmtree(node_run_dir)
                        _log(f"[fetch] {suite.name} 已清理本机节点侧目录: {node_run_dir}")
                    except OSError as e:
                        _log(f"[fetch] {suite.name} 清理失败 ({e}), 保留: {node_run_dir}")
                    # run_id 目录空了则顺手清掉 (其他用例还在用时 rmdir 自然失败)
                    for d in (os.path.dirname(node_run_dir),
                              os.path.dirname(os.path.dirname(node_run_dir))):
                        try:
                            os.rmdir(d)
                        except OSError:
                            pass
            else:
                _log(f"[fetch] {suite.name} 拉回失败 (rc={frc}), 保留节点侧原件: {node_run_dir}")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        _log(f"[execute] {suite.name} 流水线异常: {type(e).__name__}: {e}")
    result["duration_sec"] = round(time.perf_counter() - tic, 1)
    return result
