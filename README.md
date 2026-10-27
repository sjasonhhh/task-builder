# task-builder

从论文、数据和代码生成科研任务，在 H 集群通过 Harbor 运行和评分。

## 🚀 Quick Start

需要 H 集群权限、可访问的科研镜像和模型 API；开发机使用 Python 3.11+，worker 使用 Python 3.12+。将示例中的 `<...>` 替换为自己的参数。

**1 — 下载项目**

在开发机的共享存储目录中执行：

```bash
git clone "https://github.com/sjasonhhh/task-builder.git" task-builder
cd task-builder
export TB="$PWD"
export SCI_ENV=single-cell
```

**2 — 准备论文材料**

每篇论文放在一个目录中：

```text
inputs/single-cell/
└── paper-001/
    ├── context/
    │   ├── text.md        # 论文正文
    │   ├── links.json     # 论文链接及上下文
    │   ├── figures/       # 论文图片
    │   └── tables/        # 论文表格
    ├── data/             # 实验数据
    └── code/             # 论文代码
```

在 `config/run.txt` 中填写要生成任务的论文目录名，每行一个：

```text
paper-001
paper-002
```

**3 — 配置模型与集群**

模型配置填写在 `config/author.env`：

```dotenv
MODEL_ID=<模型名称>
ANTHROPIC_BASE_URL=<模型接口地址>
ANTHROPIC_AUTH_TOKEN=<API密钥>
```

镜像与集群参数填写在 `image/single-cell/image-env.toml`：

```toml
[images]
experiment = "<实验镜像地址>"
evaluation = "<评分镜像地址>"

[hcluster]
namespace = "<集群命名空间>"
charged_group = "<计费资源组>"
mount = "gpfs://gpfs1/<共享存储名称>:<开发机上的共享存储绝对路径>"
max_cpu = 16
max_memory_mb = 65536
max_agent_gpus = 1
max_verifier_gpus = 1
priority = 9

[timeouts]
agent_timeout_sec = 36000
harbor_timeout_sec = 36000
```

`mount` 使用与开发机相同的共享存储路径，使 worker（运行任务的机器）能够从相同路径读取项目。

模型请求通过开发机转发，配置填写在 `relay/config.json`：

```json
{
  "bind_host": "0.0.0.0",
  "advertised_host": "<worker可以访问的开发机IP>",
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

**4 — 启动 worker**

在开发机执行。worker 镜像需包含科研软件、Claude Code 和 rjob；可直接使用预装 Harbor 0.22.0 及项目补丁的镜像。

```bash
python3 "$TB/relay/relay.py" start

bash "$TB/scripts/start_worker.sh" \
  --sci-env "$SCI_ENV" \
  --image "<worker镜像地址>" \
  --cpu 16 \
  --memory-mb 65536 \
  --gpus 1
```

进入启动结果中的 worker：

```bash
brainctl exec --namespace="<集群命名空间>" \
  -it "process/<worker名称>" -- bash
```

**5 — 生成任务**

以下命令在 worker 内执行：

```bash
export TB="<与开发机相同的项目绝对路径>"
export SCI_ENV=single-cell
export PATH="/kubebrain:$PATH"
cd "$TB"

bash setup_env.sh

bash run.sh \
  --sci-env "$SCI_ENV" \
  --input "$TB/inputs" \
  --output "$TB/output" \
  --author-env "$TB/config/author.env" \
  --model-relay "$TB/relay/connection.json" \
  --run-txt "$TB/config/run.txt" \
  --max-revisions 3 \
  --concurrency 1 \
  --task-timeout-sec 19800
```

生成的任务保存在 `output/single-cell/final/`，运行记录保存在 `output/single-cell/runs/`。

## 答题与评分

在 `config/run-task.txt` 中填写已生成的任务目录名，每行一个。在 worker 内执行：

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

评分结果保存在 `output/agent-runs/<环境名>/<运行编号>/summary.json`。

## 继续已有运行

在 worker 内指定旧运行目录，以及原来使用的环境根目录：

```bash
bash "$TB/run.sh" \
  --sci-env "$SCI_ENV" \
  --environment-root "<包含single-cell等目录的原环境根目录>" \
  --input "$TB/inputs" \
  --output "$TB/output" \
  --author-env "$TB/config/author.env" \
  --model-relay "$TB/relay/connection.json" \
  --run-txt "$TB/config/run.txt" \
  --resume-from "<旧运行目录>/runs/<运行编号>" \
  --max-revisions 3 \
  --concurrency 1 \
  --task-timeout-sec 19800
```

## 结束运行

任务结束后，在开发机执行：

```bash
brainctl stop "process/<worker名称>" \
  --namespace="<集群命名空间>" \
  --request-timeout=180s

python3 "$TB/relay/relay.py" stop
```
