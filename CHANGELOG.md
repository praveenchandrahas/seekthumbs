# Changelog

## [Unreleased]

- With `read_url_base`, a film whose HTTP probe fails is no longer read through the mount. The full cause goes to `STORE/.http-errors.jsonl` and the log, and the film is recorded as failed and retried after the usual back-off.
- `keyframe_flush` (default on) and `keyframe_flush_check`: PyAV decodes the seek keyframe's own packet and drains the decoder instead of demuxing on until a frame comes out, which on a test film cut the data read per thumbnail from 8.5 MB to 1.3 MB with identical thumbnails. The built line now reports how frames were obtained (`frames by way: ...`) and, with the check on, whether they matched the old way. Measured on K-PAX (H.264): 5.7 to 1.4 MB per thumbnail on average (3.1 MB worst), 60 of 60 frame timestamps equal to the old way. On a 4K HEVC Dolby Vision remux with the flush on: 1.6 MB average, 2.1 MB worst, 10 of 10 equal; the flush-off run for that film is still to do.

- Calibrated, index-free thumbnail reads for films with no Cues (seek index) element at all, which otherwise forced an expensive scattered search for every seek (`noindex.py`; new `no_index` setting, default on). See the README's "Calibrated seeking for files with no Cues at all" entry for the design and what has and hasn't been validated yet.

## [0.1.0] - 2026-11-01

First packaged pre-release. Experimental. The code on main at the time of tagging is packaged as it is.

- Debian package `seekthumbs` (Architecture: all): the thumbnail builder, its local web page (127.0.0.1 only) and the mpv viewer script, installed under /usr/lib/seekthumbs with the `seekthumbs` command in /usr/bin and a user systemd unit for the background watcher in /usr/lib/systemd/user.
- `seekthumbs-setup` installs the mpv viewer (never the on-play build unless `--on-play`) and can enable the watcher, with `--dry-run`, `uninstall` and a check for hand-installed copies that would hide the packaged command.
- The package scripts never enable or start a service, never write into a home folder and never touch audio configuration. The packaged unit does not start without ~/.config/seekthumbs/config.ini.
