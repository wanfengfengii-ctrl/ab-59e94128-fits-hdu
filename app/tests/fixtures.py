"""Minimal FITS fixture builder (standard library only).

Produces well-formed FITS byte streams whose DATASUM/CHECKSUM cards are
computed with the exact FITS checksum convention, plus a collection of
corruption helpers used by the test-suite and the smoke verifier.
"""

import struct

from fits_audit.checksum import checksum_string, datasum_of

BLOCK = 2880
CARD = 80


def _pad80(card: bytes) -> bytes:
    assert len(card) <= CARD
    return card + b" " * (CARD - len(card))


def bool_card(keyword: str, value: bool, comment: str | None = None) -> bytes:
    text = f"{keyword:<8}= {'T' if value else 'F':>20}"
    if comment is not None:
        text += f" / {comment}"
    return _pad80(text.encode("ascii"))


def int_card(keyword: str, value: int, comment: str | None = None) -> bytes:
    text = f"{keyword:<8}= {value:>20d}"
    if comment is not None:
        text += f" / {comment}"
    return _pad80(text.encode("ascii"))


def string_card(keyword: str, value: str, comment: str | None = None,
                field: int | None = None) -> bytes:
    if field is not None:
        rendered = value.ljust(field)[:field]
    else:
        rendered = value
    text = f"{keyword:<8}= '{rendered}'"
    if comment is not None:
        text += f" / {comment}"
    return _pad80(text.encode("ascii"))


def raw_card(text: str) -> bytes:
    return _pad80(text.encode("ascii"))


def end_card() -> bytes:
    return _pad80(b"END")


_STRUCT = {
    8: ("B", 1),
    16: (">h", 2),
    32: (">i", 4),
    64: (">q", 8),
    -32: (">f", 4),
    -64: (">d", 8),
}


def encode_values(bitpix: int, values):
    fmt, width = _STRUCT[bitpix]
    if bitpix == 8:
        return bytes(v & 0xFF for v in values)
    return b"".join(struct.pack(fmt, v) for v in values)


def data_block(logical: bytes) -> bytes:
    rem = len(logical) % BLOCK
    if rem:
        logical += b"\x00" * (BLOCK - rem)
    return logical


def header_block(blob: bytes) -> bytes:
    out = bytes(blob)
    rem = len(out) % BLOCK
    if rem:
        out += b" " * (BLOCK - rem)
    return out


def build_hdu(*, primary, bitpix=8, axes=None, data=b"", extend=True,
              extname=None, extra_cards=None, datasum=True, checksum=True,
              pcount=None, gcount=None, bad_end_padding_byte=None,
              insert_before_end=None):
    """Return (header_without_checksum_cards, data_block) pieces builder.

    Use :func:`build_file` for a complete HDU; this returns the raw card list
    and data so tests can mutate intermediate state.
    """
    axes = list(axes or [])
    naxis = len(axes)
    cards = []
    if primary:
        cards.append(bool_card("SIMPLE", True, "conforms to FITS standard"))
    else:
        cards.append(string_card("XTENSION", "IMAGE",
                                 "IMAGE extension", field=8))
    cards.append(int_card("BITPIX", bitpix, "array data type"))
    cards.append(int_card("NAXIS", naxis, "number of array dimensions"))
    for i, axis in enumerate(axes, start=1):
        cards.append(int_card(f"NAXIS{i}", axis))
    if primary:
        if extend:
            cards.append(bool_card("EXTEND", True))
        if pcount is not None:
            cards.append(int_card("PCOUNT", pcount))
        if gcount is not None:
            cards.append(int_card("GCOUNT", gcount))
    else:
        cards.append(int_card("PCOUNT", 0, "number of parameters"))
        cards.append(int_card("GCOUNT", 1, "number of groups"))
    if extname is not None:
        cards.append(string_card("EXTNAME", extname, field=len(extname)))
    if extra_cards:
        cards.extend(extra_cards)
    if insert_before_end:
        cards.extend(insert_before_end)

    payload = data_block(bytes(data))

    # DATASUM first, then CHECKSUM (CHECKSSUM zeroed during its own
    # computation) - astropy writes CHECKSUM before DATASUM, but the order is
    # irrelevant to the convention.
    dsum = datasum_of(payload) if payload else 0
    if datasum:
        cards.append(string_card("DATASUM", str(dsum)))

    if checksum:
        cards.append(string_card("CHECKSUM", "0" * 16))
    cards.append(end_card())

    header = header_block(b"".join(cards))
    if checksum:
        cs = checksum_string(header, payload, dsum)
        cards[-2] = string_card("CHECKSUM", cs)
        header = header_block(b"".join(cards))

    if bad_end_padding_byte is not None:
        # Corrupt the padding after END (first pad byte).
        header = bytearray(header)
        used = (len(cards)) * CARD
        header[used] = bad_end_padding_byte
        header = bytes(header)

    return header, payload


def build_file(hdus) -> bytes:
    """*hdus*: list of (header, data) tuples from :func:`build_hdu`."""
    return b"".join(h + d for h, d in hdus)


# -- ready-made fixtures -----------------------------------------------------

def valid_primary(bitpix=8, axes=None, values=None, **kw):
    if axes is None:
        axes = []
    if values is None:
        n = 1
        for a in axes:
            n *= a
        values = list(range(n))
    data = encode_values(bitpix, values) if axes else b""
    return build_hdu(primary=True, bitpix=bitpix, axes=axes, data=data, **kw)


def valid_image(bitpix=16, axes=(4, 3), values=None, **kw):
    if values is None:
        n = 1
        for a in axes:
            n *= a
        values = list(range(n))
    data = encode_values(bitpix, values)
    return build_hdu(primary=False, bitpix=bitpix, axes=list(axes),
                     data=data, **kw)


def valid_multi_fits(n_ext=2):
    hdus = [valid_primary()]
    for i in range(n_ext):
        hdus.append(valid_image(bitpix=8, axes=(6,),
                                values=[i * 10 + j for j in range(6)],
                                extname=f"EXT{i}"))
    return build_file(hdus)
