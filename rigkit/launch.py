"""Offer search, ranking, instance creation, and URL resolution."""

import base64
import secrets
import time
import urllib.request

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


STORAGE_HOURS_PER_MONTH = 720  # Vast bills storage_cost as $/GB/month


def effective_dph(offer, disk_gb):
    """True hourly cost for the disk WE ask for.

    An offer's `dph_total` bundles storage for whatever default disk the host
    advertises — usually tiny. Requesting 120GB on a $0.33/GB/mo host adds
    $0.055/hr, roughly half again the GPU price. Worse, `storage_cost` varies
    per host, so sorting on the advertised `dph_total` can rank a genuinely
    more expensive box first.
    """
    base = offer.get("dph_base")
    if base is None:  # some payloads only carry the bundled figure
        return offer.get("dph_total") or 9e9
    storage = (offer.get("storage_cost") or 0) * disk_gb / STORAGE_HOURS_PER_MONTH
    return base + storage


def rank(offers, disk_gb=None):
    """Cheapest first, priced for the disk we intend to request."""
    if disk_gb is None:
        return sorted(offers, key=lambda o: (o.get("dph_total") or 9e9))
    return sorted(offers, key=lambda o: effective_dph(o, disk_gb))


def summarize(offer, disk_gb=None):
    return {
        "id": offer.get("id"),
        "gpu": offer.get("gpu_name"),
        "vram_gb": round((offer.get("gpu_ram") or 0) / 1024),
        "dph": (effective_dph(offer, disk_gb) if disk_gb
                else offer.get("dph_total")),
        "dph_gpu": offer.get("dph_base"),
        "ram_gb": round(offer.get("cpu_ram", 0) / 1024),
        "cpus": offer.get("cpu_cores_effective"),
        "disk_gb": round(offer.get("disk_space") or 0),
        "down_mbps": round(offer.get("inet_down") or 0),
        "reliability": round((offer.get("reliability2") or 0) * 100, 1),
        "cuda": offer.get("cuda_max_good"),
        "where": offer.get("geolocation"),
    }


def build_env(hf_token=None, auth_user=None, auth_pass=None,
              repo=DEFAULT_REPO, branch=DEFAULT_BRANCH, dinov3_url=None,
              extra=None):
    """Build the `env` object for instance creation.

    Vast wants a DICT here, not the docker-flag string the docs show — a
    string gets a flat `invalid env arguments` 400 regardless of content
    (verified 2026-07-29: even `-e FOO=bar` is rejected). Port publishing is
    expressed as a `-p host:container` *key* with a dummy value.

    Secrets travel here at launch time from the local environment. They are
    never written into the repo.
    """
    env = {
        f"-p {PROXY_PORT}:{PROXY_PORT}": "1",
        f"-p {COMFY_PORT}:{COMFY_PORT}": "1",
        "OPEN_BUTTON_PORT": str(PROXY_PORT),
        "RIG_REPO": repo,
        "RIG_BRANCH": branch,
        "RIG_COMFY_PORT": str(COMFY_PORT),
        "RIG_PROXY_PORT": str(PROXY_PORT),
    }
    if hf_token:
        env["HF_TOKEN"] = hf_token
    if dinov3_url:
        # The API accepts the raw url fine, but Vast renders env into shell
        # context on the host; base64 keeps the CloudFront signature's &, =
        # and ~ from being interpreted anywhere downstream.
        env["DINOV3_URL_B64"] = base64.b64encode(
            dinov3_url.strip().encode()).decode()
    if auth_user and auth_pass:
        env["RIG_AUTH_USER"] = auth_user
        env["RIG_AUTH_PASS"] = auth_pass
    env.update(extra or {})
    return env


def resolve_ref(repo=DEFAULT_REPO, branch=DEFAULT_BRANCH):
    """Resolve a branch to its current commit SHA on GitHub.

    Branch-named raw.githubusercontent.com URLs are CDN-cached for minutes, so
    `git push && rig up` can silently provision the PREVIOUS commit — which
    burned a full launch here, debugging a fix that was never running. Commit
    SHAs are immutable and served fresh, and they also pin exactly which code
    built any given box.
    """
    owner_repo = repo.rstrip("/").split("github.com/")[-1]
    api = f"https://api.github.com/repos/{owner_repo}/commits/{branch}"
    req = urllib.request.Request(api, headers={
        "Accept": "application/vnd.github.sha",
        "User-Agent": "trellis-rig",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode().strip()


def build_onstart(repo=DEFAULT_REPO, branch=DEFAULT_BRANCH, ref=None):
    """Tiny bootstrap: pull the real provisioning script from the public repo.

    Keeping this one line means iterating on provisioning is a git push, not a
    CLI change or a rebuilt image.
    """
    raw = repo.replace("github.com", "raw.githubusercontent.com").rstrip("/")
    url = f"{raw}/{ref or branch}/provision/bootstrap.sh"
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
           hf_token=None, auth=True, repo=DEFAULT_REPO, branch=DEFAULT_BRANCH,
           dinov3_url=None, ref=None):
    auth_user, auth_pass = make_credentials() if auth else (None, None)
    body = {
        "client_id": "me",
        "image": image,
        "disk": disk_gb,
        "label": label,
        "runtype": "ssh",
        "target_state": "running",
        "cancel_unavail": True,
        "env": build_env(hf_token, auth_user, auth_pass, repo, ref or branch,
                         dinov3_url),
        "onstart": build_onstart(repo, branch, ref),
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
