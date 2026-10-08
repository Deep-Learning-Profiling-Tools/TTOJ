#!/usr/bin/env bash
set -euo pipefail
tt_oj_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
tt_oj_service=tt-oj-web-tunnel.service

case "${1:-status}" in
    install)
        if [[ ! -f "$tt_oj_root/deployment/$tt_oj_service" ]]; then
            printf 'Copy deployment/%s.example to deployment/%s and configure it first.\n' "$tt_oj_service" "$tt_oj_service" >&2
            exit 1
        fi
        sudo install -m 644 "$tt_oj_root/deployment/$tt_oj_service" "/etc/systemd/system/$tt_oj_service"
        sudo systemctl daemon-reload
        sudo systemctl enable "$tt_oj_service"
        sudo systemctl restart "$tt_oj_service"
        ;;
    start) sudo systemctl start "$tt_oj_service" ;;
    stop) sudo systemctl stop "$tt_oj_service" ;;
    disable) sudo systemctl disable --now "$tt_oj_service" ;;
    status) systemctl status --no-pager "$tt_oj_service" ;;
    logs) sudo journalctl -u "$tt_oj_service" -n 40 --no-pager ;;
    check)
        : "${TT_OJ_VPS_HOST:?Set TT_OJ_VPS_HOST to the SSH destination (user@host)}"
        : "${TT_OJ_SSH_KEY:?Set TT_OJ_SSH_KEY to a private key path}"
        tt_oj_remote_port=${TT_OJ_REMOTE_PORT:-28888}
        if [[ ! "$tt_oj_remote_port" =~ ^[0-9]+$ ]] || (( tt_oj_remote_port < 1 || tt_oj_remote_port > 65535 )); then
            printf 'TT_OJ_REMOTE_PORT must be an integer from 1 to 65535.\n' >&2
            exit 1
        fi
        curl --fail --silent --show-error --max-time 10 --output /dev/null \
            --write-out 'Local OJ HTTP: %{http_code}\n' http://127.0.0.1:8888/
        ssh -i "$TT_OJ_SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
            -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$TT_OJ_VPS_HOST" \
            "curl --fail --silent --show-error --max-time 15 --output /dev/null --write-out 'VPS tunnel HTTP: %{http_code}\\n' http://127.0.0.1:$tt_oj_remote_port/"
        ;;
    *) printf 'Usage: %s {install|start|stop|disable|status|logs|check}\n' "$0" >&2; exit 2 ;;
esac
