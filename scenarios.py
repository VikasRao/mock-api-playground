"""
Built-in scenario presets: named snapshots that flip several endpoints
at once. Each entry maps an endpoint key to the state it should be set
to when the scenario is applied.
"""

_NO_NETWORK = {"delay": None, "drop": False, "flaky": None}

BUILTIN_SCENARIOS = {
    "Server errors": {
        "items_list": {"active_code": 500, "active_variant": "normal", "custom_body": None, "network": dict(_NO_NETWORK)},
        "items_create": {"active_code": 500, "active_variant": "normal", "custom_body": None, "network": dict(_NO_NETWORK)},
    },
}
