BEGIN TRANSACTION;
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
        ;
INSERT INTO "runs" VALUES('run-1','double_twice','{"number":3}',NULL,'completed','12',NULL,'2026-11-19T18:56:15.616964+00:00','2026-11-19T18:56:15.622365+00:00',NULL,1,NULL);
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
        ;
INSERT INTO "step_attempts" VALUES(1,'run-1','flaky_double#1','flaky_double',1,'error','b10faebab83cfeb34bfb9e0fdff88054b5662a4c0b6a01c0fee4e38885dc8a97','{"number":3}',NULL,'{"type":"ConnectionError","message":"try again","traceback":"Traceback (most recent call last):\nConnectionError: try again\n","terminal":false}','2026-11-19T18:56:15.619011+00:00','2026-11-19T18:56:15.619964+00:00');
INSERT INTO "step_attempts" VALUES(2,'run-1','flaky_double#1','flaky_double',2,'ok','b10faebab83cfeb34bfb9e0fdff88054b5662a4c0b6a01c0fee4e38885dc8a97','{"number":3}','6',NULL,'2026-11-19T18:56:15.620799+00:00','2026-11-19T18:56:15.620834+00:00');
INSERT INTO "step_attempts" VALUES(3,'run-1','flaky_double#2','flaky_double',1,'ok','eaf20cb977394ac089d7f74f8244c1b37d0f2c64d034c3b24726ee9fd78f7630','{"number":6}','12',NULL,'2026-11-19T18:56:15.621638+00:00','2026-11-19T18:56:15.621654+00:00');
COMMIT;
PRAGMA user_version = 1;
