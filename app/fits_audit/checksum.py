"""FITS one's-complement checksum primitives.

Implements the algorithm of the FITS Checksum Convention (FITS Support
Office document, Appendix A.7 / the ``CHECKSUM`` proposal):

* data are summed as big-endian unsigned 32-bit words using one's-complement
  arithmetic (end-around carry);
* the resulting accumulator is character-encoded into the 16-byte ASCII
  value stored in the ``CHECKSUM`` card.

The implementation uses only the Python standard library and is byte-for-byte
compatible with Astropy's ``_compute_checksum`` / ``_char_encode`` (which in
turn follows the reference IRAF ``fchecksum`` behaviour).  Notably the data
block participating in a checksum is padded out to a multiple of 2880 bytes
with zero bytes, exactly as it physically appears in the file.
"""

import sys
from array import array

MASK32 = 0xFFFFFFFF

# Byte masks used while spreading a 32-bit word over 16 ASCII characters.
_MASKS = [0xFF000000, 0x00FF0000, 0x0000FF00, 0x000000FF]

# ASCII characters forbidden inside an encoded checksum (quoting characters
# and a few punctuation marks), per FITS Checksum Convention Appendix A.7.2.
_EXCLUDE = frozenset(
    [0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40,
     0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x60]
)


def ones_complement_sum(data: bytes | bytearray | memoryview, sum32: int = 0) -> int:
    """Return the 32-bit one's-complement sum of *data*.

    Bytes are grouped as big-endian 32-bit words; a final partial word is
    right-padded with zero bytes (so callers must pass physically padded
    blocks).  *sum32* allows chaining regions, e.g. header then data.
    """
    s = sum32 & MASK32
    n = len(data)
    full, rem = divmod(n, 4)

    mv = memoryview(data)
    if full:
        words = array("I")
        words.frombytes(bytes(mv[: full * 4]))
        # array uses native byte order; FITS words are big-endian.
        if sys.byteorder == "little":
            words.byteswap()
        s += sum(words)
        # Fold the high bits back (end-around carry).
        while s >> 32:
            s = (s & MASK32) + (s >> 32)

    if rem:
        tail = bytes(mv[full * 4:]) + b"\x00" * (4 - rem)
        s += int.from_bytes(tail, "big")
        while s >> 32:
            s = (s & MASK32) + (s >> 32)

    return s & MASK32


def _encode_byte(byte: int) -> tuple[int, int, int, int]:
    """Encode one byte into four ASCII code points (Appendix A.7.2)."""
    quotient = byte // 4 + ord("0")
    remainder = byte % 4
    ch = [quotient + remainder, quotient, quotient, quotient]

    changed = True
    while changed:
        changed = False
        for x in _EXCLUDE:
            for j in (0, 2):
                if ch[j] == x or ch[j + 1] == x:
                    ch[j] += 1
                    ch[j + 1] -= 1
                    changed = True
    return ch[0], ch[1], ch[2], ch[3]


def char_encode(value: int) -> str:
    """Character-encode a 32-bit checksum word into a 16-character string."""
    value &= MASK32
    asc = [0] * 16
    for i in range(4):
        byte = (value & _MASKS[i]) >> ((3 - i) * 8)
        ch = _encode_byte(byte)
        for j in range(4):
            asc[4 * j + i] = ch[j]

    out = bytearray(16)
    for i in range(16):
        out[i] = asc[(i + 15) % 16]
    return out.decode("ascii")


def datasum_of(data_block: bytes | bytearray | memoryview) -> int:
    """Compute the unsigned 32-bit DATASUM of a (2880-padded) data block."""
    return ones_complement_sum(data_block, 0)


def checksum_string(header_block: bytes, data_block: bytes | bytearray | memoryview,
                    datasum: int) -> str:
    """Compute the expected CHECKSUM card value.

    *header_block* must already have its CHECKSUM card value replaced by
    sixteen ASCII ``'0'`` characters (as the convention requires), and both
    blocks must have their physical (2880-multiple) lengths.
    """
    cs = ones_complement_sum(header_block, datasum & MASK32)
    return char_encode(cs ^ MASK32)
