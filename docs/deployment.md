# Deployment guide

## Packaging

`packaging/build.py` produces every artefact. It always builds a PyInstaller
bundle first, then wraps it natively.

```bash
python packaging/build.py --target auto --clean   # native for this platform
python packaging/build.py --target portable       # zip that runs from a stick
python packaging/build.py --target onefile        # single executable
```

| Target | Platform | Extra tool | Output |
| --- | --- | --- | --- |
| `appimage` | Linux | `appimagetool` | `PDFStudio-1.0.0-x86_64.AppImage` |
| `deb` | Debian/Ubuntu | `dpkg-deb` | `pdfstudio_1.0.0_amd64.deb` |
| `msi` | Windows | Inno Setup (`iscc`) | `PDFStudio-1.0.0-setup.exe` |
| `dmg` | macOS | `create-dmg` or `hdiutil` | `PDFStudio-1.0.0.dmg` |
| `portable` | any | — | `PDFStudio-1.0.0-<os>-portable.zip` |

If a platform tool is missing the script says so and, where sensible, falls
back (for example a portable zip instead of an installer).

### Size

A bundle is roughly 180–260 MB, dominated by Qt and MuPDF. The build already
excludes QtWebEngine, Qt3D, Charts, DataVisualization, Quick3D, Multimedia,
tkinter and matplotlib. To go smaller, also drop the optional extras (Office
export, OCR, signatures) and ship them as a separate download.

---

## Code signing

### Windows

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /f certificate.pfx /p $env:CERT_PASSWORD dist\PDFStudio-1.0.0-setup.exe
```

Sign the installer *and* `pdfstudio.exe` inside it. Without a signature
SmartScreen will warn users until the binary builds reputation.

### macOS

```bash
python packaging/build.py --target dmg --sign "Developer ID Application: You (TEAMID)"

xcrun notarytool submit dist/PDFStudio-1.0.0.dmg \
  --keychain-profile AC_PASSWORD --wait
xcrun stapler staple dist/PDFStudio-1.0.0.dmg
```

Hardened runtime is enabled by the build script (`--options runtime`), which
notarisation requires.

### Linux

AppImages are usually distributed with a detached GPG signature:

```bash
gpg --detach-sign --armor dist/PDFStudio-1.0.0-x86_64.AppImage
sha256sum dist/*.AppImage > dist/SHA256SUMS
```

---

## Continuous delivery

`.github/workflows/ci.yml` runs on every push:

1. **lint** — ruff and mypy.
2. **test** — Linux, Windows and macOS with Tesseract and Ghostscript
   installed; coverage uploaded from Linux.
3. **slow** — benchmarks and stress tests on pushes to a branch.
4. **package** — on a `v*` tag, builds the AppImage, Windows installer and DMG.
5. **release** — attaches the artefacts to a draft GitHub release.
6. **publish-pypi** — publishes the wheel with trusted publishing.

Tagging is the whole release process:

```bash
git tag -a v1.0.1 -m "Release 1.0.1" && git push origin v1.0.1
```

Add signing secrets (`CERT_PASSWORD`, `APPLE_ID`, `AC_PASSWORD`, `TEAM_ID`) as
repository secrets and enable the signing steps.

---

## Automatic updates

The application checks a JSON manifest for newer versions:

```json
{
  "version": "1.0.1",
  "released": "2026-08-15",
  "notes": "https://github.com/pdfstudio/pdfstudio/releases/tag/v1.0.1",
  "downloads": {
    "linux-x86_64":  {"url": "…/PDFStudio-1.0.1-x86_64.AppImage", "sha256": "…"},
    "windows-x86_64":{"url": "…/PDFStudio-1.0.1-setup.exe",       "sha256": "…"},
    "macos-arm64":   {"url": "…/PDFStudio-1.0.1.dmg",             "sha256": "…"}
  }
}
```

Rules the updater follows:

* Never download without consent; the user sees the version and release notes.
* Always verify the SHA-256 before running anything.
* Hand off to the platform installer rather than patching in place.
* Package-manager installs (deb, Flatpak, Homebrew) disable the updater and
  point at the system tool instead.
* Updates can be disabled entirely for managed deployments.

---

## Enterprise deployment

### Silent installation

```powershell
PDFStudio-1.0.0-setup.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
```

```bash
sudo dpkg -i pdfstudio_1.0.0_amd64.deb && sudo apt-get -f install -y
```

### Central configuration

Ship a default `settings.json` and point every user at it:

```bash
export PDFSTUDIO_HOME=/opt/pdfstudio/profile     # shared, read-only defaults
```

Useful policy settings:

```json
{
  "plugins":  {"enabled": false},
  "ai":       {"enabled": false},
  "cloud":    {"auto_sync": false},
  "security": {"allow_remote_content": false, "warn_on_javascript": true},
  "autosave": {"enabled": true, "interval_seconds": 60}
}
```

### Server and container use

The head-less layers need no display, so the CLI runs anywhere:

```dockerfile
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr ghostscript libxkbcommon0 libegl1 libgl1 \
        libfontconfig1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pdfstudio[ocr,office]"
ENV QT_QPA_PLATFORM=offscreen
ENTRYPOINT ["pdfstudio-cli"]
```

```bash
docker run --rm -v "$PWD:/work" -w /work pdfstudio ocr scan.pdf -o out.pdf
```

---

## Verifying a build

Before publishing:

1. `pytest` fully green, including `-m slow`.
2. Launch the artefact on a clean machine (no Python installed).
3. Open each file in `samples/`, including the encrypted and scanned ones.
4. Run OCR, export to DOCX, and encrypt a file.
5. Check that settings persist across a restart.
6. Confirm the CLI works: `pdfstudio-cli info samples/report.pdf`.
7. Uninstall and confirm nothing is left behind except user data.

---

## Support and telemetry

PDF Studio collects **no** telemetry and makes no network requests unless the
user configures a remote AI provider or a cloud storage account. Diagnostics
are local: `cache/logs/pdfstudio.log` and `crash.log`, both shareable from
**Help ▸ Open log folder**.
