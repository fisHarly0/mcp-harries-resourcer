"""Decode static HTML using explicit declarations and a bounded meta scan."""
from email.message import Message
from html.parser import HTMLParser

import webencodings


def _lookup(label, *, meta=False):
    try:
        encoding = webencodings.lookup(label or "")
    except LookupError:
        return None
    # These legacy labels mean replacement in today's web encoding standard.
    if encoding is None or encoding.name in {"replacement", "iso-2022-kr", "hz-gb-2312"}:
        return None
    if meta and encoding.name in {"utf-16le", "utf-16be"}:
        return webencodings.lookup("utf-8")
    if meta and encoding.name == "x-user-defined":
        return webencodings.lookup("windows-1252")
    # Web GB2312/GBK labels use the GB18030 decoder, including four-byte text.
    if encoding.name == "gbk":
        return webencodings.lookup("gb18030")
    return encoding


def _charset(content_type):
    message = Message()
    message["content-type"] = content_type
    return message.get_param("charset", header="content-type")


class _MetaScan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.encoding = None
        self.raw_tag = None

    def handle_starttag(self, tag, attrs):
        if self.encoding is not None or self.raw_tag:
            return
        if tag in {"script", "style", "title", "textarea", "xmp", "iframe", "noembed", "noframes", "plaintext"}:
            self.raw_tag = tag
            # Keep example tags inside raw text, including legacy containers
            # and non-void tags incorrectly written with a self-closing />.
            self.set_cdata_mode(tag)
            return
        if tag != "meta":
            return
        attributes = {}
        for name, value in attrs:
            attributes.setdefault(name, value)  # First duplicate wins.
        label = attributes.get("charset")
        if label is None and (attributes.get("http-equiv") or "").lower() == "content-type":
            label = _charset(attributes.get("content") or "")
        if isinstance(label, str):
            self.encoding = _lookup(label, meta=True)

    def handle_endtag(self, tag):
        if self.raw_tag != "plaintext" and tag == self.raw_tag:
            self.raw_tag = None

    def handle_startendtag(self, tag, attrs):
        # HTML's non-void raw-text tags do not close just because they use />.
        self.handle_starttag(tag, attrs)


def decode_html(content: bytes, content_type: str = "") -> tuple[str, dict]:
    """Return text plus the selected codec, declaration source and error flag.

    The first 1024 bytes are scanned for meta declarations. No statistical
    guessing or second decoding is attempted when an explicit declaration lies.
    """
    encoding, source = None, "default"
    for marker, label in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16le"), (b"\xfe\xff", "utf-16be")):
        if content.startswith(marker):
            encoding, source = _lookup(label), "bom"
            content = content[len(marker):]
            break
    if encoding is None:
        label = _charset(content_type)
        encoding = _lookup(label) if isinstance(label, str) else None
        if encoding is not None:
            source = "http"
    if encoding is None:
        scan = _MetaScan()
        # Latin-1 maps byte offsets one-to-one; non-ASCII label bytes stay invalid.
        try:
            scan.feed(content[:1024].decode("latin-1"))
        except AssertionError:
            # Malformed marked sections must not crash the extraction worker.
            pass
        if scan.encoding is not None:
            encoding, source = scan.encoding, "meta"
    encoding = encoding or webencodings.lookup("utf-8")
    try:
        text = encoding.codec_info.decode(content, "strict")[0]
        had_errors = False
    except UnicodeDecodeError:
        text = encoding.codec_info.decode(content, "replace")[0]
        had_errors = True
    return text, {"encoding": encoding.name, "source": source, "had_errors": had_errors}
