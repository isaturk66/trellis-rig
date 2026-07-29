#!/usr/bin/env python3
"""trellis-rig — rent the cheapest sane GPU on Vast.ai, provision TRELLIS.2 +
Pixal3D-T under ComfyUI, hand back a URL, and destroy it when you're done.

    python rig.py offers            # what's cheap right now
    python rig.py up                # rent + provision, prints a URL
    python rig.py status            # state, uptime, spend so far
    python rig.py logs              # tail remote provisioning
    python rig.py down              # destroy, print the bill

Stdlib only — nothing to pip install locally.
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

from rigkit import launch, state, tiers
from rigkit.vast import Vast, VastError

# Measured on a real run: ~14GB for torch + ComfyUI + the node stack, ~20GB of
# weights, plus conversion scratch. 80GB leaves comfortable headroom for
# outputs while keeping the storage line off the bill — at $0.33/GB/mo every
# extra 40GB is another ~$0.018/hr whether you use it or not.
DEFAULT_DISK_GB = 80


# ---------------------------------------------------------------- formatting

def money(x):
    return "—" if x is None else f"${x:,.3f}"


def table(rows, headers):
    if not rows:
        return "  (none)"
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = lambda r: "  " + "  ".join(
        str(c).ljust(w) for c, w in zip(r, widths)
    ).rstrip()
    out = [line(headers), "  " + "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def elapsed(seconds):
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


# ------------------------------------------------------------------ commands

def cmd_offers(args):
    vast = Vast()
    query = launch.build_query(args.tier, max_dph=args.max_price)
    offers = launch.rank(vast.search_offers(query), disk_gb=args.disk)
    profile = tiers.resolve(args.tier)

    print(f"\ntier '{args.tier}' — {profile['blurb']}")
    print(f"filters: verified, >={tiers.QUALITY_FLOOR['min_inet_down_mbps']}Mbit down, "
          f">={tiers.QUALITY_FLOOR['min_reliability']:.0%} reliability, "
          f"CUDA >={tiers.QUALITY_FLOOR['min_cuda']}\n")

    if not offers:
        print("  no offers matched. try --tier cheap, or raise --max-price.")
        return 1

    rows = []
    for o in offers[: args.limit]:
        s = launch.summarize(o, disk_gb=args.disk)
        rows.append([
            s["id"], s["gpu"], f"{s['vram_gb']}GB",
            money(s["dph"]) + "/hr", money(s["dph_gpu"]),
            f"{s['ram_gb']}GB", s["cpus"],
            f"{s['down_mbps']}Mb", f"{s['reliability']}%", s["cuda"], s["where"],
        ])
    print(table(rows, ["OFFER", "GPU", "VRAM", "TOTAL", "GPU-ONLY", "RAM",
                       "CPU", "DOWN", "REL", "CUDA", "WHERE"]))
    cheapest = launch.summarize(offers[0], disk_gb=args.disk)
    print(f"\n  TOTAL includes {args.disk}GB of storage — that is often ~half "
          f"again the GPU price,")
    print(f"  and storage_cost varies per host, so it changes the ranking.")
    print(f"\n  cheapest: offer {cheapest['id']} · {cheapest['gpu']} · "
          f"{money(cheapest['dph'])}/hr all-in  →  "
          f"python rig.py up --tier {args.tier}")
    return 0


def cmd_up(args):
    vast = Vast()

    if args.offer:
        offers = [o for o in vast.search_offers(launch.build_query(args.tier))
                  if o["id"] == args.offer]
        if not offers:
            print(f"offer {args.offer} is gone or no longer matches. "
                  f"run `python rig.py offers` again.")
            return 1
        offer = offers[0]
    else:
        found = launch.rank(vast.search_offers(
            launch.build_query(args.tier, max_dph=args.max_price)),
            disk_gb=args.disk)
        if not found:
            print("no offers matched — try --tier cheap or --max-price.")
            return 1
        offer = found[0]

    s = launch.summarize(offer, disk_gb=args.disk)
    storage_dph = (s["dph"] or 0) - (s["dph_gpu"] or 0)
    monthly = (s["dph"] or 0) * 730
    print(f"\n  picking  offer {s['id']}")
    print(f"  gpu      {s['gpu']} · {s['vram_gb']}GB VRAM · {s['where']}")
    print(f"  host     {s['ram_gb']}GB RAM · {s['cpus']} cpu · "
          f"{s['down_mbps']}Mbit down · {s['reliability']}% reliable")
    print(f"  price    {money(s['dph'])}/hr all-in "
          f"({money(s['dph_gpu'])} gpu + {money(storage_dph)} for "
          f"{args.disk}GB disk)")
    print(f"           ≈{money(monthly)}/mo if you forget to run `rig down`")

    if not args.yes:
        if input("\n  launch? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  aborted.")
            return 1

    try:
        ref = launch.resolve_ref(branch=args.branch)
        print(f"  code     {args.branch} @ {ref[:9]} (pinned — raw.github "
              f"caches branch urls for minutes)")
    except Exception as exc:
        print(f"  ! could not resolve {args.branch} to a sha ({exc}); falling "
              f"back to the branch name, which may serve stale provisioning")
        ref = None

    hf_token = os.environ.get("HF_TOKEN", "").strip() or None
    dinov3_url = os.environ.get("DINOV3_URL", "").strip() or None
    if dinov3_url:
        print(f"  dinov3   Meta CDN url present — will convert to HF format")
    elif hf_token:
        print(f"  dinov3   via HF token (needs approved gated access)")
    else:
        print("\n  ! No DINOv3 source. It is the image encoder both models")
        print("    condition on, and every generation will raise without it.")
        print("    Set HF_TOKEN (approved gated access) or DINOV3_URL")
        print("    (signed dinov3.llamameta.net link) and relaunch.")

    instance_id, creds = launch.create(
        vast, offer,
        disk_gb=args.disk,
        image=args.image,
        hf_token=hf_token,
        auth=not args.no_auth,
        branch=args.branch,
        dinov3_url=dinov3_url,
        ref=ref,
    )
    rec = state.record_launch(instance_id, offer, {
        "tier": args.tier, **creds,
    })
    print(f"\n  launched instance {instance_id} — waiting for the container...")

    seen = {"status": None}

    def tick(inst):
        st = inst.get("actual_status") or inst.get("cur_state")
        if st != seen["status"]:
            seen["status"] = st
            print(f"    [{time.strftime('%H:%M:%S')}] {st}"
                  f"{' — ' + inst['status_msg'].strip() if inst.get('status_msg') else ''}")

    try:
        inst = launch.wait_running(vast, instance_id, timeout=args.timeout, on_tick=tick)
    except (TimeoutError, RuntimeError) as exc:
        print(f"\n  ! {exc}")
        print(f"  the instance is still billing. inspect with `python rig.py logs`")
        print(f"  or kill it with `python rig.py down`.")
        return 1

    url = launch.public_url(inst, launch.PROXY_PORT,
                            rec.get("auth_user"), rec.get("auth_pass"))
    print(f"\n  container up. provisioning runs in the background "
          f"(~10-20min: torch, wheels, ~25GB of weights).")
    print(f"  watch it:  python rig.py logs --follow")
    print(f"  poll it:   python rig.py ready")
    if url:
        print(f"\n  URL (once ready):  {url}")
    print(f"\n  when you're done:  python rig.py down\n")
    return 0


def cmd_status(args):
    vast = Vast()
    live = vast.instances()
    rec = state.current()

    if not live:
        print("\n  no instances running. nothing is billing.\n")
        state.clear()
        return 0

    rows = []
    total_dph = 0.0
    for inst in live:
        dph = inst.get("dph_total") or 0
        total_dph += dph
        started = inst.get("start_date")
        up = time.time() - started if started else 0
        url = launch.public_url(inst, launch.PROXY_PORT)
        rows.append([
            inst.get("id"), inst.get("gpu_name"),
            inst.get("actual_status"), money(dph) + "/hr",
            elapsed(up), money(dph * up / 3600),
            url or "(no port yet)",
        ])
    print("\n" + table(rows, ["ID", "GPU", "STATE", "RATE", "UP", "SPENT", "URL"]))
    print(f"\n  burn rate: {money(total_dph)}/hr · {money(total_dph * 24)}/day")

    if rec and rec.get("auth_pass"):
        print(f"  auth: {rec['auth_user']} / {rec['auth_pass']}")
    print()
    return 0


def cmd_url(args):
    vast = Vast()
    rec = state.current()
    iid = args.id or (rec or {}).get("instance_id")
    if not iid:
        print("no tracked instance — pass --id or run `python rig.py status`.")
        return 1
    inst = vast.instance(iid)
    url = launch.public_url(inst, launch.PROXY_PORT,
                            (rec or {}).get("auth_user"), (rec or {}).get("auth_pass"))
    if not url:
        print("no public port mapped yet — the container may still be starting.")
        return 1
    print(url)
    return 0


def cmd_ready(args):
    """Poll the remote health endpoint until provisioning finishes."""
    vast = Vast()
    rec = state.current()
    iid = args.id or (rec or {}).get("instance_id")
    if not iid:
        print("no tracked instance.")
        return 1
    inst = vast.instance(iid)
    base = launch.public_url(inst, launch.PROXY_PORT,
                             (rec or {}).get("auth_user"), (rec or {}).get("auth_pass"))
    if not base:
        print("no public port yet.")
        return 1

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/rig/health", timeout=10) as r:
                if r.status == 200:
                    print(f"\n  ready → {base}\n")
                    return 0
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        print(f"    [{time.strftime('%H:%M:%S')}] still provisioning...")
        time.sleep(args.interval)
    print("timed out waiting. check `python rig.py logs`.")
    return 1


def cmd_logs(args):
    vast = Vast()
    rec = state.current()
    iid = args.id or (rec or {}).get("instance_id")
    if not iid:
        print("no tracked instance.")
        return 1
    seen = 0
    while True:
        try:
            # The API doesn't return log text — it stages a file on S3 and
            # hands back a URL, which lags the request by a few seconds.
            resp = vast.logs(iid, tail=args.tail)
            url = resp.get("result_url")
            if not url:
                print(f"! unexpected logs response: {resp}")
            else:
                time.sleep(3)
                try:
                    with urllib.request.urlopen(url, timeout=30) as r:
                        text = r.read().decode("utf-8", "replace")
                    if args.follow:
                        # only print what's new since last poll
                        print(text[seen:], end="")
                        seen = max(seen, len(text))
                    else:
                        print("\n".join(text.splitlines()[-args.tail:]))
                except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                    print(f"! log not staged yet ({exc}) — retrying")
        except VastError as exc:
            print(f"! {exc}")
        if not args.follow:
            return 0
        time.sleep(args.interval)


def cmd_stop(args):
    """Suspend without losing the ~35GB of provisioned state."""
    vast = Vast()
    rec = state.current()
    iid = args.id or (rec or {}).get("instance_id")
    if not iid:
        print("no tracked instance.")
        return 1
    inst = vast.instance(iid)
    gpu_dph = inst.get("dph_base") or 0
    store_dph = (inst.get("storage_total_cost") or 0)
    vast.set_state(iid, "stopped")
    print(f"\n  stopped {iid}. disk kept, so `rig start` skips provisioning.")
    print(f"  billing drops {money(inst.get('dph_total'))}/hr → "
          f"{money(store_dph)}/hr (storage only)")
    if store_dph:
        breakeven = 0.5 / store_dph  # a fresh provision costs ~30min of GPU
        print(f"  cheaper than destroy+reprovision if you're back within "
              f"~{breakeven:.0f}h; otherwise `rig down`.")
    print(f"  (gpu portion {money(gpu_dph)}/hr is not billed while stopped)\n")
    return 0


def cmd_start(args):
    vast = Vast()
    rec = state.current()
    iid = args.id or (rec or {}).get("instance_id")
    if not iid:
        print("no tracked instance.")
        return 1
    vast.set_state(iid, "running")
    print(f"  starting {iid} — waiting for ports...")
    inst = launch.wait_running(vast, iid, timeout=args.timeout)
    url = launch.public_url(inst, launch.PROXY_PORT,
                            (rec or {}).get("auth_user"),
                            (rec or {}).get("auth_pass"))
    # Vast re-runs onstart when a stopped instance restarts, and bootstrap is
    # written to be re-entrant — the venv, both clones and every model are
    # skipped when already present, so this settles in a couple of minutes
    # rather than the full provision. Poll rather than assume.
    print(f"\n  running. bootstrap re-runs but skips everything already on "
          f"disk — give it a few minutes, then:")
    print(f"    python rig.py ready")
    print(f"  URL: {url}\n")
    return 0


def cmd_down(args):
    vast = Vast()
    live = vast.instances()
    if not live:
        print("\n  nothing running.\n")
        state.clear()
        return 0

    targets = live if args.all else [
        i for i in live
        if i.get("id") == (args.id or (state.current() or {}).get("instance_id"))
    ]
    if not targets:
        print("no matching instance — use --all to nuke everything, or --id.")
        return 1

    total = 0.0
    for inst in targets:
        dph = inst.get("dph_total") or 0
        started = inst.get("start_date")
        up = time.time() - started if started else 0
        total += dph * up / 3600
        print(f"  {inst['id']}  {inst.get('gpu_name')}  up {elapsed(up)}  "
              f"≈{money(dph * up / 3600)}")

    if not args.yes:
        if input(f"\n  destroy {len(targets)} instance(s)? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            print("  kept running — still billing.")
            return 1

    for inst in targets:
        vast.destroy(inst["id"])
        print(f"  destroyed {inst['id']}")
    state.clear()
    print(f"\n  session total ≈{money(total)}. nothing is billing.\n")
    return 0


# --------------------------------------------------------------------- entry

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="rig", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_tier(sp):
        sp.add_argument("--tier", default=tiers.DEFAULT_TIER,
                        choices=tiers.tier_names(),
                        help="GPU class (default: %(default)s)")
        sp.add_argument("--max-price", type=float, default=None,
                        help="skip offers above this $/hr")

    sp = sub.add_parser("offers", help="list cheapest matching offers")
    add_tier(sp)
    sp.add_argument("--limit", type=int, default=12)
    sp.add_argument("--disk", type=int, default=DEFAULT_DISK_GB,
                    help="price storage for this disk size (default: %(default)s GB)")
    sp.set_defaults(func=cmd_offers)

    sp = sub.add_parser("up", help="rent the cheapest offer and provision it")
    add_tier(sp)
    sp.add_argument("--offer", type=int, help="pin a specific offer id")
    sp.add_argument("--disk", type=int, default=DEFAULT_DISK_GB,
                    help="GB (default: %(default)s)")
    sp.add_argument("--image", default=launch.DEFAULT_IMAGE)
    sp.add_argument("--branch", default=launch.DEFAULT_BRANCH,
                    help="repo branch the remote pulls provisioning from")
    sp.add_argument("--timeout", type=int, default=900)
    sp.add_argument("--no-auth", action="store_true",
                    help="skip basic-auth proxy (exposes ComfyUI unauthenticated)")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("status", help="what's running and what it's costing")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("url", help="print the connect URL")
    sp.add_argument("--id", type=int)
    sp.set_defaults(func=cmd_url)

    sp = sub.add_parser("ready", help="poll until provisioning completes")
    sp.add_argument("--id", type=int)
    sp.add_argument("--timeout", type=int, default=2400)
    sp.add_argument("--interval", type=int, default=20)
    sp.set_defaults(func=cmd_ready)

    sp = sub.add_parser("logs", help="fetch remote bootstrap logs")
    sp.add_argument("--id", type=int)
    sp.add_argument("--tail", type=int, default=200)
    sp.add_argument("--follow", action="store_true")
    sp.add_argument("--interval", type=int, default=15)
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("stop", help="suspend, keeping the provisioned disk")
    sp.add_argument("--id", type=int)
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("start", help="resume a stopped instance")
    sp.add_argument("--id", type=int)
    sp.add_argument("--timeout", type=int, default=900)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("down", help="destroy instance(s) and stop billing")
    sp.add_argument("--id", type=int)
    sp.add_argument("--all", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_down)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except VastError as exc:
        print(f"\n  vast api error: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  interrupted — check `python rig.py status`, you may still be billing.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
