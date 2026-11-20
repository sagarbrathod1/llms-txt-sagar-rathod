"""The file format itself is pinned by the golden test in test_pipeline.py; this covers the
input real pages throw at it."""

from reference_parser import assert_round_trips

from app.engine.models import PageInfo
from app.engine.render import render_llms_txt

HOME = "https://example.com/"


def test_escapes_characters_that_break_links():
    page = PageInfo(
        url="https://example.com/a (b)/c d",
        title="[Beta] <script> C:\\path\nnext",
        description="Line one\nline two <b>",
        section="Docs",
    )
    output = render_llms_txt("Acme\nInc", "Summary\nmore", [("Docs", [page])], HOME)
    assert (
        "- [(Beta) \\<script> C:\\path next](https://example.com/a%20%28b%29/c%20d): "
        "Line one line two \\<b>"
    ) in output
    assert output.startswith("# Acme Inc\n\n> Summary more\n")
    parsed_page = PageInfo(
        "https://example.com/a%20%28b%29/c%20d", "", "Line one line two \\<b>", "Docs"
    )
    assert_round_trips(output, "Summary more", [parsed_page])
