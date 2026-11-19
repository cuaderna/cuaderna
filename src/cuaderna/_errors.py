class CuadernaError(Exception):
    pass


class LeaseLostError(CuadernaError):
    pass


class JournalConflictError(CuadernaError):
    pass


class SchemaVersionError(CuadernaError):
    pass
