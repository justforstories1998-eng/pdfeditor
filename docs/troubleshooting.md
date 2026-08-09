# Troubleshooting

## Diagnostics first

```bash
pdfstudio-cli info problem.pdf --verbose     # what is actually in the file
PDFSTUDIO_LOG_LEVEL=DEBUG pdfstudio          # verbose logging
pdfstudio --safe-mode                        # no plugins, no session restore
pdfstudio --reset-settings                   # factory defaults
```

Logs live in the cache directory (**Help ▸ Open log folder**):

* `pdfstudio.log` — rotating, 10 MB × 10, zipped
* `crash.log` — errors and unhandled exceptions with tracebacks

---

## Start-up

**The application will not start on Linux**

Qt needs a few system libraries that minimal images omit:

```bash
sudo apt-get install -y libxkbcommon0 libegl1 libgl1 libdbus-1-3 libfontconfig1
```

Over SSH without a display, use `QT_QPA_PLATFORM=offscreen` (rendering and the
CLI still work).

**Blurry text on a HiDPI screen**

Scaling is enabled automatically. If your desktop reports an unusual scale:

```bash
QT_SCALE_FACTOR=1.5 pdfstudio
QT_SCALE_FACTOR_ROUNDING_POLICY=PassThrough pdfstudio
```

**Wayland artefacts** — force X11: `QT_QPA_PLATFORM=xcb pdfstudio`.

**Start-up hangs** — a plugin is likely at fault. Start with `--safe-mode`,
then disable plugins one at a time in **Tools ▸ Plugins**.

---

## Documents

**"Password required" but you have the password**

PDFs have two passwords. The *open* password is needed to read; an *owner*
password only lifts restrictions. Enter the open password. If a copy was made
with a different password, try that.

**A document opens but every page is blank**

Usually a scan with no text layer. Run **Tools ▸ OCR**. Confirm with
`pdfstudio-cli info file.pdf --verbose` — `pages_without_text` tells you.

**"Cannot open" on a file that other viewers read**

The file is damaged. PDF Studio repairs what it can on open; check
`is_repaired` in the properties. Try:

```bash
pdfstudio-cli convert broken.pdf -o repaired.pdf
```

**Text extraction returns nothing on an encrypted file**

Fixed in 1.0. If you see it in an older build, upgrade — it was caused by a
PyMuPDF state bug we now work around.

---

## Editing

**Edited text uses the wrong font**

The original font is not embedded, so it cannot be reproduced exactly. PDF
Studio substitutes the closest base-14 font. Embed the font, or set an explicit
font in the text properties.

**Text edits shift the line**

Replacement text is drawn at the original baseline, but a different string
width can change the layout. For paragraphs, drag a rectangle over the whole
block so the text reflows within it.

**Redaction did not remove the text**

Marking is not applying. After marking, use **Protect ▸ Apply**, then save.
Verify with **Ctrl+F** — the words are genuinely gone.

**Undo did not restore everything**

Applying redactions is irreversible in the *saved* file; the undo entry only
restores the in-memory document. Save to a new name if you need the original.

---

## Performance

**Scrolling stutters in a large document**

* Preferences ▸ Performance: lower the render DPI, raise the page cache.
* Turn off **Show comments** while navigating a heavily annotated file.
* Prefetch 1–2 pages instead of 3 on slow disks.

**Memory keeps growing**

The page cache is bounded in megabytes (default 512). Lower it, or clear the
render cache in **Preferences ▸ Storage**. If it still grows, capture a report:
`pytest -m benchmark -k memory -s`.

**OCR is very slow**

OCR is CPU-bound: roughly 1–3 seconds per page at 300 dpi. Lower the DPI to
200 for clean documents, restrict the page range, or use EasyOCR with a GPU
(`pip install "pdfstudio[ocr-neural]"`).

---

## Features not available

**"Optional dependency missing"**

The dialog names the package. Common ones:

| Feature | Install |
| --- | --- |
| OCR | `pip install pytesseract` + the `tesseract-ocr` system package |
| DOCX/PPTX export | `pip install "pdfstudio[office]"` |
| Digital signatures | `pip install "pdfstudio[signatures]"` |
| QR/barcode fields | `pip install "pdfstudio[forms]"` |
| HEIC / RAW import | `pip install "pdfstudio[images]"` |

**Office import loses formatting**

The pure-Python fallback keeps text, headings and tables but not layout.
Install LibreOffice and make sure `soffice` is on `PATH` for high fidelity.

**PDF/A output is not accepted by a validator**

Without Ghostscript, PDF Studio writes a *conformance-oriented* file (correct
XMP, embedded fonts) rather than a certified one. Install Ghostscript for real
PDF/A and PDF/X conversion.

**OCR reports "no engine installed"**

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-fra   # Linux
brew install tesseract                                  # macOS
pip install pytesseract
```

Check detection in **Preferences ▸ OCR** — installed engines are listed there.

---

## Printing

**No printers listed**

Qt print support is a separate package: `pip install PySide6-Addons`. Without
it, **Print to PDF** still works.

**Output is scaled wrongly**

Set Scaling to *Actual size* and disable your printer driver's own "fit to
page". For booklets and posters use the Mode dropdown rather than driver
options.

---

## Plugins

**A plugin does not appear**

It must be a `.py` file (or a package with `__init__.py`) in the plugin folder,
defining a `Plugin` subclass and `PLUGIN`. Use **Tools ▸ Plugins ▸ Rescan** and
read the details pane for load errors.

**"…tried to import X without the 'network' permission"**

The sandbox blocked a risky import. Add the permission to the plugin's
metadata, or disable the sandbox in **Preferences ▸ Plugins** if you trust it.

---

## Recovering work

**PDF Studio closed unexpectedly**

Restart: recovered documents are offered automatically. Autosaves also live in
`data/autosave/` as ordinary PDFs — open them directly if you dismissed the
prompt.

**A document was saved over by mistake**

Autosave keeps several versions per document (default 8). Look in
`data/autosave/` for `<document-id>-<timestamp>.pdf`.

---

## Reporting a bug

Include:

1. The **crash report** (Help ▸ About shows versions; `crash.log` has the trace).
2. What you did, what happened, what you expected.
3. `pdfstudio-cli info problem.pdf --verbose --json` if a specific file is involved.
4. Your platform and how you installed.

Please do not attach confidential documents — a redacted or synthetic
reproduction is usually enough.
