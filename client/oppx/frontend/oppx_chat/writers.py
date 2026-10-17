"""Documents and pictures the agent creates.

A model writes text, never a binary file, so `create` asks it for text in a
format it knows and this module builds the file:

* Word (.docx), OpenDocument (.odt), RTF, EPUB and PDF: from Markdown
  (headings, paragraphs, lists, tables, code, **bold**, *italic*, `code`).
* Excel (.xlsx): from CSV; several sheets as "--- sheet Name ---" lines,
  the same layout `read` shows. Numbers become numbers, "=…" a formula.
* PNG, JPEG, WebP, GIF: from SVG, drawn with resvg.

Word, OpenDocument, RTF, EPUB and Excel need only the standard library; PDF
needs fpdf2 and pictures resvg-py (both installed with the engine).
"""

from __future__ import annotations

import csv
import html
import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from .documents import DOCUMENTS, IMAGES


class WriteError(Exception):
    """The file couldn't be built; the message says why."""


SOURCE = {ext: "Markdown" for ext in (".docx", ".odt", ".rtf", ".epub", ".pdf")}
SOURCE.update({".xlsx": "CSV", ".xlsm": "CSV"})
SOURCE.update({ext: "SVG" for ext in IMAGES})


def writable(p: Path) -> bool:
    return p.suffix.lower() in SOURCE


def hint(p: Path) -> str:
    """What to write for this file, for the create prompt."""
    ext = p.suffix.lower()
    name = DOCUMENTS.get(ext) or f"{IMAGES[ext]} picture"
    if SOURCE[ext] == "Markdown":
        return (f" {p.name} is a {name}: write its content as Markdown (# headings, paragraphs, - and 1. lists, "
                "| tables |, ``` code, **bold**, *italic*), and it is converted.")
    if SOURCE[ext] == "CSV":
        return (f" {p.name} is an {name}: write it as CSV, and it is converted. For several sheets, put a line "
                "--- sheet Name --- before each. Numbers stay numbers; a cell starting with = is a formula.")
    return (f" {p.name} is a {name}: write it as one SVG document (with width and height set), "
            "and it is drawn into the picture.")


def write(p: Path, source: str) -> str:
    """Builds `p` from `source`; returns what was made, for the result."""
    ext = p.suffix.lower()
    if ext in IMAGES:
        data, what = picture(source, ext)
    elif ext in (".xlsx", ".xlsm"):
        data, what = xlsx(source)
    else:
        blocks = parse(source)
        if not blocks:
            raise WriteError("there is no content to write")
        build = {".docx": docx, ".odt": odt, ".rtf": rtf, ".epub": epub, ".pdf": pdf}[ext]
        data, what = build(blocks, title_of(blocks, p))
    p.write_bytes(data)
    return what


# ---- Markdown -----------------------------------------------------------------

@dataclass
class Block:
    kind: str  # heading, para, item, table, code, rule
    text: str = ""
    level: int = 0  # heading level; list depth
    ordered: bool = False
    number: int = 0
    rows: list = field(default_factory=list)


def parse(md: str) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0

    def flush():
        if para:
            blocks.append(Block("para", " ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(Block("code", "\n".join(code)))
        elif m := re.match(r"(#{1,6})\s+(.*)", s):
            flush()
            blocks.append(Block("heading", m.group(2).rstrip("# ").strip(), level=len(m.group(1))))
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s):
            flush()
            blocks.append(Block("rule"))
        elif m := re.match(r"(\s*)([-*+]|(\d+)[.)])\s+(.*)", ln):
            flush()
            blocks.append(Block("item", m.group(4), level=len(m.group(1).expandtabs(4)) // 2,
                                ordered=m.group(3) is not None, number=int(m.group(3) or 0)))
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            blocks.append(Block("table", rows=rows))
            continue
        elif not s:
            flush()
        else:
            para.append(ln)
        i += 1
    flush()
    return blocks


# Each span's content excludes its own delimiter, so an unclosed one isn't
# rescanned to the end from every opener (scripts/check_regexes.py).
INLINE = re.compile(r"\*\*((?:[^*]|\*(?!\*))+?)\*\*|__((?:[^_]|_(?!_))+?)__|\*([^*]+?)\*|(?<!\w)_([^_]+?)_(?!\w)"
                    r"|`([^`]+)`|\[([^\[\]]+)\]\(([^)\s]+)\)")


def runs(text: str) -> list[tuple[str, str]]:
    """Inline Markdown as (text, style): style is "", "b", "i" or "code"."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], ""))
        if m.group(1) or m.group(2):
            out.append((m.group(1) or m.group(2), "b"))
        elif m.group(3) or m.group(4):
            out.append((m.group(3) or m.group(4), "i"))
        elif m.group(5):
            out.append((m.group(5), "code"))
        else:
            out.append((f"{m.group(6)} ({m.group(7)})", ""))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def plain(text: str) -> str:
    return "".join(t for t, _ in runs(text))


def title_of(blocks: list[Block], p: Path) -> str:
    return next((plain(b.text) for b in blocks if b.kind == "heading"), p.stem.replace("_", " ").replace("-", " "))


def numbered(blocks: list[Block]):
    """Blocks with a list item's marker worked out ("•", "3.")."""
    counters: dict[int, int] = {}
    for b in blocks:
        if b.kind != "item":
            counters.clear()
            yield b, ""
            continue
        for depth in [d for d in counters if d > b.level]:
            del counters[depth]
        if b.ordered:
            counters[b.level] = b.number if b.level not in counters and b.number else counters.get(b.level, 0) + 1
            yield b, f"{counters[b.level]}."
        else:
            yield b, "•"


def to_html(blocks: list[Block], fpdf: bool = False) -> str:
    def inline(text: str) -> str:
        tag = {"b": "b", "i": "i", "code": "code"}
        return "".join(f"<{tag[s]}>{html.escape(t)}</{tag[s]}>" if s else html.escape(t) for t, s in runs(text))

    out = []
    for b, marker in numbered(blocks):
        if b.kind == "heading":
            out.append(f"<h{b.level}>{inline(b.text)}</h{b.level}>")
        elif b.kind == "para":
            out.append(f"<p>{inline(b.text)}</p>")
        elif b.kind == "item":
            pad = "&nbsp;" * 4 * b.level
            out.append(f"<p>{pad}{marker} {inline(b.text)}</p>")
        elif b.kind == "code":
            out.append(f"<pre>{html.escape(b.text)}</pre>")
        elif b.kind == "rule":
            out.append("<hr/>")
        elif b.kind == "table" and b.rows:
            head, *body = b.rows
            cells = lambda row, t: "".join(f"<{t}>{inline(c)}</{t}>" for c in row)  # noqa: E731
            attrs = ' border="1" width="100%"' if fpdf else ""
            out.append(f"<table{attrs}><thead><tr>{cells(head, 'th')}</tr></thead><tbody>"
                       + "".join(f"<tr>{cells(r + [''] * (len(head) - len(r)), 'td')}</tr>" for r in body)
                       + "</tbody></table>")
    return "\n".join(out)


def _zip(files: list[tuple[str, str | bytes]], stored_first: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, data) in enumerate(files):
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED if stored_first and i == 0 else zipfile.ZIP_DEFLATED)
    return buf.getvalue()


# ---- Word ---------------------------------------------------------------------

def _w_runs(text: str, extra: str = "") -> str:
    out = []
    for t, s in runs(text):
        props = extra + {"b": "<w:b/>", "i": "<w:i/>", "code": '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>', "": ""}[s]
        out.append(f'<w:r>{"<w:rPr>" + props + "</w:rPr>" if props else ""}<w:t xml:space="preserve">{escape(t)}</w:t></w:r>')
    return "".join(out)


def docx(blocks: list[Block], title: str) -> tuple[bytes, str]:
    body = []
    for b, marker in numbered(blocks):
        if b.kind == "heading":
            body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{b.level}"/></w:pPr>{_w_runs(b.text)}</w:p>')
        elif b.kind == "para":
            body.append(f"<w:p>{_w_runs(b.text)}</w:p>")
        elif b.kind == "item":
            left = 360 * (b.level + 1)
            body.append(f'<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:ind w:left="{left + 360}" w:hanging="360"/></w:pPr>'
                        f'<w:r><w:t xml:space="preserve">{marker}\t</w:t></w:r>{_w_runs(b.text)}</w:p>')
        elif b.kind == "code":
            mono = '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr>'
            for ln in b.text.split("\n"):
                body.append(f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r>{mono}<w:t xml:space="preserve">{escape(ln)}</w:t></w:r></w:p>')
        elif b.kind == "rule":
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/></w:pBdr></w:pPr></w:p>')
        elif b.kind == "table" and b.rows:
            width = max(len(r) for r in b.rows)
            rows = []
            for n, r in enumerate(b.rows):
                cells = "".join(f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr><w:p>{_w_runs(c, "<w:b/>" if n == 0 else "")}</w:p></w:tc>'
                                for c in r + [""] * (width - len(r)))
                rows.append(f"<w:tr>{cells}</w:tr>")
            border = "".join(f'<w:{s} w:val="single" w:sz="4" w:color="999999"/>' for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
            body.append(f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>'
                        f'<w:tblGrid>{"<w:gridCol/>" * width}</w:tblGrid>{"".join(rows)}</w:tbl><w:p/>')
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {ns}><w:body>{"".join(body)}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
                "</w:body></w:document>")
    sizes = {1: 36, 2: 30, 3: 26, 4: 24, 5: 22, 6: 22}
    headings = "".join(f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/><w:basedOn w:val="Normal"/>'
                       f'<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
                       f'<w:rPr><w:b/><w:sz w:val="{s}"/></w:rPr></w:style>' for n, s in sizes.items())
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles {ns}>'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
              '<w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
              f'{headings}<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:spacing w:after="40"/></w:pPr></w:style></w:styles>')
    files = [
        ("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
         '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
         '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>'),
        ("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
         '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>'),
        ("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'),
        ("docProps/core.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
         f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{escape(title)}</dc:title></cp:coreProperties>'),
        ("word/document.xml", document),
        ("word/styles.xml", styles),
    ]
    return _zip(files), f"Word document, {len(blocks)} blocks"


# ---- OpenDocument ---------------------------------------------------------------

def _o_runs(text: str) -> str:
    style = {"b": "Bold", "i": "Italic", "code": "Code"}
    return "".join(f'<text:span text:style-name="{style[s]}">{_o_text(t)}</text:span>' if s else _o_text(t) for t, s in runs(text))


def _o_text(t: str) -> str:
    # Runs of spaces and tabs are markup in ODF, not characters.
    t = escape(t).replace("\t", "<text:tab/>")
    return re.sub(r"  +", lambda m: " " + f'<text:s text:c="{len(m.group(0)) - 1}"/>', t)


def odt(blocks: list[Block], title: str) -> tuple[bytes, str]:
    body = []
    for b, marker in numbered(blocks):
        if b.kind == "heading":
            body.append(f'<text:h text:style-name="H{b.level}" text:outline-level="{b.level}">{_o_runs(b.text)}</text:h>')
        elif b.kind == "para":
            body.append(f'<text:p text:style-name="Body">{_o_runs(b.text)}</text:p>')
        elif b.kind == "item":
            body.append(f'<text:p text:style-name="L{min(b.level, 4)}">{marker}<text:tab/>{_o_runs(b.text)}</text:p>')
        elif b.kind == "code":
            body += [f'<text:p text:style-name="Pre">{_o_text(ln)}</text:p>' for ln in b.text.split("\n")]
        elif b.kind == "rule":
            body.append('<text:p text:style-name="Rule"/>')
        elif b.kind == "table" and b.rows:
            width = max(len(r) for r in b.rows)
            rows = "".join("<table:table-row>" + "".join(
                f'<table:table-cell table:style-name="Cell" office:value-type="string"><text:p text:style-name="{"Th" if n == 0 else "Body"}">{_o_runs(c)}</text:p></table:table-cell>'
                for c in r + [""] * (width - len(r))) + "</table:table-row>" for n, r in enumerate(b.rows))
            body.append(f'<table:table table:name="Table{len(body)}"><table:table-column table:number-columns-repeated="{width}"/>{rows}</table:table>')
    ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
          'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/" '
          'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"')
    sizes = {1: "18pt", 2: "15pt", 3: "13pt", 4: "12pt", 5: "11pt", 6: "11pt"}
    styles = "".join(f'<style:style style:name="H{n}" style:family="paragraph"><style:paragraph-properties fo:margin-top="0.4cm" fo:margin-bottom="0.15cm"/>'
                     f'<style:text-properties fo:font-size="{s}" fo:font-weight="bold"/></style:style>' for n, s in sizes.items())
    styles += "".join(f'<style:style style:name="L{n}" style:family="paragraph"><style:paragraph-properties fo:margin-left="{0.6 * (n + 1):.1f}cm" '
                      'fo:text-indent="-0.5cm" fo:margin-bottom="0.05cm"/></style:style>' for n in range(5))
    styles += ('<style:style style:name="Body" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.2cm"/></style:style>'
               '<style:style style:name="Th" style:family="paragraph"><style:text-properties fo:font-weight="bold"/></style:style>'
               '<style:style style:name="Pre" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0cm"/>'
               '<style:text-properties style:font-name="Liberation Mono" fo:font-family="monospace" fo:font-size="9pt"/></style:style>'
               '<style:style style:name="Rule" style:family="paragraph"><style:paragraph-properties fo:border-bottom="0.5pt solid #999999"/></style:style>'
               '<style:style style:name="Cell" style:family="table-cell"><style:table-cell-properties fo:border="0.5pt solid #999999" fo:padding="0.1cm"/></style:style>'
               '<style:style style:name="Bold" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>'
               '<style:style style:name="Italic" style:family="text"><style:text-properties fo:font-style="italic"/></style:style>'
               '<style:style style:name="Code" style:family="text"><style:text-properties fo:font-family="monospace"/></style:style>')
    content = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-content {ns} office:version="1.3">'
               f'<office:automatic-styles>{styles}</office:automatic-styles><office:body><office:text>{"".join(body)}</office:text></office:body></office:document-content>')
    meta = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-meta {ns} office:version="1.3"><office:meta><dc:title>{escape(title)}</dc:title></office:meta></office:document-meta>')
    manifest = ('<?xml version="1.0" encoding="UTF-8"?>\n<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
                '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/></manifest:manifest>')
    files = [("mimetype", "application/vnd.oasis.opendocument.text"), ("META-INF/manifest.xml", manifest),
             ("content.xml", content), ("meta.xml", meta)]
    return _zip(files, stored_first=True), f"OpenDocument text, {len(blocks)} blocks"


# ---- RTF -------------------------------------------------------------------------

def _r_text(t: str) -> str:
    out = []
    for ch in t:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ch == "\t":
            out.append("\\tab ")
        elif ord(ch) < 128:
            out.append(ch)
        else:
            n = ord(ch)
            for unit in ([n] if n < 0x10000 else [0xD800 + ((n - 0x10000) >> 10), 0xDC00 + ((n - 0x10000) & 0x3FF)]):
                out.append(f"\\u{unit if unit < 32768 else unit - 65536}?")
    return "".join(out)


def _r_runs(text: str) -> str:
    style = {"b": "\\b ", "i": "\\i ", "code": "\\f1 "}
    return "".join("{" + style[s] + _r_text(t) + "}" if s else _r_text(t) for t, s in runs(text))


def rtf(blocks: list[Block], title: str) -> tuple[bytes, str]:
    sizes = {1: 36, 2: 30, 3: 26, 4: 24, 5: 22, 6: 22}
    out = ["{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss Helvetica;}{\\f1\\fmodern Courier New;}}",
           f"{{\\info{{\\title {_r_text(title)}}}}}\\fs22\\sa120"]
    for b, marker in numbered(blocks):
        if b.kind == "heading":
            out.append(f"{{\\pard\\sb240\\sa80\\keepn\\b\\fs{sizes[b.level]} {_r_runs(b.text)}\\par}}")
        elif b.kind == "para":
            out.append(f"{{\\pard {_r_runs(b.text)}\\par}}")
        elif b.kind == "item":
            left = 360 * (b.level + 2)
            out.append(f"{{\\pard\\li{left}\\fi-360\\sa40 {_r_text(marker)}\\tab {_r_runs(b.text)}\\par}}")
        elif b.kind == "code":
            out.append("{\\pard\\sa0\\f1\\fs18 " + "\\line ".join(_r_text(ln) for ln in b.text.split("\n")) + "\\par}")
        elif b.kind == "rule":
            out.append("{\\pard\\brdrb\\brdrs\\brdrw10\\brsp20 \\par}")
        elif b.kind == "table" and b.rows:
            width = max(len(r) for r in b.rows)
            cell_w = 9000 // max(width, 1)
            for n, r in enumerate(b.rows):
                defs = "".join(f"\\clbrdrt\\brdrs\\clbrdrl\\brdrs\\clbrdrb\\brdrs\\clbrdrr\\brdrs\\cellx{cell_w * (k + 1)}" for k in range(width))
                cells = "".join(("{\\b " if n == 0 else "{") + _r_runs(c) + "}\\cell " for c in r + [""] * (width - len(r)))
                out.append(f"\\trowd\\trgaph108{defs}\\pard\\intbl {cells}\\row")
            out.append("\\pard\\par")
    out.append("}")
    return "\n".join(out).encode("ascii"), f"RTF document, {len(blocks)} blocks"


# ---- EPUB ------------------------------------------------------------------------

def epub(blocks: list[Block], title: str) -> tuple[bytes, str]:
    # A chapter per top heading (#), or one chapter.
    chapters: list[list[Block]] = []
    for b in blocks:
        if not chapters or (b.kind == "heading" and b.level == 1 and chapters[-1]):
            chapters.append([])
        chapters[-1].append(b)
    book_id = f"urn:uuid:{uuid.uuid4()}"
    names = [f"chapter{i}.xhtml" for i in range(1, len(chapters) + 1)]
    heads = [title_of(c, Path(f"Chapter {i}")) for i, c in enumerate(chapters, 1)]
    page = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><meta charset="utf-8"/><title>{t}</title></head><body>{b}</body></html>')
    files: list[tuple[str, str | bytes]] = [
        ("mimetype", "application/epub+zip"),
        ("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
         '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'),
    ]
    for name, head, chapter in zip(names, heads, chapters):
        body = to_html(chapter).replace("<hr/>", "<hr />").replace("<br>", "<br />")
        files.append((f"OEBPS/{name}", page.format(t=html.escape(head), b=body)))
    nav = "".join(f'<li><a href="{n}">{html.escape(h)}</a></li>' for n, h in zip(names, heads))
    files.append(("OEBPS/nav.xhtml", page.format(t="Contents", b=f'<nav epub:type="toc"><h1>Contents</h1><ol>{nav}</ol></nav>')))
    manifest = "".join(f'<item id="c{i}" href="{n}" media-type="application/xhtml+xml"/>' for i, n in enumerate(names, 1))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(names) + 1))
    files.append(("OEBPS/content.opf", '<?xml version="1.0" encoding="UTF-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                  f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{book_id}</dc:identifier>'
                  f'<dc:title>{escape(title)}</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2000-01-01T00:00:00Z</meta></metadata>'
                  f'<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{manifest}</manifest>'
                  f'<spine>{spine}</spine></package>'))
    return _zip(files, stored_first=True), f"EPUB book, {len(chapters)} chapter{'s' if len(chapters) != 1 else ''}"


# ---- PDF -------------------------------------------------------------------------

FONTS = [  # (regular, bold, italic, bold italic, mono): the first family found
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
     "/System/Library/Fonts/Supplemental/Arial Italic.ttf", "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
     "/System/Library/Fonts/Supplemental/Courier New.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/ariali.ttf", "C:/Windows/Fonts/arialbi.ttf",
     "C:/Windows/Fonts/cour.ttf"),
]


def pdf(blocks: list[Block], title: str) -> tuple[bytes, str]:
    try:
        import logging

        from fpdf import FPDF
        logging.getLogger("fpdf").setLevel(logging.ERROR)
        logging.getLogger("fontTools").setLevel(logging.ERROR)
    except ImportError as e:
        raise WriteError("writing PDFs needs the coding engine's PDF support; the user can add it with `oppx --update`") from e
    doc = FPDF(format="A4")
    doc.set_title(title)
    doc.set_auto_page_break(True, margin=18)
    family, mono = "helvetica", "courier"
    found = next((f for f in FONTS if all(Path(x).is_file() for x in f)), None)
    if found:
        doc.add_font("body", "", found[0])
        doc.add_font("body", "B", found[1])
        doc.add_font("body", "I", found[2])
        doc.add_font("body", "BI", found[3])
        doc.add_font("mono", "", found[4])
        family, mono = "body", "mono"
    doc.add_page()
    doc.set_font(family, size=11)
    source = to_html(blocks, fpdf=True)
    if not found:  # the built-in fonts only know Latin-1
        source = source.encode("latin-1", errors="replace").decode("latin-1")
    try:
        from fpdf import FontFace, TextStyle
        doc.write_html(source, font_family=family,
                       tag_styles={"code": FontFace(family=mono), "pre": TextStyle(font_family=mono, font_size_pt=9),
                                   # black headings (fpdf2's are dark red), sized as in the Word file
                                   **{f"h{n}": TextStyle(font_family=family, font_style="B", font_size_pt=size, color=(0, 0, 0),
                                                         t_margin=6, b_margin=2)
                                      for n, size in {1: 18, 2: 15, 3: 13, 4: 12, 5: 11, 6: 11}.items()}})
    except Exception as e:  # noqa: BLE001 - fpdf2 raises many kinds on content it can't lay out
        raise WriteError(f"the PDF couldn't be laid out ({type(e).__name__}: {str(e)[:200]})") from e
    return bytes(doc.output()), f"PDF, {doc.page} page{'s' if doc.page != 1 else ''}"


# ---- Excel -----------------------------------------------------------------------

NUMBER = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")  # one way to split the digits
SHEET_HEAD = re.compile(r"sheet\b\s*(?:\d+\s+of\s+\d+:\s*)?", re.I)
SHEET_SIZE = re.compile(r"\(\d+ rows, \d+ columns\)$")


def sheet_name(line: str) -> str | None:
    """The name in a "--- sheet Name ---" line (or read's "--- sheet 1 of 2:
    Name (3 rows, 2 columns) ---"), "" for a sheet without one, None for any
    other line. By string, not one regex: that one was cubic."""
    line = line.strip()
    if not (line.startswith("---") and line.endswith("---") and len(line) >= 6):
        return None
    inner = line[3:-3].strip()
    head = SHEET_HEAD.match(inner)
    if not head:
        return None
    name = inner[head.end():].strip()
    size = SHEET_SIZE.search(name)
    return (name[:size.start()] if size else name).strip()


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx(source: str) -> tuple[bytes, str]:
    sheets: list[tuple[str, list[str]]] = []
    for ln in source.replace("\r\n", "\n").split("\n"):
        name = sheet_name(ln)
        if name is not None:
            sheets.append((name or f"Sheet{len(sheets) + 1}", []))
        else:
            if not sheets:
                sheets.append(("Sheet1", []))
            sheets[-1][1].append(ln)
    sheets = [(name, lines) for name, lines in sheets if any(x.strip() for x in lines)] or [("Sheet1", [])]
    seen: set[str] = set()
    files = []
    total = 0
    for k, (name, lines) in enumerate(sheets, 1):
        while lines and not lines[-1].strip():
            lines.pop()
        rows = list(csv.reader(lines))
        total += len(rows)
        out = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{_col(c)}{r}"
                if value == "":
                    continue
                if value.startswith("=") and len(value) > 1:
                    cells.append(f'<c r="{ref}"><f>{escape(value[1:])}</f></c>')
                elif NUMBER.fullmatch(value.strip()):
                    cells.append(f'<c r="{ref}"><v>{value.strip()}</v></c>')
                elif value.upper() in ("TRUE", "FALSE"):
                    cells.append(f'<c r="{ref}" t="b"><v>{1 if value.upper() == "TRUE" else 0}</v></c>')
                else:
                    bold = ' s="1"' if r == 1 else ""  # the first row, a header, in bold
                    cells.append(f'<c r="{ref}" t="inlineStr"{bold}><is><t xml:space="preserve">{escape(value)}</t></is></c>')
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        files.append((f"xl/worksheets/sheet{k}.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      f'<sheetData>{"".join(out)}</sheetData></worksheet>'))
        # Excel's rules for sheet names: 31 characters, none of []:*?/\, unique.
        clean = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or f"Sheet{k}"
        while clean.lower() in seen:
            clean = f"{clean[:28]} {k}"
        seen.add(clean.lower())
        sheets[k - 1] = (clean, lines)
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{k}" r:id="rId{k}"/>' for k, (n, _) in enumerate(sheets, 1))
                + "</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{k}" Type="{rel}/worksheet" Target="worksheets/sheet{k}.xml"/>' for k in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="{main}">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs></styleSheet>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
             '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
             + "".join(f'<Override PartName="/xl/worksheets/sheet{k}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                       for k in range(1, len(sheets) + 1)) + "</Types>")
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    data = _zip([("[Content_Types].xml", types), ("_rels/.rels", root_rels), ("xl/workbook.xml", workbook),
                 ("xl/_rels/workbook.xml.rels", rels), ("xl/styles.xml", styles), *files])
    return data, f"Excel workbook, {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}, {total} rows"


# ---- pictures ----------------------------------------------------------------------

def picture(svg: str, ext: str) -> tuple[bytes, str]:
    try:
        import resvg_py
    except ImportError as e:
        raise WriteError("drawing pictures needs the coding engine's picture support; the user can add it with `oppx --update`") from e
    from PIL import Image
    svg = svg.strip()
    if "<svg" not in svg:
        raise WriteError("the content isn't SVG: write one <svg …> document")
    opaque = ext in (".jpg", ".jpeg")
    try:
        png = bytes(resvg_py.svg_to_bytes(svg_string=svg, background="#ffffff" if opaque else None))
    except (ValueError, RuntimeError) as e:
        raise WriteError(f"the SVG couldn't be drawn ({str(e)[:200]})") from e
    if ext == ".png":
        im = Image.open(io.BytesIO(png))
        return png, f"PNG picture, {im.width}x{im.height}"
    im = Image.open(io.BytesIO(png))
    buf = io.BytesIO()
    if opaque:
        im.convert("RGB").save(buf, "JPEG", quality=92)
    elif ext == ".gif":
        im.convert("RGBA").save(buf, "GIF")
    else:
        im.save(buf, "WEBP", quality=92)
    return buf.getvalue(), f"{IMAGES[ext]} picture, {im.width}x{im.height}"
