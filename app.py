"""
Sample Mock Server with an admin toggle UI (learning/deployment-practice
edition — generic sample data only).

Run:
    pip install -r requirements.txt
    python app.py

Then open:
    http://localhost:4500/admin           -> admin UI
    http://localhost:4500/api/1.0.0/items -> mock API endpoints (respect the UI state)

Per-request override: pass x-mock-response-code to bypass the UI for a
single call. See README.md for deployment steps.
"""

import os
import secrets
import time
import re as _re
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template

from db import Database
from matcher import RouteMatcher
from endpoints import ENDPOINTS, CATEGORY_ICONS, CATEGORY_COLORS, short_label
from store import Store

def _load_env_file(path: Path | None = None) -> None:
    """Load the repo-root .env (or an explicit path, for tests) into the
    environment. Variables already set in the real environment win, so a
    shell export or docker-compose `environment:` beats the file."""
    load_dotenv(path if path is not None else Path(__file__).parent / ".env")


_load_env_file()

DB_FILE = Path(__file__).parent / "mock.db"
LEGACY_STATE_FILE = Path(__file__).parent / "mock_state.json"
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def create_app(db_file: str | None = None) -> Flask:
    app = Flask(__name__)
    # Explicit db_file (tests pass an isolated tmp_path DB) always wins.
    # Otherwise MOCKSERVER_DB_PATH lets a container point mock.db at a
    # mounted volume without touching code; falls back to the repo-root
    # default for a plain `python app.py`.
    resolved_db_file = db_file or os.environ.get("MOCKSERVER_DB_PATH")
    using_default = not resolved_db_file
    db = Database(Path(resolved_db_file) if resolved_db_file else DB_FILE)
    db.seed_if_empty(ENDPOINTS, CATEGORY_ICONS, CATEGORY_COLORS,
                     legacy_state_file=LEGACY_STATE_FILE if using_default else None)
    store = Store(db)
    app.db = db          # exposed for tests and admin CRUD routes
    app.store = store    # exposed for tests

    # With MOCKSERVER_ADMIN_PASSWORD set, every /admin* route requires HTTP
    # Basic Auth (user "admin"); mock endpoints stay open for the apps under
    # test. Unset = open, which is only acceptable for localhost dev.
    admin_password = os.environ.get("MOCKSERVER_ADMIN_PASSWORD")

    @app.before_request
    def require_admin_auth():
        if not admin_password:
            return None
        # Same prefix rule db.py reserves, so no mock route can ever be
        # confused with an admin route.
        path = request.path
        if path != "/admin" and not path.startswith("/admin/"):
            return None
        auth = request.authorization
        if (auth is not None and auth.type == "basic"
                and secrets.compare_digest(auth.username or "", "admin")
                and secrets.compare_digest(auth.password or "", admin_password)):
            return None
        return (jsonify({"error": "authentication required"}), 401,
                {"WWW-Authenticate": 'Basic realm="mock-admin"'})

    def serve(key: str):
        store.apply_network(key)
        code = store.active_code(key, request.headers)
        request_body = request.get_json(silent=True)
        if store.should_flake(key):
            flaky_mode = store.state[key]["network"]["flaky"]["mode"]
            if flaky_mode == "drop":
                time.sleep(store.DROP_HANG_SECONDS)
            served_code, body, served_label = 500, {"message": "Simulated flaky failure"}, "flaky"
        else:
            served_code = code
            body = store.active_body(key, code)
            served_label = store.served_variant_label(key, code)
        store.log_request(
            key, request.method, request.path, dict(request.headers),
            request_body, served_code, served_label,
        )
        return jsonify(body), served_code

    @app.route("/admin")
    def admin_ui():
        endpoints = db.get_endpoints()
        cats = db.get_categories()
        return render_template(
            "admin.html",
            endpoints=endpoints,
            state=store.state,
            variants_by_key={
                key: {code: sorted(variants.keys())
                      for code, variants in cfg["responses"].items()}
                for key, cfg in endpoints.items()
            },
            grouped_endpoints=db.get_grouped_endpoints(),
            category_icons={c["name"]: c["icon"] for c in cats},
            category_colors={c["name"]: c["color"] for c in cats},
            short_labels={key: short_label(cfg["path"])
                          for key, cfg in endpoints.items()},
        )

    @app.route("/admin/set", methods=["POST"])
    def admin_set():
        body = request.get_json(force=True)
        key = body.get("key")
        try:
            code = int(body.get("code"))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid key or code"}), 400
        if key not in store.endpoints or code not in store.endpoints[key]["responses"]:
            return jsonify({"error": "invalid key or code"}), 400
        store.set_code(key, code)
        return jsonify({"key": key, "code": code})

    @app.route("/admin/set_variant", methods=["POST"])
    def admin_set_variant():
        body = request.get_json(force=True)
        key = body.get("key")
        variant = body.get("variant")
        if key not in store.endpoints:
            return jsonify({"error": "invalid key"}), 400
        if not store.set_variant(key, variant):
            return jsonify({"error": "invalid variant for current code"}), 400
        return jsonify({"key": key, "variant": variant})

    @app.route("/admin/body/<key>")
    def admin_get_body(key):
        if key not in store.endpoints:
            return jsonify({"error": "invalid key"}), 404
        code = store.state[key]["active_code"]
        return jsonify({"body": store.active_body(key, code)})

    @app.route("/admin/set_body", methods=["POST"])
    def admin_set_body():
        payload = request.get_json(force=True)
        key = payload.get("key")
        body = payload.get("body")
        if key not in store.endpoints:
            return jsonify({"error": "invalid key"}), 400
        try:
            store.set_custom_body(key, body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"key": key, "ok": True})

    @app.route("/admin/reset_body", methods=["POST"])
    def admin_reset_body():
        payload = request.get_json(force=True)
        key = payload.get("key")
        if key not in store.endpoints:
            return jsonify({"error": "invalid key"}), 400
        store.reset_custom_body(key)
        return jsonify({"key": key, "ok": True})

    @app.route("/admin/set_network", methods=["POST"])
    def admin_set_network():
        payload = request.get_json(force=True)
        key = payload.get("key")
        if key not in store.endpoints:
            return jsonify({"error": "invalid key"}), 400

        drop = payload.get("drop", False)
        if not isinstance(drop, bool):
            return jsonify({"error": "drop must be a boolean"}), 400

        delay = payload.get("delay")
        if delay:
            mode = delay.get("mode")
            if mode == "fixed":
                if "seconds" not in delay:
                    return jsonify({"error": "delay.seconds is required for fixed mode"}), 400
            elif mode == "random":
                if "min" not in delay or "max" not in delay:
                    return jsonify({"error": "delay.min and delay.max are required for random mode"}), 400
            else:
                return jsonify({"error": "invalid delay.mode"}), 400

        flaky = payload.get("flaky")
        if flaky:
            one_in_n = flaky.get("one_in_n")
            if not isinstance(one_in_n, int) or isinstance(one_in_n, bool) or one_in_n < 2:
                return jsonify({"error": "flaky.one_in_n must be an integer >= 2"}), 400
            if flaky.get("mode") not in ("500", "drop"):
                return jsonify({"error": "invalid flaky.mode"}), 400

        store.set_network(key, {
            "delay": delay,
            "drop": drop,
            "flaky": flaky,
        })
        return jsonify({"key": key, "ok": True})

    @app.route("/admin/reset_all", methods=["POST"])
    def admin_reset_all():
        store.reset_all()
        return jsonify(store.state)

    @app.route("/admin/state")
    def admin_state():
        return jsonify(store.state)

    @app.route("/admin/log")
    def admin_log():
        return jsonify(store.get_log())

    @app.route("/admin/scenarios")
    def admin_scenarios():
        return jsonify(store.list_scenarios())

    @app.route("/admin/scenario/apply", methods=["POST"])
    def admin_scenario_apply():
        payload = request.get_json(force=True)
        name = payload.get("name")
        if not store.apply_scenario(name):
            return jsonify({"error": "unknown scenario"}), 400
        return jsonify({"applied": name})

    @app.route("/admin/scenario/save", methods=["POST"])
    def admin_scenario_save():
        payload = request.get_json(force=True)
        name = payload.get("name")
        if not name:
            return jsonify({"error": "name required"}), 400
        store.save_scenario(name, payload.get("keys"))
        return jsonify({"saved": name})

    def _defs_error(e: ValueError):
        return jsonify({"error": str(e)}), 400

    def _generate_key(path: str) -> str:
        base = _re.sub(r"[^a-z0-9]+", "_", short_label(path).lower()).strip("_") or "endpoint"
        key, i = base, 2
        while db.get_endpoint(key) is not None:
            key = f"{base}_{i}"
            i += 1
        return key

    @app.route("/admin/defs/category", methods=["POST", "PATCH", "DELETE"])
    def admin_defs_category():
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return jsonify({"error": "name required"}), 400
        try:
            if request.method == "POST":
                db.create_category(name, data.get("icon"), data.get("color"))
            elif request.method == "PATCH":
                db.update_category(name, new_name=data.get("new_name"),
                                   icon=data.get("icon"), color=data.get("color"))
            else:
                db.delete_category(name, cascade=bool(data.get("cascade")))
        except ValueError as e:
            return _defs_error(e)
        return jsonify({"ok": True})

    @app.route("/admin/defs/endpoint", methods=["POST", "PATCH", "DELETE"])
    def admin_defs_endpoint():
        data = request.get_json(silent=True) or {}
        try:
            if request.method == "POST":
                if not isinstance(data.get("path"), str):
                    return jsonify({"error": "path required"}), 400
                key = data.get("key") or _generate_key(data["path"])
                db.create_endpoint(key, data.get("category"), data.get("method"),
                                   data.get("path"),
                                   {200: {"normal": data.get("body")}})
                return jsonify({"ok": True, "key": key})
            key = data.get("key")
            if not isinstance(key, str):
                return jsonify({"error": "key required"}), 400
            if request.method == "PATCH":
                db.update_endpoint(key, category=data.get("category"),
                                   method=data.get("method"), path=data.get("path"))
            else:
                db.delete_endpoint(key)
        except ValueError as e:
            return _defs_error(e)
        return jsonify({"ok": True})

    @app.route("/admin/defs/response", methods=["POST", "DELETE"])
    def admin_defs_response():
        data = request.get_json(silent=True) or {}
        key, code = data.get("key"), data.get("code")
        if not isinstance(key, str) or not isinstance(code, int):
            return jsonify({"error": "key and integer code required"}), 400
        try:
            if request.method == "POST":
                variant = data.get("variant")
                if variant is None:
                    variant = "normal"
                db.set_response(key, code, variant, data.get("body"))
            else:
                db.delete_response(key, code, variant=data.get("variant"))
        except ValueError as e:
            return _defs_error(e)
        return jsonify({"ok": True})

    @app.route("/admin/defs/restore_builtins", methods=["POST"])
    def admin_defs_restore_builtins():
        try:
            db.restore_builtins(ENDPOINTS, CATEGORY_ICONS, CATEGORY_COLORS)
        except ValueError as e:
            return _defs_error(e)
        return jsonify({"ok": True})

    @app.route("/admin/export")
    def admin_export():
        resp = jsonify(db.export_all())
        resp.headers["Content-Disposition"] = "attachment; filename=mock-definitions.json"
        return resp

    @app.route("/admin/import", methods=["POST"])
    def admin_import():
        data = request.get_json(silent=True) or {}
        try:
            db.import_all(data.get("data"), data.get("mode"))
        except ValueError as e:
            return _defs_error(e)
        return jsonify({"ok": True})

    matcher_cache = {"version": -1, "matcher": None}

    def current_matcher() -> RouteMatcher:
        if matcher_cache["version"] != db.definitions_version:
            matcher_cache["matcher"] = RouteMatcher(db.get_endpoints())
            matcher_cache["version"] = db.definitions_version
        return matcher_cache["matcher"]

    @app.route("/", defaults={"_path": ""}, methods=ALL_METHODS)
    @app.route("/<path:_path>", methods=ALL_METHODS)
    def mock_catch_all(_path):
        key = current_matcher().match(request.method, request.path)
        if key is None:
            return jsonify({"error": "no mock endpoint matches this method+path"}), 404
        return serve(key)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4500))
    # Debug mode (auto-reload + Werkzeug debugger) is on by default for local
    # dev; set MOCKSERVER_DEBUG=0 in containers so the debugger isn't reachable.
    debug = os.environ.get("MOCKSERVER_DEBUG", "1") != "0"
    print("Mock server running:")
    print(f"  Admin UI : http://localhost:{port}/admin")
    print(f"  API base : http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
