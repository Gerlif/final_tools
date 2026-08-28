"""Path template parsing shared by the folder scanner and the Frame.io mapper.

A template is a ``/``-separated list of segments. Each segment is either a
literal (``Projektfiler``), a placeholder (``{year}``) or a mix of both
(``Kundecase {number}``). Templates are used in two directions:

* matching  -- pull field values out of a real directory path
* rendering -- build a Frame.io project/folder path from those field values
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_FIELD_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# A version marker anywhere in the name: "spot v2", "CBS_V02_1x1", "spot-V11".
# It must stand as its own token -- a separator or the start before it, a
# separator or the end after -- so a name that merely contains the letters,
# like "Groov5", is left alone.
_VERSION_TOKEN_RE = re.compile(
    r"(?:(?<=[\s._-])|^)[vV](?P<version>\d+(?:\.\d+)*)(?=[\s._-]|$)"
)

# Runs of separators collapse to one space before names are compared, so
# "CBS - Girltalk 16x9" and "CBS_Girltalk_16x9" are the same asset.
_SEPARATORS_RE = re.compile(r"[\s._-]+")

# An extension starts with a letter, so the ".1" of "spot v1.1" is part of the
# version rather than a file type.
_EXTENSION_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")


class TemplateError(ValueError):
    """Raised when a template cannot be parsed."""


def normalize(name: str) -> str:
    """Normalize a file/folder name for comparison.

    macOS clients write NFD-decomposed names over SMB while Linux and Frame.io
    normally carry NFC, so ``Kundecase Ødegård`` can differ byte-wise between
    the NAS and Frame.io while looking identical. Compare on NFC.
    """
    return unicodedata.normalize("NFC", name).strip()


def fold(name: str, case_sensitive: bool) -> str:
    """Normalize plus optional case folding, for dictionary lookups."""
    value = normalize(name)
    return value if case_sensitive else value.casefold()


@dataclass(frozen=True)
class Segment:
    """One path component of a template."""

    raw: str
    fields: tuple[str, ...]
    pattern: re.Pattern[str] | None
    case_sensitive: bool = False

    @property
    def is_literal(self) -> bool:
        return not self.fields

    def match(self, name: str) -> dict[str, str] | None:
        """Return the field values if ``name`` matches this segment."""
        if self.pattern is None:
            literal = fold(self.raw, self.case_sensitive)
            return {} if fold(name, self.case_sensitive) == literal else None
        matched = self.pattern.match(normalize(name))
        if matched is None:
            return None
        return {key: value.strip() for key, value in matched.groupdict().items()}

    def render(self, fields: dict[str, str]) -> str:
        try:
            return _FIELD_RE.sub(lambda m: fields[m.group(1)], self.raw)
        except KeyError as exc:  # pragma: no cover - guarded by Template.render
            raise TemplateError(f"unknown field {exc} in segment {self.raw!r}") from exc


@dataclass(frozen=True)
class Template:
    """A parsed path template."""

    raw: str
    segments: tuple[Segment, ...]
    case_sensitive: bool

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(field for segment in self.segments for field in segment.fields)

    def match(self, parts: tuple[str, ...]) -> dict[str, str] | None:
        """Match a full sequence of path components against the template."""
        if len(parts) != len(self.segments):
            return None
        fields: dict[str, str] = {}
        for segment, part in zip(self.segments, parts):
            matched = segment.match(part)
            if matched is None:
                return None
            for key, value in matched.items():
                if fields.setdefault(key, value) != value:
                    return None
        return fields

    def render(self, fields: dict[str, str]) -> tuple[str, ...]:
        """Build concrete path components from field values."""
        missing = sorted(set(self.fields) - set(fields))
        if missing:
            raise TemplateError(
                f"template {self.raw!r} needs field(s) {', '.join(missing)} "
                f"which the watch template does not provide"
            )
        return tuple(segment.render(fields) for segment in self.segments)


def parse_template(template: str, *, case_sensitive: bool = False) -> Template:
    """Parse a ``/``-separated template into segments."""
    cleaned = template.replace("\\", "/").strip().strip("/")
    if not cleaned:
        raise TemplateError("template must not be empty")

    segments: list[Segment] = []
    for raw in cleaned.split("/"):
        if not raw:
            raise TemplateError(f"template {template!r} contains an empty path segment")
        fields = tuple(_FIELD_RE.findall(raw))
        if len(set(fields)) != len(fields):
            raise TemplateError(f"segment {raw!r} repeats a field name")
        pattern: re.Pattern[str] | None = None
        if fields:
            regex = "".join(
                f"(?P<{part[1:-1]}>.+?)" if _FIELD_RE.fullmatch(part) else re.escape(part)
                for part in _split_keeping_fields(raw)
            )
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(f"^{regex}$", flags)
        segments.append(
            Segment(
                raw=raw, fields=fields, pattern=pattern, case_sensitive=case_sensitive
            )
        )
    return Template(raw=template, segments=tuple(segments), case_sensitive=case_sensitive)


def _split_keeping_fields(raw: str) -> list[str]:
    """Split a segment into literal chunks and ``{field}`` tokens, in order."""
    parts: list[str] = []
    index = 0
    for match in _FIELD_RE.finditer(raw):
        if match.start() > index:
            parts.append(raw[index : match.start()])
        parts.append(match.group(0))
        index = match.end()
    if index < len(raw):
        parts.append(raw[index:])
    return parts


@dataclass(frozen=True)
class VersionedName:
    """A file name split into what identifies the asset and which version it is.

    The version marker is removed wherever it sits, and everything else is kept:

        CBS SCM_6 sek_V02_1x1.mov  ->  "CBS SCM 6 sek 1x1",  version (2,)
        CBS - Girltalk v3 16x9.mp4 ->  "CBS Girltalk 16x9",  version (3,)
        CBS - Girltalk v04_9x16.mp4 -> "CBS Girltalk 9x16",  version (4,)

    Keeping the rest matters: an aspect ratio is part of what the file *is*, so
    a 16x9 and a 9x16 are two deliverables and never versions of each other.
    """

    identity: str
    version: tuple[int, ...] | None
    extension: str

    def same_asset_as(self, other: "VersionedName", case_sensitive: bool) -> bool:
        return fold(self.identity, case_sensitive) == fold(
            other.identity, case_sensitive
        ) and fold(self.extension, case_sensitive) == fold(
            other.extension, case_sensitive
        )


def split_version(name: str) -> VersionedName:
    """Separate a version marker from the rest of a file name."""
    cleaned = normalize(name)

    extension_match = _EXTENSION_RE.search(cleaned)
    extension = extension_match.group(0) if extension_match else ""
    stem = cleaned[: len(cleaned) - len(extension)] if extension else cleaned

    # A name can hold more than one candidate; the last is the version.
    matches = list(_VERSION_TOKEN_RE.finditer(stem))
    if not matches:
        return VersionedName(
            identity=_collapse(stem), version=None, extension=extension
        )

    marker = matches[-1]
    version = tuple(int(part) for part in marker.group("version").split("."))
    remainder = f"{stem[: marker.start()]} {stem[marker.end() :]}"
    return VersionedName(
        identity=_collapse(remainder), version=version, extension=extension
    )


def _collapse(name: str) -> str:
    """Reduce separator runs to single spaces so spelling variants compare equal."""
    return _SEPARATORS_RE.sub(" ", name).strip()
