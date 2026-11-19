class CuadernaError(Exception):
    pass


class LeaseLostError(CuadernaError):
    pass


class JournalConflictError(CuadernaError):
    pass


class SchemaVersionError(CuadernaError):
    pass


class InvalidDefinitionError(CuadernaError):
    pass


class RunNotAvailableError(CuadernaError):
    pass


class RunFailedError(RunNotAvailableError):
    pass


class RunHeldError(RunNotAvailableError):
    pass


class StepCallError(CuadernaError):
    pass


class StepFailedError(CuadernaError):
    def __init__(self, step_key: str, error_type: str, message: str, traceback: str) -> None:
        super().__init__(f"step {step_key!r} failed with {error_type}: {message}")
        self.step_key = step_key
        self.error_type = error_type
        self.message = message
        self.traceback = traceback


class StepMismatchError(CuadernaError):
    pass


class RunMismatchError(CuadernaError):
    pass


class RunNotFoundError(CuadernaError):
    pass


class ReplayDivergedError(CuadernaError):
    pass


class TerminalError(CuadernaError):
    pass
