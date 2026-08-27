"""
Static endpoint definitions: route shape + canned response bodies.

ENDPOINTS[key]["responses"][code] is a dict of named variants, e.g.
    {"normal": {...}, "empty": {...}}
Every code always has at least a "normal" variant. Every entry also has
a "category" label used to group endpoints in the admin UI. This module
is seed data only — once mock.db exists it is consulted only when
seeding an empty database or via "Restore built-ins".

This is the sample/learning edition: two generic endpoints with
invented data, nothing captured from any real backend.
"""


def group_by_category(endpoints: dict) -> dict:
    """Group ENDPOINTS by their "category" field, preserving each
    category's first-seen order and each endpoint's original order
    within its category."""
    grouped: dict = {}
    for key, cfg in endpoints.items():
        grouped.setdefault(cfg["category"], {})[key] = cfg
    return grouped


def short_label(path: str) -> str:
    """Last non-parameter path segment, used as the endpoint's display
    name in the admin UI's endpoint list ("/api/1.0.0/items" ->
    "items"). Trailing Flask "<param>" segments are skipped."""
    segments = [s for s in path.strip("/").split("/") if not s.startswith("<")]
    return segments[-1] if segments else path


CATEGORY_ICONS = {
    "Sample Items": "📦",
}

CATEGORY_COLORS = {
    "Sample Items": "#5b8cff",
}

ENDPOINTS = {
    "items_list": {
        "category": "Sample Items",
        "label": "List items",
        "method": "GET",
        "path": "/api/1.0.0/items",
        "responses": {
            200: {
                "normal": {
                    "items": [
                        {"id": 1, "name": "Blue Notebook", "price": 4.50, "inStock": True},
                        {"id": 2, "name": "Gel Pen", "price": 1.20, "inStock": True},
                        {"id": 3, "name": "Desk Lamp", "price": 18.99, "inStock": False},
                    ],
                    "count": 3,
                },
                "empty": {"items": [], "count": 0},
            },
            404: {
                "normal": {"error": "not_found", "message": "No item catalogue available"},
            },
            500: {
                "normal": {"error": "internal_error", "message": "Something went wrong"},
            },
        },
    },
    "items_create": {
        "category": "Sample Items",
        "label": "Create item",
        "method": "POST",
        "path": "/api/1.0.0/items",
        "responses": {
            200: {
                "normal": {
                    "id": 4,
                    "name": "Sticky Notes",
                    "price": 2.75,
                    "inStock": True,
                    "message": "Item created",
                },
            },
            400: {
                "normal": {
                    "error": "validation_failed",
                    "message": "name and price are required",
                },
            },
            500: {
                "normal": {"error": "internal_error", "message": "Something went wrong"},
            },
        },
    },
}
