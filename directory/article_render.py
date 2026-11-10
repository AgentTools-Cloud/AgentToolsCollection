from __future__ import annotations

import io
import re
import warnings
from html.parser import HTMLParser
from urllib.parse import urlsplit

import nh3
from markdown_it import MarkdownIt
from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MEDIA_PATH = re.compile(r"/articles/media/([0-9a-f]{32})\Z")
ALLOWED_TAGS = {
    "p", "br", "hr", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "a", "strong", "em", "blockquote", "pre", "code", "table", "thead", "tbody",
    "tr", "th", "td", "img", "del",
}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "img": {"src", "alt", "title"},
                      "code": {"class"}, **{heading: {"id"} for heading in ("h2", "h3", "h4", "h5", "h6")}}


def _clean(html: str, private: bool = False, media_prefix: str = "/admin", sponsored: bool = False) -> str:
    def attributes(tag: str, attribute: str, value: str) -> str | None:
        if tag == "img" and attribute == "src":
            match = MEDIA_PATH.fullmatch(value)
            return (media_prefix + value if private else value) if match else None
        if tag == "code" and attribute == "class":
            return value if re.fullmatch(r"language-[a-zA-Z0-9_+-]{1,40}", value) else None
        return value

    return nh3.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES,
                     attribute_filter=attributes, url_schemes={"https", "http", "mailto"},
                     link_rel="noopener noreferrer sponsored" if sponsored else "noopener noreferrer")


def render_markdown(markdown: str, *, sponsored: bool = False) -> dict:
    parser = MarkdownIt("default", {"html": False})
    environment = {}
    tokens = parser.parse(markdown, environment)
    contents, media_ids = [], set()
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            level = max(2, int(token.tag[1:]))
            token.tag = "h" + str(level)
            if index + 2 < len(tokens) and tokens[index + 2].type == "heading_close":
                tokens[index + 2].tag = token.tag
            anchor = "section-" + str(len(contents) + 1)
            token.attrSet("id", anchor)
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            label = "".join(child.content for child in (inline.children or [])
                            if child.type in ("text", "code_inline")) if inline else ""
            contents.append({"id": anchor, "title": label, "level": level})
        for child in token.children or []:
            if child.type == "image":
                match = MEDIA_PATH.fullmatch(child.attrGet("src") or "")
                if not match:
                    raise ValueError("invalid_image_url")
                media_ids.add(match.group(1))
    html = parser.renderer.render(tokens, parser.options, environment)
    return {"html": _clean(html, sponsored=sponsored), "toc": contents, "media_ids": sorted(media_ids)}


def private_html(html: str, *, author_mode: bool = False, api_mode: bool = False, sponsored: bool = False) -> str:
    prefix = "/api/v1" if api_mode else "/account" if author_mode else "/admin"
    return _clean(html, private=True, media_prefix=prefix, sponsored=sponsored)


def outbound_link_count(html: str, site_url: str) -> int:
    site = urlsplit(site_url)
    site_origin = (site.scheme, site.hostname, site.port or (443 if site.scheme == "https" else 80))

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.count = 0

        def handle_starttag(self, tag, attributes):
            if tag != "a":
                return
            href = dict(attributes).get("href", "")
            try:
                address = urlsplit(href)
                if not address.netloc:
                    return
                scheme = address.scheme or site.scheme
                origin = (scheme, address.hostname, address.port or (443 if scheme == "https" else 80))
            except ValueError:
                self.count += 1
                return
            if origin != site_origin:
                self.count += 1

    parser = LinkParser()
    parser.feed(html)
    return parser.count


def prepare_image(content: bytes) -> dict:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("invalid_image_size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format
                if image_format not in ("PNG", "JPEG", "WEBP") or getattr(image, "is_animated", False):
                    raise ValueError("invalid_image_type")
                width, height = image.size
                if width > 8000 or height > 8000 or width * height > 16_000_000:
                    raise ValueError("invalid_image_dimensions")
                image.load()
                alpha = "A" in image.getbands() or "transparency" in image.info
                mode = "RGBA" if alpha and image_format != "JPEG" else "RGB"
                clean = Image.frombytes(mode, image.size, image.convert(mode).tobytes())
                output = io.BytesIO()
                clean.save(output, format=image_format, **({"quality": 90} if image_format != "PNG" else {}))
                serialized = output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("invalid_image") from None
    if len(serialized) > MAX_IMAGE_BYTES:
        raise ValueError("invalid_image_size")
    return {"content": serialized, "mime": {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format],
            "width": width, "height": height}