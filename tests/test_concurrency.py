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
