"""Exception hierarchy for PDF Studio.

Every recoverable failure raised by the head-less layers derives from
:class:`PdfStudioError` so the UI can present a single, consistent error dialog
while still being able to react to specific subclasses (for example prompting
for a password when :class:`PasswordRequiredError` is raised).
"""

from __future__ import annotations


class PdfStudioError(Exception):
    """Base class for all application errors."""

    #: Human readable, user-facing headline. Subclasses may override.
    title: str = "PDF Studio error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message if not self.detail else f"{self.message}: {self.detail}"


class DocumentError(PdfStudioError):
    """Raised when a document cannot be opened, parsed or saved."""

    title = "Document error"


class PasswordRequiredError(DocumentError):
    """The document is encrypted and needs a (different) password."""

    title = "Password required"

    def __init__(self, path: str, *, wrong_password: bool = False) -> None:
        msg = (
            f"The password supplied for {path!r} was rejected."
            if wrong_password
            else f"{path!r} is password protected."
        )
        super().__init__(msg)
        self.path = path
        self.wrong_password = wrong_password


class PermissionDeniedError(DocumentError):
    """A requested operation is forbidden by the document permission flags."""

    title = "Operation not permitted"


class RenderError(PdfStudioError):
    """Rasterisation of a page failed."""

    title = "Render error"


class OcrError(PdfStudioError):
    """OCR back-end missing or failed."""

    title = "OCR error"


class PluginError(PdfStudioError):
    """A plugin failed to load, validate or execute."""

    title = "Plugin error"


class DependencyMissingError(PdfStudioError):
    """An optional third-party dependency is required for this feature."""

    title = "Optional dependency missing"

    def __init__(self, package: str, feature: str) -> None:
        super().__init__(
            f"{feature} requires the optional package {package!r}.",
            detail=f"Install it with:  pip install {package}",
        )
        self.package = package
        self.feature = feature


class ValidationError(PdfStudioError):
    """User supplied input failed validation."""

    title = "Invalid input"
