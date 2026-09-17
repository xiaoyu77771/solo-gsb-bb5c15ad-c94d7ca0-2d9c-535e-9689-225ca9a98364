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
