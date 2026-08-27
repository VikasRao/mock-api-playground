"""Basic-auth protection for the /admin surface.

When MOCKSERVER_ADMIN_PASSWORD is set, every /admin* route requires
HTTP Basic Auth (user "admin"); mock endpoints stay open. When unset,
the admin surface stays open (local-dev behavior).
"""

import base64

import pytest

from app import create_app


def _basic(password, user="admin"):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def protected_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKSERVER_ADMIN_PASSWORD", "s3cret")
    app = create_app(db_file=str(tmp_path / "mock.db"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_admin_open_when_no_password_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("MOCKSERVER_ADMIN_PASSWORD", raising=False)
    app = create_app(db_file=str(tmp_path / "mock.db"))
    with app.test_client() as c:
        assert c.get("/admin/state").status_code == 200


@pytest.mark.parametrize("path", [
    "/admin",
    "/admin/state",
    "/admin/log",
    "/admin/export",
])
def test_admin_get_routes_require_auth(protected_client, path):
    resp = protected_client.get(path)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")


def test_admin_post_routes_require_auth(protected_client):
    resp = protected_client.post("/admin/reset_all")
    assert resp.status_code == 401


def test_admin_defs_routes_require_auth(protected_client):
    resp = protected_client.post(
        "/admin/defs/endpoint",
        json={"path": "/x/y", "method": "GET", "body": {}},
    )
    assert resp.status_code == 401


def test_wrong_password_rejected(protected_client):
    resp = protected_client.get("/admin/state", headers=_basic("wrong"))
    assert resp.status_code == 401


def test_wrong_username_rejected(protected_client):
    resp = protected_client.get("/admin/state", headers=_basic("s3cret", user="root"))
    assert resp.status_code == 401


def test_correct_credentials_accepted(protected_client):
    resp = protected_client.get("/admin/state", headers=_basic("s3cret"))
    assert resp.status_code == 200


def test_mock_endpoints_stay_open_without_credentials(protected_client):
    resp = protected_client.get("/api/1.0.0/items")
    assert resp.status_code == 200


def test_mock_header_override_stays_open(protected_client):
    resp = protected_client.get(
        "/api/1.0.0/items",
        headers={"x-mock-response-code": "404"},
    )
    assert resp.status_code == 404
