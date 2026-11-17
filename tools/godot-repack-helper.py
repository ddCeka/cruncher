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

# Godot 4 RSRC variant types
TYPE_NIL = 0
TYPE_BOOL = 1
TYPE_INT = 2
TYPE_FLOAT = 3
TYPE_STRING = 4
TYPE_VECTOR2 = 5
TYPE_VECTOR2I = 6
TYPE_RECT2 = 7
TYPE_RECT2I = 8
TYPE_VECTOR3 = 9
TYPE_VECTOR3I = 10
TYPE_TRANSFORM2D = 11
TYPE_VECTOR4 = 12
TYPE_VECTOR4I = 13
TYPE_PLANE = 14
TYPE_QUATERNION = 15
TYPE_AABB = 16
TYPE_BASIS = 17
TYPE_TRANSFORM3D = 18
TYPE_PROJECTION = 19
TYPE_COLOR = 20
TYPE_STRING_NAME = 21
TYPE_NODE_PATH = 22
TYPE_RID = 23
TYPE_OBJECT = 24
TYPE_CALLABLE = 25
TYPE_SIGNAL = 26
TYPE_DICTIONARY = 27
TYPE_ARRAY = 30
TYPE_PACKED_BYTE_ARRAY = 31
TYPE_PACKED_INT32_ARRAY = 32
TYPE_PACKED_INT64_ARRAY = 33
TYPE_PACKED_FLOAT32_ARRAY = 34
TYPE_PACKED_FLOAT64_ARRAY = 35
TYPE_PACKED_STRING_ARRAY = 36
TYPE_PACKED_VECTOR2_ARRAY = 37
TYPE_PACKED_VECTOR3_ARRAY = 38
TYPE_PACKED_COLOR_ARRAY = 39


def _make_ogg_crc_table():
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            if r & 0x80000000:
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                r = (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table


OGG_CRC_TABLE = _make_ogg_crc_table()


def ogg_crc32(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ b]) & 0xFFFFFFFF
    return crc


def build_ogg_page(packets, granule_pos, serial_no, page_seq, header_type=0):
    """Frames one or more Vorbis packets into a valid OggS page with CRC32."""
    lacing = bytearray()
    payload = bytearray()
    for pkt in packets:
        rem = len(pkt)
        while rem >= 255:
            lacing.append(255)
            rem -= 255
        lacing.append(rem)
        payload.extend(pkt)

    hdr = bytearray(27 + len(lacing))
    hdr[0:4] = b"OggS"
    hdr[4] = 0  # stream_structure_version
    hdr[5] = header_type
    struct.pack_into("<q", hdr, 6, granule_pos)
    struct.pack_into("<I", hdr, 14, serial_no)
    struct.pack_into("<I", hdr, 18, page_seq)
    struct.pack_into("<I", hdr, 22, 0)  # CRC placeholder
    hdr[26] = len(lacing)
    hdr[27:] = lacing

    full_page = hdr + payload
    crc = ogg_crc32(full_page)
    struct.pack_into("<I", full_page, 22, crc)
    return bytes(full_page)


def reconstruct_ogg_from_packets(pages, granules):
    """Rebuilds a valid Ogg Vorbis stream from Godot 4 packet_data + granule_positions."""
    if not pages or len(pages) != len(granules):
        return None

    serial_no = 0x47444F54  # "GDOT"
    out = bytearray()
    page_seq = 0

    for idx, (pkts, gran) in enumerate(zip(pages, granules)):
        htype = 0
        if idx == 0:
            htype |= 0x02  # BOS (Beginning of Stream)
        if idx == len(pages) - 1:
            htype |= 0x04  # EOS (End of Stream)

        # Split into multiple Ogg pages if total lacing segments exceed 255
        chunk_pkts = []
        chunk_segs = 0
        for p in pkts:
            needed_segs = (len(p) // 255) + 1
            if chunk_pkts and (chunk_segs + needed_segs > 255):
                sub_htype = htype & ~0x04
                out.extend(build_ogg_page(chunk_pkts, -1, serial_no, page_seq, sub_htype))
                page_seq += 1
                htype &= ~0x02
                chunk_pkts = []
                chunk_segs = 0
            chunk_pkts.append(p)
            chunk_segs += needed_segs

        if chunk_pkts:
            out.extend(build_ogg_page(chunk_pkts, gran, serial_no, page_seq, htype))
            page_seq += 1

    return bytes(out)


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
                if len(pkt) >= 16 and pkt[:7] == b"\x01vorbis":
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


class RsrcReader:
    """Minimal Godot 4 binary RSRC reader for AudioStreamOggVorbis extraction."""

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.strings = []
        self.use_real64 = False

    def read_u32(self):
        val = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_i32(self):
        val = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_u64(self):
        val = struct.unpack_from("<Q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_i64(self):
        val = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_f32(self):
        val = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_f64(self):
        val = struct.unpack_from("<d", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_str(self):
        slen = self.read_u32()
        if slen == 0:
            return ""
        raw = self.data[self.pos : self.pos + slen]
        self.pos += slen
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("utf-8", errors="replace")

    def align4(self):
        rem = self.pos % 4
        if rem:
            self.pos += 4 - rem

    def read_variant(self):
        vtype = self.read_u32()
        if vtype == TYPE_NIL:
            return None
        if vtype == TYPE_BOOL:
            return self.read_u32() != 0
        if vtype == TYPE_INT:
            return self.read_i32()
        if vtype == TYPE_FLOAT:
            return self.read_f64() if self.use_real64 else self.read_f32()
        if vtype == TYPE_STRING:
            return self.read_str()
        if vtype == TYPE_STRING_NAME:
            return self.read_str()
        if vtype == TYPE_OBJECT:
            obj_type = self.read_u32()
            if obj_type == 0:  # EMPTY
                return None
            if obj_type == 1:  # EXTERNAL
                _res_type = self.read_str()
                _res_path = self.read_str()
                return None
            if obj_type == 2:  # INTERNAL
                return ("internal", self.read_u32())
            if obj_type == 3:  # SUBRESOURCE
                return ("subresource", self.read_u32())
            raise ValueError(f"Unsupported OBJECT subtype {obj_type}")
        if vtype == TYPE_DICTIONARY:
            dlen = self.read_u32() & 0x7FFFFFFF
            res = {}
            for _ in range(dlen):
                k = self.read_variant()
                v = self.read_variant()
                res[k] = v
            return res
        if vtype == TYPE_ARRAY:
            alen = self.read_u32() & 0x7FFFFFFF
            return [self.read_variant() for _ in range(alen)]
        if vtype == TYPE_PACKED_BYTE_ARRAY:
            blen = self.read_u32()
            raw = bytes(self.data[self.pos : self.pos + blen])
            self.pos += blen
            self.align4()
            return raw
        if vtype == TYPE_PACKED_INT32_ARRAY:
            alen = self.read_u32()
            vals = [self.read_i32() for _ in range(alen)]
            return vals
        if vtype == TYPE_PACKED_INT64_ARRAY:
            alen = self.read_u32()
            vals = [self.read_i64() for _ in range(alen)]
            return vals
        if vtype == TYPE_PACKED_FLOAT32_ARRAY:
            alen = self.read_u32()
            vals = [self.read_f32() for _ in range(alen)]
            return vals
        if vtype == TYPE_PACKED_FLOAT64_ARRAY:
            alen = self.read_u32()
            vals = [self.read_f64() for _ in range(alen)]
            return vals
        if vtype == TYPE_PACKED_STRING_ARRAY:
            alen = self.read_u32()
            return [self.read_str() for _ in range(alen)]
        raise ValueError(f"Unsupported RSRC variant type {vtype}")


def parse_godot4_ogg_rsrc(data):
    """Parses a Godot 4 .oggvorbisstr RSRC file into (pages, granules, sample_rate, loop, loop_offset)."""
    if len(data) < 64 or data[:4] != b"RSRC":
        return None

    r = RsrcReader(data)
    r.pos = 4
    big_endian = r.read_u32()
    use_real64 = r.read_u32()
    fmt_ver = r.read_u32()
    if big_endian != 0 or fmt_ver < 2:
        return None

    r.use_real64 = (use_real64 != 0)
    _ver_major = r.read_u32()
    _ver_minor = r.read_u32()
    _ver_patch = r.read_u32()
    root_type = r.read_str()
    if root_type != "AudioStreamOggVorbis":
        return None

    _import_md_offset = r.read_u64()
    flags = r.read_u32()
    # FORMAT_FLAG_REAL_T_IS_DOUBLE = 1 << 3
    if flags & (1 << 3):
        r.use_real64 = True
    # Skip UID (16 bytes) + 11 * u32 reserved (44 bytes)
    r.pos += 16 + 44

    str_count = r.read_u32()
    r.strings = [r.read_str() for _ in range(str_count)]

    ext_count = r.read_u32()
    for _ in range(ext_count):
        _ext_type = r.read_str()
        _ext_path = r.read_str()
        if flags & (1 << 2):  # FORMAT_FLAG_UIDS
            _ext_uid = r.read_u64()

    int_count = r.read_u32()
    int_offsets = []
    for _ in range(int_count):
        _int_path = r.read_str()
        int_offsets.append(r.read_u64())

    pages = None
    granules = None
    sample_rate = 44100
    loop = False
    loop_offset = 0.0

    for off in int_offsets:
        r.pos = off
        res_class = r.read_str()
        prop_count = r.read_u32()
        props = {}
        for _ in range(prop_count):
            name_idx = r.read_u32()
            prop_name = r.strings[name_idx] if name_idx < len(r.strings) else f"unk_{name_idx}"
            props[prop_name] = r.read_variant()

        if res_class == "OggPacketSequence":
            pages = props.get("packet_data")
            granules = props.get("granule_positions")
            if "sampling_rate" in props and props["sampling_rate"] is not None:
                sample_rate = int(props["sampling_rate"])
        elif res_class == "AudioStreamOggVorbis":
            loop = bool(props.get("loop", False))
            loop_offset = float(props.get("loop_offset", 0.0))

    if not pages or not granules:
        return None
    return pages, granules, sample_rate, loop, loop_offset


def build_godot4_ogg_rsrc(pages, granules, sample_rate, loop, loop_offset):
    """Builds a minimal valid Godot 4 RSRC binary for AudioStreamOggVorbis."""

    def pack_str(s):
        b = s.encode("utf-8") + b"\x00"
        return struct.pack("<I", len(b)) + b

    def pad4(buf):
        rem = len(buf) % 4
        if rem:
            buf.extend(b"\x00" * (4 - rem))

    strings = ["packet_sequence", "loop", "loop_offset", "packet_data", "granule_positions", "sampling_rate"]
    str_tbl = bytearray(struct.pack("<I", len(strings)))
    for s in strings:
        str_tbl.extend(pack_str(s))

    # Internal resource 0: OggPacketSequence
    seq_body = bytearray()
    seq_body.extend(struct.pack("<I", 3))

    # 1) packet_data: Array of Array of PackedByteArray
    seq_body.extend(struct.pack("<II", 3, TYPE_ARRAY))
    seq_body.extend(struct.pack("<I", len(pages)))
    for pg in pages:
        seq_body.extend(struct.pack("<II", TYPE_ARRAY, len(pg)))
        for pkt in pg:
            seq_body.extend(struct.pack("<II", TYPE_PACKED_BYTE_ARRAY, len(pkt)))
            seq_body.extend(pkt)
            pad4(seq_body)

    # 2) granule_positions: PackedInt64Array
    seq_body.extend(struct.pack("<III", 4, TYPE_PACKED_INT64_ARRAY, len(granules)))
    for g in granules:
        seq_body.extend(struct.pack("<q", g))

    # 3) sampling_rate: float (f32 when use_real64=0)
    seq_body.extend(struct.pack("<IIf", 5, TYPE_FLOAT, float(sample_rate)))

    # Internal resource 1: AudioStreamOggVorbis
    main_body = bytearray()
    prop_count = 1 + (1 if loop else 0) + (1 if loop_offset > 0.0 else 0)
    main_body.extend(struct.pack("<I", prop_count))

    # packet_sequence -> internal resource index 0 (OBJ_INTERNAL = 2)
    main_body.extend(struct.pack("<IIII", 0, TYPE_OBJECT, 2, 0))
    if loop:
        main_body.extend(struct.pack("<III", 1, TYPE_BOOL, 1))
    if loop_offset > 0.0:
        main_body.extend(struct.pack("<IIf", 2, TYPE_FLOAT, float(loop_offset)))

    out = bytearray()
    out.extend(b"RSRC")
    out.extend(struct.pack("<III", 0, 0, 2))  # big_endian=0, use_real64=0, format_ver=2
    out.extend(struct.pack("<III", 4, 2, 0))  # Godot 4.2.0 header tag
    out.extend(pack_str("AudioStreamOggVorbis"))
    out.extend(struct.pack("<Q", 0))  # import_md_offset
    out.extend(struct.pack("<I", 0))  # flags
    out.extend(b"\x00" * 16)          # uid
    out.extend(b"\x00" * (11 * 4))    # reserved

    out.extend(str_tbl)
    out.extend(struct.pack("<I", 0))  # 0 external resources

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
            return False  # Skip BasisU / raw GPU formats
        if 56 + payload_len > len(data):
            return False

        raw_img = data[56 : 56 + payload_len]
        ext = "webp" if data_format == GST2_DATA_FORMAT_WEBP else "png"
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
            return False

        with open(out_tmp, "rb") as f:
            new_webp = f.read()

        if len(new_webp) == 0 or len(new_webp) >= payload_len:
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
            return False

        with open(out_tmp, "rb") as f:
            new_webp = f.read()

        if len(new_webp) == 0 or len(new_webp) >= payload_len:
            return False

        new_fmt = (fmt & ~GDST_FORMAT_MASK) | GDST_FORMAT_WEBP
        new_header = bytearray(data[:24])
        struct.pack_into("<II", new_header, 16, new_fmt, len(new_webp))
        with open(path, "wb") as f:
            f.write(new_header)
            f.write(new_webp)
        return True

    return False


def repack_audio(path, ffmpeg_bin, tmp_dir):
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RSRC":
        return False

    is_godot4 = b"AudioStreamOggVorbis" in data[:512]
    in_ogg = os.path.join(tmp_dir, "in.ogg")
    out_ogg = os.path.join(tmp_dir, "out.ogg")

    if not is_godot4:
        # Godot 3 (.oggstr): raw Ogg Vorbis stream embedded inside PackedByteArray
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
            f.write(b"\x00" * pad_len)
            f.write(b"RSRC")
        return True

    # Godot 4 (.oggvorbisstr): parse OggPacketSequence -> reframe OggS -> transcode -> rebuild RSRC
    try:
        parsed_rsrc = parse_godot4_ogg_rsrc(data)
    except Exception:
        return False

    if not parsed_rsrc:
        return False

    pages, granules, _sr, loop, loop_offset = parsed_rsrc
    reconstructed_ogg = reconstruct_ogg_from_packets(pages, granules)
    if not reconstructed_ogg:
        return False

    with open(in_ogg, "wb") as f:
        f.write(reconstructed_ogg)

    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_ogg, "-c:a", "libvorbis", "-qscale:a", "3", "-f", "ogg", out_ogg
    ]
    if subprocess.run(cmd).returncode != 0 or not os.path.exists(out_ogg):
        return False

    with open(out_ogg, "rb") as f:
        new_ogg = f.read()

    if len(new_ogg) == 0:
        return False

    new_parsed = parse_ogg_packets(new_ogg)
    if not new_parsed:
        return False

    new_pages, new_granules, new_sr = new_parsed
    new_rsrc = build_godot4_ogg_rsrc(new_pages, new_granules, new_sr, loop, loop_offset)

    if len(new_rsrc) >= len(data):
        return False

    with open(path, "wb") as f:
        f.write(new_rsrc)
    return True


def main():
    if len(sys.argv) < 4:
        sys.exit(1)

    kind = sys.argv[1]
    target = sys.argv[2]
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
