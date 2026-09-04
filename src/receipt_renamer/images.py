"""Image loading and preparation before upload.

Large flatbed scans are downscaled and re-encoded as JPEG, which is what keeps
per-receipt token cost low.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

DEFAULT_MAX_EDGE = 2000
DEFAULT_JPEG_QUALITY = 85

#: Extensions handled natively by Pillow.
NATIVE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif"})
HEIC_EXTENSIONS = frozenset({".heic", ".heif"})
PDF_EXTENSIONS = frozenset({".pdf"})
SUPPORTED_EXTENSIONS = NATIVE_EXTENSIONS | HEIC_EXTENSIONS | PDF_EXTENSIONS


class UnsupportedImageError(Exception):
    """The file cannot be prepared for upload (bad format, or missing optional dep)."""


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """A downscaled, JPEG-encoded copy of a source image, ready to upload."""

    data: bytes
    mime_type: str
    width: int
    height: int
    source_width: int
    source_height: int


def _heif_available() -> bool:
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    return True


def _register_heif() -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()


def is_supported(path: Path | str) -> bool:
    """True when the extension is one we could conceivably process."""
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def unsupported_reason(path: Path | str) -> str | None:
    """Return a human-readable reason this file cannot be processed, or ``None``."""
    suffix = Path(path).suffix.lower()
    if suffix in NATIVE_EXTENSIONS:
        return None
    if suffix in HEIC_EXTENSIONS:
        if _heif_available():
            return None
        return "HEIC support requires pillow-heif (pip install 'receipt-renamer[heic]')"
    if suffix in PDF_EXTENSIONS:
        try:
            import pdf2image  # noqa: F401
        except ImportError:
            return "PDF support requires pdf2image + poppler (pip install 'receipt-renamer[pdf]')"
        return None
    return f"unsupported file type {suffix or '(none)'}"


def _open_image(path: Path) -> Image.Image:
    suffix = path.suffix.lower()
    if suffix in PDF_EXTENSIONS:
        try:
            from pdf2image import convert_from_path
        except ImportError as exc:
            raise UnsupportedImageError(
                "PDF support requires pdf2image + poppler (pip install 'receipt-renamer[pdf]')"
            ) from exc
        try:
            pages = convert_from_path(str(path), first_page=1, last_page=1, dpi=200)
        except Exception as exc:  # noqa: BLE001 - poppler failures vary wildly
            raise UnsupportedImageError(f"could not render PDF: {exc}") from exc
        if not pages:
            raise UnsupportedImageError("PDF contained no pages")
        return pages[0]

    if suffix in HEIC_EXTENSIONS:
        if not _heif_available():
            raise UnsupportedImageError(
                "HEIC support requires pillow-heif (pip install 'receipt-renamer[heic]')"
            )
        _register_heif()

    try:
        return Image.open(path)
    except Exception as exc:  # noqa: BLE001 - Pillow raises a wide range of errors
        raise UnsupportedImageError(f"could not open image: {exc}") from exc


def prepare_image(
    path: Path | str,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> PreparedImage:
    """Load, EXIF-rotate, downscale and JPEG-encode an image for upload."""
    path = Path(path)
    reason = unsupported_reason(path)
    if reason:
        raise UnsupportedImageError(reason)

    with _open_image(path) as image:
        image.load()
        image = ImageOps.exif_transpose(image) or image
        source_width, source_height = image.size
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        longest = max(image.size)
        if longest > max_edge:
            scale = max_edge / float(longest)
            new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            image = image.resize(new_size, Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        return PreparedImage(
            data=buffer.getvalue(),
            mime_type="image/jpeg",
            width=image.width,
            height=image.height,
            source_width=source_width,
            source_height=source_height,
        )
