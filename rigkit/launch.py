"""Offer search, ranking, instance creation, and URL resolution."""

import secrets
import time

from . import tiers

# Vast's own base image: their SSH injection and port plumbing are already
# wired, and the -py312 variant matches the cp312 Linux wheels ComfyUI-Trellis2
# ships. `devel` because a couple of pip deps still compile against nvcc.
# A plain nvidia/cuda image also works but has no sshd, so `runtype: ssh` is
# a coin flip there.
DEFAULT_IMAGE = (
    "vastai/base-image:cuda-12.8.1-cudnn-devel-ubuntu24.04-py312-2026-06-15"
)

DEFAULT_REPO = "https://github.com/isaturk66/trellis-rig"
DEFAULT_BRANCH = "main"

COMFY_PORT = 8188
PROXY_PORT = 8189  # authenticated front door; what we actually publish


def build_query(tier_name, max_dph=None, floor=None):
    profile = tiers.resolve(tier_name)
    f = dict(tiers.QUALITY_FLOOR)
    if floor:
        f.update(floor)

    query = {
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "num_gpus": {"eq": 1},
        "gpu_name": {"in": profile["gpu_names"]},
        "gpu_ram": {"gte": profile["min_gpu_ram_mb"]},
        "reliability2": {"gte": f["min_reliability"]},
        "inet_down": {"gte": f["min_inet_down_mbps"]},
        "disk_space": {"gte": f["min_disk_gb"]},
        "cuda_max_good": {"gte": f["min_cuda"]},
        "direct_port_count": {"gte": f["min_direct_ports"]},
        "type": "on-demand",
        "order": [["dph_total", "asc"]],
        "limit": 32,
    }
    if max_dph is not None:
        query["dph_total"] = {"lte": max_dph}
    return query


def rank(offers):
    """Cheapest first. Vast's own ordering is advisory, so sort locally too."""
    return sorted(offers, key=lambda o: (o.get("dph_total") or 9e9))


def summarize(offer):
    return {
        "id": offer.get("id"),
        "gpu": offer.get("gpu_name"),
        "vram_gb": round((offer.get("gpu_ram") or 0) / 1024),
        "dph": offer.get("dph_total"),
        "ram_gb": round(offer.get("cpu_ram", 0) / 1024),
        "cpus": offer.get("cpu_cores_effective"),
        "disk_gb": round(offer.get("disk_space") or 0),
        "down_mbps": round(offer.get("inet_down") or 0),
        "reliability": round((offer.get("reliability2") or 0) * 100, 1),
        "cuda": offer.get("cuda_max_good"),
        "where": offer.get("geolocation"),
    }


def build_env(hf_token=None, auth_user=None, auth_pass=None,
              repo=DEFAULT_REPO, branch=DEFAULT_BRANCH, extra=None):
    """Vast wants docker-style flags in a single string.

    Secrets travel here at launch time from the local environment. They are
    never written into the repo.
    """
    parts = [
        f"-p {PROXY_PORT}:{PROXY_PORT}",
        f"-p {COMFY_PORT}:{COMFY_PORT}",
        f"-e RIG_REPO={repo}",
        f"-e RIG_BRANCH={branch}",
        f"-e RIG_COMFY_PORT={COMFY_PORT}",
        f"-e RIG_PROXY_PORT={PROXY_PORT}",
        f"-e OPEN_BUTTON_PORT={PROXY_PORT}",
    ]
    if hf_token:
        parts.append(f"-e HF_TOKEN={hf_token}")
    if auth_user and auth_pass:
        parts.append(f"-e RIG_AUTH_USER={auth_user}")
        parts.append(f"-e RIG_AUTH_PASS={auth_pass}")
    for k, v in (extra or {}).items():
        parts.append(f"-e {k}={v}")
    return " ".join(parts)


def build_onstart(repo=DEFAULT_REPO, branch=DEFAULT_BRANCH):
    """Tiny bootstrap: pull the real provisioning script from the public repo.

    Keeping this one line means iterating on provisioning is a git push, not a
    CLI change or a rebuilt image.
    """
    raw = repo.replace("github.com", "raw.githubusercontent.com").rstrip("/")
    url = f"{raw}/{branch}/provision/bootstrap.sh"
    return (
        "set -o pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "mkdir -p /var/log/rig; "
        # bootstrap.sh tees its own bootstrap.log; keep the outer capture
        # separate so a curl failure is still visible somewhere.
        f"(curl -fsSL {url} -o /root/bootstrap.sh && bash /root/bootstrap.sh) "
        "2>&1 | tee /var/log/rig/onstart.log"
    )


def make_credentials():
    return "rig", secrets.token_urlsafe(18)


def create(vast, offer, disk_gb=120, image=DEFAULT_IMAGE, label="trellis-rig",
           hf_token=None, auth=True, repo=DEFAULT_REPO, branch=DEFAULT_BRANCH):
    auth_user, auth_pass = make_credentials() if auth else (None, None)
    body = {
        "client_id": "me",
        "image": image,
        "disk": disk_gb,
        "label": label,
        "runtype": "ssh",
        "target_state": "running",
        "cancel_unavail": True,
        "env": build_env(hf_token, auth_user, auth_pass, repo, branch),
        "onstart": build_onstart(repo, branch),
    }
    resp = vast.create_instance(offer["id"], body)
    instance_id = resp.get("new_contract") or resp.get("id")
    if not instance_id:
        raise RuntimeError(f"launch failed, API said: {resp}")
    return instance_id, {"auth_user": auth_user, "auth_pass": auth_pass}


def public_url(instance, port=PROXY_PORT, auth_user=None, auth_pass=None):
    """Resolve the externally reachable URL for a mapped container port."""
    ip = instance.get("public_ipaddr")
    ports = instance.get("ports") or {}
    mapping = ports.get(f"{port}/tcp")
    if not ip or not mapping:
        return None
    host_port = mapping[0].get("HostPort")
    if not host_port:
        return None
    ip = ip.strip()
    if auth_user and auth_pass:
        return f"http://{auth_user}:{auth_pass}@{ip}:{host_port}"
    return f"http://{ip}:{host_port}"


def wait_running(vast, instance_id, timeout=900, interval=10, on_tick=None):
    """Block until Vast reports the container running with ports mapped."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        inst = vast.instance(instance_id)
        status = inst.get("actual_status")
        if on_tick:
            on_tick(inst)
        if status == "running" and inst.get("public_ipaddr") and inst.get("ports"):
            return inst
        if status in ("exited", "created_failed"):
            raise RuntimeError(
                f"instance {instance_id} died during startup: "
                f"{inst.get('status_msg') or status}"
            )
        time.sleep(interval)
    raise TimeoutError(f"instance {instance_id} not running after {timeout}s")
