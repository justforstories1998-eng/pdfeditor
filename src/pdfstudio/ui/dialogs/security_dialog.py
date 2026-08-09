"""Encryption and permissions dialog with live password-strength feedback."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.pdfengine.security import (
    EncryptionSettings,
    PermissionSet,
    generate_password,
    password_strength,
)
from pdfstudio.pdfengine.types import EncryptionMethod


class SecurityDialog(QDialog):
    """Collects passwords, cipher and permission flags."""

    def __init__(
        self, parent: QWidget | None = None, *, permissions_only: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Security" if permissions_only else "Encrypt document")
        self.resize(500, 560)
        self._permissions_only = permissions_only

        layout = QVBoxLayout(self)

        passwords = QGroupBox("Passwords", self)
        form = QFormLayout(passwords)
        self.user_password = QLineEdit(passwords)
        self.user_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.user_password.setPlaceholderText("Required to open the document")
        self.owner_password = QLineEdit(passwords)
        self.owner_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.owner_password.setPlaceholderText("Required to change permissions")

        show = QCheckBox("Show passwords", passwords)
        show.toggled.connect(self._toggle_echo)
        generate = QPushButton("Generate strong password", passwords)
        generate.clicked.connect(self._generate)

        self.strength = QProgressBar(passwords)
        self.strength.setRange(0, 100)
        self.strength.setTextVisible(True)
        self.strength.setFormat("")

        form.addRow("Open password:", self.user_password)
        form.addRow("Owner password:", self.owner_password)
        form.addRow("", show)
        form.addRow("Strength:", self.strength)
        form.addRow("", generate)
        passwords.setEnabled(not permissions_only)
        layout.addWidget(passwords)

        cipher = QGroupBox("Encryption", self)
        cipher_form = QFormLayout(cipher)
        self.method = QComboBox(cipher)
        for label, value in (
            ("AES-256 (recommended, PDF 2.0)", EncryptionMethod.AES_256),
            ("AES-128 (Acrobat 7+)", EncryptionMethod.AES_128),
            ("RC4-128 (legacy)", EncryptionMethod.RC4_128),
            ("RC4-40 (very weak, legacy)", EncryptionMethod.RC4_40),
        ):
            self.method.addItem(label, value)
        cipher_form.addRow("Algorithm:", self.method)
        cipher.setEnabled(not permissions_only)
        layout.addWidget(cipher)

        permissions = QGroupBox("Allow", self)
        permissions_layout = QVBoxLayout(permissions)
        self.checks: dict[str, QCheckBox] = {}
        for key, label in (
            ("printing", "Printing"),
            ("high_quality_printing", "High-resolution printing"),
            ("modify", "Changing the document"),
            ("copy", "Copying text and graphics"),
            ("annotate", "Adding comments and form fields"),
            ("fill_forms", "Filling in form fields"),
            ("accessibility", "Content extraction for accessibility"),
            ("assemble", "Assembling pages (insert, delete, rotate)"),
        ):
            box = QCheckBox(label, permissions)
            box.setChecked(True)
            self.checks[key] = box
            permissions_layout.addWidget(box)
        presets = QHBoxLayout()
        all_button = QPushButton("Allow all", permissions)
        read_button = QPushButton("Read only", permissions)
        none_button = QPushButton("Restrict all", permissions)
        all_button.clicked.connect(lambda: self._apply_preset(PermissionSet()))
        read_button.clicked.connect(lambda: self._apply_preset(PermissionSet.read_only()))
        none_button.clicked.connect(lambda: self._apply_preset(PermissionSet.none()))
        for button in (all_button, read_button, none_button):
            presets.addWidget(button)
        permissions_layout.addLayout(presets)
        layout.addWidget(permissions)

        note = QLabel(
            "Permissions are advisory: they are enforced by conforming viewers, "
            "not by the file format itself. Use an open password to control access.",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self.user_password.textChanged.connect(self._update_strength)
        self._update_strength("")

    def _toggle_echo(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.user_password.setEchoMode(mode)
        self.owner_password.setEchoMode(mode)

    def _generate(self) -> None:
        password = generate_password(20)
        self.user_password.setText(password)
        self.owner_password.setText(generate_password(20))
        self.user_password.setEchoMode(QLineEdit.EchoMode.Normal)

    def _update_strength(self, text: str) -> None:
        score, verdict = password_strength(text)
        self.strength.setValue(score)
        self.strength.setFormat(verdict if text else "")
        colour = "#e5484d" if score < 50 else "#e8a33d" if score < 75 else "#2fbf71"
        self.strength.setStyleSheet(f"QProgressBar::chunk {{ background-color: {colour}; }}")

    def _apply_preset(self, preset: PermissionSet) -> None:
        for key, box in self.checks.items():
            box.setChecked(getattr(preset, key))

    def permission_set(self) -> PermissionSet:
        return PermissionSet(**{key: box.isChecked() for key, box in self.checks.items()})

    def result_settings(self) -> EncryptionSettings:
        """The encryption configuration chosen by the user."""
        return EncryptionSettings(
            method=self.method.currentData(),
            user_password=self.user_password.text(),
            owner_password=self.owner_password.text(),
            permissions=self.permission_set(),
        )
