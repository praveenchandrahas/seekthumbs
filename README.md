# seekthumbs: seek-bar thumbnails for an rclone library

Netflix-style previews on the mpv seek bar (called *trickplay* or *seek thumbnails*), for a film library on a **read-only rclone mount**.
The thumbnails are made in the background and kept in a **local folder**; the library is only ever read.

State (2026-10-25): generator (parallel, adaptive), watcher, mpv viewer and tests are written and pass on synthetic films. **Not yet run on the real library or
inside real mpv/uosc** (the sandbox has neither). Open items are listed at the end.

## Setup (once the paths are known)

```bash
sudo apt install python3-av                      # recommended: opens each film once instead of once per thumbnail (see "Speed"); the pip wheel lacks zscale (HDR)
mkdir -p ~/.config/seekthumbs
cat > ~/.config/seekthumbs/config.ini <<'INI'
[seekthumbs]
library = /path/to/the/rclone/mount/Movies
store   = /path/to/local/thumbnail/folder
INI
cd seekthumbs      # this repository
./seekthumbs.py probe "/path/to/one/film.mkv"     # seconds and MB received per thumbnail for each extractor, and the projected time for 100 such films
./seekthumbs.py plan                              # lists what would be built (directory listings only)
./seekthumbs.py scan --max-films 1                # build one film, look at the result
./seekthumbs.py scan                              # the whole library, as fast as the mount allows (run it overnight; Ctrl+C is safe, it resumes)
./seekthumbs.py status                            # size per 2 h against the 10 MB budget
./seekthumbs.py install-service --enable          # afterwards: the background watcher picks up new films (systemd user service)
./seekthumbs.py install-viewer                    # the mpv side, into ~/.config/mpv-object-audio/scripts (see below)
```

## How it works

- **Extraction:** one `ffprobe` per film, then one `ffmpeg` per thumbnail: `-ss <t> -i FILE -noaccurate_seek -frames:v 1`, so ffmpeg jumps to the
  nearest earlier keyframe and decodes only that frame. Default every **20 s** (360 per 2 h). Thumbnail *i* is the keyframe at or before `(i + 0.5) * interval`.
  HDR sources are tone-mapped to SDR with zscale/tonemap (untested on a real HDR file; Dolby Vision profile 5 will probably look wrong).
- **Storage:** `STORE/<path relative to the library>.seekthumbs/thumbs.pack` (the JPEGs back to back) and `index.json` (offsets, lengths, file size, interval,
  width/height, quality). Budget: at most `budget_mb` (10) per 2 h of film, aimed at 80 %; quality, then width, are lowered until it fits.
  `verify` flags anything over budget.
- **New films:** `watch` loops: list the library (directory listings only), queue films with no entry or whose **size changed**, newest first, build at most
  3 per pass, sleep `poll` (30 min). Files under 300 MB are ignored; files modified in the last 10 min are skipped.
  If the library folder is empty or missing the pass does nothing (a dead mount must not look like "all films deleted"). `prune` is manual (`--yes` to delete).
- **Speed first, brakes only when needed:** by default 6 reads run in parallel with no pauses (`workers`, `pause=0`, `film_pause=0`, `max_films=0` = all).
  A **governor** watches every read. If one fails with a rate-limit signal (text containing 429, 403, 503, "too many requests", "rate limit", "quota",
  "slow down", or a timeout), it halves the number of parallel reads and stops all new reads for a cool-down: 30 s, then 60, 120 ... up to 15 min. Further signals
  during the same cool-down count once. After 40 clean reads it raises the parallelism by one, back up to `workers`. If the mount still says "slow down" after
  8 cool-downs in a row at a single read at a time (about an hour), the pass stops and all work pauses for `breaker_hours`; the film is *not* marked bad.
  Other read failures (corrupt region, not a rate limit) are retried once; a film is abandoned if 6 reads fail and more fail than succeed, and is retried after
  6 h (doubling to 7 days). Three films in a row abandoned this way also pause everything for an hour. Still in force: `nice 19` and idle I/O class, a lock file
  against two instances, waiting (5 min steps) while mpv/vlc/kodi is playing, and one film at a time so the reads stay in one region of the mount.
- **Speed:** two extractors. `ffmpeg` runs one process per thumbnail (re-opens the film every time). `pyav` (PyAV) lets each worker open the film **once** and then
  only seek: it avoids the repeated open, probe and index reads, which on a network mount are the bulk of the round trips. `extractor=auto` (default) uses PyAV when it
  is installed and its filter chain builds, and falls back to the ffmpeg command otherwise (HDR films with a PyAV that lacks `zscale`, or a PyAV that fails 3 times
  before its first success). HDR frames are scaled to thumbnail size *before* tone-mapping, which makes the tone-map almost free. The JPEG encoding at the end runs in
  parallel. Measured on a local 30 min 1080p test file with 2 cores (no network, so no round-trip cost at all): ffmpeg command 2.8 reads/s, PyAV 100+ reads/s;
  a 4K 10-bit HDR thumbnail through the ffmpeg command took 0.7 s (software decode). **These are not numbers for your mount.**
- **How long for 100 films:** 100 films of 2 h = 36,000 thumbnails. One night (8 h) needs 1.3 reads/s. Rough guess for a network mount: 0.3-1 s per read per
  worker with PyAV, 1.5-3 s with the ffmpeg command, times 6 workers: about 1-2 h with PyAV, 3-5 h with the command, if the mount tolerates 6 parallel streams.
  `probe` prints the projection from your own measurement; trust it over this paragraph.
- **Linking the playing film to its thumbnails (mpv):** by path. `thumbfast.lua` takes mpv's `path`, removes the `library` prefix and looks for
  `<rest>.seekthumbs` in the store. The size recorded in the index must equal the playing file's `file-size`, so a replaced film never shows the old pictures.
  If the film is opened through another mount point, shorter and shorter tails of the path are tried (size check decides). Streams (URLs) get nothing.
- **Showing them:** uosc and the stock OSC variants call a script named **thumbfast** (`script-message-to thumbfast thumb <t> <x> <y>` / `clear`) and read a
  `thumbfast-info` message for the size. Stock thumbfast opens the film in a second mpv, which means more cloud reads per hover. `thumbfast.lua` here answers the same
  messages from the local store instead: it slices one JPEG out of the pack, converts it to raw BGRA with ffmpeg (a few ms, cached for the session in
  `$XDG_RUNTIME_DIR`), and draws it with `overlay-add` (id 42, the id thumbfast uses). It therefore **replaces** thumbfast: `install-viewer` moves a real
  `thumbfast.lua` aside as `.bak`. Films without an entry show no thumbnail (they do not fall back to the real thumbfast).

## Settings (`config.ini` `[seekthumbs]` or `--option`)

`library`, `store`, `interval` (20), `width` (240), `workers` (6), `extractor` (auto | pyav | ffmpeg), `rate_cooldown` (30 s), `pause` (0), `film_pause` (0),
`poll` (1800), `settle` (600), `min_size_mb` (300), `max_films` (0 = all), `budget_mb` (10), `safety` (0.8), `seek_timeout` (90 s), `yield_to_playback` (1),
`breaker_hours` (1).
The viewer reads `library` and `store` from the same file; `script-opts/seekthumbs.conf` can override them (also `overlay_id`, `ffmpeg`, `debug=yes`).

## Not verified yet / known weak spots

1. **Bytes read per seek on the real mount.** rclone read-ahead, chunk size and VFS cache mode decide it. `probe` now prints the bytes this machine received per
   thumbnail (all interfaces, so quiet the machine first). If it is large, a *separate* read-only mount for the overnight run with a small read-ahead
   (`--buffer-size 0 --vfs-read-chunk-size 1M --vfs-cache-mode off` as a starting point; check the rclone docs for your version) would cut the bytes; `preflight` (sdht repository) prints
   your current mount flags.
2. **Rate-limit detection is by error text and timeouts.** ffmpeg and PyAV pass the HTTP error through only sometimes; a backend that throttles by silently slowing
   reads is caught only by the 90 s timeout. A hung read in a FUSE mount can block a PyAV worker thread (the ffmpeg command is killed by its timeout).
3. **The settle check** uses the file's mtime, which on some rclone backends is the original date, not the upload time. A half-uploaded file is caught by
   ffprobe/read failures and the backoff instead.
4. **The viewer has been run only under a mocked mpv API** (lupa), not in real mpv with uosc. The thumbfast message names are from the thumbfast/uosc sources as I know
   them and are consistent with what the WebDAV-browser script sends (`script-message-to thumbfast clear`, overlay id 42), but are not checked against his uosc version.
   the `preflight` script of the [sdht](https://github.com/praveenchandrahas/Software-Defined-Home-Theatre) repository lists the mpv `scripts/` and `script-opts/` folders and any uosc/thumbfast versions.
5. Keyframe-only thumbnails can be a few seconds earlier than the hovered time (fine for scrubbing at 20 s).
6. Dolby Vision profile 5, and HDR through PyAV on a real HDR file, are untested; the circuit breaker across films and `install-service` are not covered by tests.
7. 6 parallel streams may exceed what your backend allows (some debrid/cloud services limit concurrent streams per link or per IP). If `probe`/the first film show
   throttling, lower `--workers`.

## Tests

`python3 tests/test_seekthumbs.py` (needs ffmpeg; `pip install lupa av` adds the viewer and PyAV tests).

## Adapting it to your system

Examples use the author's mount style (an rclone mount, read-only) and paths; set `library` and `store` in `~/.config/seekthumbs/config.ini` to your own.

## Licence

[Apache License 2.0](LICENSE) (see also [NOTICE](NOTICE)). Dolby, Dolby Atmos, DTS and DTS:X are trademarks of their owners; this project is not affiliated with or endorsed by them. It contains no media files and no decoder code.

Part of the [software-defined home theatre](https://github.com/praveenchandrahas/Software-Defined-Home-Theatre) project.
