#!/usr/bin/env bash
#
# 开发用自检：在没有 OceanBase 的机器上验证 POC 自身的检查代码能跑通。
#
# ⚠️ 这个脚本验证的是「harness 写对了没有」，**不是**「OceanBase 兼容不兼容」。
#    MariaDB 与 OceanBase 的差异（没有 ob_query_timeout、没有 ob_compatibility_mode、
#    元数据视图不同）恰恰是 POC 要验证的东西，所以它**不能**用来填结论文档。
#    结果一律以 --runtime mariadb-smoke 落盘，文件名与 OceanBase 运行不会混淆。
#
# 它能抓住的是另一类问题：SQL 写错、控制流写错、驱动 API 用错——这些在只有真实
# OceanBase 才能跑的前提下，原本要等拿到环境才会暴露。
#
# 用法：
#   scripts/mariadb_smoke.sh            # 假定 mariadb 已安装
#   scripts/mariadb_smoke.sh --install  # 先装 mariadb-server 再跑（需 root）
#
set -euo pipefail

POC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${POC_PYTHON:-python3}"
DB=ob_poc_smoke
RUN_USER=ob_poc
OBS_USER=ob_poc_obs

# Throwaway credentials for a throwaway local instance, generated per run rather than
# committed. Hardcoding them would put password-shaped strings in the repository for
# no benefit, and the secret scanners that guard this repo would have to be taught to
# ignore them.
RUN_PASS="poc-$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
OBS_PASS="obs-$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"

if [[ "${1:-}" == "--install" ]]; then
	echo "==> 安装 mariadb-server"
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q mariadb-server
fi

command -v mysql >/dev/null 2>&1 || { echo "缺少 mysql 客户端；用 --install 或自行安装 mariadb-server" >&2; exit 1; }

if ! mysqladmin ping >/dev/null 2>&1; then
	echo "==> 启动 mariadb"
	mkdir -p /var/run/mysqld /var/lib/mysql-files
	chown -R mysql:mysql /var/run/mysqld /var/lib/mysql-files 2>/dev/null || true
	(mysqld_safe >/tmp/mariadb-smoke-server.log 2>&1 &)
	for _ in $(seq 1 30); do
		mysqladmin ping >/dev/null 2>&1 && break
		sleep 1
	done
	mysqladmin ping >/dev/null 2>&1 || { echo "mariadb 启动失败，见 /tmp/mariadb-smoke-server.log" >&2; exit 1; }
fi

echo "==> 准备 schema 与账号"
mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS ${DB};
CREATE TABLE IF NOT EXISTS ${DB}.sample_table (
  id INT PRIMARY KEY,
  name VARCHAR(64) NOT NULL COMMENT 'name column',
  amount DECIMAL(10,2) NULL,
  created_at DATETIME NULL
) COMMENT='poc sample table';
INSERT IGNORE INTO ${DB}.sample_table VALUES (1,'alpha',12.34,NOW());
CREATE USER IF NOT EXISTS '${RUN_USER}'@'%' IDENTIFIED BY '${RUN_PASS}';
ALTER USER '${RUN_USER}'@'%' IDENTIFIED BY '${RUN_PASS}';
GRANT ALL PRIVILEGES ON ${DB}.* TO '${RUN_USER}'@'%';
CREATE USER IF NOT EXISTS '${OBS_USER}'@'%' IDENTIFIED BY '${OBS_PASS}';
ALTER USER '${OBS_USER}'@'%' IDENTIFIED BY '${OBS_PASS}';
GRANT PROCESS ON *.* TO '${OBS_USER}'@'%';
GRANT SELECT ON *.* TO '${OBS_USER}'@'%';
FLUSH PRIVILEGES;
SQL

echo "==> 运行 MySQL 模式全部检查（runtime=mariadb-smoke）"
# Query Timeout 在这里**预期失败**：MariaDB 没有 ob_query_timeout，客户端超时后服务端
# 仍会继续跑完 SLEEP。那正是这个检查应该报告的结果，不是脚本的问题。
#
# 因此这一步会以非零码结束，而脚本开了 `set -e` —— 必须显式接住它，否则脚本会在跑到
# TypeScript 侧之前就退出（这个坑踩过一次）。
PY_EXIT=0
OB_MYSQL_HOST=127.0.0.1 \
OB_MYSQL_PORT=3306 \
OB_MYSQL_USER="${RUN_USER}" \
OB_MYSQL_PASSWORD="${RUN_PASS}" \
OB_MYSQL_DATABASE="${DB}" \
OB_MYSQL_SCHEMA="${DB}" \
OB_MYSQL_OBSERVER_USER="${OBS_USER}" \
OB_MYSQL_OBSERVER_PASSWORD="${OBS_PASS}" \
OB_MYSQL_CONNECT_TIMEOUT_S=2 \
OB_MYSQL_QUERY_TIMEOUT_S=3 \
OB_MYSQL_POOL_MAX=2 \
	"${PYTHON}" "${POC_ROOT}/run_poc.py" --mode mysql --runtime mariadb-smoke --out "${POC_ROOT}/results/smoke" "$@" || PY_EXIT=$?
echo "run_poc.py 退出码：${PY_EXIT}（2 = 有检查未通过；M6 在此后端上预期如此）"

TS_DIR="${POC_ROOT}/runtimes/typescript"
if command -v node >/dev/null 2>&1 && [[ -d "${TS_DIR}/node_modules" ]]; then
	echo
	echo "==> 运行 TypeScript 侧 MySQL 检查（同一后端，同一套环境变量）"
	OB_MYSQL_HOST=127.0.0.1 \
	OB_MYSQL_PORT=3306 \
	OB_MYSQL_USER="${RUN_USER}" \
	OB_MYSQL_PASSWORD="${RUN_PASS}" \
	OB_MYSQL_DATABASE="${DB}" \
	OB_MYSQL_SCHEMA="${DB}" \
	OB_MYSQL_CONNECT_TIMEOUT_S=2 \
	OB_MYSQL_QUERY_TIMEOUT_S=3 \
		node "${TS_DIR}/run.mjs" --mode all --runtime mariadb-smoke --out "${POC_ROOT}/results/smoke" || true
else
	echo
	echo "==> 跳过 TypeScript 侧：未找到 node，或 ${TS_DIR}/node_modules 不存在（先在 runtimes/typescript 下 npm install）"
fi

echo
echo "提示：M6 在此后端上失败是预期结果（服务端没有可用的语句级超时），"
echo "      它恰恰证明「客户端放弃 ≠ 服务端停止」这条判定确实生效。"
