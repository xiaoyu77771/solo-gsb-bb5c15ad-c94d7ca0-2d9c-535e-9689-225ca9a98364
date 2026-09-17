"""FastAPI 路由层测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def payload(**overrides):
    base = {
        "repository": {
            "app": [
                {"version": "1.0.0",
                 "dependencies": [{"name": "libx", "specifier": ">=1.0"}]},
            ],
            "libx": [
                {"version": "1.0", "dependencies": []},
                {"version": "2.0",
                 "dependencies": [{"name": "liby", "specifier": ""}]},
            ],
            "liby": [{"version": "1.1"}, {"version": "0.9"}],
        },
        "target_environment": {
            "python_version": "3.11",
            "sys_platform": "linux",
            "platform_machine": "x86_64",
        },
        "root": "app",
        "specifier": "",
        "allow_prereleases": False,
    }
    base.update(overrides)
    return base


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_resolve_ok():
    r = client.post("/resolve", json=payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["root_version"] == "1.0.0"
    assert body["locked"] == {"app": "1.0.0", "libx": "2.0", "liby": "1.1"}
    assert body["edges"][0] == {"from": "root", "to": "app", "specifier": "*"}
    assert {"from": "libx==2.0", "to": "liby", "specifier": "*"} in body["edges"]


def test_resolve_conflict_409():
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [
                     {"name": "left", "specifier": ""},
                     {"name": "right", "specifier": ""},
                 ]}],
        "left": [{"version": "1.0",
                  "dependencies": [{"name": "core", "specifier": "==1.0"}]}],
        "right": [{"version": "1.0",
                   "dependencies": [{"name": "core", "specifier": "==2.0"}]}],
        "core": [{"version": "1.0"}, {"version": "2.0"}],
    }
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["subject"] == "core"
    # 每条约束都带来源链，且链从根需求开始
    for entry in conflict["constraints"]:
        chain = entry["chain"]
        assert chain[0]["package"] is None
        assert chain[-1]["requires"] in {"==1.0", "==2.0"}
    assert conflict["partial_selection"]["app"] == "1.0"


def test_resolve_unknown_root_422():
    r = client.post("/resolve", json=payload(root="missing"))
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_resolve_bad_version_422():
    repo = {"app": [{"version": "oops", "dependencies": []}]}
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 422
    assert "PEP 440" in r.json()["message"]


def test_resolve_bad_specifier_422():
    repo = {"app": [{"version": "1.0",
                     "dependencies": [{"name": "x", "specifier": ">>>1"}]}]}
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 422


def test_pydantic_validation_error_on_extra_field():
    r = client.post("/resolve", json=payload(unknown_field=1))
    assert r.status_code == 422


def test_prerelease_toggle():
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib", "specifier": ""}]}],
        "lib": [{"version": "2.0rc1"}, {"version": "1.9"}],
    }
    r1 = client.post("/resolve", json=payload(repository=repo))
    assert r1.json()["locked"]["lib"] == "1.9"
    r2 = client.post(
        "/resolve", json=payload(repository=repo, allow_prereleases=True)
    )
    assert r2.json()["locked"]["lib"] == "2.0rc1"


# --------------------------------------------------------------------- #
# PEP 508 marker / target_environment
# --------------------------------------------------------------------- #
def env(**overrides):
    base = {
        "python_version": "3.11",
        "sys_platform": "linux",
        "platform_machine": "x86_64",
    }
    base.update(overrides)
    return base


def marker_repo():
    """app 1.0 同时声明：
    * posix-only：仅 linux 激活；
    * win-only：仅 win32 激活（且要求不存在的包，验证未激活项不参与求解）；
    * py312-only：仅 python_version >= 3.12 激活。
    """
    return {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "posix-only", "specifier": "",
             "marker": "sys_platform == 'linux'"},
            {"name": "win-only", "specifier": "==9.9",
             "marker": "sys_platform == 'win32'"},
            {"name": "py312-only", "specifier": "",
             "marker": "python_version >= '3.12'"},
        ]}],
        "posix-only": [{"version": "1.0"}],
        "win-only": [{"version": "1.0"}],
        "py312-only": [{"version": "1.0"}],
    }


def test_marker_active_dependencies_only_linux_py311():
    r = client.post(
        "/resolve", json=payload(repository=marker_repo(), target_environment=env())
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] == {"app": "1.0", "posix-only": "1.0"}
    tos = {e["to"] for e in body["edges"]}
    assert "posix-only" in tos
    assert "win-only" not in tos
    assert "py312-only" not in tos


def test_marker_cross_environment_changes_result():
    # Windows：win-only 激活，但它要求 ==9.9 而快照只有 1.0 -> 409；
    # linux 下根本不参与求解的依赖，在 win32 下成为冲突来源。
    r = client.post(
        "/resolve",
        json=payload(
            repository=marker_repo(),
            target_environment=env(sys_platform="win32", platform_machine="AMD64"),
        ),
    )
    assert r.status_code == 409
    conflict = r.json()["conflicts"][0]
    assert conflict["subject"] == "win-only"
    # 冲突来源链定位到 app==1.0 声明的 ==9.9
    chain = conflict["constraints"][0]["chain"]
    assert {"package": "app", "version": "1.0", "requires": "==9.9"} in chain


def test_marker_python_version_gate():
    r = client.post(
        "/resolve",
        json=payload(
            repository=marker_repo(),
            target_environment=env(python_version="3.12.1"),
        ),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["locked"]) == {"app", "posix-only", "py312-only"}


def test_marker_arch_gate():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "arm-binary", "specifier": "",
             "marker": "platform_machine == 'arm64'"},
            {"name": "intel-binary", "specifier": "",
             "marker": "platform_machine == 'x86_64'"},
        ]}],
        "arm-binary": [{"version": "1.0"}],
        "intel-binary": [{"version": "1.0"}],
    }
    r_arm = client.post(
        "/resolve", json=payload(repository=repo, target_environment=env(platform_machine="arm64"))
    )
    assert set(r_arm.json()["locked"]) == {"app", "arm-binary"}
    r_x64 = client.post(
        "/resolve", json=payload(repository=repo, target_environment=env(platform_machine="x86_64"))
    )
    assert set(r_x64.json()["locked"]) == {"app", "intel-binary"}


def test_marker_invalid_syntax_422_locates_package_version_dependency():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "broken", "specifier": "", "marker": "sys_platform =="},
        ]}],
    }
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 422
    message = r.json()["message"]
    assert "app==1.0" in message
    assert "'broken'" in message
    assert "PEP 508" in message


def test_marker_out_of_scope_variable_422():
    # 引用了三个允许变量之外的名字（python_full_version / os_name / extra ...）
    for bad_marker in [
        "python_full_version >= '3.11.2'",
        "os_name == 'posix'",
        "extra == 'speedups'",
        "python_version >= '3.10' and platform_system == 'Linux'",
    ]:
        repo = {
            "app": [{"version": "2.5", "dependencies": [
                {"name": "dep", "specifier": "", "marker": bad_marker},
            ]}],
        }
        r = client.post("/resolve", json=payload(repository=repo))
        assert r.status_code == 422, bad_marker
        message = r.json()["message"]
        assert "app==2.5" in message and "'dep'" in message, bad_marker


def test_target_environment_required_422():
    data = payload()
    del data["target_environment"]
    r = client.post("/resolve", json=data)
    assert r.status_code == 422


def test_target_environment_bad_python_version_422():
    r = client.post(
        "/resolve",
        json=payload(target_environment=env(python_version="three-eleven")),
    )
    assert r.status_code == 422
    assert "python_version" in r.json()["message"]


def test_target_environment_empty_field_422():
    r = client.post(
        "/resolve",
        json=payload(target_environment=env(sys_platform="   ")),
    )
    assert r.status_code == 422


def test_target_environment_extra_key_rejected_422():
    r = client.post(
        "/resolve",
        json=payload(target_environment=env(os_name="posix")),
    )
    assert r.status_code == 422


def test_marker_deterministic_across_hosts():
    """相同请求体（含 target_environment）逐字节一致，不依赖宿主机。"""
    data = payload(repository=marker_repo())
    r1 = client.post("/resolve", json=data).json()
    r2 = client.post("/resolve", json=data).json()
    assert r1 == r2


def test_inactive_transitive_leg_does_not_constrain():
    """未激活依赖的整条传递子树都不参与求解：
    linux 下 win-leg 消失，app 钉 common==1.0 可解；
    win32 下 win-leg 激活（app 钉其 ==9.9）即冲突。"""
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "win-leg", "specifier": "==9.9",
             "marker": "sys_platform == 'win32'"},
            {"name": "common", "specifier": "==1.0"},
        ]}],
        "win-leg": [{"version": "1.0", "dependencies": [
            {"name": "common", "specifier": "==2.0"},
        ]}],
        "common": [{"version": "1.0"}, {"version": "2.0"}],
    }
    r_linux = client.post(
        "/resolve", json=payload(repository=repo, target_environment=env())
    )
    assert r_linux.status_code == 200
    assert r_linux.json()["locked"] == {"app": "1.0", "common": "1.0"}
    assert all(e["to"] != "win-leg" for e in r_linux.json()["edges"])

    r_win = client.post(
        "/resolve",
        json=payload(
            repository=repo,
            target_environment=env(sys_platform="win32", platform_machine="AMD64"),
        ),
    )
    assert r_win.status_code == 409
    assert r_win.json()["conflicts"][0]["subject"] == "win-leg"

