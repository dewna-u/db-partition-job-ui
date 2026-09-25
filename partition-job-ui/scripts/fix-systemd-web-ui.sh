#!/usr/bin/env bash
# One-shot fix for partition-job-api + partition-job-ui on this host.
# Run as root.
set -euo pipefail

ROOT="/opt/db-partition-job-ui-github/partition-job-ui"
UNIT_DIR="/usr/lib/systemd/system"

echo "=== Deploy root: $ROOT ==="
cd "$ROOT"

echo "=== Preflight ==="
test -f "$ROOT/api/main.py" || { echo "missing api/main.py"; exit 1; }
test -x "$ROOT/.venv/bin/python" || { echo "missing .venv/bin/python"; exit 1; }
test -d "$ROOT/frontend" || { echo "missing frontend/"; exit 1; }
test -f "$ROOT/.env" || echo "WARNING: $ROOT/.env not found (API will start but DB calls will fail)"

echo "=== Install Python API deps into existing venv ==="
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/python" -c "import uvicorn, fastapi, api.main; print('api import ok')"

echo "=== Fix frontend ownership (root-owned .next/node_modules breaks User=partitionui) ==="
if [[ ! -d "$ROOT/frontend/.next" || ! -x "$ROOT/frontend/node_modules/.bin/next" ]]; then
  echo "Building frontend..."
  cd "$ROOT/frontend"
  if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
  npm run build
  cd "$ROOT"
fi
chown -R partitionui:partitionui "$ROOT/frontend"
test -x "$ROOT/frontend/node_modules/.bin/next"

echo "=== Write systemd units to $UNIT_DIR ==="
install -m 0644 /dev/null "$UNIT_DIR/partition-job-api.service"
cat > "$UNIT_DIR/partition-job-api.service" <<EOF
[Unit]
Description=Partition Manager API (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=partitionui
Group=partitionui
WorkingDirectory=$ROOT
EnvironmentFile=-$ROOT/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

install -m 0644 /dev/null "$UNIT_DIR/partition-job-ui.service"
cat > "$UNIT_DIR/partition-job-ui.service" <<EOF
[Unit]
Description=Partition Manager UI (Next.js)
After=network-online.target partition-job-api.service
Wants=network-online.target partition-job-api.service

[Service]
Type=simple
User=partitionui
Group=partitionui
WorkingDirectory=$ROOT/frontend
EnvironmentFile=-$ROOT/.env
Environment=NODE_ENV=production
Environment=PARTITION_API_ORIGIN=http://127.0.0.1:8000
ExecStart=$ROOT/frontend/node_modules/.bin/next start --hostname 0.0.0.0 --port 8501
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

# Stale copies in /etc override /usr/lib — remove them
rm -f /etc/systemd/system/partition-job-api.service
rm -f /etc/systemd/system/partition-job-ui.service
rm -f /etc/systemd/system/multi-user.target.wants/partition-job-api.service
rm -f /etc/systemd/system/multi-user.target.wants/partition-job-ui.service

systemctl daemon-reload
systemctl enable partition-job-api.service partition-job-ui.service
systemctl restart partition-job-api.service
sleep 1
systemctl restart partition-job-ui.service
sleep 1

echo "=== Status ==="
systemctl status partition-job-api.service partition-job-ui.service --no-pager -l || true

echo "=== Unit ExecStart (must show this ROOT, not /opt/partition-job-ui) ==="
systemctl cat partition-job-api.service | grep -E 'WorkingDirectory|ExecStart'
systemctl cat partition-job-ui.service | grep -E 'WorkingDirectory|ExecStart'

echo "=== Health ==="
curl -sS http://127.0.0.1:8000/api/health || true
echo
curl -sS -o /dev/null -w "ui_http=%{http_code}\n" http://127.0.0.1:8501/ || true

echo "=== Recent logs if still failing ==="
journalctl -u partition-job-api.service -u partition-job-ui.service -n 40 --no-pager || true
