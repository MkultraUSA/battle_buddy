#!/usr/bin/env python3
"""
bb-query: Quick Battle Buddy data queries via SSH from the container.
Usage: bb-query <command> [args...]
Runs the query on the VPS and streams results back.
"""

import subprocess
import sys

VPS = "root@kevcloud.ddns.net"
BB_DATA = "/usr/local/bin/bb-data"

def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: bb-query <command> [args...]")
        print("Commands: calls, activity, incidents, events, aircraft, bookings, tips, tgid, stats, live, search")
        sys.exit(1)

    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", VPS, BB_DATA] + args
    subprocess.run(cmd)

if __name__ == "__main__":
    main()
