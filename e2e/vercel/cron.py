"""The serverless e2e's stand-in for Vercel Cron (e2e/docker-compose.vercel.yml, vercel.json "crons").

Every EVERY_S seconds it calls GET /cron/drain and GET /cron/tick through the edge (the proxy), the way Vercel does:
`Authorization: Bearer $CRON_SECRET`, `user-agent: vercel-cron/1.0`, the deployment's own URL as Host (not the
public one), and WITHOUT waiting for the previous call to finish, so a slow drain overlaps the next one exactly as
Vercel's crons may overlap. Each call is logged as one JSON line (job, start, end, the HTTP status and what the
endpoint answered), which the test reads back. Vercel's own cadence is one minute; the simulation runs faster to keep the
test short, which only makes overlaps more likely.
"""
import json
import os
import sys
import threading
import time

import httpx

EDGE = os.environ.get("EDGE", "http://proxy:8140")
EVERY_S = float(os.environ.get("EVERY_S", "5"))
SECRET = os.environ["CRON_SECRET"]
HOST = os.environ.get("DEPLOYMENT_HOST", "sc-e2e.vercel.app")


def call(job: str) -> None:
    start = time.time()
    try:
        r = httpx.get(f"{EDGE}/cron/{job}", timeout=900,
                      headers={"authorization": f"Bearer {SECRET}", "user-agent": "vercel-cron/1.0", "host": HOST,
                               "x-vercel-cron-schedule": "* * * * *"})
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = {"text": r.text[:200]}
        instance = r.headers.get("x-sim-instance")
    except httpx.HTTPError as exc:
        status, body, instance = 0, {"error": type(exc).__name__}, None
    line = {**{k: v for k, v in body.items() if k != "job"}, "cron": job, "start": round(start, 3),
            "end": round(time.time(), 3), "http": status, "instance": instance}
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdout.write(json.dumps({"cron": "start", "every_s": EVERY_S, "edge": EDGE}) + "\n")
    sys.stdout.flush()
    while True:
        for job in ("drain", "tick"):
            threading.Thread(target=call, args=(job,), daemon=True).start()
        time.sleep(EVERY_S)


if __name__ == "__main__":
    main()
