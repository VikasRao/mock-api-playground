import pytest

from app import create_app


@pytest.fixture(autouse=True)
def _no_admin_password(monkeypatch):
    """Tests must not inherit an admin password from the developer's real
    .env file (loaded at app import) or shell -- unauthenticated fixtures
    would start failing with 401s. Auth tests opt in via setenv."""
    monkeypatch.delenv("MOCKSERVER_ADMIN_PASSWORD", raising=False)


@pytest.fixture
def app(tmp_path):
    return create_app(db_file=str(tmp_path / "mock.db"))


@pytest.fixture
def client(app):
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def store(app):
    return app.store
