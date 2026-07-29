#!/usr/bin/env bash
# Entry point pulled by the Vast onstart hook. Everything downstream lives in
# the repo, so iterating on provisioning is `git push` + relaunch — no new
# image, no CLI change.
set -uo pipefail

RIG_REPO="${RIG_REPO:-https://github.com/isaturk66/trellis-rig}"
RIG_BRANCH="${RIG_BRANCH:-main}"
RIG_DIR=/opt/rig
LOG_DIR=/var/log/rig

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/bootstrap.log") 2>&1

phase() { echo -e "\n=== [$(date -u +%H:%M:%S)] $* ==="; echo "$*" > "$LOG_DIR/phase"; }
die()   { echo "!! FAILED: $*"; echo "$*" > "$LOG_DIR/failed"; exit 1; }

# Keep every large temporary off /tmp. On some hosts /tmp is a small tmpfs
# rather than part of the big overlay, and pip streams wheel downloads through
# TMPDIR — so a 2.5GB torch wheel dies with "No space left on device" while
# `df` still shows 80GB free. Exported so all child scripts inherit it.
export TMPDIR=/opt/rig-tmp
export PIP_CACHE_DIR=/opt/rig-tmp/pip-cache
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"

echo "disk layout at start:"
df -h / /tmp /opt 2>/dev/null | sed 's/^/  /'

phase "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  git curl ca-certificates build-essential \
  python3.12 python3.12-venv python3.12-dev python3-pip \
  libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 ffmpeg \
  nginx apache2-utils openssh-server unzip \
  || die "apt install"

phase "fetch rig repo (${RIG_BRANCH})"
rm -rf "$RIG_DIR"
git clone --depth 1 --branch "$RIG_BRANCH" "$RIG_REPO" "$RIG_DIR" || die "git clone rig"
chmod +x "$RIG_DIR"/provision/*.sh

phase "install comfyui + trellis2 nodes"
bash "$RIG_DIR/provision/install_comfy.sh" || die "install_comfy"

phase "fetch models"
bash "$RIG_DIR/provision/fetch_models.sh" || echo "!! model fetch had errors — \
ComfyUI will still start and can download on first use"

phase "start services"
bash "$RIG_DIR/provision/serve.sh" || die "serve"

phase "done"
date -u +%FT%TZ > "$LOG_DIR/ready"
echo "rig is up."
