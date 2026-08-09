# Writing plugins

A plugin is a single `.py` file (or a package) that subclasses `Plugin` and
exposes it as `PLUGIN`. Drop it in the plugin folder — **Help ▸ Open plugin
folder**, or `Preferences ▸ Plugins ▸ Open plugin folder` — and it is loaded at
start-up, or immediately via **Plugins ▸ Rescan**.

## Minimal plugin

```python
from pdfstudio.plugins.api import Plugin, PluginContext, PluginMetadata


class HelloPlugin(Plugin):
    metadata = PluginMetadata(
        identifier="com.example.hello",   # must be globally unique
        name="Hello",
        version="1.0.0",
        description="Adds a greeting command",
        author="Example Ltd",
    )

    def activate(self, context: PluginContext) -> None:
        context.register_command("greet", "Say hello", self.greet)

    def greet(self, context: PluginContext) -> None:
        document = context.document()
        name = document.display_name if document else "nobody"
        context.notify(f"Hello from {name}!")


PLUGIN = HelloPlugin
```

The command appears on the **Plugins** ribbon tab and in the command palette.

## The context

`PluginContext` is the plugin's whole view of the application. Everything it
registers is removed automatically when the plugin is disabled or reloaded.

| Method | Purpose |
| --- | --- |
| `register_command(id, title, handler, **kw)` | Menu/ribbon command; supports `shortcut`, `icon`, `menu`, `enabled_when` |
| `register_tool(id, title, ...)` | A canvas tool with press/move/release handlers |
| `register_format(id, title, extensions, handler, direction=)` | A new import or export format |
| `subscribe(topic, handler)` | Listen to application events |
| `publish(topic, payload)` | Emit your own events |
| `document()` | The active `PdfDocument`, or `None` |
| `notify(message, level=)` | Status-bar message or error box |
| `run_in_background(name, fn, *args)` | Off-thread work with progress |
| `push_undo(document, command)` | Register an undoable change |
| `data_path(*parts)` | A private writable directory for your plugin |
| `log` | A logger tagged with your plugin id |

## Making changes undoable

Never mutate a document directly — wrap the change in a command so the user can
undo it:

```python
from pdfstudio.pdfengine.content import PageSnapshotCommand


class StampEveryPage(PageSnapshotCommand):
    """Snapshot-based commands restore the page exactly on undo."""

    def apply(self) -> None:
        with self.doc.locked() as handle:
            handle[self.page].insert_text((72, 72), "REVIEWED", fontsize=14)


def stamp(context):
    document = context.document()
    with document.undo_stack.macro("Stamp all pages"):
        for page in range(document.page_count):
            context.push_undo(document, StampEveryPage(document, page, "Stamp"))
    context.notify(f"Stamped {document.page_count} pages")
```

For simple changes, `FunctionCommand("label", do, undo)` is enough.

## Reacting to events

```python
from pdfstudio.plugins.api import Topic, hook


class AuditPlugin(Plugin):
    ...

    @hook(Topic.DOCUMENT_OPENED)
    def on_opened(self, event) -> None:
        self.context.log.info("Opened {}", event.get("path"))

    @hook(Topic.DOCUMENT_SAVED)
    def on_saved(self, event) -> None:
        ...
```

Useful topics: `DOCUMENT_OPENED`, `DOCUMENT_SAVED`, `DOCUMENT_MODIFIED`,
`PAGE_CHANGED`, `PAGES_MUTATED`, `ANNOTATION_ADDED`, `SEARCH_RESULTS`,
`JOB_PROGRESS`, `THEME_CHANGED`.

## Long-running work

Never block the GUI thread:

```python
def analyse(self, context):
    def work(ctx):
        document = context.document()
        ctx.set_total(document.page_count)
        counts = []
        for page in range(document.page_count):
            ctx.raise_if_cancelled()
            counts.append(len(document.extract_text(page).split()))
            ctx.progress(page + 1, f"Page {page + 1}")
        return sum(counts)

    job = context.run_in_background("Counting words", work, with_context=True)
    job.add_done_callback(
        lambda j: context.notify(f"{j.result()} words")
    )
```

Progress appears in the status bar automatically, with a working Cancel button.

## Contributing a format

```python
context.register_format(
    "csv-report", "Word counts (*.csv)", ["csv"], self.export_csv, direction="export"
)
```

Your handler receives the context and a destination path; the format then
appears in the Export menu and in the batch dialog.

## Permissions and the sandbox

By default plugins may not import risky modules. Declare what you need:

```python
metadata = PluginMetadata(
    identifier="com.example.sync",
    name="Cloud sync",
    permissions=["network"],       # network | process | native | filesystem
)
```

The Plugin Manager shows requested permissions before a plugin is enabled. The
sandbox protects users from *careless* plugins; it is not a security boundary
against hostile code, and the UI says so. Turn it off in
**Preferences ▸ Plugins** if you are developing.

## Development workflow

1. Write your plugin in the plugin folder.
2. **Tools ▸ Plugins** → select it → **Reload** after each change
   (or enable **Preferences ▸ Plugins ▸ Reload when files change**).
3. Errors appear in the details pane and in `pdfstudio.log`.

Test head-lessly, without the GUI:

```python
from pdfstudio.plugins.api import PluginContext
from pdfstudio.pdfengine.document import PdfDocument
from my_plugin import MyPlugin


def test_command():
    document = PdfDocument.create()

    class Host:
        def active_document(self): return document
        def open_document(self, path): return PdfDocument.open(path)
        def notify(self, message, *, level="info"): print(message)
        def ask(self, question, options): return options[0]

    plugin = MyPlugin()
    context = PluginContext(plugin.metadata, host=Host())
    plugin.activate(context)
    plugin.greet(context)
```

## Distribution

Ship a single `.py` file, or publish a package with an entry point:

```toml
[project.entry-points."pdfstudio.plugins"]
my_plugin = "my_package.plugin:MyPlugin"
```

Installed distributions are discovered automatically.

## Examples

* [`examples/plugins/word_frequency.py`](../examples/plugins/word_frequency.py)
  — commands, a contributed export format and an event hook.
* `src/pdfstudio/plugins/builtin/page_numbers.py` — shipping a simple command.
* `src/pdfstudio/plugins/builtin/quick_redact.py` — document analysis, pattern
  matching with validation, and bulk undoable annotation.
