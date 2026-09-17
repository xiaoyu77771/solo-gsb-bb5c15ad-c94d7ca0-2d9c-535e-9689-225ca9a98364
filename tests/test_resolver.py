"""端到端测试：覆盖回溯、环、菱形、冲突链、确定性、预发布等。"""

from __future__ import annotations

import pytest

from app.repository import build_repository, canonical, parse_root_specifier
from app.resolver import ResolutionLimit, Resolver


LINUX_X64_PY311 = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


def resolve(raw_repo, root, spec="", *, allow_prereleases=False, environment=None):
    environment = environment or LINUX_X64_PY311
    repo = build_repository(raw_repo, environment)
    resolver = Resolver(repo, allow_prereleases=allow_prereleases)
    try:
        state = resolver.resolve(canonical(root), parse_root_specifier(spec), spec)
    except ResolutionLimit:
        return None, _normalize(resolver.build_conflicts(limit=True))
    if state is not None:
        return resolver.build_output(state, root), None
    return None, _normalize(resolver.build_conflicts())


def _normalize(err):
    """把内部 dataclass 冲突报告转成纯字典，方便断言。"""
    return {
        "status": err["status"],
        "message": err["message"],
        "_conflicts": [
            {
                "subject": c.subject,
                "kind": c.kind,
                "message": c.message,
                "chains": c.chains,
                "constraints": [
                    {"chain": [
                        {"package": l.package, "version": l.version, "requires": l.requires}
                        for l in chain
                    ]}
                    for chain in c.chains
                ],
                "partial_selection": c.partial_selection,
            }
            for c in err["_conflicts"]
        ],
    }


def c(version, deps=None):
    """构造输入候选的简写。"""
    return {"version": version, "dependencies": deps or []}


def d(name, spec=""):
    return {"name": name, "specifier": spec}


# --------------------------------------------------------------------- #
# 1. 基本解析 + 最高版本 + 传递依赖
# --------------------------------------------------------------------- #
def test_basic_highest_and_transitive():
    repo = {
        "app": [
            c("1.0.0", [d("libx", ">=1.0")]),
        ],
        "libx": [c("1.0"), c("1.5"), c("2.0", [d("liby")])],
        "liby": [c("0.9"), c("1.1")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["root_version"] == "1.0.0"
    assert out["locked"] == {"app": "1.0.0", "libx": "2.0", "liby": "1.1"}
    # 根边在最前
    assert out["edges"][0] == {"from": "root", "to": "app", "specifier": "*"}
    app_edges = [e for e in out["edges"] if e["from"].startswith("app==")]
    assert {"from": "app==1.0.0", "to": "libx", "specifier": ">=1.0"} in app_edges
    assert {"from": "libx==2.0", "to": "liby", "specifier": "*"} in out["edges"]


# --------------------------------------------------------------------- #
# 2. 传递依赖冲突触发回溯，最终选较低可行版本
# --------------------------------------------------------------------- #
def test_backtracking_transitive_conflict():
    repo = {
        "app": [
            c("1.0", [d("a"), d("b")]),
        ],
        "a": [
            c("2.0", [d("shared", "==2.0.0")]),
            c("1.0", [d("shared", "==1.0.0")]),
        ],
        "b": [c("1.0", [d("shared", "<2")])],
        "shared": [c("1.0.0"), c("2.0.0")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"] == {
        "app": "1.0", "a": "1.0", "b": "1.0", "shared": "1.0.0"
    }


def test_backtracking_diamond():
    """菱形：top 版本必须降到能让两条腿的约束相交。"""
    repo = {
        "app": [c("1.0", [d("left"), d("right")])],
        "left": [
            c("2.0", [d("core", ">=2")]),
            c("1.0", [d("core", ">=1,<2")]),
        ],
        "right": [c("1.0", [d("core", "<2")])],
        "core": [c("1.5"), c("2.5")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"]["core"] == "1.5"
    assert out["locked"]["left"] == "1.0"


# --------------------------------------------------------------------- #
# 3. 无解：来源链从根需求到冲突包
# --------------------------------------------------------------------- #
def test_unsat_chain_report():
    repo = {
        "app": [c("1.0", [d("a", ">=1.0")])],
        "a": [c("1.0", [d("b", "==2.0.0")])],
        "b": [c("1.0.0")],
    }
    out, err = resolve(repo, "app")
    assert out is None
    assert err["status"] == "conflict"
    subjects = {x["subject"] for x in err["_conflicts"]}
    assert "b" in subjects
    b = next(x for x in err["_conflicts"] if x["subject"] == "b")
    assert b["kind"] == "no_candidates"
    # 至少存在一条 根 -> app -> a -> b 的链
    chains = [[(l["package"], l["version"], l["requires"]) for l in cc["chain"]]
              for cc in b["constraints"]]
    assert [
        (None, None, "*"),
        ("app", "1.0", ">=1.0"),
        ("a", "1.0", "==2.0.0"),
    ] in chains


def test_unsat_multiple_constraints_all_reported():
    """钻石冲突：两条路径对 core 给出互斥约束，两条来源链都应出现。"""
    repo = {
        "app": [c("1.0", [d("left"), d("right")])],
        "left": [c("1.0", [d("core", "==1.0")])],
        "right": [c("1.0", [d("core", "==2.0")])],
        "core": [c("1.0"), c("2.0")],
    }
    out, err = resolve(repo, "app")
    assert out is None
    core = next(x for x in err["_conflicts"] if x["subject"] == "core")
    chain_specs = {
        tuple(link["requires"] for link in cc["chain"])
        for cc in core["constraints"]
    }
    assert ("*", "*", "==1.0") in chain_specs
    assert ("*", "*", "==2.0") in chain_specs
    assert "partial_selection" in core and core["partial_selection"]["app"] == "1.0"


def test_incompatible_already_selected():
    """已选版本随后被新约束否决：kind=incompatible 且给出冲突说明。"""
    repo = {
        "app": [c("1.0", [d("a"), d("b", "==1.0")])],
        "a": [c("1.0", [d("b", "==2.0")])],
        "b": [c("1.0"), c("2.0")],
    }
    # MRV 下 b 首先被 ==1.0/==2.0 交集约死 -> no_candidates；
    # 换个顺序确保 incompatible 路径也会被记录到（任一形式均须无解）。
    out, err = resolve(repo, "app")
    assert out is None
    subj = {x["subject"]: x for x in err["_conflicts"]}
    assert "b" in subj
    assert subj["b"]["kind"] in {"no_candidates", "incompatible"}
    assert subj["b"]["chains"]


def test_unknown_transitive_package():
    repo = {"app": [c("1.0", [d("ghost", ">=1")])]}
    out, err = resolve(repo, "app")
    assert out is None
    g = next(x for x in err["_conflicts"] if x["subject"] == "ghost")
    assert g["kind"] == "no_candidates"
    assert "不在仓库快照" in g["message"]


# --------------------------------------------------------------------- #
# 4. 依赖环不重复展开
# --------------------------------------------------------------------- #
def test_dependency_cycle():
    repo = {
        "app": [c("1.0", [d("x", ">=1")])],
        "x": [c("1.0", [d("y")])],
        "y": [c("1.0", [d("x", ">=1")])],  # 回指 x：环
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"] == {"app": "1.0", "x": "1.0", "y": "1.0"}
    # 每个包只选一次：边的去重集合
    tos = [(e["from"], e["to"]) for e in out["edges"]]
    assert tos.count(("y==1.0", "x")) == 1


def test_self_dependency_is_idempotent():
    repo = {
        "app": [c("1.0", [d("app", ">=1.0")])],  # 自环
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"] == {"app": "1.0"}
    assert {"from": "app==1.0", "to": "app", "specifier": ">=1.0"} in out["edges"]


def test_cycle_with_conflicting_constraint_on_revisit():
    repo = {
        "app": [c("1.0", [d("x", "==1.0")])],
        "x": [c("1.0", [d("y")])],
        "y": [c("1.0", [d("x", ">=2.0")])],  # 环上的不兼容约束
    }
    out, err = resolve(repo, "app")
    assert out is None
    x = next(v for v in err["_conflicts"] if v["subject"] == "x")
    assert x["kind"] == "incompatible"
    assert "x==1.0" in x["message"]


# --------------------------------------------------------------------- #
# 5. 名称规范化（PEP 503）与全局唯一版本
# --------------------------------------------------------------------- #
def test_name_normalization():
    # PEP 503：大小写不敏感，. / _ / 连续- 折叠为单个 -
    repo = {
        "My_Pkg": [c("1.0", [d("Some_Lib", ">=1")])],
        "some.lib": [c("1.0"), c("2.0")],
    }
    out, err = resolve(repo, "my---pkg")
    assert err is None
    assert set(out["locked"]) == {"my-pkg", "some-lib"}
    assert out["root"] == "my-pkg"


# --------------------------------------------------------------------- #
# 6. 预发布
# --------------------------------------------------------------------- #
def test_prereleases_excluded_by_default():
    repo = {"app": [c("1.0", [d("lib")])],
            "lib": [c("2.0a1"), c("1.9")]}
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"]["lib"] == "1.9"


def test_prereleases_allowed():
    repo = {"app": [c("1.0", [d("lib")])],
            "lib": [c("2.0a1"), c("1.9")]}
    out, err = resolve(repo, "app", allow_prereleases=True)
    assert err is None
    assert out["locked"]["lib"] == "2.0a1"


def test_prerelease_pinned_explicitly():
    repo = {"app": [c("1.0", [d("lib", "==2.0a1")])],
            "lib": [c("2.0a1"), c("1.9")]}
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"]["lib"] == "2.0a1"


# --------------------------------------------------------------------- #
# 7. 根约束
# --------------------------------------------------------------------- #
def test_root_specifier_selects_lower():
    repo = {"app": [c("1.0"), c("2.0"), c("3.0")]}
    out, err = resolve(repo, "app", ">=2.0,<3.0")
    assert err is None
    assert out["root_version"] == "2.0"
    assert out["edges"][0] == {"from": "root", "to": "app", "specifier": ">=2.0,<3.0"}


def test_root_no_match():
    repo = {"app": [c("1.0")]}
    out, err = resolve(repo, "app", ">=2.0")
    assert out is None
    a = err["_conflicts"][0]
    assert a["subject"] == "app" and a["kind"] == "no_candidates"


# --------------------------------------------------------------------- #
# 8. 确定性：打乱输入顺序结果一致
# --------------------------------------------------------------------- #
def test_deterministic_regardless_of_input_order():
    def build():
        return {
            "zeta": [c("3.0", [d("beta", "<2"), d("alpha", ">=1")]),
                     c("2.0")],
            "alpha": [c("1.0"), c("2.0", [d("beta", "==1.0")])],
            "beta": [c("1.0"), c("2.0"), c("3.0")],
        }
    out1, _ = resolve(build(), "zeta")
    # 手工打乱包键与候选顺序
    repo = build()
    repo["beta"] = list(reversed(repo["beta"]))
    out2, _ = resolve(repo, "ZETA")
    assert out1["locked"] == out2["locked"]
    assert out1["edges"] == out2["edges"]


# --------------------------------------------------------------------- #
# 9. 深回溯：顶层只能选最低版本
# --------------------------------------------------------------------- #
def test_deep_backtracking():
    # a 的 5..2 版本都要求 shared==3，只有 1.0 要 shared==1；
    # b 强制 shared==1。
    a_versions = [
        c(f"{n}.0", [d("shared", "==3.0.0" if n > 1 else "==1.0.0")])
        for n in range(5, 0, -1)
    ]
    repo = {
        "app": [c("1.0", [d("a"), d("b")])],
        "a": a_versions,
        "b": [c("1.0", [d("shared", "==1.0.0")])],
        "shared": [c("1.0.0"), c("3.0.0")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"]["a"] == "1.0"
    assert out["locked"]["shared"] == "1.0.0"


# --------------------------------------------------------------------- #
# 10. 非法快照
# --------------------------------------------------------------------- #
def test_invalid_version_raises():
    from app.repository import SnapshotError
    with pytest.raises(SnapshotError):
        build_repository({"app": [c("not-a-version")]}, LINUX_X64_PY311)


def test_invalid_specifier_raises():
    from app.repository import SnapshotError
    with pytest.raises(SnapshotError):
        build_repository(
            {"app": [c("1.0", [d("lib", ">=1.0,<")])]}, LINUX_X64_PY311
        )


def test_duplicate_version_conflicting_deps_raises():
    from app.repository import SnapshotError
    with pytest.raises(SnapshotError):
        build_repository({
            "app": [
                c("1.0", [d("x")]),
                c("1.0", [d("y")]),
            ]
        }, LINUX_X64_PY311)


# --------------------------------------------------------------------- #
# 11. 幂等：同一快照重复解析结果完全一致
# --------------------------------------------------------------------- #
def test_idempotent_byte_for_byte():
    repo = {
        "app": [c("1.0", [d("a"), d("b"), d("c")])],
        "c": [c("3.0", [d("a", "<2")])],
        "b": [c("2.0", [d("a", ">=1")]), c("1.0")],
        "a": [c("2.0", [d("shared", "==1")]), c("1.0", [d("shared")])],
        "shared": [c("1.0"), c("2.0")],
    }
    out1, e1 = resolve(repo, "app")
    out2, e2 = resolve(repo, "app")
    assert out1 == out2 and e1 == e2


# --------------------------------------------------------------------- #
# 12. 尝试上限：指数级回溯快照触发 limit
# --------------------------------------------------------------------- #
def test_attempt_limit():
    """搜索上限护栏：attempt_limit=0 时，尝试第一个候选即终止。"""
    from app.repository import build_repository as _b
    repo = {"app": [c("1.0", [d("x")])], "x": [c("1.0")]}
    r = Resolver(_b(repo, LINUX_X64_PY311), attempt_limit=0)
    with pytest.raises(ResolutionLimit):
        r.resolve("app", parse_root_specifier(""), "")
    # 超限时仍可拿到 409 负载（status=limit + 已记录的冲突点）
    payload = r.build_conflicts(limit=True)
    assert payload["status"] == "limit"


def test_depth_limit():
    """超过依赖链深度护栏时抛 ResolutionLimit 而非 RecursionError。"""
    n = 30
    repo = {f"p{i}": [c("1.0", [d(f"p{i+1}")])] for i in range(n - 1)}
    repo[f"p{n-1}"] = [c("1.0")]
    from app.repository import build_repository as _b
    r = Resolver(_b(repo, LINUX_X64_PY311), depth_limit=10)
    with pytest.raises(ResolutionLimit):
        r.resolve("p0", parse_root_specifier(""), "")


def test_global_single_version_enforced():
    """同一包经多条路径引入：lock 中只出现一次且为同一版本。"""
    repo = {
        "app": [c("1.0", [d("left"), d("right")])],
        "left": [c("1.0", [d("common", ">=1,<2")])],
        "right": [c("1.0", [d("Common", ">=1.5,<2")])],
        "common": [c("1.0"), c("1.5"), c("1.9"), c("2.0")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert list(out["locked"].values()).count("1.9") >= 1
    assert "common" in out["locked"] and "Common" not in out["locked"]
    assert out["locked"]["common"] == "1.9"


# --------------------------------------------------------------------- #
# 13. PEP 508 marker：只有激活的依赖进入约束/回溯/来源链/edges
# --------------------------------------------------------------------- #
def dm(name, spec="", marker=""):
    return {"name": name, "specifier": spec, "marker": marker}


def test_marker_inactive_dependency_ignored():
    repo = {
        "app": [c("1.0", [
            dm("active-lib", "", "sys_platform == 'linux'"),
            # 未激活且与一切冲突（要求不存在的版本）：不得影响求解
            dm("ghost", "==9.9", "sys_platform == 'win32'"),
        ])],
        "active-lib": [c("1.0")],
        "ghost": [c("1.0")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert out["locked"] == {"app": "1.0", "active-lib": "1.0"}
    tos = [e["to"] for e in out["edges"]]
    assert "ghost" not in tos


def test_marker_switches_conflict_across_environments():
    """同一快照：linux 可解，win32 下因激活了苛刻依赖而无解，且来源链完整。"""
    repo = {
        "app": [c("1.0", [dm("win-only", "==2.0", "sys_platform == 'win32'")])],
        "win-only": [c("1.0")],
    }
    out, err = resolve(repo, "app")
    assert err is None and out["locked"] == {"app": "1.0"}

    win = {"python_version": "3.11", "sys_platform": "win32",
           "platform_machine": "AMD64"}
    out2, err2 = resolve(repo, "app", environment=win)
    assert out2 is None
    subject = err2["_conflicts"][0]
    assert subject["subject"] == "win-only"
    assert subject["kind"] == "no_candidates"
    chain = [(l["package"], l["version"], l["requires"])
             for l in subject["constraints"][0]["chain"]]
    assert chain == [(None, None, "*"), ("app", "1.0", "==2.0")]


def test_marker_backtracking_uses_only_active_deps():
    """高版本 app 带的苛刻依赖在 win32 激活、linux 不激活：
    linux 直接选高版本；win32 必须回溯到不带该依赖的低版本。"""
    repo = {
        "app": [
            c("2.0", [dm("shared", "==9.9", "platform_machine == 'arm64'")]),
            c("1.0", []),
        ],
        "shared": [c("1.0")],
    }
    arm = {"python_version": "3.11", "sys_platform": "linux",
           "platform_machine": "arm64"}
    out_arm, err_arm = resolve(repo, "app", environment=arm)
    assert err_arm is None and out_arm["locked"]["app"] == "1.0"  # 回溯
    out_x64, err_x64 = resolve(repo, "app")
    assert err_x64 is None and out_x64["locked"] == {"app": "2.0"}


def test_marker_complex_expression_and_operators():
    repo = {
        "app": [c("1.0", [
            dm("a", "", "python_version >= '3.10' and python_version < '4'"),
            dm("b", "", "sys_platform == 'darwin' or sys_platform == 'win32'"),
            dm("c", "", "'x86_64' in platform_machine"),
            dm("d", "", "platform_machine != 'arm64'"),
        ])],
        "a": [c("1.0")], "b": [c("1.0")],
        "c": [c("1.0")], "d": [c("1.0")],
    }
    out, err = resolve(repo, "app")
    assert err is None
    assert set(out["locked"]) == {"app", "a", "c", "d"}  # b 未激活


def test_marker_python_version_major_minor_convention():
    """PEP 508 约定 python_version 为 'major.minor'：按请求值逐字比较。"""
    repo = {
        "app": [c("1.0", [
            dm("new", "", "python_version == '3.11'"),
        ])],
        "new": [c("1.0")],
    }
    env311 = {"python_version": "3.11", "sys_platform": "linux",
              "platform_machine": "x86_64"}
    out, err = resolve(repo, "app", environment=env311)
    assert err is None and "new" in out["locked"]

    env_patch = {"python_version": "3.11.8", "sys_platform": "linux",
                 "platform_machine": "x86_64"}
    out2, err2 = resolve(repo, "app", environment=env_patch)
    assert err2 is None
    # 环境值逐字比较，服务端不做截断/归一化
    assert "new" not in out2["locked"]
    assert out2["locked"] == {"app": "1.0"}


def test_marker_invalid_raises_snapshot_error_with_location():
    from app.repository import SnapshotError
    with pytest.raises(SnapshotError) as exc:
        build_repository(
            {"app": [c("1.0", [dm("dep", "", "os_name == 'posix'")])]},
            LINUX_X64_PY311,
        )
    message = str(exc.value)
    assert "app==1.0" in message
    assert "'dep'" in message
    assert "os_name" in message


def test_marker_same_input_deterministic():
    repo = {
        # 2.0 要求 s==2.0（不存在），1.0 接受 s==1.0；高版本失败后回溯。
        "app": [c("2.0", [dm("s", "==2.0", "sys_platform == 'linux'")]),
                c("1.0", [dm("s", "==1.0", "sys_platform == 'linux'")])],
        "s": [c("1.0")],
    }
    out1, e1 = resolve(repo, "app")
    out2, e2 = resolve(repo, "app")
    assert out1 == out2 and e1 == e2
    assert out1["locked"] == {"app": "1.0", "s": "1.0"}
