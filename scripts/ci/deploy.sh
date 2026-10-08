#!/usr/bin/env bash
#
# 在目标机上用一个交付包替换 ContextForge 容器。
#
# 目标机没有仓库，所以「这个包是哪个 commit」只由包里的 manifest.json 和镜像自带的
# org.opencontainers.image.revision 标签来证明；两者必须一致，否则拒绝部署。
#
# 数据安全是硬步骤不是提示：先确认 /data 挂在命名卷上、先备份数据库、换完再逐表
# 核对行数；新容器起不来就自动切回上一版镜像。
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

readonly MANIFEST="${SCRIPT_DIR}/manifest.json"
readonly PREFLIGHT="${SCRIPT_DIR}/preflight-check.sh"
readonly STATE_DIR="$(dirname -- "${SCRIPT_DIR}")/state"

MODE="load"
IMAGE_REF=""
DRY_RUN=0
FORCE_NO_VOLUME=0

TARGET_IMAGE=""
TARGET_IMAGE_ID=""
TARGET_SHORT=""
TARGET_REVISION=""
BACKUP_DIR=""
PREV_IMAGE_ID=""
PREV_IMAGE_REF=""

usage() {
    cat <<'EOF'
用法：
  ./deploy.sh                    用本包自带的镜像归档替换容器（默认）
  ./deploy.sh --build            用本包自带的源码快照在本机构建镜像再替换
  ./deploy.sh --image 引用       用本机已有的镜像替换容器（回滚用，不做 commit 校验）
  ./deploy.sh --dry-run          只做前置检查、备份与数据快照，不碰容器
  ./deploy.sh status             查看容器状态
  ./deploy.sh logs [参数...]     查看日志，例如 logs -f --tail 100
  ./deploy.sh help

选项：
  --env-file 路径      运行配置，默认来自 common.sh 的 ENV_FILE
  --force-no-volume    允许数据不在命名卷上（会丢数据，默认拒绝执行）

环境变量（覆盖 common.sh 的默认拓扑）：
  CONTEXTFORGE_CONTAINER / CONTEXTFORGE_VOLUME / CONTEXTFORGE_ENV_FILE
  CONTEXTFORGE_PORT / CONTEXTFORGE_IMAGE_REPO / CONTEXTFORGE_HEALTH_TIMEOUT
EOF
}

require_hex_component() {
    local value="$1" label="$2"
    [[ "${value}" =~ ^[0-9a-f]{7,64}$ ]] || die "manifest.json 里的 ${label} 不是合法的 commit 哈希：${value}"
}

# --- 取镜像 ------------------------------------------------------------------

verify_payload() {
    [[ -f "${SCRIPT_DIR}/SHA256SUMS" ]] || die "缺少 SHA256SUMS，无法确认包没被改动"
    info "校验交付包完整性"
    ( cd "${SCRIPT_DIR}" && sha256sum --check --quiet SHA256SUMS ) \
        || die "交付包校验失败，请重新获取，不要将就使用这个包"
}

verify_image_identity() {
    local ref="$1" expect_id="$2" expect_rev="$3"
    local image_id os arch rev
    image_id="$(docker image inspect --format '{{.Id}}' "${ref}" 2>/dev/null || true)"
    [[ -n "${image_id}" ]] || die "本机没有镜像 ${ref}"
    os="$(docker image inspect --format '{{.Os}}' "${ref}")"
    arch="$(docker image inspect --format '{{.Architecture}}' "${ref}")"
    [[ "${os}" == "linux" ]] || die "镜像操作系统不是 linux：${os}"
    [[ "${arch}" == "amd64" ]] || die "镜像架构不是 amd64：${arch}"
    if [[ -n "${expect_id}" && "${image_id}" != "${expect_id}" ]]; then
        die "镜像 ID 与 manifest 不符：实际 ${image_id}，期望 ${expect_id}"
    fi
    rev="$(image_label "${ref}" "org.opencontainers.image.revision")"
    if [[ -n "${expect_rev}" && "${rev}" != "${expect_rev}" ]]; then
        die "镜像 revision 标签与 manifest 不符：实际 ${rev:-<空>}，期望 ${expect_rev}
      这个镜像无法与源码 commit 对应，拒绝部署。"
    fi
    TARGET_IMAGE_ID="${image_id}"
    info "镜像身份校验通过：${ref}"
    info "  镜像 ID ${image_id}  linux/amd64  revision=${rev:-<空>}"
}

acquire_image() {
    local archive expected_id build_date source_dir

    # manifest 本身也在 SHA256SUMS 的覆盖范围内，所以先验包、再读 manifest、
    # 再拿做过格式校验的 git_short 去拼路径；顺序颠倒就等于信任未校验的内容。
    if [[ "${MODE}" != "image" ]]; then
        verify_payload
        TARGET_SHORT="$(manifest_value "${MANIFEST}" git_short)"
        require_hex_component "${TARGET_SHORT}" "git_short"
        TARGET_REVISION="$(manifest_value "${MANIFEST}" git_revision)"
        require_hex_component "${TARGET_REVISION}" "git_revision"
        TARGET_IMAGE="$(manifest_value "${MANIFEST}" image_ref)"
        [[ -n "${TARGET_IMAGE}" ]] || die "manifest.json 里没有 image_ref"
    fi

    case "${MODE}" in
    image)
        [[ -n "${IMAGE_REF}" ]] || die "--image 需要一个镜像引用"
        # 回滚用本机已有镜像，不读 manifest：老镜像（Containerfile 修好之前构建的）
        # 没有可信的 revision 标签，回滚路径只校验存在性与平台。
        TARGET_IMAGE="${IMAGE_REF}"
        TARGET_SHORT="rollback"
        verify_image_identity "${TARGET_IMAGE}" "" ""
        ;;
    load)
        archive="$(manifest_value "${MANIFEST}" image_archive)"
        [[ -n "${archive}" ]] || die "本包没有镜像归档（release.sh --no-image）；请改用 --build 或 --image"
        expected_id="$(manifest_value "${MANIFEST}" image_id)"

        if [[ "$(docker image inspect --format '{{.Id}}' "${TARGET_IMAGE}" 2>/dev/null || true)" == "${expected_id}" ]]; then
            info "本机已有同一镜像 ${TARGET_IMAGE}，跳过 docker load"
        else
            info "从归档载入镜像（$(( $(stat -c %s "${SCRIPT_DIR}/${archive}") / 1048576 )) MiB），可能需要几分钟"
            docker load --input "${SCRIPT_DIR}/${archive}"
        fi
        verify_image_identity "${TARGET_IMAGE}" "${expected_id}" "${TARGET_REVISION}"
        ;;
    build)
        archive="$(manifest_value "${MANIFEST}" source_archive)"
        [[ -n "${archive}" ]] || die "本包没有源码快照（release.sh --no-source）；请改用 --load 或 --image"
        build_date="$(manifest_value "${MANIFEST}" built_at)"
        source_dir="${STATE_DIR}/build-${TARGET_SHORT}"

        info "解出源码快照到 ${source_dir}"
        rm -rf "${source_dir}"
        mkdir -p "${source_dir}"
        tar -xzf "${SCRIPT_DIR}/${archive}" -C "${source_dir}" --strip-components=1
        [[ -f "${source_dir}/Containerfile" ]] || die "源码快照里没有 Containerfile，无法构建"

        info "现场构建 ${TARGET_IMAGE}（构建阶段要能访问 UBI / npm / PyPI）"
        docker build --network host --progress=plain \
            --file "${source_dir}/Containerfile" \
            --build-arg "GIT_REVISION=${TARGET_REVISION}" \
            --build-arg "BUILD_DATE=${build_date}" \
            --tag "${TARGET_IMAGE}" \
            "${source_dir}"

        verify_image_identity "${TARGET_IMAGE}" "" "${TARGET_REVISION}"
        ;;
    *)
        die "未知的取镜像方式：${MODE}"
        ;;
    esac
}

# --- 数据保全 ----------------------------------------------------------------

backup_database() {
    local image="$1" dest="$2" output
    if ! container_exists "${CONTAINER_NAME}" && ! docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1; then
        warn "既没有容器 ${CONTAINER_NAME} 也没有数据卷 ${VOLUME_NAME}，跳过备份（首次部署）"
        return 0
    fi
    mkdir -p "${dest}"
    info "备份数据库到 ${dest}"

    # 备份一律 best-effort：失败也绝不能中止脚本之外的事情。
    # 卷不会被 docker run 改动，数据本来就在，所以备份失败不是致命问题。
    # 走一次性 root 容器 —— 宿主用户读不了卷目录（/data/docker/volumes/... 无读权限）。
    if output="$(docker run --rm --network none --user root \
        --entrypoint /app/.venv/bin/python3 \
        --volume "${VOLUME_NAME}:${DATA_MOUNT}" \
        --volume "${dest}:/backup" \
        "${image}" -c '
import sqlite3

source = sqlite3.connect("/data/mcp.db")
target = sqlite3.connect("/backup/mcp.db")
with target:
    source.backup(target)
verdict = target.execute("PRAGMA integrity_check").fetchone()[0]
target.close()
source.close()
print(verdict)
' 2>&1)"; then
        if [[ "${output}" == "ok" ]]; then
            info "备份完成：integrity_check=ok，$(stat -c %s "${dest}/mcp.db") 字节"
        else
            warn "备份已写出，但 integrity_check 返回：${output}"
        fi
    else
        warn "数据库备份失败（不中止部署，卷里的数据本来就在）："
        printf '%s\n' "${output}" >&2
    fi
}

read_counts() {
    docker exec --env "CF_DB_PATH=${DB_PATH}" "${CONTAINER_NAME}" python3 -c '
import os
import sqlite3
import sys

connection = sqlite3.connect(os.environ["CF_DB_PATH"])
try:
    for name in sys.argv[1:]:
        try:
            print("%s=%d" % (name, connection.execute("SELECT count(*) FROM " + name).fetchone()[0]))
        except sqlite3.Error as error:
            print("%s=? (%s)" % (name, error))
finally:
    connection.close()
' "${TRACKED_TABLES[@]}"
}

compare_counts() {
    local before="$1" after="$2" name bv av line regressed=0
    local -A before_map=() after_map=()
    while IFS= read -r line; do
        [[ "${line}" == *=* ]] || continue
        before_map["${line%%=*}"]="${line#*=}"
    done <<< "${before}"
    while IFS= read -r line; do
        [[ "${line}" == *=* ]] || continue
        after_map["${line%%=*}"]="${line#*=}"
    done <<< "${after}"

    for name in "${TRACKED_TABLES[@]}"; do
        bv="${before_map[${name}]:-?}"
        av="${after_map[${name}]:-?}"
        if [[ "${bv}" == "?"* || "${av}" == "?"* ]]; then
            warn "  ${name}: 读不到（换容器前 ${bv}，换容器后 ${av}）"
            regressed=1
        elif (( av < bv )); then
            warn "  ${name}: 从 ${bv} 减少到 ${av}"
            regressed=1
        elif (( av > bv )); then
            info "  ${name}: ${bv} -> ${av}"
        else
            info "  ${name}: ${bv}（未变）"
        fi
    done
    return "${regressed}"
}

# --- 换容器 ------------------------------------------------------------------

start_container() {
    local image="$1"
    local -a args=()
    mapfile -t args < <(container_run_args "${image}")
    docker run --detach "${args[@]}" >/dev/null
    info "已启动 ${CONTAINER_NAME}（镜像 ${image}）"
}

wait_health() {
    local deadline=$(( SECONDS + HEALTH_TIMEOUT_SECONDS )) state
    info "等待 ${CONTAINER_NAME} 变 healthy 且 /health 返回 200（最长 ${HEALTH_TIMEOUT_SECONDS} 秒）"
    while (( SECONDS < deadline )); do
        state="$(container_state "${CONTAINER_NAME}")"
        if [[ "${state}" == "exited" || "${state}" == "dead" ]]; then
            warn "容器已退出，最后 50 行日志："
            docker logs --tail 50 "${CONTAINER_NAME}" 2>&1 || true
            return 1
        fi
        if [[ "$(container_health "${CONTAINER_NAME}")" == "healthy" ]] \
            && curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
            info "容器 healthy，/health 返回 200"
            return 0
        fi
        sleep 5
    done
    warn "等待超时，最后 50 行日志："
    docker logs --tail 50 "${CONTAINER_NAME}" 2>&1 || true
    return 1
}

swap_and_wait() {
    local image="$1"
    if container_exists "${CONTAINER_NAME}"; then
        info "停止并删除现有容器（数据在卷 ${VOLUME_NAME} 上，不受影响）"
        docker stop --time 30 "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    fi
    start_container "${image}" || return 1
    wait_health
}

rollback_after_failure() {
    if [[ -z "${PREV_IMAGE_ID}" ]]; then
        die "新容器未能通过健康检查，且没有可回退的上一版镜像。备份在 ${BACKUP_DIR:-<未备份>}"
    fi
    warn "新镜像 ${TARGET_IMAGE} 未能通过健康检查，正在切回上一版（${PREV_IMAGE_REF} / ${PREV_IMAGE_ID}）"
    if swap_and_wait "${PREV_IMAGE_ID}"; then
        die "已回滚到 ${PREV_IMAGE_REF}，服务仍然可用；新镜像不可用，原因见上面的日志"
    fi
    die "回滚也失败了，${CONTAINER_NAME} 当前没有通过健康检查。
      上一版镜像 ${PREV_IMAGE_ID} 仍在本地，数据库备份在 ${BACKUP_DIR:-<未备份>}"
}

record_state() {
    mkdir -p "${STATE_DIR}"
    if [[ -n "${PREV_IMAGE_ID}" ]]; then
        printf '%s\n' "${PREV_IMAGE_ID}" > "${STATE_DIR}/previous-image"
    fi
    {
        printf '时间：%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '镜像：%s（%s）\n' "${TARGET_IMAGE}" "${TARGET_IMAGE_ID}"
        printf 'revision：%s\n' "$(image_label "${TARGET_IMAGE}" "org.opencontainers.image.revision")"
        printf '上一版：%s %s\n' "${PREV_IMAGE_REF:-<无>}" "${PREV_IMAGE_ID:-<无>}"
        printf '备份：%s\n' "${BACKUP_DIR:-<未备份>}"
        printf '包目录：%s\n' "${SCRIPT_DIR}"
    } > "${STATE_DIR}/last-deploy.txt"
}

# --- 主流程 ------------------------------------------------------------------

deploy() {
    local -a preflight_args=()
    local before_counts="" after_counts=""

    while (( $# )); do
        case "$1" in
            --load) MODE="load" ;;
            --build) MODE="build" ;;
            --image)
                [[ $# -ge 2 ]] || die "--image 需要参数"
                MODE="image"
                IMAGE_REF="$2"
                shift
                ;;
            --image=*) MODE="image"; IMAGE_REF="${1#*=}" ;;
            --dry-run) DRY_RUN=1 ;;
            --force-no-volume) FORCE_NO_VOLUME=1 ;;
            --env-file)
                [[ $# -ge 2 ]] || die "--env-file 需要参数"
                ENV_FILE="$2"
                shift
                ;;
            --env-file=*) ENV_FILE="${1#*=}" ;;
            *) usage >&2; die "未知参数：$1" ;;
        esac
        shift
    done

    [[ -x "${PREFLIGHT}" ]] || die "找不到可执行的 ${PREFLIGHT}，交付包不完整"

    info "运行配置"
    info "  容器 ${CONTAINER_NAME}  数据卷 ${VOLUME_NAME}  端口 ${HOST_PORT}"
    info "  运行配置 ${ENV_FILE}"
    info "  包目录 ${SCRIPT_DIR}"

    preflight_args=("--${MODE}")
    if (( FORCE_NO_VOLUME )); then
        preflight_args+=("--force-no-volume")
    fi
    info "前置检查"
    if ! "${PREFLIGHT}" "${preflight_args[@]}"; then
        die "前置检查未通过，未做任何改动"
    fi

    info "取得镜像"
    acquire_image

    if container_exists "${CONTAINER_NAME}"; then
        PREV_IMAGE_ID="$(container_image_id "${CONTAINER_NAME}")"
        PREV_IMAGE_REF="$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
        info "当前运行的镜像：${PREV_IMAGE_REF}（${PREV_IMAGE_ID}）"
    else
        info "当前没有 ${CONTAINER_NAME} 容器，这将是首次部署"
    fi

    BACKUP_DIR="${STATE_DIR}/backups/$(date -u +%Y%m%dT%H%M%SZ)-${TARGET_SHORT}"
    backup_database "${TARGET_IMAGE}" "${BACKUP_DIR}"

    info "换容器前的数据快照"
    if container_exists "${CONTAINER_NAME}" && [[ "$(container_state "${CONTAINER_NAME}")" == "running" ]]; then
        before_counts="$(read_counts 2>/dev/null || true)"
        [[ -n "${before_counts}" ]] && printf '%s\n' "${before_counts}" | sed 's/^/  /'
        [[ -n "${before_counts}" ]] || warn "  读不到当前行数，换完只报告、不比对"
    else
        warn "  当前容器不在运行，跳过换容器前的快照"
    fi

    if (( DRY_RUN )); then
        printf '\n演练结束：以上是全部会发生的动作，容器未被改动。\n'
        return 0
    fi

    info "替换容器"
    if ! swap_and_wait "${TARGET_IMAGE}"; then
        rollback_after_failure
    fi

    info "换容器后的数据核对"
    after_counts="$(read_counts 2>/dev/null || true)"
    if [[ -z "${after_counts}" ]]; then
        warn "读不到换容器后的行数，请手工核对（备份在 ${BACKUP_DIR}）"
    elif [[ -z "${before_counts}" ]]; then
        printf '%s\n' "${after_counts}" | sed 's/^/  /'
    elif ! compare_counts "${before_counts}" "${after_counts}"; then
        warn "有数据表比换容器前少！备份在 ${BACKUP_DIR}，不要删除。"
        warn "确认无误后再考虑继续使用；必要时用 ./rollback.sh 回退（数据在卷 ${VOLUME_NAME} 上，回退不会二次丢数据）。"
    fi

    record_state

    printf '\n部署完成\n'
    printf '  镜像：%s\n' "${TARGET_IMAGE}"
    printf '  容器：%s（%s），http://127.0.0.1:%s/health 返回 200\n' \
        "${CONTAINER_NAME}" "$(container_health "${CONTAINER_NAME}")" "${HOST_PORT}"
    printf '  备份：%s\n' "${BACKUP_DIR}"
    if [[ -n "${PREV_IMAGE_ID}" ]]; then
        printf '  上一版镜像：%s（%s）\n' "${PREV_IMAGE_REF}" "${PREV_IMAGE_ID}"
        printf '  回退：./rollback.sh\n'
    fi
}

status() {
    require_command docker
    if ! container_exists "${CONTAINER_NAME}"; then
        die "容器 ${CONTAINER_NAME} 不存在"
    fi
    docker ps --filter "name=^${CONTAINER_NAME}\$" \
        --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    printf '  镜像 ID：%s\n' "$(container_image_id "${CONTAINER_NAME}")"
    printf '  revision：%s\n' "$(image_label "$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}")" "org.opencontainers.image.revision")"
    printf '  /health：HTTP %s\n' \
        "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "http://127.0.0.1:${HOST_PORT}/health" || echo 无响应)"
    if [[ -r "${STATE_DIR}/last-deploy.txt" ]]; then
        printf '\n最近一次部署记录（%s）：\n' "${STATE_DIR}/last-deploy.txt"
        sed 's/^/  /' "${STATE_DIR}/last-deploy.txt"
    fi
}

logs() {
    require_command docker
    container_exists "${CONTAINER_NAME}" || die "容器 ${CONTAINER_NAME} 不存在"
    if (( $# )); then
        docker logs "$@" "${CONTAINER_NAME}"
    else
        docker logs --tail 200 "${CONTAINER_NAME}"
    fi
}

main() {
    local command="deploy"
    if (( $# )) && [[ "$1" != -* ]]; then
        command="$1"
        shift
    fi
    case "${command}" in
        deploy) deploy "$@" ;;
        status) [[ $# -eq 0 ]] || die "status 不接受额外参数"; status ;;
        logs) logs "$@" ;;
        help|-h|--help) usage ;;
        *) usage >&2; die "未知命令：${command}" ;;
    esac
}

main "$@"
