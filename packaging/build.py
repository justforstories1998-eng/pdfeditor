#!/usr/bin/env python3
"""Build installers and portable bundles for every supported platform.

    python packaging/build.py --target auto        # native package
    python packaging/build.py --target onefile     # single executable
    python packaging/build.py --target appimage    # Linux AppImage
    python packaging/build.py --target deb         # Debian package
    python packaging/build.py --target msi         # Windows installer
    python packaging/build.py --target dmg         # macOS disk image

Every target first produces a PyInstaller bundle, then wraps it in the
platform's native container. Missing platform tools are reported clearly
instead of failing half-way.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "PDF Studio"
BINARY_NAME = "pdfstudio"


def _version() -> str:
    text = (SRC / "pdfstudio" / "__init__.py").read_text("utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "0.0.0"


VERSION = _version()


def run(command: list[str], **kwargs: object) -> None:
    """Run a command, echoing it first."""
    print(f"  $ {' '.join(str(c) for c in command)}")
    subprocess.run(command, check=True, **kwargs)  # noqa: S603


def require(tool: str, hint: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise SystemExit(f"error: {tool!r} is required for this target.\n       {hint}")
    return path


def clean() -> None:
    for directory in (DIST, BUILD):
        shutil.rmtree(directory, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
        spec.unlink()


# --------------------------------------------------------------------------- #
# PyInstaller
# --------------------------------------------------------------------------- #
HIDDEN_IMPORTS = [
    "pymupdf", "pikepdf", "PIL", "PIL.Image", "PIL.ImageEnhance", "PIL.ImageFilter",
    "numpy", "loguru", "sqlite3",
    "pdfstudio.plugins.builtin.page_numbers",
    "pdfstudio.plugins.builtin.quick_redact",
]
EXCLUDES = [
    "tkinter", "matplotlib", "pytest", "IPython", "notebook", "jupyter",
    "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtQuick3D", "PySide6.QtMultimedia",
]


def pyinstaller(*, onefile: bool, windowed: bool = True) -> Path:
    """Produce a PyInstaller bundle and return its path."""
    require("pyinstaller", "pip install pyinstaller")
    icon = ROOT / "src" / "pdfstudio" / "resources" / "icons"
    icon_file = {
        "Windows": icon / "pdfstudio.ico",
        "Darwin": icon / "pdfstudio.icns",
    }.get(platform.system(), icon / "pdfstudio.png")

    command = [
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--name", BINARY_NAME,
        "--onefile" if onefile else "--onedir",
        "--paths", str(SRC),
        "--collect-submodules", "pdfstudio",
        "--collect-data", "pdfstudio",
        str(SRC / "pdfstudio" / "app.py"),
    ]
    if windowed:
        command.insert(1, "--windowed")
    if icon_file.exists():
        command += ["--icon", str(icon_file)]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for module in EXCLUDES:
        command += ["--exclude-module", module]

    resources = SRC / "pdfstudio" / "resources"
    if resources.exists():
        separator = ";" if platform.system() == "Windows" else ":"
        command += ["--add-data", f"{resources}{separator}pdfstudio/resources"]

    print(f"→ Building {APP_NAME} {VERSION} with PyInstaller")
    run(command, cwd=ROOT)
    produced = DIST / (BINARY_NAME + (".exe" if platform.system() == "Windows" else ""))
    return produced if onefile else DIST / BINARY_NAME


# --------------------------------------------------------------------------- #
# Linux
# --------------------------------------------------------------------------- #
def build_appimage() -> Path:
    """Wrap the bundle in a self-contained AppImage."""
    tool = shutil.which("appimagetool")
    bundle = pyinstaller(onefile=False)
    appdir = BUILD / "PDFStudio.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "share" / "applications").mkdir(parents=True)
    (appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps").mkdir(parents=True)

    shutil.copytree(bundle, appdir / "usr" / "bin" / BINARY_NAME, dirs_exist_ok=True)
    desktop = _desktop_entry()
    (appdir / f"{BINARY_NAME}.desktop").write_text(desktop, "utf-8")
    (appdir / "usr" / "share" / "applications" / f"{BINARY_NAME}.desktop").write_text(
        desktop, "utf-8"
    )
    icon = SRC / "pdfstudio" / "resources" / "icons" / "pdfstudio.png"
    if icon.exists():
        shutil.copy(icon, appdir / f"{BINARY_NAME}.png")
        shutil.copy(
            icon,
            appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
            / f"{BINARY_NAME}.png",
        )
    runner = appdir / "AppRun"
    runner.write_text(
        "#!/bin/sh\n"
        'HERE="$(dirname "$(readlink -f "$0")")"\n'
        f'exec "$HERE/usr/bin/{BINARY_NAME}/{BINARY_NAME}" "$@"\n',
        "utf-8",
    )
    runner.chmod(0o755)

    target = DIST / f"{APP_NAME.replace(' ', '')}-{VERSION}-x86_64.AppImage"
    if tool is None:
        print(
            "warning: appimagetool not found — the AppDir is ready at\n"
            f"         {appdir}\n"
            "         Install appimagetool to produce the .AppImage."
        )
        return appdir
    run([tool, str(appdir), str(target)])
    return target


def build_deb() -> Path:
    """Build a .deb package (needs dpkg-deb)."""
    require("dpkg-deb", "Install dpkg (Debian/Ubuntu) to build .deb packages.")
    bundle = pyinstaller(onefile=False)
    staging = BUILD / "deb"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "DEBIAN").mkdir(parents=True)
    (staging / "opt").mkdir(parents=True)
    (staging / "usr" / "bin").mkdir(parents=True)
    (staging / "usr" / "share" / "applications").mkdir(parents=True)

    shutil.copytree(bundle, staging / "opt" / BINARY_NAME, dirs_exist_ok=True)
    (staging / "DEBIAN" / "control").write_text(
        f"""Package: {BINARY_NAME}
Version: {VERSION}
Section: graphics
Priority: optional
Architecture: amd64
Depends: libxkbcommon0, libegl1, libgl1, libfontconfig1
Recommends: tesseract-ocr, libreoffice, ghostscript
Maintainer: PDF Studio Project <maintainers@pdfstudio.example>
Description: {APP_NAME} — professional PDF editor
 Edit, annotate, sign, OCR, compare and convert PDF documents.
 Includes a full command-line interface and a plugin system.
""",
        "utf-8",
    )
    launcher = staging / "usr" / "bin" / BINARY_NAME
    launcher.write_text(f"#!/bin/sh\nexec /opt/{BINARY_NAME}/{BINARY_NAME} \"$@\"\n", "utf-8")
    launcher.chmod(0o755)
    (staging / "usr" / "share" / "applications" / f"{BINARY_NAME}.desktop").write_text(
        _desktop_entry(), "utf-8"
    )

    target = DIST / f"{BINARY_NAME}_{VERSION}_amd64.deb"
    run(["dpkg-deb", "--build", "--root-owner-group", str(staging), str(target)])
    return target


def _desktop_entry() -> str:
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
GenericName=PDF Editor
Comment=Edit, annotate, sign and convert PDF documents
Exec={BINARY_NAME} %F
Icon={BINARY_NAME}
Terminal=false
Categories=Office;Graphics;Viewer;
MimeType=application/pdf;application/x-pdf;
Keywords=pdf;editor;annotate;ocr;sign;form;
StartupWMClass={BINARY_NAME}
"""


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def build_msi() -> Path:
    """Build a Windows installer with Inno Setup (or fall back to a zip)."""
    bundle = pyinstaller(onefile=False)
    iscc = shutil.which("iscc") or shutil.which("ISCC.exe")
    if iscc is None:
        print("warning: Inno Setup (iscc) not found — producing a portable zip instead.")
        return build_portable()

    script = BUILD / "installer.iss"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"""; Generated by packaging/build.py
[Setup]
AppName={APP_NAME}
AppVersion={VERSION}
AppPublisher=PDF Studio Project
DefaultDirName={{autopf}}\\{APP_NAME}
DefaultGroupName={APP_NAME}
OutputDir={DIST}
OutputBaseFilename=PDFStudio-{VERSION}-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
ChangesAssociations=yes

[Files]
Source: "{bundle}\\*"; DestDir: "{{app}}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{{group}}\\{APP_NAME}"; Filename: "{{app}}\\{BINARY_NAME}.exe"
Name: "{{autodesktop}}\\{APP_NAME}"; Filename: "{{app}}\\{BINARY_NAME}.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "associate"; Description: "Open PDF files with {APP_NAME}"

[Registry]
Root: HKA; Subkey: "Software\\Classes\\.pdf\\OpenWithProgids"; \
ValueType: string; ValueName: "PDFStudio.Document"; ValueData: ""; \
Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\\Classes\\PDFStudio.Document"; \
ValueType: string; ValueName: ""; ValueData: "PDF Document"; \
Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\\Classes\\PDFStudio.Document\\shell\\open\\command"; \
ValueType: string; ValueName: ""; ValueData: """"{{app}}\\{BINARY_NAME}.exe"" ""%1"""; \
Tasks: associate

[Run]
Filename: "{{app}}\\{BINARY_NAME}.exe"; Description: "Launch {APP_NAME}"; \
Flags: nowait postinstall skipifsilent
""",
        "utf-8",
    )
    run([iscc, str(script)])
    return DIST / f"PDFStudio-{VERSION}-setup.exe"


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #
def build_dmg() -> Path:
    """Build a macOS .app bundle inside a disk image."""
    if platform.system() != "Darwin":
        raise SystemExit("error: DMG images can only be built on macOS.")
    pyinstaller(onefile=False)
    app_bundle = DIST / f"{BINARY_NAME}.app"
    if not app_bundle.exists():
        raise SystemExit("error: PyInstaller did not produce an .app bundle.")

    target = DIST / f"PDFStudio-{VERSION}.dmg"
    target.unlink(missing_ok=True)
    create_dmg = shutil.which("create-dmg")
    if create_dmg:
        run([
            create_dmg, "--volname", APP_NAME, "--window-size", "640", "400",
            "--icon-size", "110", "--icon", f"{BINARY_NAME}.app", "160", "180",
            "--app-drop-link", "460", "180", str(target), str(app_bundle),
        ])
    else:
        require("hdiutil", "hdiutil ships with macOS.")
        run([
            "hdiutil", "create", "-volname", APP_NAME,
            "-srcfolder", str(app_bundle), "-ov", "-format", "UDZO", str(target),
        ])
    return target


def sign_macos(identity: str) -> None:
    """Code-sign and notarise a macOS bundle."""
    app_bundle = DIST / f"{BINARY_NAME}.app"
    require("codesign", "Xcode command line tools are required.")
    run([
        "codesign", "--deep", "--force", "--options", "runtime",
        "--sign", identity, str(app_bundle),
    ])
    print("→ Signed. Notarise with:")
    print(f"    xcrun notarytool submit {DIST}/PDFStudio-{VERSION}.dmg "
          "--keychain-profile AC_PASSWORD --wait")
    print(f"    xcrun stapler staple {DIST}/PDFStudio-{VERSION}.dmg")


# --------------------------------------------------------------------------- #
# Portable
# --------------------------------------------------------------------------- #
def build_portable() -> Path:
    """A zip that runs from a USB stick, keeping all state beside itself."""
    bundle = pyinstaller(onefile=False)
    marker = Path(bundle) / "portable.txt"
    marker.write_text(
        "The presence of this file makes PDF Studio store its settings, cache\n"
        "and autosaves in a 'data' folder next to the executable.\n",
        "utf-8",
    )
    system = platform.system().lower()
    archive = DIST / f"PDFStudio-{VERSION}-{system}-portable"
    result = shutil.make_archive(str(archive), "zip", root_dir=bundle.parent, base_dir=bundle.name)
    return Path(result)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} packages")
    parser.add_argument(
        "--target",
        default="auto",
        choices=[
            "auto", "onedir", "onefile", "portable",
            "appimage", "deb", "msi", "dmg",
        ],
    )
    parser.add_argument("--clean", action="store_true", help="remove build output first")
    parser.add_argument("--sign", default="", help="macOS signing identity")
    args = parser.parse_args()

    if args.clean:
        clean()
    DIST.mkdir(exist_ok=True)

    target = args.target
    if target == "auto":
        target = {"Linux": "appimage", "Windows": "msi", "Darwin": "dmg"}.get(
            platform.system(), "onedir"
        )

    print(f"→ Target: {target} ({platform.system()} {platform.machine()})")
    builders = {
        "onedir": lambda: pyinstaller(onefile=False),
        "onefile": lambda: pyinstaller(onefile=True),
        "portable": build_portable,
        "appimage": build_appimage,
        "deb": build_deb,
        "msi": build_msi,
        "dmg": build_dmg,
    }
    try:
        artefact = builders[target]()
    except subprocess.CalledProcessError as exc:
        print(f"error: build step failed with exit code {exc.returncode}", file=sys.stderr)
        return 1

    if args.sign and platform.system() == "Darwin":
        sign_macos(args.sign)

    print(f"\n✓ Built: {artefact}")
    if Path(artefact).is_file():
        print(f"  Size: {Path(artefact).stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
