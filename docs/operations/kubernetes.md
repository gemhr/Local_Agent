# Kubernetes 部署

WP8 使用 Kustomize plain manifests，将 API、Outbox Publisher、Evaluation Worker、Migration Job 和 Kafka topic provisioning Job 作为独立进程部署。PostgreSQL、Redis、Kafka 在 canonical base 中是外部依赖；`deploy/k8s/local` 的单实例资源仅用于本地 smoke，不能代表生产 HA。

## 前置条件与镜像

需要 `kubectl`、一个已授权的本地/测试集群和可被节点拉取的应用镜像。镜像遵循 One Image / Multiple Commands；生产请使用 immutable tag 或 digest，不推荐 `latest`。本地构建并加载镜像由集群运行时自行完成。

先创建真实 Secret（不要提交值），字段为 `LOCAL_AGENT_DATABASE_URL`、`LOCAL_AGENT_REMOTE_API_KEY`、`LOCAL_AGENT_JWT_PUBLIC_KEY`。`secret.example.yaml` 仅是占位示例。PostgreSQL 地址与凭据统一包含在 Secret 的完整 DSN 中；ConfigMap 保存 Redis/Kafka/API/模型等非 Secret 配置、topic、consumer group、metrics 与 `LOCAL_AGENT_KB_REQUIRED=false`。

## 部署顺序

```powershell
kubectl create namespace localagent
kubectl -n localagent create secret generic localagent-secrets `
  --from-literal=LOCAL_AGENT_DATABASE_URL=$env:LOCAL_AGENT_DATABASE_URL `
  --from-literal=LOCAL_AGENT_REMOTE_API_KEY=$env:LOCAL_AGENT_REMOTE_API_KEY `
  --from-literal=LOCAL_AGENT_JWT_PUBLIC_KEY=$env:LOCAL_AGENT_JWT_PUBLIC_KEY
powershell -File scripts/deploy_k8s.ps1 -Overlay base -Context <authorized-test-context>
```

脚本先检查 context/cluster 并做 client dry-run，然后按显式顺序 apply 配置、重建并等待 `localagent-migrate`、重建并等待 `localagent-kafka-init`，最后等待三个 Deployment rollout。Job 分别执行 `alembic upgrade head` 与 `python -m scripts.provision_kafka_topics`；topic auto-create 关闭，两个 topic 是 `evaluation.jobs.v1` 和 `evaluation.jobs.v1.dlq`。

本地测试可直接使用 overlay（其中 Secret 是仅供 smoke 的占位值）：

```powershell
powershell -File scripts/deploy_k8s.ps1 -Overlay local -Context <local-context>
```

## 拓扑、探针与安全

API 是 `ClusterIP` Service `localagent-api:8000`，Base 可用 `kubectl -n localagent port-forward service/localagent-api 18080:8000` 访问；local overlay 使用 Namespace `localagent-stage6-wp8`。API liveness 为 `/health`，readiness 为 `/readyz`，startup 使用 `/health`；liveness 不检查外部依赖。Publisher/Worker 仅使用现有 `python -m scripts.healthcheck --component ...` readiness，不把依赖故障变成重启风暴。metrics ports 为 9101/9102。

所有应用 Pod 使用专用 ServiceAccount，`automountServiceAccountToken: false`，`runAsNonRoot`、UID/GID 10001、RuntimeDefault seccomp、禁止 privilege escalation、drop `ALL` capabilities 和 read-only root filesystem；`/tmp` 与 Chroma 临时目录使用 `emptyDir`。每个容器都有 requests/limits；这些是 baseline，不是 capacity benchmark。API grace 45s、Publisher 45s、Worker 75s。

## 副本、发布与停止

API 固定 `replicas: 1` 且使用 `Recreate`，避免 process-local RunRegistry/approval/execution claim 出现两个 active Runtime Pod；升级会短暂不可用，当前阶段正确性优先于 zero-downtime。API 不支持水平扩容，Kubernetes 不会自动解决 Runtime ownership。Publisher 和 Worker 默认单副本并使用 `RollingUpdate(maxUnavailable=0,maxSurge=1)`；它们依赖既有 lease/fencing/dedup 合同，Worker 可在资源允许时横向扩展。正在运行的长 Evaluation 不保证在 grace period 内完整 drain，恢复依赖 lease expiry、fencing 与 Kafka redelivery。

配置或 Secret 更新不会热刷新环境变量，必须显式 rollout/restart。WP8 不提供 HPA、Ingress、PDB、NetworkPolicy、Helm、生产 Secret Manager 或 Prometheus Server；canonical base 也不部署生产 PG/Redis/Kafka HA。RAG 仍是无 seed index/model 时的 documented degraded boundary。

## Smoke 与限制

应在明确的非生产 context 先执行 `kubectl apply --dry-run=client -k ...`，再验证两个 Job Complete、三个 Deployment Available、API Service 为 ClusterIP、`/health`、`/readyz`、`/metrics` 返回 200。可删除 API/Worker Pod 验证 Deployment 重建，并将 Worker scale 到 2 后恢复 1；不得将 API scale 到 2 作为成功测试。

WP8 已在 Docker Desktop Kubernetes 的 `docker-desktop` context 完成一次真实验证：两个 Job Complete、三个 Deployment Available、HTTP 三个端点 200、API/Worker Pod 重建、Publisher/Worker rollout、Worker `1 → 2 → 1` 与 Kafka outage/recovery 均通过。测试 Namespace 已清理。该结果证明当前功能与副本边界，不构成生产 HA、容量 benchmark、自动扩缩容或真实模型 E2E 证明。
