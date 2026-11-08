# Coconut activity journal

A LAN-only FastAPI server for the homelab pet-report pipeline. It stores Frigate source events and reconciled visits in **external PostgreSQL**, caches evidence on disk, and renders daily/weekly reports. Configuration and secrets are YAML. No Redis, SQLite, SPA, collector schedule, inference agent, or notification sender is included.

n8n owns collection/pagination, grouping, inference, report selection, and the morning schedule. This repository implements the server portion of the November 7, 2026 proposal.

## Deployment

The recommended installation directory is `/etc/pet-reporting`. The container runs as UID/GID 10001, with a read-only root filesystem and a writable media bind mount. Compose binds to loopback by default at `http://localhost:8092`. Configure your own LAN address through a local, gitignored `.env` file using `PETREPORT_BIND_ADDRESS`, and set the matching `public_base_url` in `config.yaml`. Configure Frigate's origin, allowlisted host, username, and station camera/zone names in that local YAML file. There is no reverse proxy or offsite publishing.

```sh
cd /etc/pet-reporting
sudoedit secrets.yaml
```

Fill in the blank connection values without committing the file:

```yaml
database_url: 'postgresql+psycopg://petreport:URL_ENCODED_PASSWORD@DB_HOST:5432/petreport'
internal_api_token: 'SET_32_PLUS_RANDOM_CHARACTERS'
frigate:
  password: 'YOUR_FRIGATE_PASSWORD'
```

Use a dedicated empty PostgreSQL database and application role with schema migration permissions. Do not use n8n's database. URL-encode reserved characters in the username/password; append PostgreSQL SSL parameters if required by your central database. Store the generated service token only in `secrets.yaml`; use it in the n8n credential store later.

Restart first to read the edited YAML, then migrate and check readiness:

```sh
sudo docker compose up -d --force-recreate
sudo docker compose exec server alembic upgrade head
sudo docker compose exec server python -c 'import urllib.request; from petreport.config import settings; r=urllib.request.Request("http://127.0.0.1:8092/internal/v1/readiness", headers={"Authorization": "Bearer "+settings().internal_api_token.get_secret_value()}); print(urllib.request.urlopen(r).read().decode())'
```

The readiness command reads the token inside the container and does not print it. Do not run `docker compose config` or dump the full YAML when sharing diagnostics. Restart after any configuration change. Schema migrations are explicit, so a database outage never causes an automatic schema reset.

Until PostgreSQL is configured and migrated, `/healthz` returns 200 and the archive page/readiness return 503 with a clear degraded state. Readiness distinguishes the DB, writable media, and whether Frigate credentials are configured; it is **not** an authenticated Frigate connectivity test. No production database or Frigate password is supplied in this repository. All checked-in addresses and camera names are illustrative; keep actual infrastructure settings in gitignored local configuration files.

For a fresh installation elsewhere:

```sh
cp config.example.yaml config.yaml
cp secrets.example.yaml secrets.yaml
mkdir -p media
sudo chown 10001:10001 media
sudo chown root:10001 secrets.yaml
sudo chmod 640 secrets.yaml
# Set a random internal_api_token in secrets.yaml before using the internal API.
sudo docker compose up -d --build
```

Keep the bound address reachable only through the LAN/VPN. Public report/media routes intentionally have no login. The server never edits your firewall or public proxy configuration.

## Public repository configuration policy

Keep real LAN addresses, private domains, SSH targets, service usernames, camera identifiers, deployment inventory, and credentials out of tracked files. Use reserved `.invalid` example domains and loopback addresses in public examples. Store installation-specific values only in gitignored `config.yaml`, `secrets.yaml`, and `.env`. Review the full staged diff before publishing; deleting a value in a later commit does not remove it from Git history.

## API contract

[Published OpenAPI](docs/openapi.json) contains the request schemas. The running schema is served at `GET /internal/v1/openapi.json` with service-token authentication. All `/internal/v1/*` requests require `Authorization: Bearer TOKEN`. All writes also require `Idempotency-Key` (1–128 characters), unique per logical operation and reused for transport retries with **the identical request bytes**. Reusing a key with different input is rejected.

Normal writes and their stored responses commit together. The same result is returned on retry, with `Idempotency-Replayed: true`. Idempotency records are retained for up to seven days (capped by report retention). A replay does not renew a lease: obey the returned lease expiry, use fresh heartbeat keys, and reacquire a run when needed.

**Delivery safety exception:** replaying `POST /deliveries/claim` always returns `may_send: false`, even when the original claim allowed sending. Only a fresh claim returning `may_send: true` permits an attempt. Claimed, accepted, or unknown attempts block another send, including after report regeneration. Only a recorded `definitely_failed` attempt can obtain a new attempt ID. Network timeouts are `unknown`, never `definitely_failed`.

| Operation | Contract |
| --- | --- |
| `POST /runs/claim` | Claim/reclaim one household/local-date run; new fencing token after expiry. Defaults to dry run. |
| `POST /runs/{id}/heartbeat` | Extend a current lease; stale tokens receive 409. |
| `GET /runs/{id}` | Inspect status, stage, notification permission, and lease expiry without revealing its token. |
| `POST /events/upsert` | Upsert normalized events plus original `raw`; record half-open local collection dates/status/coverage. |
| `GET /events` | Read stored overlapping and ongoing tracks using aware `start`/`end`; UUID keyset cursor avoids timestamp ties in this stored-event API. |
| `GET /processing-days` | Calendar completion/gaps and known open source IDs. |
| `PUT /visits/batch` | Replace all visit candidates for an affected calendar range transactionally; omitted candidates are retired. |
| `POST /media/cache` | Up to 10 event IDs / 3 variants per request; server fetches constructed Frigate paths. |
| `GET /media/{id}` | Authenticated inference binary with byte-range support. |
| `PUT /visits/{id}/classification` | Validate identity, enums, and evidence membership; save model/prompt/input versions. |
| `POST /reports/prepare` | Compute immutable factual snapshots; exactly 1 daily or 7 weekly local dates. |
| `PUT /reports/{id}/finalize` | Publish expected revision with deterministic template narration. |
| `POST /deliveries/claim` | Reserve today's household/date/channel slot; forbidden for dry runs, disabled notifications, or historical dates. |
| `POST /deliveries/{attempt}/result` | Record accepted / definitely_failed / unknown; sanitized machine error codes only. |
| `GET /deliveries` | Inspect daily delivery state for recovery. |
| `POST /runs/{id}/finish` | Persist complete/partial/failed state and release a valid lease. |
| `POST /maintenance/cleanup` | Defaults to preview; expire report content, reclaim eligible media, prune unreferenced normalized records. |
| `GET /readiness` | DB/migration and writable-media checks; independent from `/healthz` liveness. |

Paths in this table are relative to `/internal/v1`. Each lease-bound write carries `run_id` and `lease_token`; heartbeat carries only the token and uses the path ID. `finish` includes both the path and body run ID. Small JSON requests are limited to 2 MB. Contending operations return 409; retry with the same key after bounded backoff.

### Visit and report behavior

- The server accepts grouped candidates, **not a grouping algorithm**. n8n supplies `station_id`, `member_event_ids`, optional pet identity, and `grouping_version`; the server derives timestamps and `source:station:earliest-event` keys. Only configured cat station memberships are accepted. A raw event can support a visit at each of two stations, but cannot be reused by two candidates within the same station.
- Include the entire affected ownership range and every desired candidate for it. Boundary visits require expanding the range to include their start and finished member bounds. Requests that would silently retire a visit owned by a previous date are rejected. Preserve known open event IDs and refresh them during collection.
- Ongoing candidates have a null end and remain provisional. Visits count on their local start date; later reports show continuation without counting twice. Bounds are tracking bounds, not behavior duration.
- `events/upsert` resets grouping completion for its processed dates. Call `visits/batch` after every collection refresh. A complete report requires both collection completion and a grouping version for every date. The server trusts n8n's collection-complete assertion; it cannot establish Frigate pagination completeness itself.
- Unchanged batches/classifications do not increment revisions. Membership or classification changes invalidate affected prepared/published reports. A new report revision archives the previous facts and preserves historical media references until that report expires.
- Classification `supported` requires attached ready evidence. Supported behavior counts are deduplicated per visit and labeled model suggestions. This validates structure and provenance, not the truth of a model's visual reasoning.
- Free-form model narration is intentionally rejected in this first server release. Finalization requires `use_template: true` (default), and no narrative. Deterministic summaries always work without Ollama. A grounded narrative validator can be added with the inference workflow.
- Coverage remains explicitly **unknown**. No detections does not imply no activity. Missing collection dates appear as incomplete rather than zero totals. The weekly view highlights the final completed date. Finalize saved daily reports as well if you want weekly day links, then select just one report for delivery.

### Media and recovery

Frigate session cookies stay in the server's HTTP client. The client refreshes once on 401, verifies TLS, blocks redirects, validates event IDs and the allowed HTTPS origin, bounds downloads, checks MIME/file signatures, and atomically renames a temporary file before marking it ready. Original snapshots are the default. `bbox_verified` and `clip_verified` remain false until the installed 0.17.1 schema and actual playback have been verified. Enabling a capability flag is an operator assertion, not a discovery mechanism. No guessed boxes are drawn.

Media I/O and file deletion run outside database transactions. Download intents and cleanup's `pending_delete` states commit before I/O; an interrupted operation returns 409 for its original idempotency key. Inspect run/media state, then retry with a **new operation key** after the five-minute stale-download window. Unique media keys and transactional references prevent duplicate assets; files are redownloaded safely when recovery is needed. The server renews its current lease during bounded media work and fences the final DB write again. Keep n8n cache batches small and extend HTTP execution timeouts appropriately.

Public `/media/{id}` serves only ready files referenced by unexpired published/stale report revisions. Draft evidence is accessible only through the token-protected API. Browser byte ranges support clip seeking; actual source codecs still need footage testing. Templates escape all model/source text and load no external scripts/fonts.

Default retention is 90 days for reports/media, 365 days for unreferenced normalized events/visits, and a seven-day grace period for unpublished media. Shared media survives while any unexpired report references it. Expired report content, revision snapshots and joins are purged; an empty tombstone keeps old links returning 410. Delivery/run audit rows remain to preserve deduplication and history. Cleanup never runs on a server schedule; n8n must invoke it. A failure leaves recoverable DB states for a later cleanup call.

Back up the dedicated PostgreSQL database and `/etc/pet-reporting/media` to compatible restore points; preserve `config.yaml` and `secrets.yaml` separately with appropriate access. Restore and verify the DB plus media together before switching traffic. No production backup/restore has been verified yet.

## Development and validation

```sh
uv sync --frozen
uv run ruff check petreport tests migrations
uv run pytest -q
# Full isolated PostgreSQL suite (no production DB and no published DB port):
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yaml down
```

Local tests skip DB cases unless `TEST_DATABASE=1`; the full Compose suite migrates an isolated ephemeral PostgreSQL 17 database and tests idempotency, fencing/concurrency, transactional reconciliation, report snapshots, classification evidence, escaped HTML, DST, boundary continuation, media visibility/ranges, shared-media retention/recovery, and delivery replay/unknown semantics. Test credentials are synthetic and have no deployment access.

Remaining integration acceptance: central PostgreSQL connectivity/migration, authenticated Frigate snapshots, installed bbox/clip support and codecs, full-day pagination/ties, footage-based grouping thresholds, Ollama inference accuracy, n8n workflow authoring, Home Assistant catalog/device checks, phone review, and coordinated production backup/restore. No n8n workflow or family notification is activated by this server.
