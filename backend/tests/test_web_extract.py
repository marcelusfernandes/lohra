"""Tests for html_to_text — strips boilerplate, collapses, caps length."""

from lohra.web.extract import html_to_text


def test_extracts_visible_text():
    html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
    assert html_to_text(html) == "Title Hello world"


def test_drops_script_and_style():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>evil()</script><p>kept</p></body></html>"
    )
    assert html_to_text(html) == "kept"


def test_drops_nav_and_footer():
    html = "<nav>menu home about</nav><p>article body</p><footer>copyright</footer>"
    assert html_to_text(html) == "article body"


def test_collapses_whitespace():
    html = "<p>a   b\n\n  c</p>"
    assert html_to_text(html) == "a b c"


def test_caps_length():
    html = "<p>" + ("word " * 10_000) + "</p>"
    assert len(html_to_text(html, max_chars=50)) == 50


def test_decodes_entities():
    assert html_to_text("<p>Tom &amp; Jerry &lt;3</p>") == "Tom & Jerry <3"
