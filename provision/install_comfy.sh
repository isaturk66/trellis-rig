#!/usr/bin/env bash
# ComfyUI + ComfyUI-Trellis2, using the project's prebuilt Linux cp312 wheels.
# Ubuntu 24.04 ships Python 3.12, which is exactly what those wheels target —
# that is the whole reason this provisions in minutes instead of hours.
set -euo pipefail

COMFY_DIR=/opt/comfy
VENV="$COMFY_DIR/venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
CONSTRAINTS="$COMFY_DIR/constraints.txt"

# Must match the wheels/Linux/<TorchXXX> directory we install from.
TORCH_VERSION="${TORCH_VERSION:-2.9.1}"
# Pin the companions explicitly. Leaving torchaudio to the resolver got
# 2.11.0 alongside torch 2.9.1 — same index, no error, but built against a
# different libtorch ABI, so ComfyUI died on
# `_torchaudio.abi3.so: undefined symbol: torch_library_impl`. torchaudio
# tracks torch's minor version; torchvision runs one major behind.
TORCHVISION_VERSION="${TORCHVISION_VERSION:-0.24.1}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.9.1}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu128}"
WHEEL_DIR_NAME="${WHEEL_DIR_NAME:-Torch291}"

mkdir -p "$COMFY_DIR"

if [ ! -x "$PY" ]; then
  python3.12 -m venv "$VENV"
fi
"$PIP" install --upgrade pip wheel setuptools -q

echo "--- torch ${TORCH_VERSION} (cu128)"
# torchaudio is installed deliberately even though nothing here uses it:
# ComfyUI's requirements.txt depends on it, and transformers imports it
# eagerly via loss_rnnt. Leave it out and pip satisfies ComfyUI from PyPI with
# a CUDA 13 build, which then fails to dlopen libcudart.so.13 and takes down
# every `from transformers import ...`. Pulling it from the cu128 index here,
# and pinning it below, is what keeps the three in lockstep.
"$PIP" install -q \
  "torch==${TORCH_VERSION}" \
  "torchvision==${TORCHVISION_VERSION}" \
  "torchaudio==${TORCHAUDIO_VERSION}" \
  --index-url "$TORCH_INDEX"

# Freeze the torch stack. Any later `pip install --upgrade <anything that
# depends on torch>` will happily pull a build for a different CUDA and break
# every compiled wheel above; passing `-c` on subsequent installs makes that
# impossible rather than merely unlikely.
"$PY" - > "$CONSTRAINTS" <<'PYPIN'
import importlib.metadata as md
for pkg in ("torch", "torchvision", "torchaudio"):
    try:
        print(f"{pkg}=={md.version(pkg)}")
    except md.PackageNotFoundError:
        pass
PYPIN
echo "    pinned:"; sed 's/^/      /' "$CONSTRAINTS"

echo "--- comfyui"
if [ ! -d "$COMFY_DIR/ComfyUI/.git" ]; then
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY_DIR/ComfyUI"
fi
"$PIP" install -q -c "$CONSTRAINTS" -r "$COMFY_DIR/ComfyUI/requirements.txt"

echo "--- ComfyUI-Trellis2"
NODE_DIR="$COMFY_DIR/ComfyUI/custom_nodes/ComfyUI-Trellis2"
if [ ! -d "$NODE_DIR/.git" ]; then
  git clone --depth 1 https://github.com/visualbruno/ComfyUI-Trellis2 "$NODE_DIR"
fi

echo "--- prebuilt cuda wheels (${WHEEL_DIR_NAME}, cp312 linux)"
WHEELS="$NODE_DIR/wheels/Linux/$WHEEL_DIR_NAME"
if [ -d "$WHEELS" ]; then
  # --no-deps: several of these declare a bare `torch` dep and will happily
  # yank our pinned build out from under us otherwise.
  for whl in "$WHEELS"/*.whl; do
    echo "    $(basename "$whl")"
    "$PIP" install -q --no-deps --force-reinstall "$whl"
  done

  # ...but --no-deps also drops their harmless pure-python requirements, and
  # nothing else pulls them in: o_voxel dies at import with
  # "No module named 'trimesh'" without this. Listed explicitly so the torch
  # pin stays untouched.
  echo "--- wheel runtime deps (skipped by --no-deps)"
  "$PIP" install -q -c "$CONSTRAINTS" trimesh easydict plyfile zstandard tqdm
else
  echo "!! no wheel dir at $WHEELS — check available versions:"
  ls "$NODE_DIR/wheels/Linux" || true
  exit 1
fi

echo "--- node requirements"
"$PIP" install -q -c "$CONSTRAINTS" -r "$NODE_DIR/requirements.txt" || \
  echo "!! some node requirements failed — continuing"

# Wheels pulled their own transitive deps in with --no-deps off in the past;
# make sure torch is still the build we asked for.
echo "--- verify"
"$PY" - <<'PYCHECK'
import torch
print(f"    torch {torch.__version__} | cuda {torch.version.cuda} | "
      f"available={torch.cuda.is_available()}")
if torch.cuda.is_available():
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    print(f"    gpu {name} | compute capability {cap[0]}.{cap[1]}")
    if cap[0] < 8:
        print("    !! CC < 8.0 — bf16 unsupported, flow models will fail")
bad = []
# torchaudio is imported DIRECTLY, not via transformers. transformers swallows
# the failure, so it once reported "ok" while the same torchaudio was busy
# killing ComfyUI with `undefined symbol: torch_library_impl`. ComfyUI imports
# it unconditionally (comfy/ldm/lightricks/vae/audio_vae.py), so loading its
# native .so here is the honest test of whether the stack agrees.
for mod in ("cumesh", "o_voxel", "flex_gemm", "nvdiffrast", "trimesh",
            "torchaudio", "transformers"):
    try:
        __import__(mod)
        print(f"    ok   {mod}")
    except Exception as exc:
        print(f"    FAIL {mod}: {exc}")
        bad.append(mod)
if bad:
    # Fail loudly here rather than at the first prompt 20 minutes later.
    raise SystemExit(f"unusable install, broken imports: {', '.join(bad)}")

# Belt and braces: a torchaudio whose minor version leads torch's is an ABI
# landmine even when the import happens to succeed on this machine.
import torchaudio
tv = torch.__version__.split("+")[0].rsplit(".", 1)[0]
av = torchaudio.__version__.split("+")[0].rsplit(".", 1)[0]
print(f"    torch {torch.__version__} / torchaudio {torchaudio.__version__}")
if tv != av:
    raise SystemExit(
        f"torch/torchaudio minor mismatch ({tv} vs {av}) — pin "
        f"TORCHAUDIO_VERSION to match TORCH_VERSION")
PYCHECK

echo "--- workflows"
WF_SRC="$NODE_DIR/example_workflows"
WF_DST="$COMFY_DIR/ComfyUI/user/default/workflows"
mkdir -p "$WF_DST"
[ -d "$WF_SRC" ] && cp -f "$WF_SRC"/*.json "$WF_DST"/ 2>/dev/null || true
echo "    $(ls -1 "$WF_DST" 2>/dev/null | wc -l) workflows available in the UI"
