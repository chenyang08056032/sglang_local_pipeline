# sglang 本地测试流水线使用指导

在没有 CI / k8s 的物理服务器（NPU 环境）上批量执行 sglang 测试用例的轻量工具。复用 CI 的 ascend 工具覆盖逻辑，只自研本地必需的节点编排 + docker run 两个环节。

## 30 秒上手

```bash
# 1. 按场景复制模板 (configs/ 下三选一, 见下方"场景选型")
cp configs/example_single.yaml configs/my.yaml

# 2. 改必填项: 模板内已标 [必填], 共 5 处 (workspace、镜像、节点 host+arch、用例 file);
#    其余项不写即取默认值。sglang 仓放到 {workspace}/sglang 即可 (git clone 或整目录拷贝均可)
#    模板里的 extra_mounts/datasets 是示例值 (已注释), 需要时取消注释改成自己的路径, 不配也不影响基础流程

# 3. 先 dry-run 核对将执行的命令 (不消耗 NPU), 再正式执行
python3 src/run.py --config configs/my.yaml --dry-run
python3 src/run.py --config configs/my.yaml
```

- 结果：每条用例完成即更新 `{workspace}/results/{run_id}/summary.json`，控制台最后一行打印结果目录
- 退出码：`0`=全部通过 / `1`=有用例失败 / `2`=配置错误

## 三个概念 + 场景选型

| 概念 | 一句话 |
|---|---|
| 执行机 | 跑 `run.py` 的机器，管编排/复制代码/汇总结果；不需要 NPU/Docker，可以就是某台节点 |
| 节点 | 真正跑测试的 NPU 机器（`arch: a3`=16 卡 / `a5`=8 卡）；只需 Docker + NPU 驱动 |
| 用例 | sglang 仓里的测试文件，共三种类型 ↓ |

| 用例类型 | suites 里配 | 模板 | 详见 |
|---|---|---|---|
| 单机（一个节点跑一个用例） | `node` | `example_single.yaml` | 第 3.3 节 |
| 多机 PD 分离（prefill/decode/router） | `roles` | `example_pd.yaml` | 第 7 节 |
| 多机混布 TP（跨节点 TP） | `multinode` | `example_tp.yaml` | 第 8 节 |

## 目录

1. [目录结构](#sec-1)
2. [环境准备](#sec-2)
3. [配置文件说明](#sec-3)
4. [快速开始](#sec-4)（命令行参数、退出码）
5. [执行流程](#sec-5)
6. [结果产物与日志路径](#sec-6)
7. [多机（PD 分离）用例](#sec-7)
8. [多机（混布 TP）用例](#sec-8)
9. [常见问题](#sec-9)
- [附录 A: 配置 SSH 免密](#sec-appendix-a)

## <a id="sec-1"></a>1. 目录结构

```
sglang_local_pipeline/
├── src/
│   ├── run.py          # 入口: 加载配置、节点准备、编排执行、汇总结果
│   └── pipeline.py     # 核心: 节点执行 (自动检测本地/SSH)、节点准备、docker 命令构造、日志拉回
├── configs/
│   ├── example_single.yaml   # 单机用例配置模板
│   ├── example_pd.yaml       # 多机 PD 分离用例配置模板
│   ├── example_tp.yaml       # 多机混布 TP 用例配置模板
│   └── pip_deps.txt          # 单机共享容器依赖安装命令 (固定默认文件, 存在即生效)
├── README.md
└── .gitignore
```

## <a id="sec-2"></a>2. 环境准备

### 执行机（跑 `python3 src/run.py` 的机器）

| 项目 | 要求 |
|---|---|
| sglang_local_pipeline 代码 | clone 或拷贝本目录 |
| Python 3 | 加 `pip install pyyaml`（唯一第三方依赖） |
| ssh 客户端 + tar | 仅远程节点需要（本地节点直接 subprocess 执行，无需 ssh） |
| sglang 代码仓 | 放在 `{workspace}/sglang`（**唯一代码准备点**，各节点代码均由此复制）。非空则直接用（不动 git）；缺失且配了 `run.code.git_remote` 时自动 clone（详见 3.1） |
| 网络 | 远程节点需可达 22 端口；节点缺镜像时会尝试 `docker pull`（需可达 docker registry，失败不阻断）；执行机 clone 代码需可达 git remote（仓缺失时） |
| 磁盘 | workspace（含 sglang 仓 + results） |

执行机**不需要** Docker、NPU 驱动——这些在节点上。
执行机也可以就是节点本身（如 A3 上直接跑）：pipeline 自动检测节点 host 是否指向本机，是则直接本地执行（不走 SSH、无需免密配置、无需 sshd），否则走 SSH。

### 节点（A3 / A5 等目标机器）

| 项目 | 要求 |
|---|---|
| SSH 免密 | 仅远程节点需要（本地节点=执行机时自动走 subprocess，无需 SSH） |
| Docker | 已安装并运行 |
| NPU 驱动 | 存在 `/usr/local/Ascend/driver`、`/usr/local/Ascend/firmware`、`/dev/davinci*`、`/dev/davinci_manager`、`/dev/hisi_hdc`、`/etc/ascend_install.info` |
| 网络（可选） | 节点缺镜像时会尝试 `docker pull`（需可达 docker registry，失败不阻断，离线节点需提前手动 `docker pull`）；**代码无需外网/git**——各节点代码由流水线从执行机整仓复制（先清理节点旧仓），保证多节点版本严格一致 |
| 磁盘 | 模型缓存 `~/.cache`（几十 GB）+ workspace（含复制过来的 sglang 仓 + runs） |

镜像：节点缺镜像时流水线自动尝试 `docker pull`（失败不阻断；离线节点需提前手动 `docker pull`）。

代码：只需在**执行机**准备（见上表"sglang 代码仓"行）。各节点代码每次运行前由流水线清理旧仓（`rm -rf {workspace}/sglang`）后从执行机整仓复制（tar 经 ssh，含 `.git`），保证多节点版本严格一致——节点不需要外网/git。

## <a id="sec-3"></a>3. 配置文件说明

按场景复制 `configs/` 下对应模板修改：`example_single.yaml`（单机）、`example_pd.yaml`（PD 分离）、`example_tp.yaml`（混布 TP）。模板中必填项裸露、可选项已注释（取消注释即用）。各参数的默认值、是否必填按段落列表如下，YAML 里不写即取默认值。

### 3.1 run 段

| 参数 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `run.workspace` | 无 | 是 | 节点上的工作目录。sglang 仓固定在 `{workspace}/sglang`，执行机结果目录固定在 `{workspace}/results`（与节点 `runs/` 平级，同机不冲突），均不可另配 |
| `run.code.git_remote` | 无 | 否 | 执行机 `{workspace}/sglang` **不存在或为空目录时**用于 `git clone`（节点不做 git 操作）。仓已存在且非空则忽略此字段，直接使用现状。未配且仓缺失/为空时报错提示手动准备 |
| `run.code.ref` | 无 | 否 | `git clone -b {ref}` 的分支/tag/commit。仅仓缺失/为空 clone 时生效；仓已存在不干预版本（用户自行 checkout）。未配时 clone 用远端默认分支 |
| `run.docker.image` | 无 | 是 | 测试容器镜像 |
| `run.docker.devices` | `auto` | 否 | `auto`=按节点 arch 推导卡数映射 `/dev/davinci0..N-1`（a3=16、a5=8）+ 管理设备；或显式列表如 `[0,1,2,3]`，以列表为准 |
| `run.docker.net` | `host` | 否 | 容器网络模式 |
| `run.docker.shm_size` | `16g` | 否 | 容器共享内存大小；固定附加 `--privileged --ipc=host` |
| `run.docker.extra_mounts` | `[]` | 否 | 额外 `-v` 挂载项（追加到默认 driver/缓存等挂载之后），格式同 docker -v：`"host:container"` 或 `"/data:/data:ro"` |
| `run.env` | 内置 7 项（见 3.4） | 否 | 注入容器的环境变量，按 key 合并覆盖内置默认，可追加新键 |
| `run.a5_env` | `{}`（不注入） | 否 | 仅注入 **a5 节点单机用例**容器的环境变量（多机用例及其他 arch 节点不注入），覆盖 `run.env` 同名键；如 A5 灵衢互联 `ASCEND_USE_FIA: "1"` |
| `run.datasets` | `[]`（不预置） | 否 | 数据集路径列表（节点本地**绝对路径**），容器启动后 cp 到 `/tmp/`，缺失则回退在线下载。详见下方说明 ① |

① **datasets**：所在目录自动挂载进容器（同路径映射；不支持根目录直属文件——所在目录为 `/` 会整盘挂载）。文件/目录缺失则忽略、回退在线下载。单机用例需该 `node` 节点存在该路径；多机用例仅需 `router`（PD 分离）/ `master`（混布 TP）节点存在——PD/worker 节点只起 server 不读数据集，路径不存在时 cp 静默忽略。示例：

```yaml
run:
  datasets:
    - /root/.cache/modelscope/hub/datasets/tmp/test.jsonl
    - /data/datasets/ShareGPT_V3_unfiltered_cleaned_split.json
```

② **evalscope（精度框架，零配置）**：源码固定在 `{workspace}/evalscope`（与 sglang 仓同约定），**仅当配置了精度用例才准备**——prepare 阶段读执行机 repo 上的用例文件，含 `test_npu_accuracy_utils` import（或直接调 `run_evalscope`）即判定为精度用例，纯性能配置不 clone、不分发；用例文件读不到时保守视为精度用例（宁可多 clone）。判定通过后执行机自动浅克隆官方仓（目录非空则跳过，想用自定义版本直接放入该目录；代理取 `run.env` 的 `http_proxy`/`https_proxy`），再以 sglang 仓同款方式（清理旧目录 + 整目录复制）同步到精度用例跑测试的节点（单机=node，混布 TP=master，PD 分离=router；worker/PD 节点只起 server 不需要）。节点就绪后容器内软链到 `/root/.cache/.cache/evalscope`（`run_evalscope.sh` 硬编码的本地源检查路径），实现本地 `pip install -e` 安装；clone/分发失败或节点无源码时不软链，回退清华镜像在线安装（需外网）。（旧版 `run.evalscope_source` 字段已移除，无需配置）

③ **pip_deps.txt（单机共享容器依赖，零配置）**：同节点的全部单机用例复用一个**长驻共享容器**（`sgl-pipeline-single`，首个单机用例时启动，run 结束统一删除）：容器启动时执行一次初始化（覆盖 ascend 工具、软链、数据集预置）并逐条执行 `configs/pip_deps.txt` 里的安装命令，随后每条用例经 `docker exec` 在容器内执行（用例超时由容器内 `timeout` 控制，执行前自动清理上一条用例残留的 sglang 进程防占卡）。

依赖安装命令**固定读流水线的 `configs/pip_deps.txt`，无需在 yaml 配置**：文件存在即生效（已预置 CI `_npu-pr-test-stage.yml` Install dependencies 步骤的内容，CI 更新后直接把对应行抄过来）；**每行一条命令按序执行**（同一 shell，`cd`/变量状态延续；首行 `cd /root/sglang` 对齐 CI 的 repo 根执行目录），`#` 注释/空行忽略；**单条失败只记 WARN 跳过、不终止初始化**（缺依赖的影响留给用例自身暴露，事后看 `.init.log` 里的 `[pip_deps] [WARN]` 行定位是哪条；文件内不要写 `exit`，会终止整个初始化）；**清空或删除该文件 = 不装任何依赖**。安装走容器默认 pip 源，慢时可配镜像源/代理（都是 `run.env` 加环境变量，`pip_deps.txt` 无需改动，用例内的 pip 调用同样生效）：`PIP_INDEX_URL: "https://pypi.tuna.tsinghua.edu.cn/simple"`（pip 原生识别；清华等公网源仍需 `http_proxy` 出网），内网源（如华为 mirrors.huawei.com）直连更快但须把源域名加入 `run.env` 的 `no_proxy`（流水线会自动拼接节点/协调地址，显式配置的值保留在前）。多机用例容器不执行（依赖需打进镜像）。文件结构（完整内容看文件本身）：

```bash
cd /root/sglang                    # 对齐 CI 的执行目录 (初始化建好的 repo 软链)
# ---- 基础依赖 ----
pip install sentence_transformers zss "wandb>=0.16.0" ...   # 照抄 CI Install dependencies
# ---- sgl-eval ----
. "${sglang_source_path}/scripts/ci/utils/sgl_eval_ref.sh"
pip install "$SGL_EVAL_SPEC"
# ---- lmms-eval (源码安装) ----
git clone --branch v0.3.3 --depth 1 https://github.com/EvolvingLMMs-Lab/lmms-eval.git /tmp/lmms-eval
...
# ---- sglang_router ----
apt-get install -y libssl-dev
pip install sglang_router
```

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
| `suites[].name` | 取 `file` basename 去 `.py` | 否 | 用例名称（`--suite` 过滤用）。未配置时默认取 `file` 的 basename 去掉 `.py`（如 `.../test_npu_qwen3_32b.py` → `test_npu_qwen3_32b`） |
| `suites[].node` | 无 | 三选一 | 单机用例：执行的节点 host。**nodes 仅定义 1 个节点时可省略**，默认用该节点；多节点时必须显式指定 |
| `suites[].roles` | 无 | 三选一 | 多机 PD 分离用例（第 7 节）：prefill/decode/router → 节点 |
| `suites[].multinode` | 无 | 三选一 | 多机混布 TP 用例（第 8 节）：节点列表，第一个 = master |
| `suites[].file` | 无 | 是 | 用例文件路径，相对 repo；绝对路径须在容器可见挂载内（repo 或 `~/.cache`） |
| `suites[].timeout_minutes` | 无（兜底 60 分钟） | 否 | 用例执行超时（分钟）。未配置时用默认 60 分钟兜底，防止容器内进程 hang 导致整个 run 卡死。单机用例=容器内 `timeout` 包裹用例进程（超时退出码 124）；多机用例=每角色容器 docker run 超时，prefill/decode/worker 实际再加 2 分钟余量 |

`node` / `roles` / `multinode` 三者互斥，只能配一个。

### 3.4 run.env 内置默认值

不写 `run.env` 时容器自动注入以下 7 项（与 CI 一致）；需覆盖某项或追加新键时才写：

| 环境变量 | 默认值 |
|---|---|
| `SGLANG_USE_MODELSCOPE` | `true` |
| `HF_ENDPOINT` | `https://hf-mirror.com` |
| `SGLANG_IS_IN_CI` | `true` |
| `SGLANG_TEST_MAX_RETRY` | `0` |
| `TORCH_EXTENSIONS_DIR` | `/tmp/torch_extensions` |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` |
| `STREAMS_PER_DEVICE` | `32` |

其中 `SGLANG_TEST_MAX_RETRY=0` 关闭用例方法级外层重试（性能基准重跑无意义且耗时；内层 `@retry()` 与精度用例的数据集重试不受影响）。

此外流水线会自动为所有容器注入：

- `TZ=Asia/Shanghai`：容器默认 UTC，与流水线日志时区混排会造成时序误判，统一对齐（需其他时区在 `run.env` 覆盖 `TZ`）；
- `no_proxy`/`NO_PROXY`（含全部节点 IP + 协调服务地址 + localhost，且优先于节点 docker 的代理配置注入）：协调服务、健康检查等内网请求直连，不受节点 `/root/.docker/config.json` 代理影响；外网下载仍走代理。
- `PYTHONPATH=/output`：注入 sitecustomize.py（单机/多机用例一致）——多机用例的 fake kubernetes 协调桥接（见第 7/8 节）+ 关闭 evalscope venv 内 requests 的 SSL 校验（规避企业代理自签证书导致 modelscope 数据集下载报 `CERTIFICATE_VERIFY_FAILED`；仅 venv 进程生效，其余 python 进程无副作用）。

### 3.5 配置示例（仓缺失时由流水线 clone）

```yaml
run:
  workspace: /root/sglang_local_pipeline
  code:                                        # 仓存在且非空时整段可删 (流水线不动 git, 直接用现状)
    git_remote: https://github.com/sgl-project/sglang.git
    ref: main                                  # 可省: 不配时 clone 用远端默认分支
  docker:
    image: <镜像地址>

nodes:
  - host: 192.168.10.1                  # A3; user/port 取默认时可只写 host + arch
    arch: a3
  - host: 192.168.10.2                  # A5 (8 卡, 单机用例 --tp-size 自动减半)
    arch: a5

suites:
  - node: 192.168.10.1
    file: test/registered/npu/llm_models/test_npu_qwen3_32b.py
    timeout_minutes: 120
    # name 可省略: 默认取 file basename → test_npu_qwen3_32b
  # 多机用例用 roles / multinode 代替 node, 见第 7/8 节
```

### 3.6 最小配置（单节点，仓已就位）

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
  - file: test/registered/npu/llm_models/test_npu_qwen3_32b.py
    timeout_minutes: 120
    # name/node 均可省略: name 默认取 file basename, node 默认取唯一节点
```

注意：该默认仅在**恰好 1 个节点**时生效。多节点配置下用例漏配 `node`/`roles`/`multinode` 会在启动时直接报错（不会静默选择某个节点）。

## <a id="sec-4"></a>4. 快速开始

```bash
# 1. 首次验证: 只打印将在节点上执行的命令，不消耗 NPU 资源
python3 src/run.py --config configs/example_single.yaml --dry-run

# 2. 执行全部用例
python3 src/run.py --config configs/example_single.yaml

# 3. 只执行指定用例（--suite 可传多次）
python3 src/run.py --config configs/example_single.yaml --suite test_npu_qwen3_32b

# 4. 定时执行（等到指定时间再开始，便于夜间无人值守跑用例）
python3 src/run.py --config configs/example_single.yaml --at "2026-09-17 18:00:00"   # 已过则立即执行并警告
python3 src/run.py --config configs/example_single.yaml --at "18:00:00"   # 今天已过则取明天
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
| `--at TIME` | 定时执行：阻塞到指定时间再开始。支持 `YYYY-MM-DD HH:MM:SS`（已过则立即执行并警告）或 `HH:MM:SS`（今天已过则取明天）。等待期间每分钟打印剩余秒数 |

### 退出码

- `0`：全部通过（或 dry-run）
- `1`：有用例失败
- `2`：配置错误（配置文件解析/校验失败、用例引用的节点未定义、无匹配用例等）

## <a id="sec-5"></a>5. 执行流程

```
python3 src/run.py
    │
    ├─ [prepare] 执行机准备代码仓 (唯一 git 操作点, 仓缺失时):
    │     仓存在且非空 ({workspace}/sglang) → 直接使用, 不做任何 git 操作
    │     仓缺失 + 配了 git_remote → git clone [-b {ref}] {remote} {workspace}/sglang
    │                                (用 run.env 代理; 只 clone, 不 fetch/checkout/reset;
    │                                 空目录 = 上轮 clone 失败残留, 重 clone 自愈)
    │     仓缺失 + 未配 git_remote → 报错提示手动准备
    │
    ├─ [prepare] 每个被用到的节点执行一次（SSH 到节点）:
    │     docker image inspect 镜像 || docker pull   # 镜像不存在则尝试 pull, 失败不阻断
    │     (远程节点) 清理旧代码仓 rm -rf {workspace}/sglang
    │       → 从执行机整仓复制 (tar 经 ssh, 含 .git)
    │     就绪后清理 sglang 残留:
    │       杀宿主机 sglang 进程（pkill -f: python -m sglang.* /
    │         sglang serve / sglang::* worker / sglang_router）
    │       删 sgl-pipeline-* 残留容器（docker rm -f; 同镜像他人容器不删）
    │       → 确保没有进程占用 NPU 卡
    │
    ├─ [execute] 逐用例串行执行（SSH 到节点）:
    │     单机用例: 每节点一个长驻共享容器 (sgl-pipeline-single, 首个单机
    │       用例时启动, 初始化/依赖只做一次, run 结束统一删除):
    │       docker run -d --privileged --ipc=host
    │         --device /dev/davinci0..N-1 + 管理设备
    │         挂载: repo / workspace 输出目录(runs/{run_id}) / driver /
    │              模型缓存(~/.cache) + run.datasets 所在目录 (自动, 可选)
    │              + run.docker.extra_mounts (用户自定义, 可选)
    │       容器内初始化 (执行机轮询 .init-ok 标记确认完成):
    │         覆盖 ascend 工具 → 按 run.datasets 预置数据集到 /tmp (可选)
    │         → 逐条执行 configs/pip_deps.txt 安装命令 (可选, 只装一次)
    │       每条用例: docker exec 执行 (执行前清理上一条用例残留的 sglang
    │         进程防占卡; 超时由容器内 timeout 控制; A5 节点经
    │         /output/run_case.py 包装启动, --tp-size 自动减半)
    │     多机用例 (roles): router 容器先启动 (等其写入 active-test-class),
    │       再启动 PD 容器; 各节点 docker run 同一用例文件, 以
    │       HOSTNAME/POD_IP 环境变量区分角色 (见第 7 节; 混布 TP
    │       multinode 全节点并发启动, 见第 8 节)
    │     每条用例完成即重写 summary.json
    │       (run.py 中途被杀也能看到已完成用例的结果)
    │
    └─ [fetch] 拉回节点上的运行产物到本地结果目录
         (tmp/ 及注入的固定脚本不回传, 见 6.2)
```

## <a id="sec-6"></a>6. 结果产物与日志路径

下面用一个完整示例说明。假设：

- 执行机就是 A3 本身（192.168.10.1）
- 配置文件为 `configs/single.yaml`（复制自 `example_single.yaml`），workspace 使用默认值 `/root/sglang_local_pipeline`（结果目录固定为 `/root/sglang_local_pipeline/results`）
- 执行的用例为 `qwen3-32b-gsm8k`，run_id 为 `single-20260916-100000`

执行机和节点使用**不同的目录**（`results/` vs `runs/`），即使同一台机器也不冲突。

### 6.1 节点上的日志（原始产物）

路径由 `workspace` + `runs` + `run_id` 拼成。日志文件名 = 用例脚本名去 `.py` 加 `.log`（如 `test_npu_qwen3_32b.py` → `test_npu_qwen3_32b.log`）。单机用例全部跑在一个长驻共享容器里（见 3.1 说明 ③），`run_id` 这一层即容器 `/output` 的挂载点，除每条用例的子目录外还含共享文件：

```
/root/sglang_local_pipeline/runs/single-20260916-100000/   ← 共享容器 /output
├── sitecustomize.py         # 流水线注入（evalscope SSL 补丁，经 PYTHONPATH 生效）
├── run_case.py              # 仅 A5 节点：--tp-size 减半包装器（流水线注入）
├── .init.log                # 共享容器初始化日志（含 pip 依赖安装输出）
├── tmp/                     # 共享容器 /tmp 挂载（全部单机用例共用，fetch 不回传）
├── plog/                    # 共享容器 /root/ascend/log 挂载（全部单机用例混写，fetch 不回传）
└── qwen3-32b-gsm8k/         # 每条单机用例一个子目录
    ├── test_npu_qwen3_32b.log
    └── plog/                # 用例结束时从共享 plog/ 拷贝的快照（按用例隔离，随用例目录回传）
```

多机用例在 `用例名` 下再按角色/节点序号分一层子目录（`prefill-0/`、`decode-0/`、`router-0/` 或 `node-0/`、`node-1/`），每个子目录内有自己的 `tmp/`、`plog/` 挂载及注入的协调桥接 `sitecustomize.py`（及其编译缓存 `__pycache__/`，见第 7/8 节）——多机用例仍每角色一个独立容器。

拉回后节点侧 `runs/` **不删除**（本机/远程节点行为一致），多次执行会按 run_id 各占一个子目录，累积保留作排查现场（含 `.init.log`、`tmp/` 等 fetch 不回传的内容）；磁盘紧张时手动 `rm -rf` 旧 run 目录即可。

### 6.2 执行机上的日志（拉回副本 + 实时回显）

路径由 `{workspace}/results` + `run_id` + `用例名` 拼成（不受执行目录影响），与节点的 `runs/` 分开。`run_id` 格式为 `{yaml_stem}-{timestamp}`（如 `single-20260916-100000`），目录名同时体现来源 yaml 与执行时间：

```
/root/sglang_local_pipeline/results/single-20260916-100000/
├── summary.json                       # run.py 写入的汇总 (每跑完一条用例即更新)
└── qwen3-32b-gsm8k/
    ├── test_npu_qwen3_32b.log         # ssh_run 实时回显 → fetch 覆盖为容器内落盘版本
    ├── ssh.log                        # SSH 连接诊断（本地直写，fetch 不覆盖）
    └── plog/                          # 从节点拉回的 NPU 底层日志
```

以下内容 **fetch 不回传**（节点侧 `runs/` 原件始终保留——本机/远程一致，需要深度排查时直接查看）：

- `tmp/`（容器内 /tmp：预置数据集、torch 编译缓存；单机共享容器在 `run_id/` 层、多机角色在各自子目录）——体积大且排查价值低；
- 共享容器的 `plog/`（`run_id/` 层，全部单机用例混写；每条用例目录内已有隔离快照副本）；
- `run_case.py`、`sitecustomize.py`（流水线注入的固定脚本，内容为内置常量）及其编译缓存 `__pycache__/`、`.init.log`（共享容器初始化日志，排查依赖安装问题时直接看节点上该文件）——无回传价值。

多个用例时，每个用例各占一个子目录，互不干扰：

```
/root/sglang_local_pipeline/results/single-20260916-100000/
├── summary.json
├── qwen3-32b-gsm8k/
│   ├── test_npu_qwen3_32b.log
│   ├── ssh.log
│   └── plog/
└── qwen3-14b-gsm8k/
    ├── test_npu_qwen3_14b.log
    ├── ssh.log
    └── plog/
```

run 结束时控制台最后一行打印绝对路径：`结果: /root/sglang_local_pipeline/results/single-20260916-100000`

summary.json **每跑完一条用例即全量重写一次**（非结束时统一写）：run.py 中途被杀（Ctrl+C、终端断开、执行机重启等）时，已完成用例的汇总与日志均已落盘可查；被中断时正在执行的用例不计入 summary，但其过程日志仍实时写在用例子目录的 `{脚本名}.log` 里。

### 6.3 执行机 = 节点时的路径关系

本例执行机就是节点，但两边使用**不同的目录名**（`results/` vs `runs/`），不会冲突：

```
/root/sglang_local_pipeline/
├── runs/                                    ← 节点（共享容器挂载写入）
│   └── single-20260916-100000/              ← 共享容器 /output
│       ├── sitecustomize.py / run_case.py / .init.log / tmp/ / plog/
│       │                                      ← 共享文件（始终保留）
│       └── qwen3-32b-gsm8k/                 ← 用例目录（始终保留）
│           ├── test_npu_qwen3_32b.log       ← 容器重定向写
│           └── plog/                        ← 用例结束从共享 plog 拷贝的快照
│
└── results/                                 ← 执行机（run.py + ssh_run + fetch 写入）
    └── single-20260916-100000/
        ├── summary.json                     ← run.py 写
        └── qwen3-32b-gsm8k/
            ├── test_npu_qwen3_32b.log       ← ssh_run 回显 → fetch 覆盖为容器内落盘版本
            ├── ssh.log                      ← ssh_run 诊断直写
            └── plog/                        ← fetch 从节点拉回
```

两个目录完全独立，无并发写入同一文件的问题。节点侧 `runs/` 原件（本机/远程一致）**始终保留**作排查现场——含 `.init.log`、`tmp/` 等 fetch 不回传的内容；代价是与 `results/` 重复占一份磁盘，紧张时手动 `rm -rf` 旧 run 目录即可。run 结束统一删除的只有容器（`sgl-pipeline-single` / 多机角色容器），不删 `runs/` 下任何文件。

跨节点执行时同理：节点上留在 `runs/`，执行机上落在 `results/`，fetch 把节点 `runs/{id}/{suite}/` 内容拉回到执行机 `results/{id}/{suite}/`。

### 6.4 在任意路径执行 run.py

`run.py` 可在任意目录下用绝对路径调用，不受 cwd 限制：

```bash
cd /home
python3 /root/sglang_local_pipeline/src/run.py --config /root/sglang_local_pipeline/configs/single.yaml
```

结果目录固定为 `{workspace}/results`（如上例为 `/root/sglang_local_pipeline/results/single-20260916-100000/`），不随 cwd 变化，无需配置。

## <a id="sec-7"></a>7. 多机（PD 分离）用例

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
- `router` 的值只能对应单个节点（字符串或单元素列表均可，等价），配多个节点会报错。
- 节点卡数在 `docker.devices: auto`（默认）时由 `arch` 决定（`a3`→16 卡、`a5`→8 卡），未知/缺失 arch 启动时报错；显式配 devices 列表时以列表为准。
- router 进程不占 NPU（容器仍挂卡，仅转发与压测），通常复用 P/D 节点（host 网络下端口不冲突：PD 服务 8000、router 6677），也可配独立节点。
- 各角色的启动参数、环境变量、断言阈值完全由用例文件自身定义（与 CI 一致），流水线只负责编排。

### 7.2 工作原理

这类用例（`TestNpuPerfMultiNodePdSepTestCaseBase`）原本跑在 K8s 上：用 `HOSTNAME` 区分角色、`POD_IP` 标识地址、ConfigMap 做节点发现与结束通知。本地无 K8s，流水线做了等价替代，**不修改 sglang 代码**：

1. 执行机起一个轻量 HTTP 协调服务（固定端口 9377，模拟 ConfigMap 的读/写。端口被残留的流水线进程占用时自动清理后重试；被无关进程占用或清理失败则启动报错，报错信息附带手动 kill 命令。**不会回退随机端口**——环境只放行 9377，换端口会导致远程节点静默连不上）；
2. 每个角色容器启动前注入 `sitecustomize.py`（落在挂载的 /output，经 `PYTHONPATH` 生效），把用例用到的 kubernetes 客户端接口重定向到协调服务。各角色注入的内容完全相同（角色差异全在环境变量），该文件及编译缓存仅留在节点 `runs/` 侧，fetch 不回传（见 6.2）；
3. 流水线预置所有 pod 的注册信息 `sglang-prefill-0`/`sglang-prefill-1`/`sglang-decode-0`/... → 节点 IP（K8s 里由各 pod 自注册），PD 节点据此确定 master 地址和 `ASCEND_MF_STORE_URL`，router 据此收集 PD 地址列表；
4. **router 容器先启动**，等它向协调服务写入 `active-test-class` 后再启动 prefill/decode 容器——保证 PD 首次查询 ConfigMap 即能看到该 key（对齐 CI 时序：CI 的 router pod 不挂 NPU、必然先写好；本地 router 同样挂满 NPU，启动竞争无偏向，需显式控制顺序。router 提前退出或等待超 120s 时兜底直接启动 PD）。各节点 `docker run` 同一用例文件，以环境变量区分角色和序号：
   - `HOSTNAME=sglang-{role}-{idx}`：用例框架据此识别角色（含 role 名）和序号（末尾数字）；
   - `POD_IP=节点 IP`：服务绑定与互访地址（容器 host 网络）；
   - prefill/decode 拉起 PD 服务后轮询等待结束信号；router 等 PD 端口（8000）全部就绪后拉起 router 进程，`/health` 就绪后执行基准测试；
5. router 结束（无论成败）后，流水线向协调服务写结束信号，所有 prefill/decode 收到后正常退出；任一 PD 服务提前崩溃时同样广播信号，避免其他节点空等超时。结束信号写入 60s 后仍未退出的容器（如卡在端口等待不查 ConfigMap）会被强制 `docker rm -f`（等价 CI 外层 runner 删 job，避免拖到容器超时）；用例收尾时另对全部角色容器做一次幂等 `docker rm -f` 兜底，清理角色自身超时（docker 客户端被杀、容器本体仍在跑）留下的孤儿容器。
6. 用例判定对齐 CI（CI 只看 router pod 日志的 `OK`/`FAILED`，判定后直接删 job，PD pod 是被连带杀掉的）：router 退出码为 0 即通过；prefill/decode 收到结束信号以 0 退出、或超过宽限期被强杀（rc=137）均不判失败，其余非 0 退出码视为 PD 崩溃判失败（等价 CI 检测 pod 非 Running）。基准结果与断言都在 router 的日志里。

### 7.3 前置条件（多机用例额外要求）

| 项目 | 要求 |
|---|---|
| 节点间网络互通 | router 需访问 prefill/decode 的 8000 端口；PD 分离还用到 8995（bootstrap）、24666（MF store）等端口，节点间防火墙需放行 |
| 节点可达执行机 | 各节点容器需访问执行机的协调服务端口（固定 9377）。执行机在 NAT 后、节点无法回访时不支持 |
| 模型缓存 | prefill/decode 节点均需预置模型缓存（`~/.cache`，与单机用例一致） |
| 镜像 | router 所在节点镜像需含 `sglang_router`（与 CI 一致的镜像已含；缺失时 router 日志会报 ModuleNotFoundError） |

节点 `/root/.docker/config.json` 配了代理也不影响：流水线给所有容器注入 `no_proxy`（含全部节点 IP + 协调服务地址），协调服务与节点互访直连（见 3.4）。

### 7.4 产物

多机用例每个角色在 results 下各占一个子目录，结构同 6.2（`{脚本名}.log` + `ssh.log` + `plog/`；基准测试结果看 `router-0/{脚本名}.log`）。以 2p2d 为例：

```
results/{run_id}/
├── summary.json
└── dsv4-flash-w8a8-2p2d-16p/
    ├── prefill-0/
    │   ├── {脚本名}.log
    │   ├── ssh.log
    │   └── plog/
    ├── prefill-1/
    │   ├── {脚本名}.log
    │   ├── ssh.log
    │   └── plog/
    ├── decode-0/
    │   ├── {脚本名}.log
    │   ├── ssh.log
    │   └── plog/
    ├── decode-1/
    │   ├── {脚本名}.log
    │   ├── ssh.log
    │   └── plog/
    └── router-0/
        ├── {脚本名}.log
        ├── ssh.log
        └── plog/
```

节点 `runs/` 侧同构，但多出 `tmp/`、`sitecustomize.py`、`__pycache__/`（fetch 不回传，见 6.2）。

执行过程中控制台**只回显 router 的输出**（每行带 `[router-0] ` 前缀），对齐 CI 各 pod 日志隔离的观感；prefill/decode 日志量大且与 router 交错，仅实时写入各自子目录的 `{脚本名}.log` 不回显（PD 角色异常结束时，控制台的状态行会指明其日志路径）。基准结果与断言看 `router-0/{脚本名}.log`，PD 服务问题看对应角色目录。混布 TP 用例（第 8 节）同理：只回显 master（node-0），worker 仅写文件。

## <a id="sec-8"></a>8. 多机（混布 TP）用例

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
| 结束信号 | router 结束 → 广播 → PD 退出 | master 结束 → 广播（worker 不轮询该信号，sleep 3600s 保活，由流水线在宽限期后强杀清理） |
| 判定 | router rc==0 即通过（PD 被强杀的 137 不判失败） | master rc==0 即通过（worker 被强杀的 137 不判失败） |

用例的 `launch_pd_mix_node` 从 ConfigMap 查 `sglang-node-0` 的 IP，拼接 `--dist-init-addr={master_ip}:5000 --node-rank={pod_index}` 启动 sglang server。

### 8.3 前置条件

与第 7 节相同（节点间互通、可达执行机协调端口），但无 router 端口需求。所有节点均需预置模型缓存。

### 8.4 产物

与 PD 分离结构类似，按节点序号分子目录，每个子目录同 6.2 结构（基准测试结果看 `node-0/{脚本名}.log`）：

```
results/{run_id}/
├── summary.json
└── glm5_2-16p-gpqa/
    ├── node-0/                # master (跑测试)
    │   ├── {脚本名}.log
    │   ├── ssh.log
    │   └── plog/
    └── node-1/                # worker (只起 server)
        ├── {脚本名}.log
        ├── ssh.log
        └── plog/
```

## <a id="sec-9"></a>9. 常见问题

**Q: 改了个人 fork 的分支，节点上的旧仓库会冲突吗？**
不会。仓存在且非空时流水线不动 git（用户自行 `git fetch/checkout/分支切换`）；各节点代码每次运行前都会被清理后从执行机整仓复制，不存在节点侧残留冲突。换 fork 时自己在执行机 `git remote set-url` + `git fetch` 切即可，流水线不干预。

**Q: 节点无法访问 GitHub 怎么办？**
代码不受影响：git 操作只在执行机做（且仅仓缺失时才 clone），节点无需外网（代码由流水线从执行机复制过去）。镜像则需节点可达 docker registry，节点缺镜像时流水线会尝试 `docker pull`，失败不阻断——离线节点提前手动 `docker pull` 即可。
代码也只需在执行机准备好：把代码放到执行机的 `{workspace}/sglang`（如手动 `git clone`），流水线会自动清理各节点旧仓后复制过去。
数据集（如 gsm8k 默认从 GitHub 在线下载）：提前下载后放到节点本地目录，配 `run.datasets` 指向**绝对路径**即可（挂载与回退规则见 3.1 说明 ①）：

```yaml
run:
  datasets:
    - /data/datasets/ShareGPT_V3_unfiltered_cleaned_split.json
```

**Q: 如何测某个特定 commit？**
仓存在且非空时自己在执行机 `git checkout <commit>` 即可（流水线不动 git）。仓缺失且想让流水线 clone 时指定版本：`run.code.ref` 改为分支/tag/commit SHA（`git clone -b {ref}` 对三者通用）。

**Q: `file` 能配 repo 外的自定义用例吗？**
能，但用例跑在容器里，文件必须在容器可见的挂载内。最简单的做法：把用例放到节点的 `~/.cache/` 下（随模型缓存挂载进容器），然后 `file` 配绝对路径，如 `/root/.cache/my_tests/test_foo.py`。其他节点路径（如 `/data/...`）容器内不可见，会报 `No such file or directory`。

**Q: 需要登录的私有镜像？**
在节点上提前 `docker login`，节点缺镜像时流水线自动 `docker pull`（私有 registry 已登录即可拉）。

**Q: 只想看会执行什么命令？**
用 `--dry-run`，输出节点上将要执行的完整 docker 命令，不消耗 NPU 资源。

**Q: A5 节点上跑单机用例，脚本里的 `--tp-size` 是按 A3 卡数配置的，怎么办？**
该节点配置 `arch: a5`。流水线会在其单机用例容器启动前注入包装器（`/output/run_case.py`），把传给 server 的 `--tp-size` **大于 2 时**自动除以 2 再执行用例（`{脚本名}.log` 里有 `[a5-适配] --tp-size 8 -> 4` 记录可核对），不修改 sglang 仓库代码；脚本里没配 `--tp-size`、配了 1/2（视为已按 A5 卡数适配）或不经 server 启动的用例均不受影响。多机用例（`roles`/`multinode`）不做此适配。

**Q: A5 单机用例需要开灵衢互联（FIA）怎么办？**
配置 `run.a5_env`（仅对 a5 节点单机用例容器生效，其他容器不受影响）：

```yaml
run:
  a5_env:
    ASCEND_USE_FIA: "1"
```

**Q: 为什么 `docker ps` 只看到一个 `sgl-pipeline-single` 容器？**
同节点的全部单机用例复用一个长驻共享容器（见 3.1 说明 ③）：首个单机用例时启动（初始化 + `configs/pip_deps.txt` 依赖只装一次），每条用例经 `docker exec` 在其中执行，run 结束统一删除。初始化完成时控制台会自动汇总被跳过的依赖命令（`[pip_deps] [WARN]` 行）；初始化报错则屏显 `.init.log` 尾部；完整安装过程看节点上 `runs/{run_id}/.init.log`。

**Q: 能在同一执行机同时跑两个 run.py 吗？**
不建议。多机用例的协调服务固定用 9377 端口，第二个 run 启动协调服务时会**自动清理占用该端口的残留流水线进程**——会把第一个仍在运行的 run.py 主进程杀掉。多个 run 请串行执行（或用 `--at` 定时错开）。

## <a id="sec-appendix-a"></a>附录 A: 配置 SSH 免密

```bash
# 执行机上（已有 key 可跳过第一步）
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
ssh-copy-id root@192.168.10.1
ssh-copy-id root@192.168.10.2
```

不支持在配置文件中传密码（原生 ssh 不接受非交互传密码）。
