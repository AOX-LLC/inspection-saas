"""Why a job could not finish."""


class JobError(Exception):
    """A job that cannot succeed as it is.

    `code` is stored on the job row and the photo, so it is a fixed word such as
    `storage_error`, never text from the photo, the database or an exception.
    `retryable` says whether another attempt could change the outcome.
    """

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
