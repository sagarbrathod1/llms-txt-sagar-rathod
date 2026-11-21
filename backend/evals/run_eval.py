"""Run the engine over a corpus of real websites and record each output with its metrics.

A manual eval, not a test: live sites change and rate-limit, so it can't gate merges and
nothing in CI runs it. Run it before and after an engine change and compare the outputs.

Usage (from backend/):
    uv run python evals/run_eval.py                         # whole corpus, ~6 minutes
    uv run python evals/run_eval.py svelte hono             # sites whose URL contains these
    uv run python evals/run_eval.py --out evals/out-main    # e.g. a baseline from main

Sites run one at a time, with the engine's own limits (the same entry point as the worker),
so the crawl stays polite and timings aren't skewed by each other.

Writes, per site, to the output folder (default evals/out/, git-ignored):
- <slug>.txt: the generated llms.txt
- <slug>.json: timing, page counts, warnings or the error, the spec lint and reference-parser
  results, and automatic metrics (links, descriptions, duplicate titles, sections, size)

The automatic parts catch crashes, failures, non-conformance and obvious regressions. Quality
is a human read; score each output 0, 0.5 or 1 on seven points:
h1 (the site's real name), summary (says what the site is), key_pages (the pages a newcomer
needs are there), no_junk (no logins, bot checks, archives or random deep pages), sections
(sensible groups, none huge), descriptions (page-specific, stable from day to day),
coverage (the site's main areas are all represented).

corpus.json lists each site with its category, why it was picked, and the URL of its own
hand-written llms.txt if it has one (see compare.py).
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The spec lint and the reference parser are test helpers; the eval reuses them as-is.
sys.path.insert(0, str(HERE.parent / "tests" / "engine"))

from llms_txt_lint import validate_llms_txt  # noqa: E402
from reference_parser import parse_llms_file  # noqa: E402

from app.engine import GenerationError, generate_llms_txt  # noqa: E402
from app.engine.urls import normalize_origin  # noqa: E402

LINK_ITEM = re.compile(r"^- \[(.*?)\]\((.*?)\)(?::\s*(.*))?$")


def slug(url: str) -> str:
    """ "fastht.ml/docs" -> "fastht-ml-docs": a file name per corpus entry."""
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")


def load_corpus(filters: list[str]) -> list[dict]:
    corpus = json.loads((HERE / "corpus.json").read_text())
    if filters:
        corpus = [site for site in corpus if any(f in site["url"] for f in filters)]
    return corpus


def metrics(llms_txt: str) -> dict:
    """Counts a reviewer would otherwise tally by hand."""
    lines = llms_txt.splitlines()
    items, sections, section = [], [], None
    for line in lines:
        if line.startswith("## "):
            section = line[3:]
            sections.append(section)
        elif match := LINK_ITEM.match(line):
            title, url, description = match.groups()
            items.append({"section": section, "title": title, "url": url, "desc": description})
    descriptions = [item["desc"] for item in items if item["desc"]]
    return {
        "h1": lines[0][2:] if lines else None,
        "summary": next((line[2:] for line in lines if line.startswith("> ")), None),
        "sections": sections,
        "links": len(items),
        "with_desc": len(descriptions),
        "dup_desc": len(descriptions) - len(set(descriptions)),
        "dup_titles": len(items) - len({item["title"].lower() for item in items}),
        "optional": sum(item["section"] == "Optional" for item in items),
        "largest_section": max(
            (sum(item["section"] == name for item in items) for name in sections), default=0
        ),
        "bytes": len(llms_txt.encode()),
    }


def conformance(llms_txt: str, pages_listed: int) -> dict:
    """The spec lint's problems, and whether the reference parser reads every link back."""
    try:
        parsed = parse_llms_file(llms_txt)
        found = sum(len(links) for links in parsed["sections"].values())
        parser = "ok" if found == pages_listed else f"link count {found} != {pages_listed}"
    except Exception as error:  # noqa: BLE001 - a parser crash is a finding to record
        parser = f"error: {error!r}"
    return {"lint": validate_llms_txt(llms_txt), "ref_parser": parser}


async def run_site(site: dict, out: Path) -> dict:
    name = slug(site["url"])
    record = {**site, "origin": normalize_origin(site["url"]), "slug": name}
    started = time.monotonic()
    try:
        result = await generate_llms_txt(record["origin"])
    except GenerationError as error:
        record.update(ok=False, error=str(error))
    except Exception as error:  # noqa: BLE001 - a crash is a finding, not a harness failure
        record.update(ok=False, error=f"CRASH {type(error).__name__}: {error}")
    else:
        (out / f"{name}.txt").write_text(result.llms_txt)
        record.update(
            ok=True,
            pages_listed=len(result.pages),
            pages_attempted=result.pages_attempted,
            pages_failed=result.pages_failed,
            warnings=result.warnings,
            **conformance(result.llms_txt, len(result.pages)),
            metrics=metrics(result.llms_txt),
        )
    record["seconds"] = round(time.monotonic() - started, 1)
    (out / f"{name}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    return record


def print_line(record: dict) -> None:
    if record["ok"]:
        detail = (
            f"listed={record['pages_listed']} attempted={record['pages_attempted']} "
            f"lint={len(record['lint'])} parser={record['ref_parser']}"
        )
    else:
        detail = record["error"]
    print(f"{record['slug']:28} {record['seconds']:6.1f}s  {detail}", flush=True)


def print_summary(records: list[dict]) -> None:
    done = [record for record in records if record["ok"]]
    conformant = [r for r in done if not r["lint"] and r["ref_parser"] == "ok"]
    links = sum(r["metrics"]["links"] for r in done)
    described = sum(r["metrics"]["with_desc"] for r in done)
    print(
        f"\n{len(done)}/{len(records)} succeeded, {len(conformant)}/{len(done)} conformant, "
        f"{links} links, {described / max(links, 1):.0%} with a description, "
        f"{sum(r['metrics']['dup_titles'] for r in done)} duplicate titles"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("filters", nargs="*", help="only sites whose URL contains one of these")
    parser.add_argument("--out", type=Path, default=HERE / "out", help="output folder")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    records = []
    for site in load_corpus(args.filters):
        records.append(await run_site(site, args.out))
        print_line(records[-1])
    print_summary(records)


if __name__ == "__main__":
    asyncio.run(main())
