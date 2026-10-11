#!/usr/bin/env bash
# Start Plane Tracker:  ./start_mac_linux.sh   (needs Python 3.10+)
cd "$(dirname "$0")"
python3 run_app.py "$@"
