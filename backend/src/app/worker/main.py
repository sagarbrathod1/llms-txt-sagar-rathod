"""Worker entrypoint. The same queue loop (app.worker.loop) runs in one of three modes:

- Dedicated: `python -m app.worker.main`. An always-on process that polls for work every few
  seconds. The setup at scale: run more of these to crawl more sites in parallel.
- Embedded: `EMBEDDED_WORKER=true` on the API. The API process runs the loop as a background
  task while it is up (see the lifespan in app.api.main). Used by the $0 deployment: when the
  API machine stops for lack of traffic, the loop stops too, so the database can suspend.
- Drain: `python -m app.worker.main --drain`. One pass: recover stale jobs, queue due
  rechecks, run jobs until none are queued, then exit: 0, or 1 if any tick errored. Run
  hourly by a scheduled machine, so monitoring keeps working while the API machine is stopped.

Any mix of modes can run at the same time; see app.worker.loop.
"""

import argparse
import asyncio
import logging
import signal
import sys

from app.config import configure_logging
from app.db import create_pool
from app.worker.loop import drain, run_forever

log = logging.getLogger(__name__)


async def run(drain_mode: bool) -> int:
    """Run the chosen mode; returns the process exit code."""
    # SIGTERM (deploys, scale-downs, a scheduled machine being stopped) cancels this task,
    # which is the loop's shutdown path (see loop.tick). Installed here rather than in the
    # loop because the embedded loop runs inside uvicorn, which handles signals itself.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    async with create_pool() as pool:
        if drain_mode:
            outcomes = await drain(pool)
            return 1 if outcomes["errored"] else 0
        await run_forever(pool)
        return 1  # not reached: run_forever only ends by raising (e.g. cancelled by SIGTERM)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker.main", description="Run the llms.txt job worker."
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help="run queued work and due rechecks until the queue is empty, then exit",
    )
    args = parser.parse_args(argv)
    configure_logging()
    try:
        exit_code = asyncio.run(run(args.drain))
    except (asyncio.CancelledError, KeyboardInterrupt):  # SIGTERM or Ctrl-C
        log.info("Worker stopped")
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
