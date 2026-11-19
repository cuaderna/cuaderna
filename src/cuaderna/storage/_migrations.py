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
            finished_at TEXT,
            lease_owner TEXT,
            lease_token INTEGER NOT NULL DEFAULT 0,
            lease_expires_at REAL
        ) STRICT
        """,
        """
        CREATE TABLE step_attempts (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs (run_id),
            step_key TEXT NOT NULL,
            name TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            status TEXT NOT NULL,
            args_hash TEXT NOT NULL,
            args_json TEXT NOT NULL,
            result_json TEXT,
            error_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            UNIQUE (run_id, step_key, attempt)
        ) STRICT
        """,
    ),
    (
        "ALTER TABLE step_attempts ADD COLUMN lease_token INTEGER",
        "ALTER TABLE step_attempts ADD COLUMN annotations_json TEXT",
    ),
    (
        """
        CREATE TABLE approval_requests (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs (run_id),
            step_key TEXT NOT NULL,
            name TEXT NOT NULL,
            args_hash TEXT NOT NULL,
            args_json TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            UNIQUE (run_id, step_key, args_hash)
        ) STRICT
        """,
        """
        CREATE TABLE approval_answers (
            request_id INTEGER PRIMARY KEY REFERENCES approval_requests (id),
            approved INTEGER NOT NULL,
            answered_by TEXT NOT NULL,
            note TEXT,
            answered_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TRIGGER step_attempts_no_update BEFORE UPDATE ON step_attempts
        BEGIN
            SELECT RAISE(ABORT, 'step_attempts is append-only');
        END
        """,
        """
        CREATE TRIGGER step_attempts_no_delete BEFORE DELETE ON step_attempts
        BEGIN
            SELECT RAISE(ABORT, 'step_attempts is append-only');
        END
        """,
        """
        CREATE TRIGGER approval_requests_no_update BEFORE UPDATE ON approval_requests
        BEGIN
            SELECT RAISE(ABORT, 'approval_requests is append-only');
        END
        """,
        """
        CREATE TRIGGER approval_requests_no_delete BEFORE DELETE ON approval_requests
        BEGIN
            SELECT RAISE(ABORT, 'approval_requests is append-only');
        END
        """,
        """
        CREATE TRIGGER approval_answers_no_update BEFORE UPDATE ON approval_answers
        BEGIN
            SELECT RAISE(ABORT, 'approval_answers is append-only');
        END
        """,
        """
        CREATE TRIGGER approval_answers_no_delete BEFORE DELETE ON approval_answers
        BEGIN
            SELECT RAISE(ABORT, 'approval_answers is append-only');
        END
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
