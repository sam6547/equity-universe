"""Outcome of fetching one dataset for one ticker."""
from __future__ import annotations

OK = "ok"            # fresh data
NO_DATA = "no_data"  # the source answered and has nothing for this ticker (not an outage)
FAILED = "failed"    # the source could not be reached / refused / returned something unreadable


class Result(object):
    __slots__ = ("status", "data", "error", "category")

    def __init__(self, status, data=None, error="", category=""):
        self.status = status
        self.data = data
        self.error = error
        self.category = category  # blocked | error | unavailable | format (for FAILED)

    @classmethod
    def ok(cls, data):
        return cls(OK, data)

    @classmethod
    def no_data(cls, reason):
        return cls(NO_DATA, None, reason)

    @classmethod
    def failed(cls, response):
        """From a net.Response that was not OK."""
        return cls(FAILED, None, response.describe(), response.kind)

    @classmethod
    def broken(cls, reason):
        """The source answered but in a layout we no longer understand."""
        return cls(FAILED, None, reason, "format")

    @property
    def is_ok(self):
        return self.status == OK
