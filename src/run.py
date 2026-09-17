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
from pipeline import execute_suite, prepare_node


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
    user: str = "root"
    port: int = 22
    npus: int = 8
    # tp-size 缩放因子: 用例 other_args 中的 --tp-size 会除以该值后启动 server。
    # 默认 1 = 原样执行 (A3 环境); A5 单机环境配 2 即把 tp-size 自动减半,
    # 不影响未显式配 --tp-size 的用例 (默认 1, 不除)。
    tp_divisor: int = 1


@dataclass
class DockerConfig:
    image: str
    devices: Union[str, List[int]] = "auto"  # "auto"=按节点 npus 生成 davinci0..N-1
    net: str = "host"
    shm_size: str = "16g"


@dataclass
class PrepareConfig:
    """节点准备。online=true 需节点可达 registry/git remote;
    false 则完全使用节点现状 (镜像/代码已手动就位, 不联网不动代码)。"""
    online: bool = False


@dataclass
class RunConfig:
    workspace: str
    repo: str
    git_remote: str = None
    ref: str = "main"
    docker: DockerConfig = None
    prepare: PrepareConfig = None
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class SuiteConfig:
    name: str
    node: str = None
    file: str = None
    timeout_minutes: int = None


@dataclass
class PipelineConfig:
    run: RunConfig
    nodes: List[NodeConfig]
    suites: List[SuiteConfig]
    output_dir: str = "results"

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
    )
    prepare = PrepareConfig(online=run_raw.get("prepare", False))
    git_remote = run_raw.get("code", {}).get("git_remote")
    if prepare.online and not git_remote:
        raise ValueError("联网模式 (prepare: true) 必须配置 run.code.git_remote")

    run = RunConfig(
        workspace=run_raw["workspace"],
        repo=run_raw["code"]["repo"],
        git_remote=git_remote,
        ref=run_raw.get("code", {}).get("ref", "main"),
        docker=docker,
        prepare=prepare,
        env={**_DEFAULT_ENV,
             **{str(k): str(v) for k, v in run_raw.get("env", {}).items()}},
    )

    nodes = [NodeConfig(host=n["host"], user=n.get("user", "root"),
                        port=n.get("port", 22), npus=n.get("npus", 8),
                        tp_divisor=n.get("tp_divisor", 1))
             for n in raw.get("nodes", [])]

    suites = []
    for s in raw.get("suites", []):
        suites.append(SuiteConfig(
            name=s["name"], node=s.get("node"),
            file=s["file"], timeout_minutes=s.get("timeout_minutes"),
        ))

    return PipelineConfig(run=run, nodes=nodes, suites=suites,
                          output_dir=raw.get("output", {}).get("dir", "results"))


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
        ts = now.strftime("%H:%M:%S")
        print(f"[{ts}][定时] 等待至 {target_dt.strftime('%Y-%m-%d %H:%M:%S')} "
              f"(剩余 {int(remaining)} 秒)", flush=True)
        time.sleep(min(remaining, 60))
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}][定时] "
          f"到达指定时间, 开始执行", flush=True)


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
    print(f"[配置] {config_path}: 节点={len(cfg.nodes)} 用例={len(cfg.suites)} "
          f"prepare={'联网' if cfg.run.prepare.online else '离线'} ref={cfg.run.ref} "
          f"repo={cfg.run.repo}")
    print(f"[配置] 镜像: {cfg.run.docker.image}")
    for n in cfg.nodes:
        tip = f" (tp_divisor={n.tp_divisor}, 用例 --tp-size 自动除以该值)" if n.tp_divisor > 1 else ""
        print(f"[配置] 节点 {n.host}: npus={n.npus}{tip}")


def filter_suites(cfg, names):
    """按 --suite 过滤用例, 返回是否仍有可执行用例。"""
    if not names:
        return True
    selected = set(names)
    cfg.suites = [s for s in cfg.suites if s.name in selected]
    if not cfg.suites:
        print(f"[错误] 没有匹配的用例: {', '.join(selected)}")
        return False
    print(f"[过滤] --suite 命中 {len(cfg.suites)} 个用例: "
          f"{', '.join(s.name for s in cfg.suites)}")
    return True


def prepare_nodes(cfg, dry_run):
    """按节点去重, 逐节点准备 (镜像 pull / 代码 clone / fetch / checkout)。

    返回 {host: 是否就绪}; 节点未定义时返回 None。
    """
    prepared = {}
    for host in dict.fromkeys(s.node for s in cfg.suites):
        node = cfg.find_node(host)
        if node is None:
            print(f"[错误] 用例引用的节点未在 nodes 中定义: {host}")
            return None
        print(f"\n----- [prepare] {host} -----")
        prepared[host] = prepare_node(cfg, node, dry_run)
        if not prepared[host]:
            print(f"[错误] 节点 {host} 准备失败, 其用例将全部记为失败 (status=error)")
    return prepared


def run_suites(cfg, prepared, run_id, run_dir, dry_run):
    """逐个执行用例, 返回结果列表。"""
    results = []
    for i, suite in enumerate(cfg.suites):
        print(f"\n----- [{i+1}/{len(cfg.suites)}] {suite.name} -----")
        if not prepared.get(suite.node):
            print(f"[错误] 节点 {suite.node} 未就绪, {suite.name} 记为 error 不执行")
            res = {"name": suite.name, "node": suite.node,
                   "status": "error", "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "duration_sec": 0, "error": "节点准备失败, 未执行"}
        else:
            node = cfg.find_node(suite.node)
            res = execute_suite(cfg, suite, node, run_id, run_dir, dry_run)
        res["file"] = suite.file
        if dry_run:
            res["status"] = "dryrun"
        results.append(res)
        print(f"[结果] {suite.name}: {res['status']}")
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

    cfg = load_config(args.config)
    print_config(cfg, args.config)

    if not filter_suites(cfg, args.suite):
        return 2

    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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
