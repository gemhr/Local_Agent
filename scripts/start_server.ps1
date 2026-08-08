# LocalAgent Server Startup (Windows Native)
#
# 职责（仅限）：
#   1. minimal prerequisite/preflight
#   2. uv run python server.py（启动单一 server 进程）
#
# 明确禁止：
#   - 不实现 Settings parser / 第二 raw env reader
#   - 不写死 secret / API Key / Wiki Cookie
#   - 不构造 Runtime 资源
#   - 不注册第二 signal owner（uvicorn 是 signal owner）
#   - 不启动 multi-worker / multi-process
#   - 不自动启动 Client（Client 由 operator 手动执行 uv run python main.py）
#
# 配置注入只通过进程环境变量（由 operator / 企业 process host 提供），
# 本脚本不读取、不解析、不写入任何配置值。

$ErrorActionPreference = "Stop"

# ---- minimal preflight ----
if (-not $env:UV) {
    try {
        $null = Get-Command uv -ErrorAction Stop
    } catch {
        Write-Error "uv not found. Install uv first (https://docs.astral.sh/uv/)."
        exit 1
    }
}

if (-not (Test-Path -Path "pyproject.toml")) {
    Write-Error "pyproject.toml not found. Run this script from the repository root."
    exit 1
}

# ---- start single server process ----
uv run python server.py