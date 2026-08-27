from matcher import RouteMatcher, path_signature


def _m(**endpoints):
    return RouteMatcher(endpoints)


def test_literal_path_matches_exactly():
    m = _m(a={"method": "GET", "path": "/api/1.0.0/items"})
    assert m.match("GET", "/api/1.0.0/items") == "a"
    assert m.match("PUT", "/api/1.0.0/items") is None
    assert m.match("GET", "/api/1.0.0/items/extra") is None
    assert m.match("GET", "/api/1.0.0") is None


def test_param_segment_matches_any_single_segment():
    m = _m(a={"method": "GET", "path": "/tx/<client_id>/<tx_id>"})
    assert m.match("GET", "/tx/1/4275") == "a"
    assert m.match("GET", "/tx/1") is None
    assert m.match("GET", "/tx/1/4275/resend") is None


def test_exact_beats_parameterized():
    m = _m(
        wild={"method": "GET", "path": "/tx/<id>"},
        exact={"method": "GET", "path": "/tx/all"},
    )
    assert m.match("GET", "/tx/all") == "exact"
    assert m.match("GET", "/tx/99") == "wild"


def test_trailing_slash_in_request_is_tolerated():
    m = _m(a={"method": "GET", "path": "/token"})
    assert m.match("GET", "/token/") == "a"


def test_root_path():
    m = _m(a={"method": "GET", "path": "/"})
    assert m.match("GET", "/") == "a"
    assert m.match("GET", "/x") is None


def test_param_does_not_match_empty_segment():
    m = _m(a={"method": "GET", "path": "/<x>"})
    assert m.match("GET", "/") is None


def test_path_signature_normalizes_param_names():
    assert path_signature("/tx/<client_id>/x") == path_signature("/tx/<cid>/x")
    assert path_signature("/tx/all") != path_signature("/tx/<id>")
