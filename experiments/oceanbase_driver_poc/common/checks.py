"""The verification items the requirement asks for, one tuple per compatibility mode.

These are declared as data rather than as methods so that the requirement's list and
the code cannot drift apart: the harness iterates this registry, so a check that is
not declared here cannot silently be reported as covered.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckSpec:
    """One verification item from the requirement."""

    check_id: str
    name: str
    description: str
    requires_server_observation: bool = False


MYSQL_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("M1", "建立连接", "以环境变量提供的连接信息建立连接，并读回实例版本与兼容模式"),
    CheckSpec("M2", "SELECT 1 / 简单查询", "SELECT 1 与多类型列的简单查询返回预期结果"),
    CheckSpec("M3", "参数绑定", "占位符绑定返回正确结果，特殊字符参数不改变 SQL 结构"),
    CheckSpec("M4", "Connection Pool", "借出/归还/复用可达，池满有界等待，失效连接被剔除"),
    CheckSpec("M5", "Connection Timeout", "对不可达地址在配置期限内返回可辨识错误"),
    CheckSpec("M6", "Query Timeout", "期限内收到超时错误，且服务端查询确实停止", requires_server_observation=True),
    CheckSpec("M7", "Metadata 查询", "表、列、类型、可空、注释与基准一致"),
    CheckSpec("M8", "Connection close / reconnect", "close 后复用可辨识报错，重连成功，失效检测有效"),
    CheckSpec("M9", "异常处理", "认证失败/对象不存在/语法错误/权限不足映射到稳定错误码"),
)

ORACLE_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("O1", "建立连接", "以环境变量提供的连接信息建立连接，并读回实例版本与兼容模式"),
    CheckSpec("O2", "简单 SELECT", "含 FROM DUAL 的简单查询返回预期结果"),
    CheckSpec("O3", "Named Bind Parameter", "命名绑定返回正确结果，特殊字符与类型边界参数不改变 SQL 结构"),
    CheckSpec("O4", "Connection Pool", "借出/归还/复用可达，池满有界等待，失效连接被剔除"),
    CheckSpec("O5", "Connection Timeout", "对不可达地址在配置期限内返回可辨识错误"),
    CheckSpec("O6", "Query Timeout", "期限内收到超时错误，且服务端查询确实停止", requires_server_observation=True),
    CheckSpec("O7", "Schema / Table / Column Metadata", "字典视图与反射路径一致，标识符大小写与引用语义保留"),
    CheckSpec("O8", "Connection close / reconnect", "close 后复用可辨识报错，重连成功，失效检测有效"),
    CheckSpec("O9", "异常处理", "原生 ORA 码映射到稳定错误码，未识别码保留原始值"),
)

CHECKS: dict[str, tuple[CheckSpec, ...]] = {"mysql": MYSQL_CHECKS, "oracle": ORACLE_CHECKS}


def checks_for(mode: str) -> tuple[CheckSpec, ...]:
    """Return the verification items for one compatibility mode.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.

    Returns:
        The mode's check specifications, in requirement order.

    Raises:
        KeyError: If ``mode`` is not a known compatibility mode.
    """
    return CHECKS[mode]
