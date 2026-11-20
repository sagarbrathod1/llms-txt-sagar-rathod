"""A small linter for the llms.txt structure defined at https://llmstxt.org/ .

A test oracle, not product code: it checks what our renderer must get right, so tests can
assert every output is valid (alongside the reference parser's round trip):
- the first non-blank line is the H1, and there is only one H1;
- a blockquote, if present, comes directly after the H1 (before other content);
- free-form content before the first H2 contains no headings;
- every non-blank line inside an H2 section is a list item made of a well-formed link,
  optionally followed by ":" and notes.

Requiring absolute http(s) link URLs is our rule rather than the spec's: an llms.txt is read
out of context, where a relative link can't be followed.
"""

import re
from urllib.parse import urlsplit

# "- [text](url)" with optional ": notes". Text may contain backslash-escaped characters;
# the URL may not contain spaces or parentheses (we percent-encode those when rendering).
LINK_ITEM = re.compile(r"^[-*+] \[(?P<text>(?:[^\[\]\\]|\\.)+)\]\((?P<url>[^\s()]+)\)(?P<rest>.*)$")
HEADING = re.compile(r"^(#{1,6})\s")


def validate_llms_txt(text: str) -> list[str]:
    """Return a list of human-readable violations; an empty list means the file is valid."""
    lines = text.removeprefix("\ufeff").split("\n")  # the spec allows a leading BOM
    errors: list[str] = []
    seen_h1 = False
    seen_other_content = False  # anything after the H1 other than blank lines
    in_section = False  # inside an H2 file list

    for number, line in enumerate(lines, start=1):
        # A multi-line blockquote is consecutive ">" lines; a blank line ends it.
        continues_quote = number > 1 and lines[number - 2].startswith(">")
        if not line.strip():
            continue
        heading = HEADING.match(line)
        level = len(heading.group(1)) if heading else 0

        if not seen_h1:
            if level != 1:
                errors.append(f"line {number}: the file must start with an H1 title")
            seen_h1 = True  # report the missing H1 once, then keep checking the rest
            continue

        if level == 1:
            errors.append(f"line {number}: only one H1 is allowed")
        elif level == 2:
            in_section = True
        elif level:
            errors.append(f"line {number}: only H2 headings may follow the H1")
        elif line.startswith(">") and (in_section or seen_other_content and not continues_quote):
            errors.append(f"line {number}: the blockquote summary must come right after the H1")
        elif in_section:
            errors.extend(_check_item(number, line))
        seen_other_content = True

    if not seen_h1:
        errors.append("the file is empty; it needs at least an H1 title")
    return errors


def _check_item(number: int, line: str) -> list[str]:
    match = LINK_ITEM.match(line)
    if not match:
        return [f"line {number}: H2 sections may only contain '- [name](url)' list items"]
    errors = []
    # The spec allows the notes with or without a space after the colon.
    if match.group("rest") and not match.group("rest").startswith(":"):
        errors.append(f"line {number}: notes after a link must start with ':'")
    url = urlsplit(match.group("url"))
    if url.scheme not in ("http", "https") or not url.netloc:
        errors.append(f"line {number}: link URL must be an absolute http(s) URL")
    return errors
