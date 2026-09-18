# sglang 本地测试流水线使用指导

在没有 CI / k8s 的物理服务器（NPU 环境）上批量执行 sglang 测试用例的轻量工具。复用 CI 的 ascend 工具覆盖逻辑，只自研本地必需的节点编排 + docker run 两个环节。

## 1. 目录结构

```
sglang_local_pipeline/
├── src/
│   ├── run.py          # 入口: 加载配置、节点准备、编排执行、汇总结果
│   └── pipeline.py     # 核心: 节点执行 (自动检测本地/SSH)、节点准备、docker 命令构造、日志拉回
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
| ssh 客户端 + tar | 仅远程节点需要（本地节点直接 subprocess 执行，无需 ssh） |
| 网络 | 远程节点需可达 22 端口 |

执行机**不需要** Docker、NPU 驱动、git、sglang 仓库——这些全部在节点上。
执行机也可以就是节点本身（如 A3 上直接跑）：pipeline 自动检测节点 host 是否指向本机，是则直接本地执行（不走 SSH、无需免密配置、无需 sshd），否则走 SSH。

### 节点（A3 / A5 等目标机器）

| 项目 | 要求 |
|---|---|
| SSH 免密 | 仅远程节点需要（本地节点=执行机时自动走 subprocess，无需 SSH） |
| Docker | 已安装并运行 |
| NPU 驱动 | 存在 `/usr/local/Ascend/driver`、`/usr/local/Ascend/firmware`、`/dev/davinci*`、`/dev/davinci_manager`、`/dev/hisi_hdc`、`/etc/ascend_install.info` |
| 网络（可选） | 可达 docker registry 和 git remote（用于自动 pull / clone / fetch；离线时可提前手动准备） |
| 磁盘 | 模型缓存 `~/.cache`（几十 GB）+ 仓库 + workspace |

镜像、sglang 仓库：`prepare: true` 时由流水线自动准备（pull / clone / checkout），无需提前操作；默认 `prepare: false`（离线）时需提前手动 `docker pull`、把代码放到 `{workspace}/sglang`，流水线不联网、不动代码。

## 3. 配置文件说明

复制 `configs/example.yaml` 修改。各参数的默认值、是否必填按段落列表如下，YAML 里不写即取默认值。

### 3.1 run 段

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `run.workspace` | 无 | 是 | 节点上的工作目录。sglang 仓固定在 `{workspace}/sglang`，执行机结果目录固定在 `{workspace}/results`（与节点 `runs/` 平级，同机不冲突），均不可另配 |
| `run.prepare` | `false` | 否 | `true`=联网：镜像 pull + 代码 clone/fetch/checkout；`false`=离线：镜像/代码需提前手动就位，流水线不联网、不动代码 |
| `run.code.git_remote` | 无 | `prepare: true` 时必填 | clone/fetch 来源。**仅联网模式读取**；离线模式不生效，配置里可整段省略 |
| `run.code.ref` | `main` | 否 | 目标版本：分支名 / tag / commit SHA。仅联网模式 checkout |
| `run.docker.image` | 无 | 是 | 测试容器镜像 |
| `run.docker.devices` | `auto` | 否 | `auto`=按节点 arch 推导卡数映射 `/dev/davinci0..N-1`（a3=16、a5=8）+ 管理设备；或显式列表如 `[0,1,2,3]`，以列表为准 |
| `run.docker.net` | `host` | 否 | 容器网络模式 |
| `run.docker.shm_size` | `16g` | 否 | 容器共享内存大小；固定附加 `--privileged --ipc=host` |
| `run.docker.extra_mounts` | `[]` | 否 | 额外 `-v` 挂载项（追加到默认 driver/缓存等挂载之后），格式同 docker -v：`"host:container"` 或 `"/data:/data:ro"` |
| `run.env` | 内置 6 项（见 3.4） | 否 | 注入容器的环境变量，按 key 合并覆盖内置默认，可追加新键 |

### 3.2 nodes 段

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `nodes[].host` | 无 | 是 | 节点 IP。指向执行机本身时自动本地执行（不走 SSH、无需免密） |
| `nodes[].arch` | 无 | 是 | 节点架构：`a3`=16 卡、`a5`=8 卡。决定挂卡数量（devices=auto 时）及 A5 单机用例 `--tp-size` 减半适配；缺失或拼错启动时报错 |
| `nodes[].user` | `root` | 否 | SSH 用户名 |
| `nodes[].port` | `22` | 否 | SSH 端口 |

### 3.3 suites 段

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `suites[].name` | 无 | 是 | 用例名称（`--suite` 过滤用） |
| `suites[].node` | 无 | 三选一 | 单机用例：执行的节点 host。**nodes 仅定义 1 个节点时可省略**，默认用该节点；多节点时必须显式指定 |
| `suites[].roles` | 无 | 三选一 | 多机 PD 分离用例（第 7 节）：prefill/decode/router → 节点 |
| `suites[].multinode` | 无 | 三选一 | 多机混布 TP 用例（第 8 节）：节点列表，第一个 = master |
| `suites[].file` | 无 | 是 | 用例文件路径，相对 repo；绝对路径须在容器可见挂载内（repo 或 `~/.cache`） |
| `suites[].timeout_minutes` | 无（不限时） | 否 | docker run 超时（分钟）。多机用例为每角色容器超时，prefill/decode/worker 实际再加 2 分钟余量 |

`node` / `roles` / `multinode` 三者互斥，只能配一个。

### 3.4 run.env 内置默认值

不写 `run.env` 时容器自动注入以下 6 项（与 CI 一致）；需覆盖某项或追加新键时才写：

| 环境变量 | 默认值 |
|---|---|
| `SGLANG_USE_MODELSCOPE` | `true` |
| `HF_ENDPOINT` | `https://hf-mirror.com` |
| `SGLANG_IS_IN_CI` | `true` |
| `TORCH_EXTENSIONS_DIR` | `/tmp/torch_extensions` |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` |
| `STREAMS_PER_DEVICE` | `32` |

此外流水线会自动为所有容器注入 `no_proxy`/`NO_PROXY`（含全部节点 IP + localhost，且优先于节点 docker 的代理配置注入）：协调服务、健康检查等内网请求直连，不受节点 `/root/.docker/config.json` 代理影响；外网下载仍走代理。

### 3.5 配置示例（联网模式）

```yaml
run:
  workspace: /root/sglang_local_pipeline
  prepare: true                                # 离线改 false, 且 code: 段可整段删除
  code:
    git_remote: https://github.com/sgl-project/sglang.git
    ref: main
  docker:
    image: <镜像地址>

nodes:
  - host: 192.168.10.1                  # A3; user/port 取默认时可只写 host + arch
    arch: a3
  - host: 192.168.10.2                  # A5 (8 卡, 单机用例 --tp-size 自动减半)
    arch: a5

suites:
  - name: qwen3-32b-gsm8k
    node: 192.168.10.1
    file: test/registered/npu/llm_models/test_npu_qwen3_32b.py
    timeout_minutes: 120
  # 多机用例用 roles / multinode 代替 node, 见第 7/8 节
```

### 3.6 最小配置（单节点离线）

`nodes` 仅定义 1 个节点时，用例的 `node` 可省略（默认用该节点），单机快速验证的配置精简到：

```yaml
run:
  workspace: /root/sglang_local_pipeline
  docker:
    image: <镜像地址>

nodes:
  - host: 192.168.10.1
    arch: a3

suites:
  - name: qwen3-32b-gsm8k
    file: test/registered/npu/llm_models/test_npu_qwen3_32b.py
    timeout_minutes: 120
```

注意：该默认仅在**恰好 1 个节点**时生效。多节点配置下用例漏配 `node`/`roles`/`multinode` 会在启动时直接报错（不会静默选择某个节点）。

## 4. 快速开始

```bash
# 1. 首次验证: 只打印将在节点上执行的命令，不消耗 NPU 资源
python3 src/run.py --config configs/example.yaml --dry-run

# 2. 执行全部用例
python3 src/run.py --config configs/example.yaml

# 3. 只执行指定用例（--suite 可传多次）
python3 src/run.py --config configs/example.yaml --suite test_npu_qwen3_32b

# 4. 定时执行（等到指定时间再开始，便于夜间无人值守跑用例）
python3 src/run.py --config configs/example.yaml --at "2026-09-17 18:00:00"
python3 src/run.py --config configs/example.yaml --at "18:00:00"   # 今天已过则取明天
```

`--at` 按**执行机本地时间**计算（不是节点时间）。执行机与节点跨时区时尤其要注意。

查执行机当前时间：

```bash
date +"%Y-%m-%d %H:%M:%S"   # 输出形如 2026-09-17 18:00:00
date +"%H:%M:%S"            # 输出形如 18:00:00
```

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--config` / `-c` | 配置文件路径（必填） |
| `--suite NAME` | 只执行指定用例，可多次传，按 name 匹配 |
| `--dry-run` | 只打印命令不执行，用于校验配置和 docker 命令 |
| `--at TIME` | 定时执行：阻塞到指定时间再开始。支持 `YYYY-MM-DD HH:MM:SS` 或 `HH:MM:SS`（今天已过则取明天）。等待期间每分钟打印剩余秒数 |

### 退出码

- `0`：全部通过（或 dry-run）
- `1`：有用例失败
- `2`：配置错误（配置文件解析/校验失败、用例引用的节点未定义、无匹配用例等）

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
    │     单机用例: docker run --rm --privileged --ipc=host
    │       --device /dev/davinci0..N-1 + 管理设备
    │       挂载: repo / workspace 输出目录 / driver / 模型缓存(~/.cache)
    │              + run.docker.extra_mounts (用户自定义, 可选)
    │     容器内: 覆盖 ascend 工具 → 预置 gsm8k/ShareGPT 数据集到 /tmp
    │              → 单个用例文件 (A5 节点经 /output/run_case.py 包装启动,
    │                 --tp-size 自动减半)
    │     多机用例 (roles): 各角色节点并发 docker run 同一用例文件,
    │       以 HOSTNAME/POD_IP 环境变量区分角色 (见第 7 节)
    │
    └─ [fetch] tar 管道拉回节点上的运行产物到本地结果目录
```

## 6. 结果产物与日志路径

下面用一个完整示例说明。假设：

- 执行机就是 A3 本身（192.168.10.1）
- 配置使用默认值：`workspace: /root/sglang_local_pipeline`（结果目录固定为 `/root/sglang_local_pipeline/results`）
- 执行的用例为 `qwen3-32b-gsm8k`，run_id 为 `20260916-100000`

执行机和节点使用**不同的目录**（`results/` vs `runs/`），即使同一台机器也不冲突。

### 6.1 节点上的日志（原始产物）

路径由 `workspace` + `runs` + `run_id` + `用例名` 拼成：

```
/root/sglang_local_pipeline/runs/20260916-100000/qwen3-32b-gsm8k/
├── case.log           # 容器内 tee /output/case.log 写入
├── tmp/               # 容器内 /tmp 挂载
└── plog/              # 容器内 /root/ascend/log 挂载（NPU 底层日志）
```

拉回后远程节点的 `runs/` **不删除**，多次执行会按 run_id 各占一个子目录，累积保留；
本机节点（执行机=节点）在拷贝成功后自动清理 `runs/` 侧副本（见 6.3）。

### 6.2 执行机上的日志（拉回副本 + 实时回显）

路径由 `{workspace}/results` + `run_id` + `用例名` 拼成（不受执行目录影响），与节点的 `runs/` 分开。`run_id` 格式为 `{yaml_stem}-{timestamp}`（如 `example-20260916-100000`），目录名同时体现来源 yaml 与执行时间：

```
/root/sglang_local_pipeline/results/example-20260916-100000/
├── summary.json                       # run.py 写入的汇总
└── qwen3-32b-gsm8k/
    ├── case.log                       # ssh_run 实时回显 → fetch 覆盖为容器 tee 版本
    └── plog/                          # 从节点拉回的 NPU 底层日志
```

`tmp/`（容器内 /tmp：预置数据集、torch 编译缓存）体积大且排查价值低，**fetch 不回传**。远程节点的 `runs/` 原件始终保留，需要深度排查（如 JIT 编译问题）时可手动重拉。

多个用例时，每个用例各占一个子目录，互不干扰：

```
/root/sglang_local_pipeline/results/example-20260916-100000/
├── summary.json
├── qwen3-32b-gsm8k/
│   ├── case.log
│   └── plog/
└── qwen3-14b-gsm8k/
    ├── case.log
    └── plog/
```

run 结束时控制台最后一行打印绝对路径：`结果: /root/sglang_local_pipeline/results/example-20260916-100000`

### 6.3 执行机 = 节点时的路径关系

本例执行机就是节点，但两边使用**不同的目录名**（`results/` vs `runs/`），不会冲突：

```
/root/sglang_local_pipeline/
├── runs/                                    ← 节点（容器挂载写入）
│   └── example-20260916-100000/
│       └── qwen3-32b-gsm8k/
│           ├── case.log                     ← 容器 tee 写
│           ├── tmp/                          ← mkdir -p + 挂载
│           └── plog/                        ← mkdir -p + 挂载
│
└── results/                                 ← 执行机（run.py + ssh_run + fetch 写入）
    └── example-20260916-100000/
        ├── summary.json                     ← run.py 写
        └── qwen3-32b-gsm8k/
            ├── case.log                     ← ssh_run 回显 → fetch 覆盖为容器 tee 版本
            └── plog/                        ← fetch 从节点拉回（tmp/ 不回传）
```

两个目录完全独立，无并发写入同一文件的问题。且本机节点时 fetch 拷贝成功后会**自动删除**节点侧 `runs/{run_id}/{用例名}/`（避免与 `results/` 重复占磁盘），拷贝失败则保留原件；远程节点的 `runs/` 始终保留。

跨节点执行时同理：节点上留在 `runs/`，执行机上落在 `results/`，fetch 把节点 `runs/{id}/{suite}/` 内容拉回到执行机 `results/{id}/{suite}/`。

### 6.4 在任意路径执行 run.py

`run.py` 可在任意目录下用绝对路径调用，不受 cwd 限制：

```bash
cd /home
python3 /root/sglang_local_pipeline/src/run.py --config /root/sglang_local_pipeline/configs/example.yaml
```

结果目录固定为 `{workspace}/results`（如上例为 `/root/sglang_local_pipeline/results/20260916-100000/`），不随 cwd 变化，无需配置。

## 7. 多机（PD 分离）用例

支持一个用例在多个节点上协同执行，例如双机 PD 分离性能用例（prefill、decode 各占一个 16 卡节点拉起服务，router 拉起路由并执行基准测试）。

### 7.1 配置

suites 里用 `roles` 代替 `node`（两者互斥），三个角色各配节点 host（须已在 `nodes` 中定义）。`prefill`/`decode` 支持多节点（写列表），`router` 只能配单个节点（sglang 框架限制：多个 router 会各自起 router 进程造成冲突）：

```yaml
nodes:
  - host: 192.168.10.1            # A3 (16 卡, prefill + router 复用)
    arch: a3
  - host: 192.168.10.2            # A3 (16 卡, decode)
    arch: a3
  - host: 192.168.10.3            # A3 (16 卡, 第二个 prefill/decode)
    arch: a3

suites:
  # 1p1d: prefill/decode 各 1 节点 (字符串 = 单节点)
  - name: dsv4-flash-w8a8-1p1d-16p
    roles:
      prefill: 192.168.10.1       # 拉起 prefill 服务
      decode: 192.168.10.2        # 拉起 decode 服务
      router: 192.168.10.1        # 等 PD 就绪后拉起 router 并执行测试
    file: test/registered/npu/performance/deepseek_v4_flash/test_npu_deepseek_v4_flash_w8a8_1p1d_16p_in8k_out1k_50ms.py
    timeout_minutes: 240          # 角色容器超时（分钟）; prefill/decode 实际再加 2 分钟余量
                                   # （router 结束后它们还需一个轮询周期才收到退出信号）

  # 2p2d: prefill/decode 各 2 节点 (列表 = 多节点)
  - name: dsv4-flash-w8a8-2p2d-16p
    roles:
      prefill: [192.168.10.1, 192.168.10.2]
      decode: [192.168.10.3, 192.168.10.4]
      router: 192.168.10.1
    file: test/registered/npu/performance/deepseek_v4_flash/test_npu_deepseek_v4_flash_w8a8_2p2d_16p.py
    timeout_minutes: 240
```

- `prefill`/`decode` 的值可以是字符串（单节点）或列表（多节点），两种写法等价。
- `router` 的值只能是字符串（单节点），配列表会报错。
- 节点卡数在 `docker.devices: auto`（默认）时由 `arch` 决定（`a3`→16 卡、`a5`→8 卡），未知/缺失 arch 启动时报错；显式配 devices 列表时以列表为准。
- router 不占 NPU（仅转发与压测），通常复用 P/D 节点（host 网络下端口不冲突：PD 服务 8000、router 6677），也可配独立节点。
- 各角色的启动参数、环境变量、断言阈值完全由用例文件自身定义（与 CI 一致），流水线只负责编排。

### 7.2 工作原理

这类用例（`TestNpuPerfMultiNodePdSepTestCaseBase`）原本跑在 K8s 上：用 `HOSTNAME` 区分角色、`POD_IP` 标识地址、ConfigMap 做节点发现与结束通知。本地无 K8s，流水线做了等价替代，**不修改 sglang 代码**：

1. 执行机起一个轻量 HTTP 协调服务（固定端口 9377，模拟 ConfigMap 的读/写。端口被残留的流水线进程占用时自动清理后重试；被无关进程占用或清理失败则启动报错，报错信息附带手动 kill 命令。**不会回退随机端口**——环境只放行 9377，换端口会导致远程节点静默连不上）；
2. 每个角色容器启动前注入 `sitecustomize.py`（落在挂载的 /output，经 `PYTHONPATH` 生效），把用例用到的 kubernetes 客户端接口重定向到协调服务；
3. 流水线预置所有 pod 的注册信息 `sglang-prefill-0`/`sglang-prefill-1`/`sglang-decode-0`/... → 节点 IP（K8s 里由各 pod 自注册），PD 节点据此确定 master 地址和 `ASCEND_MF_STORE_URL`，router 据此收集 PD 地址列表；
4. 所有节点**并发** `docker run` 同一用例文件，以环境变量区分角色和序号：
   - `HOSTNAME=sglang-{role}-{idx}`：用例框架据此识别角色（含 role 名）和序号（末尾数字）；
   - `POD_IP=节点 IP`：服务绑定与互访地址（容器 host 网络）；
   - prefill/decode 拉起 PD 服务后轮询等待结束信号；router 等 PD 端口（8000）全部就绪后拉起 router，`/health` 就绪后执行基准测试；
5. router 结束（无论成败）后，流水线向协调服务写结束信号，所有 prefill/decode 收到后正常退出；任一 PD 服务提前崩溃时同样广播信号，避免其他节点空等超时；
6. 用例判定 = 全部节点退出码为 0（正常结束时 PD 角色不跑测试，收到结束信号后以 0 退出，基准结果与断言都在 router 的日志里）。

### 7.3 前置条件（多机用例额外要求）

| 项目 | 要求 |
|---|---|
| 节点间网络互通 | router 需访问 prefill/decode 的 8000 端口；PD 分离还用到 8995（bootstrap）、24666（MF store）等端口，节点间防火墙需放行 |
| 节点可达执行机 | 各节点容器需访问执行机的协调服务端口（固定 9377）。执行机在 NAT 后、节点无法回访时不支持 |
| 模型缓存 | prefill/decode 节点均需预置模型缓存（`~/.cache`，与单机用例一致） |
| 镜像 | router 所在节点镜像需含 `sglang_router`（与 CI 一致的镜像已含；缺失时 router 日志会报 ModuleNotFoundError） |

节点 `/root/.docker/config.json` 配了代理也不影响：流水线给所有容器注入 `no_proxy`（含全部节点 IP），协调服务与节点互访直连（见 3.4）。

### 7.4 产物

多机用例在每个节点的子目录下各有一份产物（基准测试结果看 `router-0/case.log`）。以 2p2d 为例：

```
results/{run_id}/
├── summary.json
└── dsv4-flash-w8a8-2p2d-16p/
    ├── prefill-0/
    │   ├── case.log
    │   └── plog/
    ├── prefill-1/
    │   ├── case.log
    │   └── plog/
    ├── decode-0/
    │   ├── case.log
    │   └── plog/
    ├── decode-1/
    │   ├── case.log
    │   └── plog/
    └── router-0/
        ├── case.log
        └── plog/
```

执行过程中控制台**只回显 router 的输出**（每行带 `[router-0] ` 前缀），对齐 CI 各 pod 日志隔离的观感；prefill/decode 日志量大且与 router 交错，仅实时写入各自子目录的 case.log 不回显（PD 角色异常结束时，控制台的状态行会指明其 case.log 路径）。基准结果与断言看 `router-0/case.log`，PD 服务问题看对应角色目录。混布 TP 用例（第 8 节）同理：只回显 master（node-0），worker 仅写文件。

## 8. 多机（混布 TP）用例

与 PD 分离（第 7 节）不同：多节点组成**一个** sglang server 实例（TP 跨节点），无 prefill/decode/router 角色。第一个节点 = master（启动 server + 跑测试），其余 = worker（只起 server）。

### 8.1 配置

suites 里用 `multinode` 代替 `node`/`roles`（三者互斥），值为节点 IP 列表（≥2 个，第一个是 master）：

```yaml
suites:
  - name: glm5_2-16p-gpqa
    multinode: [192.168.10.1, 192.168.10.2]   # 第一个 = master, 其余 = worker
    file: test/registered/npu/accuracy/glm5_2/test_npu_glm_5_2_w8a8_16p_gpqa.py
    timeout_minutes: 240          # 节点容器超时（分钟）; worker 实际再加 2 分钟余量
```

### 8.2 原理

协调机制与 PD 分离完全相同（CoordService + sitecustomize.py），区别仅在：

| | PD 分离 (第 7 节) | 混布 TP (本节) |
|---|---|---|
| 角色 | prefill / decode / router | master / worker（按序号） |
| HOSTNAME | `sglang-prefill-0`, `sglang-decode-0` | `sglang-node-0`, `sglang-node-1` |
| ConfigMap key | `sglang-prefill-0`, `sglang-decode-0` | `sglang-node-0`, `sglang-node-1` |
| 谁跑测试 | router | master（node-0） |
| 结束信号 | router 结束 → 广播 → PD 退出 | master 结束 → 广播 → worker 退出 |

用例的 `launch_pd_mix_node` 从 ConfigMap 查 `sglang-node-0` 的 IP，拼接 `--dist-init-addr={master_ip}:5000 --node-rank={pod_index}` 启动 sglang server。

### 8.3 前置条件

与第 7 节相同（节点间互通、可达执行机协调端口），但无 router 端口需求。所有节点均需预置模型缓存。

### 8.4 产物

与 PD 分离结构类似，按节点序号分子目录（基准测试结果看 `node-0/case.log`）：

```
results/{run_id}/
├── summary.json
└── glm5_2-16p-gpqa/
    ├── node-0/                # master (跑测试)
    │   ├── case.log
    │   └── plog/
    └── node-1/                # worker (只起 server)
        ├── case.log
        └── plog/
```

## 9. 常见问题

**Q: 改了个人 fork 的分支，节点上的旧仓库会冲突吗？**
不会。`prepare` 阶段会 `git remote set-url` 切到新 remote 再 fetch；分支用 `reset --hard origin/{ref}` 对齐，只影响当前 checkout 的分支，不影响其他本地分支。

**Q: 节点无法访问 GitHub / 镜像仓库怎么办？**
节点预配代理；或用离线模式：提前手动 `git clone` + `docker pull`，配置里 `prepare: false`——pipeline 不联网、不动代码，完全使用节点现状。
gsm8k 数据集默认从 GitHub 在线下载，离线节点可提前放到节点 `~/.cache/modelscope/hub/datasets/tmp/test.jsonl`，perf 套件的 ShareGPT 数据集放 `~/.cache/modelscope/hub/datasets/otavia/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json`——容器启动时会自动拷入 /tmp（缓存缺失则忽略，回退在线下载）。

**Q: 如何测某个特定 commit？**
`run.code.ref` 改为 commit SHA 即可，checkout 逻辑对分支/tag/SHA 通用。

**Q: `file` 能配 repo 外的自定义用例吗？**
能，但用例跑在容器里，文件必须在容器可见的挂载内。最简单的做法：把用例放到节点的 `~/.cache/` 下（随模型缓存挂载进容器），然后 `file` 配绝对路径，如 `/root/.cache/my_tests/test_foo.py`。其他节点路径（如 `/data/...`）容器内不可见，会报 `No such file or directory`。

**Q: 需要登录的私有镜像？**
在节点上提前 `docker login`，`prepare: true` 即可 pull。

**Q: 只想看会执行什么命令？**
用 `--dry-run`，输出节点上将要执行的完整 docker 命令，不消耗 NPU 资源。

**Q: A5 节点上跑单机用例，脚本里的 `--tp-size` 是按 A3 卡数配置的，怎么办？**
该节点配置 `arch: a5`。流水线会在其单机用例容器启动前注入包装器（`/output/run_case.py`），把传给 server 的 `--tp-size` 自动除以 2 再执行用例（case.log 里有 `[a5-适配] --tp-size 8 -> 4` 记录可核对），不修改 sglang 仓库代码；脚本里没配 `--tp-size` 或不经 server 启动的用例不受影响。多机用例（`roles`/`multinode`）不做此适配。

## 附录 A: 配置 SSH 免密

```bash
# 执行机上（已有 key 可跳过第一步）
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
ssh-copy-id root@192.168.10.1
ssh-copy-id root@192.168.10.2
```

不支持在配置文件中传密码（原生 ssh 不接受非交互传密码）。
