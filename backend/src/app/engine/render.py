"""Turn structured site data into llms.txt markdown.

Rules implemented, from the spec at https://llmstxt.org/ :
- The file is markdown. Sections appear in this order:
  1. an H1 with the site's name (the only required section);
  2. a blockquote with a short summary of the site;
  3. optional free-form detail with no headings;
  4. zero or more H2 sections, each a "file list": markdown list items made of a required
     link `[name](url)`, optionally followed by `: notes`.
- An H2 section named "Optional" holds secondary links an agent can skip when it needs a
  shorter context; by convention it comes last.

Also compatible with the spec's reference parser (llms_txt.parse_llms_file), which is
stricter than the prose: link titles can't contain "]" at all (escaping doesn't help), and
the summary is only recognized when more text follows it. Hence the one-line detail
section (3.), which also carries the one instruction the spec defines: Optional links can
be skipped.

When the homepage has no description we leave the blockquote out rather than invent one:
the spec only requires the H1, and a made-up "Acme website." tells an agent nothing.

This module is a pure function of its input, with no I/O, so the same data always gives
byte-identical output (the versioning rule compares output hashes).
"""

from app.engine.models import PageInfo
from app.engine.rank import OPTIONAL_SECTION


def escape_text(text: str) -> str:
    """One line of markdown-safe text: no newlines, and no "<" that could start raw HTML."""
    return " ".join(text.split()).replace("<", "\\<")


def escape_link_text(text: str) -> str:
    """Link text with brackets turned into parentheses.

    Backslash-escaping (\\[) is valid markdown, but the reference parser reads a title up to
    the first "]" regardless, so a bracket would cut the title short.
    """
    return escape_text(text).replace("[", "(").replace("]", ")")


def escape_url(url: str) -> str:
    """Percent-encode characters that would end the (url) part of a link early."""
    for char, encoded in ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        url = url.replace(char, encoded)
    return url


def render_llms_txt(
    site_name: str,
    summary: str | None,
    sections: list[tuple[str, list[PageInfo]]],
    home_url: str,
) -> str:
    lines = [f"# {escape_text(site_name)}", ""]
    if summary:
        lines += [f"> {escape_text(summary)}", ""]
    lines += [_detail_line(sections, home_url), ""]
    for name, pages in sections:
        lines += [f"## {escape_text(name)}", ""]
        for page in pages:
            item = f"- [{escape_link_text(page.title)}]({escape_url(page.url)})"
            if page.description:
                item += f": {escape_text(page.description)}"
            lines.append(item)
        lines.append("")
    # Exactly one trailing newline, however the last section ended.
    return "\n".join(lines).rstrip("\n") + "\n"


def _detail_line(sections: list[tuple[str, list[PageInfo]]], home_url: str) -> str:
    if not sections:
        return f"The homepage is {escape_url(home_url)}; no other pages could be listed."
    line = f"Key pages of {escape_url(home_url)}, grouped by section."
    if any(name == OPTIONAL_SECTION for name, _ in sections):
        line += " Links under Optional can be skipped when a shorter context is needed."
    return line
