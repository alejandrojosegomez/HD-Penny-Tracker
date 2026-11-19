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


def detect_job() -> None:
    try:
        detect.run()
    except Exception as e:
        log.exception("detect job crashed")
        send(f"HD Penny Tracker: detect job crashed: {e}")


def community_then_detect() -> None:
    community_job()
    detect_job()


def main() -> None:
    sched = BlockingScheduler(timezone=settings.timezone)
    every = max(1, settings.scrape_every_hours)
    # 6:00 .. 22:00 local, every `every` hours
    hours = ",".join(str(h) for h in range(6, 23, every))
    # misfire_grace_time: if the PC was asleep at the scheduled time, still run when it wakes
    # (up to a few hours late) instead of silently skipping the job
    if settings.scrape_enabled:
        sched.add_job(scrape_then_detect, CronTrigger(hour=hours, minute=5), id="scrape",
                      max_instances=1, coalesce=True, misfire_grace_time=3 * 3600)
        community_fn, catch_up = community_job, scrape_after_community
    else:
        # manual prices: no Home Depot requests at all. Re-score on the same cadence so
        # "days at this price" and new community reports still raise scores over time.
        sched.add_job(detect_job, CronTrigger(hour=hours, minute=5), id="detect",
                      max_instances=1, coalesce=True, misfire_grace_time=3 * 3600)
        community_fn, catch_up = community_then_detect, community_then_detect
    sched.add_job(community_fn, CronTrigger(hour=settings.community_hour, minute=0), id="community",
                  max_instances=1, coalesce=True, misfire_grace_time=12 * 3600)
    # catch up at start-up when the community list hasn't run yet today
    if not community.ran_today():
        sched.add_job(catch_up, id="catch-up", max_instances=1)

    def _stop(*_):
        log.info("stopping")
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if settings.scrape_enabled:
        send(f"HD Penny Tracker worker started. Scrape at hours {hours} ({settings.timezone}), community list at {settings.community_hour}:00.")
    else:
        send(f"HD Penny Tracker prendido en modo manual: anota precios en el panel. "
             f"Lista de la comunidad a las {settings.community_hour}:00, puntajes a las {hours} ({settings.timezone}).")
    log.info("scheduler running: scrape=%s hours=%s community=%s:00",
             "on" if settings.scrape_enabled else "off (manual prices)", hours, settings.community_hour)
    sched.start()
