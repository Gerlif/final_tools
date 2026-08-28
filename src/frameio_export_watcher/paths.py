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
