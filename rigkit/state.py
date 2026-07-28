"""Local, gitignored record of what we've launched.

Only convenience data (instance id, when it started, what it cost per hour) so
`rig url` / `rig down` work without the user pasting ids around. Never secrets.
"""

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "state.json")


def load():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save(data):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def record_launch(instance_id, offer, opts):
    data = load()
    data["current"] = {
        "instance_id": instance_id,
        "launched_at": time.time(),
        "gpu_name": offer.get("gpu_name"),
        "dph": offer.get("dph_total"),
        "geolocation": offer.get("geolocation"),
        "tier": opts.get("tier"),
        "auth_user": opts.get("auth_user"),
        "auth_pass": opts.get("auth_pass"),
    }
    save(data)
    return data["current"]


def current():
    return load().get("current")


def clear():
    data = load()
    data.pop("current", None)
    save(data)
