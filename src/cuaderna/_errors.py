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
