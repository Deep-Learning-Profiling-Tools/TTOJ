#!/bin/sh
set -eu

mkdir -p /root/.hydro /data/files
if [ ! -f /root/.hydro/config.json ]; then
    printf '%s\n' '{"host":"mongo","port":"27017","name":"hydro","username":"","password":""}' > /root/.hydro/config.json
fi
node - <<'JS'
const fs = require('node:fs');
const file = '/root/.hydro/addon.json';
const addons = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : ['@hydrooj/ui-default'];
if (!addons.includes('/opt/hydro/tt-site')) addons.push('/opt/hydro/tt-site');
fs.writeFileSync(file, JSON.stringify(addons));
JS

# Start upstream once so its migrations and asset preparation finish before seed.
hydrooj --host 0.0.0.0 --port 8888 &
site_pid=$!
trap 'kill "$site_pid" 2>/dev/null || true' EXIT INT TERM
ready=false
for attempt in $(seq 1 120); do
    if node /app/healthcheck.cjs; then ready=true; break; fi
    if ! kill -0 "$site_pid" 2>/dev/null; then wait "$site_pid"; exit 1; fi
    sleep 1
done
if [ "$ready" != true ]; then
    printf '%s\n' 'Hydro startup did not become ready within 120 seconds.' >&2
    exit 1
fi

hydrooj cli execute 'await require("/app/bootstrap.cjs")().catch((e) => { console.error(e); process.exit(1); })'
kill "$site_pid"
wait "$site_pid" || true
trap - EXIT INT TERM
touch /root/.hydro/tt-bootstrap-complete
exec hydrooj --host 0.0.0.0 --port 8888
