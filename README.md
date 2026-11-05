# task-builder

从论文、数据和代码生成科研任务，支持 H 集群和沙盒两种运行方式。

| 运行方式 | 造题 | 实验与评分 | 支持资源 |
|---|---|---|---|
| H 集群 | 在 worker 中运行 | 分别创建集群任务 | CPU、GPU |
| 沙盒 | 每篇论文使用独立沙盒 | 分别创建实验和评分沙盒 | CPU |

以下以 `chemistry` 为例。使用其他领域时，修改 `SCI_ENV`，并准备对应的输入目录和环境文件。将示例中的 `<...>` 替换为自己的参数。

## 一、准备项目

### 1. 下载与安装

需要 Python 3.12+、Git LFS 和 `patch`。执行造题或答题程序的机器需具备相应的 Claude Code 环境。

```bash
export TB="/mnt/shared-storage-user/cl4mind/data/scienv/task-builder" # 项目绝对路径，按实际位置修改
export SCI_ENV=chemistry # 领域名，对应 inputs/ 和 image/ 下的同名目录

git lfs install
git clone https://github.com/sjasonhhh/task-builder.git "$TB"
cd "$TB"
git lfs pull

bash "$TB/setup_env.sh"
```

### 2. 准备论文材料

```text
inputs/chemistry/<论文目录名>/
├── context/
│   ├── text.md
│   ├── figures/
│   └── tables/
├── data/
└── code/
```

`context/` 放论文正文和图表，`data/` 放数据，`code/` 放原始代码。chemistry 论文材料需自行准备，仓库保留已有的 single-cell 示例。

```bash
cp -n "$TB/config/run.txt.example" "$TB/config/run.txt"
cp -n "$TB/config/run-task.txt.example" "$TB/config/run-task.txt"
```

在 `config/run.txt` 中填写需要造题的论文目录名，每行一个：

```text
s41467-021-23415-2
s41467-023-38192-3
```

### 3. 配置模型

```bash
cp -n "$TB/config/author.env.example" "$TB/config/author.env"
```

填写 `config/author.env`：

```dotenv
MODEL_ID=<模型名称>
ANTHROPIC_BASE_URL=<模型接口地址>
ANTHROPIC_AUTH_TOKEN=<API密钥>
```

### 4. 配置领域环境

```text
image/chemistry/
├── Dockerfile.experiment
├── Dockerfile.evaluation
├── requirements-experiment.txt
├── requirements-evaluation.txt
├── README.md
├── image-env.toml.example
└── human_advise.md             # 可选：领域专家建议
```

```bash
cp -n "$TB/image/$SCI_ENV/image-env.toml.example" "$TB/image/$SCI_ENV/image-env.toml"
```

准备可访问的实验镜像和评分镜像，填写 `image/chemistry/image-env.toml`：

```toml
[images]
experiment = "<实验镜像地址>"
evaluation = "<评分镜像地址>"

[hcluster]
max_cpu = 16
max_memory_mb = 65536

# 以下配置用于 H 集群
namespace = "<集群命名空间>"
charged_group = "<计费资源组>"
mount = "gpfs://gpfs1/<共享存储名称>:<共享存储绝对路径>"
max_agent_gpus = 1
max_verifier_gpus = 1
priority = 9

[timeouts]
agent_timeout_sec = 14400
harbor_timeout_sec = 18000
```

## 二、H 集群流程

开发机需要 `rlaunch`、`brainctl` 和 H 集群使用权限。worker 镜像需要包含 Python 3.12+、Claude Code、`rjob` 和 `brainctl`。

### 1. 配置模型转发

```bash
cp -n "$TB/relay/config.example.json" "$TB/relay/config.json"
```

填写 `relay/config.json`。`advertised_host` 填 worker 能访问的开发机 IP；模型接口需要网络代理时，填写 `upstream_proxy`，否则使用 `null`。

```json
{
  "bind_host": "0.0.0.0",
  "advertised_host": "<开发机IP>",
  "upstream_proxy": null,
  "entries": [
    {
      "name": "author",
      "env_file": "../config/author.env",
      "base_url_var": "ANTHROPIC_BASE_URL",
      "auth_var": "ANTHROPIC_AUTH_TOKEN",
      "auth_type": "bearer",
      "model_var": "MODEL_ID"
    }
  ]
}
```

### 2. 启动 worker

在开发机执行：

```bash
python3 "$TB/relay/relay.py" start

bash "$TB/scripts/start_worker.sh" \
  --sci-env "$SCI_ENV" \
  --image "<造题worker镜像地址>" \
  --cpu 16 \
  --memory-mb 65536 \
  --gpus 1
```

使用启动结果中的 worker 名称进入容器：

```bash
export H_NAMESPACE="<集群命名空间>"
export WORKER_NAME="<worker名称>"

brainctl exec \
  --namespace="$H_NAMESPACE" \
  -it "process/$WORKER_NAME" -- bash
```

### 3. 初始化 worker

在 worker 内执行，`TB` 使用与开发机相同的项目路径：

```bash
export TB="/mnt/shared-storage-user/cl4mind/data/scienv/task-builder" # 与开发机的项目路径一致
export SCI_ENV=chemistry
export PATH="/kubebrain:$PATH"

cd "$TB"
bash "$TB/setup_env.sh"
```

### 4. 生成任务

在 worker 内执行：

```bash
bash "$TB/run.sh" \
  --sci-env "$SCI_ENV" \
  --input "$TB/inputs" \
  --output "$TB/output" \
  --author-env "$TB/config/author.env" \
  --model-relay "$TB/relay/connection.json" \
  --run-txt "$TB/config/run.txt" \
  --human-review \
  --max-revisions 3 \
  --concurrency 1 \
  --task-timeout-sec 19800
```

### 5. 继续已有运行

需要复用旧运行中的计划和草稿时，在 worker 内执行：

```bash
bash "$TB/run.sh" \
  --sci-env "$SCI_ENV" \
  --input "$TB/inputs" \
  --output "$TB/output" \
  --author-env "$TB/config/author.env" \
  --model-relay "$TB/relay/connection.json" \
  --run-txt "$TB/config/run.txt" \
  --resume-from "$TB/output/$SCI_ENV/runs/<运行编号>" \
  --human-review \
  --max-revisions 3 \
  --concurrency 1 \
  --task-timeout-sec 19800
```

### 6. 答题与评分

在 `config/run-task.txt` 中填写 `output/<领域>/final/` 下的任务目录名，每行一个。

在 worker 内执行：

```bash
bash "$TB/run_task.sh" \
  --sci-env "$SCI_ENV" \
  --input "$TB/output/$SCI_ENV/final" \
  --output "$TB/output/agent-runs/$SCI_ENV" \
  --run_txt "$TB/config/run-task.txt" \
  --env "$TB/config/author.env" \
  --model-relay "$TB/relay/connection.json" \
  --concurrency 1
```

### 7. 结束运行

退出 worker：

```bash
exit
```

回到开发机，释放 worker 并关闭模型转发：

```bash
brainctl stop "process/$WORKER_NAME" \
  --namespace="$H_NAMESPACE" \
  --request-timeout=180s

python3 "$TB/relay/relay.py" stop
```

## 三、沙盒流程

需要沙盒平台账号和资源池访问权限，模型接口应能从沙盒直接访问。

每篇论文使用独立造题沙盒，实验与评分分别使用另外的沙盒。造题沙盒默认使用 `image-env.toml` 中的实验镜像，CPU 和内存读取 `max_cpu`、`max_memory_mb`。

造题镜像需要预装 Claude Code，以及位于 `/opt/orch/venv` 的 Python 3.12+、Harbor 0.22.0 和项目补丁。使用其他造题镜像时，可通过 `--image` 指定。

### 1. 配置沙盒

```bash
cp -n "$TB/config/sandbox.env.example" "$TB/config/sandbox.env"
```

填写 `config/sandbox.env`：

```dotenv
BASE_URL=<沙盒平台HTTP或HTTPS地址>
TASK_BUILDER_SANDBOX_RESOURCE=<资源池标识>
AD_USERNAME=<实验室AD账号>
AD_PASSWORD='<实验室AD密码>'
```

### 2. 生成任务

在开发机的项目目录中执行：

```bash
bash "$TB/run_sandbox.sh" \
  --sci-env "$SCI_ENV" \
  --input "$TB/inputs" \
  --output "$TB/output" \
  --author-env "$TB/config/author.env" \
  --run-txt "$TB/config/run.txt" \
  --human-review \
  --max-revisions 3 \
  --concurrency 1 \
  --task-timeout-sec 19800
```

### 3. 继续已有运行

```bash
bash "$TB/run_sandbox.sh" \
  --sci-env "$SCI_ENV" \
  --input "$TB/inputs" \
  --output "$TB/output" \
  --author-env "$TB/config/author.env" \
  --run-txt "$TB/config/run.txt" \
  --resume-from "$TB/output/$SCI_ENV/runs/<运行编号>" \
  --human-review \
  --max-revisions 3 \
  --concurrency 1 \
  --task-timeout-sec 19800
```

### 4. 答题与评分

在 `config/run-task.txt` 中填写 `output/<领域>/final/` 下的任务目录名，每行一个。执行这条命令的开发机需要安装 Claude Code。

```bash
bash "$TB/run_task.sh" \
  --sci-env "$SCI_ENV" \
  --input "$TB/output/$SCI_ENV/final" \
  --output "$TB/output/agent-runs/$SCI_ENV" \
  --run_txt "$TB/config/run-task.txt" \
  --env "$TB/config/author.env" \
  --concurrency 1 \
  --sandbox
```

造题结果完整回传后，程序自动清理对应沙盒。

## 四、人工审核与修改

生成命令中的 `--human-review` 会为成品任务生成审核文件：

```text
output/<领域>/final/<任务目录名>/human_review.md
```

审核通过时，将状态改为 `通过`。

需要修改时，填写：

```markdown
状态：需要修改

## 修改意见

填写需要调整的内容。
```

将对应论文保留在 `config/run.txt` 中，再执行原运行方式的生成命令。程序会读取修改意见并修复已有任务。

## 五、输出位置

```text
output/
├── <领域>/
│   ├── final/
│   │   └── <任务目录名>/
│   └── runs/
│       └── <运行编号>/
└── agent-runs/
    └── <领域>/
        └── <运行编号>/
            └── summary.json
```

- `final/`：生成的任务。
- `runs/`：造题计划、草稿、日志和验证记录。
- `agent-runs/`：独立答题与评分结果。
