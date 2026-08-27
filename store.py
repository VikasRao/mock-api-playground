"""
Store: runtime behavior for the mock server admin panel.

Definitions and persisted state live in db.Database; Store adds the
per-request behavior on top: body resolution, header overrides, network
simulation, flake counters, the in-memory request log, and scenarios.
"""

import random
import re
import time
from collections import deque

from scenarios import BUILTIN_SCENARIOS

REDACTED = "***redacted***"

# Redaction happens at capture time in log_request, so the log deque never
# holds credentials/PII from the apps under test -- not merely hides them.
_SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token",
}
_SENSITIVE_HEADER_SUBSTRINGS = ("token", "secret", "api-key", "apikey")

# Short names ("pin", "pan", "otp"...) must match as whole words within a
# camelCase/snake_case key -- substring matching would hit "shipping"/
# "companyName". Longer unambiguous names match as substrings so
# "accountNumber"/"account_number"/"newPassword" are all caught.
_SENSITIVE_KEY_WORDS = {
    "pin", "mpin", "otp", "cvv", "cvv2", "pan", "password", "passwd",
    "token", "secret", "auth", "apikey",
}
_SENSITIVE_KEY_SUBSTRINGS = (
    "password", "secret", "token", "apikey",
    "accountnumber", "idnumber", "cardnumber",
)


def _is_sensitive_key(key) -> bool:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", snake) if w]
    if any(w in _SENSITIVE_KEY_WORDS for w in words):
        return True
    joined = "".join(words)
    return any(s in joined for s in _SENSITIVE_KEY_SUBSTRINGS)


def _redact_headers(headers: dict) -> dict:
    return {
        name: REDACTED
        if (name.lower() in _SENSITIVE_HEADERS
            or any(s in name.lower() for s in _SENSITIVE_HEADER_SUBSTRINGS))
        else value
        for name, value in headers.items()
    }


def _redact_body(value):
    if isinstance(value, dict):
        return {k: (REDACTED if _is_sensitive_key(k) else _redact_body(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_body(v) for v in value]
    return value


class Store:
    DROP_HANG_SECONDS = 300

    def __init__(self, db):
        self.db = db
        self.log = deque(maxlen=50)
        self._flake_counters = {}
        self._endpoints_cache = None
        self._endpoints_cache_version = -1

    @property
    def endpoints(self) -> dict:
        if self._endpoints_cache_version != self.db.definitions_version:
            self._endpoints_cache = self.db.get_endpoints()
            self._endpoints_cache_version = self.db.definitions_version
        return self._endpoints_cache

    @property
    def state(self) -> dict:
        return self.db.get_state()

    def _entry(self, key: str) -> dict:
        return self.db.get_state()[key]

    def active_code(self, key: str, headers) -> int:
        header_override = headers.get("x-mock-response-code")
        if header_override:
            try:
                code = int(header_override)
            except ValueError:
                code = None
            if code is not None and code in self.endpoints[key]["responses"]:
                return code
        return self._entry(key)["active_code"]

    def variant_body(self, key: str, code: int, variant: str) -> dict:
        variants = self.endpoints[key]["responses"][code]
        return variants.get(variant, variants["normal"])

    def _resolve_body_and_label(self, key: str, code: int) -> tuple:
        """Return (body, label) for the body that active_body would serve,
        where label is what was *actually* used to produce it -- "custom"
        for a saved custom body, or the real variant name (which may be
        "normal" due to the header-override fallback below, even when the
        UI's active_variant is something else). Used so request-log entries
        can record what was truly served instead of blindly reading the
        UI's active_variant.
        """
        entry = self._entry(key)
        if entry["active_code"] != code:
            # code came from a header override that differs from the UI
            # selection -- ignore any custom body/variant meant for a
            # different code and serve that code's normal variant.
            return self.variant_body(key, code, "normal"), "normal"
        if entry["custom_body"] is not None:
            return entry["custom_body"], "custom"
        variant = entry["active_variant"]
        return self.variant_body(key, code, variant), variant

    def active_body(self, key: str, code: int) -> dict:
        body, _ = self._resolve_body_and_label(key, code)
        return body

    def served_variant_label(self, key: str, code: int) -> str:
        """The label (variant name or "custom") that active_body(key, code)
        actually used -- see _resolve_body_and_label for the full rationale.
        """
        _, label = self._resolve_body_and_label(key, code)
        return label

    def set_code(self, key: str, code: int) -> None:
        entry = self._entry(key)
        entry.update(active_code=code, active_variant="normal", custom_body=None)
        self.db.set_state(key, entry)

    def available_variants(self, key: str, code: int) -> list:
        return sorted(self.endpoints[key]["responses"][code].keys())

    def set_variant(self, key: str, variant: str) -> bool:
        entry = self._entry(key)
        if variant not in self.endpoints[key]["responses"][entry["active_code"]]:
            return False
        entry.update(active_variant=variant, custom_body=None)
        self.db.set_state(key, entry)
        return True

    def reset_all(self) -> None:
        self.db.reset_state_to_defaults()

    def set_custom_body(self, key: str, body: dict) -> None:
        if not isinstance(body, dict):
            raise ValueError("custom body must be a JSON object")
        entry = self._entry(key)
        entry["custom_body"] = body
        self.db.set_state(key, entry)

    def reset_custom_body(self, key: str) -> None:
        entry = self._entry(key)
        entry["custom_body"] = None
        self.db.set_state(key, entry)

    def set_network(self, key: str, network: dict) -> None:
        entry = self._entry(key)
        entry["network"] = {
            "delay": network.get("delay"),
            "drop": bool(network.get("drop", False)),
            "flaky": network.get("flaky"),
        }
        self.db.set_state(key, entry)

    def apply_network(self, key: str) -> None:
        net = self._entry(key)["network"]
        if net["drop"]:
            time.sleep(self.DROP_HANG_SECONDS)
            return
        delay = net["delay"]
        if not delay:
            return
        if delay["mode"] == "fixed":
            time.sleep(delay["seconds"])
        elif delay["mode"] == "random":
            time.sleep(random.uniform(delay["min"], delay["max"]))

    def should_flake(self, key: str) -> bool:
        flaky = self._entry(key)["network"]["flaky"]
        if not flaky:
            return False
        n = flaky["one_in_n"]
        self._flake_counters[key] = self._flake_counters.get(key, 0) + 1
        return self._flake_counters[key] % n == 0

    def log_request(self, key, method, path, headers, body, served_code, served_variant) -> None:
        self.log.appendleft({
            "timestamp": time.time(),
            "key": key,
            "method": method,
            "path": path,
            "headers": _redact_headers(dict(headers)),
            "body": _redact_body(body),
            "served_code": served_code,
            "served_variant": served_variant,
        })

    def get_log(self) -> list:
        return list(self.log)

    def apply_scenario(self, name: str) -> bool:
        scenario = self.db.get_scenarios().get(name) or BUILTIN_SCENARIOS.get(name)
        if scenario is None:
            return False
        endpoints = self.endpoints
        for key, snapshot in scenario.items():
            if key not in endpoints:
                continue
            # A snapshot can reference a code deleted since it was saved --
            # skip rather than persist a state that would 500 every request.
            if snapshot["active_code"] not in endpoints[key]["responses"]:
                continue
            self.db.set_state(key, {
                "active_code": snapshot["active_code"],
                "active_variant": snapshot["active_variant"],
                "custom_body": snapshot["custom_body"],
                "network": dict(snapshot["network"]),
            })
        return True

    def save_scenario(self, name: str, keys: list = None) -> None:
        state = self.state
        keys = keys or list(state.keys())
        self.db.save_scenario(name, {
            key: {
                "active_code": state[key]["active_code"],
                "active_variant": state[key]["active_variant"],
                "custom_body": state[key]["custom_body"],
                "network": dict(state[key]["network"]),
            }
            for key in keys if key in state
        })

    def list_scenarios(self) -> dict:
        result = {name: {"builtin": True, "keys": list(s.keys())}
                  for name, s in BUILTIN_SCENARIOS.items()}
        result.update({name: {"builtin": False, "keys": list(s.keys())}
                       for name, s in self.db.get_scenarios().items()})
        return result
