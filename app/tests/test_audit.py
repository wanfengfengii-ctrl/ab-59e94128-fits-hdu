"""Unit tests for the FITS auditor and checksum primitives."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fits_audit import audit as audit_mod
from fits_audit.audit import audit, BLOCK_LEN, CARD_LEN
from fits_audit.checksum import (char_encode, checksum_string,
                                 ones_complement_sum, datasum_of)
from tests.fixtures import (build_file, build_hdu, data_block, encode_values,
                            end_card, header_block, int_card, raw_card,
                            string_card, bool_card, valid_image,
                            valid_multi_fits, valid_primary)


class ChecksumTests(unittest.TestCase):
    def test_word_sum_basic(self):
        self.assertEqual(ones_complement_sum(b"\x00\x00\x00\x01"), 1)
        self.assertEqual(ones_complement_sum(b"\xff\xff\xff\xff"), 0xFFFFFFFF)

    def test_end_around_carry(self):
        # 0xFFFFFFFF + 0x00000002 = 0x1_00000001 -> fold -> 2
        self.assertEqual(
            ones_complement_sum(b"\xff\xff\xff\xff\x00\x00\x00\x02"), 2)

    def test_partial_word_padded(self):
        self.assertEqual(ones_complement_sum(b"\x00\x00\x01"), 0x0100)

    def test_chain_with_sum32(self):
        a = b"\x10\x00\x00\x00"
        b = b"\x02\x00\x00\x00"
        self.assertEqual(
            ones_complement_sum(b, ones_complement_sum(a)),
            ones_complement_sum(a + b),
        )

    def test_char_encode_excluded_chars_never_used(self):
        excluded = set(b":;<=>?@[\\]^_`")
        for value in range(0, 0x10000, 0x97):
            encoded = char_encode(value).encode("ascii")
            self.assertFalse(excluded.intersection(encoded))

    def test_checksum_roundtrip(self):
        # The CHECKSUM stored by the fixture must equal a fresh recomputation
        # performed with the stored card blanked to sixteen zero characters.
        blob = build_file([valid_primary(
            bitpix=8, axes=[4], values=[1, 2, 3, 4])])
        h = audit(blob)
        self.assertTrue(h.accepted, h.message)
        report = h.hdus[0]
        self.assertEqual(report.checksum.status, "valid")

        # Independently re-derive it from the bytes on disk.
        pos = blob.find(b"CHECKSUM")
        q0 = blob.find(b"'", pos) + 1
        zeroed = bytearray(blob[:BLOCK_LEN])
        for p in range(q0, q0 + 16):
            zeroed[p] = ord("0")
        expected = checksum_string(
            zeroed, blob[BLOCK_LEN:2 * BLOCK_LEN],
            int(report.datasum.expected))
        self.assertEqual(expected, report.checksum.stored)


class ValidFileTests(unittest.TestCase):
    def test_minimal_primary(self):
        result = audit(build_file([valid_primary()]))
        self.assertTrue(result.accepted, result.message)
        self.assertEqual(len(result.hdus), 1)
        h = result.hdus[0]
        self.assertEqual(h.type, "PRIMARY")
        self.assertEqual(h.byte_range, (0, BLOCK_LEN))
        self.assertEqual(h.data_bytes, 0)
        self.assertEqual(h.datasum.status, "valid")
        self.assertEqual(h.checksum.status, "valid")

    def test_primary_with_data_and_extensions(self):
        h0 = valid_primary(bitpix=16, axes=(3, 2),
                           values=[1, -2, 3, -4, 5, -6])
        h1 = valid_image(bitpix=-32, axes=(2, 2),
                         values=[1.0, 2.5, -3.25, 4.125])
        h2 = valid_image(bitpix=8, axes=(10,),
                         values=list(range(10)), extname="PLANE")
        result = audit(build_file([h0, h1, h2]))
        self.assertTrue(result.accepted, result.message)
        self.assertEqual([h.type for h in result.hdus],
                         ["PRIMARY", "IMAGE", "IMAGE"])
        # h0: header + one data block; h1: header + 16-byte payload; ...
        self.assertEqual(result.hdus[0].byte_range, (0, 2 * BLOCK_LEN))
        self.assertEqual(result.hdus[1].data_bytes, 16)
        self.assertEqual(result.hdus[1].byte_range,
                         (2 * BLOCK_LEN, 4 * BLOCK_LEN))
        self.assertEqual(result.hdus[2].byte_range,
                         (4 * BLOCK_LEN, 6 * BLOCK_LEN))

    def test_64bit_and_zero_axis(self):
        h0 = valid_primary(bitpix=64, axes=[100],
                           values=list(range(100)))
        result = audit(build_file([h0]))
        self.assertTrue(result.accepted, result.message)
        self.assertEqual(result.hdus[0].data_bytes, 800)

    def test_sixteen_extensions_accepted(self):
        hdus = [valid_primary()]
        for i in range(15):
            hdus.append(valid_image(bitpix=8, axes=[1], values=[i]))
        result = audit(build_file(hdus))
        self.assertTrue(result.accepted, result.message)
        self.assertEqual(len(result.hdus), 16)

    def test_primary_without_extend_is_valid_alone(self):
        h0 = valid_primary(extend=False)
        result = audit(build_file([h0]))
        self.assertTrue(result.accepted, result.message)

    def test_multi_block_header(self):
        # More than 36 cards forces a second 2880-byte header block.
        extras = [int_card(f"CUST{i:02d}", i, f"custom card {i}")
                  for i in range(40)]
        h0 = valid_primary(extra_cards=extras)
        header, payload = h0
        self.assertGreater(len(header), BLOCK_LEN)
        result = audit(build_file([h0]))
        self.assertTrue(result.accepted, result.message)
        self.assertEqual(result.hdus[0].byte_range[1],
                         2 * BLOCK_LEN)  # header only, no data

    def test_zero_length_axis_yields_empty_data_block(self):
        # NAXIS2 = 0 -> zero elements -> no data block at all.
        header, payload = build_hdu(
            primary=False, bitpix=8, axes=(4, 0), data=b"")
        self.assertEqual(payload, b"")
        blob = build_file([valid_primary()]) + header + payload
        result = audit(blob)
        self.assertTrue(result.accepted, result.message)
        self.assertEqual(result.hdus[1].data_bytes, 0)
        self.assertEqual(result.hdus[1].byte_range,
                         (BLOCK_LEN, 2 * BLOCK_LEN))

    def test_fixed_width_datasum_with_spaces(self):
        # Rewrite the DATASUM card value padded to a wider field, the way
        # the reference implementation writes it, re-signing CHECKSUM after.
        from fits_audit.checksum import datasum_of, checksum_string
        header0, payload = valid_primary(
            bitpix=8, axes=(2,), values=(10, 20))
        dsum = datasum_of(payload)
        cards = [
            bool_card("SIMPLE", True),
            int_card("BITPIX", 8), int_card("NAXIS", 1),
            int_card("NAXIS1", 2), bool_card("EXTEND", True),
            raw_card(f"DATASUM = '{str(dsum).ljust(20)}' / padded field"),
            string_card("CHECKSUM", "0" * 16),
            end_card(),
        ]
        h = header_block(b"".join(cards))
        cs = checksum_string(h, payload, dsum)
        cards[-2] = string_card("CHECKSUM", cs)
        h = header_block(b"".join(cards))
        result = audit(h + payload)
        self.assertTrue(result.accepted, result.message)
        self.assertEqual(result.hdus[0].datasum.stored, str(dsum))


class StructureFailureTests(unittest.TestCase):
    def _reject(self, blob, reason, hdu=None):
        result = audit(blob)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, reason, result.message)
        if hdu is not None:
            self.assertEqual(result.hdu, hdu)
        self.assertIsInstance(result.offset, int)
        self.assertGreaterEqual(result.offset, 0)
        return result

    def test_empty_file(self):
        self._reject(b"", "EMPTY_FILE")

    def test_truncated_header_mid_card(self):
        # Fewer bytes than a single card: the next 80-byte card is missing.
        blob = b"A" * 40
        self._reject(blob, "TRUNCATED_HEADER")

    def test_garbage_card_without_indicator(self):
        # A full 80-byte ASCII card that is not a value or commentary card.
        self._reject(b"A" * CARD_LEN, "INVALID_CARD")

    def test_non_ascii_header_card(self):
        h0 = bytearray(build_file([valid_primary()]))
        h0[10] = 0x01
        self._reject(bytes(h0), "NON_ASCII_CARD")

    def test_non_ascii_byte_offset_is_exact(self):
        h0 = bytearray(build_file([valid_primary()]))
        h0[3 * CARD_LEN + 37] = 0xFF
        result = self._reject(bytes(h0), "NON_ASCII_CARD")
        self.assertEqual(result.offset, 3 * CARD_LEN + 37)

    def test_bad_simple_value(self):
        cards = [
            bool_card("SIMPLE", False),
            int_card("BITPIX", 8), int_card("NAXIS", 0),
            string_card("DATASUM", "0"),
            string_card("CHECKSUM", "0" * 16), end_card(),
        ]
        blob = header_block(b"".join(cards))
        self._reject(blob, "INVALID_KEYWORD_VALUE")

    def test_boolean_value_must_be_at_column_30(self):
        cards = [
            raw_card("SIMPLE  = T"),
            int_card("BITPIX", 8), int_card("NAXIS", 0),
            end_card(),
        ]
        self._reject(header_block(b"".join(cards)), "INVALID_CARD_VALUE")

    def test_bad_bitpix(self):
        cards = [
            bool_card("SIMPLE", True),
            int_card("BITPIX", 24), int_card("NAXIS", 0),
            string_card("DATASUM", "0"),
            string_card("CHECKSUM", "0" * 16), end_card(),
        ]
        self._reject(header_block(b"".join(cards)), "INVALID_KEYWORD_VALUE")

    def test_wrong_mandatory_order(self):
        cards = [
            bool_card("SIMPLE", True),
            int_card("NAXIS", 0), int_card("BITPIX", 8),
            string_card("DATASUM", "0"),
            string_card("CHECKSUM", "0" * 16), end_card(),
        ]
        self._reject(header_block(b"".join(cards)), "UNEXPECTED_KEYWORD")

    def test_duplicate_keyword(self):
        cards = [
            bool_card("SIMPLE", True), int_card("BITPIX", 8),
            int_card("NAXIS", 0), int_card("NAXIS", 0),
            end_card(),
        ]
        self._reject(header_block(b"".join(cards)), "DUPLICATE_KEYWORD")

    def test_extension_with_pcount_nonzero(self):
        data = b"\x00" * 8
        payload = data_block(data)
        cards = [
            string_card("XTENSION", "IMAGE", field=8),
            int_card("BITPIX", 8), int_card("NAXIS", 1),
            int_card("NAXIS1", 8), int_card("PCOUNT", 2),
            int_card("GCOUNT", 1),
            string_card("DATASUM", str(datasum_of(payload))),
            string_card("CHECKSUM", "0" * 16), end_card(),
        ]
        h = header_block(b"".join(cards))
        cs = checksum_string(h, payload, datasum_of(payload))
        cards[-2] = string_card("CHECKSUM", cs)
        ext = header_block(b"".join(cards)) + payload
        blob = build_file([valid_primary()]) + ext
        self._reject(blob, "PCOUNT_CONFLICT", hdu=1)

    def test_table_extension_rejected(self):
        cards = [
            string_card("XTENSION", "BINTABLE", field=8),
            int_card("BITPIX", 8), int_card("NAXIS", 2),
            int_card("NAXIS1", 8), int_card("NAXIS2", 1),
            int_card("PCOUNT", 0), int_card("GCOUNT", 1),
            end_card(),
        ]
        blob = (build_file([valid_primary()])
                + header_block(b"".join(cards)) + data_block(b"\x00" * 8))
        self._reject(blob, "UNSUPPORTED_EXTENSION", hdu=1)

    def test_extension_without_extend_in_primary(self):
        h0 = valid_primary(extend=False)
        h1 = valid_image(bitpix=8, axes=[1], values=[1])
        self._reject(build_file([h0, h1]), "MISSING_EXTEND", hdu=1)

    def test_too_many_extensions(self):
        hdus = [valid_primary()]
        for i in range(16):
            hdus.append(valid_image(bitpix=8, axes=[1], values=[i]))
        result = audit(build_file(hdus))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "TOO_MANY_HDUS")
        self.assertEqual(result.hdu, 16)

    def test_trailing_bytes_after_last_hdu(self):
        good = build_file([valid_primary()])
        self._reject(good + b"GARBAGE", "TRAILING_BYTES", hdu=1)

    def test_trailing_bytes_offset_reported(self):
        good = build_file([valid_primary()])
        result = self._reject(good + b"\x00" * BLOCK_LEN + b"X",
                              "TRAILING_BYTES", hdu=1)
        # The empty primary HDU occupies one block; junk starts at BLOCK_LEN.
        self.assertEqual(result.offset, BLOCK_LEN)

    def test_truncated_data_block(self):
        h0 = valid_primary(bitpix=8, axes=[100], values=list(range(100)))
        header, payload = h0
        # Drop half of the padded data block.
        blob = header + payload[:BLOCK_LEN // 2]
        self._reject(blob, "TRUNCATED_DATA")

    def test_nonzero_data_padding(self):
        h0 = bytearray(build_file([valid_primary(
            bitpix=8, axes=[3], values=[1, 2, 3])]))
        # Logical data is 3 bytes; first padding byte is at BLOCK_LEN + 3.
        h0[BLOCK_LEN + 3] = 0x7F
        self._reject(bytes(h0), "NONZERO_PADDING")

    def test_nonzero_header_padding(self):
        h0 = bytearray(build_file([valid_primary()]))
        # END is the last logical card; first padding byte follows it.
        # Find END card.
        end_pos = h0.find(b"END" + b" " * 77)
        h0[end_pos + CARD_LEN] = ord("X")
        self._reject(bytes(h0), "INVALID_HEADER_PADDING")

    def test_second_simple_card_rejected(self):
        # A "primary HDU" glued where an extension is required.
        h0 = valid_primary()
        second_header, payload = build_hdu(
            primary=True, bitpix=8, axes=[1], data=b"\x01", extend=False)
        blob = build_file([h0]) + second_header + payload
        self._reject(blob, "DUPLICATE_PRIMARY", hdu=1)


class ChecksumFailureTests(unittest.TestCase):
    def _reject(self, blob, reason, hdu=None):
        result = audit(blob)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, reason, result.message)
        if hdu is not None:
            self.assertEqual(result.hdu, hdu)
        return result

    def test_missing_datasum(self):
        h0 = valid_primary(datasum=False)
        self._reject(build_file([h0]), "MISSING_DATASUM")

    def test_missing_checksum(self):
        h0 = valid_primary(checksum=False)
        self._reject(build_file([h0]), "MISSING_CHECKSUM")

    def test_corrupted_datasum(self):
        blob = bytearray(build_file([valid_primary(
            bitpix=8, axes=[4], values=[1, 2, 3, 4])]))
        pos = bytes(blob).find(b"DATASUM")
        # Increment the final digit inside the quoted value.
        q = bytes(blob).find(b"'", pos)
        blob[q + 1] = ord("9")
        result = self._reject(bytes(blob), "DATASUM_MISMATCH")
        self.assertEqual(result.hdu, 0)
        self.assertEqual(result.offset, pos - pos % CARD_LEN)

    def test_corrupted_checksum_flags_second_hdu(self):
        hdus = [valid_primary()]
        for i in range(3):
            hdus.append(valid_image(bitpix=8, axes=[2], values=[i, i + 1],
                                    extname=f"E{i}"))
        blob = bytearray(build_file(hdus))
        # Layout: primary occupies 1 block, each populated IMAGE 2 blocks;
        # HDU 2 header therefore starts at block index 3.
        hdu2_start = 3 * BLOCK_LEN
        pos = bytes(blob).find(b"CHECKSUM", hdu2_start)
        q = bytes(blob).find(b"'", pos)
        blob[q + 1] = ord("A")
        result = self._reject(bytes(blob), "CHECKSUM_MISMATCH")
        self.assertEqual(result.hdu, 2)
        self.assertEqual(result.offset, pos - pos % CARD_LEN)

    def test_data_byte_corrupts_datasum_and_checksum(self):
        # A flipped data byte breaks DATASUM first (reported before CHECKSUM).
        blob = bytearray(build_file([valid_primary(
            bitpix=16, axes=[2], values=[1, 2])]))
        blob[BLOCK_LEN] ^= 0xFF
        self._reject(bytes(blob), "DATASUM_MISMATCH")

    def test_header_comment_change_corrupts_checksum_only(self):
        blob = bytearray(build_file([valid_primary()]))
        # Change a comment character after a / (does not affect DATASUM).
        pos = bytes(blob).find(b"conforms to FITS standard")
        blob[pos] = ord("X")
        self._reject(bytes(blob), "CHECKSUM_MISMATCH")

    def test_malformed_checksum_string(self):
        # Build a header with a short CHECKSUM value; skip fixture signing.
        cards = [
            bool_card("SIMPLE", True), int_card("BITPIX", 8),
            int_card("NAXIS", 0), string_card("DATASUM", "0"),
            raw_card("CHECKSUM= 'SHORT'"), end_card(),
        ]
        self._reject(header_block(b"".join(cards)), "MALFORMED_CHECKSUM")

    def test_malformed_datasum_value(self):
        cards = [
            bool_card("SIMPLE", True), int_card("BITPIX", 8),
            int_card("NAXIS", 0),
            raw_card("DATASUM = 'abc'"),
            string_card("CHECKSUM", "0" * 16), end_card(),
        ]
        self._reject(header_block(b"".join(cards)), "MALFORMED_DATASUM")


class OffsetStabilityTests(unittest.TestCase):
    def test_first_failure_is_earliest_hdu(self):
        hdus = [valid_primary()]
        for i in range(4):
            hdus.append(valid_image(bitpix=8, axes=[1], values=[i]))
        blob = bytearray(build_file(hdus))
        # Corrupt HDU 1 data and HDU 3 header; HDU 1 must be reported.
        blob[BLOCK_LEN + BLOCK_LEN] ^= 0x01          # first data byte HDU 1
        # HDU 3 header starts after primary (1 block) + 3 image HDUs
        # (2 blocks each) = block 7.
        h3 = bytes(blob).find(b"CHECKSUM", 7 * BLOCK_LEN)
        blob[h3 + 12] ^= 0x01  # character inside the encoded checksum
        result = audit(bytes(blob))
        self.assertFalse(result.accepted)
        self.assertEqual(result.hdu, 1)
        self.assertEqual(result.reason, "DATASUM_MISMATCH")

    def test_offset_points_inside_file(self):
        blob = build_file([valid_primary()]) + b"!!"
        result = audit(blob)
        self.assertFalse(result.accepted)
        self.assertLess(result.offset, len(blob))


if __name__ == "__main__":
    unittest.main(verbosity=2)
