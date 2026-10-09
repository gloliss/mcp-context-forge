#!/usr/bin/env bash
#
# 在隔离网里、拿到交付包之后跑：用包里的源码快照，在内网 GitLab 上把一个项目仓库建起来。
#
# 刻意**只建一个提交**、不带历史（用户的选择）：快照比 git bundle 小得多（约 16MB
# vs 155MB），过人工审核要轻松得多。代价是内网仓库与开发仓库没有共同祖先，将来两边
# 无法直接 merge —— 这一点写在这里备案，不是疏漏。
#
# 只读包内容，所有产出都落在 --work-dir 下，不动交付包本身。
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

readonly MANIFEST="${SCRIPT_DIR}/manifest.json"
readonly STATE_DIR="$(dirname -- "${SCRIPT_DIR}")/state"

WORK_DIR="${CONTEXTFORGE_INNER_REPO_DIR:-${STATE_DIR}/inner-repo}"
REMOTE=""
BRANCH="main"
DO_PUSH=0
DRY_RUN=0
FORCE=0

usage() {
    cat <<'EOF'
用法：
  ./bootstrap-inner-repo.sh                     解出快照、建好本地仓库（不推）
  ./bootstrap-inner-repo.sh --remote <url>      再挂上内网 GitLab 的地址
  ./bootstrap-inner-repo.sh --remote <url> --push
                                                并推上去（需要该地址的凭据）

  --work-dir 目录   仓库放哪（默认 <包目录的上一层>/state/inner-repo）
  --branch 名字     分支名（默认 main）
  --force           目标目录已存在时先删掉重建
  --dry-run         只打印将要做什么

推之前请先在内网 GitLab 上建好空项目（不要勾选「用 README 初始化」）。
EOF
}

run() {
    if (( DRY_RUN )); then
        printf '  [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

# 用 -c 传身份，不去依赖目标机的 git 全局配置 —— 隔离网里的机器多半没配过。
git_quiet() {
    if (( DRY_RUN )); then
        printf '  [dry-run] git %s\n' "$*"
        return 0
    fi
    git -c user.name="ContextForge Delivery" \
        -c user.email="contextforge@local" \
        -c commit.gpgsign=false "$@"
}

main() {
    local arg archive short revision subject
    while (( $# )); do
        case "$1" in
        --work-dir) [[ $# -ge 2 ]] || die "--work-dir 需要参数"; WORK_DIR="$2"; shift ;;
        --work-dir=*) WORK_DIR="${1#*=}" ;;
        --remote) [[ $# -ge 2 ]] || die "--remote 需要参数"; REMOTE="$2"; shift ;;
        --remote=*) REMOTE="${1#*=}" ;;
        --branch) [[ $# -ge 2 ]] || die "--branch 需要参数"; BRANCH="$2"; shift ;;
        --branch=*) BRANCH="${1#*=}" ;;
        --push) DO_PUSH=1 ;;
        --force) FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        help|-h|--help) usage; return 0 ;;
        *) usage >&2; die "未知参数：$1" ;;
        esac
        shift
    done

    (( DO_PUSH == 0 )) || [[ -n "${REMOTE}" ]] || die "--push 需要同时给 --remote"

    archive="$(manifest_value "${MANIFEST}" source_archive)"
    [[ -n "${archive}" ]] || die "本包没有源码快照（release.sh --no-source），无法建仓库"
    [[ -f "${SCRIPT_DIR}/${archive}" ]] || die "源码快照不存在：${SCRIPT_DIR}/${archive}"

    short="$(manifest_value "${MANIFEST}" git_short)"
    revision="$(manifest_value "${MANIFEST}" git_revision)"
    subject="$(manifest_value "${MANIFEST}" git_subject)"

    info "来源快照 ${archive}"
    info "  commit ${short}  ${subject}"
    info "  完整 revision ${revision}"
    info "  目标目录 ${WORK_DIR}（分支 ${BRANCH}）"

    if [[ -e "${WORK_DIR}" ]]; then
        if (( FORCE )); then
            info "已存在，--force 覆盖：${WORK_DIR}"
            run rm -rf -- "${WORK_DIR}"
        else
            die "目标目录已存在：${WORK_DIR}；要覆盖请加 --force，或用 --work-dir 换个位置"
        fi
    fi

    info "解出源码快照"
    run mkdir -p -- "${WORK_DIR}"
    run tar -xzf "${SCRIPT_DIR}/${archive}" -C "${WORK_DIR}" --strip-components=1

    # 装到仓库根，而不是留在 scripts/ci/ 下：GitLab 只认根目录的 .gitlab-ci.yml。
    info "安装 CI 配置到仓库根 .gitlab-ci.yml"
    if (( DRY_RUN )); then
        printf '  [dry-run] cp %s/scripts/ci/gitlab-ci.yml %s/.gitlab-ci.yml\n' "${WORK_DIR}" "${WORK_DIR}"
    elif [[ -f "${WORK_DIR}/scripts/ci/gitlab-ci.yml" ]]; then
        cp -- "${WORK_DIR}/scripts/ci/gitlab-ci.yml" "${WORK_DIR}/.gitlab-ci.yml"
    else
        warn "快照里没有 scripts/ci/gitlab-ci.yml，内网仓库将不含 CI 配置"
    fi

    info "初始化仓库并建立首次提交"
    # 一律用 git -C，不 cd 进工作目录：--dry-run 下那个目录根本不存在，
    # 一旦 cd 进去就会在"演练"里报一个没有意义的错误。
    git_quiet -C "${WORK_DIR}" init -q -b "${BRANCH}" 2>/dev/null \
        || {
            git_quiet -C "${WORK_DIR}" init -q
            git_quiet -C "${WORK_DIR}" symbolic-ref HEAD "refs/heads/${BRANCH}"
        }
    git_quiet -C "${WORK_DIR}" add -A
    git_quiet -C "${WORK_DIR}" commit -q -m "从交付包快照建立内网仓库

对应开发仓库 commit ${short}：${subject}
完整 revision：${revision}

本仓库由交付包内的源码快照建立，仅含此一个提交、不含历史。"

    if [[ -n "${REMOTE}" ]]; then
        info "设置远端 ${REMOTE}"
        git_quiet -C "${WORK_DIR}" remote remove origin 2>/dev/null || true
        git_quiet -C "${WORK_DIR}" remote add origin "${REMOTE}"
    fi

    if (( DO_PUSH )); then
        info "推送到 ${REMOTE}（${BRANCH}）"
        git -C "${WORK_DIR}" push -u origin "${BRANCH}"
    fi

    printf '\n'
    if (( DRY_RUN )); then
        printf '演练结束，未做任何改动。\n'
        return 0
    fi
    printf '内网仓库已就绪：%s\n' "${WORK_DIR}"
    if [[ -z "${REMOTE}" ]]; then
        printf '接下来：\n'
        printf '  在内网 GitLab 上新建空项目（不要用 README 初始化），然后：\n'
        printf '    cd %s\n' "${WORK_DIR}"
        printf '    git remote add origin <项目地址>\n'
        printf '    git push -u origin %s\n' "${BRANCH}"
    elif (( DO_PUSH == 0 )); then
        printf '远端已设置但未推送。确认无误后：\n'
        printf '  cd %s && git push -u origin %s\n' "${WORK_DIR}" "${BRANCH}"
    else
        printf 'CI 配置已推到仓库根（.gitlab-ci.yml）。\n'
        printf '内网项目里需要设置 CI/CD 变量 CONTEXTFORGE_CI=1 管线才会启用；\n'
        printf 'runner 的安装与注册只能在隔离网内手工完成，步骤见 README-DEPLOY.txt。\n'
    fi
}

main "$@"
