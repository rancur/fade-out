# Database migrations (Alembic)

Schema changes are versioned here instead of relying on `Base.metadata.create_all`.
`app.database.init_db()` (create_all) is still used for the zero-config dev/first-run
path, but production schema changes should go through Alembic.

## Common commands

Run from `backend/` (install alembic via `requirements.txt`):

```bash
# Apply all migrations to the DB pointed at by DATABASE_URL
alembic upgrade head

# Create a new revision after changing app/models.py
alembic revision --autogenerate -m "describe change"

# Roll back one revision
alembic downgrade -1

# Target a specific DB without changing env (overrides settings.DATABASE_URL)
alembic -x db_url=sqlite+aiosqlite:///./local.db upgrade head
```

The DB URL is resolved in `env.py` from `app.config.settings.DATABASE_URL`
(normalized to the async `aiosqlite` driver), so migrations always hit the same
database the app uses.

## Adopting Alembic on an EXISTING database

A database created by `create_all` already has the `0001_initial_schema` tables.
Stamp that baseline (don't re-run it), then upgrade:

```bash
alembic stamp 0001_initial_schema
alembic upgrade head
```

## Revisions

- `0001_initial_schema` — baseline (mixes, pipeline_steps, brand_settings,
  ai_usage, notifications, app_settings).
- `0002_add_mixcloud_url` — adds `mixes.mixcloud_url` for the Mixcloud path.
