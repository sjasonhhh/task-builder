```bash
cd /mnt/shared-storage-user/cl4mind/data/scienv/task-builder/image/test_env
IMAGE=registry.h.pjlab.org.cn/ailab-datatools-data_tools_gpu_pool/shaoyuting:scienv-test_env-cns-evaluation-cu118-$(date +%Y%m%d-%H%M%S)
docker buildx build --load --platform linux/amd64 --network host -f Dockerfile.evaluation -t "$IMAGE" .
docker push "$IMAGE"
```

```bash
cd /mnt/shared-storage-user/cl4mind/data/scienv/task-builder/image/test_env
IMAGE=registry.h.pjlab.org.cn/ailab-datatools-data_tools_gpu_pool/shaoyuting:scienv-test_env-cns-experiment-cu118-$(date +%Y%m%d-%H%M%S)
docker buildx build --load --platform linux/amd64 --network host \
  --build-context claude-bin=/root/.local/share/claude/versions \
  -f /mnt/shared-storage-user/cl4mind/data/Dockerfile.base -t "$IMAGE" .
docker push "$IMAGE"
```
