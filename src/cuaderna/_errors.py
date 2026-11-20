from decimal import Decimal


class CuadernaError(Exception):
    pass


class LeaseLostError(CuadernaError):
    pass


class JournalConflictError(CuadernaError):
    pass


class JournalBusyError(CuadernaError):
    pass


class SchemaVersionError(CuadernaError):
    pass


class ApprovalNotPendingError(CuadernaError):
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


class PermissionDeniedError(StepFailedError):
    def __init__(self, step_key: str, message: str, reason: str, rules: tuple[str, ...]) -> None:
        super().__init__(step_key, "PermissionDeniedError", message, "")
        Exception.__init__(self, message)
        self.reason = reason
        self.rules = rules


class StepMismatchError(CuadernaError):
    pass


class RunMismatchError(CuadernaError):
    pass


class RunNotFoundError(CuadernaError):
    pass


class ReplayDivergedError(CuadernaError):
    pass


class PriceTableError(CuadernaError):
    pass


class PolicyError(CuadernaError):
    pass


class BudgetExceededError(CuadernaError):
    def __init__(self, run_id: str, budget_usd: Decimal, spent_usd: Decimal) -> None:
        super().__init__(
            f"run {run_id!r} has spent {spent_usd} USD and its budget is {budget_usd} USD"
        )
        self.run_id = run_id
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd


class CostUnknownError(CuadernaError):
    def __init__(self, run_id: str, step_key: str, attempt: int, reason: str | None) -> None:
        super().__init__(
            f"run {run_id!r} has a budget and the cost of step {step_key!r}, "
            f"attempt {attempt}, is unknown: {reason}"
        )
        self.run_id = run_id
        self.step_key = step_key
        self.attempt = attempt
        self.reason = reason


class ApprovalRequiredError(CuadernaError):
    def __init__(self, run_id: str, step_key: str, name: str) -> None:
        super().__init__(f"run {run_id!r} needs an approval to run step {step_key!r}")
        self.run_id = run_id
        self.step_key = step_key
        self.name = name


class RunFileError(CuadernaError):
    pass


class JournalTamperedError(CuadernaError):
    def __init__(
        self, message: str, run_id: str, step_key: str | None, attempt: int | None
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.step_key = step_key
        self.attempt = attempt


class TerminalError(CuadernaError):
    pass
