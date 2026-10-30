"""The one exception every command raises for a refusal or a failure; `code` is a key of contract.ERRORS."""
from __future__ import annotations


class IngestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
