"""Import only the Biomozg Instagram workflow and its internal runner credential.

No Meta token is imported into n8n. Run after install_instagram_service.py.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

ROOT = Path("/srv/biomozg-instagram")
N8N = Path("/srv/biomozg-n8n")
SOURCE = Path(__file__).resolve().parents[1]


def cli(*args):
    subprocess.run(["docker", "compose", "exec", "-T", "n8n", "n8n", *args], cwd=N8N, check=True)


def install():
    if os.geteuid() != 0:
        raise SystemExit("Run as root.")
    workflow = json.loads((SOURCE / "automation" / "instagram.n8n.json").read_text())
    credentials = json.loads((ROOT / "secrets" / "credentials.json").read_text())
    database = N8N / "data" / "database.sqlite"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / "backups" / ("n8n-" + stamp)
    backup.mkdir(parents=True, mode=0o700)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as con:
        row = con.execute('SELECT projectId FROM shared_workflow WHERE workflowId=?', ("IbjiUKzgiI38yae7",)).fetchone()
        if not row:
            raise SystemExit("The known Biomozg project was not found; nothing imported.")
        project_id = row[0]
        existing = con.execute('SELECT name FROM workflow_entity WHERE id=?', (workflow["id"],)).fetchone()
        if existing and existing[0] != workflow["name"]:
            raise SystemExit("Workflow ID belongs to another workflow; nothing imported.")
        with closing(sqlite3.connect(backup / "database.sqlite")) as target:
            con.backup(target)
    shutil.copy2(N8N / ".env", backup / "n8n.env")
    runner = [{"id": "BiomozgIgRunner01", "name": "Биомозг · защищённый обработчик Instagram",
               "type": "httpHeaderAuth", "data": {"name": "Authorization", "value": "Bearer " + credentials["runner_token"]}}]
    credential_file = N8N / "data" / ".instagram-credential-import.json"
    workflow_file = N8N / "data" / ".instagram-workflow-import.json"
    try:
        for file, value in ((credential_file, runner), (workflow_file, workflow)):
            file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            os.chown(file, 1000, 1000)
            os.chmod(file, 0o600)
        cli("import:credentials", "--input=/home/node/.n8n/" + credential_file.name, "--projectId=" + project_id)
        cli("import:workflow", "--input=/home/node/.n8n/" + workflow_file.name, "--projectId=" + project_id)
        cli("publish:workflow", "--id=" + workflow["id"])
    finally:
        credential_file.unlink(missing_ok=True)
        workflow_file.unlink(missing_ok=True)
    subprocess.run(["docker", "compose", "restart", "n8n"], cwd=N8N, check=True)
    print("Instagram runner imported and published. Meta account approval is a separate required step.")


if __name__ == "__main__":
    install()
