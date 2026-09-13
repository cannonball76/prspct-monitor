# PRSPCT Mining Monitor

Live dashboard + collector for **GPU proof-of-work mint races** (hash-under-target
mints where difficulty climbs with every unit minted).

Built 2026-09-14 during the PRSPCT run, after a night where the miner was healthy
but there was no single place to see *whether it was working, what it was earning,
and how long was left*. This is that place.

```
monitor.py                 collector — reads miner + chain + Vast, writes JSON
dashboard/index.html       the UI — static, polls the JSON every 60s
monitor-config.example.json  copy to monitor-config.json and fill in
dashboard/data/status.json the live output (regenerated each pass)
```

## Run it

```bash
cp monitor-config.example.json monitor-config.json   # fill in your host/port
python3 monitor.py                                   # one pass
python3 monitor.py --watch 300                       # refresh every 5 min
```

Then serve the dashboard (it needs a local server — `fetch` is blocked on `file://`):

```bash
cd dashboard && python3 -m http.server 8787 --bind 127.0.0.1
```

## What it shows

| panel | meaning |
|---|---|
| **Hash rate** | live GH/s from the miner log |
| **GPU util** | per-card utilisation — reveals an oversubscribed host |
| **Progress** | share of *one expected hit* completed |
| **P(≥1 NFT)** | Poisson probability of at least one hit over the remaining credit |
| Best leading zeros | how close the best hash is to target (more = closer) |
| ETA to next hit | hashes-per-hit ÷ our rate |
| Ceiling | total NFTs obtainable if we mined forever (`H·Td·3600/(W·ln2)`) |

The last three are the decision numbers. A ceiling below 0.69 means the **median
outcome is zero NFTs** — no amount of extra runtime fixes that.

## Why difficulty doubling matters

Work per unit doubles roughly every 30 minutes. Your hashrate does not. So the
number of units you can still get **halves every half hour**, and the total is
bounded no matter how long you run:

```
ceiling = H · Td · 3600 / (W · ln2)
```

The practical consequence is that **setup time is not overhead, it is the loss**.
A 25-minute deploy costs more than half a play that a 2-minute deploy would have
kept. Optimise the clock, not the hashrate.

## Reading a shared host

A cheap multi-GPU offer on a marketplace is often cheap **because it is
oversubscribed**. Look at the GPU util panel:

- `~100%` — you have the cards
- `60–70%` — you are sharing them; the rate you measure is a **floor**, not the
  card's capability

Measured on the PRSPCT run: 8× RTX 5090 gave **29.2 GH/s at 63% util** on a shared
host. A single 5090 measured **5.31 GH/s dedicated**. The extrapolation (8 × 5.31 =
42.4) was never observed. **Price per hour is not price per hash** — check
reliability and expect contention on the cheapest listings.

## Design notes

- **Read-only.** The collector never writes to the mining box and never handles a
  key or password. It reads a log, calls a public RPC, and queries a price API.
- **No secrets in the output.** `status.json` contains public chain data and plain
  numbers only — no host, no port, no key path. `monitor-config.json` holds those
  and is gitignored.
- **Config-driven.** Point `spec` at any play spec that has a contract, a progress
  getter, and a work curve. The arithmetic is generic; only the spec changes.
- **Failure-tolerant.** Every collector is wrapped: an unreachable box or a dead
  RPC degrades that panel instead of blanking the dashboard.

## Known gaps

- **8×5090 on a dedicated host is unmeasured.** 29.2 GH/s is a floor (shared host).
  Every ceiling number inherits that uncertainty — up to 45%.
- **4090 / 3090 keccak rates are unmeasured.** Do not price a play on them.
- The 30-minute doubling constant is derived from one measured network mint rate.
  Re-measure per chain; do not assume it carries over.
