#!/usr/bin/env python3
"""Banc d'essai : mêmes règles sur NQ, ES, YM, RTY et sur deux couples d'unités de temps
(exécution 5m / structure 15m, exécution 15m / structure 1h). Résultats en R de points
(frais exclus, slippage inclus) pour pouvoir comparer les marchés entre eux.

    python3 research.py --data ../data --set stop_mode="fvg" --grade B
"""
import argparse, json, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Desk, load_bars, simulate
from desk import parse_set, GRADE_RANK

PAIRS = [("5m", "15m"), ("15m", "1h")]
SYMS = ["nq", "es", "ym", "rt"]


def run(data, cfg, min_grade, pairs=PAIRS, syms=SYMS, windows=None):
    trades = []
    for sym in syms:
        for ex, st in pairs:
            pe, ps, ph = (os.path.join(data, f"{sym}_{x}.json") for x in (ex, st, "1h"))
            if not (os.path.exists(pe) and os.path.exists(ps)):
                continue
            desk = Desk(load_bars(pe), load_bars(ph), dict(cfg), load_bars(ps))
            busy = -1
            for k in range(1, len(desk.days)):
                for s in sorted(desk.detect_day(k), key=lambda x: x.known_idx):
                    if GRADE_RANK[s.grade] < GRADE_RANK[min_grade] or s.known_idx <= busy:
                        continue
                    if windows and s.window not in windows:
                        continue
                    r = simulate(desk, s)
                    if r["result"] in ("annulé", "non exécuté"):
                        continue
                    busy = r["exit_idx"]
                    trades.append({"_desk": desk, "_k": k, "stop_pts": s.stop_pts, "sym": sym, "tf": ex, "date": s.date, "dir": s.direction, "grade": s.grade,
                                   "score": s.score, "window": s.window, "pool": s.pool, "rr": s.rr_gross,
                                   "res": r["result"], "r": r["r_pts"]})
    return trades


def random_p(trades, runs=500, seed=5):
    """Part des tirages au hasard (même jour, même fenêtre, même stop et même R visé, sens et
    moment aléatoires) qui font au moins aussi bien que la stratégie."""
    from engine import Setup, rtick
    rnd = random.Random(seed)
    real = sum(t["r"] for t in trades) / len(trades)
    pools = []
    for t in trades:
        d, k = t["_desk"], t["_k"]
        day = d.days[k]
        idx = [i for i in range(day.start, day.end - 1) if d.in_window(i) == t["window"]]
        pools.append(idx)
    better = 0
    for _ in range(runs):
        tot = 0.0
        for t, idx in zip(trades, pools):
            d = t["_desk"]
            i = rnd.choice(idx)
            short = rnd.random() < 0.5
            e, sp = d.b.c[i], t["stop_pts"]
            st = Setup("short" if short else "long", t["date"], t["window"], "RND", e, i, e, i, e, i,
                       e + 1e9 if short else e + 1, e - 1 if short else e - 1e9, rtick(e),
                       rtick(e + sp) if short else rtick(e - sp), rtick(e - sp * t["rr"]) if short else rtick(e + sp * t["rr"]),
                       "RND", sp, t["rr"])
            r = simulate(d, st)
            tot += r.get("r_pts", 0.0) if r["result"] not in ("annulé", "non exécuté") else 0.0
        better += tot / len(trades) >= real
    return round(better / runs, 3)


def summarize(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    rnd = random.Random(3)
    means = sorted(sum(rnd.choice(rs) for _ in rs) / n for _ in range(2000))
    return {"trades": n, "reussite": round(sum(r > 0 for r in rs) / n, 3), "r_moyen": round(sum(rs) / n, 3),
            "ic95": [round(means[50], 3), round(means[1949], 3)]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    p.add_argument("--set", action="append")
    p.add_argument("--grade", default="B")
    p.add_argument("--windows")
    p.add_argument("--by", default="")
    p.add_argument("--baseline", action="store_true")
    a = p.parse_args()
    tr = run(a.data, parse_set(a.set), a.grade, windows=a.windows.split(",") if a.windows else None)
    out = {"global": summarize(tr)}
    if a.baseline and tr:
        out["p_hasard"] = random_p(tr)
    for key in [k for k in a.by.split(",") if k]:
        groups = {}
        for t in tr:
            groups.setdefault(str(t[key]), []).append(t)
        out[key] = {g: summarize(v) for g, v in sorted(groups.items())}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
