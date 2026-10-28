"""Run the timed jobs (expiry, reminders, stuck-read report) every minute.

The first run happens at start. That is the "check on boot" step: anything
that should have happened while the laptop was off happens now.
Run it with:  order-desk scheduler
"""

from __future__ import annotations

import logging

from order_desk.config import Settings
from order_desk.db.connection import Database
from order_desk.services import maintenance
from order_desk.workers.lifecycle import StopFlag

log = logging.getLogger(__name__)


def main(settings: Settings, every_seconds: float = 60.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(settings.db_path)
    db.initialize()
    stop = StopFlag().install()
    log.info("scheduler started")
    while not stop.stopped:
        try:
            result = maintenance.run_once(db, settings)
            if result["expired"] or result["reminders"]:
                log.info("scheduler: %s", result)
        except Exception:
            log.exception("scheduler run failed; trying again next time")
        stop.sleep(every_seconds)
    log.info("scheduler stopped cleanly")
