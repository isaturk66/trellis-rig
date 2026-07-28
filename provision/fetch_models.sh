#!/usr/bin/env bash
# Pre-warm the model cache. ComfyUI-Trellis2 resolves checkpoints under
# ComfyUI/models/<org>/<name>, so we lay them down there directly rather than
# letting the node stall on a 9GB download during the first prompt.
set -uo pipefail

COMFY_DIR=/opt/comfy
PIP="$COMFY_DIR/venv/bin/pip"
PY="$COMFY_DIR/venv/bin/python"
MODELS="$COMFY_DIR/ComfyUI/models"
RIG_DIR="${RIG_DIR:-/opt/rig}"

# huggingface_hub 1.x dropped the hf_transfer extra and deprecated
# HF_HUB_ENABLE_HF_TRANSFER (the accelerated backend is built in now), so
# asking for either just prints warnings.
"$PIP" install -q --upgrade huggingface_hub
mkdir -p "$MODELS"

# ---------------------------------------------------------------------------
# Generation models. Both ungated — no token needed.
#
# No RMBG-2.0 here on purpose: the official repo's app.py uses it for
# background removal, but the ComfyUI node pack uses the ungated `rembg`
# package instead (see its requirements.txt / `from rembg import remove`).
# ---------------------------------------------------------------------------
"$PY" - <<'PYFETCH'
import os
import sys
from huggingface_hub import snapshot_download

MODELS = "/opt/comfy/ComfyUI/models"
TOKEN = os.environ.get("HF_TOKEN") or None

REPOS = [
    ("microsoft/TRELLIS.2-4B", "microsoft/TRELLIS.2-4B"),
    ("TencentARC/Pixal3D-T", "TencentARC/Pixal3D-T"),
]

failed = []
for repo_id, subdir in REPOS:
    dest = os.path.join(MODELS, subdir)
    print(f"--- {repo_id} -> {dest}", flush=True)
    try:
        snapshot_download(repo_id=repo_id, local_dir=dest, token=TOKEN, max_workers=8)
        print("    ok", flush=True)
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}", flush=True)
        failed.append(repo_id)

sys.exit(1 if failed else 0)
PYFETCH
GEN_RC=$?

# ---------------------------------------------------------------------------
# DINOv3 — the image encoder both TRELLIS.2 and Pixal3D condition on.
# Mandatory: nodes.py raises outright when it is missing.
#
# Two independent ways in, because Meta runs two separate approval queues:
#   1. HF gated repo (`gated: manual`, human review) -> straight snapshot
#   2. Meta's own CDN portal -> original .pth, needs converting to HF layout
# Whichever you have, we use.
# ---------------------------------------------------------------------------
DEST="$MODELS/facebook/dinov3-vitl16-pretrain-lvd1689m"
DINO_RC=1

if [ -n "${HF_TOKEN:-}" ]; then
  echo "--- dinov3: trying gated HF repo"
  if "$PY" - <<'PYDINO'
import os, sys
from huggingface_hub import snapshot_download
try:
    snapshot_download(
        repo_id="facebook/dinov3-vitl16-pretrain-lvd1689m",
        local_dir="/opt/comfy/ComfyUI/models/facebook/dinov3-vitl16-pretrain-lvd1689m",
        token=os.environ.get("HF_TOKEN"),
        max_workers=8,
    )
except Exception as exc:
    print(f"    not available via HF: {type(exc).__name__}: {exc}")
    sys.exit(1)
PYDINO
  then
    echo "    ok (hf)"
    DINO_RC=0
  fi
fi

if [ "$DINO_RC" -ne 0 ] && [ -n "${DINOV3_URL_B64:-}" ]; then
  echo "--- dinov3: falling back to Meta CDN + official converter"
  bash "$RIG_DIR/provision/fetch_dinov3.sh" && DINO_RC=0
fi

if [ "$DINO_RC" -ne 0 ]; then
  echo ""
  echo "!! DINOv3 IS MISSING — every generation will raise."
  echo "   Provide ONE of:"
  echo "     HF_TOKEN with approved access to"
  echo "       facebook/dinov3-vitl16-pretrain-lvd1689m  (gated: manual)"
  echo "     DINOV3_URL_B64 = base64 of your signed dinov3.llamameta.net URL"
  echo "       for dinov3_vitl16_pretrain_lvd1689m-8aa4cbdd.pth"
fi

echo "--- disk"
df -h /opt | tail -1
du -sh "$MODELS" 2>/dev/null || true

[ "$GEN_RC" -eq 0 ] && [ "$DINO_RC" -eq 0 ]
