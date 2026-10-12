import sys
import time


def log(msg):
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
