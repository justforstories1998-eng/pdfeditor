# PDF Studio — user manual

## Getting started

Launch from your applications menu, or run `pdfstudio` in a terminal. Open a
document with **Ctrl+O**, by dragging files onto the window, or from
**File ▸ Open recent**. Each document opens in its own tab; the previous
session is restored automatically unless you disable it in Preferences.

### The window

| Area | Purpose |
| --- | --- |
| **Ribbon** | Ten tabs grouping every command. Switch to a classic toolbar in **View ▸ Classic toolbar** |
| **Left dock** | Pages, Bookmarks, Comments, Layers, Attachments (tabbed) |
| **Canvas** | The document. Scroll, zoom, select and annotate here |
| **Right dock** | Search and Properties (hidden until needed) |
| **Status bar** | Progress, page number, zoom, file size, cancel button |

Every panel is dockable: drag it to another edge, tab it with a neighbour, or
float it on a second monitor. The layout is remembered between sessions.

---

## Viewing

* **Zoom** — Ctrl+scroll, pinch, the zoom box, or `Ctrl +` / `Ctrl -`.
  `Ctrl 0` fits the page, `Ctrl 1` fits the width.
* **Layouts** — View ▸ Single, Continuous, Facing or Book.
* **Presentation** — **F5** for full-screen, chrome-free reading; arrow keys or
  Page Up/Down to move; **Esc** to exit.
* **Night mode** — View ▸ Night mode inverts page colours without changing the
  file. The Sepia theme is easier for long reading.
* **Rotate view** — rotates on screen only; use **Pages ▸ Rotate** to change
  the document.

Very large documents stay responsive: only visible pages are laid out and
rasterised, and neighbouring pages are rendered ahead of time.

---

## Editing text

Editing happens **directly on the page** — there is no dialog.

1. Start editing in whichever way suits you:
   * **double-click** any text (works with the Select, Pan, Text and Edit Text
     tools) — the quickest route;
   * choose **Edit text** on the **Home** tab (or press **E**), then click;
   * choose **Edit text** on the **Edit** tab, then click;
   * **right-click** the text and pick *Edit this line* or *Edit this
     paragraph*.

   With the Edit Text tool armed, hovering over text shows an I-beam and
   outlines the line you are about to edit.
2. The words you clicked become editable where they sit, in their original
   font, size and colour, with the caret on the character you clicked.
3. Type. Press **Enter** to apply, **Esc** to cancel.

| Key | Action |
| --- | --- |
| Enter | Apply the change |
| Shift+Enter | Insert a line break |
| Esc | Cancel and restore the original |
| Ctrl+B / Ctrl+I | Bold / italic |
| Ctrl+L / Ctrl+E / Ctrl+R | Align left / centre / right |
| Ctrl+[ / Ctrl+] | Decrease / increase the wrapped-line indent |
| Click elsewhere | Apply, then start editing there |

A small toolbar floats above the text with font, size, bold, italic, colour,
three alignment buttons and indent controls. Alignment and indent are applied
to the text **on screen as well as on the page**, so what you see while typing
is what gets written.

Editing never paints over the page: the original glyphs are removed rather
than covered with a white box, so tinted pages, letterheads and artwork behind
the text survive an edit untouched.

**What happens to the layout.** Only the line you clicked is edited — the rest
of the paragraph is left alone. Text wraps at the width of the original column
as you type, so a long sentence never runs off the page. If the replacement
needs more lines than it replaced, the following text is pushed down to make
room; when there is no room left, the font is scaled down to fit and an amber
line under the editor warns you.

To edit a whole paragraph at once (all its lines together, with full reflow),
hold **Alt** while clicking.

### Editing a whole paragraph

To change more than one line at once:

* **Alt+click** the paragraph, or
* **Edit ▸ Paragraph ▸ Edit paragraph** (**Ctrl+Shift+P**), or
* right-click the text and choose *Edit this paragraph*.

Every line opens in one editor and reflows together as you type. PDF Studio
works out where the paragraph ends from the line spacing and the font size, so
a heading above it and a list below it are left alone.

### Rearranging and reformatting

These act on the paragraph you last clicked, so a ribbon button works without
placing a cursor first. Everything here is a single undo step.

| Action | How |
| --- | --- |
| Move a line up / down | **Alt+Up** / **Alt+Down**, or *Paragraph ▸ Move line* |
| Duplicate a line | **Ctrl+Shift+D** |
| Delete a line | **Ctrl+Shift+K** |
| Bulleted / numbered list | *Lists & case ▸ Bullets / Numbering* |
| Remove list markers | *Lists & case ▸ Remove list* |
| Change case | *Lists & case ▸ UPPERCASE / lowercase / Title Case / Sentence case* |
| Line spacing | *Lists & case ▸ Single / 1.5 / Double* |
| Underline, strike-through | *Text style* group, **Ctrl+U** for underline |
| Copy a paragraph's look | *Lines & format ▸ Copy formatting*, then *Apply formatting* |
| Word count | *Lines & format ▸ Word count* |

Moving a line swaps it with its neighbour, so pressing **Alt+Up** twice moves
the same line two places — the cursor follows the text.

> **Note on underlines.** PDF has no underline attribute; PDF Studio draws a
> hairline rule under the glyphs. The text stays selectable and searchable,
> but a reader that reflows content may not treat the rule as an underline.

### Fixing indents on wrapped lines

When a line wraps, the continuation can inherit an indent from the document's
original layout and appear pushed to the right. While editing:

* **Edit ▸ Paragraph ▸ Remove indent** pulls wrapped lines back to the margin
  (12 pt per press); **Add indent** pushes them right again.
* **Align left / Centre / Align right** set the alignment of the text you are
  editing, previewing live.

### Moving things on the page

Some corrections are not about the words but about *where they sit* — a rule
touching a heading, a logo a few points too low.

1. Choose **Move object** on the Home or Edit tab (or press **M**).
2. Hover: the object under the cursor is outlined. Click to select it; handles
   and a size readout appear.
3. Then either:

| Action | Result |
| --- | --- |
| Drag | Move freely; hold **Shift** to constrain to one axis |
| Arrow keys | Nudge 2 pt (**Ctrl** = 0.5 pt fine, **Shift** = 10 pt coarse) |
| **Backspace** | Pull the object back towards the left margin |
| **Ctrl+C** / **Ctrl+V** | Copy the object, then paste a copy |
| **Ctrl+X** | Cut it to the object clipboard |
| **Ctrl+D** | Duplicate it in one step |
| **Edit ▸ Align left / centre / right** | Snap it to that side of the page |
| **Shift+Delete** | Remove the object |
| **Esc** | Deselect |

Text, images, vector rules and comments can all be moved. Right-clicking an
object also offers *Copy / Cut / Duplicate*, a *Move* submenu and an *Align*
submenu (left, centre, right, top, middle, bottom) directly. Every move, paste
and deletion is a single undo step.

#### Copying objects

Copy and paste work on whole page objects, not just selected text. Select
something with the Move tool and press **Ctrl+C**, then **Ctrl+V** — the copy
lands slightly offset from the original and is selected straight away, so it
can be dragged into place. To put it somewhere specific, right-click where you
want it and choose **Paste object here**.

This is a separate clipboard from the system one, because a vector rule or a
comment has no meaningful plain-text or bitmap form. **Ctrl+V** with nothing
copied still pastes an image or text from the system clipboard as before.

> **The page background is not selectable.** A shape covering almost the whole
> page — a colour tint, a letterhead panel, a full-bleed photograph — is
> treated as the background and ignored when you click empty space. Without
> that rule, clicking a blank margin would grab the background and dragging it
> would slide an opaque sheet over the rest of the page.

**Add text** places a new text box — with the Edit Text tool, clicking empty
space starts one. **Ctrl+H** opens find & replace, which supports regular
expressions and applies across the document in one undo step.

---

## Working with pages

The Pages tab and the thumbnail panel share the same operations. Select one or
more thumbnails (Ctrl/Shift click) then right-click, or use the ribbon:

| Operation | Notes |
| --- | --- |
| Insert / Delete / Duplicate | All undoable |
| Move | Drag thumbnails, or Pages ▸ Move |
| Rotate | 90° steps, applied to the file |
| Crop | Removes a margin from every selected page |
| Resize | Scales content onto a new paper size |
| Extract | Opens the chosen pages as a new document |
| Split | By page count, or at chosen boundaries |
| Merge | Combines files and keeps their bookmarks |
| N-up / Booklet | Imposition for handouts and saddle stitching |
| Page labels | Roman numerals, letters, prefixes |

---

## Comments and markup

Pick a tool from the **Comment** tab, then work on the page:

* **Text markup** — select text, then Highlight, Underline or Strikeout.
* **Sticky note** — click to drop a note and type.
* **Text box / Callout** — free text, optionally with a leader line.
* **Ink** — freehand drawing, ideal with a stylus.
* **Shapes** — rectangle, ellipse, polygon, polyline, arrow, cloud.
* **Stamps** — the fourteen standard stamps, or your own image and text stamps.
* **Measure** — distance, area and perimeter in mm, cm, inches or points.

The **Comments** panel lists everything, filtered by author, type or text.
Right-click a comment to reply, edit, resolve or delete it. Comment sets can be
exported and imported as **XFDF** (interoperable with Acrobat and Foxit) or
**JSON**, which is how review cycles work between reviewers.

**Flatten** merges comments into the page content permanently.

---

## Forms

Create fields from the **Forms** tab: text (single-line, multi-line, comb,
password), check boxes, radio groups, dropdowns, list boxes, date pickers,
signature fields and QR/barcodes.

* **Validate** checks required fields, lengths, dates and numbers.
* **Calculations** evaluate arithmetic over other fields safely — expressions
  are restricted to numbers and operators, so no code can run.
* **Import/export** supports FDF, XFDF, JSON and CSV.
* **Mail-merge** produces one filled PDF per row of data.
* **Flatten** turns filled values into ordinary page content.

---

## Security

### Passwords and permissions

**Protect ▸ Encrypt** offers AES-256 (recommended), AES-128 and legacy RC4.
Set an *open* password to control who can read the file, and an *owner*
password to control what readers may do. Permissions are advisory — enforced by
conforming viewers, not by the format — which the dialog states plainly.

### Redaction

1. **Protect ▸ Mark** and drag over the content, or **Find text to redact**.
2. Review the red marks.
3. **Apply** — the underlying text and image pixels are deleted, not covered.

Redaction is irreversible in the saved file. Verify with **Edit ▸ Find**
afterwards: redacted words genuinely no longer exist.

### Sanitising

**Protect ▸ Sanitise** removes metadata, JavaScript, embedded files, hidden
layers and off-page content — everything that leaks information you did not
intend to publish.

### Signatures

**Protect ▸ Sign** lets you draw, type or import a signature. With a PKCS#12
certificate (and the optional pyHanko package) the signature is
cryptographically verifiable, optionally with an RFC 3161 timestamp.
**Validate** reports integrity, trust and coverage for every signature.

---

## OCR

**Tools ▸ OCR** recognises text in scanned pages and adds an invisible text
layer, so the page looks unchanged but becomes searchable, selectable and
copyable.

Configure the engine, languages and pre-processing in
**Preferences ▸ OCR**. Deskew and denoise are on by default and make the
biggest difference on phone photos and fax-quality scans. After a run you get a
confidence report listing suspect words.

---

## AI assistant

The AI tools work **offline** with no API key:

* **Summarise** — an extractive summary of the document or a page range.
* **Chat with PDF** — questions answered from the text, with page citations.
* **Generate bookmarks** — detects headings by font size and weight.
* **Suggest metadata** — title, subject and keywords.
* **Extract tables** — detected tables exported as CSV.

Configure an OpenAI-compatible endpoint in **Preferences ▸ AI** to add
translation and rewriting. The API key is read from an environment variable and
never written to disk.

---

## Comparing documents

**Tools ▸ Compare files** produces a word-level textual diff plus a pixel diff,
reports which pages changed, and can build a side-by-side PDF with the
differences boxed in red.

---

## Batch processing

**Tools ▸ Batch process** applies a pipeline to many files: OCR, watermark,
compress, encrypt, sanitise, Bates numbering, convert, extract. Outputs are
renamed with tokens such as `{stem}`, `{counter:04d}`, `{date}` and `{bates}`.
One failed file does not stop the run; a per-file report is shown at the end.

The same pipelines are available from the CLI:

```bash
pdfstudio-cli batch inbox/*.pdf -o outbox/ --ops ocr,watermark,compress
```

---

## Accessibility

* **Accessibility ▸ Full check** audits tags, title, language, alt text,
  contrast, scanned pages and form-field descriptions.
* **Fix automatically** applies the safe remediations.
* **Read aloud** speaks the current page or selection.
* The **High contrast** theme and full keyboard navigation are built in; every
  control is reachable with Tab and has an accessible name.

---

## Optimising and exporting

**Convert ▸ Compress** offers Screen, E-book, Print, Prepress and Maximum
profiles. **Analyse size** shows exactly where the bytes are before you commit.

Export to Word, PowerPoint, images (PNG/JPEG/TIFF/WEBP/BMP), SVG, text, HTML,
Markdown, CSV tables, and the archival profiles PDF/A, PDF/X and PDF/UA.

---

## Automation

**Tools ▸ Script console** runs Python inside the application with `doc`,
`pages`, `text`, `annots`, `forms`, `security`, `search` and `ai` already
bound:

```python
for page in range(doc.page_count):
    annots.highlight_text(page, "confidential")
window.refresh_after_edit()
```

Scripts can be saved and re-run as macros. See the
[plugin guide](plugins.md) to package automation as a proper extension.

---

## Keyboard shortcuts

| Shortcut | Action | | Shortcut | Action |
| --- | --- | --- | --- | --- |
| Ctrl+O | Open | | Ctrl+F | Find |
| Ctrl+S | Save | | Ctrl+H | Replace |
| Ctrl+Shift+S | Save as | | Ctrl+A | Select all on page |
| Ctrl+P | Print | | Ctrl+C | Copy selection |
| Ctrl+W | Close tab | | Ctrl+X | Cut object |
| Ctrl+D | Duplicate object | | Ctrl+V | Paste |
| Alt+Up / Alt+Down | Move line up / down | | Ctrl+Shift+P | Edit paragraph |
| Ctrl+Shift+D | Duplicate line | | Ctrl+Shift+K | Delete line |
| Ctrl+U | Underline | | Ctrl+L / E / R | Align left / centre / right |
| M | Move object tool | | F5 | Presentation mode |
| Ctrl+Z / Ctrl+Y | Undo / redo | | F11 | Full screen |
| Ctrl + / Ctrl - | Zoom | | V / H / T | Select / pan / text tool |
| Ctrl+0 / Ctrl+1 | Fit page / width | | Ctrl+Shift+H | Highlight |
| Home / End | First / last page | | Ctrl+Tab | Next tab |
| Space | Scroll down | | Esc | Cancel / exit presentation |

---

## Recovery and autosave

Open documents are autosaved every two minutes (configurable). If PDF Studio
exits unexpectedly, the next launch offers to restore the recovered documents.
Several versions are retained, so you can go back further than the last save.

---

## Troubleshooting

See [troubleshooting.md](troubleshooting.md). Logs live in the cache directory
(**Help ▸ Open log folder**), and `crash.log` contains a shareable report for
bug reports.
