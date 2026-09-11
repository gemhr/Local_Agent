# Docker Compose Runtime

WP7 使用同一个非 root Python 3.12 镜像，以不同命令运行 API、Outbox Publisher、Evaluation Worker 和 Alembic Migration。PostgreSQL、Redis、单节点 Kafka KRaft 只在 Compose 私有网络可见。

## 前置条件与环境变量

需要 Docker Desktop 与 Compose v2。先复制 `.env.example` 为未提交的 `.env`，至少把 `POSTGRES_PASSWORD` 改为随机本地密码。远程模型凭据使用 `LOCAL_AGENT_REMOTE_API_KEY`，JWT 验证公钥正文使用 `LOCAL_AGENT_JWT_PUBLIC_KEY`；两者都只从宿主环境或 `.env` 注入，不进入镜像。API 启动与本 WP smoke 不会调用真实模型。

Compose 使用非 TEST 的 `LOCAL` environment profile 与 `PRODUCTION` runtime profile。容器内 PostgreSQL、Redis、Kafka 地址使用 Compose DNS；宿主开发默认仍保持 `127.0.0.1`。

桌面 GUI 与本地 GGUF 依赖使用 Windows platform marker；宿主 Windows 的标准安装命令保持不变：

```powershell
uv sync
```

## 构建、启动与状态

```powershell
docker compose version
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps -a
```

默认只有 API 发布到 `127.0.0.1:8000`。如果 8000 已占用，可在当前 PowerShell 使用 `$env:LOCAL_AGENT_API_PORT="18000"` 后再执行 Compose 命令。PostgreSQL、Redis、Kafka、Publisher metrics 与 Worker metrics 均不发布宿主端口。

`migrate` 在 PostgreSQL healthy 后执行 `alembic upgrade head`；它是唯一 Migration Owner。`kafka-init` 在 Kafka healthy 后幂等创建 `evaluation.jobs.v1` 和 `evaluation.jobs.v1.dlq`（2 partitions、replication factor 1）。Kafka auto-create 保持关闭。API 不依赖 Kafka；Publisher/Worker 必须等待 migration 和 topic provisioning 成功。

## 健康、指标与日志

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:8000/readyz
Invoke-WebRequest http://127.0.0.1:8000/metrics
docker compose logs --tail 100 api
docker compose logs --tail 100 publisher
docker compose logs --tail 100 worker
```

API `/health` 只表示进程存活，`/readyz` 检查 PostgreSQL、required Redis limiter 与 Runtime admission。Publisher/Worker healthcheck 复用 `python -m scripts.healthcheck`，检查 PostgreSQL 与各自 Kafka Topic。Publisher metrics `9101`、Worker metrics `9102` 仅在 Compose 网络内监听。

## 重启、停止与持久化

```powershell
docker compose restart api
docker compose restart worker
docker compose stop
docker compose down
```

`init: true` 转发信号；API、Publisher、Worker 配置了有界 `stop_grace_period`。正常 `restart`、`stop`、`down` 不删除 `postgres_data`、`kafka_data`、`chroma_data`。只有明确需要破坏性清理本项目数据时才执行：

```powershell
docker compose down -v
```

该命令不可恢复本 Compose 的数据库、Kafka 数据与 Chroma 索引；不要对其它项目的容器或 volume 使用。

## 故障语义

- Redis 停止：API `/readyz` 返回 503，`/health` 保持 200；Redis 恢复后 readiness 自动恢复。
- Kafka 停止：API readiness 保持 200；Publisher/Worker not ready，Kafka 恢复后自动恢复。
- PostgreSQL 停止：API、Publisher、Worker 均 not ready；后台进程可由 `restart: unless-stopped` 重启，数据库恢复后重新 ready。
- Migration 失败：API、Publisher、Worker 不启动。

## Chroma 与已知限制

`chroma_data` 挂载到 `/app/chroma_db`，避免容器重建时静默丢失索引。镜像不复制宿主模型或已有 Chroma 数据，且默认 `LOCAL_AGENT_KB_REQUIRED=false`；未由 operator 导入兼容 embedding model/index 时，RAG 明确 degraded，不影响 WP7 基础运行栈启动。

这是本地 production-like 单节点环境，不提供 TLS、Kafka SASL、HA、负载均衡、自动扩缩容、Prometheus Server、OTel Collector 或真实模型 E2E。API 固定单 replica；不声称 process-local Runtime Owner 已支持水平扩展。Worker 对空闲/消息边界执行 graceful shutdown；未声称正在运行的长 Evaluation 能在 grace period 内必然 drain，强制结束后的恢复依赖现有 PostgreSQL lease/fencing 与 Kafka redelivery。
