from cuaderna._errors import SchemaVersionError

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            input_json TEXT NOT NULL,
            code_version TEXT,
            status TEXT NOT NULL,
            output_json TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        ) STRICT
        """,
    ),
)

LATEST_VERSION = len(_MIGRATIONS)


def pending_statements(current_version: int) -> tuple[str, ...]:
    if current_version > LATEST_VERSION:
        raise SchemaVersionError(
            f"journal schema version {current_version} is newer than this library supports "
            f"({LATEST_VERSION}); upgrade cuaderna to open it"
        )
    return tuple(
        statement for migration in _MIGRATIONS[current_version:] for statement in migration
    )
