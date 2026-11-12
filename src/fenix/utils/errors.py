"""
    Domain exception for Fenix. Every one inherits from FenixError.

    The worker decides what to do with a failure by looking at the
    exception.

    type: transient errors are retried, permanent ones are kept for
    the DLQ. 
"""


class FenixError(Exception):
    """Base class of every error raised on purpuse by this project."""


class ConfigError(FenixError):
    """An environment variable is missing or the environment is unsafe."""


class StateError(FenixError):
    """A file inside data/ is corrupt or could not be read."""


class ValidationError(FenixError):
    """An event (or a single value) does not satisfy the contract."""


class BadBodyError(FenixError):
    """The HTTP body is not valid UTF-8 or not valid JSON."""


class SignatureError(FenixError):
    """The HMAC signature is missing, malformed, stale or wrong."""


class QueueUnavailableError(FenixError):
    """SQS did not answer or rejected an operation."""


class ConflictError(FenixError):
    """Same event_id arrived again with content different from the first."""


class TransientError(FenixError):
    """A recoverable failure: it is worth retrying later.

    Attributes:
        retry_after: Seconds the remote side asked us to wait (0 if none).
    """

    def __init__(self, message, retry_after=0):
        """Build the error.

        Args: 
            message: Human readable description (must not contain secrets).
            retry_after: Seconds to wait before retrying, if known. 

        Returns:
            None.

        Raises:
            None.
        """

        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(FenixError):
    """A failure that retrying will not fix."""


class NotifyTransientError(TransientError):
    """Timeout, network error, HTTP 429 or 5xx from the notification API."""


class NotifyPermanentError(PermanentError):
    """The notification setup was rejected (HTTP 400, 401, 403, 404 ...)."""


class SimulatedFailure(TransientError):
    """A failure provoked on propuse through ``simulate_failures``."""
