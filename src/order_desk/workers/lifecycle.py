"""Clean stop on SIGTERM / SIGINT for the background workers.

What happens when the process gets SIGTERM (for example `kill`, a laptop
shutdown, or Ctrl-C):
1. The handler only sets a flag. It does not stop work in the middle.
2. The worker finishes the item it is on, then leaves its loop.
3. The worker gives back any rows it leased but did not send.
If the process dies without a signal (power cut, `kill -9`), the leases end
by time and the next start takes those rows again. Both paths lead to the
same safe state. See docs/06-reliability.md.
"""

from __future__ import annotations

import logging
import signal
import threading

log = logging.getLogger(__name__)


class StopFlag:
    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> "StopFlag":
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle)
        return self

    def _handle(self, signum, _frame) -> None:
        log.info("got %s: finishing the current item, then stopping", signal.Signals(signum).name)
        self._event.set()

    def set(self) -> None:
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def sleep(self, seconds: float) -> None:
        """Sleep, but wake at once when a stop is requested."""
        self._event.wait(seconds)
