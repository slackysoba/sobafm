# 0009: Keep settings on a writable /data volume and move the catalog to /catalog

Status: **Accepted** (2026-11-05), under [#126](https://github.com/slackysoba/sobafm/issues/126), which asks the implementer to decide the layout.

This record supersedes one part of [ADR 0007](0007-fma-dataset-batch-catalog.md): its Location row, `./data/fma/` mounted read-only at `/data`. The audio moves to `./catalog/fma/`, mounted read-only at `/catalog`. It stays out of Git and the image. ADR 0007's limits, requests, removal path and acceptance are unchanged, and that record isn't edited.

## Problem

The requirements ask for persisted settings in SQLite and a persistent `/data` volume. Until now `/data` was a read-only bind of the operator's catalog and audio, and the root filesystem is read-only, so the service had nowhere to write. The catalog, reviews and audio must stay read-only. The service also needs a defined result when its database can't be used.

## Options

1. **A named volume at `/data`, and `./catalog` read-only at `/catalog`.** A new named volume takes the ownership of the image's `/data`, UID 1000, so it works for any host user with no setup. It survives `docker compose down` and can't be committed by accident. Operators must move their files once and change three paths.
2. **A writable `./data` bind at `/data`, and `./catalog` read-only at `/catalog`.** The database is easy to see on the host. But the directory must be writable by UID 1000, which needs a `chown` wherever the operator's UID differs.
3. **Keep `./data` for the catalog and mount it read-only at `/catalog`, with a named volume at `/data`.** No files move, but the host's `./data` would no longer be the container's `/data`, which misleads anyone reading the guide or debugging a path.
4. **A writable volume nested at `/data/state`, keeping the read-only bind at `/data`.** Nothing moves, but it contradicts the requirement that `/data` is the persistent volume.

## Decision

Option 1. The database is `/data/sobafm.db`, named by the required `SOBAFM_DATABASE`. Compose names the volume `sobafm-data`, so settings don't depend on the checkout's directory name. The root filesystem stays read-only and the service stays non-root, with no new capability or tmpfs.

**At startup the database must open, pass validation and accept a real write, or the service exits 4 before connecting to Discord.** The log line names a safe kind, never a path. The check covers a missing or unwritable location, a file that isn't a database, a newer schema, an invalid row and too many servers. A refused file is left exactly as it was. The alternative, running on defaults, would look healthy while every server's settings were silently lost or ignored, and a read-only mount is the likeliest cause, so failing closed makes the mistake obvious. The restart policy stops retrying after five attempts, as it does for exit 2 and 3.

A write that fails while running changes nothing and tells the manager so. Playback carries on, because no audio path depends on the database.

## Consequences

- Operators with an existing `./data` run `mv data catalog` once and update `SOBAFM_CATALOG`, `SOBAFM_REVIEWS` and `SOBAFM_MEDIA` in `.env`. Without that, startup refuses with exit 2 and a message naming the variable.
- `docker compose down -v` deletes the settings. [Operations](../operations.md) says so and gives a backup command.
- The decoder runs as the same user, so it could write to `/data` if it were compromised. Its command line writes only to stdout. A damaged database stops the next startup rather than being trusted, and it holds only settings, with no credentials. Separating the decoder's user would need capabilities the service deliberately drops.
- The service gains one worker thread and the `sqlite3` module. Both count against the unchanged [ADR 0004](0004-amd64-whole-service-adoption.md) group: no limit or capacity partition changes.

## Verification needed

This issue approves no Docker, so only the file syntax and contents are checked here. The coordinator must exercise the real image and confirm that:

- `import sqlite3` works in the image and its SQLite supports `STRICT` tables (3.37 or later);
- a new `sobafm-data` volume is writable by UID 1000 under the read-only root;
- settings survive `docker compose restart` and `docker compose up --build -d`;
- a read-only or missing `/data` exits 4;
- the backup and restore steps in [Operations](../operations.md#server-settings) work as written;
- the service still fits under the group limit.
