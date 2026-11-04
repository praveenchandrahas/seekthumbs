# Changelog

## [Unreleased]

- Calibrated, index-free thumbnail reads for films with no Cues (seek index) element at all, which otherwise forced an expensive scattered search for every seek (`noindex.py`; new `no_index` setting, default on). See the README's "Calibrated seeking for files with no Cues at all" entry for the design and what has and hasn't been validated yet.

## [0.1.0] - 2026-11-01

First packaged pre-release. Experimental. The code on main at the time of tagging is packaged as it is.

- Debian package `seekthumbs` (Architecture: all): the thumbnail builder, its local web page (127.0.0.1 only) and the mpv viewer script, installed under /usr/lib/seekthumbs with the `seekthumbs` command in /usr/bin and a user systemd unit for the background watcher in /usr/lib/systemd/user.
- `seekthumbs-setup` installs the mpv viewer (never the on-play build unless `--on-play`) and can enable the watcher, with `--dry-run`, `uninstall` and a check for hand-installed copies that would hide the packaged command.
- The package scripts never enable or start a service, never write into a home folder and never touch audio configuration. The packaged unit does not start without ~/.config/seekthumbs/config.ini.
