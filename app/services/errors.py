"""Custom exceptions used across services."""


class EstudiaError(Exception):
    """Base error for user-facing failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotRegistered(EstudiaError):
    pass


class OwnershipError(EstudiaError):
    pass


class ValidationError(EstudiaError):
    pass


class DatabaseUnavailable(EstudiaError):
    pass
