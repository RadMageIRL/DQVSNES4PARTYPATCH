#!/usr/bin/env python3
"""
build.py - Dragon Quest V (SNES) DeJap + 4-Party Members, one-command build.

You supply two files this repository cannot legally contain:

  1. The unmodified Japanese ROM, "Dragon Quest V - Tenkuu no Hanayome (Japan)"
     SHA-1 1c47ed62c561d7965fe5dc2a03f4c37feb4a46b5, CRC32 BC955F3B, 1572864 bytes
  2. DeJap's English translation patch, DQ5E.IPS (v2.01 Final, 23-FEB-2002)

This script does the rest, and in particular it handles the 512-byte copier
header for you. Mishandling that header is what broke the original release of
this hack, so the whole point of shipping this script is to take that step out
of your hands.

Usage:
    python build.py <base.sfc> <DQ5E.IPS> [-o output.sfc]
    python build.py --help

Stdlib only. No pip installs. Works on Windows, Linux and macOS.
"""

import argparse
import hashlib
import struct
import sys
import zlib
from pathlib import Path

HDR = 512
PATCH_NAME = "dq5-4party-dejap-fixed-v1.bps"
IPS_FALLBACK = "dq5-4party-dejap-fixed-v1.ips"

BASE_SHA1 = "1c47ed62c561d7965fe5dc2a03f4c37feb4a46b5"
BASE_SIZE = 1572864

# Expected CRC32 after each stage. Every one of these is asserted.
EXPECT = {
    "base":     0xBC955F3B,
    "headered": 0x8E637962,
    "dejap":    0xA2E6F36A,
    "stripped": 0x9400CB3C,
    "patched":  0x8FEDE6AC,
    "final":    0x8FEDE6AC,
}

# Stage-specific guidance, printed when a stage CRC does not match.
HINTS = {
    "headered": "Header insertion produced unexpected bytes. This should not happen "
                "with a verified base ROM; please open an issue.",
    "dejap":    "Your DeJap IPS may not be v2.01 Final (23-FEB-2002). Other releases "
                "and pre-patched 'translated' ROMs will not produce this CRC.",
    "stripped": "Header removal produced unexpected bytes; please open an issue.",
    "patched":  "The 4-party patch did not apply as expected. If you replaced the "
                "bundled patch file, restore the original.",
    "final":    "Checksum correction produced unexpected bytes; please open an issue.",
}


class BuildError(Exception):
    """A build step failed in a way the user needs to act on."""


# --------------------------------------------------------------------------
# hashing helpers
# --------------------------------------------------------------------------

def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# --------------------------------------------------------------------------
# copier header
# --------------------------------------------------------------------------

def has_header(data: bytes) -> bool:
    return len(data) % 32768 == HDR


def add_header(data: bytes) -> bytes:
    return b"\x00" * HDR + data


def strip_header(data: bytes) -> bytes:
    return data[HDR:]


# --------------------------------------------------------------------------
# IPS
# --------------------------------------------------------------------------

def apply_ips(rom: bytes, patch: bytes) -> bytes:
    if patch[:5] != b"PATCH":
        raise BuildError("That file is not an IPS patch (missing 'PATCH' magic).\n"
                         "Make sure you passed DQ5E.IPS as the second argument.")
    out = bytearray(rom)
    i = 5
    while True:
        if i + 3 > len(patch):
            raise BuildError("IPS patch is truncated (no EOF marker).")
        if patch[i:i + 3] == b"EOF":
            if len(patch) - (i + 3) == 3:               # truncation extension
                out = out[:int.from_bytes(patch[i + 3:i + 6], "big")]
            break
        off = int.from_bytes(patch[i:i + 3], "big"); i += 3
        size = int.from_bytes(patch[i:i + 2], "big"); i += 2
        if size == 0:                                   # RLE run
            run = int.from_bytes(patch[i:i + 2], "big"); i += 2
            chunk = patch[i:i + 1] * run; i += 1
        else:
            chunk = patch[i:i + size]; i += size
        end = off + len(chunk)
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[off:end] = chunk
    return bytes(out)


# --------------------------------------------------------------------------
# BPS
# --------------------------------------------------------------------------

class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.i = data, pos

    def num(self) -> int:
        data, shift = 0, 1
        while True:
            x = self.d[self.i]; self.i += 1
            data += (x & 0x7F) * shift
            if x & 0x80:
                return data
            shift <<= 7
            data += shift


def apply_bps(source: bytes, patch: bytes) -> bytes:
    if patch[:4] != b"BPS1":
        raise BuildError("Bundled patch is not a BPS file; the repository copy may be damaged.")
    src_crc, tgt_crc, patch_crc = struct.unpack("<III", patch[-12:])
    if crc32(patch[:-4]) != patch_crc:
        raise BuildError("Bundled BPS patch is corrupt (its own checksum does not match).\n"
                         "Re-download it from the repository.")
    if crc32(source) != src_crc:
        raise BuildError(
            "The 4-party patch refused this ROM.\n"
            f"  expected source CRC32 {src_crc:08X}\n"
            f"  actual   source CRC32 {crc32(source):08X}\n"
            "This means the DeJap stage did not produce the expected ROM.")

    r = _Reader(patch, 4)
    src_size, tgt_size, meta_size = r.num(), r.num(), r.num()
    r.i += meta_size
    if len(source) != src_size:
        raise BuildError(f"Source size mismatch: patch expects {src_size} bytes, got {len(source)}.")

    out = bytearray(tgt_size)
    out_off = src_rel = tgt_rel = 0
    end = len(patch) - 12
    while r.i < end:
        cmd = r.num()
        action, length = cmd & 3, (cmd >> 2) + 1
        if action == 0:                                  # SourceRead
            out[out_off:out_off + length] = source[out_off:out_off + length]
            out_off += length
        elif action == 1:                                # TargetRead
            out[out_off:out_off + length] = r.d[r.i:r.i + length]
            r.i += length
            out_off += length
        elif action == 2:                                # SourceCopy
            n = r.num()
            src_rel += (-1 if n & 1 else 1) * (n >> 1)
            for _ in range(length):
                out[out_off] = source[src_rel]
                out_off += 1
                src_rel += 1
        else:                                            # TargetCopy
            n = r.num()
            tgt_rel += (-1 if n & 1 else 1) * (n >> 1)
            for _ in range(length):
                out[out_off] = out[tgt_rel]
                out_off += 1
                tgt_rel += 1
    result = bytes(out)
    if crc32(result) != tgt_crc:
        raise BuildError(f"BPS output checksum mismatch: expected {tgt_crc:08X}, got {crc32(result):08X}.")
    return result


# --------------------------------------------------------------------------
# SNES internal checksum
# --------------------------------------------------------------------------

def _mirror_sum(rest: bytes, target_size: int) -> int:
    """Sum `rest` as though mirrored to fill `target_size` bytes."""
    n = len(rest)
    pow2 = 1 << (n.bit_length() - 1)
    if pow2 == n:
        return sum(rest) * (target_size // n)
    head = sum(rest[:pow2])
    tail = _mirror_sum(rest[pow2:], pow2)
    return (head + tail) * (target_size // (pow2 * 2))


def compute_checksum(data: bytes) -> int:
    """Stored checksum for an image of any size.

    Non-power-of-two images split into the largest power-of-two prefix plus a
    remainder; the remainder is summed as if mirrored up to the prefix size.
    """
    n = len(data)
    pow2 = 1 << (n.bit_length() - 1)
    if pow2 == n:
        return sum(data) & 0xFFFF
    return (sum(data[:pow2]) + _mirror_sum(data[pow2:], pow2)) & 0xFFFF


def _score_header(data: bytes, base: int) -> int:
    if base + 0x40 > len(data):
        return -999
    h = data[base:base + 0x40]
    comp, chk = struct.unpack("<HH", h[0x1C:0x20])
    reset = struct.unpack("<H", data[base + 0x3C:base + 0x3E])[0]
    score = sum(1 for c in h[0x00:0x15] if 0x20 <= c <= 0x7E)
    if (comp ^ chk) == 0xFFFF:
        score += 12
    if 0x08 <= h[0x17] <= 0x0D:
        score += 6
    if (h[0x15] & 1) == (0 if base == 0x7FC0 else 1):
        score += 8
    if reset >= 0x8000:
        score += 6
    return score


def detect_header_base(data: bytes) -> int:
    """Return 0x7FC0 for LoROM or 0xFFC0 for HiROM, whichever scores higher."""
    return 0x7FC0 if _score_header(data, 0x7FC0) >= _score_header(data, 0xFFC0) else 0xFFC0


def fix_checksum(data: bytes):
    """Return (data, base, old_chk, new_chk); rewrites only if needed."""
    base = detect_header_base(data)
    _, old = struct.unpack("<HH", data[base + 0x1C:base + 0x20])
    buf = bytearray(data)
    struct.pack_into("<HH", buf, base + 0x1C, 0xFFFF, 0x0000)
    chk = compute_checksum(bytes(buf))
    struct.pack_into("<HH", buf, base + 0x1C, chk ^ 0xFFFF, chk)
    return bytes(buf), base, old, chk


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def read_file(path: Path, what: str) -> bytes:
    if not path.exists():
        raise BuildError(f"{what} not found: {path}")
    if path.is_dir():
        raise BuildError(f"{what} is a directory, not a file: {path}")
    return path.read_bytes()


def check_stage(rows, name, data, expect_key=None):
    key = expect_key or name
    want = EXPECT[key]
    got = crc32(data)
    rows.append((name, len(data), got, want, got == want))
    if got != want:
        raise BuildError(
            f"Stage '{name}' CRC32 mismatch: got {got:08X}, expected {want:08X}\n"
            f"  {HINTS.get(key, '')}")


def print_table(rows):
    print()
    print(f"  {'stage':<22}{'size':>10}  {'crc32':>8}  {'expected':>8}   ok")
    print("  " + "-" * 62)
    for name, size, got, want, ok in rows:
        print(f"  {name:<22}{size:>10}  {got:08X}  {want:08X}   {'yes' if ok else 'NO'}")
    print()


def build(base_path: Path, ips_path: Path, out_path: Path, patch_dir: Path,
          use_ips: bool = False) -> Path:
    rows = []

    base = read_file(base_path, "Base ROM")
    if has_header(base):
        print("  note: base ROM has a 512-byte copier header; removing it first.")
        base = strip_header(base)
    if len(base) != BASE_SIZE:
        raise BuildError(
            f"Base ROM is {len(base)} bytes; expected {BASE_SIZE}.\n"
            "  This does not look like the Japanese Dragon Quest V ROM.")
    got_sha = sha1(base)
    if got_sha != BASE_SHA1:
        raise BuildError(
            "Base ROM SHA-1 does not match the required ROM.\n"
            f"  expected {BASE_SHA1}\n"
            f"  actual   {got_sha}\n"
            "  You need the unmodified Japanese ROM, not a translated or pre-patched one.")
    check_stage(rows, "base", base)

    dejap_ips = read_file(ips_path, "DeJap IPS patch")

    headered = add_header(base)
    check_stage(rows, "headered", headered)

    dejap = apply_ips(headered, dejap_ips)
    check_stage(rows, "dejap", dejap)

    if not has_header(dejap):
        raise BuildError("DeJap stage lost its header unexpectedly; please open an issue.")
    stripped = strip_header(dejap)
    check_stage(rows, "stripped", stripped)

    name = IPS_FALLBACK if use_ips else PATCH_NAME
    patch_path = patch_dir / name
    if not patch_path.exists():
        raise BuildError(
            f"Bundled patch not found: {patch_path}\n"
            "  Keep build.py in the same directory as the .bps/.ips patch files.")
    patch = patch_path.read_bytes()
    patched = apply_ips(stripped, patch) if use_ips else apply_bps(stripped, patch)
    check_stage(rows, "patched", patched)

    final, base_off, old_chk, new_chk = fix_checksum(patched)
    check_stage(rows, "final", final)

    print_table(rows)
    mapping = "LoROM" if base_off == 0x7FC0 else "HiROM"
    if old_chk == new_chk:
        print(f"  internal checksum already correct ({new_chk:04X}, {mapping}) - no change needed")
    else:
        print(f"  internal checksum corrected {old_chk:04X} -> {new_chk:04X} ({mapping})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(final)
    print(f"\n  wrote {out_path}")
    print(f"    size  {len(final)} bytes")
    print(f"    crc32 {crc32(final):08X}")
    print(f"    sha1  {sha1(final)}")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Build Dragon Quest V (SNES) with the DeJap English translation and the\n"
            "corrected 4-Party Members hack."),
        epilog=(
            "WHAT YOU MUST SUPPLY\n"
            "  base.sfc   The unmodified Japanese ROM:\n"
            "             'Dragon Quest V - Tenkuu no Hanayome (Japan)'\n"
            "             1572864 bytes, CRC32 BC955F3B\n"
            "             SHA-1 1c47ed62c561d7965fe5dc2a03f4c37feb4a46b5\n"
            "             A 512-byte copier header is fine; it is removed automatically.\n"
            "  DQ5E.IPS   DeJap's translation patch, v2.01 Final (23-FEB-2002)\n"
            "\n"
            "WHY NO ROMS ARE INCLUDED\n"
            "  Distributing copyrighted ROM data is not legal, so this repository ships\n"
            "  patches only. You must provide your own legally obtained copy.\n"
            "\n"
            "EXAMPLE\n"
            "  python build.py \"Dragon Quest V (Japan).sfc\" DQ5E.IPS -o DQ5-4party.sfc\n"))
    parser.add_argument("base", type=Path, help="path to the Japanese base ROM (.sfc/.smc)")
    parser.add_argument("dejap_ips", type=Path, help="path to DeJap's DQ5E.IPS (v2.01)")
    parser.add_argument("-o", "--output", type=Path, default=Path("DQ5-4party-final.sfc"),
                        help="output ROM path (default: DQ5-4party-final.sfc)")
    parser.add_argument("--use-ips", action="store_true",
                        help="apply the bundled IPS instead of the BPS "
                             "(BPS is preferred: it validates the source ROM)")
    args = parser.parse_args(argv)

    # Resolve the bundled patch relative to this script, never the working directory.
    patch_dir = Path(__file__).resolve().parent

    print("Dragon Quest V - DeJap + 4 Party Members builder")
    try:
        build(args.base.expanduser(), args.dejap_ips.expanduser(),
              args.output.expanduser(), patch_dir, use_ips=args.use_ips)
    except BuildError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1
    except (OSError, IndexError, struct.error) as exc:
        print(f"\nERROR: unexpected failure while building: {exc}\n", file=sys.stderr)
        return 1
    print("\n  Done. Enjoy your four-member party.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
