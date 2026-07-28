#!/usr/bin/env bash
# Start ComfyUI bound to localhost, and put nginx in front of it with basic
# auth. ComfyUI has no auth of its own and custom nodes execute arbitrary
# Python — an open port here is remote code execution on a box you're paying
# for. The proxy also terminates websockets, which ComfyUI needs for progress.
set -uo pipefail

COMFY_DIR=/opt/comfy
LOG_DIR=/var/log/rig
COMFY_PORT="${RIG_COMFY_PORT:-8188}"
PROXY_PORT="${RIG_PROXY_PORT:-8189}"
AUTH_USER="${RIG_AUTH_USER:-}"
AUTH_PASS="${RIG_AUTH_PASS:-}"

mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR/ready"

echo "--- comfyui on 127.0.0.1:${COMFY_PORT}"
cd "$COMFY_DIR/ComfyUI"
nohup "$COMFY_DIR/venv/bin/python" main.py \
  --listen 127.0.0.1 --port "$COMFY_PORT" \
  > "$LOG_DIR/comfy.log" 2>&1 &
echo $! > "$LOG_DIR/comfy.pid"

if [ -n "$AUTH_USER" ] && [ -n "$AUTH_PASS" ]; then
  htpasswd -bc /etc/nginx/.rig_htpasswd "$AUTH_USER" "$AUTH_PASS" >/dev/null 2>&1
  AUTH_BLOCK="auth_basic \"rig\"; auth_basic_user_file /etc/nginx/.rig_htpasswd;"
  echo "--- basic auth enabled for user '${AUTH_USER}'"
else
  AUTH_BLOCK="auth_basic off;"
  echo "!! running WITHOUT auth — anyone who finds this port gets code execution"
fi

cat > /etc/nginx/sites-available/rig <<NGINX
server {
    listen ${PROXY_PORT} default_server;
    server_name _;

    # 404s until bootstrap finishes, which is exactly what \`rig ready\` polls.
    location = /rig/health {
        auth_basic off;
        alias ${LOG_DIR}/ready;
        default_type text/plain;
    }

    location = /rig/log {
        ${AUTH_BLOCK}
        alias ${LOG_DIR}/bootstrap.log;
        default_type text/plain;
    }

    location / {
        ${AUTH_BLOCK}
        proxy_pass http://127.0.0.1:${COMFY_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;

        # 3D assets are big and generation is slow; don't cut either off.
        client_max_body_size 1024M;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
NGINX

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/rig /etc/nginx/sites-enabled/rig
nginx -t || { echo "!! nginx config invalid"; exit 1; }
service nginx restart || nginx

echo "--- waiting for comfyui to answer"
for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${COMFY_PORT}/system_stats" >/dev/null 2>&1; then
    echo "    up after ${i}0s"
    exit 0
  fi
  sleep 10
done

echo "!! comfyui did not answer in 15min — last 40 lines:"
tail -40 "$LOG_DIR/comfy.log" || true
exit 1
