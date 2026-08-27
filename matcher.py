"""Pure method+path matching for DB-defined mock endpoints.

Flask cannot register routes after startup, so user-added endpoints are
served by one catch-all route that asks a RouteMatcher (rebuilt whenever
definitions change) which endpoint key owns the request.
"""

import re

PARAM_RE = re.compile(r"^<[^/<>]+>$")


def _segments(path: str) -> list:
    if path == "/":
        return [""]
    return path.strip("/").split("/")


def path_signature(path: str) -> str:
    """Normalize a path pattern so two patterns that could match the same
    request (same literals, params in the same positions) get the same
    signature -- used to enforce route uniqueness at definition time.
    """
    return "/".join("<>" if PARAM_RE.match(s) else s for s in _segments(path))


class RouteMatcher:
    def __init__(self, endpoints: dict):
        self._routes = []
        for key, cfg in endpoints.items():
            segs = _segments(cfg["path"])
            literal_count = sum(1 for s in segs if not PARAM_RE.match(s))
            self._routes.append((cfg["method"], segs, literal_count, key))

    def match(self, method: str, path: str):
        req = _segments(path)
        best_key, best_literals = None, -1
        for route_method, segs, literal_count, key in self._routes:
            if route_method != method or len(segs) != len(req):
                continue
            ok = all(
                (PARAM_RE.match(p) and r != "") or p == r
                for p, r in zip(segs, req)
            )
            if ok and literal_count > best_literals:
                best_key, best_literals = key, literal_count
        return best_key
