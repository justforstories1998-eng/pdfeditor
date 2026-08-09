# Architecture

## Design goals

1. **The UI must never block.** Anything that can take longer than a frame runs
   on a worker and reports progress through the event bus.
2. **Qt is an implementation detail.** Only `pdfstudio.ui` imports PySide6, so
   the same operations power the GUI, the CLI, plugins and tests.
3. **Every change is undoable.** Mutations are `Command` objects on a
   per-document stack, with merging, macros and snapshots.
4. **Failures are contained.** A bad plugin, a corrupt page or a missing
   optional dependency degrades one feature, never the application.

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ ui/            MainWindow · PageView · panels · dialogs      │  Qt
│                DocumentController (commands → services)      │
├──────────────────────────────────────────────────────────────┤
│ services/      history · autosave/recovery · batch           │
│ plugins/       API · manager · sandbox · built-ins           │  no Qt
│ ai/  ocr/      document intelligence · recognition           │
│ render/        rasteriser · tiles · memory & disk caches     │
│ pdfengine/     document · pages · content · annotations ·    │
│                forms · security · search · convert · optimize│
├──────────────────────────────────────────────────────────────┤
│ core/          settings · logging · events · undo · jobs     │  no Qt, no PDF
└──────────────────────────────────────────────────────────────┘
```

Dependencies point **downwards only**. `core` knows nothing about PDFs;
`pdfengine` knows nothing about Qt; `ui` knows nothing about MuPDF internals.

## Command flow

A click travels through exactly one path:

```
RibbonBar.command("pages.delete")
        ↓
MainWindow.dispatch()            resolves tools, local actions, plugins
        ↓
DocumentController.execute()     gathers parameters, picks the service
        ↓
PageService.delete()             builds a DeletePagesCommand
        ↓
UndoStack.push()                 executes it, publishes UNDO_STACK_CHANGED
        ↓
MainWindow.refresh_after_edit()  invalidates caches, reloads panels
```

Because the controller is a plain object, tests call
`controller.page_action("delete", [0])` directly — no synthetic mouse events.

## Threading model

| Thread | Responsibility |
| --- | --- |
| GUI | All widget access, painting, dialogs |
| `pdfstudio-io` pool | Rendering, search, export, OCR, batch work |
| Process pool | Optional CPU-heavy work that pickles cleanly |
| Autosave timer | Periodic snapshots of modified documents |

**MuPDF is not thread-safe.** Every document owns an `RLock`; all access goes
through `PdfDocument.locked()`. Render workers hold the lock only while
rasterising a page, so the GUI keeps repainting.

Results must cross back to the GUI thread. `QTimer.singleShot` is *not* usable
for this — a timer created on a thread without an event loop never fires — so
`DocumentController` exposes a queued signal (`call_on_gui_thread`) and dialogs
use `GuiInvoker`. This was a real defect caught by the GUI tests.

## Rendering pipeline

```
PageView.paintEvent
   → visible_pages()                  virtualised: only what is on screen
   → PageRenderer.request()           returns a cached page or a Job
        → MemoryCache (bytes-bounded LRU)
        → DiskCache   (PNG, survives restarts)
        → MuPDF rasterisation under the document lock
   → callback marshalled to the GUI thread → QImage → drawImage
```

* Keys include a **generation counter**, so one edit invalidates every cached
  tile for that document without scanning the cache.
* Above roughly 24 MB per page the renderer switches to **tiles** so memory
  stays flat at extreme zoom.
* `prefetch()` warms the pages around the viewport during idle time.

## Undo strategy

Three techniques, chosen per operation:

| Technique | Used for | Cost |
| --- | --- | --- |
| Inverse operation | rotation, field values, layer visibility | O(1) |
| Page snapshot (`PageSnapshotCommand`) | content edits, annotations, forms | O(page) |
| Document snapshot | page resizing, structural rewrites | O(document) |

Snapshots are serialised MuPDF pages, so restoring is exact — content streams,
resources, annotations and form fields all come back. `UndoStack` accounts for
the retained bytes and drops the oldest entries when the budget is exceeded.

## Event bus

`pdfstudio.core.events` is a small synchronous pub/sub with **weak references
to bound methods**, so a closed tab unsubscribes itself. The engine publishes
domain events (`DOCUMENT_MODIFIED`, `JOB_PROGRESS`, `ANNOTATION_ADDED`); the UI
adapts them to Qt signals. This keeps the engine importable without Qt while
still letting the UI react to everything.

## Error handling

```
PdfStudioError
├── DocumentError
│   ├── PasswordRequiredError     → the UI prompts for a password
│   └── PermissionDeniedError     → explains which permission is missing
├── RenderError, OcrError, PluginError
├── DependencyMissingError        → names the pip package to install
└── ValidationError               → bad user input
```

The controller catches `PdfStudioError` and shows a dialog; anything else is
logged with a full traceback and reported as an unexpected error, so the window
survives.

## Persistence

| What | Where | Format |
| --- | --- | --- |
| Preferences | `config/settings.json` | JSON, atomic writes, typed dataclasses |
| Recent files, sessions, stamps | `data/pdfstudio.sqlite3` | SQLite (WAL) |
| Render cache | `cache/render/` | PNG, LRU-trimmed |
| Autosave | `data/autosave/` | PDF + JSON manifest |
| Logs | `cache/logs/` | rotating, zipped, plus `crash.log` |

Set `PDFSTUDIO_HOME` to relocate all of it — that is how the tests isolate
themselves and how portable installations work.

## Known platform quirks

Documented because they caused real bugs:

* **`needs_pass` after `authenticate()`** corrupts decryption in PyMuPDF 1.28 —
  the encryption state is captured once, at open time.
* **`is_encrypted` flips to `False`** after a successful password, so it cannot
  be used to answer "is this file protected?".
* **Qt style sheets reject 8-digit hex** (`#rrggbbaa`) and render it yellow;
  `_css_color()` converts translucent tokens to `rgba()`.
* **MuPDF 1.26+ removed linearisation**; `SaveOptions` detects this and falls
  back to an optimised save with a warning.
* **`QListWidget` ignores pixmap size** unless `setIconSize()` is called, which
  silently reduced every thumbnail to 32 px.
* **Redactions paint white by default.** `add_redact_annot()` treats `fill=None`
  as white and stamps an opaque rectangle into the content stream. Every edit
  used to do this, which left a white patch on any page that was not plain
  white. `fill=False` erases without painting; see `content._redact_fill`.
* **Render results can arrive out of order.** A rasterisation started before an
  edit can complete after it and overwrite the new image, so `PageView`
  captures the render generation in the callback and discards anything older
  than the renderer's current value.
* **The disk cache outlives the process.** `GenerationTracker` therefore starts
  its counters from a random base: two sessions both starting at 0 produced
  identical cache keys for different page states, and the second session
  served the first session's pixels.

## Page objects and the background

`pdfengine/objects.py` reconstructs a movable-object model over PDF's flat
stream of drawing operators. One rule is worth calling out: a shape covering
90 % or more of the page (`ObjectService.BACKGROUND_COVERAGE`) is classed as
the page *background* and excluded from hit-testing unless asked for
explicitly. Page tints, letterhead panels and full-bleed images are ordinary
drawings as far as PDF is concerned, so without this a click on an empty
margin selected the background and dragging it covered the whole page.

Copying is mediated by `ObjectClip`, a self-contained snapshot (image bytes,
per-line text and styles, the vector path, or the raw annotation dictionary).
A `PageObject` cannot serve as a clipboard entry because its `payload` holds
extraction records that go stale the moment the page is rewritten — and the
source object may be deleted by a cut before the paste happens.
