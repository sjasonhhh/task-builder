# chemistry

制药方向共享环境，包含分子结构、骨架与片段处理、分子性质预测和生成分子分析所需软件。

科研环境为 Python 3.11、PyTorch 2.4.1 和 CUDA 12.1；Python 包版本见两个 requirements 文件。主要软件包括 RDKit、NumPy、pandas、SciPy、scikit-learn、matplotlib、seaborn、openpyxl、SELFIES 和 DeepSMILES。

造题程序使用 `/opt/orch/venv` 中的 Python 3.12、Harbor 0.22.0 及项目补丁；镜像还需提供 Claude Code、`setsid` 和 dockerfile-parse。tmux 已启用鼠标滚动和 100000 行历史记录。

实验镜像工作目录为 `/workspace`，评分镜像继承实验环境，工作目录为 `/tests`。沙盒使用 CPU，H 集群可使用 GPU。

## 配置镜像

```bash
export TB="/mnt/shared-storage-user/cl4mind/data/scienv/task-builder" # 项目绝对路径，按实际位置修改
export SCI_ENV=chemistry

cp -n "$TB/image/$SCI_ENV/image-env.toml.example" "$TB/image/$SCI_ENV/image-env.toml"
```

在 `image-env.toml` 中填写可访问的镜像地址与资源配置。Dockerfile 保留现有基础镜像定义；自行构建时，可用 `BASE_IMAGE` 构建参数指定兼容的基础镜像。
