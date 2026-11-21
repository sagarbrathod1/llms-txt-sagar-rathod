import time

import pytest
from fakesite import page

from app.engine.extract import MAX_DESCRIPTION_CHARS, extract_page

ORIGIN = "https://example.com"
URL = "https://example.com/docs/"


def extract(html, **kwargs):
    return extract_page(html.encode(), URL, ORIGIN, **kwargs)


def test_title_prefers_title_tag_then_og_title_then_h1():
    og = '<meta property="og:title" content="OG title">'
    assert extract(page("  Real\n title ", head=og)).title == "Real title"
    assert extract(page(head=og, body="<h1>Heading</h1>")).title == "OG title"
    assert extract(page(body="<h1>The <b>heading</b></h1>")).title == "The heading"
    assert extract(page()).title is None


def test_description_prefers_meta_then_og():
    og = '<meta property="og:description" content="From OG">'
    assert extract(page(description="From meta", head=og)).description == "From meta"
    assert extract(page(head=og)).description == "From OG"
    assert extract(page()).description is None


def test_long_description_is_cut_at_a_word():
    description = extract(page(description="word " * 200)).description
    assert len(description) <= MAX_DESCRIPTION_CHARS + 1
    assert description.endswith("word…")


def test_noindex_from_meta_or_header():
    assert extract(page(head='<meta name="robots" content="NOINDEX, follow">')).noindex
    assert extract(page(), x_robots_tag="noindex").noindex
    assert not extract(page(head='<meta name="robots" content="index, follow">')).noindex


def test_canonical_is_normalized_and_same_origin_only():
    same = extract(page(head='<link rel="canonical" href="/docs/?utm_source=x">'))
    assert same.canonical == "https://example.com/docs/"
    other = extract(page(head='<link rel="canonical" href="https://mirror.com/docs/">'))
    assert other.canonical is None


def test_links_are_same_origin_normalized_and_deduped_in_order():
    links = ["b", "/a#top", "/a", "https://other.com/", "mailto:x@example.com", "/b.pdf"]
    assert extract(page(links=links)).links == [
        "https://example.com/docs/b",
        "https://example.com/a",
    ]


def test_uses_meta_charset_when_header_has_none():
    html = '<html><head><meta charset="iso-8859-1"><title>Caf\xe9</title></head></html>'
    assert extract_page(html.encode("iso-8859-1"), URL, ORIGIN).title == "Café"


def test_meta_and_rel_matching_ignores_case():
    head = (
        '<META NAME="Description" CONTENT="Upper">'
        '<meta name="ROBOTS" content="NoIndex">'
        '<link rel="Canonical" href="/docs/">'
    )
    extracted = extract(page(head=head))
    assert extracted.description == "Upper"
    assert extracted.noindex
    assert extracted.canonical == "https://example.com/docs/"


def test_title_ignores_inline_svg_titles():
    html = (
        "<html><body><svg><title>Close icon</title></svg>"
        "<head><title>Real title</title></head></body></html>"
    )
    assert extract(html).title == "Real title"


def test_description_falls_back_to_twitter_and_first_real_paragraph_is_kept():
    twitter = '<meta name="twitter:description" content="From Twitter card">'
    assert extract(page(head=twitter)).description == "From Twitter card"

    body = (
        "<nav><p>Navigation text that is definitely long enough to count as a paragraph.</p></nav>"
        '<div class="cookie-banner"><p>We use cookies to improve your experience on this site.</p>'
        "</div><main><p>Short.</p>"
        "<p>Acme builds widgets that last for decades and ships them worldwide.</p></main>"
    )
    expected = "Acme builds widgets that last for decades and ships them worldwide."
    assert extract(page(body=body)).paragraphs[0] == expected


def test_paragraph_fallback_prefers_main_content():
    body = (
        "<p>A long introduction paragraph outside of the main element of the page.</p>"
        "<article><p>The article's own first paragraph, which is what describes it.</p></article>"
    )
    expected = "The article's own first paragraph, which is what describes it."
    assert extract(page(body=body)).paragraphs[0] == expected


def test_hreflang_alternates():
    head = (
        '<link rel="alternate" hreflang="de" href="/de/">'
        '<link rel="Alternate" hreflang="x-default" href="/">'
        '<link rel="alternate" type="application/rss+xml" href="/feed">'
    )
    assert extract(page(head=head)).alternates == ["https://example.com/de/"]


def test_javascript_notices_are_not_descriptions():
    body = (
        '<div id="nojs"><p>Notice: This page displays a fallback because scripts did not run.</p>'
        "</div><noscript><p>Please enable JavaScript to use every feature of this website.</p>"
        "</noscript><p>Python is used in many application domains, from web to science.</p>"
    )
    expected = "Python is used in many application domains, from web to science."
    assert extract(page(body=body)).paragraphs[0] == expected


def test_no_js_class_on_html_does_not_hide_the_content():
    html = (
        '<html class="no-js"><head><title>T</title></head><body>'
        "<p>Python is used in many application domains, from web to science.</p></body></html>"
    )
    expected = "Python is used in many application domains, from web to science."
    assert extract(html).paragraphs == [expected]


def test_image_map_links_are_followed():
    body = '<map name="nav"><area shape="rect" href="/essays/great-work.html"></map>'
    assert extract(page(body=body)).links == ["https://example.com/essays/great-work.html"]


def test_meta_description_html_is_stripped():
    head = '<meta name="description" content="&lt;p&gt;Breaking &lt;b&gt;news&lt;/b&gt;&lt;/p&gt;">'
    assert extract(page(head=head)).description == "Breaking news"


def test_fallback_paragraphs_must_read_as_descriptions():
    body = (
        "<main><p>Posted Nov 19 by Jane Doe in the community updates section of the blog.</p>"
        "<p>Download Node.js v22.11.0 for your platform and get started building today.</p>"
        "<p>Node.js is a free, open source JavaScript runtime for servers and tools</p>"
        "<p>Node.js is a free, open source JavaScript runtime for building servers.</p></main>"
    )
    # Dated, versioned, and unfinished sentences are skipped.
    expected = ["Node.js is a free, open source JavaScript runtime for building servers."]
    assert extract(page(body=body)).paragraphs == expected


def test_alternative_site_names_from_application_name_and_jsonld():
    head = (
        '<meta name="application-name" content="Acme App">'
        '<script type="application/ld+json">{"@graph": [{"@type": "Organization", '
        '"name": "Acme Inc"}, {"@type": ["WebSite"], "name": "Acme"}]}</script>'
        '<script type="application/ld+json">{broken</script>'
    )
    assert extract(page(head=head)).alt_site_names == ["Acme App", "Acme", "Acme Inc"]


def test_teasers_for_other_pages_are_not_the_pages_own_paragraphs():
    """Regression (BBC, simonwillison.net): a section front's description was the teaser of
    today's top story, so it changed every day."""
    teaser = "Today's top story is about something that happened this very morning."
    own = "This section covers the business news that matters to you, every day."
    wrapped_card = f'<a href="/story-1"><h3>Story</h3><p>{teaser}</p></a>'
    heading_card = f'<div><h2><a href="/2026/11/story-2">Story</a></h2><p>{teaser}</p></div>'
    assert extract(page(body=f"<main>{wrapped_card}</main>")).paragraphs == []
    assert extract(page(body=f"<main>{heading_card}</main>")).paragraphs == []
    # A heading that links to the page itself (a permalink, a "¶" anchor) isn't a card.
    permalink = f'<div><h1>Docs <a href="#docs">¶</a> <a href="/docs/">#</a></h1><p>{own}</p></div>'
    assert extract(page(body=f"<main>{permalink}</main>")).paragraphs == [own]


def test_nearly_all_link_text_marks_a_hub_and_skips_tag_clouds():
    intro = "<p>This section covers the business news that matters to you, every day.</p>"
    headlines = "".join(
        f'<li><a href="/story-{n}">A headline about story number {n}</a></li>' for n in range(9)
    )
    hub = extract(page(body=f"<main>{intro}<ul>{headlines}</ul></main>"))
    assert hub.is_hub  # so its paragraphs never become its description
    article = extract(page(body=f'<main>{intro * 3}<a href="/related">Related</a></main>'))
    assert not article.is_hub
    # simonwillison.net's homepage summary was its tag cloud.
    tags = ["python", "webassembly", "llm-release", "security", "browsers"] * 4
    cloud = " ".join(f'<a href="/tags/{tag}/">{tag}</a> {n}' for n, tag in enumerate(tags))
    assert extract(page(body=f"<main><p>On {cloud}.</p></main>")).paragraphs == []
    # Prose with inline links is still prose (jvns.ca: 0.6 link text).
    prose = (
        'I just learned that <a href="/a">strace has a --stack-traces option</a> from '
        '<a href="/b">this nice blog post</a> today.'
    )
    assert extract(page(body=f"<main><p>{prose}</p></main>")).paragraphs


INTRO = "This guide explains how the widget pipeline works and how to configure it."


@pytest.mark.parametrize(
    "body",
    [
        # A linked "Read next" heading after the intro.
        f'<h1>Guide</h1><p>{INTRO}</p><h2><a href="/next">Read next</a></h2><p>{INTRO} 2</p>',
        # Linked cards after the intro, in the same wrapper.
        f"<div><p>{INTRO}</p>"
        + "".join(
            f'<div><h2><a href="/c{n}">Card {n}</a></h2><p>Teaser {n}.</p></div>' for n in range(3)
        )
        + "</div>",
        # A doc whose later section heading links to the API reference.
        f'<div><p>{INTRO}</p><h2><a href="/api">API</a></h2><p>Call it like this.</p></div>',
    ],
)
def test_a_linked_heading_after_the_intro_does_not_make_it_a_teaser(body):
    assert extract(page(body=f"<main>{body}</main>")).paragraphs[:1] == [INTRO]


def test_a_card_with_its_headline_below_the_blurb_is_a_teaser():
    # The Verge's cards put the linked headline under the text.
    card = f'<div><div><p>{INTRO}</p></div><h2><a href="/story">Story</a></h2></div>'
    assert extract(page(body=f"<main>{card}{card}</main>")).paragraphs == []


def test_a_page_of_thousands_of_paragraphs_parses_quickly():
    """Regression: teaser checks scanned the whole wrapper for every paragraph, so a 160 KB
    page of 6,000 short paragraphs took 36 s on the parse thread, which nothing can stop."""
    lines = "".join(f"<p>Short line number {n}.</p>" for n in range(6000))
    started = time.perf_counter()
    extract(page(body=f"<main><div>{lines}</div></main>"))
    assert time.perf_counter() - started < 2  # generous: about 0.1 s in practice
