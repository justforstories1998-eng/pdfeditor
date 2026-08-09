"""Backend-neutral value objects shared by the PDF engine and the UI.

Keeping these free of PyMuPDF/pikepdf types means the UI never imports a PDF
library directly and the engine could grow alternative back-ends later.
All coordinates are in **PDF points** (1/72 inch) with the origin at the
top-left of the page, matching PyMuPDF's convention.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y

    def translated(self, dx: float, dy: float) -> Point:
        return Point(self.x + dx, self.y + dy)

    def distance_to(self, other: Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle ``(x0, y0)`` top-left, ``(x1, y1)`` bottom-right."""

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_size(cls, x: float, y: float, width: float, height: float) -> Rect:
        return cls(x, y, x + width, y + height)

    @classmethod
    def bounding(cls, points: list[Point]) -> Rect:
        xs = [p.x for p in points] or [0.0]
        ys = [p.y for p in points] or [0.0]
        return cls(min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> Point:
        return Point((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def normalized(self) -> Rect:
        return Rect(
            min(self.x0, self.x1),
            min(self.y0, self.y1),
            max(self.x0, self.x1),
            max(self.y0, self.y1),
        )

    def translated(self, dx: float, dy: float) -> Rect:
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    def scaled(self, factor: float) -> Rect:
        return Rect(self.x0 * factor, self.y0 * factor, self.x1 * factor, self.y1 * factor)

    def expanded(self, amount: float) -> Rect:
        return Rect(self.x0 - amount, self.y0 - amount, self.x1 + amount, self.y1 + amount)

    def contains(self, other: Rect | Point) -> bool:
        if isinstance(other, Point):
            return self.x0 <= other.x <= self.x1 and self.y0 <= other.y <= self.y1
        return (
            self.x0 <= other.x0
            and self.y0 <= other.y0
            and self.x1 >= other.x1
            and self.y1 >= other.y1
        )

    def intersects(self, other: Rect) -> bool:
        return not (
            self.x1 < other.x0 or other.x1 < self.x0 or self.y1 < other.y0 or other.y1 < self.y0
        )

    def intersection(self, other: Rect) -> Rect:
        return Rect(
            max(self.x0, other.x0),
            max(self.y0, other.y0),
            min(self.x1, other.x1),
            min(self.y1, other.y1),
        )

    def united(self, other: Rect) -> Rect:
        return Rect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def __iter__(self) -> Iterator[float]:
        yield from self.as_tuple()


@dataclass(frozen=True, slots=True)
class Color:
    """RGBA colour with components in ``0.0 – 1.0``."""

    r: float = 0.0
    g: float = 0.0
    b: float = 0.0
    a: float = 1.0

    @classmethod
    def from_hex(cls, value: str) -> Color:
        """Parse ``#rgb``, ``#rrggbb`` or ``#rrggbbaa``."""
        text = value.lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) == 6:
            text += "ff"
        if len(text) != 8:
            raise ValueError(f"Invalid colour {value!r}")
        r, g, b, a = (int(text[i : i + 2], 16) / 255 for i in range(0, 8, 2))
        return cls(r, g, b, a)

    @classmethod
    def from_bytes(cls, r: int, g: int, b: int, a: int = 255) -> Color:
        return cls(r / 255, g / 255, b / 255, a / 255)

    def to_hex(self, *, alpha: bool = False) -> str:
        parts = [self.r, self.g, self.b] + ([self.a] if alpha else [])
        return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in parts)

    def to_rgb_tuple(self) -> tuple[float, float, float]:
        return (self.r, self.g, self.b)

    def with_alpha(self, alpha: float) -> Color:
        return Color(self.r, self.g, self.b, alpha)

    @property
    def luminance(self) -> float:
        return 0.2126 * self.r + 0.7152 * self.g + 0.0722 * self.b


BLACK = Color(0, 0, 0)
WHITE = Color(1, 1, 1)
YELLOW = Color(1, 0.878, 0.4)
RED = Color(0.9, 0.2, 0.2)


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class Rotation(IntEnum):
    NONE = 0
    CW_90 = 90
    CW_180 = 180
    CW_270 = 270

    @classmethod
    def normalise(cls, degrees: int) -> Rotation:
        return cls(degrees % 360 // 90 * 90)


class PageLayout(StrEnum):
    SINGLE = "single"
    CONTINUOUS = "continuous"
    FACING = "facing"
    BOOK = "book"


class ZoomMode(StrEnum):
    CUSTOM = "custom"
    FIT_PAGE = "fit-page"
    FIT_WIDTH = "fit-width"
    FIT_HEIGHT = "fit-height"
    ACTUAL = "actual"


class AnnotationType(StrEnum):
    HIGHLIGHT = "highlight"
    UNDERLINE = "underline"
    STRIKEOUT = "strikeout"
    SQUIGGLY = "squiggly"
    TEXT = "text"  # sticky note
    FREE_TEXT = "freetext"
    INK = "ink"
    LINE = "line"
    ARROW = "arrow"
    SQUARE = "square"
    CIRCLE = "circle"
    POLYGON = "polygon"
    POLYLINE = "polyline"
    STAMP = "stamp"
    CARET = "caret"
    FILE_ATTACHMENT = "fileattachment"
    SOUND = "sound"
    MOVIE = "movie"
    REDACT = "redact"
    POPUP = "popup"
    WIDGET = "widget"
    CLOUD = "cloud"
    CALLOUT = "callout"
    DISTANCE = "distance"
    AREA = "area"
    PERIMETER = "perimeter"


class FieldType(StrEnum):
    TEXT = "text"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    COMBOBOX = "combobox"
    LISTBOX = "listbox"
    PUSHBUTTON = "pushbutton"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"


class EncryptionMethod(StrEnum):
    NONE = "none"
    RC4_40 = "RC4-40"
    RC4_128 = "RC4-128"
    AES_128 = "AES-128"
    AES_256 = "AES-256"


class Permission(IntEnum):
    """PDF permission bits (subset of ISO 32000-1 Table 22)."""

    PRINT = 1 << 2
    MODIFY = 1 << 3
    COPY = 1 << 4
    ANNOTATE = 1 << 5
    FILL_FORMS = 1 << 8
    ACCESSIBILITY = 1 << 9
    ASSEMBLE = 1 << 10
    PRINT_HIGH_RES = 1 << 11

    @classmethod
    def all_bits(cls) -> int:
        return sum(int(p) for p in cls)


class ConformanceLevel(StrEnum):
    """Archival / exchange conformance targets for export."""

    PDF_A_1B = "PDF/A-1b"
    PDF_A_2B = "PDF/A-2b"
    PDF_A_2U = "PDF/A-2u"
    PDF_A_3B = "PDF/A-3b"
    PDF_X_1A = "PDF/X-1a"
    PDF_X_4 = "PDF/X-4"
    PDF_E_1 = "PDF/E-1"
    PDF_UA_1 = "PDF/UA-1"


class BlendMode(StrEnum):
    NORMAL = "Normal"
    MULTIPLY = "Multiply"
    SCREEN = "Screen"
    OVERLAY = "Overlay"
    DARKEN = "Darken"
    LIGHTEN = "Lighten"
    DIFFERENCE = "Difference"
    EXCLUSION = "Exclusion"
    HUE = "Hue"
    SATURATION = "Saturation"
    COLOR = "Color"
    LUMINOSITY = "Luminosity"


# --------------------------------------------------------------------------- #
# Content records
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TextSpan:
    """A run of characters sharing the same font and style."""

    text: str
    rect: Rect
    font: str = ""
    size: float = 0.0
    color: Color = field(default_factory=lambda: BLACK)
    bold: bool = False
    italic: bool = False
    origin: Point = field(default_factory=lambda: Point(0, 0))
    char_rects: list[Rect] = field(default_factory=list)


@dataclass(slots=True)
class TextLine:
    spans: list[TextSpan]
    rect: Rect

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass(slots=True)
class TextBlock:
    """A paragraph-ish grouping produced by the text extractor."""

    lines: list[TextLine]
    rect: Rect
    block_no: int = 0

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(slots=True)
class ImageInfo:
    """Metadata about a raster image placed on a page."""

    xref: int
    rect: Rect
    width: int
    height: int
    colorspace: str = ""
    bpc: int = 8
    size_bytes: int = 0
    ext: str = "png"
    smask: int = 0
    dpi: tuple[int, int] = (72, 72)
    name: str = ""


@dataclass(slots=True)
class DrawingPath:
    """A vector drawing item extracted from (or written to) a page."""

    items: list[tuple[str, list[Point]]]  # ("l"|"c"|"re"|"qu", points)
    rect: Rect
    stroke: Color | None = None
    fill: Color | None = None
    width: float = 1.0
    opacity: float = 1.0
    closed: bool = False
    dashes: str = ""
    blend: BlendMode = BlendMode.NORMAL
    even_odd: bool = False


@dataclass(slots=True)
class Annotation:
    """Backend-neutral annotation record."""

    id: str
    type: AnnotationType
    page: int
    rect: Rect
    author: str = ""
    contents: str = ""
    subject: str = ""
    color: Color | None = None
    interior_color: Color | None = None
    opacity: float = 1.0
    border_width: float = 1.0
    created: str = ""
    modified: str = ""
    flags: int = 0
    quad_points: list[Rect] = field(default_factory=list)
    vertices: list[Point] = field(default_factory=list)
    ink_list: list[list[Point]] = field(default_factory=list)
    name: str = ""
    icon: str = ""
    state: str = ""
    reply_to: str | None = None
    resolved: bool = False
    rich_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FormField:
    """An AcroForm field."""

    name: str
    type: FieldType
    page: int
    rect: Rect
    value: Any = None
    default_value: Any = None
    options: list[str] = field(default_factory=list)
    required: bool = False
    readonly: bool = False
    multiline: bool = False
    max_length: int = 0
    tooltip: str = ""
    font: str = "Helv"
    font_size: float = 0.0
    text_color: Color = field(default_factory=lambda: BLACK)
    fill_color: Color | None = None
    border_color: Color | None = None
    format_script: str = ""
    validate_script: str = ""
    calculate_script: str = ""
    export_values: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Bookmark:
    """Outline entry; children make the tree."""

    title: str
    page: int
    level: int = 1
    y: float = 0.0
    zoom: float = 0.0
    bold: bool = False
    italic: bool = False
    color: Color | None = None
    open: bool = True
    uri: str = ""
    named_destination: str = ""
    children: list[Bookmark] = field(default_factory=list)

    def flatten(self) -> list[Bookmark]:
        out = [self]
        for child in self.children:
            out.extend(child.flatten())
        return out


@dataclass(slots=True)
class Link:
    """Hyperlink or internal jump."""

    rect: Rect
    page: int
    kind: str = "uri"  # uri | goto | gotor | launch | named
    uri: str = ""
    target_page: int = -1
    target_point: Point | None = None
    file: str = ""
    zoom: float = 0.0
    tooltip: str = ""


@dataclass(slots=True)
class Attachment:
    """An embedded file."""

    name: str
    size: int
    description: str = ""
    mime: str = ""
    created: str = ""
    modified: str = ""
    checksum: str = ""
    page: int = -1  # >=0 when it is a file-attachment annotation


@dataclass(slots=True)
class Layer:
    """Optional content group (OCG)."""

    xref: int
    name: str
    visible: bool = True
    locked: bool = False
    intent: str = "View"
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentMetadata:
    """Document information dictionary + XMP."""

    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    creator: str = ""
    producer: str = ""
    creation_date: str = ""
    modification_date: str = ""
    trapped: str = ""
    custom: dict[str, str] = field(default_factory=dict)
    xmp: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "author": self.author,
            "subject": self.subject,
            "keywords": self.keywords,
            "creator": self.creator,
            "producer": self.producer,
            "creationDate": self.creation_date,
            "modDate": self.modification_date,
            "trapped": self.trapped,
        }


@dataclass(slots=True)
class PageInfo:
    """Lightweight description of a page for lists and thumbnails."""

    index: int
    width: float
    height: float
    rotation: int = 0
    label: str = ""
    has_text: bool = True
    annotation_count: int = 0
    image_count: int = 0

    @property
    def size(self) -> tuple[float, float]:
        return (self.width, self.height)

    @property
    def is_landscape(self) -> bool:
        return self.width > self.height

    @property
    def display_label(self) -> str:
        return self.label or str(self.index + 1)


@dataclass(slots=True)
class SearchHit:
    """One match produced by the search service."""

    page: int
    rect: Rect
    text: str
    context: str = ""
    index: int = 0
    match_start: int = 0
    match_end: int = 0
    source: str = "text"  # text | annotation | bookmark | metadata | form


@dataclass(slots=True)
class SearchQuery:
    """Full-featured search request."""

    text: str
    case_sensitive: bool = False
    whole_words: bool = False
    regex: bool = False
    boolean: bool = False  # supports AND / OR / NOT
    include_annotations: bool = False
    include_bookmarks: bool = False
    include_metadata: bool = False
    include_forms: bool = False
    pages: tuple[int, int] | None = None
    max_hits: int = 5000


@dataclass(slots=True)
class PageSize:
    """Named paper size in points."""

    name: str
    width: float
    height: float

    def landscape(self) -> PageSize:
        return PageSize(f"{self.name} landscape", self.height, self.width)


PAGE_SIZES: dict[str, PageSize] = {
    "A0": PageSize("A0", 2384, 3370),
    "A1": PageSize("A1", 1684, 2384),
    "A2": PageSize("A2", 1191, 1684),
    "A3": PageSize("A3", 842, 1191),
    "A4": PageSize("A4", 595, 842),
    "A5": PageSize("A5", 420, 595),
    "A6": PageSize("A6", 298, 420),
    "Letter": PageSize("Letter", 612, 792),
    "Legal": PageSize("Legal", 612, 1008),
    "Tabloid": PageSize("Tabloid", 792, 1224),
    "Executive": PageSize("Executive", 522, 756),
    "B4": PageSize("B4", 709, 1001),
    "B5": PageSize("B5", 499, 709),
}


def parse_page_ranges(spec: str, page_count: int) -> list[int]:
    """Parse ``"1-3,7,10-"`` (1-based, inclusive) into 0-based indices.

    Supports ``even``/``odd``/``all``, open ended ranges and reverse ranges.

    Raises:
        ValueError: if the specification cannot be parsed.
    """
    text = spec.strip().lower()
    if not text or text == "all":
        return list(range(page_count))
    if text == "even":
        return [i for i in range(page_count) if (i + 1) % 2 == 0]
    if text == "odd":
        return [i for i in range(page_count) if (i + 1) % 2 == 1]

    pages: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start_s, _, end_s = chunk.partition("-")
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else page_count
            if start > end:
                start, end = end, start
            pages.extend(range(start - 1, min(end, page_count)))
        else:
            value = int(chunk)
            if 1 <= value <= page_count:
                pages.append(value - 1)
    seen: set[int] = set()
    return [p for p in pages if 0 <= p < page_count and not (p in seen or seen.add(p))]


def format_page_ranges(indices: list[int]) -> str:
    """Inverse of :func:`parse_page_ranges` (produces a compact 1-based string)."""
    if not indices:
        return ""
    ordered = sorted(set(indices))
    parts: list[str] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
        start = prev = page
    parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
    return ",".join(parts)
