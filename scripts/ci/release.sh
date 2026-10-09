#!/usr/bin/env bash
#
# 在仓库里构建镜像并打出一个自带源码、镜像与部署脚本的交付包。
#
# 目标机没有仓库，所以包里必须自带源码快照（--build 用）和/或镜像归档（--load 用），
# 以及一份 manifest.json 说明这个包是哪个 commit。构建时把 commit 写进镜像的
# org.opencontainers.image.revision 标签，目标机才有办法把运行的镜像与源码对上。
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

readonly RELEASES_DIR="${CONTEXTFORGE_RELEASES_DIR:-/data/xin.feng/workspace/releases}"

WITH_IMAGE=1
WITH_SOURCE=1
SKIP_BUILD=0
ALLOW_DIRTY=0
FORCE=0
TAG_OVERRIDE=""
OUT_OVERRIDE=""

usage() {
    cat <<EOF
用法：
  ./scripts/ci/release.sh                构建镜像并打出完整交付包（镜像 + 源码）
  ./scripts/ci/release.sh --no-source    只打镜像（目标机走 deploy.sh）
  ./scripts/ci/release.sh --no-image     只打源码，不在本机构建
                                         （目标机走 deploy.sh --build 现场构建）
  ./scripts/ci/release.sh --skip-build   复用本机同名镜像，不重新构建
  ./scripts/ci/release.sh --allow-dirty  允许有未提交改动（镜像将不等于 commit）
  ./scripts/ci/release.sh --force        覆盖已存在的同名交付包
  ./scripts/ci/release.sh --tag 名字     覆盖镜像 tag（默认 <分支>-<短sha>-<日期>）
  ./scripts/ci/release.sh --out 目录     覆盖输出目录
  ./scripts/ci/release.sh --list         列出已产出的交付包

输出目录：${RELEASES_DIR}/<tag>/（可用 CONTEXTFORGE_RELEASES_DIR 覆盖）
包内容：manifest.json、SHA256SUMS、image/、source/、README-DEPLOY.txt、
        deploy.sh、rollback.sh、preflight-check.sh、check-deps.sh、
        bootstrap-inner-repo.sh、common.sh

--no-image 产出的包不含镜像，因此不需要 docker：构建、身份校验与冒烟测试都留给
目标机的 deploy.sh --build 去做（它会带 GIT_REVISION 构建参数并回校 revision 标签，
接线不对同样会在那边被拦下）。
EOF
}

list_releases() {
    [[ -d "${RELEASES_DIR}" ]] || die "还没有产出过交付包：${RELEASES_DIR}"
    local entry
    for entry in "${RELEASES_DIR}"/*/; do
        [[ -f "${entry}/manifest.json" ]] || continue
        printf '%s\t%s\t%s\n' \
            "$(basename "${entry}")" \
            "$(manifest_value "${entry}/manifest.json" git_subject)" \
            "$(manifest_value "${entry}/manifest.json" built_at)"
    done | sort
}

require_clean_tree() {
    local dirty untracked
    dirty="$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=no)"
    if [[ -n "${dirty}" ]]; then
        DIRTY_FLAG=1
        if (( ALLOW_DIRTY )); then
            warn "工作树有未提交的已跟踪改动（--allow-dirty）：镜像内容将不等于 commit ${SHORT}"
            printf '%s\n' "${dirty}" | sed 's/^/  /' >&2
        else
            printf '%s\n' "${dirty}" | sed 's/^/  /' >&2
            die "工作树有未提交的已跟踪改动。镜像必须等于 commit，请先提交；确要带改动出包再加 --allow-dirty"
        fi
    fi

    untracked="$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all | grep '^??' || true)"
    if [[ -n "${untracked}" ]]; then
        warn "有未跟踪文件：它们不进源码快照，但会进本地构建上下文（除非被 .dockerignore 排除）"
        printf '%s\n' "${untracked}" | sed 's/^/  /' >&2
    fi
}

prepare_output_dir() {
    if [[ ! -e "${OUT_DIR}" ]]; then
        mkdir -p "${OUT_DIR}"
        return
    fi
    # 只允许清空「本工具自己产出过的目录」，避免 --force 变成一次盲目的 rm -rf。
    [[ -f "${OUT_DIR}/manifest.json" ]] \
        || die "输出目录已存在，但没有 manifest.json，不像是本工具产出的交付包：${OUT_DIR}"
    if (( FORCE )); then
        info "覆盖已存在的交付包：${OUT_DIR}"
        rm -rf "${OUT_DIR}"
        mkdir -p "${OUT_DIR}"
    else
        die "交付包已存在：${OUT_DIR}；要覆盖请加 --force"
    fi
}

build_image() {
    if (( SKIP_BUILD )); then
        docker image inspect "${IMAGE_REF}" >/dev/null 2>&1 \
            || die "--skip-build 但本机没有镜像 ${IMAGE_REF}"
        info "复用本机已有镜像 ${IMAGE_REF}"
        return
    fi
    info "构建 ${IMAGE_REF}"
    info "  GIT_REVISION=${REVISION}  BUILD_DATE=${BUILD_DATE}"
    docker build --network host --progress=plain \
        --file "${REPO_ROOT}/Containerfile" \
        --build-arg "GIT_REVISION=${REVISION}" \
        --build-arg "BUILD_DATE=${BUILD_DATE}" \
        --tag "${IMAGE_REF}" \
        "${REPO_ROOT}"
}

verify_image() {
    IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE_REF}")"
    local arch revision
    arch="$(docker image inspect --format '{{.Architecture}}' "${IMAGE_REF}")"
    revision="$(image_label "${IMAGE_REF}" "org.opencontainers.image.revision")"
    [[ "${arch}" == "amd64" ]] || die "镜像架构是 ${arch}，交付包只面向 amd64"
    if [[ "${revision}" != "${REVISION}" ]]; then
        die "镜像的 org.opencontainers.image.revision 是 ${revision:-<空>}，期望 ${REVISION}。
      Containerfile 里的 GIT_REVISION 传递没生效 —— 没有这个标签，目标机上就无法
      把运行的镜像与源码 commit 对应起来，请先修 Containerfile。"
    fi
    info "镜像 identity：${IMAGE_ID}  linux/amd64  revision=${revision}"
}

smoke_test() {
    local output rc=0
    info "冒烟测试：在容器里导入应用与数据库驱动"
    # 宽松超时：负载高的宿主机上 docker create 本身就要几分钟。
    output="$(timeout "${CONTEXTFORGE_SMOKE_TIMEOUT:-900}" docker run --rm --network none \
        --entrypoint /app/.venv/bin/python3 "${IMAGE_REF}" \
        -c 'import fastapi, grpc, httpx, mcpgateway, oracledb, psycopg, pymysql, sqlalchemy; print("smoke ok")' 2>&1)" || rc=$?
    if (( rc == 0 )); then
        info "冒烟测试通过"
        return
    fi
    printf '%s\n' "${output}" >&2
    if (( rc == 124 )); then
        die "冒烟测试超时（${CONTEXTFORGE_SMOKE_TIMEOUT:-900} 秒）"
    fi
    die "冒烟测试失败：镜像里应用或依赖导入不了（退出码 ${rc}）"
}

export_image_archive() {
    info "导出镜像归档（未压缩约 434 MiB，压缩需要几分钟）"
    docker save "${IMAGE_REF}" | gzip > "${STAGING}/image.tar.gz"
    mv -f "${STAGING}/image.tar.gz" "${OUT_DIR}/${IMAGE_ARCHIVE_REL}"
}

export_source_archive() {
    info "生成源码快照（git archive，不含 .git 与未跟踪文件）"
    git -C "${REPO_ROOT}" archive --format=tar.gz \
        --prefix="mcp-context-forge-${SHORT}/" \
        --output="${STAGING}/source.tar.gz" HEAD
    mv -f "${STAGING}/source.tar.gz" "${OUT_DIR}/${SOURCE_ARCHIVE_REL}"
}

write_manifest() {
    MF_OUT="${OUT_DIR}/manifest.json" \
    MF_IMAGE_REF="${IMAGE_REF}" \
    MF_IMAGE_ID="${IMAGE_ID}" \
    MF_GIT_REVISION="${REVISION}" \
    MF_GIT_SHORT="${SHORT}" \
    MF_GIT_BRANCH="${BRANCH}" \
    MF_GIT_SUBJECT="${SUBJECT}" \
    MF_BUILT_AT="${BUILD_DATE}" \
    MF_DIRTY="${DIRTY_FLAG}" \
    MF_IMAGE_ARCHIVE="${IMAGE_ARCHIVE_REL}" \
    MF_SOURCE_ARCHIVE="${SOURCE_ARCHIVE_REL}" \
    MF_CONTAINER="${CONTAINER_NAME}" \
    MF_VOLUME="${VOLUME_NAME}" \
    MF_ENV_FILE="${ENV_FILE}" \
    MF_PORT="${HOST_PORT}" \
    MF_DB_PATH="${DB_PATH}" \
    python3 - <<'PY'
import json
import os

document = {
    "schema": 1,
    "image_ref": os.environ["MF_IMAGE_REF"],
    "image_id": os.environ["MF_IMAGE_ID"],
    "git_revision": os.environ["MF_GIT_REVISION"],
    "git_short": os.environ["MF_GIT_SHORT"],
    "git_branch": os.environ["MF_GIT_BRANCH"],
    "git_subject": os.environ["MF_GIT_SUBJECT"],
    "built_at": os.environ["MF_BUILT_AT"],
    "built_on": os.uname().nodename,
    "source_tree_dirty": os.environ["MF_DIRTY"] == "1",
    "image_archive": os.environ["MF_IMAGE_ARCHIVE"] or None,
    "source_archive": os.environ["MF_SOURCE_ARCHIVE"] or None,
    "container": {
        "name": os.environ["MF_CONTAINER"],
        "volume": os.environ["MF_VOLUME"],
        "env_file": os.environ["MF_ENV_FILE"],
        "port": int(os.environ["MF_PORT"]),
        "db_path": os.environ["MF_DB_PATH"],
    },
}
with open(os.environ["MF_OUT"], "w", encoding="utf-8") as handle:
    json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

write_checksums() {
    local -a payload=("manifest.json")
    if [[ -n "${IMAGE_ARCHIVE_REL}" ]]; then
        payload+=("${IMAGE_ARCHIVE_REL}")
    fi
    if [[ -n "${SOURCE_ARCHIVE_REL}" ]]; then
        payload+=("${SOURCE_ARCHIVE_REL}")
    fi
    ( cd "${OUT_DIR}" && sha256sum "${payload[@]}" > SHA256SUMS )
}

install_scripts() {
    install -m 0755 "${SCRIPT_DIR}/deploy.sh" "${OUT_DIR}/deploy.sh"
    install -m 0755 "${SCRIPT_DIR}/rollback.sh" "${OUT_DIR}/rollback.sh"
    install -m 0755 "${SCRIPT_DIR}/preflight-check.sh" "${OUT_DIR}/preflight-check.sh"
    install -m 0755 "${SCRIPT_DIR}/check-deps.sh" "${OUT_DIR}/check-deps.sh"
    install -m 0755 "${SCRIPT_DIR}/bootstrap-inner-repo.sh" "${OUT_DIR}/bootstrap-inner-repo.sh"
    install -m 0644 "${SCRIPT_DIR}/common.sh" "${OUT_DIR}/common.sh"
}

# 操作说明按这一包的实际标识生成。隔离网里的人只看这一个文件，里面的 commit
# 必须是这一包的 —— 用 sed 替换会被提交说明里的 & 与 | 改坏，所以走 python。
install_readme() {
    local template="${SCRIPT_DIR}/README-DEPLOY.txt.in"
    [[ -f "${template}" ]] || die "缺少说明模板：${template}"
    RD_TEMPLATE="${template}" \
    RD_OUT="${OUT_DIR}/README-DEPLOY.txt" \
    RD_TAG="${TAG_NAME}" \
    RD_SHORT="${SHORT}" \
    RD_REVISION="${REVISION}" \
    RD_SUBJECT="${SUBJECT}" \
    RD_BUILT_AT="${BUILD_DATE}" \
    RD_IMAGE_REF="${IMAGE_REF}" \
    python3 - <<'PY'
import os

with open(os.environ["RD_TEMPLATE"], encoding="utf-8") as handle:
    text = handle.read()
for token, key in (
    ("@TAG@", "RD_TAG"),
    ("@SHORT@", "RD_SHORT"),
    ("@REVISION@", "RD_REVISION"),
    ("@SUBJECT@", "RD_SUBJECT"),
    ("@BUILT_AT@", "RD_BUILT_AT"),
    ("@IMAGE_REF@", "RD_IMAGE_REF"),
):
    text = text.replace(token, os.environ[key])
with open(os.environ["RD_OUT"], "w", encoding="utf-8") as handle:
    handle.write(text)
PY
    chmod 0644 "${OUT_DIR}/README-DEPLOY.txt"
}

verify_package() {
    local junk crlf leftover
    junk="$(find "${OUT_DIR}" \( -name '._*' -o -name '.DS_Store' \) -print -quit)"
    [[ -z "${junk}" ]] || die "交付包里出现了 macOS 垃圾文件：${junk}"

    crlf="$(grep -rlU $'\r' "${OUT_DIR}"/*.sh "${OUT_DIR}"/*.txt 2>/dev/null || true)"
    [[ -z "${crlf}" ]] || die "脚本或说明里有 CRLF 行尾（目标机是 Linux）：${crlf}"

    leftover="$(grep -ohE '@[A-Z_]+@' "${OUT_DIR}/README-DEPLOY.txt" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
    [[ -z "${leftover}" ]] || die "说明文件里还有没替换掉的占位符：${leftover}"

    ( cd "${OUT_DIR}" && sha256sum --check --quiet SHA256SUMS ) \
        || die "自检失败：SHA256SUMS 对不上"
    info "包自检通过：无 ._* / 无 CRLF / 占位符已替换 / SHA256SUMS 一致"
}

main() {
    local arg
    while (( $# )); do
        case "$1" in
            --no-image) WITH_IMAGE=0 ;;
            --no-source) WITH_SOURCE=0 ;;
            --skip-build) SKIP_BUILD=1 ;;
            --allow-dirty) ALLOW_DIRTY=1 ;;
            --force) FORCE=1 ;;
            --tag) [[ $# -ge 2 ]] || die "--tag 需要参数"; TAG_OVERRIDE="$2"; shift ;;
            --tag=*) TAG_OVERRIDE="${1#*=}" ;;
            --out) [[ $# -ge 2 ]] || die "--out 需要参数"; OUT_OVERRIDE="$2"; shift ;;
            --out=*) OUT_OVERRIDE="${1#*=}" ;;
            --list) list_releases; return 0 ;;
            help|-h|--help) usage; return 0 ;;
            *) usage >&2; die "未知参数：$1" ;;
        esac
        shift
    done

    (( WITH_IMAGE || WITH_SOURCE )) || die "--no-image 与 --no-source 不能同时用，那样包里没有东西可部署"

    require_command git
    require_command sha256sum
    require_command python3
    require_command install
    if (( WITH_IMAGE )); then
        require_command docker
        require_command gzip
        docker info >/dev/null 2>&1 || die "无法访问 Docker daemon"
    fi

    cd "${REPO_ROOT}"
    REVISION="$(git rev-parse HEAD)"
    SHORT="$(git rev-parse --short=9 HEAD)"
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    SUBJECT="$(git log -1 --format=%s)"
    DIRTY_FLAG=0

    require_clean_tree

    TAG_NAME="${TAG_OVERRIDE:-$(printf '%s' "${BRANCH}" | tr '/' '-')-${SHORT}-$(date +%Y%m%d)}"
    IMAGE_REF="${IMAGE_REPO}:${TAG_NAME}"
    BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    OUT_DIR="${OUT_OVERRIDE:-${RELEASES_DIR}/${TAG_NAME}}"

    info "仓库 ${REPO_ROOT}"
    info "  commit ${REVISION}（${SHORT}，分支 ${BRANCH}）"
    info "  ${SUBJECT}"
    info "交付包 ${OUT_DIR}"

    # 不打镜像包就完全不碰本地镜像：构建、身份校验、冒烟测试都是镜像的事，
    # 留给目标机的 deploy.sh --build 去做 —— 它在那边会用同样的 GIT_REVISION
    # 构建参数并回校 revision 标签，接线不对同样会被拦下。
    IMAGE_ID=""
    if (( WITH_IMAGE )); then
        build_image
        verify_image
        smoke_test
    else
        info "只打源码包：跳过构建、身份校验与冒烟测试"
    fi

    prepare_output_dir
    STAGING="${OUT_DIR}/.staging-$$"
    mkdir -p "${STAGING}"
    trap 'rm -rf "${STAGING}"' EXIT

    IMAGE_ARCHIVE_REL=""
    SOURCE_ARCHIVE_REL=""
    if (( WITH_IMAGE )); then
        IMAGE_ARCHIVE_REL="image/${IMAGE_REPO}-${TAG_NAME}.tar.gz"
        mkdir -p "${OUT_DIR}/image"
    fi
    if (( WITH_SOURCE )); then
        SOURCE_ARCHIVE_REL="source/mcp-context-forge-${SHORT}.tar.gz"
        mkdir -p "${OUT_DIR}/source"
    fi

    if (( WITH_IMAGE )); then
        export_image_archive
    fi
    if (( WITH_SOURCE )); then
        export_source_archive
    fi

    write_manifest
    write_checksums
    install_scripts
    install_readme
    verify_package

    local size deploy_cmd preflight_cmd
    size="$(du -sh "${OUT_DIR}" | awk '{print $1}')"
    if (( WITH_IMAGE )); then
        deploy_cmd="./deploy.sh"
        preflight_cmd="./preflight-check.sh"
    else
        deploy_cmd="./deploy.sh --build"
        preflight_cmd="./preflight-check.sh --build"
    fi

    printf '\n交付包已就绪：%s（%s）\n' "${OUT_DIR}" "${size}"
    printf '  commit：%s  %s\n' "${SHORT}" "${SUBJECT}"
    if (( WITH_IMAGE )); then
        printf '  镜像：%s（%s）\n' "${IMAGE_REF}" "${IMAGE_ID}"
        printf '  镜像 revision 标签与 commit 一致，目标机可以据此确认部署的是哪一版。\n'
    else
        printf '  镜像：不含。目标机现场构建 %s\n' "${IMAGE_REF}"
        printf '  构建依赖请用 ./check-deps.sh --build 逐项确认，它只报告不拦。\n'
    fi
    printf '\n发到目标机后：\n'
    printf '  cd %s\n' "${OUT_DIR}"
    printf '  sha256sum --check SHA256SUMS      # 先验包\n'
    printf '  ./check-deps.sh                   # 再确认依赖\n'
    printf '  %s      # 前置检查\n' "${preflight_cmd}"
    printf '  %s --dry-run     # 演练（会备份、快照，但不换容器）\n' "${deploy_cmd}"
    printf '  %s               # 真正替换\n' "${deploy_cmd}"
    printf '\n隔离网里的那个人只需要看 README-DEPLOY.txt。\n'
}

main "$@"
