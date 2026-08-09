"""``python -m pdfstudio`` — launches the GUI, or the CLI with a subcommand."""

from __future__ import annotations

import sys

#: Sub-commands that belong to the head-less CLI rather than the GUI.
_CLI_COMMANDS = {
    "info",
    "merge",
    "split",
    "extract",
    "convert",
    "ocr",
    "compress",
    "encrypt",
    "decrypt",
    "watermark",
    "search",
    "compare",
    "batch",
    "text",
    "images",
    "forms",
    "sanitize",
    "rotate",
    "delete-pages",
    "bates",
}


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in _CLI_COMMANDS:
        from pdfstudio.cli import main as cli_main

        return cli_main(argv)
    from pdfstudio.app import main as gui_main

    return gui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
