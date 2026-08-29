#!/usr/bin/env python3
"""
Analyzes QLOG files for Multipath Connection ID (CID) lifecycle events,
Path Validation (PATH_CHALLENGE / PATH_RESPONSE), RETIRE_CONNECTION_ID frames,
Path Abandonment, and Disconnection errors.
"""

import sys
import os
import glob
import json

def analyze_qlogs(qlog_dir="logs/qlog"):
    if not os.path.isdir(qlog_dir):
        print(f"Error: Directory {qlog_dir} not found.")
        return

    files = sorted(glob.glob(os.path.join(qlog_dir, "*.qlog")), key=os.path.getmtime)
    if not files:
        print(f"No .qlog files found in {qlog_dir}.")
        return

    print("=" * 115)
    print(f"{'QLOG File':<45} | {'New CID':<8} | {'Retire CID':<10} | {'Challenges':<10} | {'Responses':<10} | {'Abandons':<8} | {'Errors'}")
    print("=" * 115)

    for fpath in files[-24:]:  # last 24 runs (12 client + 12 server)
        fname = os.path.basename(fpath)
        new_cids = 0
        retire_cids = 0
        challenges = 0
        responses = 0
        abandons = 0
        errors = 0

        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                lower = line_str.lower()
                if "retire_connection_id" in lower:
                    retire_cids += 1
                if "new_connection_id" in lower or "path_new_connection_id" in lower:
                    new_cids += 1
                if "path_challenge" in lower:
                    challenges += 1
                if "path_response" in lower:
                    responses += 1
                if "path_abandon" in lower:
                    abandons += 1
                if "connection_close" in lower or "protocol_violation" in lower or "transport_error" in lower:
                    errors += 1

        print(f"{fname[:45]:<45} | {new_cids:<8} | {retire_cids:<10} | {challenges:<10} | {responses:<10} | {abandons:<8} | {errors}")

    print("=" * 115)

if __name__ == "__main__":
    qlog_path = sys.argv[1] if len(sys.argv) > 1 else "logs/qlog"
    analyze_qlogs(qlog_path)
