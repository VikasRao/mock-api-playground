# Upgrading to schema v2 (per-variant body overrides)

## What changes on first start

`mock.db` is migrated from schema v1 to v2 automatically. Each endpoint's single
custom body becomes an override for its active code and variant, stored in the
new `custom_bodies` table. Overrides are now kept per endpoint + code + variant,
so switching code or variant no longer discards them.

## Before deploying

Back up the database first:

    cp mock.db mock.db.v1-backup

On PythonAnywhere the file is `/home/vikasrao88/mock-api-playground/mock.db`.
After pulling the new code, Reload the web app from the Web tab.

## Rolling back

Restore the backup together with the old code:

    cp mock.db.v1-backup mock.db

Do not run old code against a v2 `mock.db`. It does not understand the new
schema: it re-seeds the built-in endpoints (losing any edits made to them) and
ignores the overrides.
