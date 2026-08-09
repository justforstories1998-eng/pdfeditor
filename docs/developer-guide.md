# Developer guide

## Setting up

```bash
git clone https://github.com/pdfstudio/pdfstudio.git
cd pdfstudio
python -m venv .venv && source .venv/bin/activate
pip install -e ".[full,dev]"
pre-commit install
pytest -m "not slow"
```

Headless machines (CI, containers) need Qt's offscreen platform and a few
system libraries:

```bash
export QT_QPA_PLATFORM=offscreen
sudo apt-get install -y libxkbcommon0 libegl1 libgl1 libdbus-1-3 libfontconfig1
```

## Repository layout

```
src/pdfstudio/     the application (see docs/architecture.md)
tests/             unit, integration, GUI and benchmark suites
docs/              this documentation
examples/plugins/  sample plugins
packaging/         installer build scripts
scripts/           developer utilities
samples/           generated sample PDFs for manual testing
```

## Coding standards

* **Python 3.13+**, full type hints, `from __future__ import annotations`.
* **PEP 8** via `ruff format`; 96-column lines.
* **Docstrings** on every public module, class and function. Explain *why*, not
  just what — especially around library work-arounds.
* **Errors**: raise a `PdfStudioError` subclass with an actionable message;
  never let a bare exception reach the UI.
* **Logging**: `get_logger("scope")`, lazy `{}` formatting, no f-strings in log
  calls.
* **Thread safety**: touch a document only through `PdfDocument.locked()`;
  touch widgets only on the GUI thread.

```bash
ruff check src tests
ruff format src tests
mypy src
```

## Testing

| Command | Scope |
| --- | --- |
| `pytest` | everything |
| `pytest -m "not slow"` | fast feedback (default in CI) |
| `pytest -m gui` | Qt widgets, offscreen |
| `pytest -m ocr` | needs Tesseract |
| `pytest -m benchmark -s` | performance report |
| `pytest --cov=src/pdfstudio` | coverage |

The suite is isolated: `conftest.py` points `PDFSTUDIO_HOME` at a temporary
directory, so your real settings, caches and autosaves are never touched.

### Writing tests

* Use the `document`, `blank_document`, `tmp_pdf`, `scanned_pdf` and
  `sample_image` fixtures.
* GUI tests drive the **controller**, not synthetic mouse events:
  `window.controller.page_action("delete", [0])`.
* For anything asynchronous, use the `wait_for(qapp, predicate)` helper rather
  than a fixed sleep.
* Assert on observable behaviour (`"Invoice" not in document.extract_text(0)`),
  not on internal call counts.

## Adding a feature

Worked example — "Add a *Split by bookmarks* command":

1. **Engine.** Implement `PageService.split_by_bookmarks()` in
   `pdfengine/pages.py`, returning new documents. Add unit tests.
2. **Command.** If it mutates the open document, express it as a `Command`
   subclass so undo works.
3. **Controller.** Add a handler in `DocumentController._build_handlers()`
   keyed `pages.split_bookmarks`; collect parameters with a dialog and call the
   service through `_run_job` if it may be slow.
4. **UI.** Add a `RibbonItem` to the Pages tab in `ribbon.py`. Nothing else is
   needed — the menu bar and command palette are generated from the same spec.
5. **CLI.** Add a sub-command in `cli.py` so scripts get it too.
6. **Docs.** Update the user manual and, if it is part of the public API, the
   API reference.

## Performance guidelines

* Never iterate every page for something the user cannot see. `PageView`
  lays out lazily; keep it that way.
* Cache expensive results, but bound the cache **in bytes**, not entries.
* Bump the render generation instead of scanning caches to invalidate.
* Profile with the benchmark suite before and after:
  `pytest -m benchmark -s`.

Current reference numbers (2 vCPU container, 500-page document):

| Operation | Time |
| --- | --- |
| Open | < 1 ms |
| Render one A4 page @100% | ~8 ms |
| Cached render | < 0.1 ms |
| 50 thumbnails | 35 ms |
| Search 500 pages | 81 ms |
| Rotate 500 pages | 38 ms |
| Save 500 pages | 4 ms |

## Debugging

```bash
PDFSTUDIO_LOG_LEVEL=DEBUG pdfstudio          # verbose logging
pdfstudio --safe-mode                        # no plugins, no session restore
pdfstudio --reset-settings                   # factory defaults
QT_QPA_PLATFORM=offscreen pytest -m gui      # reproduce CI locally
```

Logs: `cache/logs/pdfstudio.log` (rotating) and `crash.log` (errors with
tracebacks). `crash_report()` builds a shareable report with versions and
platform details.

## Library quirks worth knowing

These caused real bugs; the work-arounds are commented in the source:

| Library | Behaviour | Work-around |
| --- | --- | --- |
| PyMuPDF | Reading `needs_pass` after `authenticate()` corrupts decryption | Capture the flag once, at open time |
| PyMuPDF | `is_encrypted` becomes `False` after a correct password | Use the snapshot in `PdfDocument.is_encrypted` |
| PyMuPDF | Assigning `""` to a text widget is ignored | Write `/V` directly, then `update()` |
| PyMuPDF | `show_pdf_page` refuses source == target | Stamp from a serialised copy |
| PyMuPDF | `add_redact_annot(fill=None)` defaults to **white** and paints an opaque box | Pass `fill=False` to erase without painting (`content._redact_fill`) |
| PyMuPDF | A zero-height rect can never "cover" line art, so redacting a hairline rule is a no-op | Expand drawing boxes before redacting |
| PyMuPDF | No `copy_annot()`; hand-editing `/Annots` is invisible to the C layer | `page._addAnnot_FromString()`, then `doc._reset_page_refs()` |
| PyMuPDF | `reload_page()` asserts while the `Page` is still referenced | Use `_reset_page_refs()` and re-index the document |
| PyMuPDF | `annot_xrefs()` returns `(xref, type, name)` tuples, not xrefs | Unpack `entry[0]` |
| PyMuPDF | Annotation `/Rect` is bottom-up while the API is top-down | Set it via `Annot.set_rect()`, never by writing the key |
| MuPDF | `get_drawings()` reports `dashes=None` for a solid line | `str(x or "")` — `str(None)` writes the literal `None` into the stream |
| MuPDF ≥ 1.26 | Linearisation removed | Detect and fall back to an optimised save |
| Qt | Style sheets mis-parse `#rrggbbaa` | Convert to `rgba()` in `_css_color` |
| Qt | Timers created off the GUI thread never fire | Queued signals (`call_on_gui_thread`, `GuiInvoker`) |
| Qt | `QListWidget` ignores pixmap size | Call `setIconSize()` |
| Python | An object defining `__len__` is falsy when empty | `x if x is not None else default` |
| Qt | A `QTabWidget` adopts its **widest** tab's width as the minimum for the whole widget, and that propagates to the window | Wrap each ribbon tab in a `QScrollArea` with `setMinimumWidth(0)` |
| Qt | `QClipboard.mimeData()` returns `None` when no clipboard is available (headless, or held by another process) | Guard before calling `hasImage()` |
| Qt | A widget's size hint is measured with the font it had when built; a stylesheet applied later can enlarge the font and elide captions | Override `sizeHint()` and re-measure on `QEvent.FontChange` (`_LargeToolButton`) |
| Qt | `QWidget.close()` only hides a window — a `MainWindow` keeps ~18 child top-levels alive | Call `deleteLater()` too, or they accumulate across a test session |

## Repositioning text exactly

`draw_layout()` places the first baseline using a cap-height approximation
(`ascender * 0.86`). That is right for text being *reflowed*, where the exact
top edge is not observable, but it is a couple of points out when a line has
to land back on a known baseline. For exact repositioning use
`_draw_single_line()`, which derives the baseline from the font's ascender and
descender against the measured line box.

The distinction matters because the error compounds: line swaps built on
`draw_layout` walked a paragraph up the page a few points per swap until
MuPDF stopped reporting it as one block and paragraph selection broke.

## Paragraph grouping

`TextEditor._paragraph_group` decides where a paragraph ends. Two rules, both
learned from bugs:

* measure leading from the **average line height**, never the block height —
  a three-line block reports ~45 pt and then any gap looks like normal
  spacing;
* a font-size jump of more than 15 % ends the paragraph, so a heading is not
  swallowed by the body text beneath it.

Operations that make text wider (list markers, upper case) must widen the
target rectangle via `_column_right_for()`. Otherwise lines wrap, and since
each visual line is one list item, re-numbering turns a wrapped fragment into
its own entry.

## Themes

Test the app **with a theme applied**, the way `app.py` starts it:

```python
themes = ThemeManager()
themes.apply("dark", app)          # before constructing MainWindow
window = MainWindow(theme_manager=themes)
```

Constructing `MainWindow` on its own exercises Qt's default style, which no
user ever sees. That is how the invisible tab strip survived so long.

Palette rules the tests enforce (`TestTabStripIsVisible`):

* `tab_inactive` must never equal `tab_strip`, or unselected tabs vanish;
* `tab_active` must differ from `tab_inactive`;
* tab labels need 4.5:1 contrast against their background.

A control needs an enclosing **shape** — background plus border — not merely
an underline. An underline alone disappears against a flat background.

## Modal dialogs

Never call `QMessageBox.information/warning/question/critical` directly. Use
`MainWindow.inform()`, `warn()` and `confirm()`, which fall back to the status
bar when `PDFSTUDIO_NO_DIALOGS` is set. A raw modal blocks forever when there
is nobody to click it, which makes the surrounding command untestable and
hides any bug behind it — `a11y.autofix` once hung a full-surface sweep
indefinitely. `confirm()` returns **False** by default under suppression, so
unattended runs cannot trigger destructive actions.
`TestDialogsNeverBlock::test_controller_has_no_raw_message_boxes` enforces
this statically.

## Release checklist

1. `pytest` green, including `-m slow`.
2. `ruff check` and `mypy src` clean.
3. Bump `__version__` in `src/pdfstudio/__init__.py` and `pyproject.toml`.
4. Update `CHANGELOG.md`.
5. `python packaging/build.py --target auto` and smoke-test the artefact.
6. Tag `vX.Y.Z`; CI publishes the installers.
