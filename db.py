"""SQLite data layer: endpoint definitions, runtime state, scenarios.

The single source of truth at runtime. endpoints.py is seed data only;
mock_state.json is a legacy file imported once on first seed.
"""

import json
import re
import sqlite3
import threading
from pathlib import Path

from matcher import PARAM_RE, path_signature

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
RESERVED_PREFIX = "/admin"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_ICON = "📦"
DEFAULT_COLOR = "#5b8cff"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
  name     TEXT PRIMARY KEY,
  icon     TEXT NOT NULL,
  color    TEXT NOT NULL,
  position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoints (
  key            TEXT PRIMARY KEY,
  category       TEXT NOT NULL REFERENCES categories(name)
                 ON UPDATE CASCADE,
  method         TEXT NOT NULL,
  path           TEXT NOT NULL,
  path_signature TEXT NOT NULL,
  position       INTEGER NOT NULL,
  UNIQUE (method, path_signature)
);
CREATE TABLE IF NOT EXISTS responses (
  endpoint_key TEXT NOT NULL REFERENCES endpoints(key) ON DELETE CASCADE,
  code         INTEGER NOT NULL,
  variant      TEXT NOT NULL,
  body_json    TEXT NOT NULL,
  PRIMARY KEY (endpoint_key, code, variant)
);
CREATE TABLE IF NOT EXISTS endpoint_state (
  key              TEXT PRIMARY KEY REFERENCES endpoints(key) ON DELETE CASCADE,
  active_code      INTEGER NOT NULL,
  active_variant   TEXT NOT NULL,
  custom_body_json TEXT,
  network_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenarios (
  name          TEXT PRIMARY KEY,
  snapshot_json TEXT NOT NULL
);
"""

DEFAULT_NETWORK = {"delay": None, "drop": False, "flaky": None}


class Database:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
        self.definitions_version = 0

    # -- reads ----------------------------------------------------------

    def get_categories(self) -> list:
        rows = self._conn.execute(
            "SELECT name, icon, color, position FROM categories ORDER BY position"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_endpoints(self) -> dict:
        eps = {}
        for r in self._conn.execute(
            "SELECT key, category, method, path FROM endpoints ORDER BY position"
        ):
            eps[r["key"]] = {
                "category": r["category"], "method": r["method"],
                "path": r["path"], "responses": {},
            }
        for r in self._conn.execute(
            "SELECT endpoint_key, code, variant, body_json FROM responses"
        ):
            eps[r["endpoint_key"]]["responses"].setdefault(r["code"], {})[
                r["variant"]] = json.loads(r["body_json"])
        return eps

    def get_endpoint(self, key: str):
        return self.get_endpoints().get(key)

    def get_grouped_endpoints(self) -> dict:
        grouped = {c["name"]: {} for c in self.get_categories()}
        for key, cfg in self.get_endpoints().items():
            grouped.setdefault(cfg["category"], {})[key] = cfg
        return grouped

    # -- runtime state --------------------------------------------------

    def default_state_entry(self, key: str) -> dict:
        codes = sorted(self.get_endpoint(key)["responses"])
        return {
            "active_code": 200 if 200 in codes else codes[0],
            "active_variant": "normal",
            "custom_body": None,
            "network": dict(DEFAULT_NETWORK),
        }

    def get_state(self) -> dict:
        state = {}
        for r in self._conn.execute(
            "SELECT key, active_code, active_variant, custom_body_json, network_json"
            " FROM endpoint_state"
        ):
            state[r["key"]] = {
                "active_code": r["active_code"],
                "active_variant": r["active_variant"],
                "custom_body": json.loads(r["custom_body_json"])
                if r["custom_body_json"] is not None else None,
                "network": json.loads(r["network_json"]),
            }
        return state

    def set_state(self, key: str, entry: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO endpoint_state"
                " (key, active_code, active_variant, custom_body_json, network_json)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET active_code=excluded.active_code,"
                "  active_variant=excluded.active_variant,"
                "  custom_body_json=excluded.custom_body_json,"
                "  network_json=excluded.network_json",
                (key, entry["active_code"], entry["active_variant"],
                 json.dumps(entry["custom_body"])
                 if entry["custom_body"] is not None else None,
                 json.dumps(entry["network"])),
            )

    def reset_state_to_defaults(self) -> None:
        for key in self.get_endpoints():
            self.set_state(key, self.default_state_entry(key))

    # -- scenarios ------------------------------------------------------

    def get_scenarios(self) -> dict:
        return {
            r["name"]: json.loads(r["snapshot_json"])
            for r in self._conn.execute("SELECT name, snapshot_json FROM scenarios")
        }

    def save_scenario(self, name: str, snapshot: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO scenarios (name, snapshot_json) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET snapshot_json=excluded.snapshot_json",
                (name, json.dumps(snapshot)),
            )

    # -- seeding --------------------------------------------------------

    def seed_if_empty(self, seed_endpoints, icons, colors,
                      legacy_state_file=None) -> bool:
        if self._conn.execute("PRAGMA user_version").fetchone()[0] == 1:
            return False
        with self._lock, self._conn:
            self._seed_categories(seed_endpoints, icons, colors)
            self._upsert_seed_endpoints(seed_endpoints)
        existing_state = set(self.get_state())
        for key in seed_endpoints:
            if key not in existing_state:
                self.set_state(key, self.default_state_entry(key))
        if legacy_state_file is not None:
            self._import_legacy_state(Path(legacy_state_file))
        with self._lock, self._conn:
            self._conn.execute("PRAGMA user_version = 1")
        self.definitions_version += 1
        return True

    def _seed_categories(self, seed_endpoints, icons, colors) -> None:
        # Only called on an empty DB (seed_if_empty); restore_builtins does
        # its own category upsert because REPLACE-ing a referenced category
        # row would violate the endpoints FK. Uses DO NOTHING to be safely
        # re-entrant after a crash mid-seed.
        positions = {}
        for cfg in seed_endpoints.values():
            cat = cfg["category"]
            if cat not in positions:
                positions[cat] = len(positions)
                self._conn.execute(
                    "INSERT INTO categories (name, icon, color, position)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(name) DO NOTHING",
                    (cat, icons.get(cat, DEFAULT_ICON),
                     colors.get(cat, DEFAULT_COLOR), positions[cat]),
                )

    def _upsert_seed_endpoints(self, seed_endpoints) -> None:
        # ON CONFLICT DO UPDATE, never INSERT OR REPLACE: REPLACE deletes
        # the old row first, and the ON DELETE CASCADE on endpoint_state
        # would silently wipe the user's saved state for that endpoint.
        for position, (key, cfg) in enumerate(seed_endpoints.items()):
            self._conn.execute(
                "INSERT INTO endpoints"
                " (key, category, method, path, path_signature, position)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET category=excluded.category,"
                "  method=excluded.method, path=excluded.path,"
                "  path_signature=excluded.path_signature",
                (key, cfg["category"], cfg["method"], cfg["path"],
                 path_signature(cfg["path"]), position),
            )
            self._conn.execute("DELETE FROM responses WHERE endpoint_key = ?", (key,))
            for code, variants in cfg["responses"].items():
                for variant, body in variants.items():
                    self._conn.execute(
                        "INSERT INTO responses (endpoint_key, code, variant, body_json)"
                        " VALUES (?, ?, ?, ?)",
                        (key, code, variant, json.dumps(body)),
                    )

    def _import_legacy_state(self, legacy_file: Path) -> None:
        """One-time migration of the pre-DB mock_state.json, with the same
        defensive validation Store._load used to do: unknown keys skipped,
        network merged over the default shape, invalid active_code falls
        back to that endpoint's defaults.
        """
        if not legacy_file.exists():
            return
        try:
            on_disk = json.loads(legacy_file.read_text())
        except (json.JSONDecodeError, OSError):
            return
        endpoints = self.get_endpoints()
        for key, entry in on_disk.get("state", {}).items():
            if key not in endpoints:
                continue
            default = self.default_state_entry(key)
            merged = dict(default)
            merged.update(entry)
            network = dict(default["network"])
            network.update(merged.get("network") or {})
            merged["network"] = network
            if merged.get("active_code") not in endpoints[key]["responses"]:
                merged = default
            self.set_state(key, merged)
        for name, snapshot in on_disk.get("scenarios", {}).items():
            self.save_scenario(name, snapshot)
        legacy_file.rename(legacy_file.with_suffix(legacy_file.suffix + ".imported"))

    # -- validation helpers ---------------------------------------------

    @staticmethod
    def _require_name(value, what):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{what} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _validate_key_format(key):
        if not KEY_RE.match(key):
            raise ValueError(
                "endpoint key must match ^[A-Za-z0-9_-]+$ (letters, digits, "
                "underscore, hyphen only)")

    @staticmethod
    def _validate_route_shape(method, path):
        """DB-independent route shape validation: method, leading slash, reserved
        prefix, and parameter segment format. Does not check uniqueness."""
        if method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must start with '/'")
        if path == RESERVED_PREFIX or path.startswith(RESERVED_PREFIX + "/"):
            raise ValueError("paths under /admin are reserved")
        for seg in path.strip("/").split("/"):
            if ("<" in seg or ">" in seg) and not PARAM_RE.match(seg):
                raise ValueError("params must be whole segments like <id>")

    def _validate_route(self, method, path, exclude_key=None):
        self._validate_route_shape(method, path)
        row = self._conn.execute(
            "SELECT key FROM endpoints WHERE method = ? AND path_signature = ?",
            (method, path_signature(path)),
        ).fetchone()
        if row and row["key"] != exclude_key:
            raise ValueError(f"{method} {path} already matches endpoint '{row['key']}'")

    @staticmethod
    def _validate_code(code):
        if not isinstance(code, int) or isinstance(code, bool) or not 100 <= code <= 599:
            raise ValueError("status code must be an integer 100-599")

    @staticmethod
    def _validate_body(body):
        if not isinstance(body, dict):
            raise ValueError("response body must be a JSON object")

    def _validate_responses(self, responses):
        if not isinstance(responses, dict) or not responses:
            raise ValueError("at least one status code is required")
        for code, variants in responses.items():
            self._validate_code(code)
            if not isinstance(variants, dict) or "normal" not in variants:
                raise ValueError(f"code {code} needs a 'normal' variant")
            for variant, body in variants.items():
                self._require_name(variant, "variant name")
                self._validate_body(body)

    def _bump(self):
        self.definitions_version += 1

    def _reset_state_if_invalid(self, key):
        cfg = self.get_endpoint(key)
        entry = self.get_state().get(key)
        if entry is None:
            self.set_state(key, self.default_state_entry(key))
            return
        variants = cfg["responses"].get(entry["active_code"], {})
        if entry["active_code"] not in cfg["responses"] or \
                entry["active_variant"] not in variants:
            self.set_state(key, self.default_state_entry(key))

    # -- category CRUD --------------------------------------------------

    def create_category(self, name, icon=DEFAULT_ICON, color=DEFAULT_COLOR):
        name = self._require_name(name, "category name")
        if any(c["name"] == name for c in self.get_categories()):
            raise ValueError(f"category '{name}' already exists")
        if icon is not None and not isinstance(icon, str):
            raise ValueError("icon must be a string")
        if color is not None and not isinstance(color, str):
            raise ValueError("color must be a string")
        position = len(self.get_categories())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO categories (name, icon, color, position) VALUES (?, ?, ?, ?)",
                (name, icon or DEFAULT_ICON, color or DEFAULT_COLOR, position),
            )
        self._bump()

    def update_category(self, name, new_name=None, icon=None, color=None):
        cats = {c["name"]: c for c in self.get_categories()}
        if name not in cats:
            raise ValueError(f"unknown category '{name}'")
        if new_name is not None:
            new_name = self._require_name(new_name, "category name")
            if new_name != name and new_name in cats:
                raise ValueError(f"category '{new_name}' already exists")
        if icon is not None and not isinstance(icon, str):
            raise ValueError("icon must be a string")
        if color is not None and not isinstance(color, str):
            raise ValueError("color must be a string")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE categories SET name = ?, icon = ?, color = ? WHERE name = ?",
                (new_name or name, icon or cats[name]["icon"],
                 color or cats[name]["color"], name),
            )
        self._bump()

    def delete_category(self, name, cascade=False):
        if not any(c["name"] == name for c in self.get_categories()):
            raise ValueError(f"unknown category '{name}'")
        members = [k for k, cfg in self.get_endpoints().items()
                   if cfg["category"] == name]
        if members and not cascade:
            raise ValueError(
                f"category '{name}' still has {len(members)} endpoint(s); "
                "pass cascade to delete them too")
        for key in members:
            self.delete_endpoint(key)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM categories WHERE name = ?", (name,))
        self._bump()

    # -- endpoint CRUD --------------------------------------------------

    def create_endpoint(self, key, category, method, path, responses):
        key = self._require_name(key, "endpoint key")
        self._validate_key_format(key)
        if self._conn.execute(
                "SELECT 1 FROM endpoints WHERE key = ?", (key,)).fetchone():
            raise ValueError(f"endpoint '{key}' already exists")
        if not any(c["name"] == category for c in self.get_categories()):
            raise ValueError(f"unknown category '{category}'")
        self._validate_route(method, path)
        self._validate_responses(responses)
        position = self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM endpoints").fetchone()[0]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO endpoints"
                " (key, category, method, path, path_signature, position)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, category, method, path, path_signature(path), position),
            )
            for code, variants in responses.items():
                for variant, body in variants.items():
                    self._conn.execute(
                        "INSERT INTO responses (endpoint_key, code, variant, body_json)"
                        " VALUES (?, ?, ?, ?)",
                        (key, code, variant, json.dumps(body)),
                    )
        self.set_state(key, self.default_state_entry(key))
        self._bump()

    def update_endpoint(self, key, category=None, method=None, path=None):
        cfg = self.get_endpoint(key)
        if cfg is None:
            raise ValueError(f"unknown endpoint '{key}'")
        category = category if category is not None else cfg["category"]
        method = method if method is not None else cfg["method"]
        path = path if path is not None else cfg["path"]
        if not any(c["name"] == category for c in self.get_categories()):
            raise ValueError(f"unknown category '{category}'")
        self._validate_route(method, path, exclude_key=key)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE endpoints SET category = ?, method = ?, path = ?,"
                " path_signature = ? WHERE key = ?",
                (category, method, path, path_signature(path), key),
            )
        self._bump()

    def delete_endpoint(self, key):
        if self.get_endpoint(key) is None:
            raise ValueError(f"unknown endpoint '{key}'")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM endpoints WHERE key = ?", (key,))
        self._bump()

    # -- response CRUD --------------------------------------------------

    def set_response(self, key, code, variant, body):
        cfg = self.get_endpoint(key)
        if cfg is None:
            raise ValueError(f"unknown endpoint '{key}'")
        self._validate_code(code)
        variant = self._require_name(variant, "variant name")
        self._validate_body(body)
        if code not in cfg["responses"] and variant != "normal":
            raise ValueError('a new status code must start with a "normal" variant')
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO responses (endpoint_key, code, variant, body_json)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(endpoint_key, code, variant)"
                " DO UPDATE SET body_json=excluded.body_json",
                (key, code, variant, json.dumps(body)),
            )
        self._bump()

    def delete_response(self, key, code, variant=None):
        cfg = self.get_endpoint(key)
        if cfg is None:
            raise ValueError(f"unknown endpoint '{key}'")
        if code not in cfg["responses"]:
            raise ValueError(f"endpoint '{key}' has no code {code}")
        if variant is not None and not isinstance(variant, str):
            raise ValueError("variant must be a string")
        variants = cfg["responses"][code]
        if variant is None or (variant == "normal" and len(variants) == 1):
            if len(cfg["responses"]) == 1:
                raise ValueError("an endpoint must keep at least one status code;"
                                 " delete the endpoint instead")
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM responses WHERE endpoint_key = ? AND code = ?",
                    (key, code))
        else:
            if variant not in variants:
                raise ValueError(f"code {code} has no variant '{variant}'")
            if variant == "normal":
                raise ValueError('the "normal" variant can only be removed by'
                                 " deleting its whole status code")
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM responses WHERE endpoint_key = ? AND code = ?"
                    " AND variant = ?", (key, code, variant))
        self._reset_state_if_invalid(key)
        self._bump()

    # -- restore built-ins ---------------------------------------------

    def restore_builtins(self, seed_endpoints, icons, colors):
        try:
            with self._lock, self._conn:
                existing_cats = {c["name"] for c in self.get_categories()}
                next_pos = len(existing_cats)
                seen = set()
                for cfg in seed_endpoints.values():
                    cat = cfg["category"]
                    if cat in seen:
                        continue
                    seen.add(cat)
                    if cat in existing_cats:
                        self._conn.execute(
                            "UPDATE categories SET icon = ?, color = ? WHERE name = ?",
                            (icons.get(cat, DEFAULT_ICON),
                             colors.get(cat, DEFAULT_COLOR), cat))
                    else:
                        self._conn.execute(
                            "INSERT INTO categories (name, icon, color, position)"
                            " VALUES (?, ?, ?, ?)",
                            (cat, icons.get(cat, DEFAULT_ICON),
                             colors.get(cat, DEFAULT_COLOR), next_pos))
                        next_pos += 1
                self._upsert_seed_endpoints(seed_endpoints)
        except sqlite3.IntegrityError as e:
            # A user-added endpoint occupies a built-in's method+path; the
            # UNIQUE(method, path_signature) index rejects the upsert.
            raise ValueError(
                "restore conflicts with a user-added endpoint on the same"
                f" method+path -- delete or move it first ({e})")
        for key in seed_endpoints:
            self._reset_state_if_invalid(key)
        self._bump()

    # -- export / import ------------------------------------------------

    def export_all(self) -> dict:
        return {
            "version": 1,
            "categories": [{"name": c["name"], "icon": c["icon"], "color": c["color"]}
                           for c in self.get_categories()],
            "endpoints": {
                key: {
                    "category": cfg["category"], "method": cfg["method"],
                    "path": cfg["path"],
                    "responses": {str(code): variants
                                  for code, variants in cfg["responses"].items()},
                }
                for key, cfg in self.get_endpoints().items()
            },
            "state": self.get_state(),
            "scenarios": self.get_scenarios(),
        }

    def import_all(self, payload, mode):
        if mode not in ("merge", "replace"):
            raise ValueError('mode must be "merge" or "replace"')
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported export payload (want version 1)")
        # Validate into plain structures first so a bad payload rejects
        # atomically, before any row is touched.
        categories = payload.get("categories") or []
        endpoints = {}
        for key, cfg in (payload.get("endpoints") or {}).items():
            try:
                responses = {int(code): variants
                             for code, variants in cfg["responses"].items()}
            except (KeyError, TypeError, AttributeError, ValueError):
                raise ValueError(f"endpoint '{key}': malformed responses")
            endpoints[key] = {"category": cfg.get("category"),
                              "method": cfg.get("method"),
                              "path": cfg.get("path"), "responses": responses}
        known_cats = {c.get("name") for c in categories}
        if mode == "merge":
            known_cats |= {c["name"] for c in self.get_categories()}
        for key, cfg in endpoints.items():
            if cfg["category"] not in known_cats:
                raise ValueError(f"endpoint '{key}': unknown category"
                                 f" '{cfg['category']}'")
            self._validate_responses(cfg["responses"])

        # Pre-validate all field shapes and intra-payload uniqueness before
        # destructive operations (replace mode wipes all tables).
        for c in categories:
            self._require_name(c.get("name"), "category name")
            icon = c.get("icon")
            if icon is not None and not isinstance(icon, str):
                raise ValueError("icon must be a string")
            color = c.get("color")
            if color is not None and not isinstance(color, str):
                raise ValueError("color must be a string")
        for key, cfg in endpoints.items():
            self._require_name(key, "endpoint key")
            self._validate_key_format(key)
            self._validate_route_shape(cfg["method"], cfg["path"])
        # Detect intra-payload route duplicates
        seen_routes = {}
        for key, cfg in endpoints.items():
            route = (cfg["method"], path_signature(cfg["path"]))
            if route in seen_routes:
                raise ValueError(f"endpoints '{seen_routes[route]}' and '{key}' share"
                                 f" the same route ({cfg['method']} {cfg['path']})")
            seen_routes[route] = key

        if mode == "replace":
            with self._lock, self._conn:
                for table in ("responses", "endpoint_state", "endpoints",
                              "categories", "scenarios"):
                    self._conn.execute(f"DELETE FROM {table}")
        for c in categories:
            name = self._require_name(c.get("name"), "category name")
            if any(x["name"] == name for x in self.get_categories()):
                self.update_category(name, icon=c.get("icon"), color=c.get("color"))
            else:
                self.create_category(name, c.get("icon") or DEFAULT_ICON,
                                     c.get("color") or DEFAULT_COLOR)
        for key, cfg in endpoints.items():
            if self.get_endpoint(key) is not None:
                self.delete_endpoint(key)
            self.create_endpoint(key, cfg["category"], cfg["method"],
                                 cfg["path"], cfg["responses"])
        for key, entry in (payload.get("state") or {}).items():
            if self.get_endpoint(key) is None:
                continue
            default = self.default_state_entry(key)
            merged = dict(default)
            merged.update(entry)
            if merged.get("active_code") not in self.get_endpoint(key)["responses"]:
                merged = default
            self.set_state(key, merged)
        for name, snapshot in (payload.get("scenarios") or {}).items():
            self.save_scenario(name, snapshot)
        self._bump()
