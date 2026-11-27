"""Email HTML cleanup: an allowlist rebuild before the reader's sandboxed, script-free frame.

The frame (static/js/emailReader.js) is the real boundary: no scripts, no remote
loads. This is the second layer, so a frame regression still shows no active
content and fetches nothing: remote images are dropped and counted, `cid:`
images become data: URIs, and only http(s)/mailto links survive.
"""
import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote", "br", "caption", "center",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption",
    "figure", "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd",
    "li", "main", "mark", "nav", "ol", "p", "pre", "q", "s", "samp", "section", "small", "span", "strike",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt",
    "u", "ul", "var", "wbr", "style",
}
# Dropped together with everything inside them.
DROP_WITH_CONTENT = {"script", "noscript", "template", "iframe", "frame", "frameset", "object", "embed",
                     "applet", "svg", "math", "title", "textarea", "select", "button", "canvas", "audio", "video"}
VOID_TAGS = {"br", "col", "hr", "img", "wbr"}
ALLOWED_ATTRS = {
    "align", "alt", "bgcolor", "border", "cellpadding", "cellspacing", "class", "color", "colspan", "dir",
    "face", "headers", "height", "hspace", "lang", "rowspan", "scope", "size", "span", "start", "style",
    "summary", "title", "type", "valign", "vspace", "width", "href", "src", "datetime", "open", "reversed",
}
_SAFE_HREF = re.compile(r"^(https?:|mailto:)", re.I)
_DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp|bmp);base64,[a-z0-9+/=\s]+$", re.I)
# CSS that could load something or run something; url(data:...) is kept.
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(?!data:image/)[^)]*\)", re.I)
_CSS_BAD = re.compile(r"@import|expression\s*\(|behavior\s*:|-moz-binding|javascript:", re.I)
MAX_DATA_URI = 2_000_000


def _clean_css(css: str) -> str:
    return _CSS_BAD.sub("", _CSS_URL.sub("none", css))


class _Cleaner(HTMLParser):
    def __init__(self, inline_images: dict[str, str]):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.skip_tag = ""
        self.open: list[str] = []
        self.inline = inline_images
        self.remote_images = 0

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        if self.skip_depth:
            if tag == self.skip_tag and not closed:
                self.skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            if not closed and tag not in VOID_TAGS:
                self.skip_depth, self.skip_tag = 1, tag
            return
        if tag not in ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name not in ALLOWED_ATTRS or name.startswith("on"):
                continue
            if name == "style":
                value = _clean_css(value)
            elif name == "href":
                if tag != "a" or not _SAFE_HREF.match(value.strip()):
                    continue
            elif name == "src":
                if tag != "img":
                    continue
                value = self._image(value.strip())
                if not value:
                    continue
            kept.append((name, value))
        if tag == "img" and not any(name == "src" for name, _ in kept):
            return
        if tag == "a" and any(name == "href" for name, _ in kept):
            kept += [("target", "_blank"), ("rel", "noopener noreferrer")]
        text = "".join(f' {name}="{html.escape(value, quote=True)}"' for name, value in kept)
        self.out.append(f"<{tag}{text}>")
        if tag not in VOID_TAGS:
            if closed:
                self.out.append(f"</{tag}>")
            else:
                self.open.append(tag)

    def _image(self, src: str) -> str:
        if src.lower().startswith("cid:"):
            return self.inline.get(src[4:].strip("<>").lower(), "")
        if _DATA_IMAGE.match(src) and len(src) <= MAX_DATA_URI:
            return src
        if src:
            self.remote_images += 1
        return ""

    def handle_endtag(self, tag):
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if tag in self.open:
            while self.open:
                last = self.open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.open and self.open[-1] == "style":
            # Style text is not escaped by browsers, so it must not be able to close its own tag.
            self.out.append(_clean_css(data).replace("<", "\\3c "))
        else:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        return "".join(self.out) + "".join(f"</{tag}>" for tag in reversed(self.open))


def clean_html(raw: str, inline_images: dict[str, str] | None = None) -> tuple[str, int]:
    """(safe HTML, number of remote images left out). `inline_images` maps lower-case Content-IDs to data URIs."""
    cleaner = _Cleaner(inline_images or {})
    cleaner.feed(raw or "")
    cleaner.close()
    return cleaner.result(), cleaner.remote_images
