"""GS1 element-string and Digital Link URI parser.

Supports AI 01 (GTIN-14), AI 10 (batch/lot), AI 17 (expiry date),
AI 21 (serial number) and the variable-metric weight family
AI 310n (net weight, kilograms) and AI 320n (net weight, pounds),
where ``n`` (0-9) is the number of implied decimal places and the
field is a fixed 6-digit number.

Two input forms are accepted by :func:`parse`:

1. Raw GS1 element strings as emitted by scanners. Fixed-length fields
   (01, 17, 310n, 320n) are delimited implicitly by their length;
   variable-length fields (10, 21) must be terminated by the FNC1
   separator (ASCII 29, GS) unless they are the last field in the
   string. Four-character AIs are recognised by longest match (the
   310n/320n family takes precedence over any 2-character AI).

2. GS1 Digital Link URIs (``http://`` / ``https://``).  AI/value pairs
   are read from the URI path (``/AI/value/AI/value``) first and then
   from query parameters (``?AI=value&AI=value``).  Percent-encoded
   bytes are decoded as UTF-8 before the same character-set, length,
   check-digit and date validation is applied.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from decimal import Decimal

GS = "\x1d"  # ASCII 29 <GS>, transmitted as FNC1 by scanners

DIGITS = frozenset("0123456789")
HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")

URI_SCHEMES = ("https://", "http://")

# GS1 AI encodable character set 82 (GS1 General Specifications, fig. 7.11-1)
AI82 = frozenset(
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "!\"%&'()*+,-./:;<=>?_"
)


@dataclass(frozen=True)
class AISpec:
    ai: str
    name: str
    min_length: int
    max_length: int
    fixed_length: bool
    charset: frozenset
    charset_rule: str
    ai_length: int = 2  # number of AI characters (4 for the 310n/320n family)
    decimals: int | None = None  # implied decimal places (310n/320n only)
    unit: str | None = None  # measurement unit emitted alongside decimal_value


# Variable-metric weight families: AI prefix -> (name, unit)
WEIGHT_AI_FAMILIES = {
    "310": ("NET WEIGHT (KG)", "kg"),
    "320": ("NET WEIGHT (LB)", "lb"),
}


def _build_ai_specs() -> dict[str, AISpec]:
    specs = {
        "01": AISpec("01", "GTIN", 14, 14, True, DIGITS, "digits 0-9"),
        "17": AISpec("17", "EXPIRY", 6, 6, True, DIGITS, "digits 0-9"),
        "10": AISpec("10", "BATCH/LOT", 1, 20, False, AI82, "GS1 AI character set 82"),
        "21": AISpec("21", "SERIAL", 1, 20, False, AI82, "GS1 AI character set 82"),
    }
    for prefix, (name, unit) in WEIGHT_AI_FAMILIES.items():
        for n in range(10):
            ai = f"{prefix}{n}"
            specs[ai] = AISpec(
                ai,
                name,
                6,
                6,
                True,
                DIGITS,
                "digits 0-9",
                ai_length=4,
                decimals=n,
                unit=unit,
            )
    return specs


AI_SPECS = _build_ai_specs()

SUPPORTED_AIS = (
    "01, 10, 17, 21 and the weight family 310n/320n "
    "(n = 0-9 implied decimal places, fixed 6 digits)"
)


class GS1ParseError(Exception):
    """Parse/validation failure carrying a byte offset and the expected rule."""

    def __init__(self, code, message, offset, expected, found=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset
        self.expected = expected
        self.found = found

    def to_dict(self):
        detail = {
            "error": self.code,
            "message": self.message,
            "offset": self.offset,
            "expected": self.expected,
        }
        if self.found is not None:
            detail["found"] = self.found
        return detail


@dataclass(frozen=True)
class Element:
    ai: str
    name: str
    value: str
    offset: int  # byte offset of the AI within the input
    decimal_value: float | None = None  # 310n/320n only: value with implied decimal point
    unit: str | None = None  # 310n/320n only: "kg" or "lb"


def _byte_offset(data: str, index: int) -> int:
    """Byte offset (UTF-8) of the character at ``index``."""
    return len(data[:index].encode("utf-8"))


def parse(data: str) -> list[Element]:
    """Parse a GS1 element string or Digital Link URI into ordered elements."""
    if not data:
        raise GS1ParseError(
            "EMPTY_INPUT",
            "Input is empty",
            offset=0,
            expected=f"at least one element string starting with AI {SUPPORTED_AIS}",
        )
    if data.startswith(URI_SCHEMES):
        return _parse_digital_link(data)
    return _parse_element_string(data)


# ----------------------------------------------------------------- element strings


def _parse_element_string(data: str) -> list[Element]:
    elements = []
    pos = 0
    while pos < len(data):
        ai_offset = _byte_offset(data, pos)
        if data[pos] == GS:
            raise GS1ParseError(
                "UNEXPECTED_SEPARATOR",
                "Unexpected separator (ASCII 29); expected an application identifier",
                offset=ai_offset,
                expected=f"numeric AI, one of: {SUPPORTED_AIS}",
                found="<GS>",
            )
        spec, pos = _match_element_string_ai(data, pos, ai_offset)
        value_pos = pos

        if spec.fixed_length:
            value, pos = _read_fixed(data, pos, spec)
        else:
            value, pos = _read_variable(data, pos, spec)

        # UTF-8 byte offset of every value character in the raw input
        offsets = [_byte_offset(data, value_pos + i) for i in range(len(value))]
        _check_charset(spec, value, offsets)
        decimal_value, unit = _apply_semantics(spec, value, offsets)

        elements.append(
            Element(
                ai=spec.ai,
                name=spec.name,
                value=value,
                offset=ai_offset,
                decimal_value=decimal_value,
                unit=unit,
            )
        )
    return elements


def _match_element_string_ai(data: str, pos: int, ai_offset: int):
    """Recognise the AI at ``pos`` by longest match.

    Returns ``(spec, new_pos)`` or raises :class:`GS1ParseError`. The
    four-character 310n/320n identifiers take precedence over the
    two-character set; a malformed family prefix (missing or non-digit
    precision digit) is reported as INVALID_PRECISION rather than being
    mistaken for a 2-character AI.
    """
    head = data[pos : pos + 2]
    if len(head) < 2 or not all(c in DIGITS for c in head):
        raise GS1ParseError(
            "INVALID_AI",
            f"Expected a numeric application identifier, found {head!r}",
            offset=ai_offset,
            expected=f"numeric AI, one of: {SUPPORTED_AIS}",
            found=head,
        )

    family = data[pos : pos + 3]
    if family in WEIGHT_AI_FAMILIES:
        if pos + 3 >= len(data):
            raise GS1ParseError(
                "INVALID_PRECISION",
                f"AI family {family} requires a precision digit (0-9) but the input ended",
                offset=_byte_offset(data, len(data)),
                expected=f"precision digit 0-9 for AI {family}n",
                found="end of input",
            )
        precision = data[pos + 3]
        if precision == GS:
            raise GS1ParseError(
                "INVALID_PRECISION",
                f"AI family {family} requires a precision digit (0-9) but a "
                "separator (ASCII 29) was found",
                offset=_byte_offset(data, pos + 3),
                expected=f"precision digit 0-9 for AI {family}n",
                found="<GS>",
            )
        if precision not in DIGITS:
            raise GS1ParseError(
                "INVALID_PRECISION",
                f"AI family {family} requires a precision digit 0-9 as its 4th "
                f"character, found {precision!r}",
                offset=_byte_offset(data, pos + 3),
                expected=f"precision digit 0-9 for AI {family}n",
                found=precision,
            )

    # Longest match first: the complete 4-character weight AI, otherwise
    # the 2-character set.
    four = data[pos : pos + 4]
    spec = AI_SPECS.get(four) if len(four) == 4 else None
    if spec is None:
        ai = data[pos : pos + 2]
        spec = AI_SPECS.get(ai)
        if spec is None:
            raise GS1ParseError(
                "UNSUPPORTED_AI",
                f"Unsupported application identifier '{ai}'",
                offset=ai_offset,
                expected=f"one of: {SUPPORTED_AIS}",
                found=ai,
            )
        return spec, pos + 2
    return spec, pos + spec.ai_length


def _read_fixed(data, pos, spec):
    field = data[pos : pos + spec.max_length]
    sep = field.find(GS)
    if sep != -1:
        raise GS1ParseError(
            "FIELD_TOO_SHORT",
            f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
            f"but a separator was found after {sep}",
            offset=_byte_offset(data, pos + sep),
            expected=f"{spec.max_length} characters for AI {spec.ai}",
            found="<GS>",
        )
    if len(field) < spec.max_length:
        raise GS1ParseError(
            "FIELD_TOO_SHORT",
            f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
            f"but the input ended after {len(field)}",
            offset=_byte_offset(data, len(data)),
            expected=f"{spec.max_length} characters for AI {spec.ai}",
            found=f"{len(field)} character(s)",
        )
    return field, pos + spec.max_length


def _read_variable(data, pos, spec):
    end = data.find(GS, pos)
    terminated = end != -1
    if not terminated:
        end = len(data)
    field = data[pos:end]
    if len(field) < spec.min_length:
        raise GS1ParseError(
            "FIELD_EMPTY",
            f"AI {spec.ai} ({spec.name}) requires at least {spec.min_length} character",
            offset=_byte_offset(data, pos),
            expected=f"{spec.min_length}-{spec.max_length} characters for AI {spec.ai}",
            found="empty field",
        )
    if len(field) > spec.max_length:
        raise GS1ParseError(
            "FIELD_TOO_LONG",
            f"AI {spec.ai} ({spec.name}) allows at most {spec.max_length} characters "
            f"but got {len(field)} before {'the separator' if terminated else 'end of input'}",
            offset=_byte_offset(data, pos + spec.max_length),
            expected=f"separator (ASCII 29) or end of input within {spec.max_length} characters",
        )
    return field, end + 1 if terminated else end


# ------------------------------------------------------------ shared value checks


def _check_charset(spec, field, offsets):
    """``offsets[i]`` is the raw byte offset of ``field[i]``."""
    for i, ch in enumerate(field):
        if ch not in spec.charset:
            raise GS1ParseError(
                "INVALID_CHARACTER",
                f"Character {ch!r} (U+{ord(ch):04X}) is not allowed in AI {spec.ai} ({spec.name})",
                offset=offsets[i],
                expected=spec.charset_rule,
                found=ch,
            )


def _check_gtin(gtin, offsets):
    total = 0
    for i, ch in enumerate(reversed(gtin[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    expected = (10 - total % 10) % 10
    if expected != int(gtin[-1]):
        raise GS1ParseError(
            "INVALID_CHECK_DIGIT",
            f"GTIN check digit mismatch: expected {expected}, found {gtin[-1]}",
            offset=offsets[-1],
            expected=f"Mod-10 check digit {expected}",
            found=gtin[-1],
        )


def _check_date(yymmdd, offsets):
    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    year = 2000 + yy
    if not 1 <= mm <= 12:
        raise GS1ParseError(
            "INVALID_DATE",
            f"Invalid expiry month {mm:02d} in AI 17 date '{yymmdd}'",
            offset=offsets[2],
            expected="month 01-12",
            found=yymmdd[2:4],
        )
    last_day = calendar.monthrange(year, mm)[1]
    if dd == 0:
        return  # GS1: day "00" denotes the last day of the month
    if dd > last_day:
        raise GS1ParseError(
            "INVALID_DATE",
            f"Invalid expiry day {dd:02d} for {year}-{mm:02d} (max {last_day})",
            offset=offsets[4],
            expected=f"day 01-{last_day} for {year}-{mm:02d} (00 = last day of month)",
            found=yymmdd[4:6],
        )


def _apply_semantics(spec: AISpec, value: str, offsets):
    """Run AI-specific validation after charset/length checks.

    Returns ``(decimal_value, unit)``, both ``None`` unless the AI
    belongs to the 310n/320n weight family.
    """
    if spec.ai == "01":
        _check_gtin(value, offsets)
    elif spec.ai == "17":
        _check_date(value, offsets)
    elif spec.decimals is not None:
        return _decode_weight(value, spec), spec.unit
    return None, None


def decode_weight(value: str, decimals: int) -> Decimal:
    """Interpret the 6 fixed digits with ``decimals`` implied places exactly.

    The GS1 variable-metric rule is ``int(digits) / 10**n``; when n > 6
    the missing high-order fractional digits are implied zero, e.g.
    ``3109 000001`` -> Decimal('0.000000001').  The result is built from
    the integer digits and a power of ten, so no binary float is ever
    involved.
    """
    return Decimal(int(value)).scaleb(-decimals)


def _decode_weight(value: str, spec: AISpec) -> float:
    """Float view of :func:`decode_weight` for the ``decimal_value`` field."""
    return float(decode_weight(value, spec.decimals))


# ------------------------------------------------------------ Digital Link URIs


def _parse_digital_link(data: str) -> list[Element]:
    authority_start = data.index("://") + 3  # works for http:// and https://

    # Locate path / query / fragment boundaries directly on the raw URI so
    # that every error can carry an accurate byte offset.
    path_start = len(data)
    for marker in ("/", "?", "#"):
        j = data.find(marker, authority_start)
        if j != -1:
            path_start = min(path_start, j)
    fragment_start = data.find("#", path_start)
    if fragment_start == -1:
        fragment_start = len(data)
    query_mark = data.find("?", path_start, fragment_start)
    if query_mark == -1:
        path_end = fragment_start
        query_start = query_end = -1
    else:
        path_end = query_mark
        query_start = query_mark + 1
        query_end = fragment_start

    # ---- path: /AI/value/AI/value (a single trailing slash is tolerated) ----
    segments = _split_path(data, path_start, path_end)
    if len(segments) % 2:
        odd_start = segments[-1][0]
        odd_raw = data[segments[-1][0] : segments[-1][1]]
        raise GS1ParseError(
            "UNPAIRED_PATH",
            "URI path must contain AI/value pairs but ends with a value missing "
            f"its AI or an AI missing its value: {odd_raw!r}",
            offset=_byte_offset(data, odd_start),
            expected="even number of path segments: /AI/value[/AI/value]...",
            found=odd_raw,
        )

    # ---- query: AI=value&AI=value (query order follows path order) ----
    pairs = _split_query(data, query_start, query_end)

    if not segments and not pairs:
        raise GS1ParseError(
            "EMPTY_INPUT",
            "URI contains no AI/value pairs in its path or query string",
            offset=0,
            expected=f"at least one path segment or query parameter with AI {SUPPORTED_AIS}",
        )

    elements = []
    seen: dict[str, tuple[int, str]] = {}

    def consume(ai_span, value_span, location):
        ai_start, ai_end = ai_span
        val_start, val_end = value_span
        ai, ai_offsets = _percent_decode(data, ai_start, ai_end)
        ai_byte = ai_offsets[0] if ai_offsets else _byte_offset(data, ai_start)

        # In a URI the AI is the whole path segment / query key, so unlike
        # element strings there is no fixed 2-character window to read.
        if not ai:
            raw_ai = data[ai_start:ai_end]
            raise GS1ParseError(
                "INVALID_AI",
                f"Expected a numeric application identifier in the {location}, "
                "found an empty segment",
                offset=ai_byte,
                expected=f"numeric AI, one of: {SUPPORTED_AIS}",
                found=raw_ai,
            )

        # A 310n/320n family prefix with a missing or non-digit precision
        # digit is reported as INVALID_PRECISION; this must be checked
        # before the generic all-digits rule and the longest-match lookup.
        family = ai[:3]
        if family in WEIGHT_AI_FAMILIES:
            if len(ai) < 4:
                raise GS1ParseError(
                    "INVALID_PRECISION",
                    f"AI family {family} requires a precision digit (0-9) as "
                    f"its 4th character in the {location}",
                    offset=_byte_offset(data, ai_end),
                    expected=f"4-character AI {family}n (precision digit 0-9)",
                    found=ai,
                )
            if ai[3] not in DIGITS:
                raise GS1ParseError(
                    "INVALID_PRECISION",
                    f"AI family {family} requires a precision digit 0-9 as its "
                    f"4th character, found {ai[3]!r}",
                    offset=ai_offsets[3],
                    expected=f"precision digit 0-9 for AI {family}n",
                    found=ai[3],
                )

        if not all(c in DIGITS for c in ai):
            raw_ai = data[ai_start:ai_end]
            raise GS1ParseError(
                "INVALID_AI",
                f"Expected a numeric application identifier in the {location}, "
                f"found {ai!r}",
                offset=ai_byte,
                expected=f"numeric AI, one of: {SUPPORTED_AIS}",
                found=raw_ai,
            )
        spec = AI_SPECS.get(ai)
        if spec is None:
            raise GS1ParseError(
                "UNSUPPORTED_AI",
                f"Unsupported application identifier '{ai}' in the {location}",
                offset=ai_byte,
                expected=f"one of: {SUPPORTED_AIS}",
                found=ai,
            )
        if ai in seen:
            first_offset, first_location = seen[ai]
            if first_location == location:
                message = (
                    f"Application identifier '{ai}' is repeated in the {location}; "
                    "each AI may appear only once"
                )
            else:
                message = (
                    f"Application identifier '{ai}' appears in both the "
                    f"{first_location} (byte {first_offset}) and the {location}; "
                    "an AI may not be given in both places"
                )
            raise GS1ParseError(
                "DUPLICATE_AI",
                message,
                offset=ai_byte,
                expected=f"AI '{ai}' to appear at most once",
                found=ai,
            )
        seen[ai] = (ai_byte, location)

        value, offsets = _percent_decode(data, val_start, val_end)
        decimal_value, unit = _validate_uri_value(
            spec, value, offsets, _byte_offset(data, val_end)
        )
        elements.append(
            Element(
                ai=spec.ai,
                name=spec.name,
                value=value,
                offset=ai_byte,
                decimal_value=decimal_value,
                unit=unit,
            )
        )

    for k in range(0, len(segments), 2):
        consume(segments[k], segments[k + 1], "URI path")
    for key_span, value_span in pairs:
        consume(key_span, value_span, "query string")

    return elements


def _split_path(data, start, end):
    """Return ``[(seg_start, seg_end), ...]`` for raw path segments."""
    segments = []
    i = start
    if i < end and data[i] == "/":
        i += 1
    while i < end:
        slash = data.find("/", i, end)
        if slash == -1:
            slash = end
        segments.append((i, slash))
        i = slash + 1
    # A trailing slash yields one empty segment anchored at ``end``; ignore it
    # so that "/01/x/" and "/01/x" are equivalent.
    if segments and segments[-1] == (end, end):
        segments.pop()
    return segments


def _split_query(data, start, end):
    """Return ``[(key_span, value_span), ...]`` for raw ``key=value`` pairs."""
    if start == -1:
        return []
    pairs = []
    i = start
    while i < end:
        amp = data.find("&", i, end)
        if amp == -1:
            amp = end
        equals = data.find("=", i, amp)
        if equals == -1:
            # Treat "AI" without '=' as AI with an empty value; the field
            # validation reports the empty-field error.
            pairs.append(((i, amp), (amp, amp)))
        else:
            pairs.append(((i, equals), (equals + 1, amp)))
        i = amp + 1
    return pairs


def _percent_decode(data: str, start: int, end: int) -> tuple[str, list[int]]:
    """Percent-decode ``data[start:end]`` as UTF-8.

    Returns the decoded text and, for every decoded character, the raw
    UTF-8 byte offset of the first byte it came from.
    """
    raw = bytearray()
    origin = []  # character index in ``data`` where each decoded byte began
    i = start
    while i < end:
        ch = data[i]
        if ch == "%":
            hex_ok = (
                i + 2 < end
                and data[i + 1] in HEX_DIGITS
                and data[i + 2] in HEX_DIGITS
            )
            if not hex_ok:
                raise GS1ParseError(
                    "INVALID_PERCENT_ENCODING",
                    f"Invalid percent-encoding at byte {_byte_offset(data, i)}: "
                    f"expected %XX but found {data[i:end]!r}",
                    offset=_byte_offset(data, i),
                    expected="percent-encoded UTF-8 byte of the form %XX",
                    found=data[i : min(end, i + 3)],
                )
            raw.append(int(data[i + 1 : i + 3], 16))
            origin.append(i)
            i += 3
        else:
            for b in ch.encode("utf-8"):
                raw.append(b)
                origin.append(i)
            i += 1

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        bad = origin[exc.start]
        raise GS1ParseError(
            "INVALID_PERCENT_ENCODING",
            "Percent-encoded bytes are not valid UTF-8: "
            f"0x{raw[exc.start]:02X} at byte {_byte_offset(data, bad)}",
            offset=_byte_offset(data, bad),
            expected="valid UTF-8 percent-encoded bytes",
            found=f"0x{raw[exc.start]:02X}",
        ) from None

    offsets = []
    b = 0
    for ch in text:
        offsets.append(_byte_offset(data, origin[b]))
        b += len(ch.encode("utf-8"))
    return text, offsets


def _validate_uri_value(spec, value, offsets, end_offset):
    """Length, charset and semantic checks for a percent-decoded URI value.

    Returns ``(decimal_value, unit)`` (both ``None`` outside the 310n/320n
    weight family).
    """
    if spec.fixed_length:
        if len(value) < spec.max_length:
            raise GS1ParseError(
                "FIELD_TOO_SHORT",
                f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                f"but found {len(value)} after percent-decoding",
                offset=end_offset,
                expected=f"{spec.max_length} characters for AI {spec.ai}",
                found=f"{len(value)} character(s)",
            )
        if len(value) > spec.max_length:
            raise GS1ParseError(
                "FIELD_TOO_LONG",
                f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                f"but found {len(value)} after percent-decoding",
                offset=offsets[spec.max_length],
                expected=f"{spec.max_length} characters for AI {spec.ai}",
                found=f"{len(value)} characters",
            )
    else:
        if len(value) < spec.min_length:
            raise GS1ParseError(
                "FIELD_EMPTY",
                f"AI {spec.ai} ({spec.name}) requires at least {spec.min_length} character",
                offset=end_offset,
                expected=f"{spec.min_length}-{spec.max_length} characters for AI {spec.ai}",
                found="empty field",
            )
        if len(value) > spec.max_length:
            raise GS1ParseError(
                "FIELD_TOO_LONG",
                f"AI {spec.ai} ({spec.name}) allows at most {spec.max_length} characters "
                f"but found {len(value)} after percent-decoding",
                offset=offsets[spec.max_length],
                expected=f"at most {spec.max_length} characters for AI {spec.ai}",
                found=f"{len(value)} characters",
            )

    _check_charset(spec, value, offsets)
    return _apply_semantics(spec, value, offsets)
