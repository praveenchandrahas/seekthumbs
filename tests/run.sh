#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m py_compile ../seekthumbs.py ../seekthumbs_web.py ../realdebrid.py ../live.py ../tools/live-check.py
python3 test_seekthumbs.py
python3 test_web.py
python3 test_realdebrid.py
python3 test_live.py
