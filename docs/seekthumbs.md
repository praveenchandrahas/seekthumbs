# Seek-bar thumbnails for the rclone library (`tools/seekthumbs`)

Netflix-style previews on the mpv seek bar (called *trickplay* or *seek thumbnails*), for a film library on a **read-only rclone mount**.
The thumbnails are made in the background and kept in a **local folder**; the library is only ever read.

State (2026-10-25): generator, watcher, mpv viewer and tests are written and pass on synthetic films. **Not yet run on the real library or
inside real mpv/uosc** (the sandbox has neither). Open items are listed at the end.

## Setup (once the paths are known)

```bash
mkdir -p ~/.config/seekthumbs
cat > ~/.config/seekthumbs/config.ini <<'INI'
[seekthumbs]
library = /path/to/the/rclone/mount/Movies
store   = /path/to/local/thumbnail/folder
INI
cd ~/SDHT/tools/seekthumbs
./seekthumbs.py probe "/path/to/one/film.mkv"     # times 3 seeks; writes nothing. Measure the network counter around it.
./seekthumbs.py plan                              # lists what would be built (directory listings only)
./seekthumbs.py scan --max-films 1                # build one film, look at the result
./seekthumbs.py status                            # size per 2 h against the 10 MB budget
./seekthumbs.py install-service --enable          # the background watcher (systemd user service)
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
- **Being gentle to the cloud API:** one film and one ffmpeg at a time; 1.5 s between seeks, 60 s between films; `nice 19`, idle I/O class, 50 % CPU quota;
  waits (5 min steps) while mpv/vlc/kodi is running; each failed read is retried once; 3 failed reads in a row fail that film (retry after 6 h, doubling to 7 days)
  **and** stop all work for an hour (circuit breaker). A lock file stops two instances. Expect roughly 30-60 min per film, so a big backlog takes days.
- **Linking the playing film to its thumbnails (mpv):** by path. `thumbfast.lua` takes mpv's `path`, removes the `library` prefix and looks for
  `<rest>.seekthumbs` in the store. The size recorded in the index must equal the playing file's `file-size`, so a replaced film never shows the old pictures.
  If the film is opened through another mount point, shorter and shorter tails of the path are tried (size check decides). Streams (URLs) get nothing.
- **Showing them:** uosc and the stock OSC variants call a script named **thumbfast** (`script-message-to thumbfast thumb <t> <x> <y>` / `clear`) and read a
  `thumbfast-info` message for the size. Stock thumbfast opens the film in a second mpv, which means more cloud reads per hover. `thumbfast.lua` here answers the same
  messages from the local store instead: it slices one JPEG out of the pack, converts it to raw BGRA with ffmpeg (a few ms, cached for the session in
  `$XDG_RUNTIME_DIR`), and draws it with `overlay-add` (id 42, the id thumbfast uses). It therefore **replaces** thumbfast: `install-viewer` moves a real
  `thumbfast.lua` aside as `.bak`. Films without an entry show no thumbnail (they do not fall back to the real thumbfast).

## Settings (`config.ini` `[seekthumbs]` or `--option`)

`library`, `store`, `interval` (20), `width` (240), `pause` (1.5 s between seeks), `film_pause` (60), `poll` (1800), `settle` (600), `min_size_mb` (300),
`max_films` (3 per pass), `budget_mb` (10), `safety` (0.8), `seek_timeout` (90 s), `yield_to_playback` (1), `breaker_hours` (1).
The viewer reads `library` and `store` from the same file; `script-opts/seekthumbs.conf` can override them (also `overlay_id`, `ffmpeg`, `debug=yes`).

## Not verified yet / known weak spots

1. **Bytes read per seek on the real mount.** rclone read-ahead, chunk size and VFS cache mode decide it; the tool does not count bytes. Run `probe` with the
   network counter (or rclone's stats) around it, then tune `interval`/`pause`. `preflight` now prints the rclone mount command line and flags.
2. **Throttling that only slows reads** is caught by the 90 s per-seek timeout, not by an explicit 429 check (ffmpeg's error text is opaque).
3. **The settle check** uses the file's mtime, which on some rclone backends is the original date, not the upload time. A half-uploaded file is caught by
   ffprobe/read failures and the backoff instead.
4. **The viewer has been run only under a mocked mpv API** (lupa), not in real mpv with uosc. The thumbfast message names are from the thumbfast/uosc sources as I know
   them and are consistent with what the WebDAV-browser script sends (`script-message-to thumbfast clear`, overlay id 42), but are not checked against his uosc version.
   `preflight` now lists the mpv `scripts/` and `script-opts/` folders and any uosc/thumbfast versions.
5. Keyframe-only thumbnails can be a few seconds earlier than the hovered time (fine for scrubbing at 20 s).
6. HDR / Dolby Vision tone-mapping, the circuit breaker and `install-service` are not covered by tests.
