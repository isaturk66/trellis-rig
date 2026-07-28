"""GPU tier profiles.

Tiers are allowlists of Vast.ai `gpu_name` strings rather than pure VRAM
thresholds: TRELLIS.2 / Pixal3D flow models are trained in bf16, which needs
compute capability >= 8.0. Volta (V100) and Turing (RTX 6000, T4) report
plenty of VRAM but fall over with "bf16 is only supported on A100+ GPUs".
Every card listed here is Ampere or newer.
"""

TIERS = {
    # 24-32GB. Runs TRELLIS.2 at its stated minimum. Expect OOM tuning on
    # post-processing (dual contouring, remesh) — the model itself fits fine.
    "cheap": {
        "gpu_names": [
            "RTX 3090",
            "RTX 3090 Ti",
            "RTX 4090",
            "RTX 4090D",
            "RTX 5090",
            "A10",
            "A10G",
        ],
        "min_gpu_ram_mb": 24000,
        "blurb": "24-32GB · runs it, but you'll fight post-processing OOMs",
    },
    # 48GB. The sweet spot: clears the remesh/dual-contouring ceiling that
    # kills 24GB cards, without paying 80GB prices. Default.
    "mid": {
        "gpu_names": [
            "RTX A6000",
            "A40",
            "RTX 6000Ada",
            "L40",
            "L40S",
        ],
        "min_gpu_ram_mb": 46000,
        "blurb": "48GB · clears the post-processing ceiling — the value pick",
    },
    # 80GB. Only needed for 1536-cubed with heavy meshes, where peaks over
    # 63GB have been reported. H100 is the model's reference platform.
    "max": {
        "gpu_names": [
            "A100 PCIE",
            "A100 SXM4",
            "A100X",
            "H100 PCIE",
            "H100 SXM",
            "H100 NVL",
            "H200",
        ],
        "min_gpu_ram_mb": 79000,
        "blurb": "80GB+ · for 1536-cubed hero assets with 63GB+ peaks",
    },
}

DEFAULT_TIER = "mid"

# Baseline sanity filters applied on top of the tier allowlist. The cheapest
# offer on Vast is frequently a 50 Mbit box with 92% uptime — useless when the
# bootstrap pulls ~25GB of weights and the job runs for an hour.
QUALITY_FLOOR = {
    "min_reliability": 0.98,
    "min_inet_down_mbps": 200,
    "min_disk_gb": 100,
    "min_cuda": 12.4,   # TRELLIS.2 needs >= 12.4; the common 12.2 default fails
    "min_direct_ports": 2,
}


def tier_names():
    return list(TIERS.keys())


def resolve(tier):
    if tier not in TIERS:
        raise SystemExit(
            f"unknown tier {tier!r} — pick one of: {', '.join(tier_names())}"
        )
    return TIERS[tier]
