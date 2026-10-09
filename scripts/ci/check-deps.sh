#!/usr/bin/env bash
#
# 依赖体检。回答一个问题：在这个目标机上，能不能完成一次部署 / 一次现场构建。
#
# 设计前提：这里很可能跑在完全隔离的网里。从网内**无法区分「网络被封了」和
# 「服务挂了」**，所以本脚本不去猜网络，只回答「这个依赖在本机到底有没有」。
# 拿不准的地方就如实说拿不准，不用一个 curl 的失败去冒充结论。
#
# 默认只报告，永远退出 0；deploy.sh --build 会用 --require 把它当成硬性闸门。
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

readonly MANIFEST="${SCRIPT_DIR}/manifest.json"
MODE="load"
REQUIRE=0
declare -a MISSING=()
declare -a MISSING_REFS=()

usage() {
    cat <<'EOF'
用法：
  ./check-deps.sh            按 --load 体检（默认）
  ./check-deps.sh --build    按现场构建体检，逐项列出缺什么
  ./check-deps.sh --require  有缺项时退出码非零（给 deploy.sh --build 当闸门用）

只做检查，不改动任何东西。缺依赖只影响 --build，不影响 --load。
EOF
}

ok() { printf '  [ OK ] %-12s %s\n' "$1" "$2"; }
miss() {
    printf '  [缺  ] %-12s %s\n' "$1" "$2"
    MISSING+=("$1")
}
note() { printf '         %-12s %s\n' "" "$1"; }

# 从包里那份源码快照取出 Containerfile，读一个 ARG 的默认值。
#
# 刻意从快照里读而不是在脚本里写死：基础镜像的 tag 是浮动日期戳，会随上游更新，
# 写死就会和真正要构建的那份源码漂移 —— 体检说「有」，构建却去拉另一个 tag。
source_arg_value() {
    local archive="$1" name="$2" containerfile
    [[ -n "${archive}" ]] || return 0
    [[ -f "${SCRIPT_DIR}/${archive}" ]] || return 0
    containerfile="$(tar -xzOf "${SCRIPT_DIR}/${archive}" --wildcards '*/Containerfile' 2>/dev/null \
        | grep -E "^ARG ${name}=" | head -n 1 || true)"
    [[ -n "${containerfile}" ]] || return 0
    printf '%s' "${containerfile#ARG "${name}"=}"
}

# 本机有没有这个镜像。查本地，不查远程：隔离网里能查到的只有本机。
image_present() {
    docker image inspect "$1" >/dev/null 2>&1
}

# 顺带探一下网络，但只作为参考信息 —— 探不通不等于「网被封」，探得通也不代表
# 构建就能过（见下面 dnf 那一条）。
probe_url() {
    local code
    code="$(curl --silent --show-error --output /dev/null \
        --write-out '%{http_code}' --max-time 15 "$1" 2>/dev/null || true)"
    printf '%s' "${code:-无响应}"
}

# 「主机有没有应答」。只要拿到了 HTTP 状态码，就说明这条网络路径是通的 ——
# 401/403 是主机在拒绝这个具体路径，不是网络不通。把 403 当成「不可达」会冤枉
# 一台真能拉包的机器：Red Hat CDN 的根路径对裸 GET 就是 403。
answered() { [[ -n "$1" && "$1" != "000" && "$1" != "无响应" ]]; }

src_archive() {
    manifest_value "${MANIFEST}" source_archive 2>/dev/null || true
}

# 本包到底有没有镜像归档 —— --load 能不能走完全取决于它。
# 纯源码包里还印一句「镜像归档在本包内」，比不给结论更糟：那是在骗看报告的人，
# 而这份报告的全部价值就在于「看了就能决定下一步」。
package_image_archive() {
    local archive
    archive="$(manifest_value "${MANIFEST}" image_archive 2>/dev/null || true)"
    [[ -n "${archive}" ]] || return 1
    [[ -f "${SCRIPT_DIR}/${archive}" ]] || return 1
    printf '%s' "${archive}"
}

report_load() {
    local archive
    archive="$(package_image_archive || true)"
    info "取镜像方式：--load"
    if [[ -z "${archive}" ]]; then
        miss "package" "本包不含镜像归档（release.sh --no-image），--load 这条路走不了"
        note "改用 ./check-deps.sh --build 看现场构建缺什么。"
        return
    fi
    ok "package" "镜像归档在本包内：${archive}，docker load 即可"
    ok "外部依赖" "无。--load 不需要目标机有任何网络、基础镜像或包源"
}

report_build() {
    local archive ubi_base nodejs ubi_minimal
    local redhat_code npm_code pypi_code
    archive="$(src_archive)"
    if [[ -z "${archive}" ]]; then
        miss "source" "本包没有源码快照（release.sh --no-source），无法现场构建"
        return
    fi

    ubi_base="$(source_arg_value "${archive}" UBI_BASE)"
    nodejs="$(source_arg_value "${archive}" NODEJS_IMAGE)"
    ubi_minimal="$(source_arg_value "${archive}" UBI_MINIMAL)"

    redhat_code="$(probe_url https://registry.access.redhat.com/v2/)"
    pypi_code="$(probe_url https://pypi.org/simple/)"
    npm_code="$(probe_url https://registry.npmjs.org/)"

    info "取镜像方式：--build（现场构建）"

    # 每一项的判据都是「**本机有** 或 **拉得到**」—— 两者满足其一即可。
    # 只看本机会冤枉一台有外网的机器，只看网络又会在隔离网里给出假结论。
    local pair name ref
    for pair in "base-ubi:${ubi_base}" "base-node:${nodejs}" "base-minimal:${ubi_minimal}"; do
        name="${pair%%:*}"
        ref="${pair#*:}"
        if [[ -z "${ref}" ]]; then
            miss "${name}" "读不到源码快照里对应的 ARG 默认值"
            continue
        fi
        if image_present "${ref}"; then
            ok "${name}" "${ref}（本机已有）"
        elif answered "${redhat_code}"; then
            ok "${name}" "${ref}（本机没有，但 registry.access.redhat.com 有应答 HTTP ${redhat_code}，可拉取）"
        else
            miss "${name}" "本机没有且拉不到：${ref}"
            MISSING_REFS+=("${ref}")
        fi
    done

    # 最容易漏掉的一条：基础镜像**内部**的 dnf / microdnf 要访问 Red Hat CDN。
    # 把三个基础镜像都 docker load 进来也解决不了，因为它发生在镜像内部。
    local cdn_code
    cdn_code="$(probe_url https://cdn-ubi.redhat.com/)"
    if answered "${cdn_code}" || answered "${redhat_code}"; then
        ok "cdn-ubi" "Red Hat CDN 可达（HTTP ${cdn_code}）"
    else
        miss "cdn-ubi" "基础镜像内部的 dnf/microdnf 要访问 Red Hat CDN，本机探测为 ${cdn_code}"
        note "注意：把基础镜像 docker load 进来**不能**绕过这一条 ——"
        note "它发生在镜像内部，与基础镜像在不在本机是两回事"
    fi

    if answered "${pypi_code}"; then
        ok "pypi-index" "pypi.org 可达（HTTP ${pypi_code}）"
    else
        miss "pypi-index" "pypi.org 本机探测为 ${pypi_code}"
        note "pip install --upgrade pip setuptools wheel uv 这一步是无条件的，"
        note "即使 /wheels 里有完整闭包也绕不过去"
        note "另外 cpex-* 插件包在部分镜像源上缺失，换源不一定能解决"
    fi

    if answered "${npm_code}"; then
        ok "npm-registry" "registry.npmjs.org 可达（HTTP ${npm_code}）"
    else
        miss "npm-registry" "registry.npmjs.org 本机探测为 ${npm_code}"
        note "Containerfile 里有两处 npm ci（tailwind 与 vite 前端构建）"
    fi
}

print_receipt() {
    printf '\n'
    # 结论只有一处：按这次体检的**模式**给，别在 --load 的报告里说 --build 可以尝试。
    if (( ${#MISSING[@]} == 0 )); then
        if [[ "${MODE}" == "load" ]]; then
            printf '结论：--load 路径零依赖，可以直接部署。\n'
        else
            printf '结论：依赖齐全，--build 可以尝试。\n'
        fi
        return
    fi
    printf '%s\n' "---------------------------------------------------------------"
    printf '下一包需要带的东西（把这一段原样发出去即可）：\n\n'
    if [[ "${MODE}" == "load" ]]; then
        printf '  本包缺少以下内容，无法按 --load 部署：\n\n'
    else
        printf '  目标机缺少以下依赖，无法完成现场构建：\n\n'
    fi
    printf '    - %s\n' "${MISSING[@]}"
    if (( ${#MISSING_REFS[@]} > 0 )); then
        printf '\n  基础镜像（在有外网的机器上执行，然后把 deps/ 目录发进来）：\n'
        local ref
        for ref in "${MISSING_REFS[@]}"; do
            printf '    docker pull %s\n' "${ref}"
            printf '    docker save %s -o deps/%s.tar\n' "${ref}" "$(printf '%s' "${ref}" | tr '/:' '__')"
        done
    fi
    if [[ "${MODE}" == "load" ]]; then
        printf '\n  补上镜像归档即可（有外网的机器上 release.sh 默认就会带）。\n'
        printf '  或者改用 ./check-deps.sh --build，看现场构建要补什么。\n'
    else
        printf '\n  注意：只补齐基础镜像并不够。dnf/microdnf 在基础镜像内部访问 Red Hat CDN，\n'
        printf '  还需要一个 rpm 源方案（内网镜像源或预先烤好的基础镜像），否则构建仍会失败。\n'
        printf '  在解决它之前，请改用 --load：镜像归档就在本包内，不需要任何外部依赖。\n'
    fi
    printf '%s\n' "---------------------------------------------------------------"
}

main() {
    local arg
    for arg in "$@"; do
        case "${arg}" in
        --load) MODE="load" ;;
        --build) MODE="build" ;;
        --require) REQUIRE=1 ;;
        help|-h|--help) usage; return 0 ;;
        *) usage >&2; die "未知参数：${arg}" ;;
        esac
    done

    printf '依赖体检：%s\n\n' "${CONTAINER_NAME}"

    case "${MODE}" in
    load) report_load ;;
    build) report_build ;;
    esac

    print_receipt

    if (( REQUIRE )) && (( ${#MISSING[@]} > 0 )); then
        printf '\n' >&2
        printf '依赖不满足，拒绝现场构建（--require）。\n' >&2
        return 1
    fi
    return 0
}

main "$@"
