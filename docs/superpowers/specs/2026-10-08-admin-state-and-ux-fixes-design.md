# Admin UI: state persistence, feedback and move-endpoint — design

Date: 2026-10-08
Status: Draft for review

## Goal

Make the admin switchboard trustworthy and less frustrating for people
configuring mock responses:

1. The selected category/endpoint must survive a reload and must not be
   hijacked by another tab.
2. A chosen status code, variant or edited body must stay saved until the
   user deliberately changes it.
3. Saving must show progress and surface failures.
4. Switching code/variant must show progress and update the body editor.
5. Endpoints can be moved to another category by drag-and-drop.

Non-goals: rewriting the admin UI as a JS app; changing the mock-serving API
for apps under test (beyond the header-override detail in §2.3); adding a
JS test framework.

## 1. Findings (verified 2026-10-08)

| # | Issue | Status | Root cause |
|---|-------|--------|------------|
| 1 | Jumps to first category, no endpoint selected | Intermittent | Selection lives only in `localStorage` (shared by all tabs; last click in any tab wins). Falls back to first category when the saved name is missing/renamed. Every definition edit does `location.reload()`, so the fallback is hit often. Narrow layout (<900px) always reloads onto the categories pane. |
| 2a | Custom body lost | Confirmed | `Store.set_code` / `set_variant` set `custom_body=None` — even when the already-active chip is re-clicked. |
| 2b | Custom body lost | Confirmed | One custom-body slot per endpoint, so 200→404→200 discards it. |
| 2c | Status "saved" but isn't | Confirmed | `setCode` / `setVariant` in `admin.html` never check `r.ok`; a 400/500 still flips the chip and shows a success toast. |
| 2d | Lost updates / 500s | Confirmed | Store read-modify-write has no lock around the read; reads on the shared SQLite connection are unlocked. Probe: 200 concurrent `set_code`+`set_network` rounds → 123 lost updates, 35 HTTP 500s (`KeyError` in `get_state`). |
| 3 | No save progress | Confirmed | No spinner/disabled state; double-submits possible. |
| 4 | Editor stale after switch | Confirmed | `toggleEditor` loads the body only when opened; switching then pressing Save writes the old body onto the new code. |
| 5 | Move endpoint | Partial | Possible via ✎ edit form category dropdown (`PATCH /admin/defs/endpoint`); no drag-and-drop. |

## 2. Backend

### 2.1 Per code+variant overrides (schema v2)

New table:

```sql
CREATE TABLE IF NOT EXISTS custom_bodies (
  endpoint_key TEXT NOT NULL REFERENCES endpoints(key) ON DELETE CASCADE,
  code         INTEGER NOT NULL,
  variant      TEXT NOT NULL,
  body_json    TEXT NOT NULL,
  PRIMARY KEY (endpoint_key, code, variant)
);
```

Migration `PRAGMA user_version` 1 → 2, run on startup inside one
transaction: for every `endpoint_state` row with non-null
`custom_body_json`, insert it into `custom_bodies` at
`(key, active_code, active_variant)`, then set `custom_body_json` to NULL.
The column is left in place (unused) to avoid a table rebuild. Fresh
databases are seeded straight to version 2.

### 2.2 Store semantics

- `set_code(key, code)`: sets `active_code`, `active_variant="normal"`.
  Does not touch overrides.
- `set_variant(key, variant)`: sets `active_variant`. Does not touch overrides.
- `set_custom_body(key, body)`: upserts the override for the active
  code+variant.
- `reset_custom_body(key)`: deletes the override for the active
  code+variant only.
- `reset_all()`: resets state to defaults and deletes all overrides.
- `Database.delete_response` deletes overrides for the removed code or
  variant. Deleting an endpoint cascades via FK.
- `restore_builtins` keeps overrides whose code+variant still exists and
  drops the rest.

State shape returned by `Store.state` / `/admin/state`:

```json
{
  "active_code": 200,
  "active_variant": "normal",
  "custom_body": { "...": "override for active code+variant, or null" },
  "custom_bodies": { "200/normal": { }, "404/normal": { } },
  "network": { }
}
```

`custom_body` stays so the template badges and existing callers keep
working.

### 2.3 Serving

`_resolve_body_and_label(key, code)`:

- `code == active_code`: serve the override for `(code, active_variant)` if
  present (label `"custom"`), else the variant body.
- `code != active_code` (header override): serve the override for
  `(code, "normal")` if present (label `"custom"`), else that code's
  `normal` variant. **Behaviour change:** today the header path never
  serves custom bodies.

### 2.4 Endpoint responses

- `POST /admin/set` and `POST /admin/set_variant` return
  `{key, code, variant, body, is_custom}` for the new active selection.
- `GET /admin/body/<key>` returns `{code, variant, body, is_custom}`.
- `POST /admin/set_body` and `POST /admin/reset_body` return the same shape.

### 2.5 Scenarios and export/import

- `save_scenario` snapshots include `custom_bodies`.
- `apply_scenario` accepts both shapes: a snapshot with `custom_bodies`
  replaces that endpoint's overrides with the snapshot's (skipping
  code+variants that no longer exist); a legacy snapshot with only
  `custom_body` applies it to the snapshot's active code+variant.
- `export_all` emits `custom_bodies` in each state entry. `import_all`
  accepts both shapes, same rules as scenarios. Export `version` stays 1
  (the change is additive).

### 2.6 Concurrency

- `Database.update_state(key, fn)`: holds `self._lock` and one transaction
  across read → `fn(entry)` → write. All Store mutations use it.
- All `Database` read methods take `self._lock`, so no read iterates a
  cursor while another thread commits on the shared connection.

## 3. Frontend (`templates/admin.html`)

### 3.1 Request helper

`api(url, {method, body})` wraps `fetch`: JSON encode, parse response,
throw `Error(serverError || 'Request failed')` when `!r.ok` or on network
error, and toast the message. All existing call sites move to it. UI state
changes only in the success path.

### 3.2 Progress

- `withBusy(el, promise)`: disables `el`, adds a spinner class, restores
  on settle. For code/variant chips, all chips on that card are disabled
  for the duration, which serialises switches per endpoint.
- A "Saving…"/"Loading…" pill, fixed at the top-right of the page so it
  is visible on every pane, appears only if a request is still pending
  after 300 ms.
- Reloading actions (definition CRUD, import, restore built-ins, scenario
  apply) show a full-page "Saving…" overlay from request start until the
  reload; on error the overlay closes and the error is toasted.

### 3.3 Editor sync

- Editor header shows `Editing <code> / <variant>` plus a `custom` tag
  when `is_custom`.
- After a successful code/variant switch, the textarea, header and badges
  are updated from the response body, whether or not the editor is open.
- The editor tracks a dirty flag. Switching code/variant with unsaved
  edits asks for confirmation first; cancelling aborts the switch.
- Save and Reset update the textarea and tag from their responses.

### 3.4 Selection persistence

- Every selection change writes `location.hash = #cat=<enc>&ep=<enc>`
  (`history.replaceState`, no history spam) and `localStorage`.
- Init order: hash → `localStorage` → first category.
- Creating a category stores it as the selection before reload; renaming
  the selected category stores the new name.
- When an endpoint is restored on load, narrow layouts open on the
  detail pane.

### 3.5 Drag-and-drop move

- `.ep-row` gets `draggable="true"`; `dragstart` puts the key in
  `dataTransfer`.
- `.cat-row` handles `dragover` (highlight class) / `dragleave` / `drop`.
- Drop on a different category → `PATCH /admin/defs/endpoint
  {key, category}` → selection set to `{category: target, key}` → reload
  behind the overlay. Drop on the endpoint's own category is a no-op.
- The ✎ edit form's category select is labelled "Move to category" as the
  touch-device path.

## 4. Testing

pytest (backend):

- Override survives 200 → 404 → 200 switching.
- Re-clicking the active code keeps the override.
- Reset clears only the active code+variant override.
- Deleting a code / variant removes its overrides; deleting an endpoint
  cascades.
- v1 → v2 migration on a hand-built v1 DB moves the custom body to the
  active code+variant.
- Legacy scenario and import payloads with only `custom_body` still apply.
- Header-override request serves the `(code, "normal")` override.
- `/admin/set` etc. return `body` and `is_custom`.
- Threaded test: concurrent `set_code` + `set_network` on one endpoint →
  0 lost updates, 0 HTTP 500s.
- All existing tests still pass.

Manual (Chrome, local server, DevTools network throttling for slow
requests): spinners and pill, error toast with chips unchanged on a forced
400, editor follows code/variant switches, dirty-edit confirm, reload keeps
selection with two tabs on different endpoints, drag-and-drop move.
