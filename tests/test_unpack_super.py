"""Small end-to-end fixtures; no real firmware or root access is needed."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

SCRIPT = Path(__file__).resolve().parents[1] / "unpack_super.py"
BLOCK = 4096


def make_metadata(parts):
    partition_table, extent_table = bytearray(), bytearray()
    first = 0
    for name, extents in parts:
        partition_table += struct.pack("<36s4I", name.encode(), 1, first, len(extents), 0)
        for sector, blocks in extents:
            extent_table += struct.pack("<QIQI", blocks*8, 1 if sector is None else 0, sector or 0, 0)
        first += len(extents)
    groups = struct.pack("<36sIQ", b"default", 0, 0)
    device = struct.pack("<QIIQ36sI", 64, BLOCK, 0, 65536, b"super", 0)
    tables = [partition_table, extent_table, groups, device]
    data = b"".join(tables)
    h = bytearray(256)
    struct.pack_into("<IHHI", h, 0, 0x414C5030, 10, 2, 256)
    struct.pack_into("<I", h, 44, len(data))
    h[48:80] = hashlib.sha256(data).digest()
    offset = 0
    for i, (t, stride) in enumerate(zip(tables, (52, 24, 48, 64))):
        struct.pack_into("<III", h, 80+i*12, offset, len(t)//stride, stride)
        offset += len(t)
    h[12:44] = hashlib.sha256(h).digest()
    return h + data


def fixture(old_slot=False, ambiguous=False, bad_name=False):
    raw = bytearray(65536)
    g = bytearray(52)
    struct.pack_into("<II", g, 0, 0x616C4467, 52)
    struct.pack_into("<III", g, 40, BLOCK, 2, BLOCK)
    g[8:40] = hashlib.sha256(g).digest()
    raw[4096:4096+52] = g
    raw[8192:8192+52] = g
    system = bytearray(bytes(range(256))*16)
    struct.pack_into("<I", system, 1024, 0xE0F5E1E2)
    system[1024+12] = 12
    struct.pack_into("<I", system, 1024+36, 3)
    end = b"WXYZ"*1024
    vendor = b"ABCD"*1024
    raw[32768:36864] = system
    raw[40960:45056] = end
    raw[45056:49152] = vendor
    name = "../escape" if bad_name else "system_b"
    parts = [(name, [(64, 1), (None, 1), (80, 1)]), ("vendor_b", [(88, 1)]), ("empty_b", [])]
    other = parts
    if old_slot:
        other = [("system_a", [(64, 1)]), ("vendor_a", [(88, 1)])]
    if ambiguous:
        other = [("system_a", [(64, 1), (None, 1), (80, 1)]), ("vendor_a", [(88, 1)])]
    for slot, entries in enumerate((other, parts)):
        m = make_metadata(entries)
        for backup in (False, True):
            start = 12288 + (slot + (2 if backup else 0))*BLOCK
            raw[start:start+len(m)] = m
    return raw, bytes(system)+bytes(BLOCK)+end, vendor


def sparse(raw):
    chunks = []
    # Independently serialize each block to exercise all sparse chunk types.
    for pos in range(0, len(raw), BLOCK):
        data = bytes(raw[pos:pos+BLOCK])
        if data == bytes(BLOCK):
            chunks.append(struct.pack("<HHII", 0xCAC3, 0, 1, 12))
        elif data == data[:4]*(BLOCK//4):
            chunks.append(struct.pack("<HHII", 0xCAC2, 0, 1, 16)+data[:4])
        else:
            chunks.append(struct.pack("<HHII", 0xCAC1, 0, 1, 12+BLOCK)+data)
    crc = zlib.crc32(raw)
    chunks.append(struct.pack("<HHII", 0xCAC4, 0, 0, 16)+struct.pack("<I", crc))
    return struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, BLOCK, len(raw)//BLOCK, len(chunks), crc)+b"".join(chunks)


def replace_slot(raw, slot, parts, copies=(False, True)):
    data = make_metadata(parts)
    for backup in copies:
        start = 12288 + (slot + (2 if backup else 0))*BLOCK
        raw[start:start+BLOCK] = bytes(BLOCK)
        raw[start:start+len(data)] = data


def ab_fixture(parts):
    raw, _, vendor = fixture()
    raw[49152:53248] = b"B123"*1024
    for slot in (0, 1):
        replace_slot(raw, slot, parts)
    return raw, vendor, b"B123"*1024


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="unpack-super-unit-")
        self.root = Path(self.tmp.name)
        self.output = self.root / "super_extracted"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, raw, *extra):
        (self.root / "super.img").write_bytes(raw)
        return subprocess.run([sys.executable, str(SCRIPT), "-s", "super.img", *extra],
                              cwd=self.root, capture_output=True, text=True)

    def assert_images(self, system, vendor):
        self.assertEqual((self.output/"system_b.img").read_bytes(), system)
        self.assertEqual((self.output/"vendor_b.img").read_bytes(), vendor)
        self.assertFalse(any(p.is_dir() for p in self.output.iterdir()))
        report = json.loads((self.output/"extraction_report.json").read_text())
        self.assertFalse(report["filesystem_contents_extracted"])
        for p in report["extracted_partitions"]:
            self.assertEqual(hashlib.sha256((self.output/p["filename"]).read_bytes()).hexdigest(), p["sha256"])
        return report

    def test_raw_fragmented_and_zero_extents(self):
        raw, system, vendor = fixture()
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_images(system, vendor)
        self.assertFalse((self.output/"empty_b.img").exists())

    def test_sparse_raw_fill_hole_and_crc(self):
        raw, system, vendor = fixture()
        result = self.run_cli(sparse(raw))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_images(system, vendor)
        self.assertEqual(report["source_format"], "android-sparse")
        self.assertTrue(report["sparse_crc_verified"])

    def test_sparse_bad_crc_rejected(self):
        raw, _, _ = fixture()
        data = bytearray(sparse(raw))
        data[-1] ^= 1
        result = self.run_cli(data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CRC32 mismatch", result.stderr)
        self.assertFalse(self.output.exists())

    def test_stale_a_and_valid_b_are_both_extracted_with_warning(self):
        raw, system, vendor = fixture(old_slot=True)
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_images(system, vendor)
        self.assertEqual(report["selected_metadata_slots"], [0, 1])
        self.assertEqual(report["extracted_boot_slots"], ["a", "b"])
        self.assertEqual(report["status"], "passed_with_warnings")
        self.assertEqual((self.output/"system_a.img").read_bytes(), system[:BLOCK])
        self.assertEqual((self.output/"vendor_a.img").read_bytes(), vendor)
        self.assertEqual(len(report["extracted_partitions"]), 4)

    def test_explicit_metadata_slot_limits_output_and_keeps_warning(self):
        raw, _, _ = fixture(old_slot=True)
        result = self.run_cli(raw, "--slot", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.output.glob('*.img')}, {'system_a.img', 'vendor_a.img'})
        report = json.loads((self.output/'extraction_report.json').read_text())
        self.assertEqual(report['status'], 'passed_with_warnings')

    def test_a_and_b_in_different_metadata_slots_are_both_extracted(self):
        raw, system, vendor = fixture(ambiguous=True)
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_images(system, vendor)
        self.assertEqual((self.output/'system_a.img').read_bytes(), system)
        self.assertEqual((self.output/'vendor_a.img').read_bytes(), vendor)
        self.assertEqual(len(list(self.output.glob('*.img'))), 4)

    def test_backup_geometry_and_metadata_recovery(self):
        raw, system, vendor = fixture(old_slot=True)
        raw[4096+8] ^= 1
        raw[12288+BLOCK+12] ^= 1
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_images(system, vendor)
        system_part = next(p for p in report['extracted_partitions'] if p['name'] == 'system_b')
        self.assertEqual(system_part['metadata_sources'], [{'metadata_slot': 1, 'copy': 'backup'}])

    def test_all_metadata_checksums_bad(self):
        raw, _, _ = fixture()
        for i in range(4):
            raw[12288+i*BLOCK+12] ^= 1
        result = self.run_cli(raw)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_traversal_name_rejected(self):
        raw, _, _ = fixture(bad_name=True)
        result = self.run_cli(raw)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid partition name", result.stderr)
        self.assertFalse(self.output.exists())

    def test_truncated_raw_image_rejected(self):
        raw, _, _ = fixture()
        result = self.run_cli(raw[:44000])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_truncated_sparse_image_rejected(self):
        raw, _, _ = fixture()
        result = self.run_cli(sparse(raw)[:-1])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_zero_size_b_is_skipped_when_only_a_has_data(self):
        raw, a, _ = ab_fixture([('vendor_a', [(88, 1)]), ('vendor_b', [])])
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.output.glob('*.img')}, {'vendor_a.img'})
        self.assertEqual((self.output/'vendor_a.img').read_bytes(), a)

    def test_zero_size_a_is_skipped_when_only_b_has_data(self):
        raw, _, b = ab_fixture([('vendor_a', []), ('vendor_b', [(96, 1)])])
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.output.glob('*.img')}, {'vendor_b.img'})
        self.assertEqual((self.output/'vendor_b.img').read_bytes(), b)

    def test_a_and_b_in_same_metadata_are_both_extracted(self):
        raw, a, b = ab_fixture([('vendor_a', [(88, 1)]), ('vendor_b', [(96, 1)])])
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output/'vendor_a.img').read_bytes(), a)
        self.assertEqual((self.output/'vendor_b.img').read_bytes(), b)
        self.assertEqual(len(list(self.output.glob('*.img'))), 2)

    def test_allocated_all_zero_partition_is_skipped(self):
        raw, _, _ = ab_fixture([('vendor_a', [(88, 1)]), ('vendor_b', [(96, 1)])])
        raw[49152:53248] = bytes(BLOCK)
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.output.glob('*.img')}, {'vendor_a.img'})
        report = json.loads((self.output/'extraction_report.json').read_text())
        self.assertEqual([(p['name'],p['reason']) for p in report['skipped_empty']], [('vendor_b','all_zero')])

    def test_zero_extents_partition_is_skipped(self):
        raw, _, _ = ab_fixture([('vendor_a', [(88, 1)]), ('vendor_b', [(None, 1)])])
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.output.glob('*.img')}, {'vendor_a.img'})

    def test_sparse_hole_partition_is_skipped(self):
        raw, _, _ = ab_fixture([('vendor_a', [(88, 1)]), ('vendor_b', [(96, 1)])])
        raw[49152:53248] = bytes(BLOCK)
        result = self.run_cli(sparse(raw))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.output.glob('*.img')}, {'vendor_a.img'})

    def test_last_nonzero_byte_keeps_partition(self):
        raw, _, _ = ab_fixture([('vendor_b', [(96, 1)])])
        raw[49152:53248] = bytes(BLOCK-1)+b'\x01'
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output/'vendor_b.img').read_bytes(), bytes(BLOCK-1)+b'\x01')

    def test_duplicate_mapping_from_backup_and_other_slots_is_extracted_once(self):
        raw, system, vendor = fixture()
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_images(system, vendor)
        self.assertEqual(len(report['extracted_partitions']), 2)
        self.assertEqual(len(report['extracted_partitions'][0]['metadata_sources']), 4)

    def test_same_name_different_mappings_are_preserved(self):
        raw, a, b = ab_fixture([('vendor_b', [(88, 1)])])
        replace_slot(raw, 1, [('vendor_b', [(96, 1)])])
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output/'vendor_b.meta0-primary.img').read_bytes(), a)
        self.assertEqual((self.output/'vendor_b.meta1-primary.img').read_bytes(), b)
        self.assertEqual(len(list(self.output.glob('*.img'))), 2)

    def test_primary_backup_different_mappings_are_preserved(self):
        raw, a, b = ab_fixture([('vendor_b', [(88, 1)])])
        replace_slot(raw, 0, [('vendor_b', [(96, 1)])], copies=(True,))
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output/'vendor_b.meta0-primary.img').read_bytes(), a)
        self.assertEqual((self.output/'vendor_b.meta0-backup.img').read_bytes(), b)

    def test_all_empty_image_creates_report_only(self):
        raw, _, _ = ab_fixture([('vendor_a', []), ('vendor_b', [(None, 1)])])
        result = self.run_cli(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.output.glob('*.img')), [])
        report = json.loads((self.output/'extraction_report.json').read_text())
        self.assertEqual(len(report['skipped_empty']), 2)

    def test_existing_output_and_symlink_are_preserved(self):
        raw, _, _ = fixture()
        target = self.root/"keep"
        target.mkdir()
        (target/"sentinel").write_text("keep me")
        self.output.symlink_to(target, target_is_directory=True)
        result = self.run_cli(raw)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target/"sentinel").read_text(), "keep me")
        self.assertTrue(self.output.is_symlink())

    def test_list_writes_no_output(self):
        raw, _, _ = fixture()
        result = self.run_cli(raw, "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
