#!/usr/bin/env bash
# Measures how a mount behaves for seekthumbs: the first touch of the file, then 4 MB reads at random places 1, 2, 4 and 8 at a time, then streaming.
# Read-only (it only reads the one file you name, once per test). Quiet the machine first; run it when no scan is running.
#   tools/mount-test.sh /path/to/a/film.mkv [rounds=3] [stream_mib=512]
# Use a file that has not been read for a while for the first line; run it again on the same file to see the warm numbers.
set -euo pipefail
f="${1:-}"; rounds="${2:-3}"; stream="${3:-512}"
[ -f "$f" ] || { echo "usage: $0 /path/to/film.mkv [rounds] [stream_mib]   (a file, not a folder)" >&2; exit 2; }
size=$(( $(stat -c %s "$f") / 1048576 - 8 ))
[ "$size" -gt 64 ] || { echo "file too small for the test" >&2; exit 2; }
now() { date +%s.%N; }
span() { awk -v a="$1" -v b="$2" 'BEGIN{printf "%.1f", b-a}'; }
s=$(now); head -c 1048576 "$f" > /dev/null; e=$(now)
echo "first 1 MB of the file (cold = the file's first touch): $(span "$s" "$e") s"
for rep in $(seq "$rounds"); do
  for p in 1 2 4 8; do
    s=$(now)
    for _ in $(seq "$p"); do dd if="$f" of=/dev/null bs=1M count=4 skip=$(( (RANDOM * 32768 + RANDOM) % size )) 2>/dev/null & done
    wait
    e=$(now)
    echo "round $rep: $p parallel 4 MB reads: $(span "$s" "$e") s"
  done
done
skip=$(( size / 8 / 4 * 4 ))
s=$(now); dd if="$f" of=/dev/null bs=4M skip=$(( skip / 4 )) count=$(( stream / 4 )) 2>/dev/null; e=$(now)
awk -v m="$stream" -v t="$(span "$s" "$e")" 'BEGIN{printf "streaming %d MiB: %.1f s = %.1f MB/s\n", m, t, m*1.048576/t}'
