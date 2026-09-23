"""Filename construction and sanitization.

Everything in this module is pure (no I/O except the collision check, which takes an
explicit existence predicate) so it can be exhaustively unit-tested.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

# Characters that are illegal (or merely painful) in filenames on Windows/macOS/Linux.
_ILLEGAL = r'/\:*?"<>|'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL)}]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

MAX_BUSINESS_LEN = 48
MAX_PURPOSE_LEN = 32
MAX_STEM_LEN = 120

# Words that stay lowercase inside a title-cased business name (never first/last word).
_SMALL_WORDS = frozenset(
    {"a", "an", "and", "as", "at", "by", "de", "del", "for", "in", "la", "le",
     "of", "on", "or", "the", "to", "van", "von"}
)

# Tokens whose existing casing is deliberate and should survive title-casing.
_KEEP_CASE_RE = re.compile(r"^(?:[A-Z0-9&.\-']+|[A-Z][a-z]+[A-Z][A-Za-z]*)$")

# Reserved device names on Windows; harmless to avoid everywhere.
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_component(value: str, *, max_length: int | None = None) -> str:
    """Strip illegal characters from one filename component and normalise whitespace."""
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    # Replace (rather than delete) control characters so tabs/newlines act as breaks.
    text = _CONTROL_RE.sub(" ", text)
    # Replace (rather than delete) illegal characters so "ACME/Coffee" keeps a word break.
    text = _ILLEGAL_RE.sub(" ", text)
    # Dots and dashes at the edges cause hidden files / argparse-looking names.
    text = _WS_RE.sub(" ", text).strip(" .-_")
    if max_length is not None and len(text) > max_length:
        text = text[:max_length]
        # Prefer cutting at a word boundary rather than mid-word, when that keeps
        # most of the component.
        cut = text.rfind(" ")
        if cut >= max_length // 2:
            text = text[:cut]
        text = text.rstrip(" .-_")
    return text


def title_case_business(value: str) -> str:
    """Title-case a business name without mangling acronyms or intentional casing."""
    words = [w for w in value.split(" ") if w]
    if not words:
        return ""
    # Receipts are frequently printed ALL CAPS; treat a multi-word all-caps string as
    # shouty text to be title-cased, while still preserving short acronyms (LLC, DOJ).
    shouty = len(words) > 1 and value.upper() == value and any(c.isalpha() for c in value)
    out: list[str] = []
    last = len(words) - 1
    for index, word in enumerate(words):
        keep_case = _KEEP_CASE_RE.match(word) is not None
        if shouty:
            keep_case = word.isupper() and len([c for c in word if c.isalpha()]) <= 3
        lowered = word.lower()
        if lowered in _SMALL_WORDS:
            # Never treat a stop word as an acronym, even when printed in caps.
            out.append(lowered if index not in (0, last) else _capitalize_chunks(lowered))
            continue
        if keep_case:
            out.append(word)
            continue
        out.append(_capitalize_chunks(lowered))
    return " ".join(out)


def _capitalize_chunks(word: str) -> str:
    """Capitalise letter runs: 'o'brien-smith's' -> 'O'Brien-Smith's'.

    A run following an apostrophe is only capitalised when it is longer than one
    letter, so possessives stay as "Steve's" rather than "Steve'S".
    """

    def repl(match: re.Match[str]) -> str:
        run = match.group(0)
        start = match.start()
        preceding = word[start - 1] if start else ""
        if start and preceding in "'\u2019" and len(run) == 1:
            return run
        return run[0].upper() + run[1:]

    return re.sub(r"[A-Za-z]+", repl, word)


def clean_business(value: str) -> str:
    """Sanitize + title-case a business name."""
    return title_case_business(sanitize_component(value, max_length=MAX_BUSINESS_LEN))


def clean_purpose(value: str) -> str:
    """Sanitize + title-case an expense purpose/category."""
    return title_case_business(sanitize_component(value, max_length=MAX_PURPOSE_LEN))


class NameFormat(str, Enum):
    """Supported filename layouts."""

    DATE_FIRST = "date-first"
    BUSINESS_FIRST = "business-first"


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """How one :class:`NameFormat` renders: a field template plus a date layout."""

    template: str
    date_format: str
    example: str


#: Each preset is just data, so supporting user-supplied templates later means
#: accepting a :class:`FormatSpec` from the caller rather than reworking the renderer.
FORMAT_SPECS: dict[NameFormat, FormatSpec] = {
    NameFormat.DATE_FIRST: FormatSpec(
        template="{date} {business} - {purpose}",
        date_format="%Y-%m-%d",
        example="2024-03-14 Blue Bottle Coffee - Meals",
    ),
    NameFormat.BUSINESS_FIRST: FormatSpec(
        template="{business} - {purpose} - {date}",
        date_format="%m-%d-%Y",
        example="Blue Bottle Coffee - Meals - 03-14-2024",
    ),
}

DEFAULT_NAME_FORMAT = NameFormat.DATE_FIRST

_FIELD_RE = re.compile(r"\{(\w+)\}")
_ISO_DATE_FORMAT = "%Y-%m-%d"


def parse_name_format(value: NameFormat | str | None) -> NameFormat:
    """Coerce a string to a :class:`NameFormat`, raising ``ValueError`` when unknown."""
    if value is None:
        return DEFAULT_NAME_FORMAT
    if isinstance(value, NameFormat):
        return value
    try:
        return NameFormat(str(value).strip().lower())
    except ValueError:
        choices = ", ".join(fmt.value for fmt in NameFormat)
        raise ValueError(f"unknown name format {value!r} (choose from: {choices})") from None


def get_format_spec(value: NameFormat | str | None) -> FormatSpec:
    """Return the :class:`FormatSpec` for a format name or enum member."""
    return FORMAT_SPECS[parse_name_format(value)]


def render_template(template: str, values: dict[str, str]) -> str:
    """Fill ``template`` with ``values``, dropping empty fields and their separators.

    A literal separator is only emitted when it sits between two fields that both
    produced text, so a missing purpose yields ``Business - 03-14-2024`` rather than
    ``Business -  - 03-14-2024``.
    """
    out = ""
    pending = ""
    position = 0
    for match in _FIELD_RE.finditer(template):
        # Replace rather than accumulate: a skipped field must not leave its separator
        # behind to double up with the next one.
        pending = template[position : match.start()]
        position = match.end()
        value = values.get(match.group(1), "").strip()
        if not value:
            continue
        if out:
            out += pending
        out += value
    return out


def _format_date(value: str, date_format: str) -> str:
    """Re-render an ISO date, falling back to the raw text when it is not parseable."""
    text = sanitize_component(value)
    if not text or date_format == _ISO_DATE_FORMAT:
        return text
    try:
        parsed = datetime.strptime(text, _ISO_DATE_FORMAT).date()
    except ValueError:
        return text
    return parsed.strftime(date_format)


def build_stem(
    date: str,
    business: str,
    purpose: str,
    name_format: NameFormat | str = NameFormat.DATE_FIRST,
) -> str:
    """Build a filename stem in the requested :class:`NameFormat`."""
    spec = get_format_spec(name_format)
    values = {
        "date": _format_date(date, spec.date_format),
        "business": clean_business(business),
        "purpose": clean_purpose(purpose),
    }
    stem = render_template(spec.template, values)
    stem = _WS_RE.sub(" ", stem).strip(" .-_")
    if len(stem) > MAX_STEM_LEN:
        stem = stem[:MAX_STEM_LEN].rstrip(" .-_")
    if not stem:
        stem = "receipt"
    if stem.upper() in _RESERVED:
        stem = f"{stem} receipt"
    return stem


def normalize_extension(path: Path | str) -> str:
    """Return a lowercase extension, defaulting to ``.jpg`` when there is none."""
    suffix = Path(path).suffix.lower()
    if suffix in ("", "."):
        return ".jpg"
    if suffix == ".jpeg":
        return ".jpeg"
    return suffix


def unique_path(
    directory: Path,
    stem: str,
    extension: str,
    *,
    exists: Callable[[Path], bool] | None = None,
    reserved: Iterable[Path] = (),
) -> Path:
    """Return a non-colliding path, appending ``(2)``, ``(3)``... to the stem as needed.

    ``reserved`` lets a caller (e.g. a dry run) treat not-yet-created paths as taken.
    """
    exists_fn = exists if exists is not None else Path.exists
    taken = {Path(p) for p in reserved}
    candidate = directory / f"{stem}{extension}"
    counter = 2
    while candidate in taken or exists_fn(candidate):
        candidate = directory / f"{stem} ({counter}){extension}"
        counter += 1
    return candidate
