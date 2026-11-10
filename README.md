# sglang 本地测试流水线

在没有 CI / K8s 的 NPU 物理机上，批量执行 sglang 测试用例的轻量工具。

只需写一个 YAML 配置、跑一条命令，流水线自动完成：准备代码 → 准备节点 → 启动容器 → 执行用例 → 汇总结果。三种用例（单机 / 多机 PD 分离 / 多机混布，性能或精度均可）用的是**同一条命令**，区别只在 YAML 怎么写。

## 快速上手：五步跑通

### 第 1 步：一次性环境准备（只有下面几件事需要人工）

| 需要人工准备 | 说明 |
|---|---|
| SSH 免密 | 执行机免密登录每台节点（附录 A）；执行机就是节点时可跳过 |
| sglang 代码 | 放到执行机 `{workspace}/sglang`；或在 YAML 配 `code.git_remote` 让流水线自动 clone |
| 模型权重 + 镜像 | 权重提前放节点 `~/.cache`；镜像缺失会尝试自动 `docker pull`，离线节点需手动 pull |
| 出网手段（仅精度用例） | `run.env` 配代理，或按 4.3 ② 离线预置 evalscope + 配置 `run.datasets` |

其余全部自动：执行机只需 Python 3 + `pip install pyyaml`（唯一依赖），不需要 NPU/Docker；代码只放在执行机，各节点由流水线自动复制。

**流水线启动时会先做统一预检**：SSH 免密、docker 可用性、workspace 可写、数据集是否存在、代码仓是否就绪，一次性列出所有问题并附修复命令，全部通过才开始跑，不用逐个试错。

### 第 2 步：复制模板，改 5 处必填

```bash
cp configs/example_single.yaml configs/my.yaml   # 多机场景换 example_multinode_pd.yaml / example_multinode_mixed.yaml
```

模板里只需改这 5 处（都已标 `[必填]`）：`workspace`、`image`、节点 `host`、节点 `arch`、用例 `file`。其余项不写就用默认值。

模板分三段，看名字就知道填什么：

```yaml
run:       # 流水线怎么跑：工作目录、镜像
nodes:     # 用哪些机器：IP + 卡数（a3=16 / a5=8）
suites:    # 跑哪些用例：file 必填，超时/节点可省
```

模板只保留必填项和一行提示，保持精简。挂载、数据集、代理、`a5_env` 等可选项不再写进模板，按需对照[第 4 节](#sec-4)加到对应位置即可（YAML 里没写的字段一律用默认值）。

### 第 3 步：dry-run 核对（不消耗 NPU）

```bash
python3 src/run.py --config configs/my.yaml --dry-run
```

只打印将在节点上执行的 docker 命令，用于检查配置有没有写错。

### 第 4 步：正式执行

```bash
python3 src/run.py --config configs/my.yaml
```

- 长时间任务建议加 `-d` 后台执行（SSH 断链/关窗不影响）
- 只想跑某条用例：`--suite 用例名`（可传多次）
- 夜间定时：`--at "22:00:00"`

### 第 5 步：看结果

每跑完一条用例即更新 `{workspace}/results/{run_id}/summary.json`，控制台最后一行打印结果目录。退出码：`0`=全部通过 / `1`=有用例失败 / `2`=配置错误。

```
results/{run_id}/
├── summary.json          # 汇总（每条用例的通过/失败）
└── 用例名/
    ├── xxx.log           # 用例日志（先看这个）
    ├── ssh.log           # SSH 诊断
    └── plog/             # NPU 底层日志
```

到这里单机场景就已经能用了。三种场景的具体配置与执行细节见下。

## 三个核心概念

| 概念 | 一句话 |
|---|---|
| 执行机 | 跑 `run.py` 的机器，管编排/复制代码/汇总结果；不需要 NPU/Docker，可以就是某台节点 |
| 节点 | 真正跑测试的 NPU 机器（`arch: a3`=16 卡 / `a5`=8 卡）；只需 Docker + NPU 驱动 |
| 用例 | sglang 仓里的测试文件，分三种部署类型 × 性能/精度两种内容 |

## 场景导航

| 你要跑什么 | suites 里配 | 模板 | 章节 |
|---|---|---|---|
| 单机用例（一个节点跑一个用例，性能/精度均可） | `node` | `example_single.yaml` | [第 1 节](#sec-1) |
| 多机 PD 分离（prefill/decode/router 三角色） | `roles` | `example_multinode_pd.yaml` | [第 2 节](#sec-2) |
| 多机混布（多节点组成一个 server，跨节点并行，通常为 TP） | `multinode` | `example_multinode_mixed.yaml` | [第 3 节](#sec-3) |

精度用例不需要任何额外配置：`file` 指向精度用例文件即可，evalscope 框架自动准备（判定与分发机制见各节"跑精度用例"）。

## 目录

1. [单机用例（含单机精度）](#sec-1)
2. [多机（PD 分离）用例（含多机精度）](#sec-2)
3. [多机混布用例（含多机精度）](#sec-3)
4. [配置参考](#sec-4)
5. [命令行参数与退出码](#sec-5)
6. [执行流程](#sec-6)
7. [结果与日志](#sec-7)
8. [常见问题](#sec-8)
- [附录 A：配置 SSH 免密](#sec-appendix-a)

## <a id="sec-1"></a>1. 单机用例（含单机精度）

一个用例在一个节点上执行。这是最简单的场景，也是精度/性能用例最常见的跑法。

### 1.1 配置

单节点、仓已就位时的最小配置：

```yaml
run:
  workspace: /root/sglang_local_pipeline
  docker:
    image: <镜像地址>

nodes:
  - host: 192.168.10.1
    arch: a3            # a3=16 卡 / a5=8 卡

suites:
  - file: test/registered/npu/llm_models/test_npu_qwen3_32b.py
    timeout_minutes: 120
    # name 可省略（默认取 file 名）；单节点时 node 也可省略（默认用该节点）
```

- 全部可选字段（数据集、环境变量、挂载、依赖等）见[第 4 节](#sec-4)。
- 配置了多个节点时，不指定 `node` 的单机用例会**按负载均分到各节点并行执行**（指定 `node` 的固定不动），见 4.2。

### 1.2 执行

```bash
python3 src/run.py --config configs/my.yaml --dry-run    # 建议先核对
python3 src/run.py --config configs/my.yaml               # 正式执行
python3 src/run.py --config configs/my.yaml -d           # 后台执行（断链不影响）
python3 src/run.py --config configs/my.yaml --suite test_npu_qwen3_32b   # 只跑一条
```

执行行为：

- 同节点全部单机用例共用一个**长驻共享容器**（`sgl-pipeline-single`）：首个单机用例时启动容器并做一次初始化（装 `pip_deps.txt` 依赖，可能几分钟到几十分钟，进度看节点 `runs/{run_id}/.init.log`），之后逐条 `docker exec` 执行，run 结束统一删除。
- 多节点时未指定 `node` 的用例自动均分，节点间并行、同节点内串行（卡只有一套）。
- 每条用例执行前后自动清理容器内残留的 sglang 进程，防止占卡。

### 1.3 结果与判定

- 判定：用例脚本自身的断言/退出码，0 即通过；结果在 `summary.json` 与用例日志 `{脚本名}.log`。
- 用例失败时先看 `results/{run_id}/{用例名}/{脚本名}.log`，再深入 `plog/`（NPU 底层日志）。
- 节点侧 `runs/` 保留原始产物供排查，见[第 7 节](#sec-7)。

### 1.4 跑单机精度用例

配置与性能用例完全一样，只是 `file` 指向精度用例（多在 `test/registered/npu/accuracy/` 下）：

```yaml
suites:
  - name: dsv4-flash-8p-gpqa
    node: 192.168.10.1
    file: test/registered/npu/accuracy/deepseek_v4_flash/test_npu_deepseek_v4_flash_w8a8_8p_gpqa.py
    timeout_minutes: 120
```

不需要任何额外配置：

- **evalscope 自动准备**：流水线读用例文件内容，含 `import test_npu_accuracy_utils`（或调用 `run_evalscope`）即判定为精度用例，自动 clone evalscope 到 `{workspace}/evalscope` 并分发到该节点，容器内本地安装。纯性能用例不触发，用例文件读不到时保守按精度处理。想用自定义版本：直接把源码放进执行机 `{workspace}/evalscope`（非空则跳过 clone）。
- **数据集**：gsm8k/gpqa 等默认在线下载；节点离线时提前下载后配 `run.datasets` 指向**绝对路径**（只需执行节点上有该文件，机制见 4.3 ①）。
- **结果**：精度分数与阈值断言在该用例的 `{脚本名}.log` 末尾，`summary.json` 记录断言结果。

执行命令同 1.2，无需任何变化。

## <a id="sec-2"></a>2. 多机（PD 分离）用例（含多机精度）

一个用例在多个节点协同执行：prefill/decode 节点各拉起服务，router 拉起路由并执行基准测试。这类用例原本跑在 K8s 上，流水线在本地做了等价替代（执行机起协调服务 + 向容器注入桥接脚本），**不修改 sglang 代码**。

### 2.1 配置

suites 里用 `roles` 代替 `node`（两者互斥），三个角色各配节点 host（须已在 `nodes` 中定义）：

```yaml
nodes:
  - host: 192.168.10.1
    arch: a3
  - host: 192.168.10.2
    arch: a3

suites:
  - name: dsv4-flash-w8a8-1p1d-16p
    roles:
      prefill: 192.168.10.1       # 拉起 prefill 服务
      decode: 192.168.10.2        # 拉起 decode 服务
      router: 192.168.10.1        # 等 PD 就绪后拉起 router 并执行测试
    file: test/registered/npu/performance/deepseek_v4_flash/test_npu_deepseek_v4_flash_w8a8_1p1d_16p_in8k_out1k_50ms.py
    timeout_minutes: 240          # 角色容器超时；prefill/decode 实际再加 2 分钟余量
```

- `prefill`/`decode` 支持多节点（写列表，如 `[192.168.10.1, 192.168.10.2]`，即 2p2d），`router` 只能单节点（框架限制：多个 router 会冲突）。
- router 不占 NPU（容器仍挂卡，仅转发与压测），通常复用 P/D 节点（host 网络端口不冲突），也可配独立节点。
- 各角色的启动参数、环境变量、断言阈值由用例文件自身定义（与 CI 一致），流水线只编排。

### 2.2 执行

命令与单机**完全相同**：

```bash
python3 src/run.py --config configs/my.yaml --dry-run
python3 src/run.py --config configs/my.yaml
```

流水线额外自动做的事：

- 执行机起一个轻量 HTTP 协调服务（固定 9377 端口，模拟 K8s ConfigMap）；
- **router 容器先启动**，等它写好协调信息后再启动 prefill/decode 容器（对齐 CI 启动时序）；
- router 结束（无论成败）后广播结束信号，PD 容器收到后退出，超时未退的强制删除（等价 CI 删 job）；
- 控制台**只回显 router 的输出**（`[router-0]` 前缀）；prefill/decode 日志量大，实时写进各自子目录文件，不刷屏。

### 2.3 判定与结果

- 判定：**只看 router 的退出码**，0 即通过（与 CI 一致——CI 也只看 router pod）。PD 容器被结束后信号连带退出不算失败；PD 自身崩溃才判失败。
- 基准结果与断言都在 `results/{run_id}/{用例名}/router-0/{脚本名}.log`。
- 产物：每个角色一个子目录，结构同单机：

```
results/{run_id}/{用例名}/
├── prefill-0/          ├── {脚本名}.log + ssh.log + plog/
├── decode-0/           ├── 同上
└── router-0/           └── 同上（基准结果看这里）
```

### 2.4 跑多机精度（PD 分离）

配置同 2.1，`file` 换成精度用例即可：

```yaml
suites:
  - name: xxx-16p-gpqa
    roles:
      prefill: 192.168.10.1
      decode: 192.168.10.2
      router: 192.168.10.1
    file: test/registered/npu/accuracy/<你的精度用例>.py   # 换成实际精度用例
    timeout_minutes: 240
```

- **evalscope 只分发到 router 节点**（跑测试的节点）；prefill/decode 只起 server 不读框架，无需分发。
- **数据集同样只需 router 节点有**（PD 节点路径不存在时静默跳过）。
- 精度分数与断言看 `router-0/{脚本名}.log`，判定仍只看 router。
- 执行命令与 2.2 完全相同。

### 2.5 前置条件（多机额外要求）

| 项目 | 要求 |
|---|---|
| 节点间网络互通 | router 访问 PD 的 8000 端口；另用到 8995、24666 端口，防火墙需放行 |
| 节点可达执行机 | 各节点容器要访问执行机协调服务（固定 9377 端口）；执行机在 NAT 后不支持 |
| 模型缓存 | prefill/decode 节点均需预置 `~/.cache` |
| 镜像 | router 节点镜像需含 `sglang_router`（与 CI 一致的镜像已含，缺失时 router 日志报 ModuleNotFoundError） |

## <a id="sec-3"></a>3. 多机混布用例（含多机精度）

与 PD 分离不同：多个节点组成**一个** sglang server 实例（跨节点并行，最常见是 TP），无 prefill/decode/router 角色之分。第一个节点 = **master**（起 server + 跑测试），其余 = **worker**（只起 server）。具体并行方式（TP / DP attention / EP 等）由用例文件自己决定，流水线不感知。

### 3.1 配置

suites 里用 `multinode`（与 `node`/`roles` 互斥），值为节点列表（≥2 个，第一个是 master）：

```yaml
suites:
  - name: glm5_2-16p-gpqa
    multinode: [192.168.10.1, 192.168.10.2]   # 第一个 = master，其余 = worker
    file: test/registered/npu/accuracy/glm5_2/test_npu_glm_5_2_w8a8_16p_gpqa.py
    timeout_minutes: 240          # worker 实际再加 2 分钟余量
```

（上面这个例子本身就是精度用例——多机混布常用于 16p/32p 大卡数精度验证。）

### 3.2 执行

命令与单机**完全相同**。流水线自动做的事与 PD 分离同一套机制（协调服务 + 桥接脚本），区别仅在编排：

- 全部节点容器**同时启动**（无角色顺序），用例按 `HOSTNAME=sglang-node-{序号}` 区分 master/worker；
- master 的用例通过协调服务拿到 master IP，拼 `--dist-init-addr` 启动跨节点 TP server；
- master 结束后广播结束信号；worker 不轮询信号（保活等待），由流水线在宽限期后强制清理；
- 控制台**只回显 master（node-0）的输出**，worker 日志只写文件。

### 3.3 判定与结果

- 判定：**只看 master（node-0）的退出码**，0 即通过；worker 被连带清理不算失败。
- 测试结果与断言在 `results/{run_id}/{用例名}/node-0/{脚本名}.log`。
- 产物按节点序号分子目录：

```
results/{run_id}/{用例名}/
├── node-0/    # master：测试结果看这里
└── node-1/    # worker：只起 server
```

### 3.4 跑多机精度（混布）

配置同 3.1——`multinode` + 精度用例 `file`（见 3.1 示例，就是标准写法）。机制上的差异：

- **evalscope 只分发到 master（node-0）**；worker 不需要。
- **数据集只需 master 节点有**。
- 精度分数与断言看 `node-0/{脚本名}.log`。
- 执行命令与单机完全相同。

### 3.5 前置条件

与 PD 分离相同（节点间互通、可达执行机 9377 协调端口），但无 router 端口需求；**所有节点**均需预置模型缓存。

### PD 分离 vs 多机混布速查

| | PD 分离（第 2 节） | 多机混布（第 3 节） |
|---|---|---|
| roles/multinode | `roles: prefill/decode/router` | `multinode: [ip1, ip2, ...]` |
| 谁跑测试 | router | master（node-0，列表第一个） |
| evalscope / 数据集分给谁 | router 节点 | master 节点 |
| 判定 | router rc==0 即通过 | master rc==0 即通过 |
| 控制台回显 | `[router-0]` | `[node-0]` |
| 典型用途 | PD 分离性能基准 | 跨节点并行（常为 TP）的性能/精度 |

## <a id="sec-4"></a>4. 配置参考

按场景复制对应模板修改即可，模板中必填项裸露、可选项已注释（取消注释即用）。YAML 里不写的项自动取默认值。

### 4.1 参数表

**run 段**

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `run.workspace` | 无 | 是 | 节点上的工作目录。sglang 仓固定在 `{workspace}/sglang`，结果固定在 `{workspace}/results`，均不可另配 |
| `run.docker.image` | 无 | 是 | 测试容器镜像 |
| `run.code.git_remote` | 无 | 否 | 执行机仓**缺失时**自动 `git clone` 用；仓已存在则忽略此字段 |
| `run.code.ref` | 无 | 否 | clone 的分支/tag/commit（`git clone -b`）；仓已存在不生效（版本自己 checkout） |
| `run.docker.devices` | `auto` | 否 | `auto`=按 arch 挂卡（a3=16、a5=8）；或显式卡号列表如 `[0,1,2,3]` |
| `run.docker.net` | `host` | 否 | 容器网络模式 |
| `run.docker.shm_size` | `16g` | 否 | 共享内存大小（固定附加 `--privileged --ipc=host`） |
| `run.docker.extra_mounts` | `[]` | 否 | 额外 `-v` 挂载，格式同 docker -v。节点上路径不存在时自动跳过并 `[WARN]`（避免 docker 静默挂成空目录难排查） |
| `run.env` | 内置 7 项（见 4.4） | 否 | 注入容器的环境变量，按 key 覆盖默认值，可追加新键 |
| `run.a5_env` | `{}` | 否 | 仅注入 a5 节点单机用例容器（如灵衢互联 `ASCEND_USE_FIA: "1"`），覆盖 `run.env` 同名键 |
| `run.datasets` | `[]` | 否 | 预置数据集，见 4.3 ① |
| `run.init_timeout_minutes` | `120` | 否 | 单机共享容器初始化（含 pip 依赖安装）的最长等待，慢代理冷缓存可调大 |

**nodes 段**

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `nodes[].host` | 无 | 是 | 节点 IP。指向执行机本身时自动本地执行（不走 SSH） |
| `nodes[].arch` | 无 | 是 | `a3`=16 卡 / `a5`=8 卡。决定挂卡数及 A5 单机用例 `--tp-size` 16→8 自动适配 |
| `nodes[].user` | `root` | 否 | SSH 用户名 |
| `nodes[].port` | `22` | 否 | SSH 端口 |

**suites 段**

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `suites[].file` | 无 | 是 | 用例文件路径，相对 repo；绝对路径须在容器可见挂载内（repo 或 `~/.cache`） |
| `suites[].name` | file 去扩展名 | 否 | 用例名称（`--suite` 过滤用） |
| `suites[].node` | 自动分配 | 三选一 | 单机用例：执行节点。单节点配置可省略；多节点时省略则按负载均分并行执行 |
| `suites[].roles` | 无 | 三选一 | 多机 PD 分离用例（第 2 节） |
| `suites[].multinode` | 无 | 三选一 | 多机混布用例（第 3 节） |
| `suites[].timeout_minutes` | 60 | 否 | 用例超时（分钟），防止进程 hang 卡死整个 run |

`node` / `roles` / `multinode` 三者互斥，只能配一个。

### 4.2 多节点自动均分

多节点配置下，不指定 `node` 的单机用例自动均分（各节点用例数差 ≤1），指定的固定不动：

```yaml
nodes:
  - host: 192.168.10.1
    arch: a3
  - host: 192.168.10.2
    arch: a5

suites:
  - file: test/.../test_a.py            # 自动均分
  - file: test/.../test_b.py            # 自动均分
  - file: test/.../test_c.py
    node: 192.168.10.2                 # 固定在 10.2
```

并行规则：连续的单机用例按节点分组并行（同节点内串行）；多机用例独占涉及的全部节点单独串行，不与单机段并行抢卡；`--dry-run` 时保持串行。

### 4.3 三个零配置机制

**① datasets（预置数据集）**：数据集提前下载到节点后，把**绝对路径**列在 `run.datasets`，容器启动后自动软链到 `/tmp`（软链失败回退复制）。路径不存在时忽略、用例回退在线下载。注意：不要配 `/` 根目录直属的文件（会把整个根盘挂进容器）。

```yaml
run:
  datasets:
    - /data/datasets/ShareGPT_V3_unfiltered_cleaned_split.json
```

哪个节点需要有该文件：单机=执行节点，PD 分离=router，混布=master（见各场景"跑精度用例"）。

**② evalscope（精度框架）**：详见第 1/2/3 节的"跑精度用例"。要点：按用例文件内容自动判定（含 `test_npu_accuracy_utils` / `run_evalscope`）；源码固定 `{workspace}/evalscope`，自动 clone，非空则用你放的版本；只分发到跑测试的节点（单机=node / PD=router / 混布=master）；失败回退清华镜像在线安装（需外网）。

**出网检查（必读）**：配置了精度用例后，要么配代理，要么离线准备齐全（**两项都要**：预置 `{workspace}/evalscope` 源码 + 配 `run.datasets` 数据集），否则流水线会在节点准备**之前**直接报配置错误（退出码 `2`），并逐项打印“已就绪/缺失”——避免新手等到 clone 超时或数据集下载失败才发现。纯性能用例不受影响。

**精度用例前置准备清单**（除以下 5 项外无需额外配置）：

| 项目 | 要求 | 不满足时 |
|---|---|---|
| 出网手段 | 二选一：① `run.env` 配 `http_proxy`/`https_proxy`（推荐）；② 离线预置——evalscope 源码放执行机 `{workspace}/evalscope` **且** 配 `run.datasets` | 缺任一项都在 prepare 阶段报配置错误（退出码 `2`），并列出缺哪项 |
| 执行机可达 GitHub | 首次自动 `git clone --depth 1` evalscope 到 `{workspace}/evalscope`；代理取 `run.env`（执行机 shell 里的代理变量不生效） | 用上面的离线方式：源码放进该目录（非空即跳过 clone） |
| 节点模型缓存 | 与性能用例一致，模型权重需在 `~/.cache` | 运行时在线下载，慢且易失败 |
| 数据集 | gsm8k/gpqa 默认在线下载；离线节点提前下载后配 `run.datasets` 绝对路径 | 下载失败导致用例失败 |
| 依赖 | 单机精度用例由 `pip_deps.txt` 在共享容器初始化时安装（含 sgl_eval、lmms-eval 等） | 镜像已含这些依赖可清空该文件；否则用例 import 报错 |

**多机精度特别注意**：多机用例容器**不执行** `pip_deps.txt`（见 4.3 ③），精度依赖必须**提前打进镜像**；evalscope 源码与数据集只需 router（PD 分离）/ master（多机混布）节点就位。

**③ pip_deps.txt（单机用例依赖）**：同节点单机用例共用的共享容器启动时，逐条执行 `configs/pip_deps.txt` 里的安装命令（已预置 CI 的依赖列表，CI 更新后照抄过来即可）。

- 每行一条命令按序执行，`#` 注释/空行忽略
- 单条失败只记 `[WARN]` 跳过，不中断初始化
- 逐条进度在节点 `runs/{run_id}/.init.log`，控制台只打心跳和最后的汇总行
- 清空或删除该文件 = 不装任何依赖
- pip 慢时可配 `run.env` 的 `PIP_INDEX_URL` / `http_proxy`，`pip_deps.txt` 无需改动
- 多机用例容器不执行此文件（依赖需打进镜像）

### 4.4 run.env 内置默认值

不写 `run.env` 时容器自动注入以下 7 项（与 CI 一致）：

| 环境变量 | 默认值 |
|---|---|
| `SGLANG_USE_MODELSCOPE` | `true` |
| `HF_ENDPOINT` | `https://hf-mirror.com` |
| `SGLANG_IS_IN_CI` | `true` |
| `SGLANG_TEST_MAX_RETRY` | `0` |
| `TORCH_EXTENSIONS_DIR` | `/tmp/torch_extensions` |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` |
| `STREAMS_PER_DEVICE` | `32` |

流水线还会自动注入（一般无需关心）：`TZ=Asia/Shanghai`、`no_proxy`（节点间直连不受代理影响）、`PYTHONPATH=/output`（多机协调桥接 + evalscope SSL 规避）；并临时关闭容器内各 python（PATH 上的 `python3` 及 `/usr/local/python*/bin/python3`，含 evalscope 实际使用的解释器）requests 的 SSL 校验（规避企业代理自签证书报 `CERTIFICATE_VERIFY_FAILED`，日志里每个解释器一行 `[requests-patch] OK` 即生效，问题消失后可移除）。

### 4.5 容器内预执行命令（configs/pre_commands.txt）

需要在用例启动前临时改动容器（替换 sglang 文件、打补丁、清理现场等）时，直接编辑流水线里的 `configs/pre_commands.txt`。固定路径、零配置：文件存在且包含有效命令即生效，全为注释/空行或删除文件 = 不执行，无需改 YAML。

```bash
# configs/pre_commands.txt
cp $SGLANG_PIPELINE_ASSETS/patch/decode.py /sgl-workspace/sglang/python/sglang/srt/disaggregation/decode.py
cp $SGLANG_PIPELINE_ASSETS/patch/scheduler.py /sgl-workspace/sglang/python/sglang/srt/managers/scheduler.py
```

命令要用的文件放在流水线已有的 `assets/patch/` 目录（已预建，与 `assets/numactl` 等内置资产分开，不用新增顶层目录）。该目录内除 `.gitignore` 外的文件不入库，可直接放本地补丁文件。流水线会自动分发到每个节点，容器内用 `$SGLANG_PIPELINE_ASSETS/patch/` 引用（即 `/tmp/sglang_pipeline_assets/patch`），**文件只需在执行机保留一份**。

- 命令在**容器内**执行：单机用例在该节点共享容器初始化时执行一次（该节点全部单机用例复用）；多机用例每个角色/节点容器启动用例前各执行一次
- 文件内容原样作为 shell 命令嵌入执行，支持注释和多行语法；任一条返回非 0 即中止：单机=该节点共享容器初始化失败（该节点单机用例全部跳过），多机=该角色用例失败。单机日志在 `runs/{run_id}/.init.log.{host}`，多机在对应角色容器输出里；均有 `[pre_commands] 执行 configs/pre_commands.txt` 标记
- 若文件放在其他路径且各节点都有（如共享 NFS），也可以继续用 `extra_mounts` 挂载：

```yaml
run:
  docker:
    extra_mounts:
      - "/data/patch:/data/patch"
```

- 改 sglang 代码前先确认用例实际加载的路径：`python3 -c "import sglang; print(sglang.__file__)"`。流水线挂载的仓在 `{workspace}/sglang`，镜像自带的代码常在 `/sgl-workspace/sglang`，两者不是同一份

### 4.6 完整配置示例（仓缺失时由流水线 clone）

```yaml
run:
  workspace: /root/sglang_local_pipeline
  code:                                        # 仓已存在时整段可删
    git_remote: https://github.com/sgl-project/sglang.git
    ref: main                                  # 可省
  docker:
    image: <镜像地址>

nodes:
  - host: 192.168.10.1
    arch: a3
  - host: 192.168.10.2
    arch: a5            # 8 卡；单机用例 --tp-size 自动 16→8

suites:
  - node: 192.168.10.1
    file: test/registered/npu/llm_models/test_npu_qwen3_32b.py
    timeout_minutes: 120
  # 多机用例用 roles / multinode 代替 node，见第 2/3 节
```

## <a id="sec-5"></a>5. 命令行参数与退出码

| 参数 | 说明 |
|---|---|
| `--config` / `-c` | 配置文件路径（必填） |
| `--suite NAME` | 只执行指定用例，可多次传，按 name 匹配 |
| `--dry-run` | 只打印命令不执行 |
| `--at TIME` | 定时执行：阻塞到指定时间再开始。支持 `YYYY-MM-DD HH:MM:SS` 或 `HH:MM:SS`（今天已过取明天）。按**执行机本地时间**算 |
| `--detach` / `-d` | 后台执行：脱离终端，输出写 `{workspace}/results/{run_id}/console.log`（`tail -f` 跟踪）。停止须用 `kill -INT PID`（裸 `kill` 不走清理，容器会残留占卡） |

退出码：`0`=全部通过（或 dry-run / 成功转后台）/ `1`=有用例失败 / `2`=配置错误（解析/校验失败、引用的节点未定义、无匹配用例等）。

## <a id="sec-6"></a>6. 执行流程

`run.py` 启动后依次做 5 件事（三种场景通用）：

1. **预检**（只读）：SSH 免密、docker 可用、workspace 可写、`run.datasets` 文件存在、代码仓就绪——所有问题一次性列出并给修复命令，有问题立即退出（退出码 `2`），不浪费时间，也不残留空的 `results/{run_id}/` 目录（后台模式的 `console.log` 例外，会保留目录）。
2. **准备代码**（执行机）：`{workspace}/sglang` 有代码就直接用（不动 git）；没有且配了 `git_remote` 就自动 clone；没有又没配则报错。
3. **准备节点**（每个用到的节点一次）：缺镜像尝试 `docker pull`（失败不阻断）；远程节点清理旧仓后从执行机整仓复制代码（含 `.git`，保证多节点版本一致）；杀残留 sglang 进程、删旧容器，确保 NPU 卡干净。配了精度用例时**在此阶段**（任何用例执行之前）扫全部用例判定精度用例，一次 clone evalscope 并分发到跑测试的节点；容器内的 `pip install -e` 属于各容器初始化，不在本阶段。
4. **执行用例**：单机用例共用共享容器逐条 `docker exec`（见 1.2）；多机用例每角色一个容器编排执行（见 2.2 / 3.2）；每完成一条即重写 `summary.json`（中途被杀也能看到已完成结果）。
5. **拉回结果**：节点 `runs/` 里的用例日志/plog 拉回执行机 `results/`（tmp 等大文件不回传，节点原件保留）。

## <a id="sec-7"></a>7. 结果与日志

**先看哪里**：`results/{run_id}/summary.json`（汇总）→ 失败用例目录里的 `{脚本名}.log`（用例日志）→ `plog/`（NPU 底层日志）。多机用例再看对应角色/节点子目录（PD 看 `router-0`，混布看 `node-0`）。

`run_id` 格式为 `{yaml文件名}-{时间戳}`，如 `my-20260916-100000`。`--detach` 时完整控制台输出在 `results/{run_id}/console.log`。

执行机和节点用**不同目录**，即使执行机就是节点也不冲突：

```
{workspace}/
├── runs/       ← 节点侧原始产物（容器挂载写入，始终保留）
│   └── {run_id}/
│       ├── .init.log       # 共享容器初始化日志（排查依赖安装看这个）
│       ├── tmp/  plog/     # 共享目录（不回传）
│       └── 用例名/
│           └── xxx.log
└── results/    ← 执行机侧结果目录（fetch 拉回 + 汇总）
    └── {run_id}/
        ├── summary.json
        └── 用例名/
            ├── xxx.log     # 实时回显 → fetch 覆盖为容器内落盘版本
            ├── ssh.log
            └── plog/
```

- 多机用例在用例名下再按角色/节点序号分子目录（`prefill-0/`、`router-0/`、`node-0/` 等），每个子目录结构相同。
- 节点侧 `runs/` **不会自动删除**（保留作排查现场），磁盘紧张时手动 `rm -rf` 旧 run 目录。
- fetch 不回传：`tmp/`（体积大）、共享 `plog/`（用例目录内已有隔离快照）、流水线注入的固定脚本、`.init.log`（直接看节点上的文件）。
- `run.py` 可在任意目录用绝对路径调用，结果目录固定在 `{workspace}/results`，不随 cwd 变。

## <a id="sec-8"></a>8. 常见问题

**Q: 只想看会执行什么命令？**
`--dry-run`，输出完整 docker 命令，不消耗 NPU。

**Q: 节点无法访问 GitHub？**
代码不受影响——git 只在执行机做，节点代码由流水线复制过去。镜像需节点可达 registry，离线节点提前手动 `docker pull`。数据集提前下载后配 `run.datasets`（绝对路径）。

**Q: 如何测某个特定 commit / 换个人 fork？**
仓已存在时流水线不动 git：自己 `git checkout <commit>` / `git remote set-url` + fetch 即可，换 fork 也一样。想让它 clone 指定版本：`run.code.ref` 填分支/tag/commit。

**Q: `file` 能配 repo 外的自定义用例吗？**
能，但文件必须在容器可见的挂载内。最简单：放到节点 `~/.cache/` 下，`file` 配绝对路径（如 `/root/.cache/my_tests/test_foo.py`）。其他路径容器内不可见。

**Q: 需要登录的私有镜像？**
节点上提前 `docker login`，缺镜像时流水线自动 pull。

**Q: A5 节点跑单机用例，脚本里 `--tp-size` 按 A3 写的怎么办？**
节点配 `arch: a5` 即可：流水线自动把 `--tp-size` 值为 16 的减半成 8 再执行（日志有 `[a5-适配] --tp-size 16 -> 8` 可核对），不改 sglang 代码。其他值不受影响；多机用例不做此适配。

**Q: A5 要开灵衢互联（FIA）？**
```yaml
run:
  a5_env:
    ASCEND_USE_FIA: "1"
```

**Q: 为什么 `docker ps` 只看到一个 `sgl-pipeline-single` 容器？**
正常：同节点全部单机用例共用它（初始化+装依赖只做一次），每条用例 `docker exec` 进去跑，run 结束统一删除。等初始化时控制台每分钟打一次心跳，完整过程看节点 `runs/{run_id}/.init.log`。

**Q: 能同时跑两个 run.py 吗？**
不能。多机用例的协调服务固定 9377 端口，共享容器固定名 `sgl-pipeline-single`，两个 run 会互相清理对方。请串行执行（或用 `--at` 错开）。

**Q: 前台执行时 SSH 断链/关窗会怎样？**
自动清理节点容器再退出（不残留占卡），但任务中断。要断链后继续跑的用 `-d` 后台模式。

**Q: 容器里 requests 报 `CERTIFICATE_VERIFY_FAILED`？**
已内置规避（见 4.4"自动注入"）。核对：
```bash
docker exec sgl-pipeline-single python3 -c "import requests; print(requests.Session().verify)"   # 应为 False
```

**Q: 后台任务怎么停？**
`kill -INT PID`（同 Ctrl+C，会清理容器）。裸 `kill` 发 SIGTERM 不走清理，容器会残留占卡。

**Q: 怎么知道我的用例是不是"精度用例"（会不会准备 evalscope）？**
看用例文件内容：import 了 `test_npu_accuracy_utils` 或调用了 `run_evalscope` 就是。执行时看 prepare 阶段日志——有 `[prepare] 执行机准备 evalscope 源码` 行即已按精度用例处理；显示"未配置精度用例，跳过"则不会装 evalscope。

## <a id="sec-appendix-a"></a>附录 A：配置 SSH 免密

```bash
# 执行机上（已有 key 可跳过第一步）
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
ssh-copy-id root@192.168.10.1
ssh-copy-id root@192.168.10.2
```

不支持在配置文件中传密码（原生 ssh 不接受非交互传密码）。
