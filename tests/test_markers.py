"""app.markers 单元测试：编译、越权校验、纯请求环境求值。"""

from __future__ import annotations

import pytest

from app.markers import (
    MarkerError,
    build_environment,
    compile_marker,
    marker_active,
)

ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


def active(marker_text: str, environment: dict[str, str] | None = None) -> bool:
    return marker_active(compile_marker(marker_text), environment or ENV)


# --------------------------------------------------------------------- #
# 空 marker / 无条件
# --------------------------------------------------------------------- #
def test_empty_marker_is_unconditional():
    assert compile_marker("") is None
    assert compile_marker("   ") is None
    assert compile_marker(None) is None
    assert marker_active(None, ENV) is True


# --------------------------------------------------------------------- #
# 三个允许变量的基本求值
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("sys_platform == 'linux'", True),
        ("sys_platform == 'win32'", False),
        ("sys_platform != 'win32'", True),
        ("platform_machine == 'x86_64'", True),
        ("platform_machine == 'arm64'", False),
        ("python_version == '3.11'", True),          # PEP 508 约定 major.minor
        ("python_version == '3.11.2'", False),
        ("python_version >= '3.10'", True),
        ("python_version < '3.12'", True),
        ("python_version >= '3.12'", False),
        ("'linux' == sys_platform", True),            # 变量在右侧
        ("'arm64' != platform_machine", True),
    ],
)
def test_basic_evaluation(text, expected):
    assert active(text) is expected


def test_and_or_not_in_groups():
    assert active("sys_platform == 'linux' and platform_machine == 'x86_64'")
    assert not active("sys_platform == 'linux' and platform_machine == 'arm64'")
    assert active("sys_platform == 'win32' or sys_platform == 'linux'")
    assert active(
        "(sys_platform == 'darwin' or platform_machine == 'x86_64') "
        "and python_version >= '3.10'"
    )
    assert not active(
        "(sys_platform == 'darwin' and python_version >= '3.10') "
        "or platform_machine == 'arm64'"
    )


def test_in_operators():
    assert active("'x86' in platform_machine")
    assert active("'arm' not in platform_machine")
    assert not active("'arm64' in platform_machine")


def test_environment_drives_result_not_host():
    """同样的 marker，换请求环境结果翻转；不依赖运行测试的宿主机。"""
    text = "sys_platform == 'darwin' and platform_machine == 'arm64'"
    mac = {"python_version": "3.12", "sys_platform": "darwin",
           "platform_machine": "arm64"}
    assert active(text, mac) is True
    assert active(text, ENV) is False


@pytest.mark.parametrize(
    "text,expected",
    [
        # 变量在右侧：运算符绝不能被隐式翻转（回归测试）
        ("'3.10' <= python_version", True),
        ("'3.12' <= python_version", False),
        ("'3.12' > python_version", True),
        ("'3.10' > python_version", False),
        ("'3.11' == python_version", True),
        ("'3.11' != python_version", False),
    ],
)
def test_python_version_on_right_hand_side(text, expected):
    assert active(text) is expected


def test_chained_comparison_is_invalid():
    # PEP 508 不支持 a < b < c 链式比较
    with pytest.raises(MarkerError, match="PEP 508"):
        compile_marker("'3.10' < python_version < '4'")


# --------------------------------------------------------------------- #
# 语法非法
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "sys_platform ==",
        "and sys_platform == 'linux'",
        "== 'linux'",
        "python_version",
        "sys_platform == 'linux' garbage",
        "sys_platform 'linux'",
    ],
)
def test_invalid_syntax_raises(bad):
    with pytest.raises(MarkerError, match="PEP 508"):
        compile_marker(bad)


# --------------------------------------------------------------------- #
# 越权变量
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "os_name == 'posix'",
        "python_full_version >= '3.11.2'",
        "platform_system == 'Linux'",
        "platform_release >= '1'",
        "implementation_name == 'cpython'",
        "extra == 'speedups'",
        "sys_platform == 'linux' and os_name == 'posix'",
        "(os_name == 'posix' or sys_platform == 'linux') and python_version >= '3'",
    ],
)
def test_out_of_scope_variable_raises(bad):
    with pytest.raises(MarkerError) as exc:
        compile_marker(bad)
    message = str(exc.value)
    assert "不允许的变量" in message


def test_only_allowed_variables_accepted():
    marker = compile_marker(
        "python_version >= '3.10' and "
        "(sys_platform == 'linux' or platform_machine == 'arm64')"
    )
    assert marker is not None


# --------------------------------------------------------------------- #
# build_environment
# --------------------------------------------------------------------- #
def test_build_environment_ok():
    env = build_environment(ENV)
    assert env == ENV


def test_build_environment_strips_values():
    env = build_environment({
        "python_version": " 3.11 ",
        "sys_platform": " linux\n",
        "platform_machine": " x86_64 ",
    })
    assert env == {"python_version": "3.11",
                   "sys_platform": "linux",
                   "platform_machine": "x86_64"}


@pytest.mark.parametrize("missing", ["python_version", "sys_platform", "platform_machine"])
def test_build_environment_missing_key(missing):
    raw = dict(ENV)
    del raw[missing]
    with pytest.raises(MarkerError, match=missing):
        build_environment(raw)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_build_environment_blank_values(blank):
    with pytest.raises(MarkerError):
        build_environment({"python_version": blank,
                           "sys_platform": "linux",
                           "platform_machine": "x86_64"})


def test_build_environment_bad_python_version():
    with pytest.raises(MarkerError, match="PEP 440"):
        build_environment({"python_version": "three.eleven",
                           "sys_platform": "linux",
                           "platform_machine": "x86_64"})


def test_environment_has_exactly_three_keys():
    """实现自检：求值环境不得夹带宿主机变量。"""
    assert set(build_environment(ENV)) == {
        "python_version", "sys_platform", "platform_machine"
    }
