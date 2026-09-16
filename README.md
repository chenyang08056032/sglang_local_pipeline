# sglang 本地测试流水线使用指导

在没有 CI / k8s 的物理服务器（NPU 环境）上批量执行 sglang 测试用例的轻量工具。复用 CI 的容器内测试逻辑（`run_suite.py`、ascend 工具覆盖），只自研本地必需的 SSH 编排 + docker run 两个环节。

## 1. 目录结构

```
sglang_local_pipeline/
├── src/
│   ├── run.py          # 入口: 加载配置、节点准备、编排执行、汇总结果
│   └── pipeline.py     # 核心: SSH 远程执行、节点准备、docker 命令构造、日志拉回
├── configs/
│   └── example.yaml    # 配置模板
├── README.md
└── .gitignore
```

## 2. 环境准备

### 执行机（跑 `python3 src/run.py` 的机器）

| 项目 | 要求 |
|---|---|
| sglang_local_pipeline 代码 | clone 或拷贝本目录 |
| Python 3 | 加 `pip install pyyaml`（唯一第三方依赖） |
| ssh 客户端 + tar | Linux 自带 |
| SSH 免密 | 已配置到所有节点的免密登录（见附录 A） |
| 网络 | 可达各节点的 22 端口 |

执行机**不需要** Docker、NPU 驱动、git、sglang 仓库——这些全部在节点上。
执行机也可以就是节点本身（如 A3 上直接跑，SSH 回环到自己）。

### 节点（A3 / A5 等目标机器）

| 项目 | 要求 |
|---|---|
| SSH 免密 | 执行机可 `ssh root@<host>` 直连 |
| Docker | 已安装并运行 |
| NPU 驱动 | 存在 `/usr/local/Ascend/driver`、`/usr/local/Ascend/firmware`、`/dev/davinci*`、`/dev/davinci_manager`、`/dev/hisi_hdc`、`/etc/ascend_install.info` |
| 网络（可选） | 可达 docker registry 和 git remote（用于自动 pull / clone / fetch；离线时可提前手动准备） |
| 磁盘 | 模型缓存 `~/.cache`（几十 GB）+ 仓库 + workspace |

镜像、sglang 仓库默认由流水线自动准备，无需提前操作。

## 3. 配置文件说明

复制 `configs/example.yaml` 修改，各字段如下：

```yaml
run:
  workspace: /root/sglang_local_pipeline   # 节点上的工作目录（运行产物落在节点这里）
  code:
    repo: /root/.cache/sglang           # 节点上的 sglang 源码路径
    git_remote: https://github.com/sgl-project/sglang.git   # clone 来源
    ref: main                           # 目标版本: 分支名 / tag / commit SHA
  prepare:                              # 执行前每节点自动准备一次
    pull_image: true                    # 镜像不存在则 docker pull
    clone_repo: true                    # 仓库不存在则 clone（clone_repo 为 true 时必须配 git_remote）
    fetch: true                         # 每次 run 前 git fetch 测最新代码
  docker:
    image: <镜像地址>                    # 测试容器镜像
    devices: auto                       # auto=按节点 npus 映射; 或列表 [0,1,2,3]
    net: host
    shm_size: 16g                       # 固定附加 --privileged --ipc=host
  env:                                  # 注入容器的环境变量
    SGLANG_USE_MODELSCOPE: "true"
    HF_ENDPOINT: https://hf-mirror.com
    SGLANG_IS_IN_CI: "true"
    TORCH_EXTENSIONS_DIR: /tmp/torch_extensions
    PYTORCH_NPU_ALLOC_CONF: "expandable_segments:True"
    STREAMS_PER_DEVICE: "32"

nodes:                                  # 所有可用节点
  - host: 192.168.10.1                  # A3
    user: root
    port: 22
    npus: 16                            # NPU 卡数，devices=auto 时决定映射 /dev/davinci0..N-1
  - host: 192.168.10.2                  # A5
    user: root
    port: 22
    npus: 8

suites:                                 # 要执行的用例，串行执行
  - name: full-1-npu-a3                 # 复用 CI 套件，容器内跑 run_suite.py
    type: suite
    node: 192.168.10.1                  # 在哪个节点执行
    nightly: true                       # 透传 run_suite.py --nightly --continue-on-error
    timeout_per_file: 3600              # 透传 run_suite.py --timeout-per-file

  - name: qwen3-32b-gsm8k               # 直接跑单个测试文件
    type: file
    node: 192.168.10.1
    file: test/registered/npu/llm_models/test_npu_qwen3_32b.py   # 相对 repo 的路径；绝对路径须在容器可见的挂载内（repo 或 ~/.cache）
    timeout_minutes: 120                # 整个 docker run 的超时（分钟），可省略

output:
  dir: results                         # 本地结果目录（执行机上，默认值；勿与节点 workspace 下的 runs/ 同名同址）
```

## 4. 快速开始

```bash
# 1. 首次验证: 只打印将在节点上执行的命令，不消耗 NPU 资源
python3 src/run.py --config configs/example.yaml --dry-run

# 2. 执行全部用例
python3 src/run.py --config configs/example.yaml

# 3. 只执行指定用例（--suite 可传多次）
python3 src/run.py --config configs/example.yaml --suite full-1-npu-a3
python3 src/run.py --config configs/example.yaml --suite full-1-npu-a3 --suite qwen3-32b-gsm8k
```

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--config` / `-c` | 配置文件路径（必填） |
| `--suite NAME` | 只执行指定用例，可多次传，按 name 匹配 |
| `--dry-run` | 只打印命令不执行，用于校验配置和 docker 命令 |

### 退出码

- `0`：全部通过（或 dry-run）
- `1`：有用例失败
- `2`：配置错误（用例引用的节点未定义、无匹配用例等）

## 5. 执行流程

```
python3 src/run.py
    │
    ├─ [prepare] 每个被用到的节点执行一次（SSH 到节点）:
    │     docker image inspect 镜像 || docker pull
    │     仓库不存在则 git clone；remote 指向不符则 set-url
    │     git fetch origin --tags --force
    │     git checkout --force {ref}
    │     分支则 git reset --hard origin/{ref}   # 保证与远端严格一致
    │
    ├─ [execute] 逐用例串行执行（SSH 到节点）:
    │     docker run --rm --privileged --ipc=host
    │       --device /dev/davinci0..N-1 + 管理设备
    │       挂载: repo / workspace 输出目录 / driver / 模型缓存(~/.cache)
    │     容器内: 覆盖 ascend 工具 → 预置 gsm8k/ShareGPT 数据集到 /tmp
    │              → run_suite.py 或单个用例文件
    │
    └─ [fetch] tar 管道拉回节点上的运行产物到本地结果目录
```

## 6. 结果产物与日志路径

下面用一个完整示例说明。假设：

- 执行机就是 A3 本身（192.168.10.1），在 `/root/sglang_local_pipeline` 目录下执行
- 配置使用默认值：`workspace: /root/sglang_local_pipeline`、`output.dir: results`
- 执行的用例为 `full-1-npu-a3`，run_id 为 `20260916-100000`

执行机和节点使用**不同的目录**（`results/` vs `runs/`），即使同一台机器也不冲突。

### 6.1 节点上的日志（原始产物）

路径由 `workspace` + `runs` + `run_id` + `用例名` 拼成：

```
/root/sglang_local_pipeline/runs/20260916-100000/full-1-npu-a3/
├── suite.log          # 容器内 tee /output/suite.log 写入
├── tmp/               # 容器内 /tmp 挂载
└── plog/              # 容器内 /root/ascend/log 挂载（NPU 底层日志）
```

拉回后**不删除**，多次执行会按 run_id 各占一个子目录，累积保留。

### 6.2 执行机上的日志（拉回副本 + 实时回显）

路径由 `output.dir` + `run_id` + `用例名` 拼成，默认 `results/`，与节点的 `runs/` 分开：

```
/root/sglang_local_pipeline/results/20260916-100000/
├── summary.json                       # run.py 写入的汇总
└── full-1-npu-a3/
    ├── result.json                    # run.py 写入的单用例结果
    ├── suite.log                      # ssh_run 实时回显写盘
    ├── plog/                          # 从节点拉回的 NPU 底层日志
    └── tmp/                           # 从节点拉回的临时文件
```

多个用例时，每个用例各占一个子目录，互不干扰：

```
/root/sglang_local_pipeline/results/20260916-100000/
├── summary.json
├── full-1-npu-a3/
│   ├── result.json
│   ├── suite.log
│   └── plog/
└── qwen3-32b-gsm8k/
    ├── result.json
    ├── suite.log
    └── plog/
```

run 结束时控制台最后一行打印绝对路径：`结果: /root/sglang_local_pipeline/results/20260916-100000`

### 6.3 执行机 = 节点时的路径关系

本例执行机就是节点，但两边使用**不同的目录名**（`results/` vs `runs/`），不会冲突：

```
/root/sglang_local_pipeline/
├── runs/                                    ← 节点（容器挂载写入）
│   └── 20260916-100000/
│       └── full-1-npu-a3/
│           ├── suite.log                    ← 容器 tee 写
│           ├── tmp/                         ← mkdir -p + 挂载
│           └── plog/                        ← mkdir -p + 挂载
│
└── results/                                 ← 执行机（run.py + ssh_run + fetch 写入）
    └── 20260916-100000/
        ├── summary.json                     ← run.py 写
        └── full-1-npu-a3/
            ├── result.json                  ← run.py 写
            ├── suite.log                    ← ssh_run 写
            ├── plog/                        ← fetch 从节点拉回
            └── tmp/                         ← fetch 从节点拉回
```

两个目录完全独立，无并发写入同一文件的问题。

跨节点执行时同理：节点上留在 `runs/`，执行机上落在 `results/`，fetch 把节点 `runs/{id}/{suite}/` 内容拉回到执行机 `results/{id}/{suite}/`。

### 6.4 在任意路径执行 run.py

`run.py` 可在任意目录下用绝对路径调用，不受 cwd 限制：

```bash
cd /home
python3 /root/sglang_local_pipeline/src/run.py --config /root/sglang_local_pipeline/configs/example.yaml
```

此时节点路径不变，但执行机上的结果目录变为 `/home/results/20260916-100000/`（相对 cwd）。

想固定位置不受 cwd 影响，配置里写绝对路径：

```yaml
output:
  dir: /root/pipeline_results
```

## 7. 常见问题

**Q: 改了个人 fork 的分支，节点上的旧仓库会冲突吗？**
不会。`prepare` 阶段会 `git remote set-url` 切到新 remote 再 fetch；分支用 `reset --hard origin/{ref}` 对齐，只影响当前 checkout 的分支，不影响其他本地分支。

**Q: 节点无法访问 GitHub / 镜像仓库怎么办？**
节点预配代理；或提前手动 `git clone` + `docker pull`，配置里 `prepare` 各项设为 `false`（fetch=false 时仅 checkout，不再联网）。
gsm8k 数据集默认从 GitHub 在线下载，离线节点可提前放到节点 `~/.cache/modelscope/hub/datasets/tmp/test.jsonl`，perf 套件的 ShareGPT 数据集放 `~/.cache/modelscope/hub/datasets/otavia/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json`——容器启动时会自动拷入 /tmp（缓存缺失则忽略，回退在线下载）。

**Q: 如何测某个特定 commit？**
`run.code.ref` 改为 commit SHA 即可，checkout 逻辑对分支/tag/SHA 通用。

**Q: `file` 能配 repo 外的自定义用例吗？**
能，但用例跑在容器里，文件必须在容器可见的挂载内。最简单的做法：把用例放到节点的 `~/.cache/` 下（随模型缓存挂载进容器），然后 `file` 配绝对路径，如 `/root/.cache/my_tests/test_foo.py`。其他节点路径（如 `/data/...`）容器内不可见，会报 `No such file or directory`。

**Q: 需要登录的私有镜像？**
在节点上提前 `docker login`，`prepare.pull_image` 保持 true 即可 pull。

**Q: 只想看会执行什么命令？**
用 `--dry-run`，输出节点上将要执行的完整 docker 命令，不消耗 NPU 资源。

## 附录 A: 配置 SSH 免密

```bash
# 执行机上（已有 key 可跳过第一步）
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
ssh-copy-id root@192.168.10.1
ssh-copy-id root@192.168.10.2
```

不支持在配置文件中传密码（原生 ssh 不接受非交互传密码）。
