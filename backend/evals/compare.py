"""Compare our llms.txt with the one a site wrote by hand: which pages overlap, which don't.

Usage (from backend/, after run_eval.py):
    uv run python evals/compare.py                  # every corpus site with its own llms.txt
    uv run python evals/compare.py svelte hono      # sites whose URL contains these
    uv run python evals/compare.py --out evals/out-main

Downloads each hand-written file (the corpus's "llms" URL) into evals/ref/, which is
git-ignored: those files are the sites' own content, not ours to commit. Pages are matched
by host and path, ignoring "www.", a trailing slash, and .md/.html suffixes, since hand-written
files often link to markdown twins of the pages we link to.
"""

import argparse
import collections
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from run_eval import HERE, load_corpus, slug

from app.engine.fetch import USER_AGENT

REF = HERE / "ref"
LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
SUFFIX = re.compile(r"(\.html)?\.md$|\.html$|/index$|/$")


def page_keys(llms_txt: str) -> set[str]:
    keys = set()
    for url in LINK.findall(llms_txt):
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        keys.add(host + (SUFFIX.sub("", parts.path) or "/"))
    return keys


def top_folders(keys: set[str]) -> list[tuple[str, int]]:
    """The most common host/first-folder prefixes: where the differences are."""
    return collections.Counter("/".join(key.split("/")[:2]) for key in keys).most_common(8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("filters", nargs="*", help="only sites whose URL contains one of these")
    parser.add_argument("--out", type=Path, default=HERE / "out", help="run_eval output folder")
    args = parser.parse_args()
    REF.mkdir(exist_ok=True)
    with httpx2.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20
    ) as client:
        for site in load_corpus(args.filters):
            name = slug(site["url"])
            ours_file = args.out / f"{name}.txt"
            if not site.get("llms") or not ours_file.exists():
                continue
            response = client.get(site["llms"])
            if response.status_code != 200:
                print(f"== {name}: couldn't fetch {site['llms']} (HTTP {response.status_code})")
                continue
            (REF / f"{name}.txt").write_text(response.text)
            ours, theirs = page_keys(ours_file.read_text()), page_keys(response.text)
            print(f"== {name}: ours={len(ours)} theirs={len(theirs)} both={len(ours & theirs)}")
            print("   only theirs:", top_folders(theirs - ours))
            print("   only ours:  ", top_folders(ours - theirs))


if __name__ == "__main__":
    main()
