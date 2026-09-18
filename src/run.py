#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sglang 本地测试流水线 (最小可用版)。

用法:
    python3 src/run.py --config configs/example.yaml
    python3 src/run.py --config configs/example.yaml --suite qwen3-32b-gsm8k
    python3 src/run.py --config configs/example.yaml --dry-run
    python3 src/run.py --config configs/example.yaml --at "2026-09-17 18:00:00"
    python3 src/run.py --config configs/example.yaml --at "18:00:00"   # 今天已过则取明天
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
from pipeline import (_ARCH_NPUS, _MULTI_ROLES, _log, execute_multinode_suite,
                      execute_multinode_tp_suite, execute_suite, prepare_node)


# A3 NPU 环境的标准环境变量, 注入每个测试容器
# YAML 的 run.env 可覆盖这些默认值或追加新键 (按 key 合并, 不必全量重写)
_DEFAULT_ENV = {
    "SGLANG_USE_MODELSCOPE": "true",
    "HF_ENDPOINT": "https://hf-mirror.com",
    "SGLANG_IS_IN_CI": "true",
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
class PrepareConfig:
    """节点准备。online=true 需节点可达 registry/git remote;
    false 则完全使用节点现状 (镜像/代码已手动就位, 不联网不动代码)。"""
    online: bool = False


@dataclass
class RunConfig:
    workspace: str
    git_remote: str = None
    ref: str = "main"
    docker: DockerConfig = None
    prepare: PrepareConfig = None
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def repo(self):
        """节点上的 sglang 源码路径, 固定放在 workspace 下。"""
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

    run_raw = raw.get("run", {})
    docker_raw = run_raw.get("docker", {})
    docker = DockerConfig(
        image=docker_raw["image"],
        devices=docker_raw.get("devices", "auto"),
        net=docker_raw.get("net", "host"),
        shm_size=docker_raw.get("shm_size", "16g"),
        extra_mounts=list(docker_raw.get("extra_mounts") or []),
    )
    prepare = PrepareConfig(online=run_raw.get("prepare", False))
    code_raw = run_raw.get("code", {})
    git_remote = code_raw.get("git_remote")
    if prepare.online and not git_remote:
        raise ValueError("联网模式 (prepare: true) 必须配置 run.code.git_remote")

    workspace = run_raw["workspace"]
    run = RunConfig(
        workspace=workspace,
        git_remote=git_remote,
        ref=code_raw.get("ref", "main"),
        docker=docker,
        prepare=prepare,
        env={**_DEFAULT_ENV,
             **{str(k): str(v) for k, v in run_raw.get("env", {}).items()}},
    )

    nodes = []
    for n in raw.get("nodes", []):
        # arch 必填且须为已知架构 (挂卡数量与 A5 适配都依赖它, 拼错直接报错)
        arch = str(n.get("arch") or "").lower()
        if arch not in _ARCH_NPUS:
            raise ValueError(
                f"节点 {n['host']}: arch 必填且须为 {'/'.join(_ARCH_NPUS)} 之一 "
                f"(a3=16 卡, a5=8 卡), 实际 {n.get('arch')!r}")
        nodes.append(NodeConfig(host=n["host"], arch=arch,
                                user=n.get("user", "root"),
                                port=n.get("port", 22)))

    suites = []
    for s in raw.get("suites", []):
        roles = s.get("roles")
        multinode = s.get("multinode")
        # node / roles / multinode 三选一
        specified = [k for k, v in (("node", s.get("node")),
                                    ("roles", roles),
                                    ("multinode", multinode)) if v]
        if len(specified) > 1:
            raise ValueError(
                f"用例 {s['name']}: {'/'.join(specified)} 互斥, 只能配一个")
        node = s.get("node")
        if not specified:
            # 三者均未配: 恰好只定义 1 个节点时默认该节点 (单机配置可省略 node);
            # 多节点时无法推断, 显式报错
            if len(nodes) != 1:
                raise ValueError(
                    f"用例 {s['name']}: 须配 node/roles/multinode 之一 "
                    f"(仅当 nodes 恰好定义 1 个节点时 node 才可省略, "
                    f"当前定义了 {len(nodes)} 个)")
            node = nodes[0].host
        if roles:
            missing = set(_MULTI_ROLES) - set(roles)
            if missing:
                raise ValueError(
                    f"用例 {s['name']}: roles 缺少角色 {', '.join(sorted(missing))} "
                    f"(需 {'/'.join(_MULTI_ROLES)})")
            unknown = set(roles) - set(_MULTI_ROLES)
            if unknown:
                raise ValueError(
                    f"用例 {s['name']}: 未知角色 {', '.join(sorted(unknown))} "
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
                        f"用例 {s['name']}: roles.{r} 须为字符串或列表, 实际 {type(val).__name__}")
                if r == "router" and len(val) > 1:
                    raise ValueError(
                        f"用例 {s['name']}: router 角色只支持单个节点, "
                        f"实际配了 {len(val)} 个")
                norm[r] = val
            roles = norm
        if multinode:
            if not isinstance(multinode, list) or len(multinode) < 2:
                raise ValueError(
                    f"用例 {s['name']}: multinode 须为 ≥2 个节点的列表"
                    f" (单节点请用 node)")
        suites.append(SuiteConfig(
            name=s["name"], node=node, file=s["file"],
            timeout_minutes=s.get("timeout_minutes"),
            roles=roles, multinode=multinode,
        ))

    return PipelineConfig(run=run, nodes=nodes, suites=suites)


def parse_at(at_str):
    """解析 --at 字符串为目标 datetime。

    支持两种格式:
      - "YYYY-MM-DD HH:MM:SS" (绝对时间)
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
    _log(f"[配置] {config_path}: 节点={len(cfg.nodes)} 用例={len(cfg.suites)} "
        f"prepare={'联网' if cfg.run.prepare.online else '离线'} ref={cfg.run.ref} "
        f"repo={cfg.run.repo}")
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


def prepare_nodes(cfg, dry_run):
    """按节点去重, 逐节点准备 (镜像 pull / 代码 clone / fetch / checkout)。

    返回 {host: 是否就绪}; 节点未定义时返回 None。
    """
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
    return prepared


def run_suites(cfg, prepared, run_id, run_dir, dry_run):
    """逐个执行用例, 返回结果列表。"""
    results = []
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
            res = execute_suite(cfg, suite, node, run_id, run_dir, dry_run)
        res["file"] = suite.file
        if dry_run:
            res["status"] = "dryrun"
        results.append(res)
        _log(f"[结果] {suite.name}: {res['status']}")
    return results


def write_summary(results, run_id, run_dir):
    """写 summary.json: 成功/失败条数 + 脚本路径 (详细过程看各用例的 case.log)。"""
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
