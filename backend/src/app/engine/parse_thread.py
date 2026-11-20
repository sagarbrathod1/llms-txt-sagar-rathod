"""The one thread the engine parses on: HTML pages and sitemaps.

Parsing is CPU work in pure Python (html.parser, ElementTree's tree building), so it holds the
GIL. Run on the event loop, it stalls every other coroutine, including the API's requests
when the worker is embedded. Parallel parse threads would only take turns on the GIL and
together starve the event loop. With one parse thread, the event loop gets the GIL back every
few milliseconds. A pool of its own also keeps parses from queueing ahead of DNS lookups,
which use asyncio's default thread pool.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

_PARSE_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine-parse")


async def run_in_parse_thread[T](function: Callable[..., T], *args) -> T:
    return await asyncio.get_running_loop().run_in_executor(_PARSE_THREAD, function, *args)
