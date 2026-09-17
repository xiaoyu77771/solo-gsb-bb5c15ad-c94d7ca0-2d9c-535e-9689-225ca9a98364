"""PEP 508 environment marker 的编译、越权校验与求值。

每条依赖可携带一个 marker（如 ``sys_platform == 'linux'``），是否激活完全
由请求体中 ``target_environment`` 给出的三个变量决定：

* ``python_version``
* ``sys_platform``
* ``platform_machine``

安全约束：

* marker 中只允许引用上述三个变量；引用任何其它变量（``os_name``、
  ``python_full_version``、``extra`` ……）一律视为非法，由上层映射为 422；
* 求值环境只包含请求给出的三个键，**绝不读取宿主机**（不调用
  ``packaging.markers.default_environment`` / ``platform`` / ``sys``），
  因此同一请求在任何机器上结果一致。
"""

from __future__ import annotations

import operator
from typing import Any

from packaging.markers import InvalidMarker, Marker, Op, Value, Variable
from packaging.specifiers import InvalidSpecifier, Specifier
from packaging.version import InvalidVersion, Version

# marker 中允许出现的全部变量；其余引用一律拒绝。
ALLOWED_MARKER_VARIABLES: frozenset[str] = frozenset(
    {"python_version", "sys_platform", "platform_machine"}
)

# target_environment 的必填键（也是求值环境的全部键）。
ENVIRONMENT_KEYS: tuple[str, ...] = (
    "python_version",
    "sys_platform",
    "platform_machine",
)

# 按 PEP 508，python_version 使用 PEP 440 版本语义比较（含 == 的前缀匹配）；
# 其余两个变量按字符串比较。
_RELATIONAL_OPS = {"==", "!=", "<", "<=", ">", ">="}
_STRING_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "in": lambda lhs, rhs: lhs in rhs,
    "not in": lambda lhs, rhs: lhs not in rhs,
}


class MarkerError(ValueError):
    """marker 语法非法、引用越界，或 target_environment 非法。"""


def compile_marker(raw: str | None) -> Marker | None:
    """把 marker 文本解析为 packaging Marker；空串/None 表示无条件依赖。

    语法非法或引用了允许名单之外的变量时抛 MarkerError。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        marker = Marker(text)
    except InvalidMarker as exc:
        raise MarkerError(f"{text!r} 不是合法的 PEP 508 marker：{exc}") from exc

    used = _collect_variables(marker._markers)
    illegal = sorted(used - ALLOWED_MARKER_VARIABLES)
    if illegal:
        raise MarkerError(
            f"marker {text!r} 引用了不允许的变量 {illegal}；"
            f"仅允许引用 {sorted(ALLOWED_MARKER_VARIABLES)}"
        )
    return marker


def _collect_variables(nodes: list[Any]) -> set[str]:
    """遍历 marker AST 收集所有被引用的变量名（兼容括号产生的嵌套 list）。"""
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, list):
            found |= _collect_variables(node)
        elif isinstance(node, tuple):
            for side in (node[0], node[2]):
                if isinstance(side, Variable):
                    found.add(side.value)
    return found


def evaluate_marker(marker: Marker, environment: dict[str, str]) -> bool:
    """在仅含三个请求变量的环境上求 marker 的布尔值。

    与 packaging 的 AND/OR 分组语义一致：or 切组（组间 any），
    组内全部为 and（all）；不查询任何宿主机信息。
    """
    return _evaluate_nodes(marker._markers, environment)


def marker_active(marker: Marker | None, environment: dict[str, str]) -> bool:
    """无 marker（无条件依赖）恒激活；否则按目标环境求值。"""
    return marker is None or evaluate_marker(marker, environment)


def _evaluate_nodes(nodes: list[Any], environment: dict[str, str]) -> bool:
    groups: list[list[bool]] = [[]]
    for node in nodes:
        if isinstance(node, list):  # 括号子表达式
            groups[-1].append(_evaluate_nodes(node, environment))
        elif isinstance(node, tuple):
            groups[-1].append(_evaluate_comparison(node, environment))
        elif node == "or":
            groups.append([])
        elif node == "and":
            pass
        else:  # pragma: no cover - packaging 解析结果不应出现其它节点
            raise MarkerError(f"无法识别的 marker 节点：{node!r}")
    return any(all(group) for group in groups)


def _evaluate_comparison(node: tuple, environment: dict[str, str]) -> bool:
    lhs, op, rhs = node
    assert isinstance(op, Op)
    op_text = op.serialize()

    # 取出两侧的实际字符串值（变量侧查环境，字面量侧取 Value）。
    lhs_value = _side_value(lhs, environment)
    rhs_value = _side_value(rhs, environment)
    variable_side = lhs if isinstance(lhs, Variable) else (
        rhs if isinstance(rhs, Variable) else None
    )

    if (
        variable_side is not None
        and variable_side.value == "python_version"
        and op_text in _RELATIONAL_OPS
    ):
        # 与 packaging 完全一致：谓词永远由“右侧 token”构成，再检查
        # “左侧 token”是否被包含。这样变量在左（python_version <= '3.10'）
        # 或在右（'3.10' <= python_version）都无需翻转运算符即可正确求值。
        try:
            spec = Specifier(f"{op_text}{rhs_value}")
        except InvalidSpecifier:
            pass  # 字面量无法构成版本谓词时退回字符串比较（同 packaging）
        else:
            # prereleases=True 与 packaging marker 语义一致；
            # == 自动获得 PEP 440 的前缀匹配（'3.11' 匹配 3.11.* ）。
            return spec.contains(lhs_value, prereleases=True)

    try:
        return _STRING_OPS[op_text](lhs_value, rhs_value)
    except KeyError as exc:  # pragma: no cover - 解析器保证 op 合法
        raise MarkerError(f"marker 包含不支持的运算符 {op_text!r}") from exc


def _side_value(node: Any, environment: dict[str, str]) -> str:
    if isinstance(node, Variable):
        # compile_marker 已保证变量在允许名单内；环境恰好包含这三个键。
        return environment[node.value]
    if isinstance(node, Value):
        return node.value
    raise MarkerError(f"无法识别的 marker 操作数：{node!r}")  # pragma: no cover


def build_environment(raw: dict[str, str]) -> dict[str, str]:
    """从请求字典构造求值环境并校验。

    * 三个键全部必填且去空白后非空；
    * python_version 必须是合法的 PEP 440 版本（如 ``3.11`` / ``3.12.3``）；
    * 不补充任何宿主机派生值。
    """
    env: dict[str, str] = {}
    for key in ENVIRONMENT_KEYS:
        value = (raw.get(key) or "").strip()
        if not value:
            raise MarkerError(f"target_environment.{key} 为必填项且不能为空")
        env[key] = value

    try:
        Version(env["python_version"])
    except InvalidVersion as exc:
        raise MarkerError(
            f"target_environment.python_version {env['python_version']!r} "
            "不是合法的 PEP 440 版本"
        ) from exc
    return env
