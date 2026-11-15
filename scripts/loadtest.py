"""Throw steady traffic at the Rolodex so auto scaling has something to react to.

    python scripts/loadtest.py https://dxxxxxxxx.cloudfront.net --minutes 6

Watch the fleet panel: after a few minutes of load, ECS adds containers (up to 4).
Stop it and, a few minutes later, it scales back to 2. Standard library only.
"""
import argparse
import threading
import time
import urllib.request
from collections import Counter

ap = argparse.ArgumentParser()
ap.add_argument("url")
ap.add_argument("--minutes", type=float, default=6)
ap.add_argument("--workers", type=int, default=12)
a = ap.parse_args()
base = a.url.rstrip("/")
stop = time.time() + a.minutes * 60
seen, errors, lock = Counter(), Counter(), threading.Lock()


def worker():
    while time.time() < stop:
        try:
            with urllib.request.urlopen(base + "/api/people", timeout=10) as r:
                who = r.headers.get("X-Served-By", "?").split()[0]
            with lock:
                seen[who] += 1
        except Exception as e:
            with lock:
                errors[type(e).__name__] += 1
            time.sleep(1)


threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
for t in threads:
    t.start()
while time.time() < stop:
    time.sleep(15)
    with lock:
        total = sum(seen.values())
        print(f"{time.strftime('%H:%M:%S')}  {total} requests · {len(seen)} containers answered · "
              + "  ".join(f"{k}:{v}" for k, v in seen.most_common()) + (f"  errors {dict(errors)}" if errors else ""))
print("done")
