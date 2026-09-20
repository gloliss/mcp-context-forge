#!/usr/bin/env python3
"""Executable entry point for the OceanBase dual-mode driver POC.

Run it from anywhere; the POC root is put on ``sys.path`` first so the ``common``,
``mysql_mode``, and ``oracle_mode`` packages resolve.

    python experiments/oceanbase_driver_poc/run_poc.py --mode all

Exit status is ``0`` when nothing failed, ``2`` when a check failed, errored, or was
indeterminate, and ``1`` for a usage or configuration problem. A mode with no
configured environment reports ``SKIP_NO_ENV`` and does not by itself make the run
unclean -- pass ``--fail-on-skip`` to make it do so.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parent
if str(POC_ROOT) not in sys.path:
    sys.path.insert(0, str(POC_ROOT))

from common.config import ConnectionConfig, load_config, missing_required  # noqa: E402
from common.harness import Driver, run_mode  # noqa: E402
from common.redaction import Redactor  # noqa: E402
from common.report import render_markdown, render_summary, render_table  # noqa: E402
from common.results import ABSTAINED_STATUSES, UNCLEAN_STATUSES, RunReport  # noqa: E402
from mysql_mode import driver as mysql_driver  # noqa: E402
from oracle_mode import driver as oracle_driver  # noqa: E402

MODES = ("mysql", "oracle")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_UNHEALTHY = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Argument list; defaults to :data:`sys.argv[1:]`.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="OceanBase MySQL / Oracle 双模式 Driver POC")
    parser.add_argument("--mode", choices=(*MODES, "all"), default="all", help="要验证的兼容模式")
    parser.add_argument("--runtime", default="python", help="运行时标签，写入结果报告")
    parser.add_argument("--out", default=str(POC_ROOT / "results"), help="results JSON 输出目录")
    parser.add_argument("--md-out", default=None, help="可选：把结论文档的实测段落写到此 Markdown 文件")
    parser.add_argument("--fail-on-skip", action="store_true", help="存在未执行或未被驱动支持的检查项时也返回非零退出码")
    parser.add_argument("--print-json", action="store_true", help="同时把结果 JSON 打到 stdout")
    return parser.parse_args(argv)


def build_redactor() -> Redactor:
    """Collect every configured password so it can be masked wherever it appears.

    Returns:
        A redactor holding the passwords of every mode, regardless of which mode is
        being run, so a value cannot leak through a stray cross-mode message. The
        observer account's password is included: it is a second credential, and
        registering only the first would leave it unredacted.
    """
    secrets: list[str] = []
    for mode in MODES:
        config = load_config(mode)
        if config is not None:
            secrets.extend(config.secrets)
    return Redactor(secrets)


def build_driver(mode: str, redactor: Redactor) -> Driver:
    """Build the driver adapter for one mode.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.
        redactor: The redactor carrying this run's secret values.

    Returns:
        The mode's driver adapter, wrapping whatever package is installed.
    """
    if mode == "mysql":
        build: Callable[[Redactor], Driver] = mysql_driver.driver  # noqa: PLC0415 - mode-specific import
    else:
        build = oracle_driver.driver  # noqa: PLC0415 - mode-specific import
    return build(redactor)


def run_one_mode(mode: str, runtime: str, redactor: Redactor) -> RunReport:
    """Run every check for one mode.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.
        runtime: The runtime label recorded on the report.
        redactor: The redactor carrying this run's secret values.

    Returns:
        The report, whether or not the mode had a reachable target.
    """
    config: ConnectionConfig | None = load_config(mode)
    driver = build_driver(mode, redactor)
    return run_mode(
        mode,
        config=config,
        driver=driver,
        redactor=redactor,
        missing_env=missing_required(mode),
        runtime=runtime,
        driver_info={
            "name": getattr(driver, "name", mode),
            "version": getattr(driver, "version", "unknown"),
            "installed": getattr(driver, "installed", False),
        },
    )


def write_reports(reports: list[RunReport], out_dir: Path, runtime: str, redactor: Redactor) -> list[Path]:
    """Write each report to a timestamped JSON file.

    Args:
        reports: The reports to persist.
        out_dir: Destination directory, created if absent.
        runtime: The runtime label, part of the file name.
        redactor: Redactor applied to the serialised payload as a final guard.

    Returns:
        The paths written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written: list[Path] = []
    for report in reports:
        path = out_dir / f"{report.mode}-{runtime}-{stamp}.json"
        payload = redactor.redact_deep(report.to_dict())
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    """Run the POC.

    Args:
        argv: Argument list; defaults to :data:`sys.argv[1:]`.

    Returns:
        The process exit code.
    """
    args = parse_args(argv)
    redactor = build_redactor()
    modes = MODES if args.mode == "all" else (args.mode,)

    try:
        reports = [run_one_mode(mode, args.runtime, redactor) for mode in modes]
    except KeyError as exc:
        print(redactor.redact(f"配置错误：{exc}"), file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(redactor.redact(f"配置错误：{exc}"), file=sys.stderr)
        return EXIT_USAGE

    for path in write_reports(reports, Path(args.out), args.runtime, redactor):
        print(f"结果已写入 {path}")

    print()
    print(render_table(reports))
    print()
    print(render_summary(reports))

    if args.md_out:
        Path(args.md_out).write_text(render_markdown(reports), encoding="utf-8")
        print(f"\n结论文档段落已写入 {args.md_out}")

    if args.print_json:
        payload = redactor.redact_deep([report.to_dict() for report in reports])
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    checks = [check for report in reports for check in report.checks]
    unclean = [check for check in checks if check.status in UNCLEAN_STATUSES]
    abstained = [check for check in checks if check.status in ABSTAINED_STATUSES]

    if unclean:
        print(f"\n有 {len(unclean)} 项检查失败、异常或无法判定。", file=sys.stderr)
        return EXIT_UNHEALTHY

    if abstained and args.fail_on_skip:
        print(f"\n有 {len(abstained)} 项未执行或未被驱动支持。", file=sys.stderr)
        return EXIT_UNHEALTHY

    if abstained:
        print(f"\n注意：{len(abstained)} 项未执行或未被驱动支持，未验证项不得据本表宣称通过。")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
