#!/usr/bin/env bash
#
# ContextForge 部署工具链的共享定义。
#
# 被 release.sh / deploy.sh / rollback.sh / preflight-check.sh 引用：
# 前两个在开发机上跑，后三个随交付包发到目标机，所以这里只允许依赖 bash 和 docker，
# 并且必须能被原样复制到交付包里（release.sh 就是这么做的）。

# ---------------------------------------------------------------------------
# 运行时拓扑。整套工具链只有这一处定义，避免各脚本之间悄悄漂移。
# 目标机若与开发机不同，用环境变量覆盖，不要改这里。
# ---------------------------------------------------------------------------
CONTAINER_NAME="${CONTEXTFORGE_CONTAINER:-mcpgateway}"
VOLUME_NAME="${CONTEXTFORGE_VOLUME:-mcpgateway-data}"
ENV_FILE="${CONTEXTFORGE_ENV_FILE:-/home/xin.feng/mcpgw-env.list}"
HOST_PORT="${CONTEXTFORGE_PORT:-4444}"
IMAGE_REPO="${CONTEXTFORGE_IMAGE_REPO:-mcpgateway}"
DATA_MOUNT="/data"
DB_PATH="${DATA_MOUNT}/mcp.db"

MIN_DOCKER_VERSION="26.0.0"
# 镜像解包后 434MB，docker load 期间还要再落一份，5 GiB 是宽松下限。
MIN_FREE_KIB=$((5 * 1024 * 1024))
# 镜像自带 HEALTHCHECK 是 start-period 90s / interval 30s。默认给到 10 分钟：
# 宿主机负载高时 docker create 本身就可能花掉几分钟（codex-15 实测 load 199 时
# 连 hello-world 都要等），等久一点没有代价，超时太短却会触发一次没必要的回滚。
HEALTH_TIMEOUT_SECONDS="${CONTEXTFORGE_HEALTH_TIMEOUT:-600}"

# 换容器前后必须逐表比对的行数。这是用户点名要保住的东西：
# 已注册的 gRPC 服务、它们转化出来的 tool、MCP server 注册与关联。
TRACKED_TABLES=(grpc_services tools servers server_tool_association grpc_schema_artifacts)

# 与线上容器逐字一致的镜像内健康检查命令（docker inspect 出来的形态不能变）。
health_check_cmd() {
    printf '%s' "python3 -c \"import httpx,sys;sys.exit(0 if httpx.get('http://localhost:${HOST_PORT}/health',timeout=5).status_code==200 else 1)\""
}

# 一条 docker run 的完整参数，deploy.sh 与 preflight-check.sh 共用，
# 保证「演练」和「真部署」起出来的容器形态完全相同。
container_run_args() {
    local image="$1"
    printf '%s\n' \
        "--name" "${CONTAINER_NAME}" \
        "--network" "host" \
        "--env-file" "${ENV_FILE}" \
        "--volume" "${VOLUME_NAME}:${DATA_MOUNT}" \
        "--health-cmd" "$(health_check_cmd)" \
        "--health-interval" "30s" \
        "--health-timeout" "10s" \
        "--health-start-period" "90s" \
        "--health-retries" "3" \
        "${image}"
}

# ---------------------------------------------------------------------------
# 运行配置从哪来：能读到 ENV_FILE 就用它；读不到就地从现有容器提取。
#
# ENV_FILE 默认值指向开发机。隔离网里的目标机没有那个路径，而上一轮发过去的
# 那套交付是用 compose 起的、环境变量在本地生成的 runtime.env 里 —— 直接照搬
# 会让 preflight 硬性失败，或者更糟：让新容器以空配置起来。
#
# 目标机的运行配置现值就在正在跑的那个容器里，那是唯一权威来源，而且不需要
# 额外传文件。所以提取一份到 state/runtime.env（0600）再用。
#
# 只对外报告变量的**数量**，绝不打印值 —— 里面有 JWT / 加密密钥与管理员口令。
ENV_FILE_SOURCE="configured"
ENV_FILE_KEYS=""
resolve_env_file() {
    local dir="$1" extracted count
    ENV_FILE_SOURCE="configured"
    if [[ -r "${ENV_FILE}" ]]; then
        return 0
    fi
    if ! container_exists "${CONTAINER_NAME}"; then
        ENV_FILE_SOURCE="missing"
        return 1
    fi
    mkdir -p -- "${dir}"
    extracted="${dir}/runtime.env"
    # docker inspect 输出的正是「每行一个 KEY=value」，与 --env-file 的格式一致。
    if ! docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
        "${CONTAINER_NAME}" > "${extracted}" 2>/dev/null; then
        rm -f -- "${extracted}"
        ENV_FILE_SOURCE="missing"
        return 1
    fi
    chmod 600 -- "${extracted}" 2>/dev/null || true
    count="$(grep -c '=' -- "${extracted}" 2>/dev/null || true)"
    if [[ -z "${count}" ]] || (( count == 0 )); then
        rm -f -- "${extracted}"
        ENV_FILE_SOURCE="missing"
        return 1
    fi
    ENV_FILE="${extracted}"
    ENV_FILE_KEYS="${count}"
    ENV_FILE_SOURCE="container"
    return 0
}

# 打印「运行配置从哪来」，供各脚本写日志用。不含任何变量值。
env_file_description() {
    case "${ENV_FILE_SOURCE}" in
    container)
        printf '从现有容器 %s 提取（%s 个变量）：%s' \
            "${CONTAINER_NAME}" "${ENV_FILE_KEYS}" "${ENV_FILE}"
        ;;
    missing)
        printf '既读不到 %s，也无法从现有容器提取' "${ENV_FILE}"
        ;;
    *)
        printf '%s' "${ENV_FILE}"
        ;;
    esac
}

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少必要命令：$1"
}

version_ge() {
    local actual="${1#v}" minimum="${2#v}"
    printf '%s\n%s\n' "${minimum}" "${actual}" | sort -V -C
}

container_exists() {
    docker container inspect "$1" >/dev/null 2>&1
}

container_state() {
    docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null || true
}

container_health() {
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || true
}

# 正在运行的容器用的是哪个镜像 —— 取 ID 而不是 tag。
# tag 会被下一次 docker load/docker build 覆盖，ID 不会，回滚必须靠 ID。
container_image_id() {
    docker inspect --format '{{.Image}}' "$1" 2>/dev/null || true
}

image_label() {
    docker image inspect --format "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true
}

# 逐个取键读 env 文件：绝不 source（该文件里的 JSON 列表型变量会被 shell 引号规则改坏）。
read_env_value() {
    local key="$1" line value
    [[ -r "${ENV_FILE}" ]] || return 0
    line="$(grep -E "^${key}=" "${ENV_FILE}" | head -n 1 || true)"
    [[ -n "${line}" ]] || return 0
    value="${line#*=}"
    value="${value%\"}"
    value="${value#\"}"
    printf '%s' "${value}"
}

# 从交付包的 manifest.json 里取一个（可点分的）键。目标机没有 git，
# manifest 就是「这个包是哪个 commit」的唯一凭据，所以必须能可靠读取。
manifest_value() {
    local manifest="$1" key="$2"
    [[ -f "${manifest}" ]] || die "找不到 manifest.json：${manifest}"
    python3 - "${manifest}" "${key}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    node = json.load(handle)
for part in sys.argv[2].split("."):
    if not isinstance(node, dict) or part not in node:
        sys.exit("manifest.json 缺少键：%s" % sys.argv[2])
    node = node[part]
print("" if node is None else node)
PY
}
