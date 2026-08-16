"""Legacy Microsoft Office (DOC / PPT / XLS) conversion via LibreOffice.

The modern v2 extractors (``docx_extractorv2`` / ``pptx_extractorv2`` /
``xlsx_extractorv2``) only understand the OOXML formats. Legacy Office files
(DOC, PPT, XLS — OLE2 containers) are converted to their modern equivalents
with LibreOffice in **headless** mode before the modern extractor runs:

    .doc → (LibreOffice) → .docx → docx_extractorv2.py
    .ppt → (LibreOffice) → .pptx → pptx_extractorv2.py
    .xls → (LibreOffice) → .xlsx → xlsx_extractorv2.py

This module is deliberately *not* an extractor. Conversion is an internal
compatibility step owned by the orchestrator (`extraction_orchestratorv2.py`):
it never replaces the raw file, never writes into ``course-data/raw/``, and
the converted file is created inside a temporary directory that the orchestrator
cleans up after extraction.

Contract:

* ``find_libreoffice`` / ``require_libreoffice`` — locate LibreOffice on PATH
  (``libreoffice`` or ``soffice``) and report its version. If neither exists,
  ``require_libreoffice`` raises ``LibreOfficeNotFoundError`` with a clear
  message; this module never installs LibreOffice automatically.
* ``convert_legacy_file`` — run ``<libreoffice> --headless --convert-to
  <format> --outdir <dir> <file>``, verify the converted file (exists, readable,
  non-zero bytes) and return its path. Raises a ``LegacyConversionError``
  subclass on any failure.
* ``LEGACY_FORMATS`` — the extension registry the orchestrator uses to route a
  legacy file to (target format, MIME type, modern extractor).

Apache Tika is **not** part of this pipeline (see
``data_ingestion/TIKA_ARCHITECTURE_PLAN.md`` — plan only, superseded).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# LibreOffice is a heavyweight process; large presentations/workbooks can take
# a while. Anything past this is treated as a hang, not a slow document.
CONVERSION_TIMEOUT_SECONDS = 300


class LegacyConversionError(RuntimeError):
    """Base error: a legacy Office file could not be converted."""


class LibreOfficeNotFoundError(LegacyConversionError):
    """LibreOffice (``libreoffice`` / ``soffice``) is not installed."""


class ConvertedFileInvalidError(LegacyConversionError):
    """LibreOffice reported success but the converted file is unusable."""


@dataclass(frozen=True)
class LibreOfficeInfo:
    """A detected LibreOffice installation (executable path + version string)."""

    executable: Path
    version: str


@dataclass(frozen=True)
class LegacyFormat:
    """One legacy Office format and how it is made extractable.

    ``target_format_name`` is the ``--convert-to`` value (e.g. ``"docx"``) and
    ``target_extension`` the output file suffix (e.g. ``".docx"``); the modern
    extractor for the target extension is resolved by the orchestrator.
    """

    extension: str
    target_extension: str
    mime_type: str
    format_name: str
    target_format_name: str


LEGACY_FORMATS: dict[str, LegacyFormat] = {
    ".doc": LegacyFormat(
        extension=".doc",
        target_extension=".docx",
        mime_type="application/msword",
        format_name="doc",
        target_format_name="docx",
    ),
    ".ppt": LegacyFormat(
        extension=".ppt",
        target_extension=".pptx",
        mime_type="application/vnd.ms-powerpoint",
        format_name="ppt",
        target_format_name="pptx",
    ),
    ".xls": LegacyFormat(
        extension=".xls",
        target_extension=".xlsx",
        mime_type="application/vnd.ms-excel",
        format_name="xls",
        target_format_name="xlsx",
    ),
}

# MIME type → legacy extension, used as a routing fallback when the inventory
# record carries no usable extension but does carry the legacy MIME type.
MIME_TO_LEGACY: dict[str, str] = {
    fmt.mime_type: fmt.extension for fmt in LEGACY_FORMATS.values()
}


def find_libreoffice() -> LibreOfficeInfo | None:
    """Locate LibreOffice on PATH and report its version, or None.

    ``libreoffice`` is checked first, then ``soffice`` (the historic alias).
    """
    for name in ("libreoffice", "soffice"):
        executable = shutil.which(name)
        if executable is None:
            continue
        version = "unknown"
        try:
            proc = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            version = (proc.stdout or proc.stderr or "").strip() or "unknown"
        except Exception:  # noqa: BLE001 — a version probe must never break detection
            version = "unknown"
        return LibreOfficeInfo(executable=Path(executable), version=version)
    return None


def require_libreoffice() -> LibreOfficeInfo:
    """Return the detected LibreOffice, raising a clear error when missing."""
    info = find_libreoffice()
    if info is not None:
        return info
    raise LibreOfficeNotFoundError(
        "LibreOffice is required to extract legacy Microsoft Office "
        "(.doc/.ppt/.xls) files, but neither 'libreoffice' nor 'soffice' was "
        "found on PATH. Install LibreOffice (e.g. 'sudo apt install "
        "libreoffice' on Debian/Ubuntu) and re-run. LibreOffice is never "
        "installed automatically by the extraction pipeline."
    )


def verify_converted_file(path: Path) -> None:
    """Verify a converted file is usable: exists, readable, not zero bytes.

    Raises ``ConvertedFileInvalidError`` otherwise, so an invalid file is never
    handed to the modern extractor.
    """
    path = Path(path)
    if not path.is_file():
        raise ConvertedFileInvalidError(f"converted file not found: {path}")
    if not os.access(path, os.R_OK):
        raise ConvertedFileInvalidError(f"converted file is not readable: {path}")
    if path.stat().st_size == 0:
        raise ConvertedFileInvalidError(f"converted file is empty (0 bytes): {path}")


def convert_legacy_file(
    source: Path,
    target_format: str,
    out_dir: Path,
    libreoffice: LibreOfficeInfo | None = None,
    profile_dir: Path | None = None,
) -> Path:
    """Convert ``source`` to ``target_format`` (e.g. ``"docx"``) into ``out_dir``.

    ``target_format`` is the LibreOffice ``--convert-to`` filter name. The
    converted file is written next to nothing else in ``out_dir``; the returned
    path is ``out_dir / <source stem>.<target_format>``.

    ``libreoffice`` may be passed to reuse a single ``find_libreoffice()`` probe
    across a batch; when None it is re-detected. ``profile_dir``, when given, is
    used as a private LibreOffice user profile (``-env:UserInstallation``) so
    headless runs never fight over the default profile.

    Raises ``LegacyConversionError`` subclasses on any failure. The source file
    is never modified.
    """
    source = Path(source)
    out_dir = Path(out_dir)
    info = libreoffice if libreoffice is not None else require_libreoffice()

    if not source.is_file():
        raise LegacyConversionError(f"source file missing: {source}")
    out_dir.mkdir(parents=True, exist_ok=True)

    command = [str(info.executable), "--headless"]
    if profile_dir is not None:
        command.append(f"-env:UserInstallation=file://{Path(profile_dir).resolve()}")
    command += [
        "--convert-to", target_format,
        "--outdir", str(out_dir),
        str(source),
    ]

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
        raise LegacyConversionError(
            f"LibreOffice conversion timed out after {CONVERSION_TIMEOUT_SECONDS}s: "
            f"{source} -> {target_format}"
        ) from exc

    expected = out_dir / f"{source.stem}.{target_format}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise LegacyConversionError(
            f"LibreOffice conversion failed (exit code {proc.returncode}) for "
            f"{source} -> {target_format}: {detail or 'no diagnostic output'}"
        )

    try:
        verify_converted_file(expected)
    except ConvertedFileInvalidError as exc:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ConvertedFileInvalidError(
            f"{exc} (LibreOffice: {detail or 'no diagnostic output'})"
        ) from exc

    return expected
