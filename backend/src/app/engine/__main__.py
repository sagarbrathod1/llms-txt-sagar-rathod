"""Command line: `python -m app.engine https://example.com`.

Prints the llms.txt on stdout and warnings on stderr, so the output can be piped to a file.
"""

import asyncio
import sys

from app.engine import GenerationError, generate_llms_txt
from app.engine.urls import normalize_origin


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.engine <url>", file=sys.stderr)
        return 2
    try:
        # The API normalizes before calling the engine; the CLI does the same for typed input.
        result = asyncio.run(generate_llms_txt(normalize_origin(sys.argv[1])))
    except (ValueError, GenerationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(result.llms_txt, end="")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
