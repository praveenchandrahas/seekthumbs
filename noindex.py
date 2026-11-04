"""Calibrated, index-free thumbnail extraction for MKV/Matroska files that have no Cues element (seek index).

THE PROBLEM. seekthumbs normally grabs a thumbnail by asking PyAV/ffmpeg to seek to a timestamp
(AvExtractor._grab: `container.seek(ts, ...)`). When the file has a Cues element, libavformat jumps
straight to the right byte offset. When it does not (common for files remuxed by some tools, or muxed to
a non-seekable pipe), libavformat instead does its own scattered, iterative "estimate a byte offset, read,
check the timestamp, correct, read again" search for EVERY seek - confirmed directly against a real film
(Troy, a 68.8 GB HYBRID remux with a full, successfully-parsed `mkvinfo` dump showing no Cues section
anywhere) to cost 10x or more the bytes and time of an indexed file. A full `mkvmerge` remux would add a
real index, but the library this is built for is served from a read-only network mount with nowhere
writable to put a remuxed copy, so that fix is not available here.

THE IDEA. We do not need an exact seek. A thumbnail only needs to land within about one GOP of its
target time (the existing indexed path already accepts this: `container.seek(..., backward=True)` lands
on "the keyframe at or before" the target, not the exact frame). So: learn, from a handful of REAL
(byte_offset -> timestamp) data points discovered as the build proceeds, a byte_offset<->timestamp model
for this one file, predict a byte offset for each thumbnail's target time, locate the real Matroska
Cluster nearest that offset with a few small range reads (not a download), and hand PyAV a tiny synthetic
buffer - this file's own header (EBML head + Segment info/tracks/etc, read and cached once) followed by
just that one Cluster's bytes - instead of the whole file. PyAV decodes a keyframe out of that buffer
directly; no `.seek()` call happens at all, so libavformat's expensive internal search never runs.

HOW (the five steps, matching the design agreed with the repo owner):
  1. `scan_header()` reads the file's own EBML head + Segment metadata once, cheaply, and determines
     whether a Cues element exists (checking both a direct sighting and an indirect SeekHead pointer to
     one placed after the clusters) - gates this whole module. Indexed files are untouched: they keep
     using the existing, unmodified `AvExtractor`/`CliExtractor` path.
  2. `Calibrator` starts from a trivial two-point straight-line guess (file start <-> time 0, file end <->
     the film's known duration) - no up-front probing pass is needed.
  3. For each thumbnail, `locate_boundary()` + `walk_to_keyframe_cluster()` turn the model's predicted byte
     offset into the real nearest Matroska Cluster that actually *starts* with a keyframe (clusters do not
     reliably start on a keyframe themselves - confirmed empirically: in a synthetic multi-cluster test
     file, only 1 in 3 clusters led with one - so this walks forward, cluster by cluster, header-only,
     until it finds one that does).
  4. That one cluster's bytes, appended to the cached header, are handed to PyAV as an in-memory buffer.
     A keyframe (H.264 IDR or equivalent) decodes correctly on its own with no preceding data, since by
     definition it depends on no reference frame - confirmed by decoding a cluster in isolation this way
     and getting back exactly the timestamp the cluster itself declares.
  5. (Progressive refinement, explicitly requested as an optional extra.) Every thumbnail resolved by this
     fast path hands back the Cluster's own real (byte_offset, timestamp) pair, which `Calibrator.add_point`
     folds into the model - so the fit tightens through the film as more of it is sampled, with no separate
     calibration pass.

SAFETY. If the predicted cluster's real timestamp lands too far from the target (more than half an
interval or 8 s, whichever is larger - matching the slack a normal indexed seek already has from landing
on "the keyframe at or before" rather than the exact frame), the module retries once with the discrepancy
folded into the model, and if that still misses, gives up on the fast path FOR THAT ONE THUMBNAIL and
calls the ordinary (slower but always-correct) extractor instead. The same happens on any exception
(the EBML walk hits something it doesn't understand, a range read fails, PyAV can't decode the buffer).
This path can only ever be slower-than-hoped for a given thumbnail, never wrong: `NoIndexExtractor` wraps
the ordinary extractor rather than replacing it, and every failure mode falls through to it.

NOT YET DONE: a fallback decode's own real byte offset is not harvested back into the calibrator (PyAV's
public API doesn't expose where a `.seek()` actually landed), so progressive refinement only comes from
fast-path hits. In practice that is enough: the first few thumbnails of a film (near the two seed anchors)
already tend to land close, and every fast-path success anywhere in the file sharpens the local fit for
its neighbours.
"""
import os
import threading

ID_EBML_HEAD = 0x1A45DFA3
ID_SEGMENT = 0x18538067
ID_SEEKHEAD = 0x114D9B74
ID_SEEK = 0x4DBB
ID_SEEKID = 0x53AB
ID_INFO = 0x1549A966
ID_TIMECODESCALE = 0x2AD7B1
ID_CUES = 0x1C53BB6B
ID_CLUSTER = 0x1F43B675
ID_TIMECODE = 0xE7
ID_SIMPLEBLOCK = 0xA3
ID_BLOCKGROUP = 0xA0

MAX_HEADER_BYTES = 64 << 20          # give up rather than buffer an absurd amount of metadata
MAX_CLUSTER_WALK = 200               # give up rather than walk forever looking for a leading keyframe
MAX_CLUSTER_BYTES = 256 << 20        # a cluster size vint this large is almost certainly a false byte match


class NeedMoreData(Exception):
    """Internal: the EBML walk ran off the end of the buffered header and needs more bytes."""


class NoIndex(Exception):
    """Internal: this read did not land on something usable; the caller falls back."""


def _vint_len(first_byte):
    if first_byte == 0:
        return None                   # not a valid EBML vint start (all-zero marker byte)
    for i in range(8):
        if first_byte & (0x80 >> i):
            return i + 1
    return None


def _read_id(buf, pos):
    """Reads an EBML element ID (the marker bits stay part of the value, unlike a size vint)."""
    if pos >= len(buf):
        raise NeedMoreData()
    n = _vint_len(buf[pos])
    if n is None:
        raise NoIndex("bad EBML ID byte")
    if pos + n > len(buf):
        raise NeedMoreData()
    return int.from_bytes(buf[pos:pos + n], "big"), pos + n


def _read_size(buf, pos):
    """Reads an EBML size vint. Returns (size, newpos); size is None for an explicit 'unknown/open-ended'
    size (all data bits set) - EBML streaming files use these and this module does not try to skip them."""
    if pos >= len(buf):
        raise NeedMoreData()
    n = _vint_len(buf[pos])
    if n is None:
        raise NoIndex("bad EBML size byte")
    if pos + n > len(buf):
        raise NeedMoreData()
    first = buf[pos] & (0xFF >> n)
    val = first
    for i in range(1, n):
        val = (val << 8) | buf[pos + i]
    if val == (1 << (7 * n)) - 1:
        return None, pos + n
    return val, pos + n


def _read_uint(buf, pos, size):
    return int.from_bytes(buf[pos:pos + size], "big")


class HeaderInfo:
    __slots__ = ("header_bytes", "has_cues", "timecode_scale", "cluster_start")

    def __init__(self, header_bytes, has_cues, timecode_scale, cluster_start):
        self.header_bytes = header_bytes
        self.has_cues = has_cues
        self.timecode_scale = timecode_scale
        self.cluster_start = cluster_start


def _scan_info_for_timecode_scale(body):
    pos = 0
    while pos < len(body) - 1:
        try:
            iid, p2 = _read_id(body, pos)
            isz, p3 = _read_size(body, p2)
        except (NeedMoreData, NoIndex):
            return None
        if iid == ID_TIMECODESCALE and isz is not None and p3 + isz <= len(body):
            return _read_uint(body, p3, isz)
        if isz is None:
            return None
        pos = p3 + isz
    return None


def _seekhead_points_at_cues(body):
    pos = 0
    while pos < len(body) - 1:
        try:
            sid, p2 = _read_id(body, pos)
            ssz, p3 = _read_size(body, p2)
        except (NeedMoreData, NoIndex):
            return False
        if ssz is None:
            return False
        if sid == ID_SEEK and p3 + ssz <= len(body):
            sub, jp = body[p3:p3 + ssz], 0
            while jp < len(sub) - 1:
                try:
                    xid, jp2 = _read_id(sub, jp)
                    xsz, jp3 = _read_size(sub, jp2)
                except (NeedMoreData, NoIndex):
                    break
                if xid == ID_SEEKID and xsz is not None and jp3 + xsz <= len(sub):
                    if int.from_bytes(sub[jp3:jp3 + xsz], "big") == ID_CUES:
                        return True
                if xsz is None:
                    break
                jp = jp3 + xsz
        pos = p3 + ssz
    return False


def scan_header(read_at, max_bytes=MAX_HEADER_BYTES):
    """read_at(offset, length) -> bytes (may return fewer than requested at EOF). Walks the EBML head and
    the Segment's top-level children up to (not including) the first Cluster. Returns a HeaderInfo, or
    None if the structure can't be parsed this way (truncated/odd file, or metadata bigger than max_bytes)
    - callers must treat None exactly like "assume this file has an index" and use the normal path."""
    chunk = 1 << 20
    data = read_at(0, chunk)
    while True:
        try:
            pos = 0
            eid, p = _read_id(data, pos)
            if eid != ID_EBML_HEAD:
                return None
            esize, p = _read_size(data, p)
            if esize is None:
                return None
            pos = p + esize
            sid, p = _read_id(data, pos)
            if sid != ID_SEGMENT:
                return None
            _, p = _read_size(data, p)          # segment size itself is unused (we only read a prefix)
            pos = p
            has_cues = False
            timecode_scale = 1_000_000           # the Matroska default, used whenever Info lacks its own
            while True:
                cid, p2 = _read_id(data, pos)
                csize, p3 = _read_size(data, p2)
                if cid == ID_CLUSTER:
                    return HeaderInfo(bytes(data[:pos]), has_cues, timecode_scale, pos)
                if cid == ID_CUES:
                    has_cues = True
                if cid == ID_INFO and csize is not None and p3 + csize <= len(data):
                    tcs = _scan_info_for_timecode_scale(data[p3:p3 + csize])
                    if tcs:
                        timecode_scale = tcs
                if cid == ID_SEEKHEAD and csize is not None and p3 + csize <= len(data):
                    if _seekhead_points_at_cues(data[p3:p3 + csize]):
                        has_cues = True
                if csize is None:
                    return None               # an unknown-size top-level element: can't skip past it cheaply
                pos = p3 + csize
        except NeedMoreData:
            if len(data) >= max_bytes:
                return None
            chunk = min(max_bytes, len(data) * 2)
            more = read_at(len(data), chunk - len(data))
            if not more:
                return None
            data += more
        except NoIndex:
            return None


def _cluster_header_info(read_at, off, max_body_peek=4096):
    """Parses one Cluster at `off`: returns (total_len, timecode_ticks_or_None, is_keyframe_leading) or
    None if `off` is not a plausible Cluster start. Reads only a small prefix of the cluster body - never
    the whole thing."""
    head = read_at(off, 16)
    if len(head) < 5 or int.from_bytes(head[:4], "big") != ID_CLUSTER:
        return None
    try:
        size, hdr_end = _read_size(head, 4)
    except (NeedMoreData, NoIndex):
        return None
    if size is None or size <= 0 or size > MAX_CLUSTER_BYTES:
        return None
    body_start = off + hdr_end
    body = read_at(body_start, min(size, max_body_peek))
    bp, tc, kf = 0, None, None
    while bp < len(body) - 1:
        try:
            eid, ep2 = _read_id(body, bp)
            esz, ep3 = _read_size(body, ep2)
        except (NeedMoreData, NoIndex):
            break
        if eid == ID_TIMECODE and esz is not None and ep3 + esz <= len(body):
            tc = _read_uint(body, ep3, esz)
        elif eid in (ID_SIMPLEBLOCK, ID_BLOCKGROUP):
            if eid == ID_SIMPLEBLOCK and esz is not None:
                block = body[ep3:ep3 + min(esz, 16)]
                try:
                    _tn, tnp = _read_size(block, 0)
                    if tnp + 2 < len(block):
                        kf = bool(block[tnp + 2] & 0x80)
                except (NeedMoreData, NoIndex):
                    pass
            break                       # the Timecode (if any) always precedes the first block; stop here
        if esz is None:
            break
        bp = ep3 + esz
    return (body_start - off) + size, tc, kf


def locate_boundary(read_at, guess, lo_bound, window=3 << 20, back_margin=1 << 16):
    """Byte-pattern search for A plausible Cluster start at/after max(lo_bound, guess - back_margin).
    Validated by a readable size and a Timecode child - cheap, but not proof; `walk_to_keyframe_cluster`'s
    own `_cluster_header_info` re-validates every step. Returns a byte offset, or None."""
    start = max(lo_bound, guess - back_margin)
    region = read_at(start, window)
    pat = b"\x1f\x43\xb6\x75"
    i = 0
    while True:
        i = region.find(pat, i)
        if i < 0:
            return None
        off = start + i
        info = _cluster_header_info(read_at, off)
        if info is not None and info[1] is not None:
            return off
        i += 1


def walk_to_keyframe_cluster(read_at, start_off, max_clusters=MAX_CLUSTER_WALK):
    """From a Cluster boundary, walks forward cluster-by-cluster (header-only reads) to the first one
    whose own first block is a keyframe - Clusters do not reliably start on one themselves. Returns
    (offset, total_len, timecode_ticks) or None (gave up within max_clusters)."""
    off = start_off
    for _ in range(max_clusters):
        info = _cluster_header_info(read_at, off)
        if info is None:
            return None
        total_len, tc, kf = info
        if kf and tc is not None:
            return off, total_len, tc
        off += total_len
    return None


class Calibrator:
    """A per-film byte_offset<->timestamp model, seeded with a straight line across the whole file and
    refined with every real data point discovered while building (step 5, progressive refinement)."""

    def __init__(self, header_end, file_size, duration):
        self.lock = threading.Lock()
        self.points = [(float(header_end), 0.0), (float(max(file_size, header_end + 1)), float(duration))]
        self._cap = 200

    def predict(self, t):
        with self.lock:
            pts = self.points
        lo, hi = pts[0], pts[-1]
        for a, b in zip(pts, pts[1:]):
            if a[1] <= t <= b[1]:
                lo, hi = a, b
                break
            if t < a[1]:
                break
        if hi[1] == lo[1]:
            return lo[0]
        frac = (t - lo[1]) / (hi[1] - lo[1])
        return lo[0] + frac * (hi[0] - lo[0])

    def add_point(self, offset, t):
        with self.lock:
            if len(self.points) >= self._cap:
                return
            pts = self.points + [(float(offset), float(t))]
            pts.sort()
            # de-dupe points that fall on the same byte offset (keep the first)
            out, last = [], None
            for p in pts:
                if last is None or p[0] != last:
                    out.append(p)
                    last = p[0]
            self.points = out


class NoIndexExtractor:
    """Wraps an ordinary extractor (AvExtractor/AutoExtractor/CliExtractor): tries the fast, index-free
    path first and falls through to the wrapped extractor on anything it can't resolve confidently. Safe
    by construction - every failure mode of the fast path ends in `self.fallback.grab(t, out)`, exactly
    what would have run without this wrapper."""

    def __init__(self, src, vf, cfg, info, size, header, read_at, fallback, log=None):
        import av
        self.av = av
        self.src, self.vf, self.cfg = src, vf, cfg
        self.header = header                                   # HeaderInfo
        self.read_at = read_at
        self.fallback = fallback
        self.log = log or (lambda *a: None)
        self.calibrator = Calibrator(header.cluster_start, size, info["duration"])
        self.tol = max(float(cfg.get("interval") or 20) * 0.5, 8.0)
        self.fast_ok = 0
        self.fast_fail = 0
        self.name = "pyav"              # the fast path always decodes via PyAV; keeps ext_of_frame's "ppm" choice correct

    def grab(self, t, out):
        try:
            ok = self._fast_grab(t, out)
            if ok:
                self.fast_ok += 1
                return True, ""
        except Exception as ex:                 # noqa: BLE001 - any failure here just means "use the slow path"
            self.fast_fail += 1
            if self.fast_fail <= 3 or self.fast_fail % 50 == 0:
                self.log(f"no-index read at {t:.0f} s fell back ({type(ex).__name__}: {ex})")
        return self.fallback.grab(t, out)

    def _fast_grab(self, t, out):
        found = self._find_near(t)
        if found is None:
            return False
        off, clen, tc = found
        actual_t = tc * self.header.timecode_scale / 1e9
        if abs(actual_t - t) > self.tol:
            self.calibrator.add_point(off, actual_t)            # fold the miss in, then try exactly once more
            found = self._find_near(t)
            if found is None:
                return False
            off, clen, tc = found
            actual_t = tc * self.header.timecode_scale / 1e9
            if abs(actual_t - t) > self.tol:
                return False
        cluster = self.read_at(off, clen)
        if len(cluster) != clen:
            return False
        buf = self.header.header_bytes + cluster
        ok = self._decode(buf, out)
        if ok:
            self.calibrator.add_point(off, actual_t)
        return ok

    def _find_near(self, t):
        guess = int(self.calibrator.predict(t))
        boundary = locate_boundary(self.read_at, guess, self.header.cluster_start)
        if boundary is None:
            return None
        return walk_to_keyframe_cluster(self.read_at, boundary)

    def _decode(self, buf, out):
        import io
        c = self.av.open(io.BytesIO(buf))
        try:
            stream = c.streams.video[0]
            try:
                stream.codec_context.skip_frame = "NONKEY"
            except Exception:                   # noqa: BLE001
                pass
            frame = None
            for pkt in c.demux(stream):
                for fr in pkt.decode():
                    frame = fr
                    break
                if frame is not None:
                    break
            if frame is None:
                return False
            from seekthumbs import AvExtractor, write_ppm            # local import: avoids a import cycle at module load
            g, fbuf, sink = AvExtractor._graph_for(self.vf, stream)
            fbuf.push(frame)
            o = sink.pull()
            plane = o.planes[0]
            ls, w, h = plane.line_size, o.width, o.height
            data = bytes(plane)
            write_ppm(out, w, h, (data[r * ls: r * ls + w * 3] for r in range(h)))
            return True
        finally:
            c.close()

    def close(self):
        self.fallback.close()
        close_read_at(self.read_at)


def local_read_at(path):
    """A thread-safe read_at(offset, length) over a local file (os.pread does not move a shared offset)."""
    fd = os.open(path, os.O_RDONLY)

    def read_at(offset, length):
        return os.pread(fd, length, offset)

    read_at._fd = fd           # keep a reference so close_read_at() can close it when the film build is done
    return read_at


def rangefile_read_at(rf):
    """A thread-safe read_at(offset, length) over a seekthumbs.RangeFile (its own chunk cache is not
    locked, so calls are serialized here - the reads this module makes are small and infrequent enough
    that this is not a bottleneck)."""
    lock = threading.Lock()

    def read_at(offset, length):
        with lock:
            rf.seek(offset)
            return rf.read(length)

    read_at._rangefile = rf
    return read_at


def close_read_at(read_at):
    fd = getattr(read_at, "_fd", None)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    rf = getattr(read_at, "_rangefile", None)
    if rf is not None:
        try:
            rf.close()
        except Exception:                        # noqa: BLE001
            pass


def detect_no_index(read_at, info):
    """Returns a HeaderInfo if the file has no usable Cues element (direct or via SeekHead), or None if
    it has one (or couldn't be determined) - either way, None means "use the normal path"."""
    try:
        hdr = scan_header(read_at)
    except Exception:                            # noqa: BLE001 - never let a parsing bug break a build
        return None
    if hdr is None or hdr.has_cues:
        return None
    return hdr
