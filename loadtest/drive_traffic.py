"""
Continuously hits /checkout to populate dashboards with real traffic, and
supports a --chaos flag to spike the error rate for a window so you can
watch the SLO burn-rate alerts actually fire in Alertmanager/Grafana, then
clear again once the window ends. This is what turns the stack from
"screenshots of empty dashboards" into a demo of the alerting actually
working end to end.

Usage:
    pip install requests
    python drive_traffic.py                       # steady baseline traffic
    python drive_traffic.py --chaos 120 --rate 0.3 # 30% error rate for 120s
"""
import argparse
import random
import time

import requests

BASE_URL = "http://localhost:8000"


def set_chaos(error_rate=None, latency_ms=None):
    payload = {}
    if error_rate is not None:
        payload["error_rate"] = error_rate
    if latency_ms is not None:
        payload["latency_ms"] = latency_ms
    requests.post(f"{BASE_URL}/admin/chaos", json=payload, timeout=5)


def fire_requests(duration_s, rps=10):
    end = time.time() + duration_s
    while time.time() < end:
        try:
            requests.post(
                f"{BASE_URL}/checkout",
                json={"cart_value": round(random.uniform(10, 250), 2)},
                timeout=5,
            )
        except requests.RequestException:
            pass
        time.sleep(1 / rps)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rps", type=float, default=10, help="baseline requests/sec")
    parser.add_argument("--chaos", type=int, default=0, help="seconds to run an error-rate spike after baseline warmup")
    parser.add_argument("--rate", type=float, default=0.3, help="error rate during the chaos window")
    parser.add_argument("--warmup", type=int, default=60, help="seconds of healthy baseline traffic before chaos")
    args = parser.parse_args()

    print(f"Baseline traffic for {args.warmup}s at {args.rps} req/s...")
    fire_requests(args.warmup, args.rps)

    if args.chaos:
        print(f"Injecting {args.rate:.0%} error rate for {args.chaos}s -- watch Alertmanager/Grafana...")
        set_chaos(error_rate=args.rate)
        fire_requests(args.chaos, args.rps)
        print("Reverting to baseline error rate...")
        set_chaos(error_rate=0.0005)

    print("Continuing steady baseline traffic (Ctrl+C to stop)...")
    while True:
        fire_requests(60, args.rps)
