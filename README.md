# Sample Mock Server

A small Flask mock server for **learning how to deploy to a public host** before deploying
the real thing. Same architecture as the full mock server — SQLite-backed endpoint
definitions (`mock.db`), an admin UI at `/admin`, response variants, network simulation,
scenarios, request log — but seeded with only **two generic endpoints** and invented data.
Nothing here comes from any real backend.

## Endpoints

| Method | Path              | Codes           | Variants (200)   |
|--------|-------------------|-----------------|------------------|
| GET    | `/api/1.0.0/items`| 200, 404, 500   | `normal`, `empty`|
| POST   | `/api/1.0.0/items`| 200, 400, 500   | `normal`         |

Per-request override without touching the UI: send an `x-mock-response-code` header.

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then:

```bash
curl http://localhost:4500/api/1.0.0/items
curl -X POST http://localhost:4500/api/1.0.0/items -H 'Content-Type: application/json' -d '{"name":"Sticky Notes","price":2.75}'
curl http://localhost:4500/api/1.0.0/items -H 'x-mock-response-code: 500'
```

Admin UI: http://localhost:4500/admin

Tests:

```bash
pytest tests/ -v
```

## Deploy to Killercoda (Ubuntu playground)

1. Push this folder to a GitHub repo of your own (it is already a git repo):

   ```bash
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

2. Open a Killercoda **Ubuntu playground** (killercoda.com → Playgrounds → Ubuntu).

3. In the playground terminal:

   ```bash
   git clone <your-repo-url>
   cd sampleMockServer
   export MOCKSERVER_ADMIN_PASSWORD=pick-something   # required — /admin is public here
   bash setup.sh
   ```

   The server starts on port **4500** (override with `export PORT=...` before running).

4. Expose the port: in Killercoda's top-right menu choose **Traffic / Ports** (the
   "Traffic Port Accessor"), enter `4500`, and open the generated URL. Then:

   - `https://<killercoda-url>/api/1.0.0/items` — the GET endpoint
   - `https://<killercoda-url>/admin` — admin UI, log in as user `admin` with the
     password you exported

   Or from a second playground terminal tab:

   ```bash
   curl http://localhost:4500/api/1.0.0/items
   curl -X POST http://localhost:4500/api/1.0.0/items -H 'Content-Type: application/json' -d '{"name":"Test","price":1}'
   ```

Killercoda playgrounds are **ephemeral** (they expire after roughly an hour and wipe
everything, including `mock.db`), so this is purely a rehearsal environment — perfect
for practicing the deploy steps, useless for anything persistent.

## Security notes for public hosting

- `MOCKSERVER_ADMIN_PASSWORD` puts the whole `/admin*` surface behind HTTP Basic Auth
  (username `admin`). `setup.sh` refuses to start without it. Unset is acceptable only
  on localhost.
- Mock endpoints themselves are always unauthenticated by design — apps under test must
  reach them freely.
- Don't put anything real (URLs, tokens, response captures) into this repo; it's meant
  to be pushed publicly.

