"""The llms.txt reference parser's regexes, ported for round-trip tests.

Source: llms_txt/core.py in https://github.com/AnswerDotAI/llms-txt (version 0.0.7,
Apache-2.0), the parser linked from https://llmstxt.org/. Ported rather than installed: the
package pulls in fastcore and a second HTTP client (httpx) for three regexes. The patterns
below are copied verbatim; only fastcore helpers (chunked, dict2obj) are replaced with
plain Python. If the upstream parser changes, update this file.
"""

import re

from llms_txt_lint import validate_llms_txt


def opt_re(s):
    return f"(?:{s})?"


def named_re(nm, pat):
    return f"(?P<{nm}>{pat})"


def parse_link(txt):
    title = named_re("title", r"[^\]]+")
    url = named_re("url", r"[^\)]+")
    desc = named_re("desc", r".*")
    desc_pat = opt_re(rf":\s*{desc}")
    pat = rf"-\s*\[{title}\]\({url}\){desc_pat}"
    return re.search(pat, txt).groupdict()


def _parse_links(links):
    return [parse_link(line) for line in re.split(r"\n+", links.strip()) if line.strip()]


def _parse_llms(txt):
    start, *rest = re.split(r"^##\s*(.*?$)", txt, flags=re.MULTILINE)
    sections = dict(zip(rest[::2], rest[1::2], strict=True))
    return start.strip(), {name: _parse_links(body) for name, body in sections.items()}


def parse_llms_file(txt):
    """Returns {"title", "summary", "info", "sections"}. Raises on files it can't parse."""
    start, sections = _parse_llms(txt)
    title = named_re("title", r".+?$")
    summ = named_re("summary", ".+?$")
    summ_pat = opt_re(rf"^>\s*{summ}$")
    info = named_re("info", ".*")
    pat = rf"^#\s*{title}\n+{summ_pat}\n+{info}"
    match = re.search(pat, start, re.MULTILINE | re.DOTALL)
    if match is None:  # upstream crashes here (None["sections"]); raise something readable
        raise ValueError("the reference parser can't parse this file's header")
    return match.groupdict() | {"sections": sections}


def assert_round_trips(llms_txt, summary, pages):
    """Our validator accepts the file, and the reference parser reads back what we wrote:
    the summary, and every page's URL and description, section by section."""
    assert validate_llms_txt(llms_txt) == []
    parsed = parse_llms_file(llms_txt)
    assert parsed["summary"] == summary
    expected = {}
    for page in pages:
        expected.setdefault(page.section, []).append((page.url, page.description))
    found = {
        name: [(link["url"], link["desc"]) for link in links]
        for name, links in parsed["sections"].items()
    }
    assert found == expected
