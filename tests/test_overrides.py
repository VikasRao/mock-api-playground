"""Body overrides are stored per endpoint+code+variant and survive
switching; legacy single-custom-body data still loads."""

import json
import sqlite3

from app import create_app
from db import Database
from endpoints import ENDPOINTS

KEY = "items_list"
URL = "/api/1.0.0/items"
NO_NETWORK = {"delay": None, "drop": False, "flaky": None}


def set_code(client, code):
    assert client.post("/admin/set", json={"key": KEY, "code": code}).status_code == 200


def set_variant(client, variant):
    assert client.post("/admin/set_variant",
                       json={"key": KEY, "variant": variant}).status_code == 200


def set_body(client, body):
    assert client.post("/admin/set_body", json={"key": KEY, "body": body}).status_code == 200


def test_override_survives_switching_codes(client, store):
    set_body(client, {"mine": 1})
    set_code(client, 404)
    set_code(client, 200)
    assert client.get(URL).get_json() == {"mine": 1}
    assert store.state[KEY]["custom_body"] == {"mine": 1}


def test_reclicking_active_code_keeps_override(client):
    set_body(client, {"mine": 1})
    set_code(client, 200)
    assert client.get(URL).get_json() == {"mine": 1}


def test_overrides_are_per_variant(client):
    set_body(client, {"a": 1})
    set_variant(client, "empty")
    assert client.get(URL).get_json() == ENDPOINTS[KEY]["responses"][200]["empty"]
    set_body(client, {"b": 2})
    set_variant(client, "normal")
    assert client.get(URL).get_json() == {"a": 1}
    set_variant(client, "empty")
    assert client.get(URL).get_json() == {"b": 2}


def test_reset_clears_only_active_override(client, store):
    set_body(client, {"a": 1})
    set_code(client, 404)
    set_body(client, {"nf": 1})
    client.post("/admin/reset_body", json={"key": KEY})
    assert store.state[KEY]["custom_bodies"] == {"200/normal": {"a": 1}}


def test_reset_all_clears_every_override(client, store):
    set_body(client, {"a": 1})
    client.post("/admin/reset_all")
    assert store.state[KEY]["custom_bodies"] == {}


def test_header_override_serves_normal_override_for_that_code(client, store):
    set_code(client, 404)
    set_body(client, {"x": 1})
    set_code(client, 200)
    resp = client.get(URL, headers={"x-mock-response-code": "404"})
    assert resp.status_code == 404
    assert resp.get_json() == {"x": 1}
    assert store.get_log()[0]["served_variant"] == "custom"


def test_deleting_variant_drops_its_override(client, store):
    set_variant(client, "empty")
    set_body(client, {"e": 1})
    resp = client.delete("/admin/defs/response",
                         json={"key": KEY, "code": 200, "variant": "empty"})
    assert resp.status_code == 200
    assert store.state[KEY]["custom_bodies"] == {}
    client.post("/admin/defs/response",
                json={"key": KEY, "code": 200, "variant": "empty", "body": {"new": 1}})
    set_variant(client, "empty")
    assert client.get(URL).get_json() == {"new": 1}


def test_deleting_inactive_variant_keeps_other_overrides(client, store):
    set_body(client, {"a": 1})
    client.delete("/admin/defs/response",
                  json={"key": KEY, "code": 200, "variant": "empty"})
    assert store.state[KEY]["custom_bodies"] == {"200/normal": {"a": 1}}


def test_restore_builtins_keeps_valid_overrides(client, store):
    set_body(client, {"a": 1})
    assert client.post("/admin/defs/restore_builtins").status_code == 200
    assert store.state[KEY]["custom_bodies"] == {"200/normal": {"a": 1}}


def test_variant_name_with_slash_round_trips(client, store):
    client.post("/admin/defs/response",
                json={"key": KEY, "code": 200, "variant": "a/b", "body": {"v": 1}})
    set_variant(client, "a/b")
    set_body(client, {"o": 1})
    assert client.get(URL).get_json() == {"o": 1}
    exported = client.get("/admin/export").get_json()
    client.post("/admin/import", json={"mode": "replace", "data": exported})
    assert client.get(URL).get_json() == {"o": 1}
    assert store.state[KEY]["custom_bodies"] == {"200/a/b": {"o": 1}}


def test_new_scenario_round_trip_restores_all_overrides(client, store):
    set_body(client, {"a": 1})
    set_code(client, 404)
    set_body(client, {"nf": 1})
    client.post("/admin/scenario/save", json={"name": "mine"})
    client.post("/admin/reset_all")
    client.post("/admin/scenario/apply", json={"name": "mine"})
    assert store.state[KEY]["custom_bodies"] == {
        "200/normal": {"a": 1}, "404/normal": {"nf": 1}}


def test_legacy_scenario_snapshot_applies_custom_body_to_its_active_variant(app, client):
    app.db.save_scenario("old", {KEY: {
        "active_code": 404, "active_variant": "normal",
        "custom_body": {"old": 1}, "network": NO_NETWORK}})
    client.post("/admin/scenario/apply", json={"name": "old"})
    resp = client.get(URL)
    assert resp.status_code == 404
    assert resp.get_json() == {"old": 1}


def test_import_legacy_state_entry_with_single_custom_body(client):
    exported = client.get("/admin/export").get_json()
    exported["state"][KEY] = {"active_code": 200, "active_variant": "normal",
                              "custom_body": {"imp": 1}, "network": NO_NETWORK}
    assert client.post("/admin/import",
                       json={"mode": "replace", "data": exported}).status_code == 200
    assert client.get(URL).get_json() == {"imp": 1}


def test_import_drops_overrides_for_unknown_code_or_variant(client, store):
    exported = client.get("/admin/export").get_json()
    exported["state"][KEY]["custom_bodies"] = {
        "200/ghost": {"g": 1}, "999/normal": {"n": 1}, "junk": {"j": 1},
        "200/normal": "not-an-object"}
    assert client.post("/admin/import",
                       json={"mode": "replace", "data": exported}).status_code == 200
    assert store.state[KEY]["custom_bodies"] == {}
    assert client.get(URL).status_code == 200


def test_v1_database_migrates_custom_body_to_its_active_variant(tmp_path):
    path = tmp_path / "mock.db"
    create_app(db_file=str(path)).db._conn.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE endpoint_state SET active_variant = 'empty',"
                 " custom_body_json = ? WHERE key = ?",
                 (json.dumps({"legacy": True}), KEY))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    app = create_app(db_file=str(path))
    state = app.store.state[KEY]
    assert state["custom_bodies"] == {"200/empty": {"legacy": True}}
    assert state["custom_body"] == {"legacy": True}
    assert app.db._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert app.db._conn.execute(
        "SELECT COUNT(*) FROM endpoint_state WHERE custom_body_json IS NOT NULL"
    ).fetchone()[0] == 0


def test_fresh_database_seeds_to_schema_v2(app):
    assert app.db._conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_admin_page_shows_custom_badge_for_active_override(client):
    set_body(client, {"x": 1})
    html = client.get("/admin").get_data(as_text=True)
    assert "custom body" in html


def test_overrides_from_snapshot_shapes():
    assert Database.overrides_from_snapshot({"custom_bodies": {"200/a": {}}}) == {"200/a": {}}
    assert Database.overrides_from_snapshot(
        {"active_code": 404, "active_variant": "normal", "custom_body": {"x": 1}}
    ) == {"404/normal": {"x": 1}}
    assert Database.overrides_from_snapshot({"active_code": 200, "custom_body": None}) == {}


def test_overrides_from_snapshot_non_dict_custom_bodies():
    assert Database.overrides_from_snapshot({"custom_bodies": 5}) == {}
    assert Database.overrides_from_snapshot({"custom_bodies": "xyz"}) == {}


def test_malformed_custom_bodies_import_rejected_without_wiping(client, store):
    set_body(client, {"keep": 1})
    client.post("/admin/scenario/save", json={"name": "keepme"})
    exported = client.get("/admin/export").get_json()
    for bad in ("xyz", 5):
        exported["state"][KEY]["custom_bodies"] = bad
        resp = client.post("/admin/import", json={"mode": "replace", "data": exported})
        assert resp.status_code == 400
        assert "keepme" in store.list_scenarios()
        assert client.get(URL).get_json() == {"keep": 1}


def test_non_object_state_entry_import_rejected(client):
    exported = client.get("/admin/export").get_json()
    exported["state"][KEY] = "oops"
    assert client.post("/admin/import",
                       json={"mode": "replace", "data": exported}).status_code == 400


def test_builtin_legacy_scenario_keeps_existing_overrides(client, store):
    set_body(client, {"a": 1})
    set_variant(client, "empty")
    set_body(client, {"e": 1})
    assert client.post("/admin/scenario/apply",
                       json={"name": "Server errors"}).status_code == 200
    assert store.state[KEY]["custom_bodies"] == {
        "200/normal": {"a": 1}, "200/empty": {"e": 1}}
    assert client.get(URL).status_code == 500


def test_legacy_scenario_merges_with_existing_override(app, client, store):
    set_body(client, {"a": 1})
    app.db.save_scenario("old", {KEY: {
        "active_code": 404, "active_variant": "normal",
        "custom_body": {"old": 1}, "network": NO_NETWORK}})
    client.post("/admin/scenario/apply", json={"name": "old"})
    assert store.state[KEY]["custom_bodies"] == {
        "200/normal": {"a": 1}, "404/normal": {"old": 1}}


def test_saved_scenario_entries_carry_both_override_shapes(client, store):
    set_body(client, {"a": 1})
    client.post("/admin/scenario/save", json={"name": "both"})
    entry = store.db.get_scenarios()["both"][KEY]
    assert entry["custom_bodies"] == {"200/normal": {"a": 1}}
    assert entry["custom_body"] == {"a": 1}


def _override_rows(app, key):
    return app.db._conn.execute(
        "SELECT COUNT(*) FROM custom_bodies WHERE endpoint_key = ?", (key,)
    ).fetchone()[0]


def test_deleting_a_code_removes_its_overrides_and_keeps_others(client, store):
    set_body(client, {"a": 1})
    set_code(client, 404)
    set_body(client, {"nf": 1})
    set_code(client, 200)
    resp = client.delete("/admin/defs/response", json={"key": KEY, "code": 404})
    assert resp.status_code == 200
    assert store.state[KEY]["custom_bodies"] == {"200/normal": {"a": 1}}


def test_deleting_an_endpoint_removes_its_override_rows(app, client):
    set_body(client, {"a": 1})
    assert _override_rows(app, KEY) == 1
    resp = client.delete("/admin/defs/endpoint", json={"key": KEY})
    assert resp.status_code == 200
    assert _override_rows(app, KEY) == 0
