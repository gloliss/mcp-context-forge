#!/usr/bin/env bash
#
# 目标机前置检查。deploy.sh 在动手之前会先调用它，也可以单独跑来体检。
#
# 只做检查，不改动任何东西。硬性项目失败即退出非零，硬性项之外一律只报告。
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="load"
FORCE_NO_VOLUME=0
FAILURES=0

usage() {
    cat <<'EOF'
用法：
  ./preflight-check.sh            按 --load 的前提体检
  ./preflight-check.sh --build    额外检查现场构建需要的网络与资源
  ./preflight-check.sh --image    按「用本机已有镜像替换」体检（回滚场景）
  ./preflight-check.sh --force-no-volume
                                  允许数据不在命名卷上（会丢数据，默认不允许）

只做检查，不做任何改动。硬性项失败时退出码非零。
EOF
}

pass() { printf '  [ OK ] %s\n' "$*"; }
soft() { printf '  [WARN] %s\n' "$*"; }
fail() {
    printf '  [FAIL] %s\n' "$*"
    FAILURES=$((FAILURES + 1))
}

check_commands() {
    local name
    for name in docker sha256sum curl tar python3 sort; do
        if command -v "${name}" >/dev/null 2>&1; then
            pass "命令可用：${name}"
        else
            fail "缺少必要命令：${name}"
        fi
    done
}

check_arch() {
    local machine
    machine="$(uname -m)"
    if [[ "${machine}" == "x86_64" ]]; then
        pass "主机架构：${machine}"
    else
        fail "交付包只面向 x86_64，当前主机是 ${machine}"
    fi
}

check_docker() {
    local version
    if ! docker info >/dev/null 2>&1; then
        fail "无法访问 Docker daemon，请确认当前用户有 docker 权限"
        return
    fi
    version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
    if [[ -z "${version}" ]]; then
        fail "读不到 Docker 版本"
        return
    fi
    if version_ge "${version}" "${MIN_DOCKER_VERSION}"; then
        pass "Docker ${version}（要求 >= ${MIN_DOCKER_VERSION}）"
    else
        fail "Docker 版本过低：${version}，至少需要 ${MIN_DOCKER_VERSION}"
    fi
}

check_space() {
    local path="$1" label="$2" free_kib
    if [[ ! -d "${path}" ]]; then
        soft "${label} 不存在，跳过空间检查：${path}"
        return
    fi
    free_kib="$(df -Pk "${path}" | awk 'NR==2 {print $4}')"
    if [[ ! "${free_kib}" =~ ^[0-9]+$ ]]; then
        soft "读不到 ${label} 的剩余空间：${path}"
        return
    fi
    if (( free_kib >= MIN_FREE_KIB )); then
        pass "${label} 剩余 $(( free_kib / 1048576 )) GiB"
    else
        fail "${label} 剩余空间不足（$(( free_kib / 1024 )) MiB，需要 $(( MIN_FREE_KIB / 1024 )) MiB）：${path}"
    fi
}

check_port() {
    local listening=""
    if command -v ss >/dev/null 2>&1; then
        listening="$(ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -E ":${HOST_PORT}\$" || true)"
    elif command -v netstat >/dev/null 2>&1; then
        listening="$(netstat -ltn 2>/dev/null | awk '{print $4}' | grep -E ":${HOST_PORT}\$" || true)"
    fi
    if [[ -z "${listening}" ]]; then
        pass "端口 ${HOST_PORT} 当前空闲"
        return
    fi
    if container_exists "${CONTAINER_NAME}" && [[ "$(container_state "${CONTAINER_NAME}")" == "running" ]]; then
        pass "端口 ${HOST_PORT} 由 ${CONTAINER_NAME} 自己占用（正常）"
    else
        fail "端口 ${HOST_PORT} 被别的进程占用，而 ${CONTAINER_NAME} 并不在运行：${listening}"
    fi
}

check_env_file() {
    local count
    if [[ -r "${ENV_FILE}" ]]; then
        pass "运行配置可读：${ENV_FILE}"
        return
    fi
    # 这一项原来是硬性失败。但 ENV_FILE 的默认值指向开发机，隔离网里的目标机
    # 通常没有这个路径 —— 而它的运行配置就在正在跑的那个容器里，取出来就能用。
    # 这里只做只读探测（不改动任何东西），真正的提取由 deploy.sh 完成。
    if ! container_exists "${CONTAINER_NAME}"; then
        fail "运行配置不存在或不可读：${ENV_FILE}（可用 CONTEXTFORGE_ENV_FILE 指定）"
        return
    fi
    count="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
        "${CONTAINER_NAME}" 2>/dev/null | grep -c '=' || true)"
    if [[ -n "${count}" ]] && (( count > 0 )); then
        pass "${ENV_FILE} 不可读，但现有容器带着 ${count} 个环境变量；
      deploy.sh 会提取到 state/runtime.env 后沿用（只报数量，不打印值）"
    else
        fail "运行配置不存在，且读不到现有容器的环境变量：${ENV_FILE}"
    fi
}

# 数据一旦不在命名卷上，替换容器就会连库一起丢掉 —— 这正是要拦住的场景。
check_data_volume() {
    local mounts volume_name
    if ! container_exists "${CONTAINER_NAME}"; then
        if docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1; then
            pass "容器尚未创建；数据卷 ${VOLUME_NAME} 已存在，会被沿用"
        else
            soft "容器和数据卷都还不存在，这看起来是首次部署"
        fi
        return
    fi
    mounts="$(docker inspect --format "{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}|{{.RW}}{{println}}{{end}}" "${CONTAINER_NAME}")"
    volume_name="$(awk -F'|' -v dest="${DATA_MOUNT}" '$3 == dest && $1 == "volume" {print $2}' <<< "${mounts}")"
    if [[ -n "${volume_name}" ]]; then
        # 只确认「是命名卷」还不够：换容器是按 VOLUME_NAME 挂载的，卷名不一致就会挂上
        # 一个全新的空卷 —— 部署看起来成功，数据却"不见了"（旧卷还在，只是没人挂它）。
        if [[ "${volume_name}" == "${VOLUME_NAME}" ]]; then
            pass "${DATA_MOUNT} 挂在命名卷 ${volume_name} 上，替换容器不会丢数据"
        else
            fail "现有容器的 ${DATA_MOUNT} 挂在卷 ${volume_name} 上，而本工具会挂 ${VOLUME_NAME}。
      两者不是同一个卷，替换后新容器会挂上一个全新的空卷：部署看起来成功，数据却「不见了」
      （数据还在 ${volume_name} 里，只是没有容器挂它）。
      确认要保留的是 ${volume_name} 里的数据，就加 CONTEXTFORGE_VOLUME=${volume_name} 重跑。"
        fi
        return
    fi
    if grep -qE "^bind\|.*\|${DATA_MOUNT}\|" <<< "${mounts}"; then
        if (( FORCE_NO_VOLUME )); then
            soft "${DATA_MOUNT} 是宿主机目录绑定而不是命名卷，--force-no-volume 已放行"
        else
            fail "${DATA_MOUNT} 是宿主机目录绑定而不是命名卷；确认数据安全后加 --force-no-volume"
        fi
        return
    fi
    if (( FORCE_NO_VOLUME )); then
        soft "${DATA_MOUNT} 没有任何挂载（数据在容器可写层），--force-no-volume 已放行 —— 替换容器会丢数据"
    else
        fail "${DATA_MOUNT} 没有挂载，数据在容器可写层里，替换容器必丢；确认后加 --force-no-volume"
    fi
}

check_build_prerequisites() {
    local cores mem_kib
    info "现场构建前提（--build）"
    check_reachable "UBI 基础镜像仓库 registry.access.redhat.com" "https://registry.access.redhat.com/v2/"
    check_reachable "Red Hat CDN（基础镜像内部的 dnf/microdnf 要用）" "https://cdn-ubi.redhat.com/"
    check_reachable "npm registry.npmjs.org" "https://registry.npmjs.org/"
    check_reachable "PyPI pypi.org" "https://pypi.org/simple/"

    # 逐项清单与「下一包该带什么」在 check-deps.sh 里；这里只做能不能构建的判断。
    printf '  提示：逐项依赖清单用 ./check-deps.sh --build 看。\n'
    printf '        把基础镜像 docker load 进来并不能绕过 CDN 那一条 ——\n'
    printf '        它发生在基础镜像内部，与基础镜像在不在本机是两回事。\n'

    cores="$(nproc 2>/dev/null || echo 0)"
    if (( cores >= 4 )); then
        pass "CPU 核数 ${cores}"
    else
        soft "CPU 核数只有 ${cores}，构建会明显偏慢"
    fi
    mem_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    if (( mem_kib >= 4194304 )); then
        pass "内存 $(( mem_kib / 1048576 )) GiB"
    else
        soft "内存只有 $(( mem_kib / 1024 )) MiB，构建可能被 OOM 杀掉"
    fi
}

check_reachable() {
    local label="$1" url="$2" code
    code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 20 "${url}" 2>/dev/null || true)"
    if [[ "${code}" =~ ^[23] ]]; then
        pass "${label} 可达（HTTP ${code}）"
    else
        fail "${label} 不可达（HTTP ${code:-无响应}）—— 没有外网就只能走 --load"
    fi
}

report_context() {
    local image
    info "环境概况"
    printf '  容器：%s' "${CONTAINER_NAME}"
    if container_exists "${CONTAINER_NAME}"; then
        printf '（%s，健康 %s，镜像 %s）\n' \
            "$(container_state "${CONTAINER_NAME}")" \
            "$(container_health "${CONTAINER_NAME}")" \
            "$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
    else
        printf '（不存在）\n'
    fi
    printf '  数据卷：%s\n' "${VOLUME_NAME}"
    printf '  运行配置：%s\n' "${ENV_FILE}"
    printf '  监听端口：%s\n' "${HOST_PORT}"
    image="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    printf '  Docker 数据目录：%s\n' "${image:-未知}"
}

main() {
    local arg
    for arg in "$@"; do
        case "${arg}" in
            --load) MODE="load" ;;
            --build) MODE="build" ;;
            --image) MODE="image" ;;
            --force-no-volume) FORCE_NO_VOLUME=1 ;;
            help|-h|--help) usage; return 0 ;;
            *) usage >&2; die "未知参数：${arg}" ;;
        esac
    done

    report_context
    info "硬性检查"
    check_commands
    check_arch
    check_docker
    check_env_file
    check_space "${SCRIPT_DIR}" "交付包所在文件系统"
    check_space "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /)" "Docker 数据目录"
    check_port
    check_data_volume

    if [[ "${MODE}" == "build" ]]; then
        check_build_prerequisites
    fi

    printf '\n'
    if (( FAILURES > 0 )); then
        printf '前置检查未通过：%d 项失败\n' "${FAILURES}" >&2
        return 1
    fi
    printf '前置检查通过\n'
}

main "$@"
