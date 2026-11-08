# Changelog

## [Unreleased]

- A failed HTTP probe now records what the server really answered: one 1-byte range request to the same address (a redirect is reported, not followed), and its status, `Retry-After`/`Server` headers and the start of the body go to the log and to the `http` field of `.http-errors.jsonl`. ffprobe's own message says only "5XX".
- A 429, 502, 503 or 504 on the probe is retried after 1 h, doubling to at most 12 h (it was 6 h to 7 days for every failure); other failures keep the old schedule.
- `RangeFile` no longer sends the WebDAV password to the host a redirect leads to (urllib keeps the Authorization header across hosts); it is dropped when the host changes. Tested with two local servers.
- Second batch on the owner's library: 25 films, 9,971 thumbnails, about 2 minutes per film; in 10 films read closely 94 of 4,150 thumbnails were lost to bursts of connection resets (the ffmpeg fallback failed too); the WebDAV probe returned 503 for 11 of 28 releases (details in the README). The throughput ceiling is not being pursued.

- With `read_url_base`, a film whose HTTP probe fails is no longer read through the mount. The full cause goes to `STORE/.http-errors.jsonl` and the log, and the film is recorded as failed and retried after the usual back-off.
- Batch result: first batch on the owner's library: 14 films (2160p HEVC with DV/HDR10/HDR10+ and 1080p AVC remuxes), 5,566 thumbnails, all by the flush, 0 failed reads, 1.7 MB per thumbnail on average (worst 5.8 MB), 11.0 GB received in total, 88 to 315 s per film; 13 other releases aliased in seconds; 4 releases of 3 films failed the HTTP probe with a 5XX within 1 to 2 s (cause not yet known); 16 parallel reads gave only about 17 % more reads per second than 10 on the 4K films (2.9 to 3.4), so a ceiling other than the worker count is in play (details in the seekthumbs README).

- `keyframe_flush` (default on) and `keyframe_flush_check`: PyAV decodes the seek keyframe's own packet and drains the decoder instead of demuxing on until a frame comes out, which on a test film cut the data read per thumbnail from 8.5 MB to 1.3 MB with identical thumbnails. The built line now reports how frames were obtained (`frames by way: ...`) and, with the check on, whether they matched the old way. Measured on a 1080p H.264 AVC remux: 5.7 to 1.4 MB per thumbnail on average (3.1 MB worst), 60 of 60 frame timestamps equal to the old way. On a 4K HEVC Dolby Vision remux with the flush on: 1.6 MB average, 2.1 MB worst, 10 of 10 equal; owner spot-check 2026-11-08: the thumbnails of one 2160p Dolby Vision HYBRID HEVC remux built with the flush looked correct (visual, one film); with the flush off the same film read 10.5 MB average and 16.8 MB worst per thumbnail (23.5 packets per grab, read phase 140 s against 79 s).

- Calibrated, index-free thumbnail reads for films with no Cues (seek index) element at all, which otherwise forced an expensive scattered search for every seek (`noindex.py`; new `no_index` setting, default on). See the README's "Calibrated seeking for files with no Cues at all" entry for the design and what has and hasn't been validated yet.

## [0.1.0] - 2026-11-01

First packaged pre-release. Experimental. The code on main at the time of tagging is packaged as it is.

- Debian package `seekthumbs` (Architecture: all): the thumbnail builder, its local web page (127.0.0.1 only) and the mpv viewer script, installed under /usr/lib/seekthumbs with the `seekthumbs` command in /usr/bin and a user systemd unit for the background watcher in /usr/lib/systemd/user.
- `seekthumbs-setup` installs the mpv viewer (never the on-play build unless `--on-play`) and can enable the watcher, with `--dry-run`, `uninstall` and a check for hand-installed copies that would hide the packaged command.
- The package scripts never enable or start a service, never write into a home folder and never touch audio configuration. The packaged unit does not start without ~/.config/seekthumbs/config.ini.
