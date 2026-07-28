#!/usr/bin/env bash
# Build the HuggingFace-format DINOv3 encoder from Meta's original .pth.
#
# Why this exists: `facebook/dinov3-vitl16-pretrain-lvd1689m` on HF is
# `gated: manual` — a human review queue. Meta's own download portal
# (dinov3.llamameta.net) is a SEPARATE approval that often lands first, but it
# hands you the reference-implementation checkpoint, not the HF layout that
# ComfyUI-Trellis2 loads (config.json + model.safetensors +
# preprocessor_config.json).
#
# transformers ships the official converter for exactly this. It normally
# pulls the .pth from the gated repo; we point it at the local file instead.
# The filename it expects — dinov3_vitl16_pretrain_lvd1689m-8aa4cbdd.pth — is
# byte-identical to what the Meta portal serves, hash included.
#
# Takes DINOV3_URL_B64 (base64 of the signed CDN URL; base64 so the signature's
# &, =, ~ survive the trip through Vast's docker-flag env string).
set -uo pipefail

COMFY_DIR=/opt/comfy
PY="$COMFY_DIR/venv/bin/python"
PIP="$COMFY_DIR/venv/bin/pip"
MODELS="$COMFY_DIR/ComfyUI/models"
DEST="$MODELS/facebook/dinov3-vitl16-pretrain-lvd1689m"
WORK=/tmp/dinov3
PTH="$WORK/dinov3_vitl16_pretrain_lvd1689m-8aa4cbdd.pth"

if [ -f "$DEST/model.safetensors" ]; then
  echo "    already present at $DEST — skipping"
  exit 0
fi

if [ -z "${DINOV3_URL_B64:-}" ]; then
  echo "!! DINOV3_URL_B64 not set and no HF access — cannot obtain the encoder."
  exit 1
fi

URL="$(echo "$DINOV3_URL_B64" | base64 -d)"
mkdir -p "$WORK" "$(dirname "$DEST")"

echo "--- downloading original checkpoint from Meta CDN"
# Signed CloudFront URLs expire; a 403 here means request a fresh link.
if ! curl -fL --retry 3 --retry-delay 5 -o "$PTH" "$URL"; then
  echo "!! download failed — the signed URL has most likely expired (they are"
  echo "   short-lived). Re-request from the DINOv3 email link and relaunch."
  exit 1
fi
ls -lh "$PTH"

echo "--- converter deps"
# NOT --upgrade, and constrained. An unconstrained `--upgrade ... torchvision`
# here resolves to the newest torchvision, which drags in a torch built for a
# different CUDA. That silently replaces the pinned 2.9.1+cu128, after which
# transformers' eager torchaudio import raises a CUDA-mismatch RuntimeError
# and DINOv3ViTModel becomes unimportable — which is exactly how this failed
# on the first live run.
"$PIP" install -q -c "$COMFY_DIR/constraints.txt" transformers httpx pillow

echo "--- fetching official converter"
# Match the converter to the transformers we actually installed. Taking it
# from main risks it referencing symbols the local version doesn't have yet.
TFV="$("$PY" -c 'import transformers; print(transformers.__version__)' 2>/dev/null)"
CONV_PATH=src/transformers/models/dinov3_vit/convert_dinov3_vit_to_hf.py
for ref in "v${TFV}" main; do
  echo "    trying ref ${ref}"
  if curl -fsSL -o "$WORK/convert_dinov3_vit_to_hf.py" \
      "https://raw.githubusercontent.com/huggingface/transformers/${ref}/${CONV_PATH}"; then
    echo "    got converter from ${ref}"
    break
  fi
done
[ -s "$WORK/convert_dinov3_vit_to_hf.py" ] || { echo "!! no converter"; exit 1; }

echo "--- converting (includes numerical verification against reference outputs)"
cd "$WORK"
DINOV3_LOCAL_PTH="$PTH" DINOV3_SAVE_ROOT="$WORK/out" "$PY" - <<'PYCONV'
import argparse
import os
import sys

sys.path.insert(0, "/tmp/dinov3")
import convert_dinov3_vit_to_hf as conv

local = os.environ["DINOV3_LOCAL_PTH"]

# The converter pulls the checkpoint from the gated hub repo. We already have
# the identical file from Meta's CDN, so short-circuit the download.
conv.hf_hub_download = lambda *a, **kw: local

args = argparse.Namespace(
    model_name="vitl16_lvd1689m",
    save_dir=os.environ["DINOV3_SAVE_ROOT"],
    push_to_hub=False,
)
# Raises on mismatch: the script asserts both the preprocessing and the
# forward-pass outputs against hardcoded reference values. If this returns
# cleanly, the weights are genuinely correct.
conv.convert_and_test_dinov3_checkpoint(args)
print("conversion verified")
PYCONV

if [ ! -f "$WORK/out/vitl16_lvd1689m/model.safetensors" ]; then
  echo "!! conversion produced no model.safetensors"
  exit 1
fi

echo "--- installing to $DEST"
rm -rf "$DEST"
mv "$WORK/out/vitl16_lvd1689m" "$DEST"
ls -la "$DEST"
rm -rf "$WORK"
echo "    dinov3 ready"
