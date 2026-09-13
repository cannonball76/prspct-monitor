#!/usr/bin/env python3
"""PRSPCT mining monitor — collects live status into a JSON file for the dashboard.

Runs LOCALLY (on the host that holds the SSH key). It never runs on the mining box,
and it never writes a key or password anywhere. Everything it emits is either public
on-chain data or a plain number.

Usage:
    python3 monitor.py                 # one collection pass
    python3 monitor.py --watch 300     # re-collect every 300s

Output: dashboard/data/status.json  (path from monitor-config.json)
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Optional local helper dir, from config (e.g. a vastlib.py for rental credit).
# Kept out of the repo so no personal path is hardcoded.
sys.path.insert(0, HERE)

CONFIG = os.path.join(HERE, "monitor-config.json")

# difficulty doubles every ~30 min (measured 2026-09-13, see evidence.md)
DOUBLING_HOURS = 0.5


# ------------------------------------------------------------------ helpers

def run(cmd, tmo=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=tmo).stdout
    except Exception:
        return ""


def ssh(cfg, remote_cmd, tmo=40):
    s = cfg["ssh"]
    return run([
        "ssh", "-i", os.path.expanduser(s["key"]),
        "-p", str(s["port"]),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=12",
        "-o", "BatchMode=yes",
        f'{s["user"]}@{s["host"]}', remote_cmd,
    ], tmo=tmo)


# The log goes into a PUBLIC file. Addresses and tx hashes are redacted here,
# generically — never by matching a specific known address, which would itself
# leak it into this source file.
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")

def sanitize(text):
    # Hashes first: a 64-hex string contains a 40-hex prefix, so redacting
    # addresses first would mangle it into a false match.
    text = HASH_RE.sub(lambda m: m.group(0)[:10] + "\u2026", text)
    text = ADDR_RE.sub("0x\u2026redacted\u2026", text)
    return text

def load_cfg():
    cfg = json.load(open(CONFIG))
    # fall back to the harness's own record of the last rental
    last = os.path.join(HERE, "last-instance.json")
    if os.path.exists(last) and not cfg.get("instance_id"):
        cfg["instance_id"] = json.load(open(last)).get("id")
    return cfg


# ------------------------------------------------------------------ collectors

def collect_miner(cfg):
    """Read the remote log + GPU state. Returns dict; never raises."""
    out = {"reachable": False}
    raw = ssh(cfg, (
        f'tail -60 {cfg["remote_log"]} 2>/dev/null; '
        'echo "===PROC==="; pgrep -c -f "prspct_miner_v[5]" 2>/dev/null; '
        'echo "===GPU==="; nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader 2>/dev/null; '
        f'echo "===MINTS==="; grep -c "mint confirmed" {cfg["remote_log"]} 2>/dev/null; '
        f'echo "===LOG==="; tail -18 {cfg["remote_log"]} 2>/dev/null'
    ))
    if not raw.strip():
        return out
    out["reachable"] = True

    log_part, _, rest = raw.partition("===PROC===")
    proc_part, _, rest2 = rest.partition("===GPU===")
    gpu_part, _, rest3 = rest2.partition("===MINTS===")
    mint_part, _, logtail_part = rest3.partition("===LOG===")

    # Sanitized log tail for the dashboard's live-log panel.
    out["log_tail"] = [sanitize(l) for l in logtail_part.strip().splitlines() if l.strip()]

    try:
        out["miner_procs"] = int(proc_part.strip().splitlines()[0])
    except Exception:
        out["miner_procs"] = 0

    utils = []
    for line in gpu_part.strip().splitlines():
        m = re.match(r"\s*(\d+)\s*%", line)
        if m:
            utils.append(int(m.group(1)))
    out["gpu_util"] = utils
    out["gpu_util_avg"] = round(sum(utils) / len(utils), 1) if utils else None
    out["gpu_count"] = len(utils)

    # last "N hashes, R GH/s best=0x..." line
    for line in reversed(log_part.splitlines()):
        m = re.search(r"([\d,]+)\s+hashes,\s*([\d.]+)\s*GH/s\s*best=(0x[0-9a-fA-F.]+)", line)
        if m:
            out["hashes_done"] = int(m.group(1).replace(",", ""))
            out["rate_ghs"] = float(m.group(2))
            out["best_hash"] = m.group(3).rstrip(".")
            break

    # last "mining depth=N ... expected=E hashes" line
    for line in reversed(log_part.splitlines()):
        m = re.search(r"mining depth=(\d+).*?expected=([\d,]+)\s*hashes", line)
        if m:
            out["log_depth"] = int(m.group(1))
            out["log_expected"] = int(m.group(2).replace(",", ""))
            break

    out["errors"] = len([l for l in log_part.splitlines() if "loop error" in l])

    # Mint detection. Count across the WHOLE log, not the tail — otherwise the flag
    # flips back to False once the line scrolls out of the last 60 lines.
    try:
        out["mints"] = int(mint_part.strip().splitlines()[0])
    except Exception:
        out["mints"] = 0
    # case-insensitive: the real log says "valid proof found" and "mint confirmed",
    # which the old case-sensitive /FOUND|minted/ regex silently missed.
    out["found"] = out["mints"] > 0 or bool(
        re.search(r"valid proof found|mint confirmed", log_part, re.I))
    return out


def collect_chain(cfg):
    """Live depth + work-per-hit straight from the contract."""
    out = {}
    try:
        import gate
        spec_path = os.path.join(HERE, cfg["spec"])
        if not os.path.exists(spec_path):  # fall back to the harness
            spec_path = os.path.expanduser(cfg.get("spec_fallback", ""))
            if not spec_path or not os.path.exists(spec_path):
                raise FileNotFoundError(cfg["spec"])
        spec = json.load(open(spec_path))
        url = spec["rpc"]
        depth = gate.read_progress(spec, url)
        work = gate.work_at(spec, url, depth)
        out["depth"] = depth
        out["work_per_hit"] = work
        out["supply"] = gate.read_supply(spec, url)
        out["chain_ok"] = True
    except Exception as exc:
        out["chain_ok"] = False
        out["chain_error"] = str(exc)[:120]
    return out


def collect_vast(cfg):
    out = {}
    try:
        import vastlib
        out["credit"] = round(float(vastlib.credit()), 4)
        inst = vastlib.instance(int(cfg["instance_id"]))
        out["instance_status"] = inst.get("actual_status")
        out["dph_total"] = inst.get("dph_total")
        out["gpu_name"] = inst.get("gpu_name")
        out["num_gpus"] = inst.get("num_gpus")
        out["vast_ok"] = True
    except Exception as exc:
        # Public users have no vastlib: degrade this panel, keep the rest.
        out["vast_ok"] = False
        out["vast_error"] = str(exc)[:120]
    return out


# ------------------------------------------------------------------ math

def compute(cfg, miner, chain, vast):
    """Derive the decision numbers. Pure arithmetic on the collected values."""
    d = {}
    rate = miner.get("rate_ghs")
    work = chain.get("work_per_hit")
    credit = vast.get("credit")
    dph = vast.get("dph_total")

    if rate and work:
        r = rate * 1e9
        d["eta_next_hit_min"] = round(work / r / 60, 1)
        # P(at least one hit) over a horizon T, with difficulty doubling:
        #   lambda = r*Td*3600/(W*ln2) * (1 - 2^(-T/Td))
        Td = DOUBLING_HOURS
        d["ceiling_nfts"] = round(r * Td * 3600 / (work * math.log(2)), 3)

    if credit is not None and dph:
        hours_left = credit / dph
        d["hours_left"] = round(hours_left, 2)
        d["burn_usd_per_h"] = round(dph, 3)
        if rate and work:
            r = rate * 1e9
            Td = DOUBLING_HOURS
            lam = (r * Td * 3600 / (work * math.log(2))) * (1 - 2 ** (-hours_left / Td))
            d["lambda_remaining"] = round(lam, 4)
            d["p_at_least_one"] = round(1 - math.exp(-lam), 3)

    if miner.get("hashes_done") and work:
        d["pct_of_one_hit"] = round(miner["hashes_done"] / work * 100, 1)

    if miner.get("best_hash"):
        try:
            best = int(miner["best_hash"], 16)
            d["best_leading_zeros"] = 256 - best.bit_length()
        except Exception:
            pass
    return d


# ------------------------------------------------------------------ main

def once(cfg):
    miner = collect_miner(cfg)
    chain = collect_chain(cfg)
    vast = collect_vast(cfg)
    derived = compute(cfg, miner, chain, vast)

    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "play": cfg.get("play", "prspct"),
        # instance_id is deliberately NOT published: it is an infra
        # identifier with no analytic value, and this file goes public.
        "miner": miner,
        "chain": chain,
        "vast": vast,
        "derived": derived,
    }

    out = os.path.join(HERE, cfg["out"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(status, fh, indent=2, sort_keys=True)
    os.replace(tmp, out)

    m = miner
    print(f"[{status['generated_at_local']}] "
          f"rate={m.get('rate_ghs','-')} GH/s  "
          f"util={m.get('gpu_util_avg','-')}%  "
          f"depth={chain.get('depth','-')}  "
          f"credit=${vast.get('credit','-')}  "
          f"P>=1={derived.get('p_at_least_one','-')}")
    return status


def publish(out_path, cfg=None):
    """Push the regenerated JSON to the data branch so the dashboard stays live.

    Why a separate branch: GitHub Pages rebuilds on every push to the branch it
    serves and is rate-limited (~10 builds/hour), which adds a minute or more of
    lag. raw.githubusercontent serves any branch straight from git with no build
    step and no such limit, so live data goes to `data` while `main` stays clean.

    Config-gated ({"push": true}) and silent on failure: a push problem must never
    take down the collector. Credentials come from the local askpass helper;
    nothing secret is ever written into the repo.
    """
    try:
        wt = os.path.expanduser((cfg or {}).get("data_worktree", "~/.prspct-data"))
        if not os.path.isdir(wt):
            return  # live publishing not set up on this host; local file is enough
        dst = os.path.join(wt, "docs", "data", "status.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(out_path, dst)
        subprocess.run(["git", "add", "-A"], cwd=wt, timeout=30,
                       capture_output=True, text=True)
        d = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt,
                           timeout=30, capture_output=True, text=True)
        if d.returncode == 0:
            return  # no change
        # Amend rather than append: at a 90s cadence this branch would otherwise
        # accumulate ~40 commits/hour of pure data churn.
        subprocess.run(["git", "commit", "-q", "--amend", "-m", "status: live refresh"],
                       cwd=wt, timeout=30, capture_output=True, text=True)
        # HEAD:data, not `data`: the worktree is a detached checkout, so the
        # local branch ref may point at a stale commit. Force, because amending
        # rewrites that single commit.
        subprocess.run(["git", "push", "-q", "-f", "origin", "HEAD:data"], cwd=wt,
                       timeout=90, capture_output=True, text=True)
    except Exception as exc:
        print("publish skipped:", str(exc)[:80])


def main():
    cfg = load_cfg()
    if cfg.get("helper_dir"):
        sys.path.insert(0, os.path.expanduser(cfg["helper_dir"]))
    if "--watch" in sys.argv:
        every = int(sys.argv[sys.argv.index("--watch") + 1])
        while True:
            try:
                once(cfg)
                if cfg.get("push"):
                    publish(os.path.join(HERE, cfg["out"]), cfg)
            except Exception as exc:
                print("collect failed:", exc)
            time.sleep(every)
    else:
        once(cfg)


if __name__ == "__main__":
    main()
