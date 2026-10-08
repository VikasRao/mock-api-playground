"""Admin state routes answer with the selection they produced, so the UI
can update chips, badges and the body editor from one response."""

from endpoints import ENDPOINTS

KEY = "items_list"
RESP = ENDPOINTS[KEY]["responses"]


def test_set_code_returns_selection(client):
    d = client.post("/admin/set", json={"key": KEY, "code": 404}).get_json()
    assert d == {"key": KEY, "code": 404, "variant": "normal",
                 "body": RESP[404]["normal"], "is_custom": False}


def test_set_variant_returns_selection(client):
    d = client.post("/admin/set_variant", json={"key": KEY, "variant": "empty"}).get_json()
    assert d == {"key": KEY, "code": 200, "variant": "empty",
                 "body": RESP[200]["empty"], "is_custom": False}


def test_set_and_reset_body_return_selection(client):
    d = client.post("/admin/set_body", json={"key": KEY, "body": {"x": 1}}).get_json()
    assert d["body"] == {"x": 1} and d["is_custom"] is True
    d = client.post("/admin/reset_body", json={"key": KEY}).get_json()
    assert d["body"] == RESP[200]["normal"] and d["is_custom"] is False


def test_get_body_returns_selection(client):
    client.post("/admin/set", json={"key": KEY, "code": 500})
    d = client.get(f"/admin/body/{KEY}").get_json()
    assert d == {"key": KEY, "code": 500, "variant": "normal",
                 "body": RESP[500]["normal"], "is_custom": False}


def test_switching_back_reports_existing_override(client):
    client.post("/admin/set_body", json={"key": KEY, "body": {"x": 1}})
    client.post("/admin/set", json={"key": KEY, "code": 404})
    d = client.post("/admin/set", json={"key": KEY, "code": 200}).get_json()
    assert d["body"] == {"x": 1} and d["is_custom"] is True


def test_errors_are_unchanged(client):
    resp = client.post("/admin/set_variant", json={"key": KEY, "variant": "nope"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid variant for current code"}
