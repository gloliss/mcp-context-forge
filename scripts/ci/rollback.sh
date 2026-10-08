#!/usr/bin/env bash
#
# 回退到上一版镜像。
#
# 「上一版」是 deploy.sh 在换容器前记下的镜像 ID，不是 tag —— tag 会被下一次
# docker load/docker build 覆盖，ID 不会，所以只有 ID 能可靠指向旧的那一份。
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

readonly STATE_DIR="$(dirname -- "${SCRIPT_DIR}")/state"
readonly PREVIOUS_IMAGE_FILE="${STATE_DIR}/previous-image"

IMAGE_REF=""
DRY_RUN=0

usage() {
    cat <<'EOF'
用法：
  ./rollback.sh                回退到最近一次部署前记下的镜像
  ./rollback.sh 镜像引用       回退到指定镜像（tag 或 sha256: 镜像 ID）
  ./rollback.sh --dry-run      只检查，不换容器
  ./rollback.sh --list         列出本机所有 mcpgateway 镜像

回退会走与 deploy.sh 完全相同的前置检查、备份与换容器流程（含健康检查失败自动切回）。
EOF
}

list_images() {
    docker images "${IMAGE_REPO}" --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}\t{{.ID}}' | sort -r
}

resolve_image() {
    if [[ -n "${IMAGE_REF}" ]]; then
        return 0
    fi
    if [[ -r "${PREVIOUS_IMAGE_FILE}" ]]; then
        IMAGE_REF="$(head -n 1 "${PREVIOUS_IMAGE_FILE}")"
        info "从 ${PREVIOUS_IMAGE_FILE} 读到上一版镜像：${IMAGE_REF}"
        return 0
    fi
    warn "找不到 ${PREVIOUS_IMAGE_FILE}（可能还没有在本包目录跑过 deploy.sh）"
    warn "本机现有的 ${IMAGE_REPO} 镜像："
    list_images | sed 's/^/  /' >&2
    die "请指定要回退到的镜像：./rollback.sh <镜像引用>"
}

main() {
    while (( $# )); do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --list)
                require_command docker
                list_images
                return 0
                ;;
            help|-h|--help) usage; return 0 ;;
            -*) usage >&2; die "未知参数：$1" ;;
            *)
                [[ -z "${IMAGE_REF}" ]] || die "只能指定一个镜像引用"
                IMAGE_REF="$1"
                ;;
        esac
        shift
    done

    require_command docker
    resolve_image

    if ! docker image inspect "${IMAGE_REF}" >/dev/null 2>&1; then
        warn "本机没有镜像 ${IMAGE_REF}。现有的 ${IMAGE_REPO} 镜像："
        list_images | sed 's/^/  /' >&2
        die "回退目标不存在"
    fi

    local -a args=("--image" "${IMAGE_REF}")
    if (( DRY_RUN )); then
        args+=("--dry-run")
    fi
    exec "${SCRIPT_DIR}/deploy.sh" "${args[@]}"
}

main "$@"
