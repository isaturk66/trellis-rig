"""Minimal Vast.ai REST client — stdlib only, no pip install required."""

import json
import os
import urllib.error
import urllib.request

API_ROOT = "https://console.vast.ai/api/v0"

# Vast is mid-migration. Probed 2026-07-29: creating (PUT /asks/{id}/),
# destroying, fetching a single instance and requesting logs are all still v0
# and answer with semantic errors. Only the instance *list* has moved — v0
# returns HTTP 410 deprecated_endpoint — and v1 has no /bundles/ equivalent,
# so offer search must stay on v0.
API_V1 = "https://console.vast.ai/api/v1"


class VastError(RuntimeError):
    pass


def _load_dotenv():
    """Read KEY=value pairs from a gitignored .env next to the repo root."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def api_key():
    _load_dotenv()
    key = os.environ.get("VAST_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "VAST_API_KEY is not set.\n"
            "  Either  set VAST_API_KEY=...   (PowerShell: $env:VAST_API_KEY='...')\n"
            "  or      write VAST_API_KEY=... into .env at the repo root (gitignored)."
        )
    return key


class Vast:
    def __init__(self, key=None, timeout=60):
        self.key = key or api_key()
        self.timeout = timeout

    def _request(self, method, path, body=None, root=API_ROOT):
        url = f"{root}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise VastError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise VastError(f"{method} {path} -> network error: {exc.reason}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise VastError(f"{method} {path} -> non-JSON response: {raw[:400]}") from None

    # ---- offers -----------------------------------------------------------

    def search_offers(self, query):
        return self._request("POST", "/bundles/", query).get("offers", [])

    # ---- instances --------------------------------------------------------

    def create_instance(self, offer_id, body):
        return self._request("PUT", f"/asks/{offer_id}/", body)

    def instances(self):
        # v0 is retired for this one: HTTP 410 deprecated_endpoint.
        return self._request("GET", "/instances/", root=API_V1).get("instances", [])

    def instance(self, instance_id):
        got = self._request("GET", f"/instances/{instance_id}/")
        # the API has returned both {"instances": {...}} and a bare object
        return got.get("instances", got)

    def destroy(self, instance_id):
        return self._request("DELETE", f"/instances/{instance_id}/")

    def set_state(self, instance_id, state):
        """stopped | running.

        Stopping keeps the disk — and therefore the whole provisioned stack,
        weights included — so a restart skips the 20-40min bootstrap entirely.
        You still pay storage while stopped, so it beats destroy only when
        you'll be back before the storage cost exceeds a fresh provision.
        """
        return self._request("PUT", f"/instances/{instance_id}/",
                             {"state": state})

    def logs(self, instance_id, tail=200):
        return self._request(
            "PUT", f"/instances/request_logs/{instance_id}/", {"tail": str(tail)}
        )
