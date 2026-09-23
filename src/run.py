#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sglang 本地测试流水线 (最小可用版)。

用法:
    python3 src/run.py --config configs/example_single.yaml   # 单机 / example_pd.yaml (PD 分离) / example_tp.yaml (混布 TP)
    python3 src/run.py --config configs/example_single.yaml --suite qwen3-32b-gsm8k
    python3 src/run.py --config configs/example_single.yaml --dry-run
    python3 src/run.py --config configs/example_single.yaml --at "2026-09-17 18:00:00"
    python3 src/run.py --config configs/example_single.yaml --at "18:00:00"   # 今天已过则取明天
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

import yaml

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
        sys.stderr.reconfigure(encoding="utf-8", errors="ignore")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (_ARCH_NPUS, _MULTI_ROLES, _log, SingleNodeContainers,
                      cleanup_node_sglang, execute_multinode_suite,
                      execute_multinode_tp_suite, execute_suite,
                      prepare_evalscope, prepare_local_repo, prepare_node)


# A3 NPU 环境的标准环境变量, 注入每个测试容器
# YAML 的 run.env 可覆盖这些默认值或追加新键 (按 key 合并, 不必全量重写)
_DEFAULT_ENV = {
    "SGLANG_USE_MODELSCOPE": "true",
    "HF_ENDPOINT": "https://hf-mirror.com",
    "SGLANG_IS_IN_CI": "true",
    # 与 CI 一致: 关闭 CustomTestCase._callTestMethod 的方法级外层重试
    # (性能基准重跑无意义且耗时; 内层 @retry() 与精度用例数据集重试不受影响)
    "SGLANG_TEST_MAX_RETRY": "0",
    "TORCH_EXTENSIONS_DIR": "/tmp/torch_extensions",  # 与 CI 一致, 编译缓存落在挂载的 /tmp
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "STREAMS_PER_DEVICE": "32",
}


@dataclass
class NodeConfig:
    host: str
    # 节点架构 (a3/a5), 必填: 决定挂卡数量 (a3=16, a5=8, 见 pipeline._ARCH_NPUS);
    # a5: 该节点上的单机用例 --tp-size 自动减半
    arch: str
    user: str = "root"
    port: int = 22


@dataclass
class DockerConfig:
    image: str
    devices: Union[str, List[int]] = "auto"  # "auto"=按节点 arch 推导卡数生成 davinci0..N-1
    net: str = "host"
    shm_size: str = "16g"
    # 额外挂载 (追加到默认 _NODE_MOUNTS 之后), 格式同 docker -v: "host:container[:ro]"
    extra_mounts: List[str] = field(default_factory=list)


@dataclass
class RunConfig:
    workspace: str
    # 执行机代码仓准备 (无 prepare 开关, 由 {workspace}/sglang 是否存在决定):
    #   - 已存在: 直接使用现状, 不做任何 git 操作 (用户对版本有完全控制)
    #   - 不存在: 用 run.env 配置的代理执行 git clone (只 clone, 配了 ref 用 -b)
    #     ; 失败报错 (无 git_remote 时也报错, 提示手动准备)
    git_remote: str = None
    # clone 时若指定则用 git clone -b {ref}; 未配 (None) 时用远端默认分支
    ref: str = None
    docker: DockerConfig = None
    env: Dict[str, str] = field(default_factory=dict)
    # 仅注入 a5 节点单机用例容器的环境变量 (如 ASCEND_USE_FIA);
    # 多机用例及其他 arch 节点不注入, 覆盖 env 同名键
    a5_env: Dict[str, str] = field(default_factory=dict)
    # 预置到容器 /tmp/ 的数据集路径列表 (节点本地绝对路径, 以 / 开头);
    # 所在目录自动挂载进容器 (同路径映射)。未配置时不预置任何数据集,
    # 由用例在线下载或自行读取
    datasets: List[str] = field(default_factory=list)
    # 单机共享容器启动时执行的依赖安装命令 (读自流水线 configs/pip_deps.txt,
    # 固定路径零配置: 文件存在即生效, 不存在跳过; # 注释/空行剔除后逐条嵌入
    # 容器初始化脚本, 如 CI Install dependencies 步骤的 pip install ...)
    pip_cmds: List[str] = field(default_factory=list)

    @property
    def repo(self):
        """sglang 源码路径, 固定放在 workspace 下 (执行机准备, 各节点同路径复制)。"""
        return f"{self.workspace}/sglang"


@dataclass
class SuiteConfig:
    name: str
    node: str = None
    file: str = None
    timeout_minutes: int = None
    # 多机 (PD 分离) 用例: 角色到节点 host 列表的映射, 与 node/multinode 互斥
    # prefill/decode 支持多节点 (如 2p2d); router 只能 1 个 (sglang 框架限制)
    roles: Dict[str, List[str]] = None
    # 多机 (混布 TP) 用例: 节点 host 列表, 与 node/roles 互斥
    # 第一个节点 = master (sglang-node-0), 其余 = worker; 无 router 角色
    multinode: List[str] = None


@dataclass
class PipelineConfig:
    run: RunConfig
    nodes: List[NodeConfig]
    suites: List[SuiteConfig]

    @property
    def output_dir(self):
        """执行机上的结果目录, 固定 {workspace}/results (与节点 runs/ 平级, 同机不冲突)。"""
        return f"{self.run.workspace}/results"

    def find_node(self, host):
        for n in self.nodes:
            if n.host == host:
                return n
        return None


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    run_raw = raw.get("run") or {}
    docker_raw = run_raw.get("docker") or {}
    # list(字符串) 会逐字符拆分, 单条挂载误写成字符串时报清晰错误而非 -v 单字符灾难
    extra_mounts = docker_raw.get("extra_mounts") or []
    if not isinstance(extra_mounts, list):
        raise ValueError(
            f"run.docker.extra_mounts 须为列表 (格式同 docker -v), 如:\n"
            f"  extra_mounts:\n    - \"/data/models:/models\"\n"
            f"实际: {extra_mounts!r}")
    # 显式空值 (devices: 等 null) 时 .get 的 default 不生效, 需 is None 判断兜底;
    # 不能用 or: devices 显式空列表 [] 是合法配置 (不挂卡)
    devices_raw = docker_raw.get("devices")
    docker = DockerConfig(
        image=docker_raw["image"],
        devices="auto" if devices_raw is None else devices_raw,
        net=docker_raw.get("net") or "host",
        shm_size=docker_raw.get("shm_size") or "16g",
        extra_mounts=list(extra_mounts),
    )
    # env 须为键值映射 (与 a5_env 同规则): 误写成列表/标量时报清晰错误,
    # 而非下方 .items() 的裸 AttributeError
    env_raw = run_raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ValueError(
            f"run.env 须为键值映射, 如:\n"
            f"  env:\n    http_proxy: \"http://proxy:port\"\n"
            f"实际: {env_raw!r}")
    # a5_env 须为键值映射 (键值自动转字符串, YAML 写 1 即 "1");
    # 误写成列表/标量时报清晰错误
    a5_env_raw = run_raw.get("a5_env") or {}
    if not isinstance(a5_env_raw, dict):
        raise ValueError(
            f"run.a5_env 须为键值映射, 如:\n"
            f"  a5_env:\n    ASCEND_USE_FIA: \"1\"\n"
            f"实际: {a5_env_raw!r}")
    # datasets 须为绝对路径字符串列表 (节点本地路径, 所在目录自动挂载进容器);
    # 误写成标量/字典/相对路径时报清晰错误
    datasets_raw = run_raw.get("datasets") or []
    if not isinstance(datasets_raw, list):
        raise ValueError(
            f"run.datasets 须为字符串列表, 如:\n"
            f"  datasets:\n    - /data/datasets/test.jsonl\n"
            f"实际: {datasets_raw!r}")
    for x in datasets_raw:
        if not isinstance(x, str) or not x.startswith("/"):
            raise ValueError(
                f"run.datasets 须为节点本地绝对路径 (以 / 开头), 实际: {x!r}")
        # 根目录直属文件: 所在目录为 /, 整盘挂载危险且无意义
        if x.rsplit("/", 1)[0] == "":
            raise ValueError(
                f"run.datasets 不支持根目录直属文件 (所在目录会整盘挂载), "
                f"请移入子目录: {x!r}")
    # 依赖安装命令固定读流水线 configs/pip_deps.txt (零配置: 存在即生效,
    # 不存在跳过不报错——空文件/删文件 = 不装); 用 __file__ 定位, 不受
    # run.py 调用 cwd 影响 (支持任意路径执行)
    _deps_file = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "configs", "pip_deps.txt")
    pip_cmds = []
    if os.path.isfile(_deps_file):
        # utf-8-sig 兼容 Windows BOM; 文本模式通用换行 + strip 兜底 \r
        with open(_deps_file, "r", encoding="utf-8-sig") as df:
            pip_cmds = [ln.strip() for ln in df
                        if ln.strip() and not ln.strip().startswith("#")]
    code_raw = run_raw.get("code") or {}
    git_remote = code_raw.get("git_remote")
    # ref str 化: YAML 纯数字 ref (如分支 123) 解析为 int, 直接传 pipeline 的
    # shlex.quote 会抛 TypeError; 顺带提示含前导零的数字 ref (如 0915) 会被
    # YAML 解析吞零 (0915→915), 须写成带引号字符串
    ref = code_raw.get("ref")
    if ref is not None and not isinstance(ref, str):
        _log(f"[警告] run.code.ref 非字符串 ({ref!r}), 按 {str(ref)!r} 使用; "
             f"若实际值含前导零请改带引号写法: ref: \"{ref}\"")
        ref = str(ref)

    workspace = run_raw["workspace"]
    if not workspace.startswith("/"):
        raise ValueError(
            f"run.workspace 须为绝对路径 (以 / 开头), 实际: {workspace!r}")
    run = RunConfig(
        workspace=workspace,
        git_remote=git_remote,
        ref=ref,
        docker=docker,
        env={**_DEFAULT_ENV,
             **{str(k): str(v) for k, v in env_raw.items()}},
        a5_env={str(k): str(v) for k, v in a5_env_raw.items()},
        datasets=list(datasets_raw),
        pip_cmds=pip_cmds,
    )

    nodes = []
    for n in raw.get("nodes") or []:
        # arch 必填且须为已知架构 (挂卡数量与 A5 适配都依赖它, 拼错直接报错)
        arch = str(n.get("arch") or "").lower()
        if arch not in _ARCH_NPUS:
            raise ValueError(
                f"节点 {n['host']}: arch 必填且须为 {'/'.join(_ARCH_NPUS)} 之一 "
                f"(a3=16 卡, a5=8 卡), 实际 {n.get('arch')!r}")
        nodes.append(NodeConfig(host=n["host"], arch=arch,
                                user=n.get("user") or "root",
                                port=n.get("port") or 22))

    suites = []
    seen_names = set()
    for s in raw.get("suites") or []:
        # name 可省略: 默认取 file basename 去 .py (如 .../test_npu_qwen3_32b.py
        # → test_npu_qwen3_32b), --suite 过滤与目录命名均自然匹配
        file_path = s["file"]
        raw_name = s.get("name")
        # str() 化: YAML 写数字 (name: 123) 时避免后续 re.match/os.path.join 对 int 报错
        name = (str(raw_name) if raw_name
                else os.path.splitext(os.path.basename(file_path))[0])
        # suite name 重复: 容器名/目录名/summary 均按 name 区分, 重复会互相覆盖
        if name in seen_names:
            raise ValueError(
                f"用例 name 重复: {name!r} (容器名/目录名/summary 均按 name 区分, "
                f"重复会互相覆盖)")
        seen_names.add(name)
        # name 用作 docker 容器名 (sgl-pipeline-{name}), 含空格/中文等特殊字符
        # 会导致 docker 报错; 允许字母数字/下划线/连字符/点
        if not re.match(r'^[A-Za-z0-9_.-]+$', name):
            raise ValueError(
                f"用例 name 只允许字母数字/下划线/连字符/点: {name!r}")
        roles = s.get("roles")
        multinode = s.get("multinode")
        # node / roles / multinode 三选一
        specified = [k for k, v in (("node", s.get("node")),
                                    ("roles", roles),
                                    ("multinode", multinode)) if v]
        if len(specified) > 1:
            raise ValueError(
                f"用例 {name}: {'/'.join(specified)} 互斥, 只能配一个")
        node = s.get("node")
        if not specified:
            # 三者均未配: 恰好只定义 1 个节点时默认该节点 (单机配置可省略 node);
            # 多节点时无法推断, 显式报错
            if len(nodes) != 1:
                raise ValueError(
                    f"用例 {name}: 须配 node/roles/multinode 之一 "
                    f"(仅当 nodes 恰好定义 1 个节点时 node 才可省略, "
                    f"当前定义了 {len(nodes)} 个)")
            node = nodes[0].host
        if roles:
            missing = set(_MULTI_ROLES) - set(roles)
            if missing:
                raise ValueError(
                    f"用例 {name}: roles 缺少角色 {', '.join(sorted(missing))} "
                    f"(需 {'/'.join(_MULTI_ROLES)})")
            unknown = set(roles) - set(_MULTI_ROLES)
            if unknown:
                raise ValueError(
                    f"用例 {name}: 未知角色 {', '.join(sorted(unknown))} "
                    f"(仅支持 {'/'.join(_MULTI_ROLES)})")
            # router 只能单个节点 (sglang 框架: 多个 router pod 会各自起 router 进程,
            # 造成端口冲突和路由混乱); prefill/decode 归一化为列表以统一处理
            norm = {}
            for r in _MULTI_ROLES:
                val = roles[r]
                if isinstance(val, str):
                    val = [val]
                elif not isinstance(val, list):
                    raise ValueError(
                        f"用例 {name}: roles.{r} 须为字符串或列表, 实际 {type(val).__name__}")
                if r == "router" and len(val) > 1:
                    raise ValueError(
                        f"用例 {name}: router 角色只支持单个节点, "
                        f"实际配了 {len(val)} 个")
                norm[r] = val
            roles = norm
        if multinode:
            if not isinstance(multinode, list) or len(multinode) < 2:
                raise ValueError(
                    f"用例 {name}: multinode 须为 ≥2 个节点的列表"
                    f" (单节点请用 node)")
        suites.append(SuiteConfig(
            name=name, node=node, file=file_path,
            timeout_minutes=s.get("timeout_minutes"),
            roles=roles, multinode=multinode,
        ))

    return PipelineConfig(run=run, nodes=nodes, suites=suites)


def parse_at(at_str):
    """解析 --at 字符串为目标 datetime。

    支持两种格式:
      - "YYYY-MM-DD HH:MM:SS" (绝对时间, 已过则立即执行并警告)
      - "HH:MM:SS"            (今天此刻, 已过则取明天)
    """
    now = datetime.datetime.now()
    for fmt, full in (("%Y-%m-%d %H:%M:%S", True), ("%H:%M:%S", False)):
        try:
            dt = datetime.datetime.strptime(at_str, fmt)
            if not full:
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
                if dt <= now:
                    dt += datetime.timedelta(days=1)
            elif dt <= now:
                _log(f"[定时] 指定时间 {dt.strftime('%Y-%m-%d %H:%M:%S')} 已过, "
                     f"立即开始执行")
            return dt
        except ValueError:
            continue
    raise ValueError(f"无法解析 --at 时间: {at_str}  (支持 'YYYY-MM-DD HH:MM:SS' 或 'HH:MM:SS')")


def wait_until(target_dt):
    """阻塞至 target_dt, 每分钟打印一次等待状态。"""
    while True:
        now = datetime.datetime.now()
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            break
        _log(f"[定时] 等待至 {target_dt.strftime('%Y-%m-%d %H:%M:%S')} "
             f"(剩余 {int(remaining)} 秒)")
        time.sleep(min(remaining, 60))
    _log(f"[定时] 到达指定时间, 开始执行")


def parse_args():
    parser = argparse.ArgumentParser(description="sglang 本地测试流水线")
    parser.add_argument("--config", "-c", required=True, help="配置文件 (YAML)")
    parser.add_argument("--suite", action="append", help="只执行指定用例 (可多次传)")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    parser.add_argument("--at", metavar="TIME",
                        help="定时执行: 指定开始时间, 支持 'YYYY-MM-DD HH:MM:SS' 或 'HH:MM:SS' "
                             "(今天已过则取明天)")
    return parser.parse_args()


def print_config(cfg, config_path):
    ref_desc = f" ref={cfg.run.ref}" if cfg.run.ref else ""
    _log(f"[配置] {config_path}: 节点={len(cfg.nodes)} 用例={len(cfg.suites)}"
        f"{ref_desc} repo={cfg.run.repo}")
    _log(f"[配置] 镜像: {cfg.run.docker.image}")


def filter_suites(cfg, names):
    """按 --suite 过滤用例, 返回是否仍有可执行用例。"""
    if not names:
        return True
    selected = set(names)
    cfg.suites = [s for s in cfg.suites if s.name in selected]
    if not cfg.suites:
        _log(f"[错误] 没有匹配的用例: {', '.join(selected)}")
        return False
    _log(f"[过滤] --suite 命中 {len(cfg.suites)} 个用例: "
         f"{', '.join(s.name for s in cfg.suites)}")
    return True


def _suite_hosts(suite):
    """用例涉及的所有节点 host (单机=1 个; 多机 PD=各角色节点; 多机 TP=所有节点)。"""
    if suite.roles:
        return [h for hosts in suite.roles.values() for h in hosts]
    if suite.multinode:
        return list(suite.multinode)
    return [suite.node]


def _suite_master_host(suite):
    """跑测试逻辑的节点 host (单机=node; PD 分离=router; 混布 TP=第一个节点)。

    worker/PD 节点只起 server, 不跑测试、不装精度框架 (与 run.datasets
    仅测试节点需要同理)。PD 分离 router 恒为 1 个 (框架限制), 取 [0]。
    """
    if suite.roles:
        return suite.roles["router"][0]
    if suite.multinode:
        return suite.multinode[0]
    return suite.node


# 精度用例特征: 用例文件 import test_npu_accuracy_utils (run_evalscope.sh 的
# 唯一触发入口, evalscope 源码/软链只服务它)。accuracy 目录外也有此类用例
# (如 basic_function/test_npu_swa_full_tokens_ratio.py), 故按文件内容判定
# 而非路径前缀; run_evalscope 兜底直接调脚本的写法
_ACCURACY_MARKERS = ("test_npu_accuracy_utils", "run_evalscope")


def _is_accuracy_suite(cfg, suite):
    """判定用例是否精度用例 (需要 evalscope 源码): 读执行机 repo 上的用例
    文件找特征 import (repo 此刻必已就绪, prepare 顺序保证)。读不到 (绝对
    路径文件不在执行机/文件缺失) 保守返回 True——宁可多 clone 一次, 不让
    精度用例静默回退在线安装。"""
    path = (suite.file if suite.file.startswith("/")
            else f"{cfg.run.repo}/{suite.file}")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return True
    return any(m in content for m in _ACCURACY_MARKERS)


def prepare_nodes(cfg, dry_run):
    """先在执行机准备代码仓 (唯一 git 操作点), 再逐节点准备。

    节点准备: 联网时镜像 pull (不存在才拉); 远程节点清理旧代码仓后从
    执行机整仓复制 (联网/离线均执行), 保证各节点代码与执行机严格一致。

    返回 {host: 是否就绪}; 节点未定义或执行机代码仓就绪失败时返回 None。
    """
    # 代码准备只发生在执行机; 失败则所有节点都无代码可分发, 直接终止
    if not prepare_local_repo(cfg, dry_run):
        _log("[错误] 执行机代码仓准备失败, 各节点无法分发代码, 终止")
        return None
    prepared = {}
    for host in dict.fromkeys(h for s in cfg.suites for h in _suite_hosts(s)):
        node = cfg.find_node(host)
        if node is None:
            _log(f"[错误] 用例引用的节点未在 nodes 中定义: {host}")
            return None
        print(f"\n----- [prepare] {host} -----")
        prepared[host] = prepare_node(cfg, node, dry_run)
        if not prepared[host]:
            _log(f"[错误] 节点 {host} 准备失败, 其用例将全部记为失败 (status=error)")
        else:
            # 就绪后清理 sglang 残留 (宿主机进程 + 容器), 确保 NPU 卡无占用;
            # 失败不阻断 (用例报卡占用时按 [cleanup] 提示手动检查)
            cleanup_node_sglang(cfg, node, dry_run)
    # evalscope 源码 (固定 {workspace}/evalscope): 仅配置了精度用例才准备
    # (按用例文件内容判定, 见 _is_accuracy_suite); 执行机 clone 一次 + 仅分发
    # 到精度用例跑测试的节点 (去重); 失败不阻断 (回退在线安装)
    acc_suites = [s for s in cfg.suites if _is_accuracy_suite(cfg, s)]
    if acc_suites:
        prepare_evalscope(
            cfg,
            [h for h in dict.fromkeys(_suite_master_host(s) for s in acc_suites)
             if prepared.get(h)],
            dry_run)
    else:
        _log("[prepare] 未配置精度用例, 跳过 evalscope 源码准备")
    return prepared


def run_suites(cfg, prepared, run_id, run_dir, dry_run):
    """逐个执行用例, 返回结果列表。

    单机用例经 SingleNodeContainers 复用节点长驻共享容器 (每节点一个,
    configs/pip_deps.txt 依赖只装一次), run 结束 (含中途异常/中断) finally
    统一删除容器 (runs/ 原件保留, 不清理)。
    """
    results = []
    shared = SingleNodeContainers(cfg, run_id, dry_run)
    try:
        for i, suite in enumerate(cfg.suites):
            print(f"\n----- [{i+1}/{len(cfg.suites)}] {suite.name} -----")
            hosts = _suite_hosts(suite)
            if not all(prepared.get(h) for h in hosts):
                missing = [h for h in hosts if not prepared.get(h)]
                _log(f"[错误] 节点 {', '.join(missing)} 未就绪, {suite.name} 记为 error 不执行")
                res = {"name": suite.name, "node": ",".join(hosts),
                       "status": "error", "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "duration_sec": 0, "error": "节点准备失败, 未执行"}
            elif suite.roles:
                res = execute_multinode_suite(cfg, suite, run_id, run_dir, dry_run)
            elif suite.multinode:
                res = execute_multinode_tp_suite(cfg, suite, run_id, run_dir, dry_run)
            else:
                node = cfg.find_node(suite.node)
                res = execute_suite(cfg, suite, node, run_id, run_dir, shared,
                                    dry_run)
            res["file"] = suite.file
            if dry_run:
                res["status"] = "dryrun"
            results.append(res)
            _log(f"[结果] {suite.name}: {res['status']}")
            # 每跑完一条即写 summary.json, 中途被杀也能看到已完成用例的结果
            write_summary(results, run_id, run_dir)
    finally:
        shared.cleanup()
    return results


def write_summary(results, run_id, run_dir):
    """写 summary.json: 成功/失败条数 + 脚本路径 (详细过程看各用例的日志)。
    每条用例完成即全量重写一次 (内容始终为当前已完成用例的汇总)。"""
    passed = [r for r in results if r["status"] == "pass"]
    failed = [r for r in results if r["status"] in ("fail", "error")]
    summary = {"run_id": run_id, "total": len(results),
               "passed": len(passed), "failed": len(failed),
               "passed_scripts": [r["file"] for r in passed],
               "failed_scripts": [r["file"] for r in failed]}
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return passed, failed


def print_summary(results, passed, failed, run_dir):
    print("\n===== 汇总 =====")
    for r in results:
        icon = {"pass": "PASS", "fail": "FAIL",
                "error": "ERROR", "dryrun": "DRY-RUN"}.get(r["status"], "?")
        print(f"  [{icon}] {r['file']}  {r.get('duration_sec', 0)}s")
    print(f"共 {len(results)} 条: 通过 {len(passed)} / 失败 {len(failed)}")
    print(f"结果: {os.path.abspath(run_dir)}")


def main():
    args = parse_args()

    if args.at:
        target = parse_at(args.at)
        wait_until(target)

    try:
        cfg = load_config(args.config)
    except (ValueError, KeyError, yaml.YAMLError, OSError) as e:
        _log(f"[错误] 配置文件无效: {e}")
        return 2
    print_config(cfg, args.config)

    if not filter_suites(cfg, args.suite):
        return 2

    # run_id = {yaml_stem}-{timestamp}: 文件名表职责 (summary.json), 目录名表来源 (哪个 yaml + 什么时间)
    yaml_stem = re.sub(r"\.(ya?ml)$", "", os.path.basename(args.config), flags=re.I)
    run_id = f"{yaml_stem}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir = os.path.join(cfg.output_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    print(f"===== 流水线 run_id={run_id}  用例={len(cfg.suites)} =====")

    prepared = prepare_nodes(cfg, args.dry_run)
    if prepared is None:
        return 2

    results = run_suites(cfg, prepared, run_id, run_dir, args.dry_run)
    passed, failed = write_summary(results, run_id, run_dir)
    print_summary(results, passed, failed, run_dir)
    return 0 if (not failed or args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
