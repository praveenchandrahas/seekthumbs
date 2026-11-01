#!/usr/bin/env bash
# The repository's full test suite, the same command in CI and on your machine. Needs python3, ffmpeg, python3-numpy, python3-lupa
# (python3-av is optional).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
bash tests/run.sh
