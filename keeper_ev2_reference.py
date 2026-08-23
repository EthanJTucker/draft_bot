"""Keeper EV v2 — fixes from adversarial review:
(1) years_exp in proj files is a CURRENT snapshot -> compute exp as-of-season.
(2) V0 from empirical band medians (actual league bids, same pos, ADP within x/1.6..x*1.6,
    pooled 2023-25) with the log-curve only as fallback (n<6).
(3) Old-RB candidates (exp_2026 >= 8) use an age-matched comparable pool.
EV = S0 + 0.8*OV1 + 0.64*OV2. Transitions use as-of-season buckets.
"""
import json, math, itertools, statistics
from collections import defaultdict

GAMMA = 0.8

def load_proj(yr):
    out = {}
    for x in json.load(open(f"proj_{yr}.json")):
        s = x.get("stats") or {}
        p = x.get("player") or {}
        out[x["player_id"]] = {"adp": s.get("adp_half_ppr"), "pos": p.get("position"),
                               "exp_now": p.get("years_exp")}
    return out

proj = {yr: load_proj(yr) for yr in ["2023", "2024", "2025", "2026"]}
picks = {yr: json.load(open(f"picks_{yr}.json")) for yr in ["2023", "2024", "2025"]}

def exp_at(exp_now, season):          # snapshot fix
    return None if exp_now is None else max(0, exp_now - (2026 - int(season)))

# actual league bids with that season's ADP
bids = []
for yr in ["2023", "2024", "2025"]:
    for p in picks[yr]:
        pr = proj[yr].get(p["player_id"], {})
        if pr.get("adp") in (None, 999.0) or p["metadata"]["position"] not in ("QB","RB","WR","TE"):
            continue
        bids.append((p["metadata"]["position"], pr["adp"], int(p["metadata"]["amount"])))

FIT = {}
for pos in ("QB", "RB", "WR", "TE"):
    xs = [math.log(b[1]) for b in bids if b[0] == pos]; ys = [b[2] for b in bids if b[0] == pos]
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    slope = sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / sum((x-mx)**2 for x in xs)
    FIT[pos] = (my - slope*mx, slope)

def curve(pos, adp):
    if adp in (None, 999.0): return 1.0
    a, b = FIT[pos]; return max(1.0, a + b*math.log(adp))

def room_price(pos, adp):
    """Empirical band median of actual bids; curve fallback."""
    if adp in (None, 999.0): return 1.0
    band = [amt for (p, a, amt) in bids if p == pos and adp/1.6 <= a <= adp*1.6]
    if len(band) >= 6: return float(statistics.median(band))
    return curve(pos, adp)

# transition samples with as-of-season experience
samples = []
for t, t1, t2 in [("2023","2024","2025"), ("2024","2025","2026"), ("2025","2026",None)]:
    for pid, v in proj[t].items():
        if v["pos"] not in ("QB","RB","WR","TE"): continue
        p_t = room_price(v["pos"], v["adp"])
        if p_t < 3.0: continue
        p_1 = room_price(v["pos"], proj[t1].get(pid, {}).get("adp"))
        p_2 = room_price(v["pos"], proj[t2].get(pid, {}).get("adp")) if t2 else None
        samples.append({"pos": v["pos"], "exp": exp_at(v["exp_now"], t), "p": p_t,
                        "r1": p_1/p_t, "r2": (p_2/p_t) if p_2 is not None else None})

def comp_pool(pos, exp26, p0):
    posgrp = {"RB": {"RB"}, "WR": {"WR"}, "TE": {"TE","WR"}, "QB": {"QB"}}[pos]
    old_rb = (pos == "RB" and (exp26 or 0) >= 8)
    young = (exp26 or 0) <= 2
    def match(s, level):
        if s["exp"] is None: return False
        if old_rb:                                   # age-matched: exp>=6 backs (Henry, Kamara, CMC, Barkley...)
            ok = s["pos"] == "RB" and s["exp"] >= (6 if level < 2 else 5)
        elif young:
            ok = (s["pos"] in posgrp or level >= 1) and s["exp"] <= 2
        else:
            ok = (s["pos"] in posgrp or level >= 1) and 3 <= s["exp"] <= 8
        band = (1.6 if level == 0 else 2.2 if level == 1 else 3.0)
        return ok and p0/band <= s["p"] <= p0*band
    for level in range(3):
        pool = [s for s in samples if match(s, level)]
        if len(pool) >= (8 if old_rb else 25): return pool, level
    return pool, 2

CANDS = [
    ("Lamar Jackson","QB",30,2026,"4881"), ("DeVonta Smith","WR",26,2026,"7525"),
    ("Christian McCaffrey","RB",57,2027,"4034"), ("Omarion Hampton","RB",35,2027,"12507"),
    ("Xavier Worthy","WR",20,2027,"11624"), ("Jaylen Waddle","WR",12,2027,"7526"),
    ("Mark Andrews","TE",7,2027,"5012"), ("Khalil Shakir","WR",7,2027,"8134"),
    ("Matthew Stafford","QB",5,2027,"421"), ("Juwan Johnson","TE",5,2027,"7002"),
    ("Jauan Jennings","WR",5,2027,"7049"),
]
P = json.load(open("players_nfl.json"))
for pid, x in P.items():
    if x.get("first_name") == "Rashid" and x.get("last_name") == "Shaheed":
        CANDS.append(("Rashid Shaheed","WR",5,2027,pid))

results = []
for name, pos, c0, last, pid in CANDS:
    v = proj["2026"].get(pid, {})
    v0 = room_price(pos, v.get("adp")); s0 = v0 - c0
    ov1 = ov2 = 0.0; npool = level = 0
    if last == 2027:
        pool, level = comp_pool(pos, v.get("exp_now"), v0)
        npool = len(pool)
        if pool:
            ov1 = sum(max(0.0, v0*s["r1"] - (c0+2)) for s in pool)/len(pool)
            two = [s for s in pool if s["r2"] is not None]
            if len(two) >= 10:
                ov2 = sum(max(0.0, v0*s["r2"] - (c0+4)) for s in two if v0*s["r1"] > c0+2)/len(two)
            else:
                ov2 = 0.5*ov1
    ev = s0 + GAMMA*ov1 + GAMMA**2*ov2
    results.append({"name": name, "pos": pos, "cost": c0, "adp": v.get("adp"),
                    "V0": round(v0,1), "S0": round(s0,1), "OV1": round(ov1,1),
                    "OV2": round(ov2,1), "n": npool, "widen": level,
                    "final": last == 2026, "EV": round(ev,1)})

results.sort(key=lambda r: -r["EV"])
hdr = f"{'player':22s}{'pos':4s}{'cost':>5s}{'V0':>7s}{'S0':>7s}{'OV1':>6s}{'OV2':>6s}{'n':>5s}{'EV':>7s}"
print(hdr); print("-"*len(hdr))
for r in results:
    print(f"{r['name']:22s}{r['pos']:4s}{r['cost']:5d}{r['V0']:7.1f}{r['S0']:7.1f}"
          f"{r['OV1']:6.1f}{r['OV2']:6.1f}{r['n']:5d}{r['EV']:7.1f}{'  (final yr)' if r['final'] else ''}")

best = sorted(itertools.combinations(results, 3), key=lambda c: -sum(x["EV"] for x in c))[:4]
print("\nTop trios:")
for c in best:
    print(f"  {' + '.join(x['name'] for x in c):58s} cost ${sum(x['cost'] for x in c):3d}  EV {sum(x['EV'] for x in c):+.1f}")
pos_only = [r["name"] for r in results if r["EV"] > 0]
print(f"\nPositive-EV: {pos_only}")
print("\nGamma sensitivity:")
for g in (0.6, 0.8, 0.9):
    print(f"  g={g}: " + ", ".join(f"{r['name'].split()[-1]} {r['S0']+g*r['OV1']+g*g*r['OV2']:+.1f}" for r in results[:5]))
json.dump(results, open("keeper_ev2_results.json","w"), indent=1)
