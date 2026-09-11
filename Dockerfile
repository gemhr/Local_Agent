FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 只安装 lock 中的 Linux backend 依赖；宿主专用 local extra 不进入镜像。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/opt/venv/bin:$PATH"

RUN addgroup --system --gid 10001 localagent \
    && adduser --system --uid 10001 --gid 10001 --home /nonexistent --no-create-home localagent \
    && mkdir -p /app/chroma_db \
    && chown localagent:localagent /app/chroma_db

COPY --chown=10001:10001 . .

USER 10001:10001

EXPOSE 8000 9101 9102
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
