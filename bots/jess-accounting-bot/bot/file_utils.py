from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/heic",
    "image/heif",
}

ALLOWED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif"}


def build_upload_path(base_dir: Path, original_name: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe_name = Path(original_name or "receipt").name.replace(" ", "_")
    return base_dir / f"{timestamp}_{safe_name}"


def sniff_mime_type(file_name: str, supplied: str | None) -> str:
    if supplied:
        return supplied.lower()
    guessed, _ = mimetypes.guess_type(file_name)
    return (guessed or "application/octet-stream").lower()


def validate_supported_file(file_name: str, mime_type: str, max_bytes: int, size: int) -> None:
    suffix = Path(file_name).suffix.lower()
    if size > max_bytes:
        raise ValueError("The file is too large for this bot")
    if mime_type not in ALLOWED_MIME_TYPES and suffix not in ALLOWED_SUFFIXES:
        raise ValueError("Unsupported file type. Send a photo, PDF, JPG, JPEG, PNG, or HEIC image.")
