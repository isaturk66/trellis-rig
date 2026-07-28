# trellis-rig

Rent the cheapest sane GPU on Vast.ai, provision **TRELLIS.2** + **Pixal3D-T**
under ComfyUI, get a URL, destroy it when you're done.

Built for burst use: spin up, generate, tear down. An idle GPU is the only way
to lose money here, so every command tells you what you're burning.

```
python rig.py offers      # what's cheap right now
python rig.py up          # rent + provision, prints a URL
python rig.py ready       # poll until provisioning finishes
python rig.py status      # uptime, burn rate, spend so far
python rig.py down        # destroy, print the bill
```

The local CLI is **stdlib-only Python** — nothing to install, nothing downloaded
locally. All the heavy lifting happens on the rented box.

---

## Setup

Needs Python 3.9+ and a Vast.ai API key.

```powershell
# repo root, gitignored
"VAST_API_KEY=<your key>" | Out-File -Encoding utf8 .env
"HF_TOKEN=<your hf token>" | Add-Content .env
```

Or use environment variables (`$env:VAST_API_KEY`, `$env:HF_TOKEN`).

### DINOv3 — the one gated dependency

DINOv3 is the image encoder both TRELLIS.2 and Pixal3D condition on: it turns
your input image into the features the flow models denoise against. There's no
substitute and no fallback — `nodes.py` raises outright when it's missing.

Meta runs **two separate approval queues**, and getting into one does not get
you into the other:

| route | what you get | how to use it |
|---|---|---|
| [HF gated repo](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m) | HF layout, drops straight in | `HF_TOKEN=...` |
| [Meta CDN portal](https://ai.meta.com/dinov3/) | original `.pth`, needs converting | `DINOV3_URL=<signed link>` |

The HF repo is `gated: manual` — a human review queue, not a click-through.
Every community mirror inherits the same gate, so there's no way around it
there. The Meta portal often approves first.

Set **either** in `.env`. If you have the Meta link, provisioning downloads the
`.pth` and runs it through the **official transformers converter**
(`convert_dinov3_vit_to_hf.py`) with `hf_hub_download` pointed at the local
file. The filename Meta serves —
`dinov3_vitl16_pretrain_lvd1689m-8aa4cbdd.pth` — is byte-identical to what the
converter expects, hash included. The script asserts both preprocessing and
forward-pass outputs against hardcoded reference values, so a clean run means
the weights are verified correct, not merely present.

> **Signed CDN links are short-lived** (~48h). A 403 during provisioning means
> re-request from the email link and relaunch. The URL is passed base64-encoded
> so the CloudFront signature's `&`, `=` and `~` survive Vast's docker-flag
> env string.

Background removal does **not** need `briaai/RMBG-2.0`. That's what the
official repo's `app.py` uses; the ComfyUI node pack uses the ungated `rembg`
package instead. Do remove backgrounds though — it materially affects output
quality ([TRELLIS.2#65](https://github.com/microsoft/TRELLIS.2/issues/65)).

---

## GPU tiers

Every card in every tier is Ampere or newer. That's deliberate: the TRELLIS.2
and Pixal3D flow models are trained in **bf16**, which needs compute capability
≥ 8.0. Volta and Turing cards advertise plenty of VRAM and then die with
`bf16 is only supported on A100+ GPUs`.

| tier | VRAM | cards | use it for |
|---|---|---|---|
| `cheap` | 24–32 GB | 3090, 4090, 5090, A10 | runs, but you'll fight post-processing OOMs |
| `mid` ⭐ | 48 GB | A6000, A40, L40S, RTX 6000 Ada | **default** — clears the remesh ceiling |
| `max` | 80 GB+ | A100 80GB, H100, H200 | 1536³ hero assets, 63 GB+ peaks |

```bash
python rig.py offers --tier mid
python rig.py up --tier max --max-price 1.50
python rig.py up --offer 12345678      # pin a specific host
```

Offers are filtered to verified hosts with ≥200 Mbit down, ≥98% reliability,
≥100 GB disk and CUDA ≥12.4, then sorted by price. The genuinely cheapest
listing on Vast is usually a 50 Mbit box that takes an hour to pull 25 GB of
weights — that's not cheap, it's slow.

---

## What lands on the box

- ComfyUI + [ComfyUI-Trellis2](https://github.com/visualbruno/ComfyUI-Trellis2)
- Torch 2.9.1 / cu128, and the project's **prebuilt Linux `cp312` wheels**
  (`cumesh`, `o_voxel`, `flex_gemm`, `nvdiffrast`, `nvdiffrec_render`) — no
  compilation, which is why this provisions in minutes rather than hours
- Models: `microsoft/TRELLIS.2-4B`, `TencentARC/Pixal3D-T`, DINOv3, RMBG-2.0
- nginx on the public port with basic auth, proxying ComfyUI on localhost

Provisioning takes **~10–20 min**, dominated by the ~25 GB of weights.

### Why the auth proxy

ComfyUI has no authentication and custom nodes execute arbitrary Python. An
open ComfyUI port is remote code execution on a machine billed to your card.
`rig up` generates a random password per launch and prints it in the URL.
`--no-auth` exists; think before using it.

---

## Iterating on provisioning

The Vast `onstart` hook is one line — it curls `provision/bootstrap.sh` from
this repo. So fixing a broken install is:

```bash
git push                       # edit provision/*.sh, push
python rig.py down -y
python rig.py up -y            # picks up the new script
```

No image rebuild, no CLI change. To test a branch before merging:

```bash
python rig.py up --branch my-fix
```

Debug endpoints on the running box:

- `/rig/health` — 404 until provisioning completes (what `rig ready` polls)
- `/rig/log` — full bootstrap log in the browser

---

## Quality notes

Defaults worth knowing when you get to the UI:

- **Pixal3D-T beats TRELLIS.2.** Per the maintainers, the released Pixal3D is
  built on the TRELLIS.2 backbone specifically because it "achieves better
  overall geometry and texture quality." Both are in the loader dropdown — A/B
  them on the same input.
- **Skip `simplify` and decimate in your DCC.** CuMesh's simplify doesn't reach
  `decimation_target` and eats sharp features
  ([CuMesh#28](https://github.com/JeffreyXiang/CuMesh/issues/28)).
- **Keep `dual_contouring_resolution` at 1024** unless you're on the `max`
  tier. 2048 OOMs 24 GB cards outright.
- GLB export at 1536 can throw `WARNING TOO BIG (stack overflow)` from the BVH
  builder. That's a CUDA stack limit, **not** VRAM — more GPU may not fix it.

Quality-first sampler settings: resolution 1536, texture 4096, 50 steps on all
three stages, `ss_guidance 8.0/0.7`, `shape_slat 8.5/0.5`, `tex_slat 2.5/0.2`.

---

## Cost

`status` and `down` both print real numbers.

```
  ID        GPU         STATE    RATE        UP     SPENT    URL
  ---------------------------------------------------------------------
  9182736   RTX A6000   running  $0.410/hr   1h20m  $0.547   http://...
```

Nothing here auto-terminates. If you close the laptop with a box running, you
pay for it. `python rig.py status` is the habit worth building.

---

## Layout

```
rig.py                  CLI entry (stdlib only)
rigkit/
  vast.py               Vast.ai REST client
  tiers.py              GPU profiles + offer quality floor
  launch.py             offer ranking, instance creation, URL resolution
  state.py              local instance tracking (gitignored)
provision/
  bootstrap.sh          remote entry — pulled by onstart
  install_comfy.sh      ComfyUI + Trellis2 nodes + prebuilt wheels
  fetch_models.sh       HF snapshot pre-warm
  serve.sh              ComfyUI + nginx auth proxy
```

No secrets in this repo. Keys travel from your local `.env` into the instance's
environment at launch time only.
