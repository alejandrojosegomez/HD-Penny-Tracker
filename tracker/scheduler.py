"""Long-running worker: scrape every N hours (daytime), community refresh once a day,
detect right after every scrape. Heartbeat on Telegram at start so I know it is alive."""
from __future__ import annotations

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import community, detect, scraper
from .notify import send
from .settings import settings

log = logging.getLogger("tracker.scheduler")


def scrape_then_detect() -> None:
    try:
        res = scraper.scrape_all()
        if not res.get("stopped"):
            detect.run()
    except Exception as e:
        log.exception("scrape job crashed")
        send(f"HD Penny Tracker: scrape job crashed: {e}")


def community_job() -> None:
    try:
        community.refresh()
    except Exception as e:
        log.exception("community job crashed")
        send(f"HD Penny Tracker: community job crashed: {e}")


def scrape_after_community() -> None:
    community_job()
    scrape_then_detect()


def main() -> None:
    sched = BlockingScheduler(timezone=settings.timezone)
    every = max(1, settings.scrape_every_hours)
    # 6:00 .. 22:00 local, every `every` hours
    hours = ",".join(str(h) for h in range(6, 23, every))
    # misfire_grace_time: if the PC was asleep at the scheduled time, still run when it wakes
    # (up to a few hours late) instead of silently skipping the job
    sched.add_job(scrape_then_detect, CronTrigger(hour=hours, minute=5), id="scrape",
                  max_instances=1, coalesce=True, misfire_grace_time=3 * 3600)
    sched.add_job(community_job, CronTrigger(hour=settings.community_hour, minute=0), id="community",
                  max_instances=1, coalesce=True, misfire_grace_time=12 * 3600)
    # catch up at start-up: nothing to scrape until the community list has run once today
    if not community.ran_today():
        sched.add_job(scrape_after_community, id="catch-up", max_instances=1)

    def _stop(*_):
        log.info("stopping")
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    send(f"HD Penny Tracker worker started. Scrape at hours {hours} ({settings.timezone}), community list at {settings.community_hour}:00.")
    log.info("scheduler running: scrape hours=%s community=%s:00", hours, settings.community_hour)
    sched.start()
