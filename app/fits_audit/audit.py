"""Strict FITS auditor.

The auditor consumes the raw bytes of a file that is supposed to contain one
primary HDU followed by at most fifteen IMAGE extensions.  It enforces, in
byte-offset order:

* every header card is exactly 80 bytes of printable ASCII;
* mandatory cards (``SIMPLE``/``XTENSION``, ``BITPIX``, ``NAXIS``,
  ``NAXISn``, ``PCOUNT``, ``GCOUNT``) are present, in order and mutually
  consistent;
* every header block ends with ``END`` and is padded to 2880 bytes with
  spaces, every data block is padded with zero bytes only;
* data length follows from ``BITPIX``/``NAXIS``/``PCOUNT``/``GCOUNT`` and the
  whole HDU lies inside the file, with no trailing bytes afterwards;
* ``DATASUM`` and ``CHECKSUM`` cards (both mandatory per archival policy)
  match the standard FITS one's-complement recomputation.

The first violation encountered aborts the audit and is reported with a
stable reason code and a locatable byte offset.
"""

from dataclasses import dataclass, field
import re

from .checksum import MASK32, checksum_string, datasum_of

CARD_LEN = 80
BLOCK_LEN = 2880
CARDS_PER_BLOCK = BLOCK_LEN // CARD_LEN
MAX_FILE_SIZE = 16 * 1024 * 1024
MAX_EXTENSIONS = 15
MAX_HDUS = MAX_EXTENSIONS + 1

VALID_BITPIX = (8, 16, 32, 64, -32, -64)
BITPIX_BYTES = {8: 1, 16: 2, 32: 4, 64: 8, -32: 4, -64: 8}

KEYWORD_RE = re.compile(rb"^[A-Z][A-Z0-9_-]{0,7}$")
INT_RE = re.compile(rb"^[+-]?[0-9]+$")
COMMENTARY_KEYWORDS = {b"COMMENT", b"HISTORY", b""}


class AuditFailure(Exception):
    """First fatal violation found while auditing a file."""

    def __init__(self, reason: str, offset: int, message: str,
                 hdu: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.offset = offset
        self.message = message
        self.hdu = hdu


@dataclass
class Card:
    index: int
    keyword: bytes
    kind: str            # 'value' | 'commentary' | 'end'
    raw: bytes
    value_type: str | None = None   # 'bool' | 'int' | 'float' | 'string'
    value: object = None
    quote: tuple[int, int] | None = None  # quote offsets within the card


@dataclass
class Verdict:
    status: str                      # 'valid' | 'missing' | 'mismatch' | 'malformed'
    offset: int | None = None
    stored: str | None = None
    expected: str | None = None


@dataclass
class HDUReport:
    index: int
    type: str
    byte_range: tuple[int, int]
    data_bytes: int
    datasum: Verdict
    checksum: Verdict


@dataclass
class AuditResult:
    accepted: bool
    size_bytes: int
    hdus: list[HDUReport] = field(default_factory=list)
    reason: str | None = None
    message: str | None = None
    hdu: int | None = None
    offset: int | None = None

    def to_dict(self) -> dict:
        out = {
            "status": "accepted" if self.accepted else "rejected",
            "conclusion": "ACCEPT" if self.accepted else "REJECT",
            "size_bytes": self.size_bytes,
            "hdus": [
                {
                    "index": h.index,
                    "type": h.type,
                    "byte_range": [h.byte_range[0], h.byte_range[1]],
                    "data_bytes": h.data_bytes,
                    "datasum": {
                        "status": h.datasum.status,
                        "offset": h.datasum.offset,
                        "stored": h.datasum.stored,
                        "actual": h.datasum.expected,
                    },
                    "checksum": {
                        "status": h.checksum.status,
                        "offset": h.checksum.offset,
                        "stored": h.checksum.stored,
                        "expected": h.checksum.expected,
                    },
                }
                for h in self.hdus
            ],
        }
        if self.accepted:
            out["error"] = None
        else:
            out["error"] = {
                "hdu": self.hdu,
                "reason": self.reason,
                "offset": self.offset,
                "message": self.message,
            }
        return out


def _fail(reason, offset, message, hdu=None):
    raise AuditFailure(reason, offset, message, hdu)


def _parse_value_card(card: bytes, base: int, hdu: int):
    """Parse the value portion (columns 10..79) of a standard 80-byte card."""
    rest = card[CARD_LEN - 70:]  # == card[10:]

    i = 0
    while i < len(rest) and rest[i] == 0x20:
        i += 1

    if i < len(rest) and rest[i] == 0x27:  # apostrophe -> character string
        start = i
        j = i + 1
        while True:
            if j >= len(rest):
                _fail("INVALID_CARD_VALUE", base,
                      "unterminated character string in header card", hdu)
            if rest[j] == 0x27:
                if j + 1 < len(rest) and rest[j + 1] == 0x27:
                    j += 2
                    continue
                end = j
                break
            j += 1

        k = end + 1
        while k < len(rest) and rest[k] == 0x20:
            k += 1
        if k < len(rest) and rest[k] != 0x2F:
            _fail("INVALID_CARD_VALUE", base,
                  "unparsed characters after character string", hdu)

        raw_value = rest[start + 1:end]
        return Card(0, b"", "value", card, "string", raw_value,
                    (10 + start, 10 + end))

    slash = rest.find(b"/")
    token = rest[:slash] if slash >= 0 else rest
    token = token.strip()
    if not token:
        _fail("INVALID_CARD_VALUE", base, "missing keyword value", hdu)

    if token == b"T" or token == b"F":
        # FITS 4.2.1.3: the logical value is a single T or F right-aligned
        # in the twenty-character value field (card column 30, i.e. rest
        # index 19). Enforcing the column prevents lenient readers from
        # accepting malformed cards.
        if slash < 0:
            field = rest[:20]
        else:
            field = rest[:min(slash, 20)]
        if len(field) != 20 or field[19] not in (0x54, 0x46) or \
                field[:19].strip(b" ") != b"":
            _fail("INVALID_CARD_VALUE", base,
                  "logical value T/F must occupy card column 30", hdu)
        return Card(0, b"", "value", card, "bool", token == b"T")
    if INT_RE.match(token):
        return Card(0, b"", "value", card, "int", int(token))
    # Everything else numeric-looking is accepted generically (floats,
    # complex values); archival validation only constrains mandatory cards.
    return Card(0, b"", "value", card, "float", token.decode("ascii"))


def _read_header(buf: bytes, start: int, hdu: int) -> tuple[list[Card], int]:
    """Read one header block starting at *start*; return cards and its size."""
    cards: list[Card] = []
    seen: set[bytes] = set()
    ci = 0
    while True:
        cstart = start + ci * CARD_LEN
        if cstart + CARD_LEN > len(buf):
            _fail("TRUNCATED_HEADER", cstart,
                  "header card / END block runs past end of file", hdu)
        raw = buf[cstart:cstart + CARD_LEN]

        for j, b in enumerate(raw):
            if b < 0x20 or b > 0x7E:
                _fail("NON_ASCII_CARD", cstart + j,
                      "header card contains a byte outside printable ASCII",
                      hdu)

        keyword = raw[:8].rstrip(b" ")

        if keyword == b"END":
            if raw[3:] != b" " * (CARD_LEN - 3):
                _fail("INVALID_CARD", cstart,
                      "END card must contain only spaces after 'END'", hdu)
            cards.append(Card(ci, b"END", "end", raw))
            break

        has_indicator = raw[8:10] == b"= "
        if not has_indicator:
            if keyword in COMMENTARY_KEYWORDS:
                cards.append(Card(ci, keyword, "commentary", raw))
            else:
                _fail("INVALID_CARD", cstart,
                      f"non-commentary keyword {keyword.decode('ascii')!r} "
                      "lacks the '= ' value indicator", hdu)
        else:
            if not KEYWORD_RE.match(keyword):
                _fail("INVALID_CARD", cstart,
                      f"illegal keyword {keyword.decode('ascii')!r}", hdu)
            if keyword in seen:
                _fail("DUPLICATE_KEYWORD", cstart,
                      f"keyword {keyword.decode('ascii')} appears more than once",
                      hdu)
            seen.add(keyword)
            parsed = _parse_value_card(raw, cstart, hdu)
            parsed.index = ci
            parsed.keyword = keyword
            cards.append(parsed)

        ci += 1

    end_index = cards[-1].index
    used_cards = end_index + 1
    hblocks = (used_cards + CARDS_PER_BLOCK - 1) // CARDS_PER_BLOCK
    hbytes = hblocks * BLOCK_LEN
    if start + hbytes > len(buf):
        _fail("TRUNCATED_HEADER", len(buf),
              "header padding to a 2880-byte block runs past end of file", hdu)

    pad_start = start + used_cards * CARD_LEN
    for off in range(pad_start, start + hbytes):
        if buf[off] != 0x20:
            _fail("INVALID_HEADER_PADDING", off,
                  "header block padding must consist of ASCII spaces", hdu)

    return cards, hbytes


def _value_card(cards: list[Card], keyword: bytes):
    for c in cards:
        if c.kind == "value" and c.keyword == keyword:
            return c
    return None


def _validate_structure(cards: list[Card], start: int, hdu: int,
                        is_primary: bool) -> dict:
    """Validate mandatory-card order/values; return key parameters."""
    def at(pos):
        return cards[pos] if pos < len(cards) and cards[pos].kind != "end" else None

    def require(pos, keyword):
        c = at(pos)
        where = start + pos * CARD_LEN
        if c is None:
            _fail("MISSING_KEYWORD", where,
                  f"required keyword {keyword.decode()} is missing "
                  "(END reached early)", hdu)
        if c.keyword != keyword:
            _fail("UNEXPECTED_KEYWORD", where,
                  f"expected keyword {keyword.decode()} at card position {pos}, "
                  f"found {c.keyword.decode('ascii')}", hdu)
        return c

    first = cards[0]
    if is_primary:
        if first.keyword != b"SIMPLE":
            _fail("INVALID_PRIMARY", start,
                  "first HDU must begin with a SIMPLE = T card", hdu)
        if first.value_type != "bool" or first.value is not True:
            _fail("INVALID_KEYWORD_VALUE", start,
                  "SIMPLE must have the boolean value T", hdu)
    else:
        if first.keyword == b"SIMPLE":
            _fail("DUPLICATE_PRIMARY", start,
                  "only the first HDU may carry a SIMPLE card", hdu)
        if first.keyword != b"XTENSION":
            _fail("TRAILING_BYTES", start,
                  "trailing bytes do not begin with an extension HDU", hdu)
        if first.value_type != "string":
            _fail("INVALID_KEYWORD_VALUE", start,
                  "XTENSION must be a character string", hdu)
        if first.value.rstrip(b" ") != b"IMAGE":
            _fail("UNSUPPORTED_EXTENSION", start,
                  "only IMAGE extensions are accepted, found XTENSION = "
                  f"{first.value.rstrip(b' ').decode('ascii', 'replace')!r}",
                  hdu)

    bitpix_card = require(1, b"BITPIX")
    if bitpix_card.value_type != "int" or bitpix_card.value not in VALID_BITPIX:
        _fail("INVALID_KEYWORD_VALUE", start + CARD_LEN,
              "BITPIX must be one of 8, 16, 32, 64, -32, -64", hdu)

    naxis_card = require(2, b"NAXIS")
    if naxis_card.value_type != "int" or not (0 <= naxis_card.value <= 999):
        _fail("INVALID_KEYWORD_VALUE", start + 2 * CARD_LEN,
              "NAXIS must be an integer between 0 and 999", hdu)
    naxis = naxis_card.value

    axes = []
    for axis_no in range(1, naxis + 1):
        pos = 2 + axis_no
        kw = f"NAXIS{axis_no}".encode()
        card = require(pos, kw)
        if card.value_type != "int":
            _fail("INVALID_KEYWORD_VALUE", start + pos * CARD_LEN,
                  f"{kw.decode()} must be an integer", hdu)
        if card.value < 0:
            _fail("INVALID_KEYWORD_VALUE", start + pos * CARD_LEN,
                  f"{kw.decode()} must not be negative", hdu)
        axes.append(card.value)

    after_axes = 2 + naxis

    if is_primary:
        # In the primary header PCOUNT/GCOUNT are optional and default to
        # 0/1 (and astropy never writes them); when present they must sit
        # at the standard positions immediately after the last NAXISn.
        pcount, gcount = 0, 1
        for offset, name, expected in (
            (1, "PCOUNT", 0),
            (2, "GCOUNT", 1),
        ):
            c = at(after_axes + offset)
            kw = name.encode()
            present = _value_card(cards, kw)
            if present is not None and present.index != after_axes + offset:
                _fail("UNEXPECTED_KEYWORD",
                      start + present.index * CARD_LEN,
                      f"{name} must immediately follow the last NAXISn card",
                      hdu)
            if c is not None and c.keyword == kw:
                if c.value_type != "int" or c.value < 0:
                    _fail("INVALID_KEYWORD_VALUE",
                          start + c.index * CARD_LEN,
                          f"{name} must be a non-negative integer", hdu)
                if c.value != expected:
                    _fail(f"{name}_CONFLICT", start + c.index * CARD_LEN,
                          f"an IMAGE primary HDU must have {name} = {expected}",
                          hdu)
                if name == "PCOUNT":
                    pcount = c.value
                else:
                    gcount = c.value
            elif present is not None:
                # Present but displaced: position failure already raised.
                if present.value_type != "int" or present.value < 0:
                    _fail("INVALID_KEYWORD_VALUE",
                          start + present.index * CARD_LEN,
                          f"{name} must be a non-negative integer", hdu)
    else:
        pcount_card = require(after_axes + 1, b"PCOUNT")
        if pcount_card.value_type != "int" or pcount_card.value < 0:
            _fail("INVALID_KEYWORD_VALUE",
                  start + (after_axes + 1) * CARD_LEN,
                  "PCOUNT must be a non-negative integer", hdu)
        if pcount_card.value != 0:
            _fail("PCOUNT_CONFLICT", start + (after_axes + 1) * CARD_LEN,
                  "an IMAGE HDU must have PCOUNT = 0", hdu)

        gcount_card = require(after_axes + 2, b"GCOUNT")
        if gcount_card.value_type != "int" or gcount_card.value != 1:
            _fail("GCOUNT_CONFLICT", start + (after_axes + 2) * CARD_LEN,
                  "an IMAGE HDU must have GCOUNT = 1", hdu)
        pcount, gcount = 0, 1

    # EXTEND must sit at card index NAXIS+3 in the primary HDU
    # (immediately after the last NAXISn) and carry a boolean value.
    extend_card = _value_card(cards, b"EXTEND")
    extend_value = False
    if extend_card is not None:
        if not is_primary:
            _fail("UNEXPECTED_KEYWORD", start + extend_card.index * CARD_LEN,
                  "EXTEND is only valid in the primary HDU", hdu)
        if extend_card.index != after_axes + 1:
            _fail("UNEXPECTED_KEYWORD", start + extend_card.index * CARD_LEN,
                  "EXTEND must immediately follow the last NAXISn card", hdu)
        if extend_card.value_type != "bool":
            _fail("INVALID_KEYWORD_VALUE",
                  start + extend_card.index * CARD_LEN,
                  "EXTEND must be boolean T or F", hdu)
        extend_value = extend_card.value is True

    # Any NAXISn outside 1..NAXIS is a structural conflict (duplicates of
    # NAXIS1..NAXISn were already rejected during card scanning).
    for c in cards:
        if c.kind == "value" and c.keyword.startswith(b"NAXIS") and \
                c.keyword != b"NAXIS":
            suffix = c.keyword[5:]
            if suffix.isdigit() and (int(suffix) < 1 or int(suffix) > naxis):
                _fail("AXIS_KEYWORD_CONFLICT",
                      start + c.index * CARD_LEN,
                      f"{c.keyword.decode()} conflicts with NAXIS = {naxis}",
                      hdu)

    datasum_card = _value_card(cards, b"DATASUM")
    checksum_card = _value_card(cards, b"CHECKSUM")
    if datasum_card is not None:
        # The convention permits a fixed-width string padded with spaces
        # (this is how the reference implementation writes DATASUM), but
        # the value must be an unsigned 32-bit decimal word.
        dtext = datasum_card.value.rstrip(b" ") if (
            datasum_card.value_type == "string") else b""
        if not re.match(rb"^[0-9]+$", dtext):
            bad_datasum = True
        else:
            bad_datasum = int(dtext) > MASK32
        if bad_datasum:
            _fail("MALFORMED_DATASUM",
                  start + datasum_card.index * CARD_LEN,
                  "DATASUM must be a decimal string of a 32-bit unsigned "
                  "word", hdu)
    if checksum_card is not None:
        if checksum_card.value_type != "string" or \
                len(checksum_card.value) != 16:
            _fail("MALFORMED_CHECKSUM",
                  start + checksum_card.index * CARD_LEN,
                  "CHECKSUM must hold a 16-character encoded string", hdu)

    return {
        "bitpix": bitpix_card.value,
        "naxis": naxis,
        "axes": axes,
        "pcount": pcount,
        "gcount": gcount,
        "extend": extend_value,
        "datasum_card": datasum_card,
        "checksum_card": checksum_card,
    }


def _audit_hdu(buf: bytes, start: int, index: int) -> tuple[HDUReport, int, bool]:
    is_primary = index == 0
    cards, hbytes = _read_header(buf, start, index)
    params = _validate_structure(cards, start, index, is_primary)

    n_elements = 0 if params["naxis"] == 0 else 1
    for axis in params["axes"]:
        n_elements *= axis  # a zero-length axis yields an empty data block
    logical = (BITPIX_BYTES[params["bitpix"]]
               * (params["pcount"] + params["gcount"] * n_elements))
    data_blocks = (logical + BLOCK_LEN - 1) // BLOCK_LEN
    padded_data = data_blocks * BLOCK_LEN

    data_start = start + hbytes
    data_end = data_start + padded_data
    if data_end > len(buf):
        _fail("TRUNCATED_DATA", data_start,
              f"data block requires {padded_data} padded bytes but only "
              f"{len(buf) - data_start} remain in the file", index)

    # Padding must never hide data.
    for off in range(data_start + logical, data_end):
        if buf[off] != 0:
            _fail("NONZERO_PADDING", off,
                  "non-zero byte found inside the data block padding", index)

    header_block = buf[start:data_start]
    data_block = buf[data_start:data_end]
    actual_datasum = datasum_of(data_block) if data_block else 0

    # ---- DATASUM verdict --------------------------------------------------
    dcard = params["datasum_card"]
    if dcard is None:
        datasum_verdict = Verdict("missing")
    else:
        stored = dcard.value.rstrip(b" ").decode("ascii")
        datasum_verdict = Verdict(
            "valid" if int(stored) == actual_datasum else "mismatch",
            start + dcard.index * CARD_LEN,
            stored,
            str(actual_datasum),
        )

    # ---- CHECKSUM verdict -------------------------------------------------
    ccard = params["checksum_card"]
    if ccard is None:
        checksum_verdict = Verdict("missing")
    else:
        q0, q1 = ccard.quote
        stored_cs = ccard.value.decode("ascii")
        zeroed = bytearray(header_block)
        card_abs = start + ccard.index * CARD_LEN
        for p in range(card_abs + q0 + 1, card_abs + q1):
            zeroed[p - start] = 0x30  # ASCII '0'
        expected_cs = checksum_string(zeroed, data_block, actual_datasum)
        checksum_verdict = Verdict(
            "valid" if stored_cs == expected_cs else "mismatch",
            card_abs,
            stored_cs,
            expected_cs,
        )

    # Archival policy: both standard checksum words must be present.
    end_off = start + cards[-1].index * CARD_LEN
    if dcard is None:
        _fail("MISSING_DATASUM", end_off,
              "DATASUM card is required by archival policy", index)
    if ccard is None:
        _fail("MISSING_CHECKSUM", end_off,
              "CHECKSUM card is required by archival policy", index)
    if datasum_verdict.status == "mismatch":
        _fail("DATASUM_MISMATCH", datasum_verdict.offset,
              f"stored DATASUM {datasum_verdict.stored} does not match "
              f"recomputed value {datasum_verdict.expected}", index)
    if checksum_verdict.status == "mismatch":
        _fail("CHECKSUM_MISMATCH", checksum_verdict.offset,
              f"stored CHECKSUM {checksum_verdict.stored!r} does not match "
              f"recomputed value {checksum_verdict.expected!r}", index)

    report = HDUReport(
        index=index,
        type="PRIMARY" if is_primary else "IMAGE",
        byte_range=(start, data_end),
        data_bytes=logical,
        datasum=datasum_verdict,
        checksum=checksum_verdict,
    )
    return report, data_end, params["extend"]


def audit(buf: bytes) -> AuditResult:
    """Audit complete FITS file bytes; never raises for malformed input."""
    size = len(buf)
    if size == 0:
        return AuditResult(False, size, reason="EMPTY_FILE",
                           message="the submitted file is empty",
                           hdu=None, offset=0)
    if size > MAX_FILE_SIZE:
        return AuditResult(False, size, reason="FILE_TOO_LARGE",
                           message=f"file exceeds {MAX_FILE_SIZE} bytes",
                           hdu=None, offset=MAX_FILE_SIZE)

    reports: list[HDUReport] = []
    primary_extend = False
    pos = 0
    index = 0
    try:
        while pos < size:
            if index >= MAX_HDUS:
                _fail("TOO_MANY_HDUS", pos,
                      f"a file may contain at most {MAX_HDUS} HDUs "
                      "(primary + 15 IMAGE extensions)", index)

            # Bytes that cannot even hold one header block, or that do not
            # start with a recognisable extension, are trailing junk:
            # typical of truncation or concatenation.
            if index > 0:
                if size - pos < BLOCK_LEN:
                    _fail("TRAILING_BYTES", pos,
                          f"{size - pos} trailing byte(s) cannot form an HDU",
                          index)
                first8 = buf[pos:pos + 8].rstrip(b" ")
                if first8 == b"SIMPLE":
                    _fail("DUPLICATE_PRIMARY", pos,
                          "only the first HDU may carry a SIMPLE card", index)
                if first8 != b"XTENSION":
                    _fail("TRAILING_BYTES", pos,
                          "trailing bytes do not begin with an extension HDU",
                          index)
                if not primary_extend:
                    _fail("MISSING_EXTEND", pos,
                          "extension HDU present but primary HDU lacks "
                          "EXTEND = T", index)

            report, end, extend_flag = _audit_hdu(buf, pos, index)
            reports.append(report)
            if index == 0:
                primary_extend = extend_flag

            pos = end
            index += 1
    except AuditFailure as exc:
        return AuditResult(False, size, hdus=reports, reason=exc.reason,
                           message=exc.message, hdu=exc.hdu,
                           offset=exc.offset)

    return AuditResult(True, size, hdus=reports)
