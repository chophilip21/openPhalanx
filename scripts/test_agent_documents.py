#!/usr/bin/env python3
"""Unit tests for documents and pictures in the agent: PDFs, Office files,
pictures sent to a model that sees, and files written from Markdown, CSV or SVG
(scripted model, no server). Run like test_agent.py."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import agent_testkit  # noqa: F401  (the package on the path)
from oppx_chat import agent as A, workspace as W


def make_pdf(pages: list[str]) -> bytes:
    """A small PDF, one line of text per page ("" for a page without text)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R "
                    "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class Pdfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "docs").mkdir()
        (root / "docs/spec.pdf").write_bytes(make_pdf(["Pairing codes expire after ten minutes", "", "Tokens last ninety days"]))
        (root / "scan.pdf").write_bytes(make_pdf(["", ""]))
        (root / "notes.md").write_text("tokens are revoked on unpair\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        self.ws = A.Workspace(str(root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_gives_the_text_page_by_page(self):
        out = self.ws.read("docs/spec.pdf", None, None)
        self.assertIn("--- page 1 of 3 ---", out)
        self.assertIn("Pairing codes expire after ten minutes", out)
        self.assertIn("(no text on this page)", out)
        self.assertIn("Tokens last ninety days", out)

    def test_outline_lists_the_pages(self):
        out = self.ws.outline("docs/spec.pdf")
        self.assertIn("PDF, 3 pages", out)
        self.assertIn("--- page 3 of 3 --- Tokens last ninety days", out)

    def test_grep_searches_pdfs_with_text_files(self):
        out = self.ws.grep("[Tt]okens")
        self.assertIn("docs/spec.pdf (1)", out)
        self.assertIn("notes.md (1)", out)
        self.assertIn("No matches", self.ws.grep("ninety", "notes.md"))

    def test_a_pdf_is_not_edited_as_text(self):
        with self.assertRaises(A.ToolError) as e:
            A.apply_edit(self.ws, "docs/spec.pdf", "x")
        self.assertIn("can't be edited as text. To change it, create it again in full", str(e.exception))

    def test_a_scan_is_seen_as_pictures_or_says_why_not(self):
        try:
            import pypdf  # noqa: F401 - installed with the engine
        except ImportError:
            self.skipTest("pypdf isn't installed")
        from PIL import Image
        root = Path(self.tmp.name)
        scan = root / "scanned.pdf"
        Image.new("RGB", (400, 500), "white").save(scan, "PDF")  # a page that is one picture
        old = W.VISION
        try:
            W.VISION = False
            with self.assertRaises(A.ToolError) as e:
                self.ws.read("scanned.pdf", None, None)
            self.assertIn("can't see pictures", str(e.exception))
            W.VISION = True
            out = self.ws.read("scanned.pdf", None, None)
            self.assertIn("PDF of 1 pages without text", out)
            self.assertEqual(len(self.ws.take_images()), 1)
        finally:
            W.VISION = old


MARKDOWN = """# Quarterly report

Revenue grew **12%** to *$4.2M* in Q3, see `report.py`.

## Highlights

- New customers: 41
- Churn fell
  - mostly in retail
1. First step
2. Second step

| Region | Revenue | Growth |
|---|---|---|
| North | 1.9 | 14% |
| South | 2.3 | 10% |

```
total = north + south
```

Café prices in € rose — slightly.
"""


class Documents(unittest.TestCase):
    """Every format the agent makes is read back by what it reads with."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def made(self, name: str, source: str) -> str:
        p = self.root / name
        from oppx_chat import documents, writers
        writers.write(p, source)
        return documents.text(p)

    def test_text_documents_round_trip(self):
        formats = [".docx", ".odt", ".rtf", ".epub"]
        try:
            import fpdf  # noqa: F401 - installed with the engine
            formats.append(".pdf")
        except ImportError:
            pass
        for ext in formats:
            with self.subTest(ext):
                text = self.made("report" + ext, MARKDOWN)
                for want in ("Quarterly report", "Revenue grew 12%", "New customers: 41", "mostly in retail",
                             "Second step", "North", "2.3", "total = north + south", "Café prices in € rose — slightly"):
                    self.assertIn(want, text, f"{ext}: {want!r} in {text!r}")

    def test_headings_and_tables_keep_their_shape(self):
        for ext in (".docx", ".odt"):
            text = self.made("r" + ext, MARKDOWN)
            self.assertIn("# Quarterly report", text, ext)
            self.assertIn("## Highlights", text, ext)
            self.assertIn("| North | 1.9 | 14% |", text, ext)

    def test_a_workbook_round_trips_with_sheets_numbers_and_formulas(self):
        from oppx_chat import documents
        source = "--- sheet Sales ---\nRegion,Q1,Q2\nNorth,10,12.5\nSouth,\"7,5\",3\nTotal,=SUM(B2:B3),=SUM(C2:C3)\n--- sheet Notes ---\nok,TRUE\n"
        text = self.made("book.xlsx", source)
        self.assertIn("--- sheet 1 of 2: Sales (4 rows, 3 columns) ---", text)
        self.assertIn('South,"7,5",3', text)
        self.assertIn("Total,=SUM(B2:B3),=SUM(C2:C3)", text)  # never calculated: the formula shows
        self.assertIn("--- sheet 2 of 2: Notes (1 rows, 2 columns) ---", text)
        self.assertIn("ok,TRUE", text)
        import zipfile
        sheet = zipfile.ZipFile(self.root / "book.xlsx").read("xl/worksheets/sheet1.xml").decode()
        self.assertIn('<c r="B2"><v>10</v></c>', sheet)  # a number, not text
        self.assertIn("<f>SUM(B2:B3)</f>", sheet)
        # What read showed can be written back as it is.
        again = self.made("again.xlsx", documents.text(self.root / "book.xlsx"))
        self.assertIn("--- sheet 1 of 2: Sales (4 rows, 3 columns) ---", again)

    def test_pictures_are_drawn_from_svg_and_seen(self):
        try:
            import resvg_py  # noqa: F401 - installed with the engine
        except ImportError:
            self.skipTest("resvg-py isn't installed")
        from PIL import Image
        from oppx_chat import writers, documents
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80" fill="#3366cc"/></svg>'
        for ext, fmt in ((".png", "PNG"), (".jpg", "JPEG"), (".webp", "WEBP"), (".gif", "GIF")):
            p = self.root / ("chart" + ext)
            writers.write(p, svg)
            with Image.open(p) as im:
                self.assertEqual((im.format, im.size), (fmt, (120, 80)), ext)
            url, w, h, tokens = documents.picture(p)
            self.assertTrue(url.startswith("data:image/jpeg;base64,") and (w, h) == (120, 80))
        with self.assertRaises(writers.WriteError):
            writers.write(self.root / "x.png", "not svg")

    def test_a_gif_shows_its_first_frame(self):
        from PIL import Image
        from oppx_chat import documents
        frames = [Image.new("RGB", (50, 50), c) for c in ("red", "blue")]
        frames[0].save(self.root / "anim.gif", save_all=True, append_images=frames[1:])
        url, *_ = documents.picture(self.root / "anim.gif")
        import base64, io
        im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        r, g, b = im.getpixel((25, 25))
        self.assertGreater(r, 200)
        self.assertLess(b, 60)

    def test_a_picture_is_attached_only_for_a_model_that_sees(self):
        from PIL import Image
        Image.new("RGB", (2000, 1000), "white").save(self.root / "shot.png")
        ws = A.Workspace(str(self.root))
        old = W.VISION
        try:
            W.VISION = False
            with self.assertRaises(A.ToolError) as e:
                ws.read("shot.png", None, None)
            self.assertIn("can't see pictures", str(e.exception))
            W.VISION = True
            self.assertIn("1280x640 pixels, attached", ws.read("shot.png", None, None))
            images = ws.take_images()
            self.assertEqual(len(images), 1)
            self.assertEqual(ws.take_images(), [])
            # The request carries it as an image part, while the message isn't cut.
            ag = A.Agent(ws=ws, hooks=A.Hooks())
            ag.messages = [{"role": "user", "_result": True, "content": "Result of step 1 (read)", "_images": images}]
            parts = ag._clean()[0]["content"]
            self.assertEqual([p["type"] for p in parts], ["text", "image_url"])
            self.assertGreater(ag._estimate(), images[0]["tokens"])
            ag.messages[0]["_cut"] = True
            self.assertIsInstance(ag._clean()[0]["content"], str)
        finally:
            W.VISION = old

    def test_only_the_newest_pictures_fit_one_request(self):
        ws = A.Workspace(str(self.root))
        ag = A.Agent(ws=ws, hooks=A.Hooks())
        big = "data:image/jpeg;base64," + "A" * (A.IMAGE_BYTES // 2 + 10)
        ag.messages = [{"role": "user", "_result": True, "content": f"r{i}", "_images": [{"url": big, "tokens": 100}]} for i in range(3)]
        sent = ag._clean()
        self.assertIn("no longer attached", json.dumps(sent[0]))
        self.assertEqual(sum(1 for m in sent for p in (m["content"] if isinstance(m["content"], list) else [])
                             if p.get("type") == "image_url"), 1)
        self.assertNotIn("no longer attached", json.dumps(sent[2]))

    def test_videos_are_refused_both_ways(self):
        (self.root / "demo.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        ws = A.Workspace(str(self.root))
        with self.assertRaises(A.ToolError) as e:
            ws.read("demo.mp4", None, None)
        self.assertIn("is a video, and videos can't be read", str(e.exception))
        ag = A.Agent(ws=ws, hooks=A.Hooks())
        with self.assertRaises(A.ToolError) as e:
            ag._create("clip.mov")
        self.assertIn("videos can't be made", str(e.exception))

    def test_presentations_are_refused_both_ways(self):
        (self.root / "pitch.pptx").write_bytes(b"PK\x03\x04")
        ws = A.Workspace(str(self.root))
        with self.assertRaises(A.ToolError) as e:
            ws.read("pitch.pptx", None, None)
        self.assertIn("is a presentation, and presentations can't be read", str(e.exception))
        with self.assertRaises(A.ToolError) as e:
            A.apply_edit(ws, "pitch.pptx", "x")
        self.assertIn("is a presentation: it can't be edited as text", str(e.exception))
        ag = A.Agent(ws=ws, hooks=A.Hooks())
        for name in ("deck.ppt", "deck.key", "slides.odp"):
            with self.assertRaises(A.ToolError) as e:
                ag._create(name)
            self.assertIn("presentations can't be made", str(e.exception))

    def test_a_turn_starts_from_the_documents_it_is_about(self):
        from oppx_chat import writers
        (self.root / "docs").mkdir()
        writers.write(self.root / "docs/travel-policy.docx", "# Travel\n\nHotels: $210.")
        (self.root / "shots").mkdir()
        (self.root / "shots/home.png").write_bytes(b"")
        (self.root / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        ag = A.Agent(ws=A.Workspace(str(self.root)), hooks=A.Hooks())
        self.assertEqual(ag._documents_for("What does the travel policy say about hotels?"), ["docs/travel-policy.docx"])
        self.assertEqual(ag._documents_for("What is wrong in the screenshot?"), ["shots/home.png"])
        self.assertEqual(ag._documents_for("Rename x to count"), [])
        # A file named without its folder is opened when only one file has that name.
        self.assertEqual(ag._named_files("what is in travel-policy.docx?"), ["docs/travel-policy.docx"])
        made = ag._documents_to_make("Create summary.docx and a chart.png, then update app.py")
        self.assertIn("summary.docx: use create on summary.docx itself; you write Markdown and it becomes the Word document", made)
        self.assertIn("chart.png: use create on chart.png itself; you write SVG and it becomes the PNG picture", made)
        self.assertNotIn("app.py", made)
        self.assertEqual(ag._documents_to_make("Fix the bug in app.py"), "")
        # A picture that exists, named to be read: not told how to make it, and told to leave it.
        read = ag._documents_to_make("The first half is the screenshot shots/home.png; read it, then build it.")
        self.assertNotIn("shots/home.png: use create", read)
        self.assertIn("If it is there to be read, leave it as it is", read)

    def test_what_the_model_cant_take_is_stopped_before_sending(self):
        from oppx_chat import documents
        pic, clip, deck, doc = (self.root / n for n in ("shot.png", "demo.mp4", "pitch.pptx", "notes.docx"))
        self.assertIn("can't see pictures", documents.blocked(pic, False, "Qwen2.5-Coder-14B"))
        self.assertIsNone(documents.blocked(pic, True, "m"))
        self.assertIn("videos aren't supported", documents.blocked(clip, True, "m"))
        self.assertIn("presentations aren't supported", documents.blocked(deck, True, "m"))
        self.assertIsNone(documents.blocked(doc, False, "m"))
        try:
            from oppx_chat import commands
        except ImportError:
            self.skipTest("the chat's commands need Aider")
        from oppx_chat import term
        for n in ("shot.png", "notes.md"):
            (self.root / n).write_bytes(b"x")
        added, said = [], []

        class Coder:
            root = str(self.root)

            class commands:  # noqa: N801 - stands in for Aider's
                cmd_add = staticmethod(added.append)
        old_step, old_vision = commands.step, W.VISION
        commands.step = lambda text, *a: said.append(text)
        try:
            W.VISION = False
            self.assertIsNone(commands.translate(Coder, "what is in @shot.png and @notes.md?"))
            self.assertEqual(added, [])  # nothing added, nothing sent
            self.assertTrue(any("can't see pictures" in t for t in said))
            self.assertEqual(commands.translate(Coder, "/add shot.png notes.md"), "/add notes.md")
            self.assertIsNone(commands.translate(Coder, "/add shot.png"))
            W.VISION = True
            self.assertEqual(commands.translate(Coder, "what is in @shot.png?"), "what is in shot.png?")
            self.assertEqual(added, ["shot.png"])
        finally:
            commands.step, W.VISION = old_step, old_vision
        del term

    def test_sheet_lines(self):
        from oppx_chat import writers
        for line, name in (("--- sheet Sales ---", "Sales"), ("--- sheet 1 of 2: Sales (4 rows, 3 columns) ---", "Sales"),
                           ("--- SHEET   Notes ---", "Notes"), ("--- sheet ---", ""), ("---sheet x---", "x"),
                           ("a,b", None), ("--- other ---", None), ("--- sheets ---", None)):
            self.assertEqual(writers.sheet_name(line), name, line)

    def test_rtf_reader_handles_unicode_and_skips_tables_of_fonts(self):
        from oppx_chat import documents
        rtf = rb"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0 Arial;}}{\*\generator Word;}Caf\'e9 \u8364? 5\par Second\tab line}"
        self.assertEqual(documents.rtf_text(rtf), "Café € 5\nSecond\tline")


if __name__ == "__main__":
    unittest.main()
