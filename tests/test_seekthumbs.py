#!/usr/bin/env python3
"""Tests for tools/seekthumbs: generator (synthetic films made with ffmpeg) and the mpv viewer (Lua run under lupa with a mocked mp API).
Needs ffmpeg and, for the viewer part, `pip install lupa` (skipped with a note if missing). Does not need mpv."""
import contextlib
try:
    import av as _av_probe  # noqa: F401
    HAVE_AV = True
except Exception:
    HAVE_AV = False
EXTRACTOR_ARGS = [] if HAVE_AV else ["--play-extractor", "auto"]   # CI runners have no PyAV: allow the ffmpeg fallback in the tests that are not about that rule
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..")
sys.path.insert(0, TOOL)
import seekthumbs as st  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails += 1


def make_film(path, seconds=130, size="1280x720", src="testsrc2", gop=48):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=s={size}:r=24", "-t", str(seconds), "-g", str(gop),
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
    old = time.time() - 7200
    os.utime(path, (old, old))


def cfg(lib, store, **kw):
    c = dict(st.DEFAULTS)
    c.update(library=lib, store=store, pause=0, film_pause=0, interval=10.0, min_size_mb=0, settle=60, yield_to_playback=0, workers=4, rate_cooldown=0.2)
    c.update(kw)
    return c


tmp = tempfile.mkdtemp(prefix="seekthumbs-test-")
lib, store = os.path.join(tmp, "lib"), os.path.join(tmp, "store")
try:
    film = os.path.join(lib, "Movies", "A Film (2020)", "A.Film.2020.mkv")
    make_film(film)
    c = cfg(lib, store)

    # --- plan finds it, scan builds it, second plan finds nothing
    check("plan finds the new film", st.scan_pass(c, dry=True) == 1)
    check("scan builds one film", st.scan_pass(c) == 1)
    edir = st.entry_dir(store, os.path.relpath(film, lib))
    ix = st.read_index(edir)
    check("entry mirrors the library path", ix is not None and os.path.isfile(os.path.join(edir, "thumbs.pack")))
    check("thumbnail count = duration // interval", ix and ix["count"] == 13, ix and ix["count"])
    check("all lengths non-zero, offsets contiguous",
          ix and all(l > 0 for l in ix["lengths"]) and all(ix["offsets"][i + 1] == ix["offsets"][i] + ix["lengths"][i] for i in range(ix["count"] - 1)))
    with open(os.path.join(edir, "thumbs.pack"), "rb") as f:
        f.seek(ix["offsets"][5])
        check("a thumbnail starts with the JPEG marker", f.read(2) == b"\xff\xd8")
    check("nothing left to do after the build", st.scan_pass(c, dry=True) == 0)
    check("verify passes", st.cmd_verify(c) == 0)

    # --- size budget: a tiny budget forces the ladder down but never above the allowance (or the ladder's floor)
    tight = cfg(lib, os.path.join(tmp, "store2"), budget_mb=0.02)
    st.scan_pass(tight)
    ix2 = st.read_index(st.entry_dir(tight["store"], os.path.relpath(film, lib)))
    sz2 = sum(os.path.getsize(os.path.join(st.entry_dir(tight["store"], os.path.relpath(film, lib)), f)) for f in ("thumbs.pack",))
    check("tight budget lowers quality or width", ix2 and (ix2["q"] > ix["q"] or ix2["w"] < ix["w"]), (ix2 and (ix2["q"], ix2["w"])))
    check("tight budget gives a smaller pack", sz2 < os.path.getsize(os.path.join(edir, "thumbs.pack")))

    # --- a changed file (new size) is rebuilt; a settle window skips fresh files
    make_film(film, seconds=140)
    check("a film whose size changed is queued again", st.scan_pass(c, dry=True) == 1)
    new = os.path.join(lib, "Fresh.mkv")
    make_film(new, seconds=60)
    os.utime(new, None)
    check("a file modified just now is skipped (settle)", st.scan_pass(c, dry=True) == 1)
    os.utime(new, (time.time() - 7200,) * 2)
    st.scan_pass(c)
    check("both are built after the settle window", st.scan_pass(c, dry=True) == 0)

    # --- failures: a corrupt file is recorded with a backoff and does not stop the others
    bad = os.path.join(lib, "Broken.mkv")
    with open(bad, "wb") as f:
        f.write(b"not a video" * 1000)
    os.utime(bad, (time.time() - 7200,) * 2)
    st.scan_pass(c)
    state = st.State(store)
    check("corrupt file is recorded as failed", "Broken.mkv" in state.d["fail"])
    why = state.d["fail"]["Broken.mkv"]["why"]
    check("the failure reason is kept without the file's path in front of it", "Broken.mkv" not in why and "<file>" in why and len(why.replace("<file>", "")) > 10, why)
    check("failed file is not retried at once", st.scan_pass(c, dry=True) == 0)

    # --- prune: orphan removal, and refusal when the library looks unmounted
    os.remove(new)
    out = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib, "--store", store, "--min-size-mb", "0", "prune"],
                         capture_output=True, text=True).stdout
    check("prune lists the orphan but deletes nothing without --yes", "Fresh.mkv" in out and os.path.isdir(st.entry_dir(store, "Fresh.mkv")))
    empty = os.path.join(tmp, "emptylib")
    os.makedirs(empty)
    r = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", empty, "--store", store, "prune", "--yes"],
                       capture_output=True, text=True)
    check("prune refuses when the library is empty", r.returncode != 0 and os.path.isdir(st.entry_dir(store, "Fresh.mkv")))
    check("scan does nothing when the library is empty", st.scan_pass(cfg(empty, store)) == 0)


    # --- the governor and the parallel build
    import threading
    g = st.Governor(4, 0.05)
    for _ in range(4):
        g.acquire()
    g.release("throttled")
    check("a rate-limit signal halves the parallel reads", g.limit == 2, g.limit)
    g.release("throttled")
    check("a burst of signals acts only once", g.limit == 2 and g.throttles == 2)
    g.release("ok"); g.release("ok")
    time.sleep(0.2)
    for _ in range(45):
        g.acquire(); g.release("ok")
    check("clean reads raise the limit again", g.limit >= 3, g.limit)

    rel2 = os.path.relpath(film, lib)
    gov = st.Governor(4, 0.05)
    cfg2 = cfg(lib, os.path.join(tmp, "store3"), extractor="ffmpeg")
    orig = st.CliExtractor.grab
    calls = {"n": 0}
    cl = threading.Lock()

    def flaky(self, t, out):
        with cl:
            calls["n"] += 1
            k = calls["n"]
        if k <= 6:
            return False, "HTTP error 429 Too Many Requests"
        return orig(self, t, out)

    st.CliExtractor.grab = flaky
    try:
        ok_ = st.build_entry(rel2, film, os.path.getsize(film), cfg2, gov, False)
    finally:
        st.CliExtractor.grab = orig
    ix3 = st.read_index(st.entry_dir(cfg2["store"], rel2))
    check("the build survives a burst of 429s and still reads every thumbnail", ok_ and ix3 and all(l > 0 for l in ix3["lengths"]), calls)
    check("the governor recorded the rate-limit signals and backed off", gov.throttles >= 1)

    st.CliExtractor.grab = lambda self, t, out: (False, "HTTP error 429 Too Many Requests")
    try:
        try:
            st.build_entry("x/" + rel2, film, os.path.getsize(film), cfg(lib, os.path.join(tmp, "store4"), extractor="ffmpeg", rate_cooldown=0.00005),
                           st.Governor(2, 0.00005), False)
            raised = None
        except st.Throttled as ex:
            raised = "Throttled"
        except Exception as ex:
            raised = type(ex).__name__
    finally:
        st.CliExtractor.grab = orig
    check("endless 429s end in Throttled (film not marked as bad)", raised == "Throttled", raised)

    st.CliExtractor.grab = lambda self, t, out: (False, "Invalid data found when processing input")
    try:
        try:
            st.build_entry("y/" + rel2, film, os.path.getsize(film), cfg(lib, os.path.join(tmp, "store5"), extractor="ffmpeg"), st.Governor(2, 0.01), False)
            raised = None
        except st.ReadFailures:
            raised = "ReadFailures"
        except Exception as ex:
            raised = type(ex).__name__
    finally:
        st.CliExtractor.grab = orig
    check("plain read failures end in ReadFailures", raised == "ReadFailures", raised)

    try:
        import av  # noqa: F401
        have_av = True
    except ImportError:
        have_av = False
        print("SKIP PyAV tests (pip install av)")
    if have_av:
        cfg6 = cfg(lib, os.path.join(tmp, "store6"), extractor="pyav")
        okv = st.build_entry(rel2, film, os.path.getsize(film), cfg6, st.Governor(4, 0.05), False)
        ixv = st.read_index(st.entry_dir(cfg6["store"], rel2))
        check("PyAV extractor builds the same number of thumbnails", okv and ixv and ixv["count"] == ix3["count"] and all(l > 0 for l in ixv["lengths"]))
        with open(os.path.join(st.entry_dir(cfg6["store"], rel2), "thumbs.pack"), "rb") as f:
            f.seek(ixv["offsets"][3])
            check("PyAV thumbnails are JPEGs", f.read(2) == b"\xff\xd8")
        # HDR-tagged film: the pip wheel has no zscale, so the PyAV path must fall back to the ffmpeg command and still finish
        hdr = os.path.join(lib, "HDR.Film.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24", "-t", "60", "-g", "48", "-c:v", "libx264", "-preset",
                        "ultrafast", "-pix_fmt", "yuv420p10le", "-color_trc", "smpte2084", "-color_primaries", "bt2020", "-colorspace", "bt2020nc", hdr], check=True)
        cfg7 = cfg(lib, os.path.join(tmp, "store7"), extractor="auto")
        okh = st.build_entry("HDR.Film.mkv", hdr, os.path.getsize(hdr), cfg7, st.Governor(4, 0.05), st.have_filter("zscale"))
        check("an HDR film builds under extractor=auto (falls back when PyAV lacks zscale)", okh and st.read_index(st.entry_dir(cfg7["store"], "HDR.Film.mkv")))


    # ================================================================ garbage decodes (seen on a rate-limited mount: errors printed, exit code 0, a grey frame)
    import re as _re
    check("decode-error lines are recognised", all(st.decode_error_in(x) for x in (
        "[hevc @ 0x55] Invalid NAL unit size (2147483647 > 10650).", "Error splitting the input into NAL units.", "Invalid data found when processing input",
        "Could not find ref with POC 12", "corrupt decoded frame", "missing reference picture", "Read error at pos. 123")), "")
    check("ordinary ffmpeg lines are not decode errors", not any(st.decode_error_in(x) for x in (
        "", "Changing video frame properties on the fly is not supported by all filters", "deprecated pixel format used", "Past duration 0.99 too large")))

    def ppm(path, fill=None, rnd=False, black_with_dots=False, w=64, h=36):
        import random as _r
        n = w * h * 3
        data = bytes(_r.randrange(256) for _ in range(n)) if rnd else (bytes([0] * n) if black_with_dots else bytes([fill] * n))
        if black_with_dots:
            b = bytearray(data)
            for i in range(0, 60, 3):
                b[i * 50: i * 50 + 3] = b"\xff\xff\xff"
            data = bytes(b)
        with open(path, "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (w, h) + data)
        return path

    pp = os.path.join(tmp, "f.ppm")
    check("a mid-grey frame is suspicious", st.suspicious_frame(ppm(pp, 128)))
    check("a white frame is suspicious", st.suspicious_frame(ppm(pp, 255)))
    check("a black frame (credits) is not", not st.suspicious_frame(ppm(pp, 0)))
    check("a dark grey frame (fade) is not", not st.suspicious_frame(ppm(pp, 40)))
    check("black with a few white pixels (credits text) is not", not st.suspicious_frame(ppm(pp, black_with_dots=True)))
    check("a busy picture is not", not st.suspicious_frame(ppm(pp, rnd=True)))
    check("only PPM frames are looked at", not st.suspicious_frame(os.path.join(tmp, "x.png")))

    def build_logged(rel, cfg_, src=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok_ = st.build_entry(rel, src or film, os.path.getsize(src or film), cfg_, st.Governor(4, 0.05), False)
        m = _re.search(r"(\d+) reads retried after decode errors, (\d+) near-constant", buf.getvalue())
        return ok_, (int(m.group(1)), int(m.group(2))) if m else None, buf.getvalue()

    # the ffmpeg-command path: exit code 0 and a frame, but decode errors on stderr
    real_run = subprocess.run
    inj = {"n": 0}

    def run_noisy(cmd, *a, **k):
        r = real_run(cmd, *a, **k)
        if isinstance(cmd, list) and "-noaccurate_seek" in cmd and inj["n"] < 3:
            inj["n"] += 1
            r = subprocess.CompletedProcess(r.args, r.returncode, r.stdout, (r.stderr or "") +
                                            "[hevc @ 0x1] Invalid NAL unit size (2147483647 > 10650).\n[hevc @ 0x1] Error splitting the input into NAL units.\n")
        return r

    subprocess.run = run_noisy
    try:
        ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store8"), extractor="ffmpeg"))
    finally:
        subprocess.run = real_run
    ix8 = st.read_index(st.entry_dir(os.path.join(tmp, "store8"), rel2))
    check("ffmpeg command: three reads with decode errors are retried and the film is complete", ok_ and counts and counts[0] == 3 and ix8 and all(l > 0 for l in ix8["lengths"]), (counts, out_[-300:]))

    if have_av:
        import av as _av
        orig_g = st.AvExtractor._grab
        seen_t, inj2 = set(), {"n": 0}

        def noisy_grab(self, t, out):
            if t not in seen_t and inj2["n"] < 4:
                inj2["n"] += 1
                _av.logging.log(_av.logging.ERROR, "hevc", "Invalid NAL unit size (2147483647 > 10650).")
            seen_t.add(t)
            return orig_g(self, t, out)

        st.AvExtractor._grab = noisy_grab
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store9"), extractor="pyav"))
        finally:
            st.AvExtractor._grab = orig_g
        ix9 = st.read_index(st.entry_dir(os.path.join(tmp, "store9"), rel2))
        check("PyAV: libav log lines with decode errors make the read fail, it is retried once, the film is complete",
              ok_ and counts and counts[0] == 4 and ix9 and all(l > 0 for l in ix9["lengths"]), (counts, out_[-300:]))

        # extractor=auto must not answer a decode error with the dearer ffmpeg command
        used_cli = []
        orig_cg = st.CliExtractor.grab
        st.CliExtractor.grab = lambda self, t_, out: used_cli.append(1) or orig_cg(self, t_, out)
        seen_t.clear(); inj2["n"] = 0
        st.AvExtractor._grab = noisy_grab
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store10"), extractor="auto"))
        finally:
            st.AvExtractor._grab = orig_g
            st.CliExtractor.grab = orig_cg
        check("extractor=auto retries a decode error with PyAV (no fall-through to one ffmpeg process per thumbnail)", ok_ and counts and counts[0] == 4 and not used_cli, (counts, used_cli))

        # a grey frame once: read again; the same white frame every time is kept (a real white-out)
        def grey_first(self, t, out):
            if t not in seen_t:
                seen_t.add(t)
                ppm(out, 128, w=64, h=36)
                return True, ""
            return orig_g(self, t, out)

        seen_t.clear()
        st.AvExtractor._grab = grey_first
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store11"), extractor="pyav"))
        finally:
            st.AvExtractor._grab = orig_g
        check("a grey frame is read again and replaced by the real picture", ok_ and counts and counts[1] == 14, (counts, out_[-200:]))
        st.AvExtractor._grab = lambda self, t, out: (ppm(out, 255, w=64, h=36), (True, ""))[1]
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store12"), extractor="pyav"))
        finally:
            st.AvExtractor._grab = orig_g
        check("the same white frame twice is kept (it is real), not retried forever", ok_ and counts and counts[1] == 14, (counts, out_[-200:]))

    # a real corrupt file: a bad NAL length in one keyframe (HEVC in mp4)
    has_x265 = "libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if has_x265:
        cor = os.path.join(tmp, "corlib", "Corrupt.One.2022.mp4")
        os.makedirs(os.path.dirname(cor))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24", "-t", "60", "-c:v", "libx265", "-preset", "ultrafast",
                        "-x265-params", "keyint=24:min-keyint=24:log-level=none", "-pix_fmt", "yuv420p", cor], check=True)
        pk = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pos,pts_time,flags", "-of", "json", cor],
                                       capture_output=True, text=True).stdout)["packets"]
        kp = [p_ for p_ in pk if "K" in p_["flags"] and abs(float(p_["pts_time"]) - 35.0) < 0.01][0]
        raw = bytearray(open(cor, "rb").read())
        raw[int(kp["pos"]):int(kp["pos"]) + 4] = bytes([0x7f, 0xff, 0xff, 0xff])
        open(cor, "wb").write(raw)
        for ex_name in ("ffmpeg",) + (("pyav",) if have_av else ()):
            ok_, counts, out_ = build_logged("Corrupt.One.2022.mp4", cfg(lib, os.path.join(tmp, "store13" + ex_name), extractor=ex_name), src=cor)
            ixc = st.read_index(st.entry_dir(os.path.join(tmp, "store13" + ex_name), "Corrupt.One.2022.mp4"))
            check(f"a corrupt keyframe ({ex_name}): the read is retried, then left out; the other thumbnails are good",
                  ok_ and counts and counts[0] >= 1 and ixc and ixc["lengths"][3] == 0 and sum(1 for l in ixc["lengths"] if l == 0) == 1, (counts, ixc and ixc["lengths"], out_[-300:]))
    else:
        print("SKIP corrupt-keyframe test (ffmpeg without libx265)")

    # verify --deep
    cd = cfg(lib, os.path.join(tmp, "store14"), extractor="ffmpeg")
    st.build_entry(rel2, film, os.path.getsize(film), cd, st.Governor(4, 0.05), False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc0 = st.cmd_verify(cd, deep=True)
    check("verify --deep accepts a good entry", rc0 == 0, buf.getvalue())
    edir14 = st.entry_dir(cd["store"], rel2)
    ix14 = st.read_index(edir14)
    wd, hd = ix14["w"], ix14["h"]
    flat = {}
    for nm, colour in (("grey", "gray"), ("black", "black")):
        fp = os.path.join(tmp, f"flat-{nm}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={wd}x{hd}", "-frames:v", "1", "-pix_fmt", "yuvj420p", fp], check=True)
        flat[nm] = open(fp, "rb").read()
    pack = open(os.path.join(edir14, "thumbs.pack"), "rb").read()
    parts = [pack[o:o + l] for o, l in zip(ix14["offsets"], ix14["lengths"])]
    parts[2], parts[4] = flat["grey"], flat["black"]
    offs, pos = [], 0
    for b_ in parts:
        offs.append(pos); pos += len(b_)
    open(os.path.join(edir14, "thumbs.pack"), "wb").write(b"".join(parts))
    ix14.update(offsets=offs, lengths=[len(b_) for b_ in parts])
    json.dump(ix14, open(os.path.join(edir14, "index.json"), "w"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc1 = st.cmd_verify(cd, deep=True)
    check("verify --deep lists a flat grey thumbnail (and not a black one)", rc1 == 1 and "thumbnail 2 " in buf.getvalue() and "thumbnail 4 " not in buf.getvalue(), buf.getvalue())
    check("plain verify does not decode and still passes", st.cmd_verify(cd) == 0)

    # ================================================================ on-play build and sharing between releases
    cases = {
        "Movie.Name.2019.2160p.UHD.BluRay.x265-GRP.mkv": "movie name|2019",
        "Movie Name (2019) [1080p].mkv": "movie name|2019",
        "Blade.Runner.2049.2017.2160p.WEB-DL.mkv": "blade runner 2049|2017",
        "2001.A.Space.Odyssey.1968.1080p.mkv": "2001 a space odyssey|1968",
        "Amélie.2001.720p.mkv": "amelie|2001",
        "Tom & Jerry 2021.mkv": "tom and jerry|2021",
        "Movies/Some Folder/Movie.2019.Extended.2160p.mkv": "movie|2019",
        "www.UIndex.org - The Imitation Game 2014 REMUX 2160p.mkv": "the imitation game|2014",
        "The.Imitation.Game.2014.MULTI.1080p.mkv": "the imitation game|2014",
        "Avatar Fire and Ash 2025 Hybrid 2160p DV HDR10.mkv": "avatar fire and ash|2025",
        "Avatar.Fire.And.Ash.2025.REPACK.MULTi.VF2.2160p.UHD.mkv": "avatar fire and ash|2025",
        "[YTS.MX] Movie Name 2019 1080p.mkv": "movie name|2019",
        "Everest.IMAX.2160p.mkv": None,
        "The.Show.S01E02.2019.1080p.mkv": None,
        "Show.1x05.2019.mkv": None,
        "Show.Season.2.2019.mkv": None,
        "No.Year.Here.1080p.mkv": None,
        "Movie.1080p.2019.mkv": None,
        "2019.mkv": None,
    }
    wrong = {k: st.release_key(k) for k, v in cases.items() if st.release_key(k) != v}
    check("release_key parses titles and years (and refuses what it cannot be sure of)", not wrong, wrong)
    check("different title strings do not match (a separate build is the safe fallback)",
          st.release_key("Hope.2026.1080p.WEB.mkv") != st.release_key("Hopeu - Hope (2026)CZtit 1080p.mkv"))

    lib2, store2 = os.path.join(tmp, "lib2"), os.path.join(tmp, "store2s")
    names = {"a": "Cool.Film.2019.2160p.WEB-DL.mkv", "b": "Cool Film (2019) 1080p BluRay.mkv", "c": "Cool.Film.2019.Extended.mkv",
             "d": "Cool.Film.2020.mkv", "e": "Other.Movie.mkv", "f": "Some.Show.S01E01.2019.mkv"}
    for k, n in names.items():
        make_film(os.path.join(lib2, n), seconds=75 if k == "c" else 60, size="1280x720" if k == "b" else "640x360")
    c2 = cfg(lib2, store2)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        st.scan_pass(c2, dry=True)
    pl = buf.getvalue()
    check("plan marks the release that is built (the smaller file) and the one that only gets an alias",
          "share this" in pl and "alias of the one above" in pl and pl.index(names["a"]) < pl.index(names["b"]), pl)
    check("plan summary counts films and aliases", "6 file(s) need thumbnails = 4 film(s) to build + 2 release(s)" in pl, pl)
    st.scan_pass(c2)
    kinds = {k: ("real" if st.read_index(st.entry_dir(store2, n)) else "alias" if st.read_alias(st.entry_dir(store2, n)) else None) for k, n in names.items()}
    ab = sorted([kinds["a"], kinds["b"]])
    check("two releases of one film: one real entry and one alias", ab == ["alias", "real"], kinds)
    check("a different cut (runtime), a different year, no year and a TV episode each get their own thumbnails",
          all(kinds[k] == "real" for k in "cdef"), kinds)
    check("batch: of two releases the smaller file is the one that is built", kinds["a"] == "real" and kinds["b"] == "alias", kinds)
    alias_k = "a" if kinds["a"] == "alias" else "b"
    real_k = "b" if alias_k == "a" else "a"
    alias_rel, real_rel = names[alias_k], names[real_k]
    adir = st.entry_dir(store2, alias_rel)
    check("an alias folder holds only its small index.json", os.listdir(adir) == ["index.json"] and os.path.getsize(os.path.join(adir, "index.json")) < 1000)
    asize, rsize = os.path.getsize(os.path.join(lib2, alias_rel)), os.path.getsize(os.path.join(lib2, real_rel))
    r_al, r_re = st.resolve_entry(store2, alias_rel, asize), st.resolve_entry(store2, real_rel, rsize)
    check("the alias resolves to the other release's thumbnails", r_al and r_re and r_al[2] == real_rel and r_al[0]["made"] == r_re[0]["made"])
    check("the alias is only valid for its own file size", st.resolve_entry(store2, alias_rel, asize + 1) is None)
    check("nothing is left to build", st.scan_pass(c2, dry=True) == 0)
    check("verify accepts aliases", st.cmd_verify(c2) == 0)
    out = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--store", store2, "status"], capture_output=True, text=True).stdout
    check("status counts aliases separately", "1 sharing another release" in out, out)

    # a replaced alias file is queued and shares again
    make_film(os.path.join(lib2, alias_rel), seconds=60, size="320x180")
    check("a replaced file with an alias is queued again", st.scan_pass(c2, dry=True) == 1)
    st.scan_pass(c2)
    asize = os.path.getsize(os.path.join(lib2, alias_rel))
    check("after the rebuild it shares again", st.read_alias(adir) is not None and st.resolve_entry(store2, alias_rel, asize))

    # the target is rebuilt: the alias stops resolving (its pictures may no longer fit), then is re-linked
    make_film(os.path.join(lib2, real_rel), seconds=60, size="480x270")
    st.scan_pass(c2, dry=False) if False else None
    rr = os.path.join(lib2, real_rel)
    lk = st.film_lock(store2, alias_rel)                    # keep the alias out of the next pass so the stale state can be looked at
    st.scan_pass(c2)
    lk.close()
    check("when the target is rebuilt the alias no longer resolves", st.resolve_entry(store2, alias_rel, asize) is None and st.read_index(st.entry_dir(store2, real_rel)))
    st.scan_pass(c2)
    check("the next pass links the alias to the new thumbnails", st.resolve_entry(store2, alias_rel, asize) is not None)

    # loops and broken links
    for nm in ("Loop.A.2000.mkv", "Loop.B.2000.mkv"):
        os.makedirs(st.entry_dir(store2, nm))
    for me, other in (("Loop.A.2000.mkv", "Loop.B.2000.mkv"), ("Loop.B.2000.mkv", "Loop.A.2000.mkv")):
        with open(os.path.join(st.entry_dir(store2, me), "index.json"), "w") as f:
            json.dump(dict(v=2, alias_of=other, src=me, size=5, duration=60, target_made="x", made="x"), f)
    check("an alias loop gives nothing (and does not hang)", st.resolve_entry(store2, "Loop.A.2000.mkv", 5) is None)
    os.makedirs(st.entry_dir(store2, "Dangling.2000.mkv"))
    with open(os.path.join(st.entry_dir(store2, "Dangling.2000.mkv"), "index.json"), "w") as f:
        json.dump(dict(v=2, alias_of="Not.There.2000.mkv", src="Dangling.2000.mkv", size=5, duration=60, target_made="x", made="x"), f)
    check("an alias to a missing entry gives nothing", st.resolve_entry(store2, "Dangling.2000.mkv", 5) is None)
    check("verify reports broken aliases", st.cmd_verify(c2) == 1)
    for nm in ("Loop.A.2000.mkv", "Loop.B.2000.mkv", "Dangling.2000.mkv"):
        shutil.rmtree(st.entry_dir(store2, nm))

    # prune and aliases
    lib3, store3 = os.path.join(tmp, "lib3"), os.path.join(tmp, "store3s")
    for n in ("Real.Film.2018.mkv", "Real Film (2018) 720p.mkv", "Zed.Keep.mkv"):
        make_film(os.path.join(lib3, n), seconds=60, size="640x360" if n.startswith("Real.") else "320x180")
    c3 = cfg(lib3, store3)
    st.scan_pass(c3)
    kinds3 = {n: ("real" if st.read_index(st.entry_dir(store3, n)) else "alias" if st.read_alias(st.entry_dir(store3, n)) else None)
              for n in ("Real.Film.2018.mkv", "Real Film (2018) 720p.mkv")}
    real3 = [n for n, k in kinds3.items() if k == "real"][0]
    alias3 = [n for n, k in kinds3.items() if k == "alias"][0]

    def prune():
        return subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib3, "--store", store3, "--min-size-mb", "0", "prune", "--yes"],
                              capture_output=True, text=True).stdout

    os.remove(os.path.join(lib3, real3))
    out = prune()
    check("prune keeps a pack whose film is gone while an alias still uses it", os.path.isfile(os.path.join(st.entry_dir(store3, real3), "thumbs.pack")) and "keeping" in out, out)
    check("the alias still resolves after that prune", st.resolve_entry(store3, alias3, os.path.getsize(os.path.join(lib3, alias3))) is not None)
    os.remove(os.path.join(lib3, alias3))
    prune()
    check("when both films are gone, prune removes the alias and the pack", not os.path.isdir(st.entry_dir(store3, real3)) and not os.path.isdir(st.entry_dir(store3, alias3)))
    # a broken alias whose film still exists is removed
    make_film(os.path.join(lib3, "Orphan.Alias.2011.mkv"), seconds=60)
    os.makedirs(st.entry_dir(store3, "Orphan.Alias.2011.mkv"))
    with open(os.path.join(st.entry_dir(store3, "Orphan.Alias.2011.mkv"), "index.json"), "w") as f:
        json.dump(dict(v=2, alias_of="Gone.2011.mkv", src="Orphan.Alias.2011.mkv", size=5, duration=60, target_made="x", made="x"), f)
    prune()
    check("prune removes an alias whose target is gone", not os.path.isdir(st.entry_dir(store3, "Orphan.Alias.2011.mkv")))
    check("prune leaves entries of films that still exist", st.read_index(st.entry_dir(store3, "Zed.Keep.mkv")) is not None)

    # ---- build FILE (what the viewer starts)
    lib4, store4 = os.path.join(tmp, "lib4"), os.path.join(tmp, "store4s")
    bf = os.path.join(lib4, "Build.Me.2021.mkv")
    make_film(bf, seconds=60, size="640x360")
    BASE = [sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib4, "--store", store4, "--min-size-mb", "0", "--interval", "10", "--play-pause", "0"] + EXTRACTOR_ARGS

    def run_build(path, extra=()):
        return subprocess.run(BASE + list(extra) + ["build", path], capture_output=True, text=True)

    t0 = time.time()
    r = run_build(bf)
    check("build FILE builds the film's thumbnails", r.returncode == 0 and st.resolve_entry(store4, "Build.Me.2021.mkv", os.path.getsize(bf)), r.stdout + r.stderr)
    check("build FILE said it uses the gentle settings (2 reads, lowest priority)", "2 parallel read(s)" in r.stdout and "lowest priority" in r.stdout, r.stdout)
    r = run_build(bf)
    check("a second build FILE finds the entry and does nothing", r.returncode == 0 and "already has thumbnails" in r.stdout, r.stdout)
    check("build FILE for a path outside the library is refused", run_build(os.path.join(tmp, "elsewhere.mkv")).returncode == 2)
    iso = os.path.join(lib4, "disc.iso")
    open(iso, "wb").write(b"x" * 100000)
    r = run_build(iso)
    check("build FILE skips a non-video file", r.returncode == 0 and "not a video" in r.stdout and not os.path.isdir(st.entry_dir(store4, "disc.iso")), r.stdout)
    r = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib4, "--store", store4, "build", bf.replace("Build.Me", "Build.Me")],
                       capture_output=True, text=True, env=dict(os.environ, HOME=tmp))
    small = os.path.join(lib4, "Small.One.2020.mkv")
    make_film(small, seconds=20, size="320x180")
    r = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib4, "--store", store4, "build", small], capture_output=True, text=True,
                       env=dict(os.environ, HOME=tmp))
    check("build FILE skips a film under the minimum size (300 MB by default)", r.returncode == 0 and "smaller than" in r.stdout and not os.path.isdir(st.entry_dir(store4, "Small.One.2020.mkv")), r.stdout)

    lockme = os.path.join(lib4, "Lock.Me.2022.mkv")
    make_film(lockme, seconds=60, size="320x180")
    lk = st.film_lock(store4, "Lock.Me.2022.mkv")
    r = run_build(lockme)
    check("build FILE does nothing while another instance holds the film's lock", r.returncode == 0 and "already being built" in r.stdout and not os.path.isdir(st.entry_dir(store4, "Lock.Me.2022.mkv")), r.stdout)
    check("scan skips a film whose lock is held", st.scan_pass(cfg(lib4, store4)) >= 0 and not os.path.isdir(st.entry_dir(store4, "Lock.Me.2022.mkv")))
    lk.close()
    r = run_build(lockme)
    check("once the lock is free build FILE builds it", r.returncode == 0 and st.read_index(st.entry_dir(store4, "Lock.Me.2022.mkv")) is not None, r.stdout)

    race = os.path.join(lib4, "Race.Me.2023.mkv")
    make_film(race, seconds=60, size="320x180")
    procs = [subprocess.Popen(BASE + ["build", race], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(3)]
    outs = [p_.communicate()[0] for p_ in procs]
    nbuilt = sum(o.count("built Race.Me.2023.mkv") for o in outs)
    check("three builds started together build the film exactly once", nbuilt == 1 and st.read_index(st.entry_dir(store4, "Race.Me.2023.mkv")), outs)

    broken = os.path.join(lib4, "Broken.Build.2020.mkv")
    open(broken, "wb").write(b"not a video" * 1000)
    r1 = run_build(broken)
    r2 = run_build(broken)
    check("a film that cannot be read fails once and is then not retried at once", r1.returncode == 1 and r2.returncode == 0 and "failed recently" in r2.stdout, (r1.stdout, r2.stdout))

    # in-process: workers, no waiting for players, scan still waits
    seen = {}
    orig_build, orig_pr = st.build_entry, st.player_running
    st.build_entry = lambda rel, src, size, c_, gov, zs: seen.update(workers=c_["workers"], jobs=c_["encode_jobs"], limit=gov.max, pause=c_["pause"]) or True
    def boom():
        raise AssertionError("build FILE must not wait for players")
    st.player_running = boom
    newf = os.path.join(lib4, "Workers.Test.2024.mkv")
    make_film(newf, seconds=30, size="320x180")
    try:
        rc = st.cmd_build(cfg(lib4, store4, play_workers=3, play_pause=0.5, yield_to_playback=1), newf, own_process=False)
    finally:
        st.build_entry, st.player_running = orig_build, orig_pr
    check("build FILE uses play_workers (not workers) and does not wait for a running player", rc == 0 and seen.get("workers") == 3 and seen.get("limit") == 3 and seen.get("jobs") == 2 and seen.get("pause") == 0.5, (rc, seen))

    # during playback: never fall back to one ffmpeg process per thumbnail
    strict = os.path.join(lib4, "Strict.Pyav.2025.mkv")
    make_film(strict, seconds=40, size="320x180")
    orig_usable, orig_grab = st.AvExtractor.usable, st.CliExtractor.grab
    st.AvExtractor.usable = staticmethod(lambda vf: (False, "PyAV is not installed"))
    used_cli = []
    st.CliExtractor.grab = lambda self, t_, out: used_cli.append(1) or orig_grab(self, t_, out)
    try:
        rc = st.cmd_build(cfg(lib4, store4, play_pause=0), strict, own_process=False)
        check("build FILE skips (and says why) when PyAV is unusable instead of reading with ffmpeg commands",
              rc == 1 and not used_cli and not os.path.isdir(st.entry_dir(store4, "Strict.Pyav.2025.mkv")), (rc, used_cli))
        check("that skip is not recorded as a failure (installing python3-av must work at once)", "Strict.Pyav.2025.mkv" not in st.State(store4).d["fail"])
        rc = st.cmd_build(cfg(lib4, store4, play_pause=0, play_extractor="auto"), strict, own_process=False)
        check("play_extractor=auto allows the ffmpeg command when PyAV is unusable", rc == 0 and used_cli and st.read_index(st.entry_dir(store4, "Strict.Pyav.2025.mkv")), (rc, used_cli))
    finally:
        st.AvExtractor.usable, st.CliExtractor.grab = orig_usable, orig_grab

    waits = []
    orig_sleep = st.sleep_interruptible
    calls_pr = {"n": 0}
    def fake_pr():
        calls_pr["n"] += 1
        return calls_pr["n"] == 1
    st.player_running, st.sleep_interruptible = fake_pr, lambda sec: waits.append(sec)
    try:
        st.scan_pass(cfg(lib4, store4, yield_to_playback=1))
    finally:
        st.player_running, st.sleep_interruptible = orig_pr, orig_sleep
    check("scan still waits while a player is running", waits == [300] and calls_pr["n"] >= 2, (waits, calls_pr))

    # stale work folders
    stale = os.path.join(store4, "Some", ".work-old"); fresh = os.path.join(store4, "Some", ".work-new")
    os.makedirs(os.path.join(stale, "jpg")); os.makedirs(fresh)
    os.utime(stale, (time.time() - 3 * 86400,) * 2)
    st.clean_stale_work(store4)
    check("a build that was killed leaves a work folder; old ones are removed, fresh ones kept", not os.path.exists(stale) and os.path.isdir(fresh))

    # settings work after the subcommand too (the README writes `scan --max-films 1`)
    lib7, store7 = os.path.join(tmp, "lib7"), os.path.join(tmp, "store7s")
    for n in ("Two.Films.Alpha.mkv", "Two.Films.Beta.mkv"):
        make_film(os.path.join(lib7, n), seconds=30, size="320x180")
    after = [sys.executable, os.path.join(TOOL, "seekthumbs.py")]
    r = subprocess.run(after + ["plan", "--library", lib7, "--store", store7, "--min-size-mb", "0"], capture_output=True, text=True)
    check("options after the subcommand work (plan --library ... --store ...)", r.returncode == 0 and "2 file(s) need thumbnails" in r.stdout, r.stdout + r.stderr)
    r = subprocess.run(after + ["scan", "--library", lib7, "--store", store7, "--min-size-mb", "0", "--interval", "10", "--max-films", "1"], capture_output=True, text=True)
    check("scan --max-films 1 builds exactly one film", r.returncode == 0 and len(list(st.store_entries(store7))) == 1, r.stdout + r.stderr)
    r = subprocess.run(after + ["--library", lib7, "--store", store7, "--min-size-mb", "0", "--interval", "10", "--max-films", "1", "scan"], capture_output=True, text=True)
    check("the options before the subcommand still work, and they are not undone by the subcommand", r.returncode == 0 and len(list(st.store_entries(store7))) == 2, r.stdout + r.stderr)

    # a reader that closes the pipe early ends the program quietly
    r = subprocess.run(["bash", "-c", f"cd '{TOOL}' && python3 -c \"import seekthumbs as st, sys; st.main = lambda a: [print('x' * 200) for _ in range(20000)] and 0; sys.argv = ['x']; st.cli()\" | head -1"],
                       capture_output=True, text=True)
    check("plan | head ends quietly on a closed pipe (no BrokenPipeError traceback)", "Traceback" not in r.stderr and "BrokenPipe" not in r.stderr, r.stderr[-300:])

    # install-viewer records the builder
    mc2 = os.path.join(tmp, "mpvconf2")
    os.makedirs(os.path.join(mc2, "script-opts"))
    with open(os.path.join(mc2, "script-opts", "seekthumbs.conf"), "w") as f:
        f.write("debug=yes\nbuilder=/old/place/seekthumbs.py\n")
    st.cmd_install_viewer(mc2)
    st.cmd_install_viewer(mc2)
    conf = open(os.path.join(mc2, "script-opts", "seekthumbs.conf")).read().split("\n")
    check("install-viewer sets builder= once, keeps the other lines, and repeats cleanly",
          conf.count("debug=yes") == 1 and [l for l in conf if l.startswith("builder=")] == ["builder=" + os.path.join(os.path.abspath(TOOL), "seekthumbs.py")], conf)

    # --- the viewer
    try:
        import lupa
    except ImportError:
        print("SKIP viewer tests (pip install lupa)")
        lupa = None
    if lupa:
        LUA = lupa.LuaRuntime(unpack_returned_tuples=True)
        source = open(os.path.join(TOOL, "thumbfast.lua")).read()

        def viewer(path, file_size, library=lib, store_=store, builder="", build=True, min_size=300, extra="", bulk=True, ffmpeg_cmd=None):
            """Loads thumbfast.lua with a mocked mp API. Returns (handlers, log, props)."""
            log = []
            props = {"path": path, "file-size": str(file_size) if file_size is not None else None, "working-directory": "/", "pid": "4242"}
            handlers = {"msg": {}, "ev": {}, "timers": [], "killed": [], "clock": {"t": 1000.0}, "timeouts": [], "defer": False, "queue": [], "started": 0}
            py = {}

            def get_property(name):
                return props.get(name)

            def commandv(*a):
                log.append(("commandv",) + tuple(str(x) for x in a))
                return True

            def command_native(t):
                t = dict(t)
                args = list(dict(t["args"]).values())
                subprocess.run(args, capture_output=True)
                return True

            def command_native_async(t, cb):
                t = dict(t)
                args = list(dict(t["args"]).values())
                handlers["started"] += 1
                if handlers["defer"]:                  # the test finishes it later, so several can be in flight at once
                    handlers["queue"].append((args, cb))
                    return
                r = subprocess.run(args, capture_output=True)
                cb(True, LUA.table(status=r.returncode))

            def add_timeout(sec, fn):
                e = {"due": handlers["clock"]["t"] + sec, "fn": fn, "dead": False}
                handlers["timeouts"].append(e)
                return LUA.table(kill=lambda *a: e.__setitem__("dead", True))

            def reg_msg(name, fn):
                handlers["msg"][name] = fn

            def reg_ev(name, fn):
                handlers["ev"][name] = fn

            LUA.execute("package.loaded['mp']=nil")
            # Stand-ins for the three modules the script requires.
            g = LUA.globals()
            mp = LUA.table(get_property=get_property, commandv=commandv, command_native=command_native, command_native_async=command_native_async,
                           register_script_message=reg_msg, register_event=reg_ev, get_time=lambda: handlers["clock"]["t"], add_timeout=add_timeout,
                           add_periodic_timer=lambda sec, fn: (handlers["timers"].append(fn), LUA.table(kill=lambda *a: handlers["killed"].append(1)))[1],
                           msg=LUA.table(info=lambda *a: log.append(("info",) + tuple(str(x) for x in a))))
            utils = LUA.eval("""function(json_decode, json_encode)
                return { join_path = function(a, b) if b:sub(1,1) == '/' then return b end return (a:gsub('/+$','')) .. '/' .. b end,
                         parse_json = json_decode, format_json = json_encode }
            end""")(lambda s: _to_lua(json.loads(s)), lambda t: json.dumps(_to_py(t)))
            options = LUA.table(read_options=lambda o, name: None)
            g.package.loaded["mp"] = mp
            g.package.loaded["mp.utils"] = utils
            g.package.loaded["mp.options"] = options
            os.environ["XDG_RUNTIME_DIR"] = tmp
            chunk = source.replace('local opts = { library = "", store = "",', f'local opts = {{ library = "{library}", store = "{store_}",')
            chunk = chunk.replace('build = true, builder = "", python = "python3", min_size_mb = 300,',
                                  f'build = {"true" if build else "false"}, builder = "{builder}", python = "python3", min_size_mb = {min_size},')
            if extra:
                chunk = chunk.replace("no_overlay = false, no_convert = false", extra)
            if not bulk:
                chunk = chunk.replace("bulk = true,", "bulk = false,")
            if ffmpeg_cmd:
                chunk = chunk.replace('ffmpeg = "ffmpeg",', f'ffmpeg = "{ffmpeg_cmd}",')
            LUA.execute(chunk)
            return handlers, log, props

        def _to_lua(v):
            if isinstance(v, dict):
                return LUA.table_from({k: _to_lua(x) for k, x in v.items()})
            if isinstance(v, list):
                return LUA.table_from([_to_lua(x) for x in v], False) if False else LUA.table(*[_to_lua(x) for x in v])
            return v

        def _to_py(t):
            if hasattr(t, "items"):
                keys = list(t.keys())
                if keys and all(isinstance(k, int) for k in keys):
                    return [_to_py(t[k]) for k in sorted(keys)]
                return {k: _to_py(v) for k, v in t.items()}
            return t

        def advance(h, dt):
            """Moves the mocked clock on and runs the timeouts that are due."""
            h["clock"]["t"] += dt
            for e in list(h["timeouts"]):
                if not e["dead"] and e["due"] <= h["clock"]["t"]:
                    e["dead"] = True
                    e["fn"]()

        def finish_one(h):
            """Finishes the oldest conversion that is in flight (defer mode): runs the real command, then calls back."""
            args, cb = h["queue"].pop(0)
            r = subprocess.run(args, capture_output=True)
            cb(True, LUA.table(status=r.returncode))

        size = os.path.getsize(film)
        # the film was regenerated after the first build; rebuild so the store matches the current file
        st.scan_pass(c)
        h, log, _ = viewer(film, size)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("viewer finds the entry by library-relative path", infos and infos[-1]["available"] is True, log)
        check("viewer reports the thumbnail size to uosc", infos and infos[-1]["width"] > 0 and infos[-1]["height"] > 0)
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        adds = [l for l in log if l[1] == "overlay-add"]
        check("hovering shows an overlay at the asked position", len(adds) == 1 and adds[0][3:5] == ("100", "50") and adds[0][7] == "bgra", log)
        check("overlay uses the thumbfast overlay id (42)", adds and adds[0][2] == "42")
        if adds:
            f, offs, w_, h_, stride = adds[0][5], int(adds[0][6]), int(adds[0][8]), int(adds[0][9]), int(adds[0][10])
            check("the frame the overlay points at lies inside its raw file", offs % (w_ * h_ * 4) == 0 and os.path.getsize(f) >= offs + w_ * h_ * 4 and stride == w_ * 4)
        log.clear()
        advance(h, 0.05)
        h["msg"]["thumb"]("37", "120", "50")
        check("a second hover on the same thumbnail reuses the converted frame", len([l for l in log if l[1] == "overlay-add"]) == 1)
        h["msg"]["clear"]()
        check("leaving the bar removes the overlay", any(l[1] == "overlay-remove" for l in log))
        log.clear()
        advance(h, 0.05)
        h["msg"]["thumb"]("99999", "10", "10")
        check("a time past the end uses the last thumbnail", len([l for l in log if l[1] == "overlay-add"]) == 1)

        # --- the cost of hovering. Measured 2026-10-29 (4K stream): the viewer sent overlay-add on every mouse move, mpv went from 26 % to
        # 76-101 % of a core, with 14 dropped frames and 16 audio underruns in 9 s while hovering. The viewer now sends a picture at most
        # every 0.04 s, only when it or its place changed, and runs at most 2 conversions at once.
        eix = json.load(open(os.path.join(store, os.path.relpath(film, lib) + ".seekthumbs", "index.json")))
        step, cnt = eix["interval"], eix["count"]
        h, log, _ = viewer(film, size)
        h["ev"]["file-loaded"]()
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        for k in range(300):                          # 300 mouse moves in 0.3 s, the place changing every 10 ms
            h["msg"]["thumb"]("37", str(100 + k // 10), "50")
            advance(h, 0.001)
        advance(h, 0.1)
        adds = [l for l in log if l[1] == "overlay-add"]
        check("300 mouse moves in 0.3 s send at most 10 overlays", 1 <= len(adds) <= 10, len(adds))
        check("the last overlay is where the mouse ended up", adds and adds[-1][3:5] == ("129", "50"), adds[-1] if adds else None)
        log.clear()
        for k in range(100):
            h["msg"]["thumb"]("37", "129", "50")
            advance(h, 0.05)
        check("the same picture in the same place is not sent again", not [l for l in log if l[1] == "overlay-add"], len(log))
        log.clear()
        h["msg"]["thumb"]("37", "131", "50")          # shown at once (the gap since the last one is over)
        advance(h, 0.01)
        h["msg"]["thumb"]("37", "133", "50")          # too soon: it waits
        h["msg"]["clear"]()                           # the mouse leaves before the gap is over
        advance(h, 0.2)
        check("leaving the bar cancels a picture that was waiting", not [l for l in log if l[1] == "overlay-add" and l[3] == "133"] and any(l[1] == "overlay-remove" for l in log), log)

        h, log, _ = viewer(film, size, bulk=False)
        h["ev"]["file-loaded"]()
        h["defer"] = True
        log.clear()
        k0 = h["started"]
        times = [i * step for i in range(cnt)]
        for t in times:                               # a fast sweep along the bar over every thumbnail
            h["msg"]["thumb"](str(t), str(10 + int(t)), "50")
            advance(h, 0.001)
        check("a fast sweep starts at most 2 conversions at once", 1 <= len(h["queue"]) <= 2 and h["started"] - k0 <= 2, (len(h["queue"]), h["started"] - k0))
        while h["queue"]:
            finish_one(h)
            advance(h, 0.001)
        advance(h, 0.1)
        if cnt >= 5:
            check("pictures the mouse had passed are not converted (2 at once, then the newest)", h["started"] - k0 == 3, h["started"] - k0)
        adds = [l for l in log if l[1] == "overlay-add"]
        check("after the sweep the picture under the mouse is shown where the mouse is", adds and adds[-1][3] == str(10 + int(times[-1])), adds[-1] if adds else None)

        for extra, what in (("no_overlay = true, no_convert = false", "no_overlay"), ("no_overlay = false, no_convert = true", "no_convert")):
            h, log, _ = viewer(film, size, extra=extra, bulk=(what == "no_convert"))
            h["ev"]["file-loaded"]()
            log.clear()
            k0 = h["started"]
            for t in (37, 80, 120):
                h["msg"]["thumb"](str(t), "100", "50")
                advance(h, 0.05)
            check(f"{what}: nothing is shown", not [l for l in log if l[1] == "overlay-add"], log)
            check(f"{what}: " + ("pictures are still converted" if what == "no_overlay" else "no conversion is started"),
                  (h["started"] - k0 > 0) if what == "no_overlay" else (h["started"] - k0 == 0), h["started"] - k0)

        h, log, _ = viewer(film, size, bulk=False)
        h["ev"]["file-loaded"]()
        h["defer"] = True
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        h["ev"]["file-loaded"]()                       # another film is loaded while the conversion runs
        while h["queue"]:
            finish_one(h)
        advance(h, 0.1)
        check("a conversion that ends after another film was loaded shows nothing", not [l for l in log if l[1] == "overlay-add"], log)


        # --- one ffmpeg run per film (2026-10-29 18:59 run: with one ffmpeg per picture the audio still ran dry 110 times inside the hover and
        # the display dropped 21 frames, none outside it; mpv's "worker" thread, which runs `subprocess`, was the busiest thread). The hover must start nothing.
        def raw_of(jpg_bytes):
            return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "image2pipe", "-c:v", "mjpeg", "-i", "-", "-f", "rawvideo", "-pix_fmt", "bgra", "-"],
                                  input=jpg_bytes, capture_output=True).stdout

        def overlay_bytes(log_, k=-1):
            a = [l for l in log_ if l[1] == "overlay-add"][k]
            f_, off_, w2, h2 = a[5], int(a[6]), int(a[8]), int(a[9])
            with open(f_, "rb") as fh:
                fh.seek(off_)
                return fh.read(w2 * h2 * 4)

        edir_b = os.path.join(store, os.path.relpath(film, lib) + ".seekthumbs")
        pack_b = open(os.path.join(edir_b, "thumbs.pack"), "rb").read()
        h, log, _ = viewer(film, size)
        h["ev"]["file-loaded"]()
        check("loading a film starts exactly one ffmpeg for all its pictures", h["started"] == 1, h["started"])
        k0 = h["started"]
        log.clear()
        for k in range(0, cnt):                          # hover over every picture in turn
            h["msg"]["thumb"](str(k * step), "100", "50")
            advance(h, 0.05)
        check("hovering over every picture starts no program at all", h["started"] == k0, h["started"] - k0)
        check("every picture was shown", len([l for l in log if l[1] == "overlay-add"]) == cnt, len([l for l in log if l[1] == "overlay-add"]))
        for k in (0, cnt // 2, cnt - 1):
            log.clear()
            advance(h, 0.05)
            h["msg"]["thumb"](str(k * step), str(200 + k), "50")
            o_, l_ = eix["offsets"][k], eix["lengths"][k]
            check(f"picture {k} shown from the shared file is the right picture", overlay_bytes(log) == raw_of(pack_b[o_:o_ + l_]), k)

        # a picture that could not be built has no bytes in the pack: the frames after it must still be matched correctly
        import shutil
        store_g = os.path.join(tmp, "store_gap")
        rel_g = os.path.relpath(film, lib)
        dg = os.path.join(store_g, rel_g + ".seekthumbs")
        os.makedirs(dg)
        parts_g = [pack_b[o:o + l] for o, l in zip(eix["offsets"], eix["lengths"])]
        drop = 2
        parts_g[drop] = b""
        offs_g, pos = [], 0
        for b_ in parts_g:
            offs_g.append(pos)
            pos += len(b_)
        open(os.path.join(dg, "thumbs.pack"), "wb").write(b"".join(parts_g))
        ig = dict(eix)
        ig.update(offsets=offs_g, lengths=[len(b_) for b_ in parts_g])
        json.dump(ig, open(os.path.join(dg, "index.json"), "w"))
        h, log, _ = viewer(film, size, store_=store_g)
        h["ev"]["file-loaded"]()
        log.clear()
        advance(h, 0.05)
        h["msg"]["thumb"](str((drop + 1) * step), "10", "10")
        orig = pack_b[eix["offsets"][drop + 1]:eix["offsets"][drop + 1] + eix["lengths"][drop + 1]]
        check("with a missing picture earlier in the pack, the next ones are still the right pictures", overlay_bytes(log) == raw_of(orig))
        log.clear()
        advance(h, 0.05)
        h["msg"]["thumb"](str(drop * step), "10", "10")
        o_, l_ = eix["offsets"][drop - 1], eix["lengths"][drop - 1]
        check("the missing picture shows the nearest earlier one", overlay_bytes(log) == raw_of(pack_b[o_:o_ + l_]))

        # a hover while the one run is still going shows nothing and starts nothing; the picture appears when the run ends
        h, log, _ = viewer(film, size)
        h["defer"] = True
        h["ev"]["file-loaded"]()
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        h["msg"]["thumb"]("80", "120", "50")
        check("while the one run is going, hovers start nothing and show nothing", h["started"] == 1 and not [l for l in log if l[1] == "overlay-add"], (h["started"], log))
        finish_one(h)
        advance(h, 0.1)
        adds = [l for l in log if l[1] == "overlay-add"]
        check("when it ends, the picture under the mouse appears where the mouse is", len(adds) == 1 and adds[0][3:5] == ("120", "50"), adds)
        # ... and a film change in the middle drops it
        h, log, _ = viewer(film, size)
        h["defer"] = True
        h["ev"]["file-loaded"]()
        h["msg"]["thumb"]("37", "100", "50")
        h["ev"]["file-loaded"]()
        log.clear()
        while h["queue"]:
            finish_one(h)
        advance(h, 0.1)
        check("a one-run conversion that ends after another film was loaded shows nothing", not [l for l in log if l[1] == "overlay-add"], log)

        # fallbacks: ffmpeg refuses the whole pack -> one picture at a time; a pack too big for bulk_max_mb -> one picture at a time
        fake = os.path.join(tmp, "fake-ffmpeg.sh")
        open(fake, "w").write('#!/bin/sh\ncase "$*" in *image2pipe*) exit 1;; esac\nexec ffmpeg "$@"\n')
        os.chmod(fake, 0o755)
        h, log, _ = viewer(film, size, ffmpeg_cmd=fake)
        h["ev"]["file-loaded"]()
        k0 = h["started"]
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        check("if the one run fails, a hover falls back to converting that picture", h["started"] - k0 == 1 and len([l for l in log if l[1] == "overlay-add"]) == 1, (h["started"] - k0, log))
        h, log, _ = viewer(film, size, extra="no_overlay = false, no_convert = false, bulk_max_mb = 0")
        h["ev"]["file-loaded"]()
        k0 = h["started"]
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        check("a pack over bulk_max_mb is converted one picture at a time", k0 == 0 and h["started"] == 1 and len([l for l in log if l[1] == "overlay-add"]) == 1, (k0, h["started"]))
        h, log, _ = viewer(film, size, bulk=False)
        h["ev"]["file-loaded"]()
        check("bulk=no starts nothing when the film loads", h["started"] == 0, h["started"])

        h, log, _ = viewer(film, size + 1)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("a replaced file (different size) shows nothing", infos and infos[-1]["available"] is False)

        mirror = os.path.join(tmp, "elsewhere", "Movies", "A Film (2020)", "A.Film.2020.mkv")
        h, log, _ = viewer(mirror, size)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("the same film opened through another mount point is matched by path tail and size", infos and infos[-1]["available"] is True)

        h, log, _ = viewer("https://example.com/video.mkv", None)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("a URL gets no thumbnails and no errors", infos and infos[-1]["available"] is False)


        # ---- aliases in the viewer (the stores were made above)
        h, log, _ = viewer(os.path.join(lib2, alias_rel), os.path.getsize(os.path.join(lib2, alias_rel)), library=lib2, store_=store2)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("viewer follows an alias to the other release's thumbnails", infos and infos[-1]["available"] is True and infos[-1]["width"] > 0, log)
        log.clear()
        h["msg"]["thumb"]("25", "10", "10")
        check("an alias shows the real entry's pictures", len([l for l in log if l[1] == "overlay-add"]) == 1)
        h, log, _ = viewer(os.path.join(lib2, alias_rel), os.path.getsize(os.path.join(lib2, alias_rel)) + 1, library=lib2, store_=store2)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("an alias checks its own file size: a replaced file shows nothing", infos and infos[-1]["available"] is False)
        for nm, tgt in (("Vloop.A.2000.mkv", "Vloop.B.2000.mkv"), ("Vloop.B.2000.mkv", "Vloop.A.2000.mkv"), ("Vdangle.2000.mkv", "Not.There.2000.mkv")):
            os.makedirs(st.entry_dir(store2, nm), exist_ok=True)
            with open(os.path.join(st.entry_dir(store2, nm), "index.json"), "w") as f:
                json.dump(dict(v=2, alias_of=tgt, src=nm, size=5, duration=60, target_made="x", made="x"), f)
        for nm in ("Vloop.A.2000.mkv", "Vdangle.2000.mkv"):
            h, log, _ = viewer(os.path.join(lib2, nm), 5, library=lib2, store_=store2)
            h["ev"]["file-loaded"]()
            infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
            check(f"viewer ignores a broken or looping alias ({nm})", infos and infos[-1]["available"] is False)

        # ---- the on-play build
        lib6, store6 = os.path.join(tmp, "lib6"), os.path.join(tmp, "store6s")
        vf = os.path.join(lib6, "View.Me.2023.mkv")
        make_film(vf, seconds=60, size="640x360")
        vsize = os.path.getsize(vf)
        BUILDER = os.path.join(TOOL, "seekthumbs.py")
        h, log, _ = viewer(vf, vsize, library=lib6, store_=store6, builder=BUILDER, min_size=0)
        h["ev"]["file-loaded"]()
        runs = [l for l in log if l[:2] == ("commandv", "run")]
        check("a film without thumbnails starts one background build", len(runs) == 1 and runs[0][2:] == ("python3", BUILDER, "--library", lib6, "--store", store6, "--min-size-mb", "0", "build", vf), runs)
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("until it is built the viewer reports no thumbnails and shows no error", infos and infos[-1]["available"] is False)
        h["ev"]["file-loaded"]()
        check("loading the same film again in the session does not start a second build", len([l for l in log if l[:2] == ("commandv", "run")]) == 1)
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        n_info = len(infos)
        n_killed = len(h["killed"])
        h["timers"][-1]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("the re-check before the build finished changes nothing", len(infos) == n_info)
        env = dict(os.environ, HOME=tmp)
        os.makedirs(os.path.join(tmp, ".config", "seekthumbs"), exist_ok=True)
        with open(os.path.join(tmp, ".config", "seekthumbs", "config.ini"), "w") as f:
            f.write("[seekthumbs]\nmin_size_mb = 0\ninterval = 10\nplay_pause = 0\n" + ("" if HAVE_AV else "play_extractor = auto\n"))
        rr_ = subprocess.run(list(runs[0][2:]), capture_output=True, text=True, env=env)       # the command mpv's `run` would execute
        check("the command the viewer issued really builds the film", rr_.returncode == 0 and st.read_index(st.entry_dir(store6, "View.Me.2023.mkv")), rr_.stdout + rr_.stderr)
        h["timers"][-1]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("when the build is done the next re-check tells the OSC the thumbnails are available", infos and infos[-1]["available"] is True and len(infos) == n_info + 1)
        check("and the re-check timer is stopped", len(h["killed"]) == n_killed + 1, (n_killed, h["killed"]))
        log.clear()
        h["msg"]["thumb"]("33", "10", "10")
        check("hovering then shows the new thumbnail", len([l for l in log if l[1] == "overlay-add"]) == 1)

        vf2 = os.path.join(lib6, "Hover.Me.2023.mkv")
        make_film(vf2, seconds=60, size="320x180")
        h, log, _ = viewer(vf2, os.path.getsize(vf2), library=lib6, store_=store6, builder="", min_size=0)
        h["ev"]["file-loaded"]()
        log.clear()
        h["msg"]["thumb"]("10", "5", "5")           # nothing yet: looks (first look), finds nothing
        c6 = cfg(lib6, store6)
        st.scan_pass(c6)                             # built by somebody else meanwhile
        h["clock"]["t"] += 1
        h["msg"]["thumb"]("10", "5", "5")
        check("hover re-checks are throttled (not every hover)", not [l for l in log if l[1] == "overlay-add"])
        h["clock"]["t"] += 6
        h["msg"]["thumb"]("10", "5", "5")
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("a later hover finds an entry built by somebody else and tells the OSC", infos and infos[-1]["available"] is True and [l for l in log if l[1] == "overlay-add"], log)

        # no build in these cases
        def runs_for(path, size, **kw):
            hh, ll, _ = viewer(path, size, **kw)
            hh["ev"]["file-loaded"]()
            return [l for l in ll if l[:2] == ("commandv", "run")]

        fresh_film = os.path.join(lib6, "Not.Built.2024.mkv")
        check("a URL is never built", not runs_for("https://example.com/Not.Built.2024.mkv", None, library=lib6, store_=store6, builder=BUILDER, min_size=0))
        check("a file outside the library is never built", not runs_for(os.path.join(tmp, "elsewhere", "Not.Built.2024.mkv"), 10 ** 9, library=lib6, store_=store6, builder=BUILDER, min_size=0))
        check("a non-video file is never built", not runs_for(os.path.join(lib6, "disc.iso"), 10 ** 9, library=lib6, store_=store6, builder=BUILDER, min_size=0))
        check("a file under the minimum size is never built", not runs_for(fresh_film, 10 * 10 ** 6, library=lib6, store_=store6, builder=BUILDER, min_size=300))
        check("a big enough film is built", len(runs_for(fresh_film, 4 * 10 ** 9, library=lib6, store_=store6, builder=BUILDER, min_size=300)) == 1)
        check("build=no turns the on-play build off", not runs_for(fresh_film, 4 * 10 ** 9, library=lib6, store_=store6, builder=BUILDER, build=False))
        check("without a builder path nothing is started (install-viewer sets it)", not runs_for(fresh_film, 4 * 10 ** 9, library=lib6, store_=store6, builder=""))

    # --- install-viewer moves a real thumbfast aside and is repeatable
    mc = os.path.join(tmp, "mpvconf")
    os.makedirs(os.path.join(mc, "scripts"))
    with open(os.path.join(mc, "scripts", "thumbfast.lua"), "w") as f:
        f.write("-- the real thumbfast\n")
    st.cmd_install_viewer(mc)
    check("install-viewer keeps the original as .bak", os.path.exists(os.path.join(mc, "scripts", "thumbfast.lua.bak")))
    st.cmd_install_viewer(mc)
    with open(os.path.join(mc, "scripts", "thumbfast.lua.bak")) as f:
        check("a second install-viewer does not overwrite the .bak with ours", f.read().startswith("-- the real thumbfast"))
    # ---- skip_tv / exclude
    c_tv = cfg("/x", "/y", skip_tv=1)
    names_tv = ["Game of Thrones S01-S08 Complete UHD/Game.of.Thrones.S08E05.The.Bells.2160p.mkv", "Show Name s02e10 1080p/Show.Name.s02e10.mkv", "Show.S1E2.mkv"]
    names_film = ["Tron.Ares.2025.2160p/Tron.Ares.2025.2160p.mkv", "IMAX - Wolves.2001.1080p.mkv", "Game of Thrones S01-S08 Complete UHD/extras.mkv", "Rise.of.Skywalker.2019.mkv", "Wes.Anderson.S1ND.mkv"]
    check("skip_tv leaves out names with an SxxExx tag", all(st.excluded(c_tv, n) for n in names_tv), [n for n in names_tv if not st.excluded(c_tv, n)])
    check("skip_tv keeps films (and folders that only say S01-S08)", not any(st.excluded(c_tv, n) for n in names_film), [n for n in names_film if st.excluded(c_tv, n)])
    check("nothing is excluded by default", not any(st.excluded(cfg("/x", "/y"), n) for n in names_tv + names_film))
    c_rx = cfg("/x", "/y", exclude="game.of.thrones|IMAX")
    check("exclude is a case-insensitive regex on the relative path, | for several", st.excluded(c_rx, "IMAX - Wolves.mkv") and st.excluded(c_rx, "GAME OF THRONES/x.mkv") and not st.excluded(c_rx, "Tron.mkv"))
    check("a broken exclude regex excludes nothing instead of crashing", not st.excluded(cfg("/x", "/y", exclude="("), "Tron.mkv"))
    lib7, store7 = os.path.join(tmp, "lib7"), os.path.join(tmp, "store7s")
    ep = os.path.join(lib7, "Show.Name.S01E02.1080p.mkv"); make_film(ep, seconds=30, size="320x180")
    fl = os.path.join(lib7, "Some.Film.2020.mkv"); make_film(fl, seconds=30, size="320x180")
    rc = st.cmd_build(cfg(lib7, store7, skip_tv=1, play_pause=0, play_extractor="auto"), ep, own_process=False)
    check("build FILE skips a TV episode when skip_tv=1", rc == 0 and not os.path.isdir(st.entry_dir(store7, "Show.Name.S01E02.1080p.mkv")))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        st.scan_pass(cfg(lib7, store7, skip_tv=1, settle=0), dry=True)
    check("plan lists the film but not the episode, and says how many were left out", "Some.Film.2020.mkv" in out.getvalue() and "S01E02" not in out.getvalue(), out.getvalue())

    # ---- cold files: wake read, probe timeout, skip-and-retry, no failure recorded
    wf = os.path.join(lib7, "Some.Film.2020.mkv")
    check("wake_file reads a file and returns the seconds it took", isinstance(st.wake_file(wf, 10), float))
    fifo = os.path.join(tmp, "stuck.mkv")
    os.mkfifo(fifo)                                              # opening a FIFO blocks until a writer appears: a file that never answers
    try:
        st.wake_file(fifo, 0.5)
        stuck_raised = False
    except st.ProbeTimeout:
        stuck_raised = True
    wr = os.open(fifo, os.O_WRONLY)                              # release the blocked reader thread
    os.close(wr)
    check("wake_file raises ProbeTimeout for a file that never answers", stuck_raised)
    real_run = st.subprocess.run

    def slow_run(*a, **kw):
        raise subprocess.TimeoutExpired("ffprobe", kw.get("timeout", 0))
    st.subprocess.run = slow_run
    try:
        st.ffprobe(wf, cfg("/x", "/y", probe_timeout=7))
        probe_raised = False
    except st.ProbeTimeout as ex:
        probe_raised = "7 s" in str(ex)
    finally:
        st.subprocess.run = real_run
    check("an ffprobe timeout becomes ProbeTimeout (with the configured limit in the message)", probe_raised)

    lib8, store8 = os.path.join(tmp, "lib8"), os.path.join(tmp, "store8")
    slow_film = os.path.join(lib8, "Slow.Film.2020.mkv"); make_film(slow_film, seconds=30, size="320x180")
    fast_film = os.path.join(lib8, "Fast.Film.2021.mkv"); make_film(fast_film, seconds=40, size="320x180")
    real_probe, calls = st.ffprobe, []

    def flaky_probe(path, c):
        calls.append(os.path.basename(path))
        if path == slow_film and calls.count("Slow.Film.2020.mkv") == 1:
            raise st.ProbeTimeout("simulated cold open")
        return real_probe(path, c)
    st.ffprobe = flaky_probe
    try:
        built = st.scan_pass(cfg(lib8, store8, settle=0, share=0, play_extractor="auto", extractor="auto"))
    finally:
        st.ffprobe = real_probe
    st_state = st.State(store8)
    check("a slow-to-open film is retried at the end of the pass and then built", built == 2 and os.path.isdir(st.entry_dir(store8, "Slow.Film.2020.mkv")), (built, calls))
    check("the retry came after the other film (it was skipped, not waited for)", calls.index("Fast.Film.2021.mkv") < len(calls) - 1 and calls[-1] == "Slow.Film.2020.mkv", calls)
    check("a slow open is not recorded as a failure", not st_state.d["fail"], st_state.d["fail"])

    # ---- progress lines: a start line and periodic counts while a film is read
    lib9, store9 = os.path.join(tmp, "lib9"), os.path.join(tmp, "store9")
    make_film(os.path.join(lib9, "Progress.Film.2022.mkv"), seconds=60, size="320x180")
    out9 = io.StringIO()
    with contextlib.redirect_stderr(out9), contextlib.redirect_stdout(out9):
        st.scan_pass(cfg(lib9, store9, settle=0, share=0, progress_every=0.05, pause=0.3, workers=1))
    check("the scan says which film it is reading and how many thumbnails", "reading 6 thumbnails of Progress.Film.2022.mkv" in out9.getvalue(), out9.getvalue()[-400:])
    check("periodic counts appear while a film is read", "thumbnails read," in out9.getvalue(), out9.getvalue()[-400:])

    # ---- refine: a set built at 30 s is filled in to 10 s by reading only the missing thumbnails
    lib11, store11 = os.path.join(tmp, "lib11"), os.path.join(tmp, "store11")
    f11 = os.path.join(lib11, "Refine.Film.2021.mkv")
    make_film(f11, seconds=180, size="320x180", gop=24)
    rel11 = os.path.relpath(f11, lib11)
    check("a coarse build at 30 s", st.scan_pass(cfg(lib11, store11, interval=30.0, workers=2)) == 1)
    ix_a = st.read_index(st.entry_dir(store11, rel11))
    check("the coarse set has 6 thumbnails at 30 s", ix_a and ix_a["count"] == 6 and ix_a["interval"] == 30.0, ix_a and (ix_a["count"], ix_a["interval"]))
    check("nothing to refine while the interval stays 30 s", st.refine_pass(cfg(lib11, store11, interval=30.0), dry=True) == 0)
    check("a dry run finds the film for 10 s and reads nothing", st.refine_pass(cfg(lib11, store11, interval=10.0), dry=True) == 1)
    out11 = io.StringIO()
    with contextlib.redirect_stderr(out11), contextlib.redirect_stdout(out11):
        done11 = st.refine_pass(cfg(lib11, store11, interval=10.0, workers=2))
    ix_b = st.read_index(st.entry_dir(store11, rel11))
    check("refine reports one film done", done11 == 1, out11.getvalue()[-500:])
    check("the refined set has 18 thumbnails at 10 s, all present", ix_b and ix_b["count"] == 18 and ix_b["interval"] == 10.0 and all(x > 0 for x in ix_b["lengths"]), ix_b and (ix_b["count"], ix_b["interval"], ix_b["lengths"]))
    check("it kept the made stamp (aliases stay valid) and marks the refinement", ix_b and ix_b["made"] == ix_a["made"] and "refined" in ix_b)
    check("only the 12 missing thumbnails were read, 6 kept", "reading 12 thumbnails of Refine.Film.2021.mkv (6 of the 18 already exist at 30 s and are kept)" in out11.getvalue(), out11.getvalue()[-600:])
    check("the pack is consistent and the film is still current", st.entry_current(store11, rel11, os.path.getsize(f11)))
    check("a second refine finds nothing to do", st.refine_pass(cfg(lib11, store11, interval=10.0), dry=True) == 0)
    # a refine to a wider width reads everything again instead of stretching narrow thumbnails
    lib13, store13 = os.path.join(tmp, "lib13"), os.path.join(tmp, "store13")
    f13 = os.path.join(lib13, "Narrow.Film.2021.mkv")
    make_film(f13, seconds=180, size="640x360", gop=24)
    rel13 = os.path.relpath(f13, lib13)
    st.scan_pass(cfg(lib13, store13, interval=30.0, width=200, workers=2))
    ix13a = st.read_index(st.entry_dir(store13, rel13))
    out13 = io.StringIO()
    with contextlib.redirect_stdout(out13), contextlib.redirect_stderr(out13):
        st.refine_pass(cfg(lib13, store13, interval=10.0, width=300, workers=2))
    ix13b = st.read_index(st.entry_dir(store13, rel13))
    check("a refine to a wider width reads all 18 thumbnails and ends at the new width", ix13a and ix13b and ix13a["w"] == 200 and ix13b["w"] == 300 and ix13b["count"] == 18 and "(0 of the 18 already exist" in out13.getvalue(), (ix13a and ix13a["w"], ix13b and ix13b["w"], out13.getvalue()[-300:]))
    # a failing refine keeps the coarse set
    lib12, store12 = os.path.join(tmp, "lib12"), os.path.join(tmp, "store12")
    f12 = os.path.join(lib12, "Keep.Film.2021.mkv")
    make_film(f12, seconds=180, size="320x180", gop=24)
    st.scan_pass(cfg(lib12, store12, interval=30.0, workers=2))
    ix_k = st.read_index(st.entry_dir(store12, os.path.relpath(f12, lib12)))
    real_ext = st.make_extractor
    def broken_extractor(*a, **k):
        raise RuntimeError("simulated read failure")
    st.make_extractor = broken_extractor
    try:
        out12 = io.StringIO()
        with contextlib.redirect_stderr(out12), contextlib.redirect_stdout(out12):
            st.refine_pass(cfg(lib12, store12, interval=10.0, workers=2))
    finally:
        st.make_extractor = real_ext
    ix_k2 = st.read_index(st.entry_dir(store12, os.path.relpath(f12, lib12)))
    check("a refine that fails keeps the coarse set unchanged", ix_k2 == ix_k and "REFINE FAILED" in out12.getvalue() and "is kept" in out12.getvalue(), out12.getvalue()[-400:])

    # ---- a failed read drops the open decoder; that must give its memory back (it used to leave ~20 MB each on a 4K HDR film)
    lib13 = os.path.join(tmp, "lib13")
    f13 = os.path.join(lib13, "Drop.Film.2021.mkv")
    make_film(f13, seconds=12, size="3840x2160", gop=24)
    ext13 = st.AvExtractor(f13, st.vf_chain(dict(transfer="bt709"), 240, False), dict(st.DEFAULTS))
    out13 = os.path.join(tmp, "drop.ppm")
    check("a read works before the drop test", ext13.grab(3.0, out13)[0])
    base13 = st.self_rss_mb()
    for k13 in range(40):
        ext13._drop()
        ext13.grab(3.0 + (k13 % 5), out13)
    grown13 = st.self_rss_mb() - base13
    check(f"40 drop-and-reopen cycles leave no memory behind ({grown13:.0f} MB; it was 52 MB before the fix)", grown13 < 25, grown13)
    check("the closed containers are not kept in the open list", len(ext13.opened) <= 1, len(ext13.opened))
    ext13.close()

    # ---- reading over HTTP (read_url_base): range requests to a local server; a dead address falls back to the mount path
    import http.server
    import socketserver
    lib14, store14 = os.path.join(tmp, "lib14"), os.path.join(tmp, "store14")
    f14 = os.path.join(lib14, "Http.Film.2021.mkv")
    make_film(f14, seconds=120, size="320x180", gop=24)
    hits14 = []

    class RangeHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _serve(self, head):
            path = os.path.join(lib14, os.path.basename(self.path.split("?")[0]))
            if not os.path.isfile(path):
                self.send_error(404)
                return
            size = os.path.getsize(path)
            rng = self.headers.get("Range")
            lo, hi = 0, size - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                lo = int(a) if a else max(0, size - int(b))
                hi = min(size - 1, int(b)) if (a and b) else size - 1
            hits14.append((self.command, rng))
            self.send_response(206 if rng else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(hi - lo + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
            self.end_headers()
            if not head:
                with open(path, "rb") as fh:
                    fh.seek(lo)
                    try:
                        self.wfile.write(fh.read(hi - lo + 1))
                    except OSError:
                        pass

        def do_GET(self):
            self._serve(False)

        def do_HEAD(self):
            self._serve(True)

    class QuietServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv14 = QuietServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=srv14.serve_forever, daemon=True).start()
    port14 = srv14.server_address[1]
    out14 = io.StringIO()
    with contextlib.redirect_stderr(out14), contextlib.redirect_stdout(out14):
        n14 = st.scan_pass(cfg(lib14, store14, interval=10.0, workers=2, read_url_base=f"http://u:secret@127.0.0.1:{port14}/"))
    ix14 = st.read_index(st.entry_dir(store14, "Http.Film.2021.mkv"))
    check("a film is built by reading over HTTP", n14 == 1 and ix14 and ix14["count"] == 12 and all(x > 0 for x in ix14["lengths"]), out14.getvalue()[-500:])
    check("the server saw range requests, and the log says HTTP", any(r for _, r in hits14) and "over HTTP" in out14.getvalue(), (len(hits14), out14.getvalue()[-300:]))
    check("the built line reports the network used per thumbnail", "MB per thumbnail" in out14.getvalue() or st.net_rx_bytes() is None, out14.getvalue()[-400:])
    check("the password is not written to the log", "secret" not in out14.getvalue() and st.mask_url("http://u:p@h/x") == "http://h/x" and st.mask_url("http://h/x") == "http://h/x")
    check("the address is the base plus the film's path, percent-encoded", st.read_source({"read_url_base": "http://h:1/w/"}, "A B/c+d.mkv", "/m/A B/c+d.mkv") == "http://h:1/w/A%20B/c%2Bd.mkv")
    check("without read_url_base the mount path is used", st.read_source({}, "a/b.mkv", "/m/a/b.mkv") == "/m/a/b.mkv")
    srv14.shutdown()
    srv14.server_close()
    # ---- bounded range reads: no request asks for more than one chunk; the open-ended default asks for the rest of the file
    f16 = os.path.join(lib14, "Bounded.mkv")                       # the range server above serves lib14
    make_film(f16, seconds=120, size="640x360", gop=24)
    sz16 = os.path.getsize(f16)
    asked16 = []

    class H16(RangeHandler):
        def _serve(self, head):
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                asked16.append((min(sz16 - 1, int(b)) if b else sz16 - 1) - int(a or 0) + 1)
            super()._serve(head)

    srv16 = QuietServer(("127.0.0.1", 0), H16)
    threading.Thread(target=srv16.serve_forever, daemon=True).start()
    u16 = f"http://u:secret@127.0.0.1:{srv16.server_address[1]}/Bounded.mkv"
    vf16 = st.vf_chain(dict(duration=120, width=640, height=360, transfer=""), 200, False)
    res16 = {}
    for kb in (256, 0):
        asked16.clear()
        ex16 = st.AvExtractor(u16, vf16, dict(st.DEFAULTS, http_chunk_kb=kb, seek_timeout=30.0))
        oks = [ex16.grab(t, os.path.join(tmp, f"b16_{kb}_{t}.ppm"))[0] for t in (5, 35, 65, 95)]
        ex16.close()
        res16[kb] = (all(oks), max(asked16 or [0]), sum(asked16))
    check("bounded reading decodes thumbnails over HTTP, with Basic auth taken from user:password@", res16[256][0], res16)
    check("bounded reading: no single request asks for more than one chunk", 0 < res16[256][1] <= 256 * 1024, res16)
    check("bounded reading asks for far less than the open-ended default", res16[256][2] * 2 < res16[0][2], res16)
    rf16 = st.RangeFile(u16, chunk=65536)
    rf16.seek(100)
    a16 = rf16.read(70000)
    with open(f16, "rb") as fh16:
        fh16.seek(100)
        same16 = fh16.read(70000)
    check("RangeFile returns the same bytes as the file, across chunk borders", a16 == same16 and rf16.seek(0, 2) == sz16 and rf16.seek(0, 0x10000) == sz16, (len(a16), rf16.size))
    srv16.shutdown()
    srv16.server_close()

    # ---- a source that keeps answering with a server error: the read stops after server_error_stop failures in a row (not after an hour of back-off)
    gv = st.Governor(4, cooldown=0.01)
    check("a success between server errors resets the count", not gv.server_error("HTTP Error 503 Service Unavailable", 3) and not gv.server_error("HTTP Error 503", 3)
          and (gv.release("ok") or True) and not gv.server_error("HTTP Error 503", 3))
    gv = st.Governor(4, cooldown=0.01)
    r_ = [gv.server_error("Server returned 5XX Server Error reply", 3) for _ in range(3)]
    check("three server errors in a row with a limit of 3 stop; other failures do not count", r_ == [False, False, True] and not st.Governor(2).server_error("no frame decoded", 1))
    check("the provider words are recognised", all(st.PROVIDER_RE.search(x) for x in ("HTTP Error 503 Service Unavailable", "bytes_limit_reached", "Server returned 5XX Server Error reply")))

    # ---- the daily read budget: counted per day (midnight in budget_tz), pauses the queue, rolls over
    from datetime import datetime as _dt, timezone as _tz
    store17 = os.path.join(tmp, "store17")
    os.makedirs(store17)
    bc = dict(store=store17, daily_budget_gb=1.0, budget_tz="Europe/Berlin")
    t_a = _dt(2026, 10, 28, 20, 0, tzinfo=_tz.utc).timestamp()           # 21:00 CET on the 28th
    t_b = _dt(2026, 10, 28, 23, 30, tzinfo=_tz.utc).timestamp()          # 00:30 CET on the 29th
    d_a, end_a = st.budget_day(bc, t_a)
    d_b, _ = st.budget_day(bc, t_b)
    check("the budget day ends at midnight in budget_tz (23:00 UTC in late October)", d_a == "2026-10-28" and d_b == "2026-10-29" and end_a == _dt(2026, 10, 28, 23, 0, tzinfo=_tz.utc).timestamp(), (d_a, d_b, end_a))
    st.spend_add(bc, 600_000_000, t_a)
    st.spend_add(bc, 300_000_000, t_a)
    check("bytes are added up within a day and the gate stays open below the limit", st.spend_load(bc, t_a) == 900_000_000 and not st.budget_gate(bc, t_a)[0])
    st.spend_add(bc, 200_000_000, t_a)
    blk, until_a, used_a = st.budget_gate(bc, t_a)
    check("at the limit the gate closes until the day ends", blk and until_a == end_a and used_a == 1_100_000_000)
    check("the next day starts again at 0 and the gate opens", st.spend_load(bc, t_b) == 0 and not st.budget_gate(bc, t_b)[0])
    check("a budget of 0 turns the gate off", not st.budget_gate(dict(bc, daily_budget_gb=0), t_a)[0])
    check("an unknown zone name falls back instead of failing", st.budget_day(dict(bc, budget_tz="No/Such_Zone"), t_a)[0] == "2026-10-28")

    # a scan with a tiny budget: the first film is built (its bytes counted), then the queue pauses and the rest is kept, not failed
    lib18, store18 = os.path.join(tmp, "lib18"), os.path.join(tmp, "store18")
    for nm in ("Budget.One.2021.mkv", "Budget.Two.2021.mkv"):
        make_film(os.path.join(lib18, nm), seconds=60, size="320x180", gop=24)
    rx_fake = [0]

    def fake_rx():
        rx_fake[0] += 2_000_000_000                                   # every call: 2 GB more received
        return rx_fake[0]
    real_rx = st.net_rx_bytes
    st.net_rx_bytes = fake_rx
    out18 = io.StringIO()
    try:
        with contextlib.redirect_stderr(out18), contextlib.redirect_stdout(out18):
            n18 = st.scan_pass(cfg(lib18, store18, interval=10.0, workers=1, daily_budget_gb=1.0))
    finally:
        st.net_rx_bytes = real_rx
    st18 = st.State(store18)
    built18 = [r for r in ("Budget.One.2021.mkv", "Budget.Two.2021.mkv") if st.entry_current(store18, r, os.path.getsize(os.path.join(lib18, r)))]
    check("a scan builds one film, then stops at the daily budget with a clear log line", n18 == 1 and len(built18) == 1 and "daily read budget reached" in out18.getvalue(), (n18, built18, out18.getvalue()[-500:]))
    check("the pause is held by the breaker and nothing is recorded as failed", st18.d["breaker_until"] > time.time() and st18.d.get("budget_until", 0) >= st18.d["breaker_until"] - 1 and not st18.d["fail"], st18.d)

    lib15, store15 = os.path.join(tmp, "lib15"), os.path.join(tmp, "store15")
    make_film(os.path.join(lib15, "Fall.Back.2021.mkv"), seconds=60, size="320x180", gop=24)
    out15 = io.StringIO()
    with contextlib.redirect_stderr(out15), contextlib.redirect_stdout(out15):
        n15 = st.scan_pass(cfg(lib15, store15, interval=10.0, workers=2, read_url_base=f"http://127.0.0.1:{port14}/"))     # nothing listens there any more
    check("a dead HTTP address falls back to the mount path and still builds the film", n15 == 1 and "using the mount path" in out15.getvalue() and st.entry_current(store15, "Fall.Back.2021.mkv", os.path.getsize(os.path.join(lib15, "Fall.Back.2021.mkv"))), out15.getvalue()[-500:])

    # ---- memory guard: the number of parallel reads follows the free memory
    real_mem, real_cg = st.mem_available_mb, st.cgroup_headroom_mb
    st.cgroup_headroom_mb = lambda: None
    try:
        mc = {"worker_mem_mb": 600, "mem_reserve_mb": 3000}
        st.mem_available_mb = lambda: 7500.0
        n7, note7 = st.memory_limited_workers(16, mc)
        check("16 reads asked for with 7.5 GB free: 7 used, and the note says why", n7 == 7 and "16 parallel reads asked for, 7 used" in note7 and "7.3 GB" in note7, (n7, note7))
        st.mem_available_mb = lambda: 20000.0
        check("plenty of memory: the request is kept, no note", st.memory_limited_workers(16, mc) == (16, ""))
        st.mem_available_mb = lambda: 1000.0
        check("almost no memory: never fewer than one read", st.memory_limited_workers(16, mc)[0] == 1)
        st.mem_available_mb = lambda: None
        check("memory unknown: the request is kept", st.memory_limited_workers(16, mc) == (16, ""))
        st.mem_available_mb = lambda: 1000.0
        check("worker_mem_mb = 0 turns the limit off", st.memory_limited_workers(16, dict(mc, worker_mem_mb=0)) == (16, ""))
        st.mem_available_mb = lambda: 3700.0              # (3700 - 3000) / 600 = 1 read
        out10 = io.StringIO()
        lib10, store10 = os.path.join(tmp, "lib10"), os.path.join(tmp, "store10")
        make_film(os.path.join(lib10, "Memory.Film.2022.mkv"), seconds=60, size="320x180")
        with contextlib.redirect_stderr(out10), contextlib.redirect_stdout(out10):
            st.scan_pass(cfg(lib10, store10, settle=0, share=0, workers=8, pause=0.0))
        check("a build logs the memory limit and reads with the reduced number", "MEMORY: 8 parallel reads asked for, 1 used" in out10.getvalue() and "with 1 parallel read(s)" in out10.getvalue(), out10.getvalue()[-500:])
        # a cgroup limit (systemd-run -p MemoryMax=5G): headroom, not free memory, decides
        st.mem_available_mb = lambda: 12000.0
        st.cgroup_headroom_mb = lambda: (2500.0, 5120.0)    # (2500 - 300) / 600 = 3 reads
        n3, note3 = st.memory_limited_workers(8, mc)
        check("under a 5 GB cgroup limit with 2.5 GB left: 3 reads, and the note names the limit", n3 == 3 and "under this service's 5.0 GB memory limit" in note3, (n3, note3))
        st.cgroup_headroom_mb = lambda: (-100.0, 5120.0)
        check("a limit already exceeded: still one read", st.memory_limited_workers(8, mc)[0] == 1)
    finally:
        st.mem_available_mb, st.cgroup_headroom_mb = real_mem, real_cg
    # the real reader, on a fake cgroup tree: tightest memory.max on the path wins, page cache is not counted
    cgr = os.path.join(tmp, "cgroot")
    os.makedirs(os.path.join(cgr, "user.slice", "app.service"))
    for rel, mx, anon in (("user.slice", "max", 0), ("user.slice/app.service", str(5 * 1024 ** 3), 2 * 1024 ** 3)):
        with open(os.path.join(cgr, rel, "memory.max"), "w") as f:
            f.write(mx + "\n")
        with open(os.path.join(cgr, rel, "memory.stat"), "w") as f:
            f.write(f"anon {anon}\nfile {9 * 1024 ** 3}\n")
    with open(os.path.join(tmp, "proc-cgroup"), "w") as f:
        f.write("0::/user.slice/app.service\n")
    os.environ["SEEKTHUMBS_CGROUP_ROOT"], os.environ["SEEKTHUMBS_PROC_CGROUP"] = cgr, os.path.join(tmp, "proc-cgroup")
    try:
        hr = st.cgroup_headroom_mb()
        check("cgroup headroom = limit minus anonymous memory (3 GB left of 5; cache ignored)", hr and abs(hr[0] - 3072) < 1 and abs(hr[1] - 5120) < 1, hr)
        os.remove(os.path.join(cgr, "user.slice", "app.service", "memory.max"))
        check("no limit anywhere on the path: None", st.cgroup_headroom_mb() is None)
    finally:
        del os.environ["SEEKTHUMBS_CGROUP_ROOT"], os.environ["SEEKTHUMBS_PROC_CGROUP"]
    ok_adj = st.prefer_to_be_killed_first(300)
    if ok_adj:
        with open("/proc/self/oom_score_adj") as f:
            check("the process asks to be the first the kernel kills when memory runs out", int(f.read()) >= 300)
    else:
        print("SKIP oom_score_adj (not writable here)")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("FAILED" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
