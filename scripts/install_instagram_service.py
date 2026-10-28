"""Install only the isolated Biomozg Instagram adapter. Run with --apply as root.

Does not restart the Telegram bot, change its DB or import Meta credentials.
The existing n8n virtual host is the only nginx file this installer edits.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess


ROOT = Path("/srv/biomozg-instagram")
SOURCE = Path(__file__).resolve().parents[1]
ACCOUNT = "biomozg-instagram"


def run(*args):
    subprocess.run(args, check=True)


def install():
    if os.geteuid() != 0:
        raise SystemExit("Run as root.")
    try:
        account = pwd.getpwnam(ACCOUNT)
    except KeyError:
        run("useradd", "--system", "--home-dir", str(ROOT), "--shell", "/usr/sbin/nologin", ACCOUNT)
        account = pwd.getpwnam(ACCOUNT)
    ROOT.mkdir(exist_ok=True)
    (ROOT / "app").mkdir(exist_ok=True)
    for name in ("instagram_gateway.py", "instagram_automation.py"):
        shutil.copy2(SOURCE / name, ROOT / "app" / name)
        os.chmod(ROOT / "app" / name, 0o644)
    for name in ("state", "secrets"):
        directory = ROOT / name
        directory.mkdir(exist_ok=True)
        os.chown(directory, account.pw_uid, account.pw_gid)
        os.chmod(directory, 0o700)
    credential_path = ROOT / "secrets" / "credentials.json"
    if not credential_path.exists():
        credential_path.write_text(json.dumps({"runner_token": secrets.token_urlsafe(48), "verify_token": secrets.token_urlsafe(32)}, indent=2) + "\n")
    os.chown(credential_path, account.pw_uid, account.pw_gid)
    os.chmod(credential_path, 0o600)
    import sys
    sys.path.insert(0, str(SOURCE))
    import instagram_automation as campaign
    config = ROOT / "settings.json"
    if not config.exists():
        campaign.save_settings(campaign.DEFAULTS, config)
    dropin = Path("/etc/systemd/system/reshalo.service.d")
    dropin.mkdir(exist_ok=True)
    (dropin / "30-instagram-admin.conf").write_text(
        "[Service]\nEnvironment=INSTAGRAM_CONFIG_PATH=/srv/biomozg-instagram/settings.json\n"
        "Environment=INSTAGRAM_STATUS_PATH=/srv/biomozg-instagram/state/status.json\n")
    unit = """[Unit]
Description=Biomozg isolated Instagram webhook and queue
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=biomozg-instagram
Group=biomozg-instagram
WorkingDirectory=/srv/biomozg-instagram/app
Environment=INSTAGRAM_CONFIG_PATH=/srv/biomozg-instagram/settings.json
Environment=INSTAGRAM_ROOT=/srv/biomozg-instagram
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /srv/biomozg-instagram/app/instagram_gateway.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/srv/biomozg-instagram/state /srv/biomozg-instagram/secrets
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077
MemoryMax=96M
CPUQuota=5%
TasksMax=24
LimitNOFILE=128

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/biomozg-instagram.service").write_text(unit)
    nginx_path = Path("/etc/nginx/sites-available/biomozg-n8n")
    original = nginx_path.read_text()
    marker = "    # Biomozg Instagram adapter\n"
    if marker not in original:
        anchor = "    client_max_body_size 16m;\n"
        if original.count(anchor) != 1:
            raise SystemExit("Unexpected n8n virtual host; nginx not edited.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = ROOT / "backups" / stamp
        backup.mkdir(parents=True)
        shutil.copy2(nginx_path, backup / "nginx-before.conf")
        locations = """    # Biomozg Instagram adapter
    location = /instagram/webhook {
        access_log off;
        client_max_body_size 256k;
        proxy_pass http://127.0.0.1:5680/instagram/webhook;
        proxy_read_timeout 15s;
    }
    location = /instagram/runner {
        access_log off;
        client_max_body_size 1k;
        proxy_pass http://127.0.0.1:5680/instagram/runner;
        proxy_read_timeout 50s;
    }
"""
        nginx_path.write_text(original.replace(anchor, anchor + locations))
        try:
            run("nginx", "-t")
        except BaseException:
            nginx_path.write_text(original)
            raise
        run("systemctl", "reload", "nginx")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "biomozg-instagram.service")
    run("systemctl", "restart", "biomozg-instagram.service")
    print("Instagram adapter installed. Meta credentials are still required; no Instagram messages sent.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    if parser.parse_args().apply:
        install()
    else:
        print("Plan: install isolated adapter + two n8n HTTPS routes + Telegram admin config paths. Use --apply to install.")
