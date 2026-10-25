#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m py_compile ../seekthumbs.py
python3 test_seekthumbs.py
