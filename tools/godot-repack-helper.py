#!/usr/bin/env python3
import os
import struct
import subprocess
import sys
import tempfile

# Godot 4 GST2 constants
GST2_MAGIC = b"GST2"
GST2_DATA_FORMAT_RAW = 0
GST2_DATA_FORMAT_PNG = 1
GST2_DATA_FORMAT_WEBP = 2
GST2_DATA_FORMAT_BASIS_UNIVERSAL = 3

# Godot 3 GDST constants
GDST_MAGIC = b"GDST"
GDST_FORMAT_PNG = 1 << 24
GDST_FORMAT_WEBP = 2 << 24
GDST_FORMAT_MASK = 0xFF000000

# Ogg CRC32 lookup table (polynomial 0x04C11DB7, MSB-first, no reflection)
_OGG_CRC_TABLE = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        if _r & 0x80000000:
            _r = ((_r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            _r = (_r << 1) & 0xFFFFFFFF
    _OGG_CRC_TABLE.append(_r)


def _ogg_crc32(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _OGG_CRC_TABLE[((crc >> 24) ^ b) & 0xFF]) & 0xFFFFFFFF
    return crc


def fix_webp_riff_bytes(data):
    """Repairs corrupted RIFF chunk size at offset 4 on WebP byte buffers."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        expected_size = len(data) - 8
        current_size = struct.unpack_from("<I", data, 4)[0]
        if current_size != expected_size:
            buf = bytearray(data)
            struct.pack_into("<I", buf, 4, expected_size)
            return bytes(buf)
    return data


def fix_webp_riff_file(path):
    """In-place RIFF size repair for standalone .webp files (e.g. Ren'Py)."""
    if not os.path.isfile(path):
        return False
    size = os.path.getsize(path)
    if size < 12:
        return False
    with open(path, "r+b") as f:
        header = f.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
            return False
        expected = size - 8
        current = struct.unpack_from("<I", header, 4)[0]
        if current != expected:
            f.seek(4)
            f.write(struct.pack("<I", expected))
    return True


def _reencode_image_bytes(raw_img, ext, convert_bin, quality, wmethod, tmp_dir):
    if ext == "webp":
        raw_img = fix_webp_riff_bytes(raw_img)

    in_tmp = os.path.join(tmp_dir, f"in.{ext}")
    out_tmp = os.path.join(tmp_dir, "out.webp")

    with open(in_tmp, "wb") as f:
        f.write(raw_img)

    cmd = [
        convert_bin,
        in_tmp,
        "-quality", str(quality),
        "-define", f"webp:method={wmethod}",
        "-define", "webp:alpha-quality=100",
        f"WEBP:{out_tmp}",
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0 or not os.path.exists(out_tmp):
        return None

    with open(out_tmp, "rb") as f:
        new_webp = f.read()

    if len(new_webp) == 0 or len(new_webp) >= len(raw_img):
        return None
    return new_webp


def repack_texture(path, convert_bin, quality, wmethod, tmp_dir):
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 24:
        return False

    magic = data[:4]

    # --- Godot 4 .ctex (GST2) ---
    if magic == GST2_MAGIC and len(data) >= 56:
        data_format, payload_len = struct.unpack_from("<II", data, 48)
        if data_format not in (GST2_DATA_FORMAT_PNG, GST2_DATA_FORMAT_WEBP):
            return False
        if 56 + payload_len > len(data):
            return False

        raw_img = data[56 : 56 + payload_len]
        ext = "webp" if data_format == GST2_DATA_FORMAT_WEBP else "png"
        new_webp = _reencode_image_bytes(raw_img, ext, convert_bin, quality, wmethod, tmp_dir)
        if new_webp is None:
            return False

        new_header = bytearray(data[:56])
        struct.pack_into("<II", new_header, 48, GST2_DATA_FORMAT_WEBP, len(new_webp))
        with open(path, "wb") as f:
            f.write(new_header)
            f.write(new_webp)
        return True

    # --- Godot 3 .stex (GDST) ---
    if magic == GDST_MAGIC and len(data) >= 24:
        fmt, payload_len = struct.unpack_from("<II", data, 16)
        fmt_type = fmt & GDST_FORMAT_MASK
        if fmt_type not in (GDST_FORMAT_PNG, GDST_FORMAT_WEBP):
            return False
        if 24 + payload_len > len(data):
            return False

        raw_img = data[24 : 24 + payload_len]
        ext = "webp" if fmt_type == GDST_FORMAT_WEBP else "png"
        new_webp = _reencode_image_bytes(raw_img, ext, convert_bin, quality, wmethod, tmp_dir)
        if new_webp is None:
            return False

        new_fmt = (fmt & ~GDST_FORMAT_MASK) | GDST_FORMAT_WEBP
        new_header = bytearray(data[:24])
        struct.pack_into("<II", new_header, 16, new_fmt, len(new_webp))
        with open(path, "wb") as f:
            f.write(new_header)
            f.write(new_webp)
        return True

    return False


def _build_ogg_page(packets, granule_pos, serial, seq_no, header_type):
    lacing = bytearray()
    payload = bytearray()
    for pkt in packets:
        rem = len(pkt)
        while rem >= 255:
            lacing.append(255)
            rem -= 255
        lacing.append(rem)
        payload.extend(pkt)

    if len(lacing) > 255:
        raise ValueError("Ogg page exceeds 255 lacing segments")

    hdr = bytearray(27)
    hdr[0:4] = b"OggS"
    hdr[4] = 0
    hdr[5] = header_type
    struct.pack_into("<q", hdr, 6, granule_pos)
    struct.pack_into("<I", hdr, 14, serial)
    struct.pack_into("<I", hdr, 18, seq_no)
    struct.pack_into("<I", hdr, 22, 0)
    hdr[26] = len(lacing)

    full_page = hdr + lacing + payload
    crc = _ogg_crc32(full_page)
    struct.pack_into("<I", full_page, 22, crc)
    return bytes(full_page)


def _mux_packets_to_ogg(pages, granules):
    if not pages or len(pages) != len(granules):
        return None
    out = bytearray()
    serial = 0x47444F54
    seq_no = 0
    total_pages = len(pages)

    for idx, (pkts, gran) in enumerate(zip(pages, granules)):
        htype = 0
        if idx == 0:
            htype |= 0x02  # BOS
        if idx == total_pages - 1:
            htype |= 0x04  # EOS

        # Split into multiple Ogg pages if lacing segments exceed 255
        chunk_pkts = []
        chunk_segs = 0
        for p_idx, pkt in enumerate(pkts):
            segs_needed = (len(pkt) // 255) + 1
            if chunk_pkts and (chunk_segs + segs_needed > 255):
                sub_htype = htype & ~0x04
                out.extend(_build_ogg_page(chunk_pkts, -1, serial, seq_no, sub_htype))
                seq_no += 1
                htype &= ~0x02
                chunk_pkts = []
                chunk_segs = 0
            chunk_pkts.append(pkt)
            chunk_segs += segs_needed

        if chunk_pkts:
            out.extend(_build_ogg_page(chunk_pkts, gran, serial, seq_no, htype))
            seq_no += 1

    return bytes(out)


def _extract_godot4_ogg_sequence(data):
    """Extracts (pages, granules) from Godot 4 RSRC OggPacketSequence binary data."""
    # Locate property pattern: TYPE_ARRAY (30), page_count, followed by nested TYPE_ARRAY (30)
    # and TYPE_PACKED_BYTE_ARRAY (31) packets, followed by TYPE_PACKED_INT64_ARRAY (33) granules.
    n = len(data)
    for pos in range(64, n - 32):
        val_type, page_count = struct.unpack_from("<II", data, pos)
        if val_type != 30 or page_count == 0 or page_count > 1000000:
            continue

        cur = pos + 8
        pages = []
        valid = True

        for _ in range(page_count):
            if cur + 8 > n:
                valid = False
                break
            inner_type, pkt_count = struct.unpack_from("<II", data, cur)
            if inner_type != 30 or pkt_count > 256:
                valid = False
                break
            cur += 8
            pkts = []
            for _ in range(pkt_count):
                if cur + 8 > n:
                    valid = False
                    break
                p_type, p_len = struct.unpack_from("<II", data, cur)
                if p_type != 31 or p_len > 10 * 1024 * 1024 or cur + 8 + p_len > n:
                    valid = False
                    break
                cur += 8
                pkts.append(data[cur : cur + p_len])
                cur += p_len
                rem = p_len % 4
                if rem:
                    cur += 4 - rem
            if not valid:
                break
            pages.append(pkts)

        if not valid or not pages:
            continue

        # Verify first packet is Vorbis identification header
        if not pages[0] or len(pages[0][0]) < 16 or pages[0][0][:7] != b"vorbis":
            continue

        # Next property should be granule_positions: [prop_idx, TYPE_PACKED_INT64_ARRAY (33), count]
        if cur + 12 > n:
            continue
        _, g_type, g_count = struct.unpack_from("<III", data, cur)
        if g_type != 33 or g_count != page_count or cur + 12 + (g_count * 8) > n:
            continue

        granules = list(struct.unpack_from(f"<{g_count}q", data, cur + 12))
        return pages, granules

    return None


def parse_ogg_packets(ogg_bytes):
    """Extracts (pages, granules, sample_rate) from raw Ogg Vorbis bytes."""
    pos = 0
    n = len(ogg_bytes)
    pages = []
    granules = []
    cur_packet = bytearray()
    sample_rate = 44100

    while pos + 27 <= n:
        if ogg_bytes[pos : pos + 4] != b"OggS":
            break
        granule = struct.unpack_from("<q", ogg_bytes, pos + 6)[0]
        num_segs = ogg_bytes[pos + 26]
        if pos + 27 + num_segs > n:
            break
        seg_table = ogg_bytes[pos + 27 : pos + 27 + num_segs]
        data_pos = pos + 27 + num_segs
        page_packets = []

        for seg_len in seg_table:
            if data_pos + seg_len > n:
                return None
            cur_packet.extend(ogg_bytes[data_pos : data_pos + seg_len])
            data_pos += seg_len
            if seg_len < 255:
                pkt = bytes(cur_packet)
                if len(pkt) >= 16 and pkt[:7] == b"vorbis":
                    sample_rate = struct.unpack_from("<I", pkt, 12)[0]
                page_packets.append(pkt)
                cur_packet = bytearray()

        if page_packets:
            pages.append(page_packets)
            granules.append(granule)
        pos = data_pos

    if not pages:
        return None
    return pages, granules, sample_rate


def build_godot4_ogg_rsrc(pages, granules, sample_rate, loop, loop_offset):
    """Builds a valid Godot 4 RSRC binary for AudioStreamOggVorbis."""
    TYPE_BOOL = 1
    TYPE_FLOAT = 3
    TYPE_ARRAY = 30
    TYPE_PACKED_BYTE_ARRAY = 31
    TYPE_PACKED_INT64_ARRAY = 33
    TYPE_OBJECT = 24

    def pack_str(s):
        b = s.encode("utf-8") + b" "
        return struct.pack("<I", len(b)) + b

    def pad4(buf):
        rem = len(buf) % 4
        if rem:
            buf.extend(b" " * (4 - rem))

    strings = ["packet_sequence", "loop", "loop_offset", "packet_data", "granule_positions", "sampling_rate"]
    str_tbl = bytearray(struct.pack("<I", len(strings)))
    for s in strings:
        str_tbl.extend(pack_str(s))

    seq_body = bytearray()
    seq_body.extend(struct.pack("<I", 3))

    seq_body.extend(struct.pack("<II", 3, TYPE_ARRAY))
    seq_body.extend(struct.pack("<I", len(pages)))
    for pg in pages:
        seq_body.extend(struct.pack("<II", TYPE_ARRAY, len(pg)))
        for pkt in pg:
            seq_body.extend(struct.pack("<II", TYPE_PACKED_BYTE_ARRAY, len(pkt)))
            seq_body.extend(pkt)
            pad4(seq_body)

    seq_body.extend(struct.pack("<III", 4, TYPE_PACKED_INT64_ARRAY, len(granules)))
    for g in granules:
        seq_body.extend(struct.pack("<q", g))

    seq_body.extend(struct.pack("<IId", 5, TYPE_FLOAT, float(sample_rate)))

    main_body = bytearray()
    prop_count = 1 + (1 if loop else 0) + (1 if loop_offset > 0.0 else 0)
    main_body.extend(struct.pack("<I", prop_count))

    main_body.extend(struct.pack("<IIII", 0, TYPE_OBJECT, 2, 1))
    if loop:
        main_body.extend(struct.pack("<III", 1, TYPE_BOOL, 1))
    if loop_offset > 0.0:
        main_body.extend(struct.pack("<IId", 2, TYPE_FLOAT, float(loop_offset)))

    out = bytearray()
    out.extend(b"RSRC")
    out.extend(struct.pack("<III", 0, 0, 2))
    out.extend(struct.pack("<III", 4, 2, 0))
    out.extend(pack_str("AudioStreamOggVorbis"))
    out.extend(struct.pack("<Q", 0))
    out.extend(struct.pack("<I", 0))
    out.extend(b" " * 16)
    out.extend(b" " * (11 * 4))

    out.extend(str_tbl)
    out.extend(struct.pack("<I", 0))

    out.extend(struct.pack("<I", 2))
    out.extend(pack_str("local://0"))
    off1_pos = len(out)
    out.extend(struct.pack("<Q", 0))
    out.extend(pack_str("local://1"))
    off2_pos = len(out)
    out.extend(struct.pack("<Q", 0))

    off1 = len(out)
    out.extend(pack_str("OggPacketSequence"))
    out.extend(seq_body)

    off2 = len(out)
    out.extend(pack_str("AudioStreamOggVorbis"))
    out.extend(main_body)

    out.extend(b"RSRC")
    struct.pack_into("<Q", out, off1_pos, off1)
    struct.pack_into("<Q", out, off2_pos, off2)
    return bytes(out)


def _extract_loop_metadata(data):
    loop = False
    loop_offset = 0.0
    loop_idx = data.find(b"   loop")
    if loop_idx >= 0 and b"      " in data[loop_idx:]:
        loop = True
    off_idx = data.find(b"   loop_offset")
    if off_idx >= 0:
        # Scan nearby for TYPE_FLOAT (3) followed by f64
        scan_start = max(64, len(data) - 256)
        for p in range(scan_start, len(data) - 12):
            vtype = struct.unpack_from("<I", data, p)[0]
            if vtype == 3:
                val = struct.unpack_from("<d", data, p + 4)[0]
                if 0.0 < val < 86400.0:
                    loop_offset = val
                    break
    return loop, loop_offset


def repack_audio(path, ffmpeg_bin, tmp_dir):
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RSRC":
        return False

    is_godot4 = b"AudioStreamOggVorbis" in data[:512]
    in_ogg = os.path.join(tmp_dir, "in.ogg")
    out_ogg = os.path.join(tmp_dir, "out.ogg")

    if not is_godot4:
        ogg_start = data.find(b"OggS")
        if ogg_start < 0:
            return False

        rsrc_end = len(data) - 4 if data[-4:] == b"RSRC" else len(data)
        raw_ogg = data[ogg_start:rsrc_end]
        if not parse_ogg_packets(raw_ogg):
            return False

        with open(in_ogg, "wb") as f:
            f.write(raw_ogg)

        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-i", in_ogg, "-c:a", "libvorbis", "-qscale:a", "3", "-f", "ogg", out_ogg
        ]
        if subprocess.run(cmd).returncode != 0 or not os.path.exists(out_ogg):
            return False

        with open(out_ogg, "rb") as f:
            new_ogg = f.read()

        if len(new_ogg) == 0 or len(new_ogg) >= len(raw_ogg):
            return False

        prefix = bytearray(data[:ogg_start])
        if len(prefix) >= 4:
            struct.pack_into("<I", prefix, len(prefix) - 4, len(new_ogg))
        pad_len = (4 - (len(new_ogg) % 4)) % 4
        with open(path, "wb") as f:
            f.write(prefix)
            f.write(new_ogg)
            f.write(b" " * pad_len)
            f.write(b"RSRC")
        return True

    # Godot 4 (.oggvorbisstr)
    seq = _extract_godot4_ogg_sequence(data)
    if not seq:
        return False

    pages, granules = seq
    muxed_ogg = _mux_packets_to_ogg(pages, granules)
    if not muxed_ogg:
        return False

    with open(in_ogg, "wb") as f:
        f.write(muxed_ogg)

    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_ogg, "-c:a", "libvorbis", "-qscale:a", "3", "-f", "ogg", out_ogg
    ]
    if subprocess.run(cmd).returncode != 0 or not os.path.exists(out_ogg):
        return False

    with open(out_ogg, "rb") as f:
        new_ogg = f.read()

    parsed = parse_ogg_packets(new_ogg)
    if not parsed:
        return False

    new_pages, new_granules, sample_rate = parsed
    loop, loop_offset = _extract_loop_metadata(data)
    new_rsrc = build_godot4_ogg_rsrc(new_pages, new_granules, sample_rate, loop, loop_offset)

    if len(new_rsrc) == 0 or len(new_rsrc) >= len(data):
        return False

    with open(path, "wb") as f:
        f.write(new_rsrc)
    return True


def main():
    if len(sys.argv) < 3:
        sys.exit(1)

    kind = sys.argv[1]
    target = sys.argv[2]

    if kind == "webp-fix":
        ok = fix_webp_riff_file(target)
        sys.exit(0 if ok else 2)

    if len(sys.argv) < 4:
        sys.exit(1)

    tool_bin = sys.argv[3]
    quality = int(sys.argv[4]) if len(sys.argv) > 4 else 80
    wmethod = int(sys.argv[5]) if len(sys.argv) > 5 else 5

    with tempfile.TemporaryDirectory() as tmp_dir:
        if kind == "tex":
            ok = repack_texture(target, tool_bin, quality, wmethod, tmp_dir)
        elif kind == "aud":
            ok = repack_audio(target, tool_bin, tmp_dir)
        else:
            ok = False

    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
