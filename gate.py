#!/usr/bin/env python3
"""PoW-race cost gate — GO / NO-GO *before* spending anything.

Answers the only question that matters before a GPU rental:
    does our hashrate earn more per hour than the rental costs?

Needs no GPU, no wallet, no funds. Run this FIRST, always.
The 2026-09-13 PRSPCT loss happened because a rental was bought before
this check ran. This file exists so that never happens again.

Usage:  python3 gate.py play-spec.json
"""
import json
import subprocess
import sys
import time

from eth_utils import keccak


# ---------------------------------------------------------------- RPC

def rpc_call(url, method, params, tmo=25):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params})
    out = subprocess.run(
        ["curl", "-s", "--max-time", str(tmo), "-X", "POST", url,
         "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:200]}


def eth_call(url, to, sig, args=()):
    data = "0x" + keccak(text=sig)[:4].hex() + "".join(
        int(a).to_bytes(32, "big").hex() for a in args)
    r = rpc_call(url, "eth_call", [{"to": to, "data": data}, "latest"])
    if "result" in r and r["result"]:
        return int(r["result"], 16)
    raise RuntimeError("call %s failed: %s" % (sig, r.get("error", r)))


def state_words(url, contract):
    r = rpc_call(url, "eth_call",
                 [{"to": contract, "data": "0x" + keccak(text="state()")[:4].hex()},
                  "latest"])
    if "result" not in r:
        raise RuntimeError("state() failed: %s" % r.get("error"))
    h = r["result"][2:]
    return [int(h[i * 64:(i + 1) * 64], 16) for i in range(len(h) // 64)]


# ---------------------------------------------------------------- metrics

def read_progress(spec, url):
    """current depth/units completed"""
    p = spec["progress"]
    if p.get("source") == "state":
        return state_words(url, spec["contract"])[p["index"]]
    return eth_call(url, spec["contract"], p["fn"], p.get("args", []))


def read_supply(spec, url):
    s = spec["supply"]
    if s.get("source") == "state":
        return state_words(url, spec["contract"])[s["index"]]
    return eth_call(url, spec["contract"], s["fn"], s.get("args", []))


def work_at(spec, url, n):
    """expected hashes to produce one unit at progress n"""
    w = spec["work"]
    if w["derive"] == "from_target":
        t = eth_call(url, spec["contract"], w["fn"], [n] + w.get("extra_args", []))
        if t <= 0:
            return float("inf")
        return (1 << 256) / t
    if w["derive"] == "direct":
        return float(eth_call(url, spec["contract"], w["fn"], [n]))
    raise ValueError("unknown work.derive: %s" % w["derive"])


def floor_usd(spec):
    if "floor_usd_override" in spec:
        return float(spec["floor_usd_override"]), "override"
    f = spec["floor"]
    if f["source"] == "opensea":
        out = subprocess.run(
            ["curl", "-s", "--max-time", "25",
             "https://api.opensea.io/api/v2/collections/%s/stats" % f["slug"],
             "-H", "accept: application/json"],
            capture_output=True, text=True).stdout
        d = json.loads(out)["total"]
        price = float(d["floor_price"])
        sym = d["floor_price_symbol"]
        rate = float(spec.get("quote_usd", {}).get(sym, 1.0))
        return price * rate, "%.4f %s x %.3f" % (price, sym, rate)
    if f["source"] == "fixed":
        return float(f["usd"]), "fixed"
    raise ValueError("unknown floor.source: %s" % f["source"])


# ---------------------------------------------------------------- main

def main(path):
    spec = json.load(open(path))
    url = spec["rpc"]
    contract = spec["contract"]
    hashrate = float(spec["our_hashrate_ghs"]) * 1e9
    rent = float(spec["rent_usd_per_h"])

    print("=" * 66)
    print("PoW COST GATE — %s" % spec.get("name", "unnamed"))
    print("=" * 66)

    n = read_progress(spec, url)
    supply = read_supply(spec, url)
    W = work_at(spec, url, n)
    floor, fsrc = floor_usd(spec)

    units_h = hashrate * 3600 / W
    revenue = units_h * floor
    net = revenue - rent

    import math
    print("  progress        %s / %s  (%s left)" % (n, supply, supply - n))
    print("  work now        2^%.2f = %.4e hashes/unit" % (math.log2(W), W))
    print("  our hashrate    %.1f GH/s" % (hashrate / 1e9))
    print("  floor           $%.4f  (%s)" % (floor, fsrc))
    print("  rent            $%.2f/h" % rent)
    print()
    print("  units/hour      %.3f" % units_h)
    print("  revenue/hour    $%.2f" % revenue)
    print("  NET/hour        $%+.2f" % net)
    print()

    # where does it die, and how long do we have?
    be_n = None
    step = max(1, (supply - n) // 4000)
    m = n
    while m < supply:
        Wm = work_at(spec, url, m)
        if hashrate * 3600 / Wm * floor < rent:
            be_n = m
            break
        m += step
    if be_n is not None:
        print("  break-even at   progress %s  (%s units away)" % (be_n, be_n - n))
    else:
        print("  break-even at   never within remaining supply")

    # measured network rate -> how fast break-even arrives
    try:
        n1 = read_progress(spec, url)
        t1 = time.time()
        time.sleep(30)
        n2 = read_progress(spec, url)
        rate = (n2 - n1) / (time.time() - t1) * 3600
        net_hash = rate * work_at(spec, url, n2) / 3600
        print("  network rate    %.0f units/h  (~%.0f GH/s network)" % (rate, net_hash / 1e9))
        print("  our share       %.3f%%" % (hashrate / net_hash * 100 if net_hash else 0))
        if be_n is not None and rate > 0:
            mins = (be_n - n2) / rate * 60
            print("  TIME TO BE      %.0f min  <-- decision deadline" % mins)
            print()
            if mins < float(spec.get("setup_minutes", 12)):
                print("  VERDICT: NO-GO — window closes before we can deploy "
                      "(%.0f min < %s min setup)" % (mins, spec.get("setup_minutes", 12)))
                return 1
    except Exception as exc:
        print("  (network rate probe failed: %s)" % exc)

    print()
    if net > 0:
        print("  VERDICT: GO — $%+.2f/h, deploy if the window allows" % net)
        return 0
    print("  VERDICT: NO-GO — already negative before any setup cost")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "play-spec.json"))
