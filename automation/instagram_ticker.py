"""Wake the isolated Instagram gateway every 5 seconds (replaces the old n8n schedule).

Runs as the gateway's system user, reads only runner_token from the credentials
file on every tick (token rotation needs no restart) and calls the gateway on
localhost. The gateway itself skips a tick while the previous one is running.
"""
import json
import os
import time
import urllib.error
import urllib.request

CREDENTIALS = os.getenv("INSTAGRAM_CREDENTIALS", "/srv/biomozg-instagram/secrets/credentials.json")
URL = os.getenv("INSTAGRAM_RUNNER_URL", "http://127.0.0.1:5680/instagram/runner")
INTERVAL = 5


def tick(credentials=CREDENTIALS, url=URL):
    with open(credentials, encoding="utf-8") as f:
        token = json.load(f)["runner_token"]
    request = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=50) as response:
        return response.status


def main():
    last = None
    while True:
        started = time.monotonic()
        try:
            state = "ok %d" % tick()
        except urllib.error.HTTPError as error:
            state = "http %d" % error.code
        except Exception as error:  # keep ticking; log only state changes
            state = "error " + type(error).__name__
        if state != last:
            print("runner:", state, flush=True)
            last = state
        time.sleep(max(0.5, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
