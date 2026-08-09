# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [1.4.0] — 2026-07-31

### Added

- **Edit whole paragraphs.** *Edit ▸ Paragraph ▸ Edit paragraph*
  (**Ctrl+Shift+P**), the right-click *Paragraph* menu, or **Alt+click** any
  text. All the paragraph's lines open in one editor and reflow together.
- **Move a line up or down** with **Alt+Up** / **Alt+Down**, the ribbon, or
  the context menu — the line swaps places with its neighbour, as in Word.
- **Duplicate line** (**Ctrl+Shift+D**) and **delete line**
  (**Ctrl+Shift+K**).
- **Bulleted and numbered lists**: turn any paragraph into a list, renumber
  it, or strip the markers. Existing markers are recognised and replaced
  rather than stacked, and wrapped text hangs under the first word.
- **Change case**: UPPERCASE, lowercase, Title Case and Sentence case.
- **Line spacing**: single, 1.5 or double.
- **Underline and strike-through.** PDF has no such text attribute, so they
  are drawn as hairline rules sized from the font metrics; the text stays
  fully selectable and searchable.
- **Format painter**: *Copy formatting* then *Apply formatting* restyles
  another paragraph to match.
- **Word count** (Edit tab) for the page, the document and any selection.

### Fixed

- **The app opened full screen with no toolbars.** `saveGeometry()` encodes
  the full-screen flag, so once a user pressed **F11** or **F5** the window
  came back full screen on *every* later launch — and because presentation
  mode also hides the ribbon and docks, `saveState()` brought back a window
  with no chrome at all. Size and position are still restored; the immersive
  modes never are. The geometry is also captured in the normal state, so
  leaving full screen no longer restores a screen-sized window.
- **Selecting a paragraph grabbed the whole page.** `_paragraph_group`
  measured line spacing from the *whole block* height, so a three-line
  paragraph reported ~45 pt of "leading" and the gap before the next
  paragraph looked like ordinary spacing. Spacing is now measured per line,
  and a marked font-size change (a heading) ends a paragraph.
- **Repeated line moves walked the paragraph up the page.** `draw_layout`
  places baselines with a cap-height approximation that is a couple of points
  out for an exact reposition; the error accumulated until MuPDF split the
  paragraph into separate blocks. Line swaps now derive the baseline from the
  font's own ascender against the measured line box, so positions are
  preserved exactly.
- Adding a list marker or converting to upper case made lines wider, wrapping
  them — and because each visual line is one item, a second pass numbered the
  wrapped fragment separately ("3. overruns."). Both operations now grow into
  the column.

## [1.3.2] — 2026-07-31

### Fixed

- **The ribbon tab strip was invisible.** Home, Edit, Pages, Comment, Forms,
  Protect, Convert, Tools, View and Accessibility were laid out correctly but
  could not be seen: the `dark` and `high-contrast` palettes set
  `tab_inactive` to exactly the same colour as `surface`, so unselected tabs
  had no edge against the background, and the selected tab's only cue was a
  2 px underline with no enclosing shape. The strip read as a row of loose
  words rather than as tabs. Tabs now have a background, a border, rounded top
  corners and an accent top edge, and sit on a new `tab_strip` rail colour
  that is always distinct from the tabs themselves.
- Every built-in theme now meets a 4.5:1 contrast minimum for tab labels and a
  1.1:1 separation between tab, strip and selection. `sepia`'s muted text was
  4.32:1 against its tabs and has been darkened.
- **"Edit text" rendered as "Edi…ext".** Large ribbon buttons measured their
  minimum width once at construction, but a theme is applied *after* the
  ribbon is built and uses a larger font, so the caption no longer fitted. The
  new `_LargeToolButton` recomputes its size hint from the current font and
  re-measures on every font change.
- Test windows leaked roughly eighteen child top-level widgets each because
  `close()` only hides them; the suite now calls `deleteLater()`, which stops
  widgets accumulating across the session.

### Notes

- `Palette` gained a `tab_strip` field. Existing user theme JSON files are
  unaffected — `Theme.from_dict` ignores unknown keys and falls back to the
  dataclass default for missing ones, which is covered by a test.

## [1.3.1] — 2026-07-30

### Fixed

- **Ribbon commands disappeared on smaller screens.** A tab page's full width
  became its `minimumSizeHint`, and `QTabWidget` reports the widest tab as the
  minimum for the whole bar — so the Edit tab (~1765 px after the Arrange and
  Paragraph groups were added in 1.3.0) imposed a hard floor on the window.
  On a 1568 px display the window could not shrink to fit and Qt clipped the
  right-hand edge, silently removing **Cut** from the Edit group and **Move
  object** from Tools, with no scrollbar to reveal them. Each tab is now
  wrapped in a horizontal `QScrollArea`, dropping the bar's minimum width from
  1769 px to 127 px.
- Dense ribbon groups stack three rows instead of two, which brings the Edit
  tab back under 1584 px so it needs no scrolling at a typical window size.
  The ribbon's height is now measured from its contents (including room for a
  horizontal scrollbar) rather than hard-coded, so adding a command can no
  longer clip a group's bottom row.
- **`a11y.autofix` hung indefinitely** with no user present: it ended in a
  modal `QMessageBox` that ignored `PDFSTUDIO_NO_DIALOGS`. All 28 message
  boxes in the controller now route through `MainWindow.inform/warn/confirm`,
  which fall back to the status bar when dialogs are suppressed. `confirm()`
  defaults to **no**, so an unattended run can never delete pages, flatten
  comments or apply redactions. A static test forbids raw `QMessageBox` calls
  in the controller.
- **Paste crashed when no clipboard was available** (headless sessions, or
  when another process holds the clipboard): `QClipboard.mimeData()` returns
  `None` and `hasImage()` was called on it unguarded. It now reports "Nothing
  to paste".
- `apply_redactions` used a `QMessageBox.warning` as a confirmation prompt and
  compared its return value to `StandardButton.Yes`, which a warning box never
  returns — the prompt could not be answered correctly. It is a proper
  confirmation now.

### Testing

- New `TestRibbonFitsTheWindow` (9 tests) asserts no tab imposes a width
  floor, the window honours a 1100 px size, every command is reachable by
  scrolling, nothing is clipped vertically, and the three specific buttons the
  user reported missing are visible. Six of the nine fail without the fix.
- New `TestEveryCommandSurvivesDispatch` dispatches all 157 ribbon commands
  with and without a document open — the sweep that found the clipboard crash.
- New `TestDialogsNeverBlock` and `TestPasteWithoutAClipboard`.

## [1.3.0] — 2026-07-30

### Added

- **Object clipboard.** Copy, cut, paste and duplicate any page object — text
  block, image, vector rule or comment — with **Ctrl+C / Ctrl+X / Ctrl+V /
  Ctrl+D**, the Arrange group on the Edit tab, or the right-click menu. A
  paste lands offset from the original and is selected immediately so it can
  be dragged into place; **Paste object here** in the context menu drops it
  exactly where you clicked. Each paste is one undo step.
  `ObjectService.copy/cut/paste/duplicate` and `ObjectClip` implement it.
- **Full alignment**: `object.align_left/center/right/top/middle/bottom`, all
  routed through the new `ObjectService.align()` so the CLI, plugins and the
  UI cannot disagree about where "centred" is. With an object selected but no
  editor open, the paragraph *Align* buttons now align the object instead of
  reporting that nothing is being edited.
- Alignment is shown as three toggle buttons on the inline editor's floating
  toolbar (was a drop-down), with **Ctrl+L / Ctrl+E / Ctrl+R** shortcuts and
  **Ctrl+[ / Ctrl+]** for the wrapped-line indent.
- Status-bar hints per tool that stay on screen while the tool is active, a
  caption naming the hovered and selected object, and copy/paste reminders in
  the selection message.

### Fixed

- **Editing or moving content no longer paints a white rectangle over the
  page.** Every redaction passed `fill=(1, 1, 1)`, which stamps an opaque box
  into the content stream — visible as a white patch on tinted pages,
  letterheads and artwork, and something the user then had to clean up. The
  fill is now `False` (erase without painting) unless a background is
  explicitly requested; see `content._redact_fill`.
- **Clicking blank space no longer selects the page background.** A shape
  covering ≥90 % of the page is treated as background and skipped by
  hit-testing (`ObjectService.is_background`). Previously a click on an empty
  margin grabbed the page tint, and dragging it slid an opaque sheet over
  everything else — the "white layer on top of the text".
- **The canvas showed a stale page after an edit.** Two causes: a render
  started before the edit could finish afterwards and overwrite the new image
  (results are now dropped if their generation is older than the renderer's),
  and `GenerationTracker` restarted at 0 each session while the *disk* cache
  persisted, so the second session's cache keys collided with the first
  session's pixels (counters now start from a random base).
- **Reflow tore table rows apart.** Growing a paragraph shifted only the lines
  intersecting the edited column and stopped at the free-space limit, leaving
  row labels behind and the rest of the page overlapping. Whole rows now move,
  all the way to the foot of the page.
- Deleting or cutting a hairline rule did nothing: its zero-height rectangle
  could never "cover" the line art, so the redaction was a no-op. Drawing
  boxes are expanded slightly before deletion, matching `_move_drawing`.
- `DrawingPath.dashes` was the literal string `"None"` for solid lines
  (MuPDF reports `None`; `str()` stringified it), which was written back into
  the content stream and made the parser fail with `unknown keyword: 'None'`.
- The inline editor's alignment and indent only affected the style that would
  be written, never the text on screen, so both looked like they did nothing.
  Rebuilding the style from the toolbar also silently reset the indent
  whenever the font or size changed.

## [1.2.0] — 2026-07-29

### Added

- **Move object tool** (Home ▸ Move object, or **M**). Select any text block,
  image, vector rule or comment and reposition it: drag it, nudge it with the
  arrow keys (2 pt, 0.5 pt with Ctrl, 10 pt with Shift), pull it back to the
  margin with **Backspace**, snap it flush with **Align left**, or remove it
  with **Shift+Delete**. Hovering outlines the object under the cursor and the
  selection shows handles plus a live offset readout. Right-clicking an object
  offers the same actions without switching tools. Each move is one undo step.
  This is the fix for a rule or graphic colliding with a heading.
- **Paragraph controls** on the Edit tab: *Align left / Centre / Align right*
  and *Add indent / Remove indent*, which act on the text being edited.
  *Remove indent* pulls a wrapped continuation line back to the left margin.
- `TextStyle.first_line_indent` and `TextStyle.wrap_indent`, honoured by both
  the wrapper and the renderer, so indented paragraphs still wrap in-column.
- New `pdfstudio.pdfengine.objects` module: `ObjectService`, `PageObject`,
  and undoable move/delete commands for every kind of page content.

### Fixed

- `Page.delete_image()` substitutes a blank stub instead of removing the
  placement, so moving or deleting an image left a ghost behind and the image
  count grew on every move. Both paths now redact with `PDF_REDACT_IMAGE_REMOVE`.
- Repeated nudges silently did nothing after the first: the selected object
  kept a stale payload describing its *old* geometry, so each move replayed the
  original coordinates and could duplicate the object. Objects are now
  re-resolved from the rewritten page after every move.
- Two ribbon buttons mapping to the same tool could not both show as active,
  because their exclusive `QButtonGroup` unchecked the sibling. Selecting a
  tool now syncs every button that maps to it.

## [1.1.1] — 2026-07-29

### Fixed

- **"Edit text" on the Edit tab did nothing.** The ribbon has two buttons
  labelled *Edit text* — the tool toggle on Home and a second one on the Edit
  tab. Only the first was wired up, so anyone reaching for the obvious one on
  the Edit tab saw no editor at all and concluded that text editing was
  broken. Both now arm the same tool.
- Audited the whole ribbon for the same class of defect: **12 of 142 commands
  had no handler** and silently reported "not available yet". All are now
  implemented — *Edit text*, *Bold*, *Italic*, *Colour*, *Move pages*,
  *Certificates*, *From scanner*, *OCR settings*, *Record macro*, *Tag
  editor*, *Reading order* and *Alternative text*. A regression test now fails
  the build if any ribbon command loses its handler.
- The context menu's *Edit text here* only armed the tool instead of editing
  the text that was right-clicked; it is now *Edit this line* / *Edit this
  paragraph* and acts immediately.

### Added

- Double-click opens the in-place editor from the Select, Pan and Text tools
  too, not only the Edit Text tool.
- Hovering editable text with the Edit Text or Select tool shows an I-beam and
  outlines the line that a click will edit.

## [1.1.0] — 2026-07-29

### Added

- **Direct on-page text editing.** Clicking text with the Edit Text tool (or
  double-clicking with Select) now opens an editor *on the page itself*,
  matching the original font, size and colour, with the caret placed where you
  clicked. The modal dialog is gone. Enter applies, Shift+Enter breaks the
  line, Esc cancels, and clicking elsewhere commits and moves the edit there.
- A floating format bar for font, size, bold, italic, colour and alignment,
  previewing live as you type.
- `wrap_text()`, `fit_text()` and `draw_layout()` in the content engine, plus
  `TextEditor.line_at()`, `block_at()` and `edit_region()`.

### Fixed

- **Editing one line pulled in the whole paragraph.** Hit-testing matched the
  text *block*, so every sibling line was loaded into the editor and rewritten.
  Editing now resolves the single line under the cursor; Alt-click still edits
  the whole paragraph.
- **Long replacements ran off the page.** Text was drawn with one unbroken
  `insert_text()` call. Replacements are now word-wrapped to the column width,
  grow into the free space below, and scale the font down only as a last
  resort. Words longer than the line are split rather than overflowing.
- **Edited paragraphs overlapped the text beneath them.** When a replacement
  needs more lines than it replaced, the following lines in the column are now
  shifted down first.
- **Edits did not appear until the page was re-opened.** Invalidating a single
  page cleared the memory cache but not the disk cache, so the stale PNG was
  loaded straight back. The render generation is now always bumped.
- The inline editor's font was overridden by the theme's global
  `QWidget { font-size }` rule, and its height was computed from
  `QPlainTextEdit.document().size()`, which reports lines rather than pixels.
- A flaky test asserted that every randomly generated password scores ≥ 90;
  about 8% legitimately score 85 when no symbol is drawn.

### Changed

- Refreshed the interface: deeper, layered surfaces with a clearer hierarchy,
  underline-style tabs, softer multi-layer page shadows, rounded page-number
  pills, roomier inputs and list rows, thinner scrollbars, and a skeleton
  placeholder while a page rasterises.

## [1.0.0] — 2026-07-28

First production release.

### Added

**Viewing** — virtualised canvas for 100 000+ page documents; single,
continuous, facing and book layouts; presentation and full-screen modes; pinch,
wheel and marquee zoom; tiled rendering at high magnification; bounded memory
and persistent disk caches; page prefetching; multi-tab and multi-window
sessions with restore.

**Editing** — in-place text editing preserving font, size and colour;
rich-text boxes; find & replace with regular expressions; image insert,
replace, crop, resample and full pixel adjustment; vector drawing with
gradients, dashes and blend modes; SVG import.

**Pages** — insert, delete, move, rotate, crop, resize, duplicate, extract,
replace, split (by count, boundary or bookmark), merge, N-up, booklet
imposition and page labels — all undoable.

**Comments** — the complete Acrobat annotation set including ink, clouds,
callouts, stamps and measurements; threaded replies and review states;
filtering by author, type and text; XFDF and JSON interchange; flattening.

**Forms** — every AcroForm widget including radio groups, date pickers,
signature fields and QR/barcodes; validation; sandboxed calculations;
FDF/XFDF/JSON/CSV data; mail-merge; flattening.

**Security** — AES-256/128 and RC4 encryption; permission control; true
redaction that deletes content; sanitisation; text and image watermarks; Bates
numbering; headers and footers; PKCS#12 signatures with timestamps and
validation through pyHanko.

**OCR** — Tesseract, EasyOCR and MuPDF back-ends; deskew, denoise,
binarisation and border removal; invisible searchable text layer aligned to
the recognised word boxes; confidence reporting; batch operation.

**AI** — fully offline extractive summaries, BM25 question answering with page
citations, keyword tagging, heading-based bookmark generation, metadata
suggestions, table extraction, grammar checks and citation formatting;
optional OpenAI-compatible endpoint for translation and rewriting.

**Conversion** — import from DOCX, PPTX, XLSX, RTF, HTML, Markdown, EPUB, CBZ,
ZIP archives, images, HEIC and camera RAW; export to DOCX, PPTX, PNG, JPEG,
TIFF, WEBP, BMP, SVG, text, HTML, Markdown, CSV tables and the PDF/A, PDF/X and
PDF/UA profiles.

**Automation** — batch pipelines with renaming templates; a complete CLI with
JSON output and meaningful exit codes; an embedded Python scripting console;
and a plugin API with sandboxing, permissions and hot reload.

**Quality of life** — unlimited undo with merging, macros and snapshots;
autosave with crash recovery; document comparison with a side-by-side report;
accessibility checker with automatic remediation; size analysis and five
optimisation profiles; four built-in themes plus user themes; dockable,
persistent layouts.

### Fixed during development

Defects found by the test-suite and fixed before release, recorded because they
are easy to reintroduce:

- Encrypted documents silently returned empty text after authentication —
  reading PyMuPDF's `needs_pass` post-authentication corrupts the decryption
  state. The flag is now captured once, at open time.
- `is_encrypted` reported `False` for protected files once the password had
  been accepted.
- Clearing a text form field was ignored by PyMuPDF; `/V` is now written
  directly before regenerating the appearance.
- Radio groups could not be created at all (PyMuPDF raised `bad xref`); they
  are now built as a proper parent field with kid widgets.
- Job results were silently dropped: `QTimer.singleShot` never fires on a
  thread without an event loop, so search, OCR and export appeared to hang.
  Worker results now cross to the GUI thread through queued signals.
- Qt style sheets mis-parsed 8-digit hex colours and rendered them bright
  yellow; translucent tokens are converted to `rgba()`.
- Thumbnails were rendered at 32 px regardless of the pixmap because
  `setIconSize()` was never called.
- Ribbon tool buttons were not mutually exclusive.
- An injected but empty render cache was discarded because `MemoryCache`
  defines `__len__` and was therefore falsy.
- Bates numbers anchored right or centre overflowed the page edge.
- The comments panel's author and type filters were never populated.
- The main window did not implement the plugin host protocol, so every plugin
  command failed.
- MuPDF 1.26 removed linearisation; saving with "optimise for web" raised
  instead of degrading gracefully.
- Each search hit on a page repeated the first match's context snippet.

[1.0.0]: https://github.com/pdfstudio/pdfstudio/releases/tag/v1.0.0
