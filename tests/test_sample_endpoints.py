"""Smoke tests for the two sample endpoints and their variants."""

from endpoints import ENDPOINTS


def test_get_items_default_normal(client):
    resp = client.get("/api/1.0.0/items")
    assert resp.status_code == 200
    assert resp.get_json() == ENDPOINTS["items_list"]["responses"][200]["normal"]


def test_get_items_header_override(client):
    for code in (404, 500):
        resp = client.get("/api/1.0.0/items",
                          headers={"x-mock-response-code": str(code)})
        assert resp.status_code == code
        assert resp.get_json() == ENDPOINTS["items_list"]["responses"][code]["normal"]


def test_get_items_empty_variant(client):
    resp = client.post("/admin/set_variant",
                       json={"key": "items_list", "variant": "empty"})
    assert resp.status_code == 200
    resp = client.get("/api/1.0.0/items")
    assert resp.status_code == 200
    assert resp.get_json() == {"items": [], "count": 0}


def test_post_items_default_normal(client):
    resp = client.post("/api/1.0.0/items",
                       json={"name": "Sticky Notes", "price": 2.75})
    assert resp.status_code == 200
    assert resp.get_json() == ENDPOINTS["items_create"]["responses"][200]["normal"]


def test_post_items_header_override(client):
    resp = client.post("/api/1.0.0/items", json={},
                       headers={"x-mock-response-code": "400"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "validation_failed"


def test_get_and_post_are_distinct_endpoints(client):
    assert client.get("/api/1.0.0/items").get_json() != \
        client.post("/api/1.0.0/items", json={}).get_json()


def test_unknown_route_returns_json_404(client):
    resp = client.get("/api/1.0.0/nothing-here")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "no mock endpoint matches this method+path"}


def test_server_errors_scenario(client):
    resp = client.post("/admin/scenario/apply", json={"name": "Server errors"})
    assert resp.status_code == 200
    assert client.get("/api/1.0.0/items").status_code == 500
    assert client.post("/api/1.0.0/items", json={}).status_code == 500
    client.post("/admin/reset_all")
    assert client.get("/api/1.0.0/items").status_code == 200
