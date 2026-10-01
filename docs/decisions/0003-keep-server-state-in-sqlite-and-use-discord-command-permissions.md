# ADR-0003: Keep server state in SQLite and use Discord command permissions

- **Status:** Accepted
- **Date:** 2026-10-01
- **Issue:** #5

## Context

Each server has a little durable state: the voice channel SobaFM was placed in, and three settings (play duration, volume, and change cooldown). Server managers also need to control who may start, stop, and configure SobaFM ([requirements](../requirements.md#access)). SobaFM is self-hosted, often on a single small machine.

## Requirements

1. State survives restarts and container replacement.
2. No external service is needed.
3. Adding a setting requires no migration tooling.
4. Access control is familiar to Discord server managers.
5. The custom code is minimal.

## Options considered

Storage:

1. **Environment variables only.** No runtime state; settings could not change per server without a restart.
2. **A JSON file.** No dependency, but atomic writes and concurrent access would need custom code.
3. **SQLite through the standard library's `sqlite3`.** Transactional, a single file, and part of Python; blocking calls run in a worker thread through `asyncio.to_thread`.
4. **An ORM with migrations, such as SQLAlchemy with Alembic.** Capable, but excessive for one table. `aiosqlite` would add a dependency for the same behavior as option 3.

Access control:

1. **A custom role setting.** Duplicates a Discord feature and needs its own commands and checks.
2. **Discord's application command permissions.** Default permissions are declared with each command, and server managers adjust them per role, member, or channel in Server Settings → Integrations.

## Decision

Store one row per server in SQLite with the standard library's `sqlite3`: the remembered channel and a `GuildSettings` document stored as JSON and validated by Pydantic, so new fields take their defaults ([data model](../architecture.md#data-model)). Use Discord's command permissions for access: `/join`, `/leave`, and `/settings` default to Manage Server. Code enforces only what Discord's permissions cannot express: `/play` and `/stop` require the caller to be in SobaFM's voice channel, except that members with Manage Server may use `/stop` from anywhere.

## Consequences

- No database dependency or migration tooling, and server managers use a familiar interface.
- The database file must live on a persistent volume in container deployments.
- Settings stored as JSON cannot be queried by field, which v1 does not need.
- The voice-channel rule lives in code because Discord's permissions cannot express it.

## Revisit triggers

- SobaFM needs to run as several processes or shards.
- Settings require relational queries.
- Discord's command permission model changes.
