import pytest
from llms_txt_lint import validate_llms_txt

GOOD = """\
# Project

> Summary line one
> and line two.

Some notes about the files.

- a plain list before the H2s is fine

## Docs

- [Intro](https://example.com/intro): Notes here
* [Escaped \\[x\\]](https://example.com/x)

## Optional

- [Extra](https://example.com/extra)
"""


def test_valid_file():
    assert validate_llms_txt(GOOD) == []
    assert validate_llms_txt("# A\n## Docs\n- [x](https://example.com):no space\n") == []
    assert validate_llms_txt("\ufeff# Just a title\n") == []  # BOM allowed


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "empty"),
        ("Intro\n# Title\n", "start with an H1"),
        ("## Docs\n", "start with an H1"),
        ("# A\n# B\n", "only one H1"),
        ("# A\n### Sub\n", "only H2"),
        ("# A\n\nnotes\n\n> late summary\n", "right after the H1"),
        ("# A\n## Docs\n> quote\n", "right after the H1"),
        ("# A\n## Docs\nplain text\n", "list items"),
        ("# A\n## Docs\n- [Broken(https://example.com)\n", "list items"),
        ("# A\n## Docs\n- [x](https://example.com/a b)\n", "list items"),
        ("# A\n## Docs\n- [x](https://example.com) notes\n", "start with ':'"),
        ("# A\n## Docs\n- [x](/relative)\n", "absolute"),
        ("# A\n## Docs\n- [x](javascript:alert(1))\n", "list items"),
    ],
)
def test_invalid_files(text, error):
    errors = validate_llms_txt(text)
    assert any(error in message for message in errors), errors
