#!/usr/bin/env bash
# Safe replacement for update_reshalo: test before stopping; no dependency upgrades.
set -Eeuo pipefail
exec 9>/run/lock/reshalo-update.lock
flock -n 9 || { echo 'Another Reshala update is running.'; exit 1; }
repo=/root/reshalo_bot
python="$repo/venv/bin/python"
cd "$repo"
test -z "$(git status --porcelain)" || { echo 'Working tree has changes; update stopped.'; exit 1; }
previous=$(git rev-parse HEAD)
git fetch -q origin main
target=$(git rev-parse FETCH_HEAD)
git merge-base --is-ancestor "$previous" "$target" || { echo 'Non-fast-forward update stopped.'; exit 1; }
git diff --quiet "$previous" "$target" -- requirements.txt || { echo 'Dependencies changed: prepare and test a separate environment first.'; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
build="/srv/biomozg-instagram-build/update-$stamp"
backup="/srv/biomozg-instagram/backups/reshalo-$stamp"
mkdir -p "$build" "$backup"
chmod 700 "$backup"
git archive "$target" | tar -x -C "$build"
echo 'Checking candidate in isolation; live bot is still running.'
(
    cd "$build"
    "$python" -m compileall -q bot.py db.py texts.py instagram_automation.py instagram_gateway.py scripts tests
    "$python" -m unittest discover -s tests > tests.log 2>&1 || { tail -60 tests.log; exit 1; }
    tail -4 tests.log
    "$python" scripts/check_db_migration.py /root/reshalo_bot/credits.db
)
test "$(git rev-parse HEAD)" = "$previous" && test -z "$(git status --porcelain)" || { echo 'Source changed during checks; stopped.'; exit 1; }
git archive --format=tar.gz --output="$backup/code-before.tar.gz" "$previous"
cp -p /usr/local/bin/update_reshalo "$backup/update_reshalo-before"
cp -p .env "$backup/bot.env"
if test -f /srv/biomozg-instagram/settings.json; then
    cp -p /srv/biomozg-instagram/settings.json "$backup/instagram-settings.json"
fi
printf '%s\n' "$previous" > "$backup/previous-commit"
rollback() {
    result=$?
    trap - ERR
    echo 'Update failed; restoring previous code when safe.'
    if test "$(git rev-parse HEAD)" = "$target" && test -z "$(git status --porcelain)"; then
        git reset --keep "$previous"
    fi
    systemctl start reshalo.service || true
    echo "Backup: $backup"
    exit "$result"
}
trap rollback ERR
echo 'Restarting only reshalo.service.'
systemctl stop reshalo.service
"$python" - "$backup/credits.db" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
source = Path('/root/reshalo_bot/credits.db')
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as origin:
    with closing(sqlite3.connect(sys.argv[1])) as target:
        origin.backup(target)
PY
git merge -q --ff-only "$target"
"$python" scripts/install_instagram_service.py --apply
systemctl start reshalo.service
sleep 3
systemctl is-active --quiet reshalo.service
trap - ERR
echo "Reshala updated to $(git rev-parse --short HEAD). Backup: $backup"
systemctl show reshalo.service --property=ActiveState,SubState,MainPID,NRestarts
