#!/usr/bin/env python3
"""Extract logical partition images from a single Android super image.

Extract all populated A/B partitions across all readable metadata slots.
Python 3.8+, standard library only. Never mounts or unpacks a filesystem.
Formats: AOSP liblp metadata v10.0-v10.2 and Android sparse image v1.
"""

import argparse
import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
import zlib

VERSION = "1.1.0"
BUFFER = 4 * 1024 * 1024
ZERO = bytes(BUFFER)


class ImageError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ImageError(message)


def sha(data):
    return hashlib.sha256(data).digest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for data in iter(lambda: stream.read(BUFFER), b""):
            digest.update(data)
    return digest.hexdigest()


class Image:
    """Random access to raw or sparse data without a temporary raw image."""

    def __init__(self, stream):
        self.stream = stream
        self.original_stat = os.fstat(stream.fileno())
        require(stat.S_ISREG(self.original_stat.st_mode), "Input must be a regular image file.")
        self.physical_size = self.original_stat.st_size
        self.size = self.physical_size
        self.format = "raw"
        self.chunks = []
        self.starts = []
        self.sparse_crc_verified = False
        if self.physical_size >= 4 and self.physical(0, 4) == b"\x3a\xff\x26\xed":
            self._sparse_index()

    def physical(self, offset, length):
        require(0 <= offset and 0 <= length and offset + length <= self.physical_size,
                "Image is truncated or contains an out-of-range offset.")
        self.stream.seek(offset)
        data = self.stream.read(length)
        require(len(data) == length, "Unexpected end of input image.")
        return data

    def _sparse_index(self):
        header = struct.unpack("<IHHHHIIII", self.physical(0, 28))
        _, major, _, hs, cs, block, blocks, count, image_crc = header
        require(major == 1 and hs >= 28 and cs >= 12, "Unsupported Android sparse header.")
        require(block > 0 and block % 4 == 0, "Invalid sparse block size.")
        require(hs <= self.physical_size and count <= (self.physical_size - hs) // cs,
                "Invalid sparse chunk count.")
        self.format = "android-sparse"
        self.size = block * blocks
        pos, logical = hs, 0
        checkpoints = []
        for _ in range(count):
            kind, _, num_blocks, total = struct.unpack("<HHII", self.physical(pos, 12))
            length = num_blocks * block
            payload = pos + cs
            require(total >= cs and pos + total <= self.physical_size, "Truncated sparse chunk.")
            if kind == 0xCAC1:
                require(total == cs + length, "Invalid RAW chunk length.")
                value = payload
            elif kind == 0xCAC2:
                require(total == cs + 4, "Invalid FILL chunk length.")
                value = self.physical(payload, 4)
            elif kind == 0xCAC3:
                require(total == cs, "Invalid DONT_CARE chunk length.")
                value = b"\0\0\0\0"
            elif kind == 0xCAC4:
                require(total == cs + 4 and length == 0, "Invalid CRC32 chunk.")
                checkpoints.append((logical, struct.unpack("<I", self.physical(payload, 4))[0]))
                pos += total
                continue
            else:
                raise ImageError("Unknown sparse chunk type: 0x%04x" % kind)
            if length:
                self.starts.append(logical)
                self.chunks.append((logical, length, kind, value))
            logical += length
            require(logical <= self.size, "Sparse chunks exceed the advertised image size.")
            pos += total
        require(logical == self.size and pos == self.physical_size, "Sparse image length mismatch.")
        if image_crc:
            checkpoints.append((self.size, image_crc))
        if checkpoints:
            print("Checking Android sparse CRC32...", flush=True)
            crc, cursor = 0, 0
            for end, expected in checkpoints:
                while cursor < end:
                    data = self.read(cursor, min(BUFFER, end - cursor))
                    crc = zlib.crc32(data, crc)
                    cursor += len(data)
                require(crc == expected, "Android sparse CRC32 mismatch.")
            self.sparse_crc_verified = True

    def read(self, offset, length):
        require(0 <= offset and 0 <= length and offset + length <= self.size,
                "Read outside the logical image.")
        if self.format == "raw":
            return self.physical(offset, length)
        result = bytearray()
        while length:
            index = bisect.bisect_right(self.starts, offset) - 1
            require(index >= 0, "Missing sparse data.")
            start, size, kind, value = self.chunks[index]
            within = offset - start
            take = min(length, size - within)
            require(take > 0, "Gap in sparse chunks.")
            if kind == 0xCAC1:
                result.extend(self.physical(value + within, take))
            else:
                shift = within % 4
                result.extend((value * ((shift + take + 3) // 4))[shift:shift + take])
            offset += take
            length -= take
        return bytes(result)

    def unchanged(self):
        current = os.fstat(self.stream.fileno())
        require((current.st_size, current.st_mtime_ns, current.st_ctime_ns) ==
                (self.original_stat.st_size, self.original_stat.st_mtime_ns,
                 self.original_stat.st_ctime_ns), "Input changed during extraction.")


def geometry(image):
    copies, errors = [], []
    for offset in (4096, 8192):
        try:
            g = image.read(offset, 4096)
            magic, size = struct.unpack_from("<II", g)
            require(magic == 0x616C4467 and 52 <= size <= 4096, "Invalid geometry header.")
            g = g[:size]
            require(sha(g[:8] + bytes(32) + g[40:]) == g[8:40], "Geometry checksum mismatch.")
            maximum, slots, block = struct.unpack_from("<III", g, 40)
            require(512 <= maximum <= 64*1024*1024 and maximum % 512 == 0,
                    "Unsupported metadata maximum size.")
            require(1 <= slots <= 32 and block >= 512 and block & (block - 1) == 0,
                    "Invalid slot count or logical block size.")
            require(12288 + 2 * slots * maximum <= image.size, "Truncated metadata area.")
            copies.append((maximum, slots, block))
        except ImageError as error:
            errors.append("geometry @%d: %s" % (offset, error))
    require(copies, "No valid primary or backup geometry: " + "; ".join(errors))
    require(len(set(copies)) == 1, "Primary and backup geometry disagree.")
    return copies[0], errors


def partition_read(image, part, offset, length):
    require(offset + length <= part["size_bytes"], "Read outside a partition.")
    data = bytearray()
    for extent in part["extents"]:
        size = extent["size_bytes"]
        if offset >= size:
            offset -= size
            continue
        take = min(length, size - offset)
        data.extend(image.read(extent["offset_bytes"] + offset, take)
                    if extent["type"] == "linear" else bytes(take))
        length -= take
        if not length:
            return bytes(data)
        offset = 0
    require(length == 0, "Missing partition extent.")
    return bytes(data)


def probe_filesystem(image, part):
    info = {"type": "unknown"}
    if part["size_bytes"] < 2048:
        return info
    sb = partition_read(image, part, 1024, 1024)
    if struct.unpack_from("<I", sb)[0] == 0xE0F5E1E2:
        shift = sb[12]
        if not 9 <= shift <= 16:
            return {"type": "erofs", "error": "Invalid EROFS block size"}
        size = struct.unpack_from("<I", sb, 36)[0] * (1 << shift)
        info = {"type": "erofs", "size_bytes": size}
    elif sb[56:58] == b"\x53\xef":
        shift = struct.unpack_from("<I", sb, 24)[0]
        if shift > 6:
            return {"type": "ext", "error": "Invalid ext block size"}
        blocks = struct.unpack_from("<I", sb, 4)[0]
        if struct.unpack_from("<I", sb, 96)[0] & 0x80:
            blocks |= struct.unpack_from("<I", sb, 336)[0] << 32
        info = {"type": "ext", "size_bytes": blocks * (1024 << shift)}
    if "size_bytes" in info:
        if info["size_bytes"] <= 0 or info["size_bytes"] > part["size_bytes"]:
            info["error"] = "Filesystem needs %d bytes, partition provides %d" % (
                info["size_bytes"], part["size_bytes"])
    return info


def metadata(image, geo, slot, backup):
    maximum, slots, block = geo
    base = 12288 + (slot + (slots if backup else 0)) * maximum
    prefix = image.read(base, 128)
    magic, major, minor, hs = struct.unpack_from("<IHHI", prefix)
    require(magic == 0x414C5030 and major == 10 and minor <= 2,
            "Unsupported or missing liblp metadata (supports v10.0-v10.2).")
    require(hs == (256 if minor >= 2 else 128), "Unexpected metadata header size.")
    h = image.read(base, hs)
    require(sha(h[:12] + bytes(32) + h[44:]) == h[12:44], "Metadata header checksum mismatch.")
    table_size = struct.unpack_from("<I", h, 44)[0]
    require(hs + table_size <= maximum, "Metadata tables exceed their allocation.")
    table = image.read(base + hs, table_size)
    require(sha(table) == h[48:80], "Metadata table checksum mismatch.")
    descriptors = [struct.unpack_from("<III", h, 80 + i*12) for i in range(4)]
    for (offset, count, size), expected in zip(descriptors, (52, 24, 48, 64)):
        require(size == expected and offset + count*size <= table_size, "Invalid table descriptor.")
    do, dn, _ = descriptors[3]
    require(dn == 1, "Multiple physical super devices are not supported.")
    first_sector, _, _, device_size, _, device_flags = struct.unpack_from("<QIIQ36sI", table, do)
    require(device_flags & ~1 == 0, "Unsupported physical device flags.")
    require(first_sector*512 >= 12288 + slots*maximum*2, "Data overlaps metadata.")
    eo, en, es = descriptors[1]
    extents = []
    for i in range(en):
        sectors, kind, sector, device = struct.unpack_from("<QIQI", table, eo + i*es)
        require(kind in (0, 1) and device == 0, "Unsupported extent type/device.")
        require(sectors*512 % block == 0, "Unaligned extent size.")
        if kind == 0:
            require(sector >= first_sector and (sector + sectors)*512 <= min(image.size, device_size),
                    "Partition extent is outside the source image/device.")
        else:
            require(sector == 0, "Invalid zero extent.")
        extents.append({"type": "linear" if kind == 0 else "zero",
                        "offset_bytes": sector*512, "size_bytes": sectors*512})
    po, pn, ps = descriptors[0]
    parts, names, spans = [], set(), []
    for i in range(pn):
        raw_name, attrs, first, count, group = struct.unpack_from("<36sIIII", table, po+i*ps)
        require(attrs & ~15 == 0, "Unsupported partition attributes.")
        require(first + count <= en and group < descriptors[2][1], "Invalid partition table index.")
        name_bytes = raw_name.split(b"\0", 1)[0]
        require(re.fullmatch(rb"[A-Za-z0-9_]+", name_bytes), "Invalid partition name.")
        name = name_bytes.decode("ascii")
        if attrs & 2:
            require(slot < 26, "Invalid slot suffix.")
            name += "_" + chr(ord("a") + slot)
        require(name not in names, "Duplicate partition name.")
        names.add(name)
        ex = extents[first:first+count]
        part = {"name": name, "attributes": attrs, "size_bytes": sum(e["size_bytes"] for e in ex),
                "extents": ex}
        part["filesystem"] = probe_filesystem(image, part)
        parts.append(part)
        for e in ex:
            if e["type"] == "linear" and e["size_bytes"]:
                spans.append((e["offset_bytes"], e["offset_bytes"] + e["size_bytes"]))
    spans.sort()
    require(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), "Overlapping partition extents.")
    issues = [p["name"] + ": " + p["filesystem"]["error"] for p in parts if "error" in p["filesystem"]]
    return {"slot": slot, "copy": "backup" if backup else "primary", "version": "%d.%d" % (major, minor),
            "tables_sha256": h[48:80].hex(), "partitions": parts, "issues": issues}


def collect_partitions(candidates, requested_slot):
    """Keep distinct mappings from every slot, including stale readable data.

    Metadata slot numbers and A/B partition suffixes are different concepts.
    One metadata slot can contain both A and B partitions.
    """
    pool = [c for c in candidates if requested_slot is None or c["slot"] == requested_slot]
    require(pool, "No valid metadata for the requested slot.")
    mappings, empty = {}, {}
    for candidate in pool:
        origin = {"metadata_slot": candidate["slot"], "copy": candidate["copy"]}
        for part in candidate["partitions"]:
            key = json.dumps([part["name"], part["size_bytes"], part["extents"]], sort_keys=True)
            bucket = mappings if part["size_bytes"] else empty
            if key not in bucket:
                bucket[key] = dict(part, metadata_sources=[])
            bucket[key]["metadata_sources"].append(origin)
    parts = list(mappings.values())
    groups = {}
    for part in parts:
        groups.setdefault(part["name"], []).append(part)
    for name, variants in groups.items():
        for part in variants:
            if len(variants) == 1:
                part["filename"] = name + ".img"
            else:
                origin = part["metadata_sources"][0]
                # Dots cannot appear in a partition name, preventing collision
                # with ordinary '<partition>.img' names.
                part["filename"] = "%s.meta%d-%s.img" % (name, origin["metadata_slot"], origin["copy"])
    require(len({p["filename"] for p in parts}) == len(parts), "Conflicting output filenames.")
    skipped = [dict(p, reason="zero_size") for p in empty.values()]
    return parts, skipped, sorted({c["slot"] for c in pool})


def extract(image, parts, skipped_empty, selected_slots, destination, context):
    require(not os.path.lexists(destination), "Output already exists; choose another directory with -o.")
    total = sum(p["size_bytes"] for p in parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    require(shutil.disk_usage(destination.parent).free >= total + 1024*1024,
            "Not enough free space for all output images (need %.2f GiB)." % (total/1024**3))
    staging = Path(tempfile.mkdtemp(prefix="." + destination.name + ".partial-", dir=str(destination.parent)))
    completed = False
    try:
        results = []
        for number, part in enumerate(parts, 1):
            if all(e["type"] == "zero" or not e["size_bytes"] for e in part["extents"]):
                skipped_empty.append(dict(part, reason="all_zero"))
                print("[%d/%d] SKIP %s: zero extents only" % (number, len(parts), part["filename"]), flush=True)
                continue
            path = staging / part["filename"]
            print("[%d/%d] %s  %.2f MiB" % (number, len(parts), path.name, part["size_bytes"]/1024**2), flush=True)
            expected = hashlib.sha256()
            done, last_progress, has_data = 0, time.monotonic(), False
            with path.open("xb") as output:
                for extent in part["extents"]:
                    offset, remaining = extent["offset_bytes"], extent["size_bytes"]
                    while remaining:
                        take = min(BUFFER, remaining)
                        data = image.read(offset, take) if extent["type"] == "linear" else ZERO[:take]
                        expected.update(data)
                        if data == ZERO[:take]:
                            output.seek(take, os.SEEK_CUR)
                        else:
                            output.write(data)
                            has_data = True
                        remaining -= take
                        offset += take
                        done += take
                        if time.monotonic() - last_progress >= 5:
                            print("  %.1f%%" % (100*done/part["size_bytes"]), flush=True)
                            last_progress = time.monotonic()
                output.truncate(part["size_bytes"])
            if not has_data:
                path.unlink()
                skipped_empty.append(dict(part, reason="all_zero"))
                print("  SKIP: all bytes are zero", flush=True)
                continue
            actual = file_sha256(path)
            require(path.stat().st_size == part["size_bytes"] and actual == expected.hexdigest(),
                    "Output size or SHA-256 mismatch: " + path.name)
            result = dict(part, sha256=actual, verification="passed")
            results.append(result)
            print("  SHA-256 OK: " + actual, flush=True)
            if "error" in part["filesystem"]:
                print("  Filesystem warning: " + part["filesystem"]["error"], flush=True)
        image.unchanged()
        filesystem_warnings = [{"filename": p["filename"], "warning": p["filesystem"]["error"]}
                               for p in results if "error" in p["filesystem"]]
        status = "passed_with_warnings" if filesystem_warnings or context["metadata_warnings"] else "passed"
        report = dict(context, status=status, selected_metadata_slots=selected_slots,
                      selection_mode="all_metadata_slots" if context["requested_metadata_slot"] is None else "explicit_metadata_slot",
                      output_directory=str(destination), extracted_partitions=results,
                      extracted_boot_slots=sorted({p["name"][-1] for p in results if p["name"].endswith(("_a", "_b"))}),
                      skipped_empty=skipped_empty, filesystem_warnings=filesystem_warnings,
                      filesystem_contents_extracted=False,
                      filesystem_integrity_checked=False,
                      verification="Output size and SHA-256 match source extents. Readable mappings with filesystem warnings are retained; unknown filesystems are not validated.")
        (staging / "extraction_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (staging / "SHA256SUMS.txt").write_text("".join(p["sha256"] + "  " + p["filename"] + "\n" for p in results), encoding="ascii")
        require(not os.path.lexists(destination), "Output appeared during extraction; refusing to replace it.")
        staging.rename(destination)
        completed = True
        label = "PASS WITH WARNINGS" if status == "passed_with_warnings" else "PASS"
        print("%s: %d images, %d bytes; %d empty mappings skipped. Output: %s" % (
            label, len(results), sum(p["size_bytes"] for p in results), len(skipped_empty), destination), flush=True)
        if filesystem_warnings:
            print("Some partition data is incomplete or inconsistent; see filesystem_warnings in extraction_report.json.")
        print("Only partition .img files were extracted; no filesystem was unpacked.")
    finally:
        if not completed:
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--super", required=True, dest="source", type=Path, help="Input super.img (raw or Android sparse)")
    parser.add_argument("-o", "--output", type=Path, help="New output directory (default: ./<input-stem>_extracted)")
    parser.add_argument("--slot", type=int, help="Limit to one metadata slot (default: ALL metadata slots, populated A/B partitions)")
    parser.add_argument("--list", action="store_true", help="Inspect all slots without writing output")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    try:
        require(args.slot is None or args.slot >= 0, "Slot must be nonnegative.")
        source = args.source.expanduser().resolve(strict=True)
        output = (args.output or Path(args.source.stem + "_extracted")).expanduser().absolute()
        with source.open("rb") as stream:
            image = Image(stream)
            geo, errors = geometry(image)
            candidates = []
            for slot in range(geo[1]):
                for backup in (False, True):
                    try:
                        candidates.append(metadata(image, geo, slot, backup))
                    except ImageError as error:
                        errors.append("slot %d %s: %s" % (slot, "backup" if backup else "primary", error))
            print("Input: %s (%s, logical size %d bytes)" % (source, image.format, image.size))
            for c in candidates:
                active = sum(p["size_bytes"] > 0 for p in c["partitions"])
                print("Slot %d %-7s: %d nonempty partitions%s" % (
                    c["slot"], c["copy"], active, "; " + "; ".join(c["issues"]) if c["issues"] else ""))
            for error in errors:
                print("Metadata warning: " + error, file=sys.stderr)
            require(candidates, "No readable logical partition metadata.")
            if args.list:
                for c in candidates:
                    print("\nSlot %d (%s):" % (c["slot"], c["copy"]))
                    for p in c["partitions"]:
                        print("  %-24s %12d bytes  %s" % (p["name"], p["size_bytes"], p["filesystem"]["type"]))
                image.unchanged()
                return 0
            parts, skipped_empty, selected_slots = collect_partitions(candidates, args.slot)
            print("Reading metadata slots %s: %d distinct allocated mappings, %d zero-size entries skipped." % (
                ", ".join(map(str, selected_slots)), len(parts), len(skipped_empty)), flush=True)
            context = {"tool_version": VERSION, "source": str(source), "source_format": image.format,
                       "source_file_bytes": image.physical_size, "source_logical_bytes": image.size,
                       "sparse_crc_verified": image.sparse_crc_verified,
                       "metadata_candidates": candidates, "metadata_warnings": errors,
                       "requested_metadata_slot": args.slot}
            extract(image, parts, skipped_empty, selected_slots, output, context)
        return 0
    except (ImageError, OSError, ValueError, struct.error) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled; incomplete staging output removed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
