# qUnleashed catalog server

One service with three jobs:

- **a public application catalog** that speaks the official Archivarius API
  (`/api/v0/*`, what the OFW mobile apps and qUnleashed read) and a v1 on top
  of it for the things OFW does not have: menu styles, animations, authors,
  popularity, install folders, build history;
- **a source archive**: every published version is a commit and a tag in a git
  repository per family, mirrored to a git host of your choice; bundles and
  builds come from the archive, never from the author's repository again;
- **Flibler**, the private build service the app uses to compile a version
  for the firmware API it has (`/api/v1/builds*`, HMAC-signed as before).

The design is written up in `docs/apps-catalog-plan.md` of the qUnleashed
repository; section numbers below refer to it.

## Install

```bash
sudo useradd -r -m -d /opt/qunleashed-catalog catalog
sudo -u catalog bash -c '
  cd /opt/qunleashed-catalog
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cp .env.example .env
'
sudo mkdir -p /var/lib/qunleashed-catalog && sudo chown catalog: /var/lib/qunleashed-catalog
sudo nano /opt/qunleashed-catalog/.env        # secrets and repositories, see the comments inside
sudo cp systemd/qunleashed-catalog.service /etc/systemd/system/
sudo systemctl enable --now qunleashed-catalog
```

`git` must be on the PATH. ufbt is installed by `requirements.txt`; every SDK
version it needs is downloaded into the data directory on first use.

Put nginx in front: only `/api/` and `/_docs` need to be reachable. The live
OpenAPI description is at `/_docs`.

## Layout of the data directory

```
catalog.sqlite        items, versions, build history, jobs, downloads, sdks
archive/<family>/     git: /<source>/<category>[/<sub>]/<appid>/  (section 4)
sdk/zips/<target>/    every SDK zip ever seen, by version
sdk/homes/<target>/<version>/   one ufbt home per SDK version
cache/<family>/<version>/<target>/<sdk>/   built .fap / .fal, 7 days from the build
bundles/              bundle.zip files exported from the archive, 7 days
assets/               icons and screenshots, by content hash
sources/              mirrors of the source repositories
logs/                 one ufbt log per job
```

## Sources (section 4.4, 4.5)

One appid has one record; the source with the highest priority owns it:
`unleashed` (your catalog), then `ofw`, then `atp`. Former ids stay valid as
aliases.

**Your catalog uploads.** Its CI zips the sources and posts them:

```bash
curl -H "Authorization: Bearer $QU_CI_TOKEN" \
     -F source=@app.zip -F origin=unleashed -F category=GPIO/ESP32 \
     -F commit_sha=<40 hex> -F origin_url=https://git.example.org/me/app.git \
     https://catalog.example.org/api/v1/sources/upload
```

The zip holds `application.fam` at the root or under `code/`. With a
`manifest.yml` beside it (description, changelog, screenshots) the OFW rules
apply; without one the zip is taken as raw sources. The server turns it into a
bundle in the OFW layout, archives it and queues the builds. `origin=atp` is
accepted for the day ATP uploads its own apps. A request without a valid token
bans its address for `QU_BAN_DAYS` (one day): every upload from it is refused
until then, with the right token too. Behind nginx set `QU_TRUST_PROXY=1` so
the ban hits the client and not the proxy.

`POST /api/v0/application/version/bundle` takes a finished `bundle.zip`, the
way the OFW catalog CI submits to Archivarius; same token.

**Read by the server** once a day (`QU_SOURCE_CHECK_HOUR_UTC`), or on
`POST /api/v1/sources/<name>/check` with the token:

| Name | Repository | Rules |
|---|---|---|
| `ofw` | `QU_OFW_CATALOG_REPO` | `applications/<Category>[/<Sub>]/<appid>/manifest.yml`, `commit_sha` required; only what changed since the last pass is rebuilt; the OFW uid is fetched once per app so `.fim` files written from the official catalogue keep matching |
| `menustyles` | `QU_MENUSTYLES_REPO` | one `.fal` per `PLUGIN` block of `application.fam`; category = folder under `styles/` |
| `animations` | `QU_ANIMATIONS_REPO` | every folder with `meta.txt`; version = content hash, install = update |

ATP is not read: it is expected to upload its apps the same way your catalog
does.

A version is published when `fap_version` (or `version` in `manifest.yml`)
grows; commits without a bump change nothing.

## Builds (section 8.5, 8.8)

Nothing public ever starts a build. `build/compatible` serves the cache or
answers `404 {"status": "expired"|"never"}` / `409 {"status": "failed"}`.

Builds are queued by, in priority order:

1. `POST /api/v1/builds` from the app (signed). Always first; a running
   scheduler build is left to finish and reused when it is the same key.
2. a new version (every active API);
3. a new SDK, found by reading the SDK index at startup and in the daily
   pass, most downloaded first. Two channels are kept, `release` and `dev`
   (`QU_SDK_CHANNELS`). Builds under the current release are held for as long
   as it is current and everything is rebuilt when it changes. Dev may be
   published several times a day; it is read once a day, and its builds live
   `QU_CACHE_TTL_DAYS` (7) from the build, like those of a superseded release;
4. keep-warm: under the current dev SDK the month's top is rebuilt a day
   before it expires, for the weeks dev does not move.

`POST /api/v1/builds` takes `{family, version_id, target, api}` or, as before,
`{bundle_url, target, api, alias}`. The response is a job; its progress streams
from `GET /api/v1/builds/{id}/events` (SSE, events `queued`, `sdk`, `building`,
`ready`, `failed`) or polls from `GET /api/v1/builds/{id}`. `DELETE` cancels
the caller's subscription; a build nobody waits for is killed.

Every build result is kept in the history (`builds` table): a successful build
keeps the item visible for that API after its file expired; a failed build is
remembered per SDK version and is not retried until a new version or SDK.

The sandbox is yours to provide: `QU_BUILD_COMMAND_PREFIX` is prepended to the
ufbt command (bubblewrap, firejail, docker). ufbt executes `application.fam`
as Python, so run it without network and with the SDK read-only.

## Status codes of `/api/v1/builds`

| Code | When |
|---|---|
| 200 / 201 | known job / new job |
| 400 | bad JSON or fields, unknown `api`, foreign `bundle_url` host |
| 401 / 403 | missing or wrong signature, foreign User-Agent, stale timestamp |
| 404 | unknown job, or a job the caller never subscribed to; dead bundle link |
| 409 | the version already failed under this SDK; artifact not ready; |
| 410 | artifact expired |
| 429 | queue full |
| 502 / 504 | bundle host unreachable / timed out |

## Search and lists

`GET /api/v1/search?query=&target=&api=&family=&limit=` searches the three
families at once, each filtered as its own list, and returns
`{"items": [...], "totals": {family: n}, "authors": [...]}`. `GET
/api/v1/{root}` lists carry the full count in `X-Total-Count`.

## Uploads of every family

`POST /api/v1/sources/upload` takes `family` = `application` (default),
`menustyle` or `animation`:

- `menustyle`: a zip with `application.fam` whose `PLUGIN` block is the style;
  installed under `/ext/apps_data/loader/plugins`;
- `animation`: a zip with one folder holding `meta.txt` and `frame_*.bm` (or
  those files at the root plus `name`); the version is the content hash.

`origin=atp` is accepted for applications only.

## Animation bundles

`POST /api/v1/animations/version/bundle`, Bearer `QU_CI_TOKEN`, a wrong token
bans the address like the upload does. Multipart field `bundle`: a zip with
`manifest.yml`, `meta.txt` and `frame_*.bm` at the root and nothing else. The
manifest gives `id` (the folder name on the SD card), `category`, `author`,
`short_description`, `description`, `sourcecode.location` and the dolphin
entry `weight`, `min_butthurt`, `max_butthurt`, `min_level`, `max_level`,
which `/plan` returns in `manifest_merge.entry`. The version is the hash of
the files and of that entry. 201 for a new version, 200 when unchanged, 400
for anything else in the zip.

## Administration

Bearer `QU_ADMIN_TOKEN`; a wrong token bans the address like the upload does.
Empty token = these endpoints refuse everything.

| Endpoint | Does |
|---|---|
| `GET /api/v1/admin/bans` | active bans |
| `DELETE /api/v1/admin/bans/{ip}` | lift a ban |
| `POST /api/v1/admin/{root}/{id}/hide` / `unhide` | hide or show an item of any source |
| `POST /api/v1/admin/builds/retry` | `{id, family?, version_id?, target?, api?}`: forget a failed build and queue it again; without `api`, under every current SDK |
| `POST /api/v1/admin/sdk/check` | read the SDK index now |

## Rate limit

`QU_RATE_LIMIT_PER_MIN` (600) requests per minute per address on `/api/`;
above it `429` with `Retry-After`. `0` turns it off.

## CORS

Off by default. `QU_CORS_ORIGINS` takes a comma-separated list of origins
(`https://lab.example.org,https://app.example.org`) or `*`. Allowed methods are
`GET`, `POST`, `DELETE`; the signature headers (`X-QU-Sha256`,
`X-QU-Sign-Ed25519`, `X-QU-Kid`) are exposed so a page can verify artifacts.
Credentials are not allowed: the private endpoints authenticate with headers,
not cookies.

## Signing (section 8.7)

With `QU_SIGNING_KEY_HEX` set, every artifact, bundle and plan file carries
`X-QU-Sha256`, `X-QU-Sign-Ed25519` and `X-QU-Kid`; `GET /api/v1/signing`
publishes the key. Generate a seed with `openssl rand -hex 32`.

## Push (section 3.6)

With `QU_FCM_SERVICE_ACCOUNT` set, a new version is announced on the `catalog`
topic once its first build is ready, and on `catalog_item_<id>` and
`catalog_author_<id>` for whoever follows the app or the author. More than
`QU_PUSH_DIGEST_THRESHOLD` per day fold into one digest. Without a service
account messages are logged instead of sent.

## Tests

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest .tests
```

The tests replace ufbt and SDK deployment with fakes; nothing downloads.
