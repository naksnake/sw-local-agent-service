"""Alembic environment: programmatic only.

`slas_api.db.migrate()` opens the connection (and holds the Postgres advisory lock) and
passes it in `config.attributes["connection"]`; there is no alembic.ini and no URL here, so
a password can never end up in a config file.
"""

from __future__ import annotations

from alembic import context

from slas_api.models import Base


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "Run migrations with `slas-api migrate`; it supplies the database connection."
        )
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
