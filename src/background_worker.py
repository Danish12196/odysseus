"""Optional worker process for durable background jobs.

The app runs the same loop in-process unless ``ODYSSEUS_INPROCESS_TASKS=0``.
Use this module when you want scheduling off the web process:

    python -m src.background_worker

Point it at the same ``DATABASE_URL`` and ``data/`` directory as the app, and
do not also leave the in-process scheduler enabled or both will fire routines.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("odysseus.background_worker")


async def _serve() -> None:
    from core.database import init_db
    init_db()
    from src.job_queue import recover_stale_locks
    from src.task_scheduler import TaskScheduler
    recover_stale_locks()
    scheduler = TaskScheduler(None)
    await scheduler.start()
    logger.info("Background worker running")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await scheduler.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
