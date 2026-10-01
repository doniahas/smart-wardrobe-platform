# Deployment

This guide describes a generic, self-hosted production deployment of Smart
Wardrobe using the production Docker Compose overlay. It is suitable for a
Linux server (VPS or VM) with Docker and a DNS name pointing at it.

**Deployment is operator-managed.** The repository's CI
(`.github/workflows/ci.yml`) runs the test suites, builds every Docker
image, and runs a live MongoDB smoke test — it does not deploy anything.
Bringing up, updating, backing up, and recovering a server are manual steps
described below.

## Contents

- [Production topology](#production-topology)
- [Requirements](#requirements)
- [First deployment](#first-deployment)
- [HTTPS and reverse proxy (Caddy)](#https-and-reverse-proxy-caddy)
- [Horizontal `web` scaling](#horizontal-web-scaling)
- [Health checks](#health-checks)
- [Persistent state](#persistent-state)
- [Backups](#backups)
- [Updating and rolling back](#updating-and-rolling-back)
- [Recovery procedures](#recovery-procedures)
- [Production cold-seed](#production-cold-seed)
- [Operational hardening checklist](#operational-hardening-checklist)

## Production topology

Production is the base `docker-compose.yml` plus the
`docker-compose.prod.yml` overlay, always applied together:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The overlay changes the local development stack in a few deliberate ways:

| Change | Why |
|---|---|
| Adds a `caddy` service publishing `80`/`443` | The single public entry point: automatic HTTPS and load balancing |
| Removes `web`'s host port (`ports: !override []`) | `web` is reachable only through Caddy on the internal network |
| `scale: 2` on `web` | Two stateless replicas by default |
| `restart: unless-stopped` on every long-running service | Crashed containers recover without manual intervention |
| `web` runs Uvicorn with `--proxy-headers --forwarded-allow-ips=*` | Real client IPs reach the rate limiter instead of Caddy's container IP |
| `SEED_DEMO_DATA=false` for the `seed` service | Production is never seeded with the public demo credentials |

`recommendation`, `llm`, `catalog`, `ollama`, `mongo`, and `redis` publish no
ports in either mode; they are reachable only over the private
`smartwardrobe-net` bridge network. A plain `docker compose up` (without the
overlay) is unaffected by any of this and remains the local development
setup.

## Requirements

- A Linux host with Docker Engine and the `docker compose` plugin. The
  user running the commands must be able to use Docker (member of the
  `docker` group, or via `sudo`).
- A DNS name (A/AAAA record) pointing at the host. Let's Encrypt cannot
  issue a certificate for a bare IP address.
- Inbound TCP `80` and `443` open to the internet (Caddy needs `80` for the
  ACME HTTP challenge and HTTP→HTTPS redirects).
- Enough resources for the local models: roughly 4 GB of disk for the two
  models and several GB of RAM; CPU-only inference works but is slow.

## First deployment

1. **Get the code** onto the host (for example `git clone`, checking out a
   tagged release).

2. **Create the environment file.** `.env` is gitignored and must never be
   committed:

   ```bash
   cp .env.example .env
   ```

   Set at least:

   ```text
   JWT_SECRET=<long random secret>
   MONGO_ROOT_USERNAME=<letters/numbers only>
   MONGO_ROOT_PASSWORD=<long random, letters/numbers only>
   DOMAIN=<your public DNS name, e.g. wardrobe.example.com>
   ```

   Generate secrets with `openssl rand -hex 32` or
   `python3 -c "import secrets; print(secrets.token_hex(32))"`. The Mongo
   credentials are interpolated into a connection URI, so avoid URI-special
   characters. `MONGO_INITDB_ROOT_*` only takes effect on an empty database
   volume; `mongod --auth` is always enforced regardless.

3. **Start the stack:**

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
   ```

4. **Pull the local models** (once per host; they persist in the
   `ollama_data` volume):

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     exec ollama ollama pull qwen2.5:3b
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     exec ollama ollama pull moondream:latest
   ```

5. **Verify** — every service `Up`, `web` replicas `(healthy)`, and the site
   reachable over HTTPS:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
   curl -fsS https://<your-domain>/health/full
   ```

Store `JWT_SECRET` and the Mongo credentials somewhere safe **off the
host** (for example a password manager). Losing them alongside the host
makes backups much harder to use.

## HTTPS and reverse proxy (Caddy)

`Caddyfile` is a single site block for `{$DOMAIN}`:

- **Automatic HTTPS.** Caddy obtains and renews a Let's Encrypt certificate
  for `DOMAIN` and redirects HTTP to HTTPS with no further configuration.
  Certificates and ACME state persist in the `caddy_data` volume.
- **WebSockets.** `/ws` upgrades are proxied transparently; the frontend
  derives `wss://` vs `ws://` from the page protocol, so no application
  change is needed behind HTTPS.
- **Client IPs.** Caddy sets `X-Forwarded-For`, and the overlay's Uvicorn
  flags make `web` trust it. Trusting all forwarders is safe here only
  because `web` publishes no port — Caddy is the only thing that can reach
  it.

### Local HTTPS validation

The full production topology can be exercised on a workstation by setting
`DOMAIN=localhost`. Caddy then uses its internal, self-signed CA instead of
contacting Let's Encrypt:

```bash
DOMAIN=localhost \
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Use this only for local testing, never for a real deployment.

## Horizontal `web` scaling

- **Two replicas by default.** `docker-compose.prod.yml` declares `scale: 2`
  for `web`, so a normal production bring-up starts two replicas with no
  extra flag. The base file has no `scale` key and still runs exactly one
  `web` locally (it also publishes `8000:8000`, which only one container
  can bind).
- **Overriding the count** for a one-off run:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --scale web=3
  ```

  `web` deliberately has no fixed `container_name` (a fixed name would make
  scaling impossible), so Compose names replicas `<project>-web-1`,
  `<project>-web-2`, and so on.
- **Load balancing.** A plain `reverse_proxy web:8000` resolves the `web`
  hostname once and keeps sending every request to that single replica.
  `Caddyfile` instead uses Caddy's `dynamic a web 8000` upstream source,
  which re-resolves Docker's embedded DNS (one A record per healthy
  replica) every 5 seconds and round-robins (`lb_policy round_robin`)
  across all of them.
- **Shared state.** Every replica uses the same MongoDB and Redis, so a JWT
  issued by one replica is valid on all of them, data written through one
  is immediately visible through another, the rate limiter enforces one
  combined ceiling, and a WebSocket notification reaches a user connected
  to any replica (Redis Pub/Sub).
- **Failure tolerance.** If one replica stops, Caddy keeps serving through
  the remaining healthy replicas; a restarted replica rejoins the rotation
  automatically.
- **Check replicas:** `docker compose -f docker-compose.yml -f
  docker-compose.prod.yml ps web` — expect one row per replica, all
  `Up (healthy)`.

Each `web` process also runs its own chat worker pool, so total concurrent
LLM calls equal replicas × `CHAT_WORKER_CONCURRENCY`; the single Ollama
instance is shared by all of them.

## Health checks

| Service | Check |
|---|---|
| `web` | Compose healthcheck on `GET /health`; Caddy and the `seed` service wait for it to be healthy |
| `recommendation`, `llm`, `catalog` | Compose healthchecks on each service's `/health` |
| `mongo` | `mongosh` ping; `web` waits for it to be healthy before starting |
| `redis` | `redis-cli ping` |

`GET /health/full` on `web` additionally reports MongoDB and
`recommendation` reachability. It keeps responding `200` during a database
outage and describes the degraded dependency, so it is suitable for
external uptime monitoring.

`web` starts without waiting for `catalog`, `llm`, or `redis` to be healthy:
each of those dependencies degrades gracefully (catalog suggestions report
`catalog_available: false`, chat reports a controlled error, and rate
limiting falls back to per-process counters), so a slow or broken optional
dependency never blocks the core application.

## Persistent state

| State | Location | Back up? |
|---|---|---|
| Application data (users, wardrobe, outfits, feedback, memory, chat jobs, forum, DMs, notifications) | `mongo_data` named volume | **Yes** |
| Uploaded wardrobe / forum / DM media | `./services/web/uploads` bind mount | **Yes — irreplaceable** |
| Secrets and configuration | `.env` (host file, gitignored) | **Yes, stored off-host** |
| TLS certificates and ACME account | `caddy_data` volume | Recommended (re-issued automatically if lost, subject to Let's Encrypt rate limits) |
| Model weights | `ollama_data` volume | No — re-pull from Ollama |
| Rate-limit counters, Pub/Sub | Redis (no volume) | No — ephemeral by design |
| Catalog data | None — `catalog` is stateless | No |

Recreating containers (`up -d`, image rebuilds) never touches volumes or
bind mounts. Data is lost only through `docker compose down -v`, an explicit
`docker volume rm`, or loss of the host directory — make sure all of these
live on durable storage, not an ephemeral/temporary disk.

## Backups

All scripts live in `scripts/` and only write to the local disk of the host
they run on.

**MongoDB** — `backup_mongo.sh` runs `mongodump` inside the running `mongo`
container and copies a gzipped archive to the host. Credentials are
required (no insecure fallback):

```bash
MONGO_ROOT_USERNAME=... MONGO_ROOT_PASSWORD=... ./scripts/backup_mongo.sh
```

**Uploaded media** — `backup_uploads.sh` tars the uploads directory:

```bash
./scripts/backup_uploads.sh
```

Both support `BACKUP_DIR` (defaults `./backups/mongo` and
`./backups/uploads`) and `BACKUP_RETENTION_DAYS` (default `7`; older local
archives are pruned after each successful backup). `UPLOADS_DIR` and
`MONGO_CONTAINER` can be overridden if your layout differs.

**Verifying a backup** — `verify_backup.sh` restores an archive into a
disposable `mongo:7` container on a separate port, prints per-collection
document counts, and removes the container. It never touches the real
database:

```bash
BACKUP_FILE=./backups/mongo/mongo-20260101-020000.archive.gz ./scripts/verify_backup.sh
```

**Recommended policy**

- Schedule `backup_mongo.sh` and `backup_uploads.sh` daily (cron or a
  systemd timer).
- Copy archives **off the host** regularly — a backup that lives only on
  the server does not protect against losing the server.
- Run `verify_backup.sh` after setting up scheduling and periodically
  afterwards.
- Take a fresh backup before every update or rollback.

## Updating and rolling back

**Update** to a new release:

```bash
./scripts/backup_mongo.sh            # with credentials, as above
./scripts/backup_uploads.sh
git fetch --tags && git checkout <new-release-tag>
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Rebuilding recreates the application containers; expect a short
interruption. Data volumes and the uploads directory are untouched.

**Roll back** by checking out the previous known-good tag or commit and
running the same `up -d --build` command. A rollback restores the
application code and that release's own Compose configuration; it never
recreates or modifies the `mongo` volume.

**Database compatibility caveat.** There is no schema-migration framework.
MongoDB is schemaless and `_ensure_indexes` only adds indexes idempotently.
The codebase has stayed additive (new optional fields read with defaults),
which makes rollbacks *likely* safe, but this is not guaranteed for every
change. Only treat a release as a rollback target once it is known to be
compatible with the current data, and keep a pre-update backup.

**Single-service problems** don't require a rollback: `recommendation`,
`llm`, and `catalog` failures degrade gracefully in `web`. Rebuild or
restart just that service:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build <service>
```

## Recovery procedures

- **Broken application release** — roll back as above; MongoDB and uploads
  are untouched.
- **MongoDB container failure (volume intact)** — `up -d mongo` recreates the
  container against the same volume; usually a non-event.
- **Corrupted or lost MongoDB data** — stop the application services, confirm
  the volume is genuinely unusable (don't wipe on assumption), start only
  `mongo`, run `scripts/restore_mongo.sh` against the most recent verified
  backup, check document counts, then start the rest of the stack.
  `restore_mongo.sh` is destructive (`mongorestore --drop`), requires an
  explicit `BACKUP_FILE`, and asks for typed confirmation.
- **Lost uploads** — extract the most recent `backup_uploads.sh` archive back
  into the uploads directory. Anything uploaded after that backup is
  unrecoverable, which is why backing up before risky operations matters.
- **Full host loss** — provision a new host, install Docker, check out the
  last known-good release, restore `.env` from the off-host copy, bring up
  `mongo` and restore the latest MongoDB backup, restore uploads, start the
  full stack, pull the models, and verify with `GET /health/full`.

## Production cold-seed

**Separate from, and never controlled by, `SEED_DEMO_DATA`.** The local
demo seed (the `seed` Compose service running `scripts/seed_demo_data.py`
with the public `demo_*` accounts and password `DemoSeed123!`) is
hard-disabled in production by the overlay. Running it against a public
site would expose a publicly documented password.

For a production site that shouldn't look empty, `scripts/seed_demo_data.py`
has a separate `--profile production` dataset: five realistic synthetic
users (no `demo_`/`test_` naming), six short posts, five comments, and eight
reactions, created through the real HTTP API with the same idempotent,
per-object reconciliation as the demo profile.

- **Credential:** the production profile's password is never hardcoded. It
  must be provided in the `PROD_SEED_PASSWORD` environment variable (set it
  in the production `.env`, which `web` already loads); the script fails
  before making any request if it is missing. Generate it like any other
  secret and store it off-host.
- **Manual and opt-in only:** it is not wired into either Compose file or
  CI. An operator runs it on demand:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    cp --index 1 scripts/seed_demo_data.py web:/tmp/seed_demo_data.py
  docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    exec --index 1 web python /tmp/seed_demo_data.py --profile production
  ```

  (`--index 1` picks one `web` replica; any replica works, since the
  script talks to the API.)

- **Safe to re-run:** it only creates or reconciles its own objects and never
  deletes or modifies other data. If a real user already holds one of the
  seed usernames, that one step fails loudly instead of overwriting the
  account.

## Operational hardening checklist

Items an operator should confirm for their own environment:

- [ ] Volumes, the uploads directory, `.env`, and `BACKUP_DIR` live on
      durable storage (not an ephemeral/temporary disk).
- [ ] Backups are scheduled and copied off-host; a restore drill with
      `verify_backup.sh` has been run.
- [ ] Inbound access is limited to `80`/`443`, with SSH restricted to known
      sources or a VPN.
- [ ] `.env` is readable only by the deploying user.
- [ ] External uptime monitoring polls `GET /health/full`.
- [ ] The host has enough RAM/CPU for the local models under expected load.
- [ ] If a custom or fine-tuned model is ever used instead of the public
      `qwen2.5:3b`/`moondream` models, `ollama_data` becomes
      backup-critical.

Known architectural limitation: the overlay runs a single MongoDB instance
without a replica set. The application tier scales horizontally; database
high availability (a replica set or managed MongoDB) is the natural next
step for a larger deployment.
