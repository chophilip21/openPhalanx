"""Documents and images for the agent's read, outline and grep.

Text formats (Markdown, JSON, CSV, HTML, plain text) are read as they are.
These are turned into text first, read-only:

* PDF: pypdf (installed with the engine), else poppler's pdftotext; page by page.
* Word (.docx), OpenDocument text (.odt), EPUB, Excel (.xlsx, .xlsm): zip
  files of XML, read with the standard library, so nothing more to install.
* RTF: a small converter here.

Videos and presentations (PowerPoint, Keynote, OpenDocument slides) are
refused, both ways.

Each part (page, sheet, chapter) starts with a "--- page 3 of 20 ---" line,
so line numbers work as in any file and an outline lists the parts.

Images (PNG, JPEG, GIF's first frame, WebP) and the pages of a PDF without
text are sent to the model as pictures, when the loaded model can see.
"""

from __future__ import annotations

import base64
import csv
import html.parser
import io
import posixpath
import re
import shutil
import subprocess
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

DOCUMENTS = {".pdf": "PDF", ".docx": "Word document", ".odt": "OpenDocument text", ".epub": "EPUB book",
             ".rtf": "RTF document", ".xlsx": "Excel workbook", ".xlsm": "Excel workbook"}
IMAGES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WebP"}
# Not supported (for now), and refused with a clear reason rather than
# "isn't a text file": extension -> what to call it.
VIDEOS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp")
PRESENTATIONS = (".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".odp", ".key")
UNSUPPORTED = {**{e: "video" for e in VIDEOS}, **{e: "presentation" for e in PRESENTATIONS}}
MAX_BYTES = 100_000_000  # a document larger than this isn't opened
MAX_UNZIPPED = 300_000_000  # nor one whose XML unpacks to more (a zip bomb)
IMAGE_SIDE = 1280  # longest side of a picture sent to the model, in pixels
IMAGE_QUALITY = 85


class DocumentError(Exception):
    """The file can't be turned into text (or a picture); the message says why."""


def kind(p: Path) -> str | None:
    """"PDF", "Word document", "PNG"…: what read turns into text or a picture."""
    ext = p.suffix.lower()
    return DOCUMENTS.get(ext) or IMAGES.get(ext)


def is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGES


def unsupported(p: Path) -> str | None:
    """"video" or "presentation" for a file the agent neither reads nor makes."""
    return UNSUPPORTED.get(p.suffix.lower())


def blocked(p: Path, vision: bool, model: str) -> str | None:
    """Why a file the user hands to the chat can't go to this model, or None:
    said to the user at once, before any request is made."""
    if what := unsupported(p):
        return f"{p.name} is a {what}, and {what}s aren't supported. Send text, documents or pictures instead."
    if is_image(p) and not vision:
        return (f"{p.name} is a picture, and the loaded model ({model}) can't see pictures. Load one that can "
                "(one with a vision encoder, such as Qwen3.5 or later, or Gemma 4) to use it.")
    return None


def part_line(name: str, i: int, n: int, label: str = "") -> str:
    return f"--- {name} {i} of {n}{': ' + label if label else ''} ---"


def join_parts(name: str, parts: list[tuple[str, str]]) -> str:
    """parts: (label, text). NFKC turns ligatures ("ﬁ") into letters, so grep finds them."""
    n = len(parts)
    return "\n".join(part_line(name, i, n, label) + "\n" + (unicodedata.normalize("NFKC", text).strip("\n") or f"(no text on this {name})")
                     for i, (label, text) in enumerate(parts, 1))


def text(p: Path) -> str:
    """The text of a document in DOCUMENTS."""
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise DocumentError(f"it is too big to read ({size // 1_000_000} MB)")
    ext = p.suffix.lower()
    if ext == ".pdf":
        pages = pdf_pages(p)
        if not any(t.strip() for t in pages):
            raise NoText(len(pages))
        return join_parts("page", [("", t) for t in pages])
    if ext == ".rtf":
        return rtf_text(p.read_bytes())
    try:
        z = zipfile.ZipFile(p)
    except (zipfile.BadZipFile, OSError) as e:
        raise DocumentError(f"it isn't a valid {DOCUMENTS[ext]} ({e})") from e
    with z:
        if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
            raise DocumentError("it unpacks to too much to read")
        try:
            if ext == ".docx":
                return docx_text(z)
            if ext == ".odt":
                return odt_text(z)
            if ext == ".epub":
                return epub_text(z)
            return xlsx_text(z)
        except (KeyError, ET.ParseError) as e:
            raise DocumentError(f"it isn't a valid {DOCUMENTS[ext]} ({type(e).__name__}: {e})") from e


class NoText(DocumentError):
    """A PDF without a text layer (a scan): its pages can still be seen as pictures."""

    def __init__(self, pages: int):
        super().__init__(f"it has {pages} page{'s' if pages != 1 else ''} but no text (a scan or images only)")
        self.pages = pages


# ---- PDF -------------------------------------------------------------------

def _pypdf():
    try:
        import logging

        import pypdf
        logging.getLogger("pypdf").setLevel(logging.ERROR)  # its font warnings went to the chat screen
        return pypdf
    except ImportError:
        return None


def _reader(p: Path):
    pypdf = _pypdf()
    reader = pypdf.PdfReader(str(p))
    if reader.is_encrypted:
        reader.decrypt("")  # many PDFs are "encrypted" with an empty password
    return reader


def pdf_pages(p: Path) -> list[str]:
    if _pypdf() is not None:
        try:
            return [page.extract_text() or "" for page in _reader(p).pages]
        except Exception as e:  # noqa: BLE001 - pypdf raises many kinds on a damaged or locked file
            raise DocumentError(f"it couldn't be read as a PDF ({type(e).__name__}: {str(e)[:200]})") from e
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(p), "-"], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as e:
            raise DocumentError("reading it took too long") from e
        if out.returncode != 0:
            raise DocumentError(f"it couldn't be read as a PDF: {out.stderr.decode(errors='replace').strip()[:200]}")
        pages = out.stdout.decode(errors="replace").split("\f")  # a form feed ends each page
        if pages and not pages[-1].strip():
            pages.pop()
        return pages
    raise DocumentError("reading PDFs needs the coding engine's PDF support; the user can add it with `oppx --update`")


def pdf_page_image(p: Path, page: int):
    """The largest picture on a PDF page (a scanned page is one picture), as a PIL image."""
    if _pypdf() is None:
        raise DocumentError("seeing a PDF's pages needs the coding engine's PDF support; the user can add it with `oppx --update`")
    try:
        pages = _reader(p).pages
        if not 1 <= page <= len(pages):
            raise DocumentError(f"it has {len(pages)} pages")
        images = [im.image for im in pages[page - 1].images]
    except DocumentError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"page {page}'s pictures couldn't be read ({type(e).__name__}: {str(e)[:200]})") from e
    images = [im for im in images if im is not None]
    if not images:
        raise DocumentError(f"page {page} has neither text nor a picture")
    return max(images, key=lambda im: im.width * im.height)


# ---- Word ------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_para(p: ET.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == W + "t":
            out.append(el.text or "")
        elif el.tag == W + "tab":
            out.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            out.append("\n")
    text = "".join(out)
    style = p.find(f"{W}pPr/{W}pStyle")
    name = (style.get(W + "val") if style is not None else "") or ""
    m = re.match(r"(?i)heading\s*(\d)", name)
    if m and text.strip():
        return "#" * int(m.group(1)) + " " + text
    if name.lower() == "title" and text.strip():
        return "# " + text
    if p.find(f"{W}pPr/{W}numPr") is not None and text.strip():
        return "- " + text
    return text


def _docx_block(el: ET.Element, out: list[str]):
    for child in el:
        if child.tag == W + "p":
            out.append(_docx_para(child))
        elif child.tag == W + "tbl":
            for row in child.iter(W + "tr"):
                cells = [" ".join(_docx_para(p) for p in tc.iter(W + "p")).strip() for tc in row.findall(W + "tc")]
                out.append("| " + " | ".join(c.replace("\n", " ") for c in cells) + " |")
            out.append("")
        elif child.tag == W + "sdt":  # content controls (tables of contents, form fields)
            content = child.find(W + "sdtContent")
            if content is not None:
                _docx_block(content, out)


def docx_text(z: zipfile.ZipFile) -> str:
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    out: list[str] = []
    if body is not None:
        _docx_block(body, out)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return unicodedata.normalize("NFKC", text) or "(no text in this document)"


# ---- OpenDocument ----------------------------------------------------------

T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def _odt_inline(el: ET.Element) -> str:
    out = [el.text or ""]
    for child in el:
        if child.tag == T + "s":
            out.append(" " * int(child.get(T + "c", "1")))
        elif child.tag == T + "tab":
            out.append("\t")
        elif child.tag == T + "line-break":
            out.append("\n")
        elif child.tag != T + "note":  # footnotes would land mid-sentence
            out.append(_odt_inline(child))
        out.append(child.tail or "")
    return "".join(out)


def _odt_block(el: ET.Element, out: list[str], depth: int = 0):
    for child in el:
        if child.tag == T + "h":
            out.append("#" * int(child.get(T + "outline-level", "1")) + " " + _odt_inline(child))
        elif child.tag == T + "p":
            out.append(("  " * (depth - 1) + "- " if depth else "") + _odt_inline(child))
        elif child.tag == T + "list":
            for item in child:
                _odt_block(item, out, depth + 1)
        elif child.tag == TABLE + "table":
            for row in child.iter(TABLE + "table-row"):
                cells = []
                for cell in row.findall(TABLE + "table-cell"):
                    parts: list[str] = []
                    _odt_block(cell, parts)
                    cells.append(" ".join(parts).strip())
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
        elif child.tag in (T + "section", T + "list-item", T + "list-header"):
            _odt_block(child, out, depth)


def odt_text(z: zipfile.ZipFile) -> str:
    root = ET.fromstring(z.read("content.xml"))
    office = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
    body = root.find(f"{office}body/{office}text")
    out: list[str] = []
    if body is not None:
        _odt_block(body, out)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return unicodedata.normalize("NFKC", text) or "(no text in this document)"


# ---- EPUB and HTML -----------------------------------------------------------

class _HtmlText(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "blockquote", "pre", "table", "ul", "ol", "dt", "dd", "hr", "figcaption"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.out.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def html_text(markup: str) -> str:
    p = _HtmlText()
    p.feed(markup)
    lines = [ln.strip() for ln in "".join(p.out).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def epub_text(z: zipfile.ZipFile) -> str:
    container = ET.fromstring(z.read("META-INF/container.xml"))
    rootfile = next(el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile"))
    opf = ET.fromstring(z.read(rootfile))
    base = posixpath.dirname(rootfile)
    items = {el.get("id"): el.get("href") for el in opf.iter() if el.tag.endswith("}item")}
    spine = [items[el.get("idref")] for el in opf.iter() if el.tag.endswith("}itemref") and el.get("idref") in items]
    parts = []
    for href in spine:
        name = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
        try:
            body = html_text(z.read(name).decode("utf-8", errors="replace"))
        except KeyError:
            continue
        if body:
            parts.append((posixpath.basename(name), body))
    if not parts:
        raise DocumentError("it has no chapters with text")
    return join_parts("chapter", parts)


# ---- Excel -------------------------------------------------------------------

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _column(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_text(z: zipfile.ZipFile) -> str:
    """Each sheet as CSV. Cells hold their stored values: a formula's last
    result, and dates as Excel's day numbers (formats aren't applied)."""
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(S + "si"):
            shared.append("".join(t.text or "" for t in si.iter(S + "t")))
    rels = {el.get("Id"): el.get("Target") for el in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheets = []
    for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(S + "sheet"):
        target = rels.get(sh.get(REL + "id"), "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(path)).iter(S + "row"):
            cells: dict[int, str] = {}
            for i, c in enumerate(row.findall(S + "c")):
                kind_, v = c.get("t"), c.find(S + "v")
                if kind_ == "s" and v is not None:
                    value = shared[int(v.text)]
                elif kind_ == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(S + "t"))
                elif kind_ == "b" and v is not None:
                    value = "TRUE" if v.text == "1" else "FALSE"
                else:
                    value = v.text if v is not None and v.text is not None else ""
                f = c.find(S + "f")
                if not value and f is not None and f.text:
                    value = "=" + f.text  # a formula never calculated (no stored result): show it
                cells[_column(c.get("r", "")) if c.get("r") else i] = value
            if cells:
                r = int(row.get("r", len(rows) + 1))
                rows += [[] for _ in range(r - 1 - len(rows))]  # empty rows keep their place
                rows.append([cells.get(j, "") for j in range(max(cells) + 1)])
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        width = max((len(r) for r in rows), default=0)
        sheets.append((f"{sh.get('name')} ({len(rows)} rows, {width} columns)", buf.getvalue() or "(empty)"))
    if not sheets:
        raise DocumentError("it has no sheets")
    return join_parts("sheet", sheets)


# ---- RTF ---------------------------------------------------------------------

RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "headerl", "headerr",
            "footerl", "footerr", "object", "themedata", "datastore", "latentstyles", "listtable",
            "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "mmathPr", "fldinst"}
RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|[\r\n]+|([^\\{}\r\n]+)")


def rtf_text(data: bytes) -> str:
    raw = data.decode("latin-1")
    if not raw.startswith("{\\rtf"):
        raise DocumentError("it isn't an RTF document")
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, pending_skip = False, 1, 0
    codepage = "cp1252"
    for m in RTF_TOKEN.finditer(raw):
        word, arg, hexa, sym, brace, plain = m.groups()
        if pending_skip and (hexa or plain):  # the fallback characters after \uN
            if hexa:
                pending_skip -= 1
                continue
            take = min(pending_skip, len(plain))
            plain, pending_skip = plain[take:], pending_skip - take
            if not plain:
                continue
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
        elif sym:
            if sym == "*":
                skip = True  # an "ignorable destination"
            elif not skip and sym in "\\{}":
                out.append(sym)
            elif not skip and sym == "~":
                out.append("\u00a0")
            elif not skip and sym in "-_":
                out.append("-" if sym == "_" else "")
        elif word:
            if word in RTF_SKIP:
                skip = True
            elif word == "uc":
                uc = int(arg or 1)
            elif word == "ansicpg" and arg:
                codepage = f"cp{arg}"
            elif skip:
                continue
            elif word == "u" and arg:
                out.append(chr(int(arg) % 65536))
                pending_skip = uc
            elif word in ("par", "line", "sect", "page", "row"):
                out.append("\n")
            elif word == "tab" or word == "cell":
                out.append("\t")
            elif word in ("emdash", "endash"):
                out.append("—" if word == "emdash" else "–")
            elif word in ("lquote", "rquote"):
                out.append("’" if word == "rquote" else "‘")
            elif word in ("ldblquote", "rdblquote"):
                out.append("”" if word == "rdblquote" else "“")
            elif word == "bullet":
                out.append("•")
        elif hexa and not skip:
            try:
                out.append(bytes([int(hexa, 16)]).decode(codepage))
            except (LookupError, UnicodeDecodeError):
                out.append(bytes([int(hexa, 16)]).decode("cp1252", errors="replace"))
        elif plain and not skip:
            out.append(plain)
    text = re.sub(r"(?<![ \t])[ \t]+\n", "\n", "".join(out))
    return unicodedata.normalize("NFKC", re.sub(r"\n{3,}", "\n\n", text).strip()) or "(no text in this document)"


# ---- pictures ------------------------------------------------------------------

def picture(source) -> tuple[str, int, int, int]:
    """A picture for the model: (data URL, width, height, estimated tokens).
    `source` is a path or a PIL image. The first frame of an animation; at
    most IMAGE_SIDE pixels on the longest side; JPEG (transparent parts on white)."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        if isinstance(source, (str, Path)):
            with Image.open(source) as f:
                f.seek(0)
                im = ImageOps.exif_transpose(f)  # phone photos are stored sideways
                im.load()
        else:
            im = ImageOps.exif_transpose(source)
    except (UnidentifiedImageError, OSError, EOFError) as e:
        raise DocumentError(f"it isn't a picture that can be opened ({e})") from e
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=IMAGE_QUALITY)
    # Vision models cut a picture into 28x28-pixel tiles, about one token each
    # (Qwen measured 400 for 800x500, 962 for 827x1170); round up for others.
    tokens = im.width * im.height // 784 + 16
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), im.width, im.height, tokens
