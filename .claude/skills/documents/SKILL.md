---
name: documents
description: "Documents and pictures in the agent (oppx_chat/documents.py, writers.py): which formats it reads and makes, how (text conversion, pictures sent to vision models, Markdown/CSV/SVG converted on create), the engine's extra packages, the server's vision flag, what was measured on the 3090 and what is still open. Read before changing documents.py, writers.py, the read/outline/grep/create paths for non-text files, or the vision plumbing."
---

# Documents and pictures: reading and making them

Part of the developer notes; the index is in `CLAUDE.md`.

## What the agent handles

| Kind | Read | Made with `create` from |
|---|---|---|
| Text: TXT, Markdown, JSON, CSV, HTML | as they are | as they are |
| PDF | text, page by page (pypdf; poppler's `pdftotext` as a fallback) | Markdown (fpdf2) |
| Word `.docx`, OpenDocument `.odt`, RTF, EPUB | text, with `#` headings and `\| tables \|` (standard library) | Markdown (standard library) |
| Excel `.xlsx`, `.xlsm` | each sheet as CSV (standard library) | CSV (standard library) |
| PNG, JPEG, GIF (first frame), WebP | a picture attached to the request, when the model sees | SVG (resvg-py, then Pillow) |
| A PDF without text (a scan) | its pages as pictures, 3 per read | — |
| Video | refused: "videos can't be read", "can't be made" | refused |
| Presentations: `.ppt`, `.pptx`, `.pptm`, `.pps`, `.ppsx`, `.odp`, `.key` | refused (overkill for now) | refused, offering a PDF or Word document instead |

* **One layout for every multi-part document:** each part starts with a `--- page 3 of 20 ---` line (`sheet`, `chapter`), so `read` line ranges work as in any file, `outline` lists the parts and headings, and `grep` searches the text (git grep skips binary files, so `grep_documents` adds up to 30 documents per search). What `read` shows for a workbook can be passed back to `create` unchanged: the `--- sheet Name ---` lines become sheets.
* **Text is NFKC-normalized**, so the "ﬁ" ligature in PDFs becomes "fi" and grep finds "file".
* **Documents are read-only as text:** `read_text` (what edits use) refuses them with "create it again in full". Converted text is cached per (path, mtime, size).
* **The model writes text, never a binary file.** `create` on a writable extension adds a line to the prompt saying what to write (`writers.hint`), and `writers.write` builds the file. The result says what was made ("Word document, 11 blocks", "PDF, 1 page") and asks the model to read it back to check. `/undo` restores a replaced document from its bytes (`Hooks.original`); the diff shown is of the text.
* **Formulas:** a number stays a number, `=…` becomes a formula. Files we write have no cached results, so the reader shows the formula itself when there is no stored value.
* **Fonts for PDFs:** DejaVu, Liberation, macOS Arial or Windows Arial, whichever is found first (Unicode). Without any of them fpdf2's built-in Helvetica is used and text outside Latin-1 becomes `?`.

## Pictures and vision

* **Set when the model loads:** the gateway's `watch_inputs` task reads SGLang's `/model_info` as soon as SGLang is ready (`has_image_understanding`, `has_audio_understanding`; `inputs.accepted`), logs `[gateway] the model takes: image, text`, and keeps it for the container's life. It is one local request after loading, so startup is unchanged: 213 s for the 14B and 218 s for the 9B, against 210–220 s before. No catalog flag is needed: SGLang loads a model's vision part by itself (log line "Multimodal data loading enabled") and knows. 28 of the catalog's 84 models have a vision encoder by their config (Qwen3.5+, Gemma 4, Mistral 3, Llama 4, Kimi K2.6+, …); the coder models are text-only.
* **Shown:** `/v1/info` has `vision` and `inputs`, and so does `/admin/status`. `oppx` prints "sees pictures" or "text only" on its connect line and passes the flag to the chat as `OPPX_VISION`.
* **Guarded at three levels, each before any model call:**
  * **Gateway** (`docker/server/inputs.py`, for every client): a request whose messages carry a part the model can't take gets HTTP 400 `unsupported_input` with the reason ("The loaded model (…) can't take pictures: it takes text only…"). Video parts are refused for every model, and so are unknown part types. Measured: 8 ms for the refusal on the 14B; the image passed on the 9B; video got 400 in 5 ms on both. Tests: `scripts/test_inputs.py`.
  * **Handing a file to the chat** (`@file`, `/add`; `commands.cannot_take`): a picture for a text-only model, a video or a presentation is refused with the reason and "Nothing was sent". `/add` keeps the other files. Punctuation after a mention ("@shot.png?") no longer hides it.
  * **Naming a file in a sentence:** a one-line notice before the turn, not a stop, since "rename logo.png" is fine. The agent's own `read` then refuses with "the loaded model can't see pictures. Tell the user", so the model doesn't guess.
* **How a picture is sent:** shrunk to at most 1280 px on the longest side, JPEG at quality 85 (transparency on white), attached to the tool-result message under `_images`. Message `content` stays a string everywhere. Only `_clean()` turns it into `[text, image_url…]` parts, and only for messages that weren't cut to save space.
* **Request size:** the gateway accepts 4 MiB (`MAX_REQUEST_BYTES`) and every request resends the whole conversation. So each request carries the newest pictures up to 2.5 MB (`IMAGE_BYTES`). Older ones are left out, with a note to read them again.
* **Token estimate:** pixels / 784 + 16 per picture, counted on top of the text estimate. It shows on `/context` as "Pictures".

## Starting a turn

Found on the 3090 with Qwen3.5-9B: without these, a 9B model answered "I can't find a travel policy PDF" without looking, said "Let me read the file" and stopped, and wrote a Python script to make a PDF or PNG.

* **A named file is opened up front, pictures and videos too.** That way the model sees the picture, or the refusal. A bare file name counts when only one file in the repository has it.
* **Documents the request is about** (`_documents_for`): files named like its words ("travel policy" finds `docs/travel-policy.pdf`), or every file of a kind it mentions ("the PDF", "the screenshot") when there are at most 10.
* **Files to make** (`_documents_to_make`): "summary.docx: use create on summary.docx itself; you write Markdown… No script is needed". A file that already exists gets this only as a condition ("exists. Only if the request asks you to change it: … If it is there to be read, leave it as it is"): given a screenshot and a PDF holding the instructions for the next phase of a project ("the first half is the screenshot docs/phase2-part1.jpg"), the note had read "Files the request asks for: docs/phase2-part1.jpg: use create on it…".
* **Seen end to end on split Qwen3.6-27B (2026-10-19):** a request naming `docs/phase2-part1.jpg` (900×540 screenshot of a README section) and `docs/phase2-part2.pdf` (text PDF, the rest of it): both opened up front, the picture sent as an `image_url` part, the PDF as its 7 lines of text.
* **Router examples:** the ask/edit prompt (`routing.INTENT_PROMPT`) now says that asking for a file to be made is "edit" whatever goes in it. Before that, "Create summary.docx: a one-page summary…" was routed to "ask" and the summary came back in the chat. `probe_routing.py` has 7 more intent cases (documents and pictures, worded differently from the prompt's examples).

## Engine packages

`engine::EXTRAS` installs pypdf 6.19.0, fpdf2 2.8.9 (which pulls in fontTools and defusedxml) and resvg-py 0.5.0 next to Aider (`uv tool install --with …`). A private engine without them counts as not current, so `oppx --update` or the next launch installs it again. Without the extras, PDFs are still read through `pdftotext` when it is installed, and making PDFs or pictures, or seeing a scan's pages, says "the user can add it with `oppx --update`". Checked: the exact install command in a scratch `OPPX_ENGINE_DIR` gives the four dist-info folders `find()` looks for.

## Measured (RTX 3090, 2026-10-16)

* **Vision through the gateway:** Qwen3.5-9B-AWQ (`Qwen3_5ForConditionalGeneration`, the same class as Qwen3.6), backend image 0.7.4, 32k context, MEM_FRACTION_STATIC 0.85. Weights 8.5 GB. Through `oppx proxy` and the gateway:
  * an 800×500 invoice: 400 image tokens, correct, 0.97 s;
  * the same with a JSON schema (constrained decoding and an image together): correct fields, 1.04 s;
  * a bar chart: highest and lowest bar right, 0.31 s;
  * the invoice again: 384 tokens cached, so the prefix cache covers images.
  * A scanned manual page: 962 image tokens at 100 dpi (827×1170), 2,145 at 150 dpi. Both answered correctly, so 1280 px is enough.
* **Qwen3.6-27B-AWQ doesn't fit the 3090 alone** with its vision part. Weights take 19.07 GB, which leaves 3 GB. At 0.85 the hybrid (mamba) state cache had no room. At 0.93 with 4 requests it still had none. At 0.95 with 1 request, bf16 state and 8k context, it ran out of memory in CUDA graph capture. It runs split across two machines.
* **End to end with `oppx -p`** (Qwen3.5-9B, scratch repo):
  * read a PDF, a Word file and a PNG receipt: all answered correctly;
  * refused a video, without guessing, after the refusal text said so;
  * made `summary.docx`, `expenses.xlsx` (formulas computed by LibreOffice), `travel-rules.pdf` and `badge.png`, all through `create`.
* **The files open in real software:**
  * LibreOffice converts the generated `.docx`, `.odt` and `.rtf` to PDF with headings, nested lists, numbered lists, tables, code, bold, italic, é and €;
  * it opens the `.xlsx` and computes its formulas;
  * the fpdf2 PDF renders the same content.
* **Tests:** `scripts/test_agent_documents.py` classes `Pdfs` and `Documents` cover:
  * the round trip of every format through `writers` and `documents`;
  * workbooks with sheets, numbers and formulas;
  * SVG drawn to all four picture formats;
  * a GIF's first frame;
  * pictures attached only for a model that sees, and dropped when their message is cut;
  * the 2.5 MB budget;
  * video and presentation refusals;
  * RTF unicode;
  * the turn-start helpers.

  The fpdf2, resvg and pypdf parts skip when the engine lacks them.

## Open

* The `vision` flag and the gateway's check need the new `gateway.py` and `inputs.py` in the image: the app's image tag changes with them (they are in `BUILD_FILES`), so the app rebuilds it on the next start. Against an older server the client sees `vision: false`, and pictures are refused.
* The VRAM estimate (`vram.rs`) doesn't count a vision encoder or image tokens. The 9B fit with room to spare; the 27B shows the encoder matters on a 24 GB card.
* Excel dates come back as Excel's day numbers (number formats aren't applied). Charts, images and comments inside Office files aren't read.
* EPUB tables read back as one line per cell row with blank lines between rows.
* Only Qwen3.5-9B was tried end to end. Gemma 4's vision path through SGLang is untested.
