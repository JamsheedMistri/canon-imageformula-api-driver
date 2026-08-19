"""Exception hierarchy for the R10 driver."""

from __future__ import annotations

from typing import Optional


class R10Error(Exception):
    """Base class for all driver errors."""


class DeviceNotFoundError(R10Error):
    """No Canon R10 was found on the USB bus."""


class TransportError(R10Error):
    """A USB / Bulk-Only Transport level failure (framing, timeout, stall)."""


class NoPaperError(R10Error):
    """The feeder is empty (feed command returned medium-not-present)."""


class NotReadyError(R10Error):
    """The scanner is powered but not ready (warming up, cover open, no paper)."""


class ScsiError(R10Error):
    """A SCSI command returned CHECK CONDITION.

    Carries the decoded sense key / additional sense code so callers can
    branch on specific conditions (e.g. end-of-paper vs. jam).
    """

    def __init__(
        self,
        message: str,
        *,
        sense_key: Optional[int] = None,
        asc: Optional[int] = None,
        ascq: Optional[int] = None,
    ) -> None:
        self.sense_key = sense_key
        self.asc = asc
        self.ascq = ascq
        detail = ""
        if sense_key is not None:
            detail = f" [sense_key=0x{sense_key:02x} asc=0x{(asc or 0):02x} ascq=0x{(ascq or 0):02x}]"
        super().__init__(message + detail)
