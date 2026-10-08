#!/usr/bin/env bash
# Install or update vaultsync on the ZFS host (run as root on the Proxmox node).
#
#   ./host/install.sh --dashboard-ip 192.168.1.50 [--source tank] [--listen 0.0.0.0] [--port 8710]
#
# Re-running updates the program and keeps the existing config and token.
set -euo pipefail

SOURCE=tank
LISTEN=0.0.0.0
PORT=8710
DASH_IP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --listen) LISTEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --dashboard-ip) DASH_IP="$2"; shift 2 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v zfs >/dev/null || { echo "zfs not found; run this on the ZFS host" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
zfs list -H -o name "$SOURCE" >/dev/null || { echo "source dataset $SOURCE not found" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$HERE/vaultsync" /usr/local/sbin/vaultsync
install -d -m 0700 /etc/vaultsync /var/lib/vaultsync

CONF=/etc/vaultsync/config.json
if [[ ! -f $CONF ]]; then
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  python3 - "$CONF" "$SOURCE" "$LISTEN" "$PORT" "$TOKEN" "$DASH_IP" <<'PY'
import json, sys
path, source, listen, port, token, dash = sys.argv[1:]
cfg = {"source": source, "exclude": [], "listen": listen, "port": int(port), "token": token,
       "allowed_clients": [dash] if dash else [], "max_jobs": 2, "verify_after_sync": False,
       "pre_snapshot_hook": "", "stale_snapshot_days": 14, "ports": {}}
json.dump(cfg, open(path, "w"), indent=2)
PY
  chmod 600 "$CONF"
  echo "created $CONF"
elif [[ -n $DASH_IP ]]; then
  python3 - "$CONF" "$DASH_IP" <<'PY'
import json, sys
path, dash = sys.argv[1:]
cfg = json.load(open(path))
if dash not in cfg.setdefault("allowed_clients", []):
    cfg["allowed_clients"].append(dash)
json.dump(cfg, open(path, "w"), indent=2)
PY
  echo "added $DASH_IP to allowed_clients"
fi

cat > /etc/systemd/system/vaultsync-agent.service <<'UNIT'
[Unit]
Description=vaultsync agent (dashboard API for USB ZFS backups)
After=zfs.target network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/sbin/vaultsync agent
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now vaultsync-agent.service
systemctl restart vaultsync-agent.service

TOKEN="$(python3 -c 'import json;print(json.load(open("/etc/vaultsync/config.json"))["token"])')"
ALLOWED="$(python3 -c 'import json;print(", ".join(json.load(open("/etc/vaultsync/config.json"))["allowed_clients"]) or "any address (set --dashboard-ip to restrict)")')"
IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

vaultsync installed.
  CLI:      vaultsync status
  Agent:    http://$IP:$PORT  (accepts: $ALLOWED)

Put these in the dashboard's .env:
  AGENT_URL=http://$IP:$PORT
  AGENT_TOKEN=$TOKEN
EOF
