# Admin State Persistence, Feedback and Move-Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make admin selections, status codes, variants and edited bodies stick; show progress and errors on every save; keep the body editor in sync; keep the user's place across reloads; and let endpoints be dragged between categories.

**Architecture:** Backend: SQLite reads and read-modify-write state updates are serialised by the existing `Database._lock`; custom bodies move from one slot per endpoint to a `custom_bodies` table keyed by endpoint+code+variant (schema v2, migrated on startup); state-changing admin routes return the resulting selection (`code`, `variant`, `body`, `is_custom`). Frontend (`templates/admin.html`, vanilla JS): one `api()` helper that rejects on HTTP errors, busy/spinner/overlay helpers, an `applySelection()` that updates chips, badges and editor from server responses, selection stored in the URL hash, and HTML5 drag-and-drop onto category rows.

**Tech Stack:** Python 3.13+ (local venv is 3.14), Flask, SQLite (`sqlite3`), Jinja2, vanilla JS, pytest. Node is used only by a test to syntax-check the page script.

**Spec:** `docs/superpowers/specs/2026-10-08-admin-state-and-ux-fixes-design.md`

## Global Constraints

- No new Python or JS dependencies; no JS framework or JS test runner.
- `PRAGMA user_version` becomes `2`. Existing v1 databases migrate on startup; fresh databases seed straight to `2`.
- Export payload `version` stays `1` (the `custom_bodies` field is additive).
- Override map keys are `"<code>/<variant>"`, e.g. `"200/normal"`. Variant names may themselves contain `/`; always split on the **first** `/` only.
- The mock-serving API is unchanged except: a request with `x-mock-response-code` for a non-active code now serves that code's `normal` override if one exists.
- `state[key]["custom_body"]` remains (derived: override for the active code+variant, else `null`).
- Run tests with `venv/bin/python -m pytest -q` from the repo root. `tests/conftest.py` already strips `MOCKSERVER_ADMIN_PASSWORD`.
- Commit messages end with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Work on branch `fix/admin-state-and-ux`. Never stage `README.md`, `blog-post.md`, `blog-preview.html`, `mock.db*`.

## Review Focus

1. Variant names containing `/` (e.g. `a/b`) must round-trip through overrides, export and import — Task 2 `test_variant_name_with_slash_round_trips`.
2. A scenario or import that carries an override for a code/variant that no longer exists (or a malformed key like `"junk"`) must be dropped silently, and the endpoint must still serve — Task 2 `test_import_drops_overrides_for_unknown_code_or_variant`.
3. Concurrent admin writes to different fields of the same endpoint must both land, with no HTTP 500s — Task 1 `test_concurrent_writes_to_one_endpoint_are_not_lost`.
4. Category names with spaces, `&`, `#` or emoji must survive the URL-hash round-trip and reload — Task 6 manual check with a category named `Customer App & QA #1 🧪`.
5. Re-clicking the already-active code or variant chip must send no request, never prompt, and never touch the body — Task 5 (guard in `setCode`/`setVariant`) and Task 2 `test_reclicking_active_code_keeps_override` for the server side.

---

### Task 1: Serialise database reads and state updates

**Files:**
- Modify: `db.py` (read methods `get_categories`, `get_endpoints`, `get_state`, `get_scenarios`; add `update_state`)
- Modify: `store.py` (`set_code`, `set_variant`, `set_custom_body`, `reset_custom_body`, `set_network`)
- Test: `tests/test_concurrency.py` (create)

**Interfaces:**
- Produces: `Database.update_state(key: str, fn: Callable[[dict], None]) -> dict` — holds `self._lock` across `get_state()[key]` → `fn(entry)` (mutates in place) → `set_state(key, entry)`; returns the written entry. All Store state mutations go through it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_concurrency.py`:

```python
"""Concurrent admin writes must not lose updates or crash.

The threaded dev server (python app.py) runs admin requests in parallel;
before the fix, two writes to the same endpoint raced their
read-modify-write and unlocked reads iterated the shared connection
mid-commit (KeyError / HTTP 500)."""

import threading

KEY = "items_list"
NO_NETWORK = {"delay": None, "drop": False, "flaky": None}
DELAYED = {"delay": {"mode": "fixed", "seconds": 1}, "drop": False, "flaky": None}


def test_concurrent_writes_to_one_endpoint_are_not_lost(app):
    errors = []

    def post(url, payload):
        resp = app.test_client().post(url, json=payload)
        if resp.status_code != 200:
            errors.append((url, resp.status_code))

    lost = 0
    for _ in range(100):
        app.store.set_code(KEY, 200)
        app.store.set_network(KEY, NO_NETWORK)
        threads = [
            threading.Thread(target=post, args=("/admin/set", {"key": KEY, "code": 404})),
            threading.Thread(target=post, args=("/admin/set_network", {"key": KEY, **DELAYED})),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entry = app.store.state[KEY]
        if entry["active_code"] != 404 or not entry["network"]["delay"]:
            lost += 1

    assert errors == []
    assert lost == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_concurrency.py -q`
Expected: FAIL — `lost` > 0 and/or `errors` contains `('/admin/set_network', 500)` (a probe on 2026-10-08 saw 123/200 lost, 35 × 500).

- [ ] **Step 3: Lock the reads in `db.py`**

Replace `get_categories`, `get_endpoints`, `get_state`, `get_scenarios` with versions that fetch all rows while holding the lock:

```python
    def get_categories(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, icon, color, position FROM categories ORDER BY position"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_endpoints(self) -> dict:
        with self._lock:
            endpoint_rows = self._conn.execute(
                "SELECT key, category, method, path FROM endpoints ORDER BY position"
            ).fetchall()
            response_rows = self._conn.execute(
                "SELECT endpoint_key, code, variant, body_json FROM responses"
            ).fetchall()
        eps = {}
        for r in endpoint_rows:
            eps[r["key"]] = {
                "category": r["category"], "method": r["method"],
                "path": r["path"], "responses": {},
            }
        for r in response_rows:
            eps[r["endpoint_key"]]["responses"].setdefault(r["code"], {})[
                r["variant"]] = json.loads(r["body_json"])
        return eps
```

```python
    def get_state(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, active_code, active_variant, custom_body_json, network_json"
                " FROM endpoint_state"
            ).fetchall()
        state = {}
        for r in rows:
            state[r["key"]] = {
                "active_code": r["active_code"],
                "active_variant": r["active_variant"],
                "custom_body": json.loads(r["custom_body_json"])
                if r["custom_body_json"] is not None else None,
                "network": json.loads(r["network_json"]),
            }
        return state
```

```python
    def get_scenarios(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, snapshot_json FROM scenarios").fetchall()
        return {r["name"]: json.loads(r["snapshot_json"]) for r in rows}
```

- [ ] **Step 4: Add `update_state` to `db.py`** (directly after `set_state`)

```python
    def update_state(self, key: str, fn) -> dict:
        """Atomic read-modify-write of one endpoint's state: fn(entry)
        mutates the entry in place while the lock is held, so concurrent
        admin requests can't overwrite each other's changes."""
        with self._lock:
            entry = self.get_state()[key]
            fn(entry)
            self.set_state(key, entry)
            return entry
```

- [ ] **Step 5: Route Store mutations through `update_state`** in `store.py`

Replace `set_code`, `set_variant`, `set_custom_body`, `reset_custom_body`, `set_network`:

```python
    def set_code(self, key: str, code: int) -> None:
        self.db.update_state(key, lambda e: e.update(
            active_code=code, active_variant="normal", custom_body=None))

    def set_variant(self, key: str, variant: str) -> bool:
        ok = False

        def apply(entry):
            nonlocal ok
            if variant in self.endpoints[key]["responses"][entry["active_code"]]:
                entry.update(active_variant=variant, custom_body=None)
                ok = True

        self.db.update_state(key, apply)
        return ok
```

```python
    def set_custom_body(self, key: str, body: dict) -> None:
        if not isinstance(body, dict):
            raise ValueError("custom body must be a JSON object")
        self.db.update_state(key, lambda e: e.update(custom_body=body))

    def reset_custom_body(self, key: str) -> None:
        self.db.update_state(key, lambda e: e.update(custom_body=None))

    def set_network(self, key: str, network: dict) -> None:
        self.db.update_state(key, lambda e: e.update(network={
            "delay": network.get("delay"),
            "drop": bool(network.get("drop", False)),
            "flaky": network.get("flaky"),
        }))
```

(The custom-body semantics stay as today in this task; Task 2 changes them.)

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass (27 existing + 1 new).

- [ ] **Step 7: Commit**

```bash
git add db.py store.py tests/test_concurrency.py
git commit -m "fix: serialise state read-modify-write and DB reads

Concurrent admin requests lost updates and raised KeyError on the shared
SQLite connection.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Per code+variant body overrides (schema v2)

**Files:**
- Modify: `db.py` (schema, version constant, override helpers, `get_state`, `set_state`, migration, `_reset_state_if_invalid`, `delete_response`, `restore_builtins`, `_import_legacy_state`, `import_all`)
- Modify: `store.py` (`set_code`, `set_variant`, `set_custom_body`, `reset_custom_body`, `_resolve_body_and_label`, `save_scenario`, `apply_scenario`)
- Test: `tests/test_overrides.py` (create)

**Interfaces:**
- Consumes: `Database.update_state` (Task 1).
- Produces (module-level in `db.py`): `SCHEMA_VERSION = 2`; `override_key(code, variant) -> str` returning `f"{code}/{variant}"`; `parse_override_key(k) -> tuple[int, str] | None`.
- Produces: `Database.overrides_from_snapshot(entry: dict) -> dict` (staticmethod) — returns `entry["custom_bodies"]` if that key is present, else `{override_key(active_code, active_variant): custom_body}` if `custom_body` is not None, else `{}`.
- Produces: `get_state()[key]` gains `"custom_bodies": {"<code>/<variant>": dict}`; `"custom_body"` becomes derived.
- Produces: `set_state(key, entry)` persists `entry.get("custom_bodies") or {}` as the endpoint's complete override set (filtered to existing code+variants and dict bodies) and **ignores** `entry["custom_body"]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_overrides.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_overrides.py -q`
Expected: most FAIL (`KeyError: 'custom_bodies'`, override lost after switching, `user_version` is 1, `AttributeError: overrides_from_snapshot`).

- [ ] **Step 3: Schema, constants and key helpers in `db.py`**

Append to `_SCHEMA` (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS custom_bodies (
  endpoint_key TEXT NOT NULL REFERENCES endpoints(key) ON DELETE CASCADE,
  code         INTEGER NOT NULL,
  variant      TEXT NOT NULL,
  body_json    TEXT NOT NULL,
  PRIMARY KEY (endpoint_key, code, variant)
);
```

After `DEFAULT_NETWORK = ...` add:

```python
# v1: single custom body per endpoint (endpoint_state.custom_body_json).
# v2: one override per endpoint+code+variant in custom_bodies; the old
# column is left in place but always NULL.
SCHEMA_VERSION = 2


def override_key(code, variant) -> str:
    """Key of an override in state["custom_bodies"], e.g. "200/normal"."""
    return f"{code}/{variant}"


def parse_override_key(k):
    """(code, variant) for an override key, or None if malformed. Splits on
    the first "/" only, since variant names may contain "/"."""
    code, sep, variant = str(k).partition("/")
    if not sep or not code.isdigit() or not variant:
        return None
    return int(code), variant
```

- [ ] **Step 4: `get_state` / `set_state` / `overrides_from_snapshot` in `db.py`**

Replace `get_state` and `set_state`:

```python
    def get_state(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, active_code, active_variant, network_json"
                " FROM endpoint_state"
            ).fetchall()
            override_rows = self._conn.execute(
                "SELECT endpoint_key, code, variant, body_json FROM custom_bodies"
            ).fetchall()
        overrides = {}
        for r in override_rows:
            overrides.setdefault(r["endpoint_key"], {})[
                override_key(r["code"], r["variant"])] = json.loads(r["body_json"])
        state = {}
        for r in rows:
            bodies = overrides.get(r["key"], {})
            state[r["key"]] = {
                "active_code": r["active_code"],
                "active_variant": r["active_variant"],
                # Derived: the override that applies to the active selection.
                "custom_body": bodies.get(
                    override_key(r["active_code"], r["active_variant"])),
                "custom_bodies": bodies,
                "network": json.loads(r["network_json"]),
            }
        return state

    def set_state(self, key: str, entry: dict) -> None:
        """Persist one endpoint's runtime state. entry["custom_bodies"]
        replaces the endpoint's whole override set; overrides for a
        code+variant the endpoint doesn't define, or whose body isn't a JSON
        object, are dropped. The derived entry["custom_body"] is ignored --
        use overrides_from_snapshot() for legacy single-body input."""
        responses = self.get_endpoint(key)["responses"]
        overrides = []
        for k, body in (entry.get("custom_bodies") or {}).items():
            parsed = parse_override_key(k)
            if parsed is None or not isinstance(body, dict):
                continue
            code, variant = parsed
            if variant in responses.get(code, {}):
                overrides.append((key, code, variant, json.dumps(body)))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO endpoint_state"
                " (key, active_code, active_variant, custom_body_json, network_json)"
                " VALUES (?, ?, ?, NULL, ?)"
                " ON CONFLICT(key) DO UPDATE SET active_code=excluded.active_code,"
                "  active_variant=excluded.active_variant,"
                "  custom_body_json=NULL,"
                "  network_json=excluded.network_json",
                (key, entry["active_code"], entry["active_variant"],
                 json.dumps(entry["network"])),
            )
            self._conn.execute(
                "DELETE FROM custom_bodies WHERE endpoint_key = ?", (key,))
            self._conn.executemany(
                "INSERT INTO custom_bodies (endpoint_key, code, variant, body_json)"
                " VALUES (?, ?, ?, ?)", overrides)

    @staticmethod
    def overrides_from_snapshot(entry: dict) -> dict:
        """Overrides carried by a scenario snapshot or export state entry.
        v2 entries have "custom_bodies"; older ones have a single
        "custom_body" meant for the entry's active code+variant."""
        if "custom_bodies" in entry:
            return dict(entry["custom_bodies"] or {})
        if entry.get("custom_body") is not None:
            return {override_key(entry.get("active_code"),
                                 entry.get("active_variant", "normal")):
                    entry["custom_body"]}
        return {}
```

- [ ] **Step 5: Migration and seeding version in `db.py`**

Replace the start and end of `seed_if_empty`:

```python
    def seed_if_empty(self, seed_endpoints, icons, colors,
                      legacy_state_file=None) -> bool:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            self._migrate_v1_to_v2()
            return False
        if version >= SCHEMA_VERSION:
            return False
```

and at its end change `PRAGMA user_version = 1` to:

```python
        with self._lock, self._conn:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
```

Add after `seed_if_empty`:

```python
    def _migrate_v1_to_v2(self) -> None:
        """Move each endpoint's single v1 custom body to custom_bodies at
        that endpoint's active code+variant."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO custom_bodies"
                " (endpoint_key, code, variant, body_json)"
                " SELECT key, active_code, active_variant, custom_body_json"
                " FROM endpoint_state WHERE custom_body_json IS NOT NULL")
            self._conn.execute("UPDATE endpoint_state SET custom_body_json = NULL")
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._prune_orphan_overrides()

    def _prune_orphan_overrides(self) -> None:
        """Drop overrides whose code+variant no longer has a response."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM custom_bodies WHERE NOT EXISTS ("
                " SELECT 1 FROM responses r"
                " WHERE r.endpoint_key = custom_bodies.endpoint_key"
                " AND r.code = custom_bodies.code"
                " AND r.variant = custom_bodies.variant)")
```

- [ ] **Step 6: Keep overrides through invalid-state resets, response deletes and restores** (`db.py`)

Replace `_reset_state_if_invalid`:

```python
    def _reset_state_if_invalid(self, key):
        cfg = self.get_endpoint(key)
        entry = self.get_state().get(key)
        if entry is None:
            self.set_state(key, self.default_state_entry(key))
            return
        variants = cfg["responses"].get(entry["active_code"], {})
        if entry["active_code"] not in cfg["responses"] or \
                entry["active_variant"] not in variants:
            # The selection is gone, but overrides for other code+variants
            # are still valid -- keep them (set_state drops the stale ones).
            fresh = self.default_state_entry(key)
            fresh["custom_bodies"] = entry["custom_bodies"]
            self.set_state(key, fresh)
```

In `delete_response`, immediately before `self._reset_state_if_invalid(key)` add:

```python
        self._prune_orphan_overrides()
```

In `restore_builtins`, immediately before `for key in seed_endpoints:` (after the `try/except`) add:

```python
        self._prune_orphan_overrides()
```

- [ ] **Step 7: Legacy file and import paths accept both shapes** (`db.py`)

In `_import_legacy_state`, replace the body of the `for key, entry in on_disk.get("state", {}).items():` loop with:

```python
            if key not in endpoints:
                continue
            default = self.default_state_entry(key)
            merged = dict(default)
            merged.update(entry)
            network = dict(default["network"])
            network.update(merged.get("network") or {})
            merged["network"] = network
            overrides = self.overrides_from_snapshot(merged)
            if merged.get("active_code") not in endpoints[key]["responses"]:
                merged = default
            merged["custom_bodies"] = overrides
            self.set_state(key, merged)
```

In `import_all`, change the replace-mode table list to start with `custom_bodies`:

```python
                for table in ("custom_bodies", "responses", "endpoint_state",
                              "endpoints", "categories", "scenarios"):
```

and replace the state loop:

```python
        for key, entry in (payload.get("state") or {}).items():
            if self.get_endpoint(key) is None:
                continue
            default = self.default_state_entry(key)
            merged = dict(default)
            merged.update(entry)
            overrides = self.overrides_from_snapshot(merged)
            if merged.get("active_code") not in self.get_endpoint(key)["responses"]:
                merged = default
            merged["custom_bodies"] = overrides
            self.set_state(key, merged)
```

(`default_state_entry` deliberately has no `custom_bodies` key, so `overrides_from_snapshot(merged)` sees only what the incoming entry carried.)

- [ ] **Step 8: Store semantics in `store.py`**

Add the import below `from scenarios import BUILTIN_SCENARIOS`:

```python
from db import override_key
```

Replace `_resolve_body_and_label`:

```python
    def _resolve_body_and_label(self, key: str, code: int) -> tuple:
        """Return (body, label) for what active_body serves: the override for
        the code+variant being served (label "custom") or that variant's
        body (label = real variant name). When code comes from an
        x-mock-response-code header that differs from the UI selection, the
        served variant is that code's "normal". Used so request-log entries
        record what was truly served.
        """
        entry = self._entry(key)
        variant = entry["active_variant"] if code == entry["active_code"] else "normal"
        override = entry["custom_bodies"].get(override_key(code, variant))
        if override is not None:
            return override, "custom"
        return self.variant_body(key, code, variant), variant
```

Replace the Task 1 versions of `set_code`, `set_variant`, `set_custom_body`, `reset_custom_body`:

```python
    def set_code(self, key: str, code: int) -> None:
        # Overrides are per code+variant, so switching never discards them.
        self.db.update_state(key, lambda e: e.update(
            active_code=code, active_variant="normal"))

    def set_variant(self, key: str, variant: str) -> bool:
        ok = False

        def apply(entry):
            nonlocal ok
            if variant in self.endpoints[key]["responses"][entry["active_code"]]:
                entry["active_variant"] = variant
                ok = True

        self.db.update_state(key, apply)
        return ok
```

```python
    def set_custom_body(self, key: str, body: dict) -> None:
        if not isinstance(body, dict):
            raise ValueError("custom body must be a JSON object")

        def apply(entry):
            active = override_key(entry["active_code"], entry["active_variant"])
            entry["custom_bodies"][active] = body

        self.db.update_state(key, apply)

    def reset_custom_body(self, key: str) -> None:
        self.db.update_state(key, lambda e: e["custom_bodies"].pop(
            override_key(e["active_code"], e["active_variant"]), None))
```

In `apply_scenario`, replace the `self.db.set_state(...)` call with:

```python
            self.db.set_state(key, {
                "active_code": snapshot["active_code"],
                "active_variant": snapshot["active_variant"],
                "custom_bodies": self.db.overrides_from_snapshot(snapshot),
                "network": dict(snapshot["network"]),
            })
```

In `save_scenario`, replace the per-key snapshot dict with:

```python
            key: {
                "active_code": state[key]["active_code"],
                "active_variant": state[key]["active_variant"],
                "custom_bodies": dict(state[key]["custom_bodies"]),
                "network": dict(state[key]["network"]),
            }
```

- [ ] **Step 9: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass (existing + `test_concurrency.py` + `test_overrides.py`).

- [ ] **Step 10: Commit**

```bash
git add db.py store.py tests/test_overrides.py
git commit -m "feat: keep body overrides per code+variant (schema v2)

Switching code or variant (even re-clicking the active one) no longer
discards an edited body. v1 databases migrate on startup; legacy
scenarios and export files with a single custom_body still load.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: State-changing routes return the resulting selection

**Files:**
- Modify: `store.py` (add `selection`)
- Modify: `app.py` (`admin_set`, `admin_set_variant`, `admin_get_body`, `admin_set_body`, `admin_reset_body`)
- Test: `tests/test_selection_responses.py` (create)

**Interfaces:**
- Consumes: `Store._resolve_body_and_label` (Task 2).
- Produces: `Store.selection(key: str) -> {"key": str, "code": int, "variant": str, "body": dict, "is_custom": bool}`. `POST /admin/set`, `POST /admin/set_variant`, `GET /admin/body/<key>`, `POST /admin/set_body`, `POST /admin/reset_body` all return this JSON on success. Error responses are unchanged. The frontend (Tasks 4–5) relies on exactly these field names.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_selection_responses.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_selection_responses.py -q`
Expected: FAIL — responses lack `variant`/`body`/`is_custom` (e.g. `/admin/set` returns `{"key", "code"}`).

- [ ] **Step 3: Add `Store.selection`** (in `store.py`, after `served_variant_label`)

```python
    def selection(self, key: str) -> dict:
        """The endpoint's active code+variant and the body it serves, as
        returned by the admin state routes."""
        entry = self._entry(key)
        code = entry["active_code"]
        body, label = self._resolve_body_and_label(key, code)
        return {"key": key, "code": code, "variant": entry["active_variant"],
                "body": body, "is_custom": label == "custom"}
```

- [ ] **Step 4: Return it from the routes** (`app.py`)

- `admin_set`: replace `return jsonify({"key": key, "code": code})` with `return jsonify(store.selection(key))`
- `admin_set_variant`: replace `return jsonify({"key": key, "variant": variant})` with `return jsonify(store.selection(key))`
- `admin_get_body`: replace its last two lines (`code = ...` and `return jsonify({"body": ...})`) with `return jsonify(store.selection(key))`
- `admin_set_body`: replace `return jsonify({"key": key, "ok": True})` with `return jsonify(store.selection(key))`
- `admin_reset_body`: replace `return jsonify({"key": key, "ok": True})` with `return jsonify(store.selection(key))`

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add store.py app.py tests/test_selection_responses.py
git commit -m "feat: admin state routes return the resulting selection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Frontend request helper, progress UI and server-confirmed updates

**Files:**
- Modify: `templates/admin.html` (CSS, two new elements, JS helpers, all fetch call sites, three `onclick` attributes)
- Test: `tests/test_admin_page.py` (create)

**Interfaces:**
- Consumes: selection JSON from Task 3.
- Produces (JS globals used by Tasks 5–7): `api(url, {method, body, quiet}) -> Promise<data>` (rejects with `Error(serverMessage)`, toasts unless `quiet`); `ignore()`; `trackPending(promise, label) -> Promise`; `withBusy(els, promise, label) -> Promise`; `saveThenReload(promise, onOk) -> Promise`; `defsRequest(url, method, payload, onOk)` (now overlay + reload); `applySelection(card, sel)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin_page.py`:

```python
"""Admin page smoke tests: it renders, has the progress elements, and its
inline script is valid JavaScript (checked with node when available)."""

import re
import shutil
import subprocess

import pytest


def admin_html(client):
    resp = client.get("/admin")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_admin_page_has_progress_elements(client):
    html = admin_html(client)
    assert 'id="busy-pill"' in html
    assert 'id="busy-overlay"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_admin_page_script_is_valid_javascript(client, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", admin_html(client), re.S)
    assert scripts, "no inline <script> found"
    js = tmp_path / "admin.js"
    js.write_text("\n".join(scripts))
    result = subprocess.run(["node", "--check", str(js)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `venv/bin/python -m pytest tests/test_admin_page.py -q`
Expected: `test_admin_page_has_progress_elements` FAILS; the node check passes (baseline).

- [ ] **Step 3: CSS** — add before the `/* ---- Mobile: drill-in` comment:

```css
  /* ---- Progress: per-control spinner, pending pill, reload overlay ---- */
  .code-btn:disabled, .variant-btn:disabled { cursor: progress; }
  .is-busy { opacity: .6; }
  .is-busy::after {
    content: ""; display: inline-block; width: 9px; height: 9px; margin-left: 6px;
    border: 2px solid currentColor; border-right-color: transparent;
    border-radius: 50%; animation: spin .7s linear infinite; vertical-align: -1px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .busy-pill {
    position: fixed; top: 12px; right: 16px; z-index: 20; display: none;
    background: var(--card); border: 1px solid var(--accent); color: var(--accent);
    padding: 4px 12px; border-radius: 999px; font-size: 12px;
  }
  .busy-pill.show { display: block; }
  .busy-overlay {
    position: fixed; inset: 0; z-index: 30; display: none;
    align-items: center; justify-content: center;
    background: rgba(15, 17, 21, .6); font-size: 14px;
  }
  .busy-overlay.show { display: flex; }
```

- [ ] **Step 4: Elements** — directly after `<div class="toast" id="toast"></div>` add:

```html
  <div class="busy-pill" id="busy-pill"></div>
  <div class="busy-overlay" id="busy-overlay"><span>Saving…</span></div>
```

- [ ] **Step 5: Pass the clicked button to body/network handlers** — in the card template change:
  - `onclick="saveBody('{{ key }}')"` → `onclick="saveBody('{{ key }}', this)"`
  - `onclick="resetBody('{{ key }}')"` → `onclick="resetBody('{{ key }}', this)"`
  - `onclick="saveNetwork('{{ key }}')"` → `onclick="saveNetwork('{{ key }}', this)"`

- [ ] **Step 6: Helpers** — at the very top of `<script>`, before `// ---- Endpoint state mutations`, add:

```js
    // ---- Requests, progress and errors ----

    // Every admin call goes through api(): it rejects on HTTP or network
    // errors with the server's message (toasted unless quiet), so callers
    // update the UI only in their success path and never report a failed
    // save as done.
    function api(url, { method = 'GET', body, quiet = false } = {}) {
      const opts = { method, headers: {} };
      if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      return fetch(url, opts)
        .catch(() => { throw new Error('Network error — is the server running?'); })
        .then(r => r.json().catch(() => ({})).then(d => {
          if (!r.ok) throw new Error(d.error || `Request failed (${r.status})`);
          return d;
        }))
        .catch(err => {
          if (!quiet) showToast(err.message);
          throw err;
        });
    }

    // Errors are already toasted by api(); use as the final .catch().
    const ignore = () => {};

    let pendingCount = 0;
    let pillTimer = null;

    // Shows the top-right pill only for requests still running after
    // 300 ms, so fast calls don't flicker.
    function trackPending(promise, label) {
      pendingCount++;
      if (pendingCount === 1) {
        pillTimer = setTimeout(() => {
          const pill = document.getElementById('busy-pill');
          pill.textContent = label;
          pill.classList.add('show');
        }, 300);
      }
      return promise.finally(() => {
        pendingCount--;
        if (pendingCount === 0) {
          clearTimeout(pillTimer);
          document.getElementById('busy-pill').classList.remove('show');
        }
      });
    }

    // Disables every element in `els` and spins the first until the request
    // settles. Chip groups pass all their chips, so a second click can't
    // race the first.
    function withBusy(els, promise, label = 'Saving…') {
      const list = Array.isArray(els) ? els : [els];
      list.forEach(el => { el.disabled = true; });
      list[0]?.classList.add('is-busy');
      return trackPending(promise, label).finally(() => {
        list.forEach(el => { el.disabled = false; });
        list[0]?.classList.remove('is-busy');
      });
    }

    // Definition edits reload the page; the overlay covers the request and
    // the reload, and closes again if the request fails.
    function saveThenReload(promise, onOk) {
      const overlay = document.getElementById('busy-overlay');
      overlay.classList.add('show');
      return promise
        .then(d => { if (onOk) onOk(d); location.reload(); })
        .catch(() => overlay.classList.remove('show'));
    }

    // Reflect a server-confirmed selection ({code, variant, body, is_custom})
    // in the card's chips, both badge sets and the body editor.
    function applySelection(card, sel) {
      card.querySelectorAll('.code-btn').forEach(b =>
        b.classList.toggle('active', Number(b.dataset.code) === sel.code));
      renderVariants(card, sel.variant);
      updateBadge(card, sel.code, sel.variant);
      if (sel.is_custom) showCustomBadge(card); else clearCustomBadge(card);
      document.getElementById('editor-text-' + card.dataset.key).value =
        JSON.stringify(sel.body, null, 2);
    }
```

- [ ] **Step 7: Replace the state-mutation call sites**

Replace `setCode`:

```js
    function setCode(key, code, btnEl) {
      const card = btnEl.closest('.card');
      const chips = [btnEl, ...card.querySelectorAll('.code-btn, .variant-btn')];
      withBusy(chips, api('/admin/set', { method: 'POST', body: { key, code } }), 'Loading…')
        .then(sel => {
          applySelection(card, sel);
          showToast(`${key} → ${code}`);
        })
        .catch(ignore);
    }
```

Replace `setVariant`:

```js
    function setVariant(key, variant, btnEl) {
      const card = btnEl.closest('.card');
      const chips = [btnEl, ...card.querySelectorAll('.code-btn, .variant-btn')];
      withBusy(chips, api('/admin/set_variant', { method: 'POST', body: { key, variant } }), 'Loading…')
        .then(sel => {
          applySelection(card, sel);
          showToast(`${key} variant → ${variant}`);
        })
        .catch(ignore);
    }
```

Replace `toggleEditor`, `saveBody`, `resetBody`:

```js
    function toggleEditor(key) {
      const panel = document.getElementById('editor-' + key);
      const isHidden = panel.style.display === 'none';
      panel.style.display = isHidden ? 'block' : 'none';
      if (isHidden) {
        const card = document.querySelector(`.card[data-key="${key}"]`);
        trackPending(api('/admin/body/' + key), 'Loading…')
          .then(sel => applySelection(card, sel))
          .catch(ignore);
      }
    }

    function saveBody(key, btnEl) {
      const parsed = readJsonInput('editor-text-' + key);
      if (parsed === null) return;
      const card = document.querySelector(`.card[data-key="${key}"]`);
      withBusy(btnEl, api('/admin/set_body', { method: 'POST', body: { key, body: parsed } }))
        .then(sel => {
          applySelection(card, sel);
          showToast(`${key} body saved`);
        })
        .catch(ignore);
    }

    function resetBody(key, btnEl) {
      const card = document.querySelector(`.card[data-key="${key}"]`);
      withBusy(btnEl, api('/admin/reset_body', { method: 'POST', body: { key } }))
        .then(sel => {
          applySelection(card, sel);
          showToast(`${key} body reset`);
        })
        .catch(ignore);
    }
```

Replace `saveNetwork` (body-building lines unchanged; only the request part and signature change):

```js
    function saveNetwork(key, btnEl) {
      const mode = document.getElementById('delay-mode-' + key).value;
      let delay = null;
      if (mode === 'fixed') {
        delay = { mode: 'fixed', seconds: Number(document.getElementById('delay-seconds-' + key).value) };
      } else if (mode === 'random') {
        delay = {
          mode: 'random',
          min: Number(document.getElementById('delay-min-' + key).value),
          max: Number(document.getElementById('delay-max-' + key).value),
        };
      }
      const drop = document.getElementById('drop-' + key).checked;
      const n = Number(document.getElementById('flaky-n-' + key).value);
      const flaky = n >= 2 ? { one_in_n: n, mode: document.getElementById('flaky-mode-' + key).value } : null;

      withBusy(btnEl, api('/admin/set_network', { method: 'POST', body: { key, delay, drop, flaky } }))
        .then(() => {
          const card = document.querySelector(`.card[data-key="${key}"]`);
          if (card) {
            const net = { delay, drop, flaky };
            card.dataset.network = JSON.stringify(net);
            updateNetBadge(card, net);
          }
          showToast(`${key} network sim updated`);
        })
        .catch(ignore);
    }
```

- [ ] **Step 8: Replace reload-style call sites**

Replace `resetAll`:

```js
    function resetAll() {
      if (!confirm('Reset every endpoint to its default response?')) return;
      saveThenReload(api('/admin/reset_all', { method: 'POST' }));
    }
```

Replace `defsRequest`:

```js
    function defsRequest(url, method, payload, onOk) {
      return saveThenReload(api(url, { method, body: payload }), onOk);
    }
```

Replace `deleteCategory`:

```js
    function deleteCategory(name) {
      if (!confirm(`Delete category "${name}"?`)) return;
      const overlay = document.getElementById('busy-overlay');
      overlay.classList.add('show');
      api('/admin/defs/category', { method: 'DELETE', body: { name }, quiet: true })
        .then(() => location.reload())
        .catch(err => {
          overlay.classList.remove('show');
          // Non-empty category: server said pass cascade -- confirm the
          // destructive version explicitly before retrying.
          if (err.message.includes('cascade') &&
              confirm(`"${name}" still has endpoints. Delete them too?`)) {
            defsRequest('/admin/defs/category', 'DELETE', { name, cascade: true });
          } else {
            showToast(err.message);
          }
        });
    }
```

Replace `loadScenarios`, `applyScenario`, `saveCurrentScenario`:

```js
    function loadScenarios() {
      api('/admin/scenarios')
        .then(scenarios => {
          const bar = document.getElementById('scenario-bar');
          bar.replaceChildren();
          Object.entries(scenarios).forEach(([name, meta]) => {
            const btn = document.createElement('button');
            btn.className = 'scenario-btn' + (meta.builtin ? ' builtin' : '');
            btn.textContent = name;
            btn.onclick = () => applyScenario(name);
            bar.appendChild(btn);
          });
          const saveBtn = document.createElement('button');
          saveBtn.className = 'scenario-btn save-new';
          saveBtn.textContent = '+ Save current as...';
          saveBtn.onclick = saveCurrentScenario;
          bar.appendChild(saveBtn);
        })
        .catch(ignore);
    }

    function applyScenario(name) {
      saveThenReload(api('/admin/scenario/apply', { method: 'POST', body: { name } }));
    }

    function saveCurrentScenario() {
      const name = prompt('Name this scenario:');
      if (!name) return;
      trackPending(api('/admin/scenario/save', { method: 'POST', body: { name } }), 'Saving…')
        .then(() => {
          showToast(`Scenario "${name}" saved`);
          loadScenarios();
        })
        .catch(ignore);
    }
```

Leave `refreshLog` on plain `fetch` (it polls every 2 s; toasting on every failed poll would be noise).

- [ ] **Step 9: Run tests**

Run: `venv/bin/python -m pytest -q`
Expected: all pass, including both `test_admin_page.py` tests.

- [ ] **Step 10: Commit**

```bash
git add templates/admin.html tests/test_admin_page.py
git commit -m "feat(admin): progress feedback and server-confirmed updates

Failed saves no longer look successful; code/variant switches update the
body editor; reloads run behind a Saving overlay.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Editor label, unsaved-edit guard and active-chip no-op

**Files:**
- Modify: `templates/admin.html` (CSS, editor panel markup, `applySelection`, `setCode`, `setVariant`, add `isEditorDirty`)
- Test: `tests/test_admin_page.py` (add one test)

**Interfaces:**
- Consumes: `applySelection`, `withBusy`, `api` (Task 4).
- Produces: `isEditorDirty(key) -> boolean`; element `#editor-label-<key>`; textarea `data-clean` attribute holding the last server-confirmed text.

- [ ] **Step 1: Write the failing test** — append to `tests/test_admin_page.py`:

```python
def test_body_editor_has_selection_label(client):
    assert 'id="editor-label-items_list"' in admin_html(client)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_admin_page.py::test_body_editor_has_selection_label -q`
Expected: FAIL.

- [ ] **Step 3: Markup and CSS**

In the card template, change the first line inside the editor panel:

```html
            <div class="editor-panel" id="editor-{{ key }}" style="display:none;">
              <div class="editor-label" id="editor-label-{{ key }}"></div>
              <textarea id="editor-text-{{ key }}" rows="8" spellcheck="false"></textarea>
```

Add to CSS (next to `.editor-panel` rules):

```css
  .editor-label { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
```

- [ ] **Step 4: Extend `applySelection`** — replace its last statement (the `editor-text-` assignment) with:

```js
      const key = card.dataset.key;
      const text = document.getElementById('editor-text-' + key);
      text.value = JSON.stringify(sel.body, null, 2);
      text.dataset.clean = text.value;
      document.getElementById('editor-label-' + key).textContent =
        `Editing ${sel.code} / ${sel.variant}` + (sel.is_custom ? ' · custom' : '');
```

Add after `applySelection`:

```js
    // True when the open body editor holds text the server hasn't confirmed.
    function isEditorDirty(key) {
      const panel = document.getElementById('editor-' + key);
      const text = document.getElementById('editor-text-' + key);
      return panel.style.display !== 'none' && text.value !== (text.dataset.clean ?? text.value);
    }
```

- [ ] **Step 5: Guard `setCode` and `setVariant`** — add as the first lines of each function body:

`setCode`:

```js
      // Re-clicking the active code is a no-op: no request, no prompt.
      if (btnEl.classList.contains('active')) return;
      if (isEditorDirty(key) && !confirm('Discard unsaved body edits?')) return;
```

`setVariant`:

```js
      if (btnEl.classList.contains('active')) return;
      if (isEditorDirty(key) && !confirm('Discard unsaved body edits?')) return;
```

- [ ] **Step 6: Run tests**

Run: `venv/bin/python -m pytest -q`
Expected: all pass (node check included).

- [ ] **Step 7: Commit**

```bash
git add templates/admin.html tests/test_admin_page.py
git commit -m "feat(admin): show what the body editor edits; guard unsaved edits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Selection kept in the URL hash

**Files:**
- Modify: `templates/admin.html` (`saveSelection`, add `storeSelection`/`readHashSelection`, `init`, `submitEndpointForm`, `submitCategoryForm`, `submitEndpointEdit`)
- Test: `tests/test_admin_page.py` (node check covers syntax; behaviour is checked manually in Task 8)

**Interfaces:**
- Consumes: `defsRequest(url, method, payload, onOk)` (Task 4).
- Produces: `storeSelection(category: string|null, key: string|null)` — writes `#cat=…&ep=…` via `history.replaceState` and `localStorage[SELECTION_KEY]`. Used by Task 7.

- [ ] **Step 1: Replace `saveSelection`** (keep `SELECTION_KEY`, `selectedCategory`, `selectedKey`, `loadSelection`):

```js
    // The selection lives in the URL hash (per tab, so other tabs can't
    // overwrite it, and it survives reloads) and in localStorage (used
    // only for fresh visits with no hash).
    function storeSelection(category, key) {
      localStorage.setItem(SELECTION_KEY, JSON.stringify({ category, key }));
      const params = new URLSearchParams();
      if (category) params.set('cat', category);
      if (key) params.set('ep', key);
      history.replaceState(null, '', '#' + params.toString());
    }

    function saveSelection() {
      storeSelection(selectedCategory, selectedKey);
    }

    function readHashSelection() {
      const params = new URLSearchParams(location.hash.slice(1));
      return { category: params.get('cat'), key: params.get('ep') };
    }
```

- [ ] **Step 2: Replace `init`**

```js
    (function init() {
      const fromHash = readHashSelection();
      const saved = fromHash.category ? fromHash : loadSelection();
      const catRows = [...document.querySelectorAll('.cat-row')];
      const validCat = catRows.some(r => r.dataset.category === saved.category)
        ? saved.category
        : catRows[0]?.dataset.category;
      if (validCat) selectCategory(validCat, { silent: true });
      // Not silent: on narrow screens a restored endpoint opens its detail
      // pane instead of dropping the user back on the category list.
      if (saved.key && document.querySelector(`.ep-row[data-key="${saved.key}"]`)) {
        selectEndpoint(saved.key);
      }
      if (localStorage.getItem(LOG_OPEN_KEY) === '1') setLogOpen(true);
      loadScenarios();
    })();
```

- [ ] **Step 3: Pin the selection before reloads that change it**

Replace `submitEndpointForm`:

```js
    function submitEndpointForm(event) {
      event.preventDefault();
      let body;
      try {
        body = JSON.parse(document.getElementById('ep-form-body').value);
      } catch (e) {
        showToast('Invalid JSON body: ' + e.message);
        return false;
      }
      defsRequest('/admin/defs/endpoint', 'POST', {
        category: selectedCategory,
        method: document.getElementById('ep-form-method').value,
        path: document.getElementById('ep-form-path').value.trim(),
        body,
      }, d => storeSelection(selectedCategory, d.key));  // select the new endpoint after reload
      return false;
    }
```

Replace `submitCategoryForm`:

```js
    function submitCategoryForm(event) {
      event.preventDefault();
      const mode = document.getElementById('cat-form-mode').value;
      const name = document.getElementById('cat-form-name').value.trim();
      const icon = document.getElementById('cat-form-icon').value.trim();
      const color = document.getElementById('cat-form-color').value;
      if (mode === 'add') {
        defsRequest('/admin/defs/category', 'POST', { name, icon, color },
                    () => storeSelection(name, null));
      } else {
        const original = document.getElementById('cat-form-original').value;
        defsRequest('/admin/defs/category', 'PATCH',
                    { name: original, new_name: name, icon, color },
                    () => {
                      if (original === selectedCategory) storeSelection(name, selectedKey);
                    });
      }
      return false;
    }
```

Replace `submitEndpointEdit`:

```js
    function submitEndpointEdit(key) {
      const category = document.getElementById('editep-category-' + key).value;
      defsRequest('/admin/defs/endpoint', 'PATCH', {
        key,
        method: document.getElementById('editep-method-' + key).value,
        path: document.getElementById('editep-path-' + key).value.trim(),
        category,
      }, () => storeSelection(category, key));
    }
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add templates/admin.html
git commit -m "fix(admin): keep the selected category/endpoint in the URL hash

Another tab can no longer overwrite it, and creating, renaming or moving
keeps the user's place after reload.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Drag an endpoint onto a category to move it

**Files:**
- Modify: `templates/admin.html` (CSS, `.ep-row` and `.cat-row` attributes, edit-form label, new JS functions)
- Test: `tests/test_admin_page.py` (add one test)

**Interfaces:**
- Consumes: `defsRequest` (Task 4), `storeSelection` (Task 6), existing `PATCH /admin/defs/endpoint {key, category}`.
- Produces: `startEndpointDrag(event, key)`, `categoryDragOver(event, row)`, `dropOnCategory(event, row)`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_admin_page.py`:

```python
def test_endpoint_rows_are_draggable_onto_categories(client):
    html = admin_html(client)
    assert 'draggable="true"' in html
    assert 'ondrop="dropOnCategory(event, this)"' in html
    assert "Move to category" in html
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_admin_page.py::test_endpoint_rows_are_draggable_onto_categories -q`
Expected: FAIL.

- [ ] **Step 3: Markup**

On the `.cat-row` div, add after `onclick="selectCategory(this.dataset.category)"`:

```html
           ondragover="categoryDragOver(event, this)"
           ondragleave="this.classList.remove('drop-target')"
           ondrop="dropOnCategory(event, this)"
```

On the `.ep-row` div, add after `onclick="selectEndpoint(this.dataset.key)"`:

```html
             draggable="true" ondragstart="startEndpointDrag(event, this.dataset.key)"
```

In the edit-endpoint form, wrap the category select (touch devices have no drag-and-drop):

```html
            <label>Move to category
              <select id="editep-category-{{ key }}">
                {% for cat in grouped_endpoints.keys() %}<option>{{ cat }}</option>{% endfor %}
              </select>
            </label>
```

CSS (after the `.cat-row.dimmed` rule):

```css
  .cat-row.drop-target {
    background: var(--card);
    outline: 2px dashed var(--cat-accent, var(--accent)); outline-offset: -2px;
  }
  .ep-row[draggable="true"] { cursor: grab; }
```

- [ ] **Step 4: JS** — add before `// ---- Scenarios ----`:

```js
    // ---- Drag an endpoint onto a category to move it ----

    function startEndpointDrag(event, key) {
      event.dataTransfer.setData('text/plain', key);
      event.dataTransfer.effectAllowed = 'move';
    }

    function categoryDragOver(event, row) {
      event.preventDefault();  // allows the drop
      event.dataTransfer.dropEffect = 'move';
      row.classList.add('drop-target');
    }

    function dropOnCategory(event, row) {
      event.preventDefault();
      row.classList.remove('drop-target');
      const key = event.dataTransfer.getData('text/plain');
      // Anything that isn't one of our endpoint rows (e.g. dragged text) is ignored.
      const epRow = document.querySelector(`.ep-row[data-key="${CSS.escape(key)}"]`);
      const target = row.dataset.category;
      if (!epRow || epRow.dataset.category === target) return;
      defsRequest('/admin/defs/endpoint', 'PATCH', { key, category: target },
                  () => storeSelection(target, key));
    }
```

- [ ] **Step 5: Run tests**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add templates/admin.html tests/test_admin_page.py
git commit -m "feat(admin): drag an endpoint onto a category to move it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: End-to-end verification in the browser

**Files:** none changed unless a check fails (fix in the owning task's file, add a regression test where it's backend, re-run, commit).

- [ ] **Step 1: Full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 2: Start a local server on a scratch DB**

```bash
MOCKSERVER_ADMIN_PASSWORD= MOCKSERVER_DB_PATH=<scratchpad>/verify.db PORT=4599 venv/bin/python app.py
```

(run in the background; `<scratchpad>` is the session scratchpad directory, never the repo's `mock.db`.)

- [ ] **Step 3: Seed test data via the UI** — add a category named `Customer App & QA #1 🧪`, add an API `/qa/thing` in it.

- [ ] **Step 4: Check each behaviour in Chrome** (DevTools → Network → throttling "Slow 4G" for progress checks):

| # | Action | Expected |
|---|--------|----------|
| a | Click a different code chip (throttled) | Chip spins, chips disabled, pill "Loading…" after ~300 ms, editor body and label update on success |
| b | Click the active code chip | No network request, no prompt |
| c | Edit body, Save, switch 200→404→200 | Edited body back, "custom" badge and label `Editing 200 / normal · custom` |
| d | Edit body without saving, click another code | Confirm "Discard unsaved body edits?"; Cancel keeps selection and text |
| e | DevTools → block request URL `/admin/set`, click a code | Error toast, chips unchanged |
| f | Add a variant (throttled) | "Saving…" overlay, reload lands on same category + endpoint |
| g | Two tabs on different endpoints; add a variant in tab A, reload tab B | Each tab keeps its own selection |
| h | Reload with `Customer App & QA #1 🧪` selected | Same category restored (hash is URL-encoded) |
| i | Drag `/qa/thing` onto `Sample Items` | Overlay, reload, endpoint selected under `Sample Items` |
| j | Drag an endpoint onto its own category; drag selected page text onto a category | Nothing happens |
| k | Window < 900px, reload with an endpoint selected | Opens on the detail pane |
| l | Request `GET /api/1.0.0/items` with `x-mock-response-code: 404` after saving a 404 override | Override body served; log shows `404 (custom)` |

- [ ] **Step 5: Stop the server; delete `<scratchpad>/verify.db*`.**

- [ ] **Step 6: Report** results per row (pass/fail with what was seen). Do not merge or push; hand off to superpowers:finishing-a-development-branch.
