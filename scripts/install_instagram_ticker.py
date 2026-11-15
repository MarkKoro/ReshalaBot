"""Install the 5-second runner timer for the Instagram gateway. Run with --apply as root.

Replaces the former n8n workflow: n8n only called /instagram/runner every 5 seconds.
Run after install_instagram_service.py. Does not restart the Telegram bot or the
gateway and never touches Meta credentials. With --stop-n8n it also stops the old
n8n container after the gateway status has been confirmed fresh.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

SOURCE = Path(__file__).resolve().parents[1]
TICKER = Path("/usr/local/lib/biomozg-instagram/ticker.py")
UNIT = Path("/etc/systemd/system/biomozg-instagram-ticker.service")
STATUS = Path("/srv/biomozg-instagram/state/status.json")
N8N = Path("/srv/biomozg-n8n")


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def status_age():
    try:
        return time.time() - json.loads(STATUS.read_text())["updated_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def install(stop_n8n):
    if os.geteuid() != 0:
        raise SystemExit("Run as root.")
    TICKER.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / "automation" / "instagram_ticker.py", TICKER)
    os.chmod(TICKER, 0o644)
    shutil.copy2(SOURCE / "automation" / "biomozg-instagram-ticker.service", UNIT)
    os.chmod(UNIT, 0o644)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "biomozg-instagram-ticker.service")
    run("systemctl", "restart", "biomozg-instagram-ticker.service")
    started = time.time()
    while time.time() - started < 30:
        age = status_age()
        if age is not None and age < 10:
            break
        time.sleep(2)
    else:
        raise SystemExit("Gateway status is not fresh; check biomozg-instagram and the ticker journal. n8n left as is.")
    print("Ticker installed; gateway status is fresh.")
    if stop_n8n and (N8N / "compose.yaml").exists():
        run("docker", "compose", "stop", "n8n", cwd=N8N)
        print("Old n8n container stopped (data kept in", N8N, ").")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--stop-n8n", action="store_true")
    args = parser.parse_args()
    if args.apply:
        install(args.stop_n8n)
    else:
        print("Plan: install biomozg-instagram-ticker.service (POST /instagram/runner every 5 s on localhost). Use --apply.")
