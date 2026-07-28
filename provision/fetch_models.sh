#!/usr/bin/env bash
# Pre-warm the model cache. ComfyUI-Trellis2 resolves checkpoints under
# ComfyUI/models/<org>/<name>, so we lay them down there directly rather than
# letting the node stall on a 9GB download during the first prompt.
set -uo pipefail

COMFY_DIR=/opt/comfy
PIP="$COMFY_DIR/venv/bin/pip"
PY="$COMFY_DIR/venv/bin/python"
MODELS="$COMFY_DIR/ComfyUI/models"

"$PIP" install -q "huggingface_hub[hf_transfer]"
export HF_HUB_ENABLE_HF_TRANSFER=1

if [ -z "${HF_TOKEN:-}" ]; then
  echo "!! HF_TOKEN unset — DINOv3 is gated and WILL fail to download."
  echo "   It is the image encoder both TRELLIS.2 and Pixal3D condition on;"
  echo "   without it the nodes raise on every prompt. Request access to"
  echo "   facebook/dinov3-vitl16-pretrain-lvd1689m, then relaunch with"
  echo "   HF_TOKEN set."
fi

mkdir -p "$MODELS"

"$PY" - <<'PYFETCH'
import os
import sys
from huggingface_hub import snapshot_download

MODELS = "/opt/comfy/ComfyUI/models"
TOKEN = os.environ.get("HF_TOKEN") or None

# (repo_id, local subdir, gated?)
#
# No RMBG-2.0 here on purpose: the official repo's app.py uses it for
# background removal, but the ComfyUI node pack uses the ungated `rembg`
# package instead (see its requirements.txt / `from rembg import remove`).
# DINOv3 is the only genuinely gated dependency, and it is mandatory.
REPOS = [
    ("facebook/dinov3-vitl16-pretrain-lvd1689m",
     "facebook/dinov3-vitl16-pretrain-lvd1689m", True),
    ("microsoft/TRELLIS.2-4B", "microsoft/TRELLIS.2-4B", False),
    ("TencentARC/Pixal3D-T", "TencentARC/Pixal3D-T", False),
]

failed = []
for repo_id, subdir, gated in REPOS:
    dest = os.path.join(MODELS, subdir)
    print(f"--- {repo_id} -> {dest}", flush=True)
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=dest,
            token=TOKEN,
            max_workers=8,
        )
        print(f"    ok", flush=True)
    except Exception as exc:
        note = " (gated — needs approved HF access)" if gated else ""
        print(f"    FAILED{note}: {type(exc).__name__}: {exc}", flush=True)
        failed.append(repo_id)

if failed:
    print(f"\n!! {len(failed)} repo(s) missing: {', '.join(failed)}")
    sys.exit(1)
print("\nall models cached.")
PYFETCH

echo "--- disk"
df -h /opt | tail -1
du -sh "$MODELS" 2>/dev/null || true
