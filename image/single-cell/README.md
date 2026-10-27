# single-cell 共享环境

单细胞与空间组学共享环境，覆盖 scRNA/scATAC/multiome、RNA velocity、空间转录组、细胞注释和单细胞基础模型。task-builder 用 `--sci-env single-cell` 选用本环境。

| | 实验镜像（答题） | 评测镜像（评分） |
|---|---|---|
| 标签 | `scienv-single-cell-experiment-gpu-20261022` | `scienv-single-cell-evaluation-gpu-20261022` |
| 底座 | `scienv-biology-author-gpu-20261016` | `scienv-biology-evaluation-gpu-20261014` |
| 系统 | Debian GNU/Linux 12 (bookworm) | Debian GNU/Linux 12 (bookworm) |
| Python | Python 3.11.17（`/usr/local/bin/python`） | Python 3.11.17（`/usr/local/bin/python`） |
| PyTorch / CUDA | torch 2.4.1+cu121 cuda 12.1 cudnn 90100 | torch 2.4.1+cu121 cuda 12.1 cudnn 90100 |
| R | R version 4.2.2 Patched (2022-11-10 r83330) -- "Innocent and Trusting" | 无 |
| Python 包 | 219 个（本环境新增 89 个） | 49 个（本环境新增 2 个） |
| R 包 | 228 个 | 无 |
| 系统包（apt） | 650 个 | 151 个 |

以下清单直接从已推送的镜像中导出（`pip list`、`installed.packages()`、`dpkg-query`），不是从 Dockerfile 推断的。底座已有的包一律不升级：构建时以底座的 `pip freeze` 作为约束。

## 实验镜像

在底座之上新增：
- **Python**：scvi-tools、squidpy、scvelo、scib、harmonypy、scanorama、bbknn、leidenalg、igraph、torch-geometric、mudata/muon、pydeseq2、gseapy、celltypist、decoupler、loompy 等，以及它们的依赖（锁定版本见 `requirements-experiment.txt`）。
- **R**（Debian bookworm 预编译包，与底座 R 同源）：biocmanager、sctransform、seurat、seuratobject、complexheatmap、dropletutils、scater、scran、scuttle、singlecellexperiment、singler，以及它们的依赖。

不含 CellRanger。

### 命令行工具

| 工具 | 版本 |
|---|---|
| samtools | samtools 1.16.1 |
| bcftools | bcftools 1.16 |
| bedtools | bedtools v2.30.0 |
| tabix | tabix (htslib) 1.16 |
| bgzip | bgzip (htslib) 1.16 |
| deno | deno 2.9.4 (stable, release, x86_64-unknown-linux-gnu) |
| claude | 2.1.301 (Claude Code) |
| uv | uv 0.9.14 |
| git | git version 2.39.5 |
| curl | curl 7.88.1 (x86_64-pc-linux-gnu) libcurl/7.88.1 OpenSSL/3.0.22 zlib/1.2.13 brot |
| gcc | gcc (Debian 12.2.0-14+deb12u1) 12.2.0 |
| make | GNU Make 4.3 |
| unzip | UnZip 6.00 |
| harbor | 0.22.0（`/opt/orch/venv/bin/harbor`，编排用 Python 3.12 venv） |
| rjob | `/usr/local/bin/rjob` |

### Python 包（219 个）

| 包 | 版本 |
|---|---|
| absl-py | 2.5.1 |
| accelerate | 0.34.2 |
| adjustText | 1.4.0 |
| aiohappyeyeballs | 2.7.1 |
| aiohttp | 3.14.5 |
| aiosignal | 1.4.0 |
| anndata | 0.10.8 |
| annotated-types | 0.8.0 |
| annoy | 1.17.3 |
| array-api-compat | 1.15.0 |
| asciitree | 0.3.3 |
| attrs | 26.1.0 |
| bbknn | 1.6.0 |
| biopython | 1.85 |
| cellpose | 3.1.0 |
| celltypist | 1.7.1 |
| certifi | 2026.7.22 |
| chardet | 7.6.0 |
| charset-normalizer | 3.5.2 |
| chex | 0.1.90 |
| click | 8.5.0 |
| cloudpickle | 3.1.2 |
| ConfigArgParse | 1.8.0 |
| contourpy | 1.3.3 |
| cycler | 0.12.1 |
| Cython | 3.3.0 |
| dask | 2026.8.0 |
| dask-image | 2026.5.0 |
| decoupler | 2.2.0 |
| Deprecated | 1.3.1 |
| docrep | 0.3.2 |
| edt | 3.1.2 |
| et_xmlfile | 2.0.0 |
| etils | 1.14.0 |
| fasteners | 0.20 |
| fastremap | 1.20.0 |
| fbpca | 1.0 |
| filelock | 3.32.3 |
| flax | 0.10.4 |
| fonttools | 4.66.1 |
| formulaic | 1.2.2 |
| formulaic-contrasts | 1.0.0 |
| frozenlist | 1.8.0 |
| fsspec | 2026.7.0 |
| geosketch | 1.3 |
| googleapis-common-protos | 1.75.5 |
| grpcio | 1.84.0 |
| gseapy | 1.3.1 |
| h5py | 3.16.0 |
| harmonypy | 2.1.0 |
| huggingface-hub | 0.25.0 |
| humanize | 4.16.0 |
| idna | 3.20 |
| igraph | 1.0.0 |
| imagecodecs | 2024.6.1 |
| imageio | 2.34.2 |
| imageio-ffmpeg | 0.5.1 |
| importlib_metadata | 9.0.1 |
| inflect | 7.6.0 |
| interface_meta | 2.0.1 |
| intervaltree | 3.2.1 |
| jaraco.functools | 4.6.0 |
| jax | 0.4.38 |
| jaxlib | 0.4.38 |
| Jinja2 | 3.1.6 |
| joblib | 1.5.3 |
| kiwisolver | 1.5.1 |
| kornia | 0.8.3 |
| kornia_rs | 0.2.0 |
| lazy-loader | 0.6 |
| legacy-api-wrap | 1.5 |
| legendkit | 0.5.2 |
| leidenalg | 0.12.0 |
| lightning | 2.6.6 |
| lightning-utilities | 0.15.3 |
| llvmlite | 0.48.0 |
| locket | 1.0.0 |
| loompy | 3.0.8 |
| lz4 | 4.4.5 |
| Markdown | 3.11.1 |
| markdown-it-py | 4.2.0 |
| MarkupSafe | 3.0.3 |
| marsilea | 0.8.1 |
| matplotlib | 3.10.9 |
| matplotlib-scalebar | 0.9.0 |
| mdurl | 0.1.2 |
| ml_collections | 1.1.0 |
| ml_dtypes | 0.5.4 |
| more-itertools | 11.2.1 |
| mpmath | 1.3.0 |
| msgpack | 1.2.3 |
| mudata | 0.3.10 |
| multidict | 7.1.0 |
| multipledispatch | 1.0.0 |
| muon | 0.1.9 |
| narwhals | 2.27.1 |
| natsort | 8.4.0 |
| nest-asyncio | 1.6.0 |
| networkx | 3.4.2 |
| numba | 0.66.0 |
| numcodecs | 0.15.2 |
| numerize | 0.12 |
| numpy | 1.26.4 |
| numpy-groupies | 0.15.0 |
| numpyro | 0.19.0 |
| nvidia-cublas-cu12 | 12.1.3.1 |
| nvidia-cuda-cupti-cu12 | 12.1.105 |
| nvidia-cuda-nvrtc-cu12 | 12.1.105 |
| nvidia-cuda-runtime-cu12 | 12.1.105 |
| nvidia-cudnn-cu12 | 9.1.0.70 |
| nvidia-cufft-cu12 | 11.0.2.54 |
| nvidia-curand-cu12 | 10.3.2.106 |
| nvidia-cusolver-cu12 | 11.4.5.107 |
| nvidia-cusparse-cu12 | 12.1.0.106 |
| nvidia-nccl-cu12 | 2.20.5 |
| nvidia-nvjitlink-cu12 | 12.9.86 |
| nvidia-nvtx-cu12 | 12.1.105 |
| omnipath | 1.0.12 |
| opencv-python-headless | 4.10.0.84 |
| openpyxl | 3.1.5 |
| opentelemetry-api | 1.45.1 |
| opentelemetry-exporter-http-transport | 0.66b1 |
| opentelemetry-exporter-otlp-common | 0.66b1 |
| opentelemetry-exporter-otlp-proto-common | 1.45.1 |
| opentelemetry-exporter-otlp-proto-http | 1.45.1 |
| opentelemetry-proto | 1.45.1 |
| opentelemetry-sdk | 1.45.1 |
| opentelemetry-semantic-conventions | 0.66b1 |
| opt_einsum | 3.4.0 |
| optax | 0.2.5 |
| orbax-checkpoint | 0.11.5 |
| packaging | 26.3 |
| pandas | 2.2.2 |
| partd | 1.4.2 |
| patsy | 1.0.3 |
| pillow | 12.3.0 |
| PIMS | 0.7 |
| pip | 24.2 |
| platformdirs | 4.13.0 |
| propcache | 0.5.4 |
| protobuf | 7.36.2 |
| psutil | 7.2.2 |
| pyarrow | 26.0.0 |
| pydantic | 2.14.1 |
| pydantic_core | 2.50.1 |
| pydeseq2 | 0.5.1 |
| pydot | 4.0.1 |
| Pygments | 2.21.0 |
| pynndescent | 0.6.0 |
| pyparsing | 3.3.3 |
| pyro-api | 0.1.2 |
| pyro-ppl | 1.9.2 |
| pysam | 0.23.3 |
| python-dateutil | 2.9.0.post0 |
| pytorch-lightning | 2.6.6 |
| pytz | 2026.5 |
| PyYAML | 6.0.3 |
| rdkit | 2024.3.5 |
| regex | 2026.9.29 |
| requests | 2.34.2 |
| rich | 15.0.0 |
| roifile | 2026.2.10 |
| safetensors | 0.4.5 |
| scanorama | 1.7.4 |
| scanpy | 1.10.2 |
| scib | 1.1.7 |
| scikit-image | 0.24.0 |
| scikit-learn | 1.5.2 |
| scikit-misc | 0.5.3 |
| scipy | 1.13.1 |
| scvelo | 0.3.2 |
| scverse-misc | 0.0.3 |
| scvi-tools | 1.1.6.post2 |
| seaborn | 0.13.2 |
| sentencepiece | 0.2.0 |
| session-info2 | 0.4.1 |
| session_info | 1.0.1 |
| setuptools | 79.0.1 |
| simplejson | 4.2.0 |
| six | 1.17.0 |
| slicerator | 1.1.0 |
| sortedcontainers | 2.4.0 |
| squidpy | 1.2.2 |
| statsmodels | 0.14.6 |
| stdlib-list | 0.12.0 |
| sympy | 1.14.0 |
| tensorboard | 2.21.0 |
| tensorboard-data-server | 0.7.2 |
| tensorstore | 0.1.85 |
| texttable | 1.7.2 |
| threadpoolctl | 3.7.0 |
| tifffile | 2024.7.24 |
| tokenizers | 0.20.0 |
| toolz | 1.2.0 |
| torch | 2.4.1+cu121 |
| torch-geometric | 2.8.1 |
| torchmetrics | 1.9.0 |
| torchvision | 0.19.1+cu121 |
| tqdm | 4.70.1 |
| trackastra | 0.3.0 |
| transformers | 4.45.2 |
| treescope | 0.1.10 |
| triton | 3.0.0 |
| typeguard | 4.6.0 |
| typing-inspection | 0.4.4 |
| typing_extensions | 4.16.0 |
| tzdata | 2026.5 |
| umap-learn | 0.5.7 |
| urllib3 | 2.8.0 |
| validators | 0.36.0 |
| wandb | 0.30.0 |
| Werkzeug | 3.1.9 |
| wheel | 0.46.3 |
| wrapt | 2.5.1 |
| xarray | 2026.9.0 |
| xxhash | 4.0.1 |
| yarl | 1.25.1 |
| zarr | 2.18.7 |
| zipp | 4.1.2 |

### R 包（228 个）

| 包 | 版本 | 类别 |
|---|---|---|
| abind | 1.4-5 | 扩展包 |
| askpass | 1.1 | 扩展包 |
| base | 4.2.2 | R 基础包 |
| base64enc | 0.1-3 | 扩展包 |
| beachmat | 2.14.0 | 扩展包 |
| beeswarm | 0.4.0 | 扩展包 |
| BH | 1.74.0-1 | 扩展包 |
| Biobase | 2.58.0 | 扩展包 |
| BiocGenerics | 0.44.0 | 扩展包 |
| BiocManager | 1.30.20 | 扩展包 |
| BiocNeighbors | 1.16.0 | 扩展包 |
| BiocParallel | 1.32.5 | 扩展包 |
| BiocSingular | 1.14.0 | 扩展包 |
| bitops | 1.0-7 | 扩展包 |
| bluster | 1.8.0 | 扩展包 |
| bslib | 0.4.2 | 扩展包 |
| cachem | 1.0.7 | 扩展包 |
| Cairo | 1.6-0 | 扩展包 |
| caTools | 1.18.2 | 扩展包 |
| circlize | 0.4.15 | 扩展包 |
| cli | 3.6.0 | 扩展包 |
| clue | 0.3-64 | 扩展包 |
| cluster | 2.1.4 | R 推荐包 |
| codetools | 0.2-19 | R 推荐包 |
| colorspace | 2.1-0 | 扩展包 |
| colourpicker | 1.2.0 | 扩展包 |
| commonmark | 1.8.1 | 扩展包 |
| compiler | 4.2.2 | R 基础包 |
| ComplexHeatmap | 2.14.0 | 扩展包 |
| cowplot | 1.1.1 | 扩展包 |
| cpp11 | 0.4.3 | 扩展包 |
| crayon | 1.5.2 | 扩展包 |
| crosstalk | 1.2.0 | 扩展包 |
| curl | 5.0.0 | 扩展包 |
| data.table | 1.14.8 | 扩展包 |
| datasets | 4.2.2 | R 基础包 |
| DelayedArray | 0.24.0 | 扩展包 |
| DelayedMatrixStats | 1.20.0 | 扩展包 |
| deldir | 1.0-6 | 扩展包 |
| digest | 0.6.31 | 扩展包 |
| doParallel | 1.0.17 | 扩展包 |
| dplyr | 1.0.10 | 扩展包 |
| dqrng | 0.3.0 | 扩展包 |
| DropletUtils | 1.18.1 | 扩展包 |
| edgeR | 3.40.2 | 扩展包 |
| ellipsis | 0.3.2 | 扩展包 |
| evaluate | 0.20 | 扩展包 |
| fansi | 1.0.4 | 扩展包 |
| farver | 2.1.1 | 扩展包 |
| fastmap | 1.1.1 | 扩展包 |
| fitdistrplus | 1.1-8 | 扩展包 |
| FNN | 1.1.3.1 | 扩展包 |
| fontawesome | 0.5.0 | 扩展包 |
| foreach | 1.5.2 | 扩展包 |
| formatR | 1.14 | 扩展包 |
| fs | 1.6.1 | 扩展包 |
| futile.logger | 1.4.3 | 扩展包 |
| futile.options | 1.0.1 | 扩展包 |
| future | 1.31.0 | 扩展包 |
| future.apply | 1.10.0 | 扩展包 |
| gdata | 2.18.0.1 | 扩展包 |
| generics | 0.1.3 | 扩展包 |
| GenomeInfoDb | 1.34.9 | 扩展包 |
| GenomeInfoDbData | 1.2.9 | 扩展包 |
| GenomicRanges | 1.50.2 | 扩展包 |
| GetoptLong | 1.0.5 | 扩展包 |
| ggbeeswarm | 0.7.1 | 扩展包 |
| ggplot2 | 3.4.1 | 扩展包 |
| ggrastr | 1.0.1 | 扩展包 |
| ggrepel | 0.9.3 | 扩展包 |
| ggridges | 0.5.4 | 扩展包 |
| GlobalOptions | 0.1.2 | 扩展包 |
| globals | 0.16.2 | 扩展包 |
| glue | 1.6.2 | 扩展包 |
| goftest | 1.2-3 | 扩展包 |
| gplots | 3.1.3 | 扩展包 |
| graphics | 4.2.2 | R 基础包 |
| grDevices | 4.2.2 | R 基础包 |
| grid | 4.2.2 | R 基础包 |
| gridExtra | 2.3 | 扩展包 |
| gtable | 0.3.1 | 扩展包 |
| gtools | 3.9.4 | 扩展包 |
| HDF5Array | 1.26.0 | 扩展包 |
| here | 1.0.1 | 扩展包 |
| highr | 0.10 | 扩展包 |
| htmltools | 0.5.4 | 扩展包 |
| htmlwidgets | 1.6.1 | 扩展包 |
| httpuv | 1.6.9 | 扩展包 |
| httr | 1.4.5 | 扩展包 |
| ica | 1.0-3 | 扩展包 |
| igraph | 1.3.5 | 扩展包 |
| IRanges | 2.32.0 | 扩展包 |
| irlba | 2.3.5.1 | 扩展包 |
| isoband | 0.2.7 | 扩展包 |
| iterators | 1.0.14 | 扩展包 |
| jquerylib | 0.1.4 | 扩展包 |
| jsonlite | 1.8.4 | 扩展包 |
| KernSmooth | 2.23-20 | R 推荐包 |
| knitr | 1.42 | 扩展包 |
| labeling | 0.4.2 | 扩展包 |
| lambda.r | 1.2.4 | 扩展包 |
| later | 1.3.0 | 扩展包 |
| lattice | 0.20-45 | R 推荐包 |
| lazyeval | 0.2.2 | 扩展包 |
| leiden | 0.4.3 | 扩展包 |
| lifecycle | 1.0.3 | 扩展包 |
| limma | 3.54.1 | 扩展包 |
| listenv | 0.9.0 | 扩展包 |
| littler | 0.3.17 | 扩展包 |
| lmtest | 0.9-40 | 扩展包 |
| locfit | 1.5-9.7 | 扩展包 |
| magrittr | 2.0.3 | 扩展包 |
| MASS | 7.3-58.2 | R 推荐包 |
| Matrix | 1.5-3 | R 推荐包 |
| MatrixGenerics | 1.10.0 | 扩展包 |
| matrixStats | 0.63.0 | 扩展包 |
| memoise | 2.0.1 | 扩展包 |
| metapod | 1.6.0 | 扩展包 |
| methods | 4.2.2 | R 基础包 |
| mgcv | 1.8-41 | R 推荐包 |
| mime | 0.12 | 扩展包 |
| miniUI | 0.1.1.1 | 扩展包 |
| munsell | 0.5.0 | 扩展包 |
| nlme | 3.1-162 | R 推荐包 |
| openssl | 2.0.5 | 扩展包 |
| parallel | 4.2.2 | R 基础包 |
| parallelly | 1.34.0 | 扩展包 |
| patchwork | 1.1.2 | 扩展包 |
| pbapply | 1.7-0 | 扩展包 |
| pheatmap | 1.0.12 | 扩展包 |
| pillar | 1.8.1 | 扩展包 |
| pkgconfig | 2.0.3 | 扩展包 |
| pkgKitten | 0.2.2 | 扩展包 |
| plotly | 4.10.1 | 扩展包 |
| plyr | 1.8.8 | 扩展包 |
| png | 0.1-8 | 扩展包 |
| polyclip | 1.10-4 | 扩展包 |
| progressr | 0.13.0 | 扩展包 |
| promises | 1.2.0.1 | 扩展包 |
| purrr | 1.0.1 | 扩展包 |
| R.methodsS3 | 1.8.2 | 扩展包 |
| R.oo | 1.25.0 | 扩展包 |
| R.utils | 2.12.2 | 扩展包 |
| R6 | 2.5.1 | 扩展包 |
| ragg | 1.2.5 | 扩展包 |
| RANN | 2.6.1 | 扩展包 |
| rappdirs | 0.3.3 | 扩展包 |
| RColorBrewer | 1.1-3 | 扩展包 |
| Rcpp | 1.0.10 | 扩展包 |
| RcppAnnoy | 0.0.20 | 扩展包 |
| RcppArmadillo | 0.12.0.1.0 | 扩展包 |
| RcppEigen | 0.3.3.9.3 | 扩展包 |
| RcppHNSW | 0.4.1 | 扩展包 |
| RcppML | 0.3.7 | 扩展包 |
| RcppProgress | 0.4.2 | 扩展包 |
| RcppTOML | 0.2.2 | 扩展包 |
| RCurl | 1.98-1.10 | 扩展包 |
| reshape2 | 1.4.4 | 扩展包 |
| reticulate | 1.28 | 扩展包 |
| rhdf5 | 2.42.0 | 扩展包 |
| rhdf5filters | 1.10.0 | 扩展包 |
| Rhdf5lib | 1.20.0 | 扩展包 |
| rjson | 0.2.21 | 扩展包 |
| rlang | 1.0.6 | 扩展包 |
| rmarkdown | 2.20 | 扩展包 |
| ROCR | 1.0-11 | 扩展包 |
| rprojroot | 2.0.3 | 扩展包 |
| rsvd | 1.0.5 | 扩展包 |
| Rtsne | 0.16 | 扩展包 |
| S4Vectors | 0.36.1 | 扩展包 |
| sass | 0.4.5 | 扩展包 |
| ScaledMatrix | 1.6.0 | 扩展包 |
| scales | 1.2.1 | 扩展包 |
| scater | 1.26.1 | 扩展包 |
| scattermore | 0.8 | 扩展包 |
| scran | 1.26.2 | 扩展包 |
| sctransform | 0.3.5 | 扩展包 |
| scuttle | 1.8.4 | 扩展包 |
| Seurat | 4.3.0 | 扩展包 |
| SeuratObject | 4.1.3 | 扩展包 |
| shape | 1.4.6 | 扩展包 |
| shiny | 1.7.4 | 扩展包 |
| shinyjs | 2.1.0 | 扩展包 |
| SingleCellExperiment | 1.20.0 | 扩展包 |
| SingleR | 2.0.0 | 扩展包 |
| sitmo | 2.0.2 | 扩展包 |
| snow | 0.4-4 | 扩展包 |
| sourcetools | 0.1.7-1 | 扩展包 |
| sp | 1.6-0 | 扩展包 |
| sparseMatrixStats | 1.10.0 | 扩展包 |
| spatstat.data | 3.0-0 | 扩展包 |
| spatstat.explore | 3.0-6 | 扩展包 |
| spatstat.geom | 3.0-6 | 扩展包 |
| spatstat.random | 3.1-3 | 扩展包 |
| spatstat.sparse | 3.0-0 | 扩展包 |
| spatstat.utils | 3.0-1 | 扩展包 |
| splines | 4.2.2 | R 基础包 |
| statmod | 1.5.0 | 扩展包 |
| stats | 4.2.2 | R 基础包 |
| stats4 | 4.2.2 | R 基础包 |
| stringi | 1.7.12 | 扩展包 |
| stringr | 1.5.0 | 扩展包 |
| SummarizedExperiment | 1.28.0 | 扩展包 |
| survival | 3.5-3 | R 推荐包 |
| sys | 3.4.1 | 扩展包 |
| systemfonts | 1.0.4 | 扩展包 |
| tcltk | 4.2.2 | R 基础包 |
| tensor | 1.5 | 扩展包 |
| textshaping | 0.3.6 | 扩展包 |
| tibble | 3.1.8 | 扩展包 |
| tidyr | 1.3.0 | 扩展包 |
| tidyselect | 1.2.0 | 扩展包 |
| tinytex | 0.44 | 扩展包 |
| tools | 4.2.2 | R 基础包 |
| utf8 | 1.2.3 | 扩展包 |
| utils | 4.2.2 | R 基础包 |
| uwot | 0.1.14 | 扩展包 |
| vctrs | 0.5.2 | 扩展包 |
| vipor | 0.4.5 | 扩展包 |
| viridis | 0.6.2 | 扩展包 |
| viridisLite | 0.4.1 | 扩展包 |
| withr | 2.5.0 | 扩展包 |
| xfun | 0.37 | 扩展包 |
| xtable | 1.8-4 | 扩展包 |
| XVector | 0.38.0 | 扩展包 |
| yaml | 2.3.7 | 扩展包 |
| zlibbioc | 1.44.0 | 扩展包 |
| zoo | 1.8-11 | 扩展包 |

### 系统包（apt，650 个）

<details><summary>展开</summary>

```text
adduser 3.134
apt 2.6.1
base-files 12.4+deb12u15
base-passwd 3.6.1
bash 5.2.15-2+b13
bcftools 1.16-1
bedtools 2.30.0+dfsg-3
binutils 2.40-2
binutils-common 2.40-2
binutils-x86-64-linux-gnu 2.40-2
bsdutils 1:2.38.1-5+deb12u3
build-essential 12.9
bzip2 1.0.8-5+b1
ca-certificates 20250419~deb12u1
coreutils 9.1-1
cpp 4:12.2.0-3
cpp-12 12.2.0-14+deb12u1
curl 7.88.1-10+deb12u15
dash 0.5.12-2
debconf 1.5.82
debian-archive-keyring 2023.3+deb12u2
debianutils 5.7-0.5~deb12u1
diffutils 1:3.8-4
dpkg 1.21.23
dpkg-dev 1.21.23
e2fsprogs 1.47.0-2+b2
ffmpeg 7:5.1.9-0+deb12u1
findutils 4.9.0-4
fontconfig 2.14.1-4
fontconfig-config 2.14.1-4
fonts-dejavu-core 2.37-6
fonts-font-awesome 5.0.10+really4.7.0~dfsg-4.1
fonts-glyphicons-halflings 1.009~3.4.1+dfsg-3+deb12u2
fonts-mathjax 2.7.9+dfsg-1
g++ 4:12.2.0-3
g++-12 12.2.0-14+deb12u1
gcc 4:12.2.0-3
gcc-12 12.2.0-14+deb12u1
gcc-12-base 12.2.0-14+deb12u1
git 1:2.39.5-0+deb12u3
git-man 1:2.39.5-0+deb12u3
gpgv 2.2.40-1.1+deb12u2
grep 3.8-5
gzip 1.12-1
hdf5-helpers 1.10.8+repack1-1
hostname 3.23+nmu1
init-system-helpers 1.65.2+deb12u1
javascript-common 11+nmu1
jq 1.6-2.1+deb12u2
libacl1 2.3.1-3
libaec-dev 1.0.6-1+b1
libaec0 1.0.6-1+b1
libaom3 3.6.0-1+deb12u3
libapt-pkg6.0 2.6.1
libasan8 12.2.0-14+deb12u1
libasound2 1.2.8-1+b1
libasound2-data 1.2.8-1
libass9 1:0.17.1-1+deb12u1
libasyncns0 0.8-6+b3
libatomic1 12.2.0-14+deb12u1
libattr1 1:2.5.1-4
libaudit-common 1:3.0.9-1
libaudit1 1:3.0.9-1
libavc1394-0 0.5.4-5
libavcodec59 7:5.1.9-0+deb12u1
libavdevice59 7:5.1.9-0+deb12u1
libavfilter8 7:5.1.9-0+deb12u1
libavformat59 7:5.1.9-0+deb12u1
libavutil57 7:5.1.9-0+deb12u1
libbinutils 2.40-2
libblas3 3.11.0-2
libblkid1 2.38.1-5+deb12u3
libblosc1 1.21.3+ds-1
libbluray2 1:1.3.4-1
libboost-dev 1.74.0.3
libboost1.74-dev 1.74.0+ds1-21
libbrotli1 1.0.9-2+b6
libbs2b0 3.1.0+dfsg-7
libbsd0 0.11.7-2
libbz2-1.0 1.0.8-5+b1
libc-bin 2.36-9+deb12u14
libc-dev-bin 2.36-9+deb12u14
libc6 2.36-9+deb12u14
libc6-dev 2.36-9+deb12u14
libcaca0 0.99.beta20-3+deb12u1
libcairo-gobject2 1.16.0-7
libcairo2 1.16.0-7
libcap-ng0 0.8.3-1+b3
libcap2 1:2.66-4+deb12u3+b1
libcc1-0 12.2.0-14+deb12u1
libcdio-cdda2 10.2+2.0.1-1
libcdio-paranoia2 10.2+2.0.1-1
libcdio19 2.1.0-4
libchromaprint1 1.5.1-2+b1
libcjson1 1.7.15-1+deb12u4
libcodec2-1.0 1.0.5-1
libcom-err2 1.47.0-2+b2
libcrypt-dev 1:4.4.33-2
libcrypt1 1:4.4.33-2
libctf-nobfd0 2.40-2
libctf0 2.40-2
libcurl3-gnutls 7.88.1-10+deb12u15
libcurl4 7.88.1-10+deb12u15
libcurl4-openssl-dev 7.88.1-10+deb12u15
libdatrie1 0.2.13-2+b1
libdav1d6 1.0.0-2+deb12u1
libdb5.3 5.3.28+dfsg2-1
libdbus-1-3 1.14.10-1~deb12u1
libdc1394-25 2.2.6-4
libdebconfclient0 0.270
libdecor-0-0 0.1.1-2
libdeflate0 1.14-1
libdpkg-perl 1.21.23
libdrm-amdgpu1 2.4.114-1+b1
libdrm-common 2.4.114-1
libdrm-intel1 2.4.114-1+b1
libdrm-nouveau2 2.4.114-1+b1
libdrm-radeon1 2.4.114-1+b1
libdrm2 2.4.114-1+b1
libedit2 3.1-20221030-2
libelf1 0.188-2.1
libepoxy0 1.5.10-1
liberror-perl 0.17029-2
libexpat1 2.5.0-1+deb12u4
libext2fs2 1.47.0-2+b2
libffi8 3.4.4-1
libflac12 1.4.2+ds-2
libflite1 2.2-5
libfontconfig1 2.14.1-4
libfreetype6 2.12.1+dfsg-5+deb12u4
libfribidi0 1.0.8-2.1
libgbm1 22.3.6-1+deb12u2
libgcc-12-dev 12.2.0-14+deb12u1
libgcc-s1 12.2.0-14+deb12u1
libgcrypt20 1.10.1-3+deb12u1
libgdbm-compat4 1.23-3
libgdbm6 1.23-3
libgdk-pixbuf-2.0-0 2.42.10+dfsg-1+deb12u4
libgdk-pixbuf2.0-common 2.42.10+dfsg-1+deb12u4
libgfortran5 12.2.0-14+deb12u1
libgl1 1.6.0-1
libgl1-mesa-dri 22.3.6-1+deb12u2
libglapi-mesa 22.3.6-1+deb12u2
libglib2.0-0 2.74.6-2+deb12u9
libglvnd0 1.6.0-1
libglx-mesa0 22.3.6-1+deb12u2
libglx0 1.6.0-1
libgme0 0.6.3-6
libgmp10 2:6.2.1+dfsg1-1.1
libgnutls30 3.7.9-2+deb12u7
libgomp1 12.2.0-14+deb12u1
libgpg-error0 1.46-1
libgprofng0 2.40-2
libgraphite2-3 1.3.14-1+deb12u1
libgsm1 1.0.22-1
libgssapi-krb5-2 1.20.1-2+deb12u5
libharfbuzz0b 6.0.0+dfsg-3
libhdf5-103-1 1.10.8+repack1-1
libhdf5-cpp-103-1 1.10.8+repack1-1
libhdf5-dev 1.10.8+repack1-1
libhdf5-fortran-102 1.10.8+repack1-1
libhdf5-hl-100 1.10.8+repack1-1
libhdf5-hl-cpp-100 1.10.8+repack1-1
libhdf5-hl-fortran-100 1.10.8+repack1-1
libhogweed6 3.8.1-2
libhts3 1.16+ds-3
libhtscodecs2 1.3.0-4
libhwy1 1.0.3-3+deb12u1
libice6 2:1.0.10-1
libicu72 72.1-3+deb12u1
libidn2-0 2.3.3-1+b1
libiec61883-0 1.2.0-6+b1
libisl23 0.25-1.1
libitm1 12.2.0-14+deb12u1
libjack-jackd2-0 1.9.21~dfsg-3
libjansson4 2.14-2
libjbig0 2.1-6.1
libjpeg-dev 1:2.1.5-2
libjpeg62-turbo 1:2.1.5-2
libjpeg62-turbo-dev 1:2.1.5-2
libjq1 1.6-2.1+deb12u2
libjs-bootstrap 3.4.1+dfsg-3+deb12u2
libjs-bootstrap4 4.6.1+dfsg1-4+deb12u1
libjs-d3 3.5.17-4
libjs-es5-shim 4.6.7-2
libjs-highlight.js 9.18.5+dfsg1-2
libjs-jquery 3.6.1+dfsg+~3.5.14-1
libjs-jquery-datatables 1.11.5+dfsg-2
libjs-jquery-selectize.js 0.12.6+dfsg-1.1
libjs-jquery-ui 1.13.2+dfsg-1
libjs-json 0~20221030+~1.0.8-1
libjs-mathjax 2.7.9+dfsg-1
libjs-microplugin.js 0.0.3+dfsg-1.1
libjs-modernizr 2.6.2+ds1-5
libjs-popper.js 1.16.1+ds-6
libjs-prettify 2015.12.04+dfsg-1.1
libjs-sifter.js 0.6.0+dfsg-3
libjs-twitter-bootstrap-datepicker 1.3.1+dfsg1-4.1
libjxl0.7 0.7.0-10+deb12u1
libk5crypto3 1.20.1-2+deb12u5
libkeyutils1 1.6.3-2
libkrb5-3 1.20.1-2+deb12u5
libkrb5support0 1.20.1-2+deb12u5
liblapack3 3.11.0-2
liblcms2-2 2.14-2+deb12u1
libldap-2.5-0 2.5.13+dfsg-5
liblerc4 4.0.0+ds-2
liblilv-0-0 0.24.14-1
libllvm15 1:15.0.6-4+b1
liblsan0 12.2.0-14+deb12u1
liblua5.3-0 5.3.6-2
liblz4-1 1.9.4-1
liblzma5 5.4.1-1+deb12u2
libmbedcrypto7 2.28.3-1
libmd0 1.0.4-2
libmfx1 22.5.4-1
libmount1 2.38.1-5+deb12u3
libmp3lame0 3.100-6
libmpc3 1.3.1-1
libmpfr6 4.2.0-1
libmpg123-0 1.31.2-1+deb12u1
libmysofa1 1.3.1~dfsg0-1
libncurses6 6.4-4
libncursesw6 6.4-4
libnettle8 3.8.1-2
libnghttp2-14 1.52.0-1+deb12u3
libnorm1 1.5.9+dfsg-2
libnsl-dev 1.3.0-2
libnsl2 1.3.0-2
libnuma1 2.0.16-1
libogg0 1.3.5-3
libonig5 6.9.8-1
libopenal-data 1:1.19.1-2
libopenal1 1:1.19.1-2
libopenjp2-7 2.5.0-2+deb12u3
libopenmpt0 0.6.9-1
libopus0 1.3.1-3
libp11-kit0 0.24.1-2
libpam-modules 1.5.2-6+deb12u2
libpam-modules-bin 1.5.2-6+deb12u2
libpam-runtime 1.5.2-6+deb12u2
libpam0g 1.5.2-6+deb12u2
libpango-1.0-0 1.50.12+ds-1
libpangocairo-1.0-0 1.50.12+ds-1
libpangoft2-1.0-0 1.50.12+ds-1
libpaper-utils 1.1.29
libpaper1 1.1.29
libpciaccess0 0.17-2
libpcre2-8-0 10.42-1+deb12u2
libperl5.36 5.36.0-7+deb12u4
libpgm-5.3-0 5.3.128~dfsg-2
libpixman-1-0 0.42.2-1
libplacebo208 4.208.0-3
libpng16-16 1.6.39-2+deb12u6
libpocketsphinx3 0.8+5prealpha+1-15
libpostproc56 7:5.1.9-0+deb12u1
libpsl5 0.21.2-1
libpulse0 16.1+dfsg1-2+b1
libpython3-stdlib 3.11.2-1+b1
libpython3.11-minimal 3.11.2-6+deb12u9
libpython3.11-stdlib 3.11.2-6+deb12u9
libquadmath0 12.2.0-14+deb12u1
librabbitmq4 0.11.0-1+deb12u3
librav1e0 0.5.1-6
libraw1394-11 2.1.2-2
libreadline8 8.2-1.3
librist4 0.2.7+dfsg-1
librsvg2-2 2.54.7+dfsg-1~deb12u1
librtmp1 2.4+20151223.gitfa8646d.1-2+b2
librubberband2 3.1.2+dfsg0-1
libsamplerate0 0.2.2-3
libsasl2-2 2.1.28+dfsg-10
libsasl2-modules-db 2.1.28+dfsg-10
libsdl2-2.0-0 2.26.5+dfsg-1
libseccomp2 2.5.4-1+deb12u1
libselinux1 3.4-1+b6
libsemanage-common 3.4-1
libsemanage2 3.4-1+b5
libsensors-config 1:3.6.0-7.1
libsensors5 1:3.6.0-7.1
libsepol2 3.4-2.1
libserd-0-0 0.30.16-1
libshine3 3.1.1-2
libslang2 2.3.3-3
libsm6 2:1.2.3-1
libsmartcols1 2.38.1-5+deb12u3
libsnappy1v5 1.1.9-3
libsndfile1 1.2.0-1+deb12u1
libsndio7.0 1.9.0-0.3+b2
libsodium23 1.0.18-1+deb12u1
libsord-0-0 0.16.14+git221008-1
libsoxr0 0.1.3-4
libspeex1 1.2.1-2
libsphinxbase3 0.8+5prealpha+1-16
libsqlite3-0 3.40.1-2+deb12u2
libsratom-0-0 0.6.14-1
libsrt1.5-gnutls 1.5.1-1+deb12u1
libss2 1.47.0-2+b2
libssh-gcrypt-4 0.10.6-0+deb12u2
libssh2-1 1.10.0-3+deb12u1
libssl-dev 3.0.22-1~deb12u1
libssl3 3.0.22-1~deb12u1
libstdc++-12-dev 12.2.0-14+deb12u1
libstdc++6 12.2.0-14+deb12u1
libsvtav1enc1 1.4.1+dfsg-1
libswresample4 7:5.1.9-0+deb12u1
libswscale6 7:5.1.9-0+deb12u1
libsystemd0 252.39-1~deb12u2
libsz2 1.0.6-1+b1
libtasn1-6 4.19.0-2+deb12u1
libtcl8.6 8.6.13+dfsg-2
libthai-data 0.1.29-1
libthai0 0.1.29-1
libtheora0 1.1.1+dfsg.1-16.1+deb12u1
libtiff6 4.5.0-6+deb12u4
libtinfo6 6.4-4
libtirpc-common 1.3.3+ds-1
libtirpc-dev 1.3.3+ds-1
libtirpc3 1.3.3+ds-1
libtk8.6 8.6.13-2
libtsan2 12.2.0-14+deb12u1
libtwolame0 0.4.0-2
libubsan1 12.2.0-14+deb12u1
libudev1 252.39-1~deb12u2
libudfread0 1.1.2-1
libunistring2 1.0-2
libusb-1.0-0 2:1.0.26-1
libuuid1 2.38.1-5+deb12u3
libuv1 1.44.2-1+deb12u1
libva-drm2 2.17.0-1
libva-x11-2 2.17.0-1
libva2 2.17.0-1
libvdpau1 1.5-2
libvidstab1.1 1.1.0-2+b1
libvorbis0a 1.3.7-1
libvorbisenc2 1.3.7-1
libvorbisfile3 1.3.7-1
libvpx7 1.12.0-1+deb12u5
libvulkan1 1.3.239.0-1
libwayland-client0 1.21.0-1
libwayland-cursor0 1.21.0-1
libwayland-egl1 1.21.0-1
libwayland-server0 1.21.0-1
libwebp7 1.2.4-0.2+deb12u1
libwebpmux3 1.2.4-0.2+deb12u1
libx11-6 2:1.8.4-2+deb12u2
libx11-data 2:1.8.4-2+deb12u2
libx11-xcb1 2:1.8.4-2+deb12u2
libx264-164 2:0.164.3095+gitbaee400-3
libx265-199 3.5-2+b1
libxau6 1:1.0.9-1
libxcb-dri2-0 1.15-1
libxcb-dri3-0 1.15-1
libxcb-glx0 1.15-1
libxcb-present0 1.15-1
libxcb-randr0 1.15-1
libxcb-render0 1.15-1
libxcb-shape0 1.15-1
libxcb-shm0 1.15-1
libxcb-sync1 1.15-1
libxcb-xfixes0 1.15-1
libxcb1 1.15-1
libxcursor1 1:1.2.1-1
libxdmcp6 1:1.1.2-3
libxext6 2:1.3.4-1+b1
libxfixes3 1:6.0.0-2
libxft2 2.3.6-1
libxi6 2:1.8-1+b1
libxkbcommon0 1.5.0-1
libxml2 2.9.14+dfsg-1.3~deb12u6
libxrandr2 2:1.5.2-2+b1
libxrender1 1:0.9.10-1.1
libxshmfence1 1.3-1
libxss1 1:1.2.3-1
libxt6 1:1.2.1-1.1
libxv1 2:1.0.11-1.1
libxvidcore4 2:1.3.7-1
libxxf86vm1 1:1.1.4-1+b2
libxxhash0 0.8.1-1
libyaml-0-2 0.2.5-1
libz3-4 4.8.12-3.1
libzimg2 3.0.4+ds1-1
libzmq5 4.3.4-6
libzstd1 1.5.4+dfsg2-5
libzvbi-common 0.2.41-1+deb12u1
libzvbi0 0.2.41-1+deb12u1
linux-libc-dev 6.1.187-1
littler 0.3.17-1
login 1:4.13+dfsg1-1+deb12u2
logsave 1.47.0-2+b2
make 4.3-4.1
mawk 1.3.4.20200120-3.1
media-types 10.0.0
mount 2.38.1-5+deb12u3
ncurses-base 6.4-4
ncurses-bin 6.4-4
netbase 6.4
node-bootstrap-sass 3.4.3-2
node-html5shiv 3.7.3+dfsg-5
node-normalize.css 8.0.1-5
ocl-icd-libopencl1 2.3.1-1
openssl 3.0.22-1~deb12u1
pandoc 2.17.1.1-2~deb12u1
pandoc-data 2.17.1.1-2~deb12u1
passwd 1:4.13+dfsg1-1+deb12u2
patch 2.7.6-7
perl 5.36.0-7+deb12u4
perl-base 5.36.0-7+deb12u4
perl-modules-5.36 5.36.0-7+deb12u4
python3 3.11.2-1+b1
python3-minimal 3.11.2-1+b1
python3.11 3.11.2-6+deb12u9
python3.11-minimal 3.11.2-6+deb12u9
r-base-core 4.2.2.20221110-2
r-bioc-beachmat 2.14.0+ds-1
r-bioc-biobase 2.58.0-2
r-bioc-biocgenerics 0.44.0-2
r-bioc-biocneighbors 1.16.0+ds-1
r-bioc-biocparallel 1.32.5-1
r-bioc-biocsingular 1.14.0+ds-2
r-bioc-bluster 1.8.0+dfsg-2
r-bioc-complexheatmap 2.14.0+dfsg-1
r-bioc-delayedarray 0.24.0+dfsg-1
r-bioc-delayedmatrixstats 1.20.0+ds-1
r-bioc-dropletutils 1.18.1+ds-1
r-bioc-edger 3.40.2+dfsg-1
r-bioc-genomeinfodb 1.34.9-1
r-bioc-genomeinfodbdata 1.2.9-1
r-bioc-genomicranges 1.50.2+dfsg-1
r-bioc-hdf5array 1.26.0-1
r-bioc-iranges 2.32.0-1
r-bioc-limma 3.54.1+dfsg-1
r-bioc-matrixgenerics 1.10.0-1
r-bioc-metapod 1.6.0+dfsg-2
r-bioc-rhdf5 2.42.0+dfsg-1
r-bioc-rhdf5filters 1.10.0+dfsg-1
r-bioc-rhdf5lib 1.20.0+dfsg-1
r-bioc-s4vectors 0.36.1-1
r-bioc-scaledmatrix 1.6.0+dfsg-1
r-bioc-scater 1.26.1+ds-1
r-bioc-scran 1.26.2+dfsg-1
r-bioc-scuttle 1.8.4+dfsg-1
r-bioc-singlecellexperiment 1.20.0+ds-1
r-bioc-singler 2.0.0+ds-1
r-bioc-sparsematrixstats 1.10.0+dfsg-1
r-bioc-summarizedexperiment 1.28.0+dfsg-1
r-bioc-xvector 0.38.0-1
r-bioc-zlibbioc 1.44.0+dfsg-1
r-cran-abind 1.4-5-2
r-cran-askpass 1.1-2
r-cran-base64enc 0.1-3-3
r-cran-beeswarm 0.4.0-1+b1
r-cran-bh 1.74.0-2
r-cran-biocmanager 1.30.20+dfsg-1
r-cran-bitops 1.0-7-1
r-cran-bslib 0.4.2+dfsg-1
r-cran-cachem 1.0.7-1
r-cran-cairo 1.6-0-1
r-cran-catools 1.18.2-1+b1
r-cran-circlize 0.4.15-1
r-cran-cli 3.6.0-1
r-cran-clue 0.3-64-1
r-cran-cluster 2.1.4-1
r-cran-codetools 0.2-19-1
r-cran-colorspace 2.1-0+dfsg-1
r-cran-colourpicker 1.2.0+dfsg-1
r-cran-commonmark 1.8.1-1
r-cran-cowplot 1.1.1+dfsg-1
r-cran-cpp11 0.4.3-1
r-cran-crayon 1.5.2-1
r-cran-crosstalk 1.2.0+dfsg-1
r-cran-curl 5.0.0+dfsg-1
r-cran-data.table 1.14.8+dfsg-1
r-cran-deldir 1.0-6-1+b1
r-cran-digest 0.6.31-1
r-cran-doparallel 1.0.17-1
r-cran-dplyr 1.0.10-1
r-cran-dqrng 0.3.0+dfsg-1+b1
r-cran-ellipsis 0.3.2-2
r-cran-evaluate 0.20-1
r-cran-fansi 1.0.4-1
r-cran-farver 2.1.1-1
r-cran-fastmap 1.1.1-1
r-cran-fitdistrplus 1.1-8-1
r-cran-fnn 1.1.3.1-1
r-cran-fontawesome 0.5.0-1
r-cran-foreach 1.5.2-1
r-cran-formatr 1.14-1
r-cran-fs 1.6.1+dfsg-1
r-cran-futile.logger 1.4.3-4
r-cran-futile.options 1.0.1-3
r-cran-future 1.31.0+dfsg-1
r-cran-future.apply 1.10.0+dfsg-1
r-cran-gdata 2.18.0.1-1
r-cran-generics 0.1.3-1
r-cran-getoptlong 1.0.5+dfsg-1
r-cran-ggbeeswarm 0.7.1-1
r-cran-ggplot2 3.4.1+dfsg-1
r-cran-ggrastr 1.0.1-2
r-cran-ggrepel 0.9.3-1
r-cran-ggridges 0.5.4-1
r-cran-globaloptions 0.1.2-1
r-cran-globals 0.16.2-1
r-cran-glue 1.6.2-1
r-cran-goftest 1.2-3-1+b1
r-cran-gplots 3.1.3-1
r-cran-gridextra 2.3-3
r-cran-gtable 0.3.1+dfsg-1
r-cran-gtools 3.9.4-1
r-cran-here 1.0.1-2
r-cran-highr 0.10+dfsg-1
r-cran-htmltools 0.5.4-1
r-cran-htmlwidgets 1.6.1+dfsg-1
r-cran-httpuv 1.6.9+dfsg-1
r-cran-httr 1.4.5+dfsg-1
r-cran-ica 1.0-3-1
r-cran-igraph 1.3.5-3
r-cran-irlba 2.3.5.1-1
r-cran-isoband 0.2.7-1
r-cran-iterators 1.0.14-1
r-cran-jquerylib 0.1.4+dfsg-4
r-cran-jsonlite 1.8.4+dfsg-1
r-cran-kernsmooth 2.23-20-1
r-cran-knitr 1.42+dfsg-1
r-cran-labeling 0.4.2-1
r-cran-lambda.r 1.2.4-2
r-cran-later 1.3.0+dfsg-1+b1
r-cran-lattice 0.20-45-3
r-cran-lazyeval 0.2.2-1+b1
r-cran-leiden 0.4.3+dfsg-1
r-cran-lifecycle 1.0.3+dfsg-1
r-cran-listenv 0.9.0+dfsg-1
r-cran-littler 0.3.17-1
r-cran-lmtest 0.9.40-1
r-cran-locfit 1.5-9.7-1
r-cran-magrittr 2.0.3-1
r-cran-mass 7.3-58.2-1
r-cran-matrix 1.5-3-1
r-cran-matrixstats 0.63.0-1
r-cran-memoise 2.0.1-1
r-cran-mgcv 1.8-41-1
r-cran-mime 0.12-1+b1
r-cran-miniui 0.1.1.1-3
r-cran-munsell 0.5.0-2
r-cran-nlme 3.1.162-1
r-cran-openssl 2.0.5+dfsg-1
r-cran-parallelly 1.34.0-1
r-cran-patchwork 1.1.2-1
r-cran-pbapply 1.7-0-1
r-cran-pheatmap 1.0.12-2
r-cran-pillar 1.8.1+dfsg-1
r-cran-pkgconfig 2.0.3-2
r-cran-pkgkitten 0.2.2-2
r-cran-plotly 4.10.1+dfsg-2
r-cran-plyr 1.8.8-1
r-cran-png 0.1-8-1
r-cran-polyclip 1.10-4-1
r-cran-progressr 0.13.0-1
r-cran-promises 1.2.0.1+dfsg-1
r-cran-purrr 1.0.1-1
r-cran-r.methodss3 1.8.2-1
r-cran-r.oo 1.25.0-1
r-cran-r.utils 2.12.2-1
r-cran-r6 2.5.1-1
r-cran-ragg 1.2.5-1
r-cran-rann 2.6.1-1+b2
r-cran-rappdirs 0.3.3-1
r-cran-rcolorbrewer 1.1-3-1
r-cran-rcpp 1.0.10-1
r-cran-rcppannoy 0.0.20-1
r-cran-rcpparmadillo 0.12.0.1.0-1
r-cran-rcppeigen 0.3.3.9.3-1
r-cran-rcpphnsw 0.4.1+ds-2
r-cran-rcppml 0.3.7-2
r-cran-rcppprogress 0.4.2-2
r-cran-rcpptoml 0.2.2+dfsg-1
r-cran-rcurl 1.98-1.10+dfsg-1
r-cran-reshape2 1.4.4-2
r-cran-reticulate 1.28+dfsg-1
r-cran-rjson 0.2.21-1
r-cran-rlang 1.0.6-1
r-cran-rmarkdown 2.20+dfsg-1
r-cran-rocr 1.0-11-2
r-cran-rprojroot 2.0.3-1
r-cran-rsvd 1.0.5-1
r-cran-rtsne 0.16-1
r-cran-sass 0.4.5+dfsg-1
r-cran-scales 1.2.1-1
r-cran-scattermore 0.8-1
r-cran-sctransform 0.3.5-1
r-cran-seurat 4.3.0-1
r-cran-seuratobject 4.1.3-1
r-cran-shape 1.4.6-1
r-cran-shiny 1.7.4+dfsg-2+deb12u1
r-cran-shinyjs 2.1.0-1
r-cran-sitmo 2.0.2-1+b1
r-cran-snow 1:0.4.4-2
r-cran-sourcetools 0.1.7-1-1
r-cran-sp 1:1.6-0+dfsg-1
r-cran-spatstat.data 3.0-0-1
r-cran-spatstat.explore 3.0-6-1
r-cran-spatstat.geom 3.0-6-1
r-cran-spatstat.random 3.1-3-1
r-cran-spatstat.sparse 3.0-0-1
r-cran-spatstat.utils 3.0-1-1
r-cran-statmod 1.5.0-1
r-cran-stringi 1.7.12-1
r-cran-stringr 1.5.0-1
r-cran-survival 3.5-3-1
r-cran-sys 3.4.1-1
r-cran-systemfonts 1.0.4-1
r-cran-tensor 1.5-4
r-cran-textshaping 0.3.6-1+b1
r-cran-tibble 3.1.8+dfsg-1
r-cran-tidyr 1.3.0-1
r-cran-tidyselect 1.2.0+dfsg-1
r-cran-tinytex 0.44-1
r-cran-utf8 1.2.3-1
r-cran-uwot 0.1.14-2
r-cran-vctrs 0.5.2-1
r-cran-vipor 0.4.5-3
r-cran-viridis 0.6.2-1
r-cran-viridislite 0.4.1-1
r-cran-withr 2.5.0-1
r-cran-xfun 0.37+dfsg-1
r-cran-xtable 1:1.8-4-2
r-cran-yaml 2.3.7-1
r-cran-zoo 1.8-11-1
readline-common 8.2-1.3
rpcsvc-proto 1.4.3-1
samtools 1.16.1-1
sed 4.9-1+deb12u1
sensible-utils 0.0.17+nmu1
shared-mime-info 2.2-1
sysvinit-utils 3.06-4
tabix 1.16+ds-3
tar 1.34+dfsg-1.2+deb12u1
tzdata 2026c-0+deb12u1
ucf 3.0043+nmu1+deb12u1
unzip 6.0-28+deb12u1
usr-is-merged 37~deb12u1
util-linux 2.38.1-5+deb12u3
util-linux-extra 2.38.1-5+deb12u3
x11-common 1:7.7+23
xdg-utils 1.1.3-4.1
xkb-data 2.35.1-1
xz-utils 5.4.1-1+deb12u2
zip 3.0-13+deb12u1
zlib1g 1:1.2.13.dfsg-1
zlib1g-dev 1:1.2.13.dfsg-1
```

</details>

## 评测镜像

在底座之上只新增 `anndata`、`h5py`（及其依赖），用于读取 `.h5ad` 结果；指标（ARI/NMI/silhouette 等）用底座已有的 scikit-learn。不含 R。

### 命令行工具

| 工具 | 版本 |
|---|---|
| deno | deno 2.9.4 (stable, release, x86_64-unknown-linux-gnu) |
| gcc | gcc (Debian 12.2.0-14+deb12u1) 12.2.0 |
| make | GNU Make 4.3 |

### Python 包（49 个）

| 包 | 版本 |
|---|---|
| anndata | 0.10.8 |
| array-api-compat | 1.15.0 |
| filelock | 3.32.3 |
| fsspec | 2026.7.0 |
| h5py | 3.16.0 |
| imagecodecs | 2024.6.1 |
| imageio | 2.34.2 |
| iniconfig | 2.3.1 |
| Jinja2 | 3.1.6 |
| joblib | 1.5.3 |
| MarkupSafe | 3.0.3 |
| mpmath | 1.3.0 |
| natsort | 8.4.0 |
| networkx | 3.6.1 |
| numpy | 1.26.4 |
| nvidia-cublas-cu12 | 12.1.3.1 |
| nvidia-cuda-cupti-cu12 | 12.1.105 |
| nvidia-cuda-nvrtc-cu12 | 12.1.105 |
| nvidia-cuda-runtime-cu12 | 12.1.105 |
| nvidia-cudnn-cu12 | 9.1.0.70 |
| nvidia-cufft-cu12 | 11.0.2.54 |
| nvidia-curand-cu12 | 10.3.2.106 |
| nvidia-cusolver-cu12 | 11.4.5.107 |
| nvidia-cusparse-cu12 | 12.1.0.106 |
| nvidia-nccl-cu12 | 2.20.5 |
| nvidia-nvjitlink-cu12 | 12.9.86 |
| nvidia-nvtx-cu12 | 12.1.105 |
| packaging | 26.3 |
| pandas | 2.2.2 |
| pillow | 12.3.0 |
| pip | 24.2 |
| pluggy | 1.6.0 |
| Pygments | 2.21.0 |
| pytest | 8.4.1 |
| pytest-json-ctrf | 0.3.5 |
| python-dateutil | 2.9.0.post0 |
| pytz | 2026.5 |
| scikit-learn | 1.5.2 |
| scipy | 1.13.1 |
| setuptools | 79.0.1 |
| six | 1.17.0 |
| sympy | 1.14.0 |
| threadpoolctl | 3.6.0 |
| tifffile | 2024.7.24 |
| torch | 2.4.1+cu121 |
| triton | 3.0.0 |
| typing_extensions | 4.16.0 |
| tzdata | 2026.5 |
| wheel | 0.46.3 |

### 系统包（apt，151 个）

<details><summary>展开</summary>

```text
adduser 3.134
apt 2.6.1
base-files 12.4+deb12u15
base-passwd 3.6.1
bash 5.2.15-2+b13
binutils 2.40-2
binutils-common 2.40-2
binutils-x86-64-linux-gnu 2.40-2
bsdutils 1:2.38.1-5+deb12u3
build-essential 12.9
bzip2 1.0.8-5+b1
ca-certificates 20250419~deb12u1
coreutils 9.1-1
cpp 4:12.2.0-3
cpp-12 12.2.0-14+deb12u1
dash 0.5.12-2
debconf 1.5.82
debian-archive-keyring 2023.3+deb12u2
debianutils 5.7-0.5~deb12u1
diffutils 1:3.8-4
dpkg 1.21.23
dpkg-dev 1.21.23
e2fsprogs 1.47.0-2+b2
findutils 4.9.0-4
g++ 4:12.2.0-3
g++-12 12.2.0-14+deb12u1
gcc 4:12.2.0-3
gcc-12 12.2.0-14+deb12u1
gcc-12-base 12.2.0-14+deb12u1
gpgv 2.2.40-1.1+deb12u2
grep 3.8-5
gzip 1.12-1
hostname 3.23+nmu1
init-system-helpers 1.65.2+deb12u1
libacl1 2.3.1-3
libapt-pkg6.0 2.6.1
libasan8 12.2.0-14+deb12u1
libatomic1 12.2.0-14+deb12u1
libattr1 1:2.5.1-4
libaudit-common 1:3.0.9-1
libaudit1 1:3.0.9-1
libbinutils 2.40-2
libblkid1 2.38.1-5+deb12u3
libbz2-1.0 1.0.8-5+b1
libc-bin 2.36-9+deb12u14
libc-dev-bin 2.36-9+deb12u14
libc6 2.36-9+deb12u14
libc6-dev 2.36-9+deb12u14
libcap-ng0 0.8.3-1+b3
libcap2 1:2.66-4+deb12u3+b1
libcc1-0 12.2.0-14+deb12u1
libcom-err2 1.47.0-2+b2
libcrypt-dev 1:4.4.33-2
libcrypt1 1:4.4.33-2
libctf-nobfd0 2.40-2
libctf0 2.40-2
libdb5.3 5.3.28+dfsg2-1
libdebconfclient0 0.270
libdpkg-perl 1.21.23
libext2fs2 1.47.0-2+b2
libffi8 3.4.4-1
libgcc-12-dev 12.2.0-14+deb12u1
libgcc-s1 12.2.0-14+deb12u1
libgcrypt20 1.10.1-3+deb12u1
libgdbm-compat4 1.23-3
libgdbm6 1.23-3
libgmp10 2:6.2.1+dfsg1-1.1
libgnutls30 3.7.9-2+deb12u7
libgomp1 12.2.0-14+deb12u1
libgpg-error0 1.46-1
libgprofng0 2.40-2
libgssapi-krb5-2 1.20.1-2+deb12u5
libhogweed6 3.8.1-2
libidn2-0 2.3.3-1+b1
libisl23 0.25-1.1
libitm1 12.2.0-14+deb12u1
libjansson4 2.14-2
libk5crypto3 1.20.1-2+deb12u5
libkeyutils1 1.6.3-2
libkrb5-3 1.20.1-2+deb12u5
libkrb5support0 1.20.1-2+deb12u5
liblsan0 12.2.0-14+deb12u1
liblz4-1 1.9.4-1
liblzma5 5.4.1-1+deb12u2
libmd0 1.0.4-2
libmount1 2.38.1-5+deb12u3
libmpc3 1.3.1-1
libmpfr6 4.2.0-1
libncursesw6 6.4-4
libnettle8 3.8.1-2
libnsl-dev 1.3.0-2
libnsl2 1.3.0-2
libp11-kit0 0.24.1-2
libpam-modules 1.5.2-6+deb12u2
libpam-modules-bin 1.5.2-6+deb12u2
libpam-runtime 1.5.2-6+deb12u2
libpam0g 1.5.2-6+deb12u2
libpcre2-8-0 10.42-1+deb12u2
libperl5.36 5.36.0-7+deb12u4
libquadmath0 12.2.0-14+deb12u1
libreadline8 8.2-1.3
libseccomp2 2.5.4-1+deb12u1
libselinux1 3.4-1+b6
libsemanage-common 3.4-1
libsemanage2 3.4-1+b5
libsepol2 3.4-2.1
libsmartcols1 2.38.1-5+deb12u3
libsqlite3-0 3.40.1-2+deb12u2
libss2 1.47.0-2+b2
libssl3 3.0.22-1~deb12u1
libstdc++-12-dev 12.2.0-14+deb12u1
libstdc++6 12.2.0-14+deb12u1
libsystemd0 252.39-1~deb12u2
libtasn1-6 4.19.0-2+deb12u1
libtinfo6 6.4-4
libtirpc-common 1.3.3+ds-1
libtirpc-dev 1.3.3+ds-1
libtirpc3 1.3.3+ds-1
libtsan2 12.2.0-14+deb12u1
libubsan1 12.2.0-14+deb12u1
libudev1 252.39-1~deb12u2
libunistring2 1.0-2
libuuid1 2.38.1-5+deb12u3
libxxhash0 0.8.1-1
libzstd1 1.5.4+dfsg2-5
linux-libc-dev 6.1.187-1
login 1:4.13+dfsg1-1+deb12u2
logsave 1.47.0-2+b2
make 4.3-4.1
mawk 1.3.4.20200120-3.1
mount 2.38.1-5+deb12u3
ncurses-base 6.4-4
ncurses-bin 6.4-4
netbase 6.4
openssl 3.0.22-1~deb12u1
passwd 1:4.13+dfsg1-1+deb12u2
patch 2.7.6-7
perl 5.36.0-7+deb12u4
perl-base 5.36.0-7+deb12u4
perl-modules-5.36 5.36.0-7+deb12u4
readline-common 8.2-1.3
rpcsvc-proto 1.4.3-1
sed 4.9-1+deb12u1
sysvinit-utils 3.06-4
tar 1.34+dfsg-1.2+deb12u1
tzdata 2026c-0+deb12u1
usr-is-merged 37~deb12u1
util-linux 2.38.1-5+deb12u3
util-linux-extra 2.38.1-5+deb12u3
xz-utils 5.4.1-1+deb12u2
zlib1g 1:1.2.13.dfsg-1
```

</details>
