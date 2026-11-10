#!/usr/bin/env python3
"""NQ Signal Desk : ligne de commande.

    python3 desk.py scan --m5 nq_5m.json --h1 nq_1h.json [--block 08:30,10:00] [--risk 200] [--json]
    python3 desk.py backtest --m5 nq_5m.json --h1 nq_1h.json [--set stop_mode=sweep] [--grade A]
    python3 desk.py account --balance 101250 --peak 101800 --day-pnl -150 [--best-day 900]

Ne passe jamais d'ordre. `scan` lit les bougies et dit SIGNAL (A/A+) ou NO TRADE.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import (Desk, load_bars, slice_bars, simulate, size_position, account_status, hm,
                    minute_of_day, POOL_LABEL, NY, PARIS, TICK, rtick)

GRADE_RANK = {"B": 0, "A": 1, "A+": 2}


def parse_set(items):
    cfg = {}
    for it in items or []:
        k, v = it.split("=", 1)
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass
        cfg[k] = v
    return cfg


def paris(d: dt.datetime) -> str:
    return d.astimezone(PARIS).strftime("%H:%M")


def to_paris(desk: Desk, st) -> dict:
    """Mêmes heures que st.times, converties en heure de Paris (changements d'heure inclus)."""
    out = {}
    idx = {"chasse": st.sweep_idx, "cassure": st.mss_idx, "fvg": st.box_idx, "signal": st.known_idx}
    for k, i in idx.items():
        d = desk.dts[i] + dt.timedelta(seconds=desk.b.secs)
        out[k] = paris(d)
    return out


def blocked(times_et: list, events: list) -> str | None:
    """Règle annonces : rien de 5 min avant à 15 min après une annonce forte (heure NY)."""
    for ev in events:
        e = hm(ev)
        for t in times_et:
            if e - 5 <= t <= e + 15:
                return ev
    return None


# ----------------------------------------------------------------------------- scan

def cmd_scan(a):
    b5 = load_bars(a.m5)
    h1 = load_bars(a.h1) if a.h1 else None
    if a.now:
        cut = dt.datetime.fromisoformat(a.now).replace(tzinfo=NY).timestamp()
        n = sum(1 for t in b5.t if t + b5.secs <= cut)
        b5 = slice_bars(b5, n)
    else:
        # la dernière bougie est encore en formation : on l'ignore
        b5 = slice_bars(b5, len(b5) - 1)
    sb = load_bars(a.struct) if a.struct else None
    desk = Desk(b5, h1, parse_set(a.set), sb)
    k = len(desk.days) - 1
    last = len(b5) - 1
    now_et = desk.dts[last] + dt.timedelta(seconds=b5.secs)
    ctx = desk.context(k)
    pools = desk.pools_for_day(k)
    events = [x for x in (a.block or "").split(",") if x]
    report = {"heure_ny": now_et.strftime("%Y-%m-%d %H:%M"), "heure_paris": paris(now_et),
              "dernier_prix": b5.c[last], "contexte": ctx, "vwap": rtick(desk.vwap(k, last) or 0),
              "niveaux": intact_levels(desk, k, pools, last),
              "signaux": [], "rejets": []}
    for st in desk.detect_day(k):
        state = live_state(desk, st, last)
        item = {"grade": st.grade, "score": st.score, "sens": "ACHAT" if st.direction == "long" else "VENTE",
                "entree": st.entry, "stop": st.stop, "objectif": st.target, "stop_pts": st.stop_pts,
                "r_brut": st.rr_gross, "niveau_chasse": POOL_LABEL.get(st.pool, st.pool),
                "objectif_niveau": POOL_LABEL.get(st.target_pool, st.target_pool),
                "heures_ny": st.times, "heures_paris": to_paris(desk, st), "raisons": st.reasons, "etat": state,
                "invalidation": st.fvg_top if st.direction == "short" else st.fvg_bottom,
                "taille": size_position(st.stop_pts, abs(st.target - st.entry), a.risk, a.max_contracts)}
        times = [hm(st.times["chasse"]), hm(st.times["cassure"]), hm(st.times["fvg"])]
        ev = blocked(times, events)
        net_ok = item["taille"]["MNQ"]["r_net"] >= desk.cfg["min_rr_net"]
        if GRADE_RANK[st.grade] < GRADE_RANK[a.grade]:
            item["motif"] = f"note {st.grade} ({st.score} pts) insuffisante"
            report["rejets"].append(item)
        elif ev:
            item["motif"] = f"annonce de {ev} NY : fenêtre interdite (-5/+15 min)"
            report["rejets"].append(item)
        elif not net_ok:
            item["motif"] = "R après frais < 1,5"
            report["rejets"].append(item)
        elif state["code"] != "ordre_a_poser":
            item["motif"] = state["texte"]
            report["rejets"].append(item)
        else:
            report["signaux"].append(item)
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(report)
    return 0 if report["signaux"] else 1


def intact_levels(desk: Desk, k: int, pools: list, last: int, max_dist_atr: float = 25.0) -> list:
    """Niveaux de liquidité encore intacts (jamais touchés depuis leur apparition), proches du prix,
    en fusionnant ceux qui sont à moins d'un tick l'un de l'autre."""
    b, day = desk.b, desk.days[k]
    px, atr = b.c[last], desk.atr[last]
    out = []
    for name, lvl, side, av in pools:
        if av > last or abs(lvl - px) > max_dist_atr * atr:
            continue
        lo = max(av, day.start)
        if lo <= last and ((side == "H" and max(b.h[lo:last + 1]) >= lvl + TICK) or
                           (side == "L" and min(b.l[lo:last + 1]) <= lvl - TICK)):
            continue
        out.append((name, rtick(lvl)))
    out.sort(key=lambda x: -x[1])
    merged = []
    for n, p in out:
        if merged and abs(merged[-1][1] - p) <= 2 * TICK:
            if n not in merged[-1][0].split("/"):
                merged[-1] = (merged[-1][0] + "/" + n, merged[-1][1])
        else:
            merged.append((n, p))
    return merged


def live_state(desk: Desk, st, last: int) -> dict:
    """Où en est l'ordre limite à la dernière bougie clôturée."""
    b, cfg = desk.b, desk.cfg
    short = st.direction == "short"
    through = TICK if cfg["fill"] == "through" else 0.0
    age = last - st.known_idx
    for i in range(st.known_idx + 1, last + 1):
        if (short and b.h[i] >= st.entry + through) or (not short and b.l[i] <= st.entry - through):
            return {"code": "deja_declenche", "texte": f"déjà déclenché à {desk.dts[i].strftime('%H:%M')} NY : trop tard"}
        if (short and b.c[i] > st.fvg_top) or (not short and b.c[i] < st.fvg_bottom):
            return {"code": "invalide", "texte": "FVG invalidé (clôture au-delà)"}
    if age >= cfg["max_wait"]:
        return {"code": "expire", "texte": "configuration expirée (ordre limite trop ancien)"}
    end_win = max(hm(e) for s, e in cfg["windows"].values())
    if minute_of_day(desk.dts[last]) >= hm(cfg["flat_by"]):
        return {"code": "expire", "texte": "après l'heure de sortie"}
    return {"code": "ordre_a_poser", "texte": f"ordre limite valable encore {cfg['max_wait'] - age} bougies",
            "valable_jusqu_ny": (desk.dts[st.known_idx] + dt.timedelta(seconds=b.secs * (cfg['max_wait'] + 1))).strftime('%H:%M'),
            "valable_jusqu_paris": paris(desk.dts[st.known_idx] + dt.timedelta(seconds=b.secs * (cfg['max_wait'] + 1)))}


def print_report(r):
    ctx = r["contexte"]
    print(f"NQ Signal Desk : {r['heure_ny']} NY ({r['heure_paris']} Paris), dernier prix {r['dernier_prix']}")
    print(f"Biais : journalier {ctx['bias']['daily']}, hebdo {ctx['bias']['weekly']} | VWAP {r['vwap']}")
    if ctx.get("vp"):
        vp = ctx["vp"]
        print(f"Profil de volume de la veille : POC {vp['poc']}  VAH {vp['vah']}  VAL {vp['val']}")
    above = [f"{n} {p}" for n, p in r["niveaux"] if p > r["dernier_prix"]]
    below = [f"{n} {p}" for n, p in r["niveaux"] if p <= r["dernier_prix"]]
    print("Liquidité au-dessus (intacte) : " + (", ".join(above[-6:]) or "aucune"))
    print("Liquidité en dessous (intacte) : " + (", ".join(below[:6]) or "aucune"))
    if not r["signaux"]:
        print("\nNO TRADE : aucune configuration A/A+ prête.")
    for s in r["signaux"]:
        t = s["taille"]
        print(f"\nSIGNAL {s['grade']} ({s['score']} pts) : {s['sens']} LIMITE {s['entree']} | stop {s['stop']} "
              f"({s['stop_pts']} pts) | objectif {s['objectif']} ({s['r_brut']}R brut)")
        print(f"  Taille : {t['MNQ']['contrats']} MNQ (risque {t['MNQ']['risque_usd']} $, R net {t['MNQ']['r_net']})"
              + (f" ou {t['NQ']['contrats']} NQ" if t['NQ']['contrats'] else ""))
        hp = s["heures_paris"]
        print(f"  Chasse du {s['niveau_chasse']} à {hp['chasse']} Paris, CISD {hp['cassure']}, FVG {hp['fvg']} | "
              f"{s['etat']['texte']} (jusqu'à {s['etat'].get('valable_jusqu_paris', '?')} Paris)")
        print(f"  Invalidation : clôture 5 min au-delà de {s['invalidation']} avant exécution")
        for x in s["raisons"]:
            print("   - " + x)
    for s in r["rejets"]:
        print(f"(écarté) {s['sens']} {s['entree']} note {s['grade']} : {s['motif']}")


# ----------------------------------------------------------------------------- backtest

def run_backtest(desk: Desk, min_grade="A", risk=200.0, windows=None):
    trades, busy_until = [], -1
    for k in range(1, len(desk.days)):
        day_pnl = 0.0
        for st in sorted(desk.detect_day(k), key=lambda x: x.known_idx):
            if GRADE_RANK[st.grade] < GRADE_RANK[min_grade] or st.known_idx <= busy_until:
                continue
            if windows and st.window not in windows:
                continue
            if day_pnl <= -600:
                break
            res = simulate(desk, st, risk)
            if res["result"] in ("annulé", "non exécuté"):
                continue
            busy_until = res["exit_idx"]
            day_pnl += res["pnl"]
            trades.append({**res, "date": st.date, "dir": st.direction, "grade": st.grade, "score": st.score,
                           "window": st.window, "pool": st.pool, "rr": st.rr_gross, "entry": st.entry, "stop_pts": st.stop_pts,
                           "time": desk.dts[res["fill_idx"]].strftime("%H:%M")})
    return trades


def stats(trades, sessions):
    n = len(trades)
    if not n:
        return {"trades": 0, "seances": sessions}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    # intervalle de confiance bootstrap à 95 % sur le R moyen
    rnd = random.Random(7)
    means = sorted(sum(rnd.choice(rs) for _ in rs) / n for _ in range(2000))
    return {"seances": sessions, "trades": n, "reussite": round(len(wins) / n, 3), "r_moyen": round(sum(rs) / n, 3),
            "ic95_r_moyen": [round(means[50], 3), round(means[1949], 3)], "pnl_total": round(eq, 2),
            "facteur_profit": round(gp / gl, 2) if gl else None, "drawdown_max": round(dd, 2)}


def cmd_backtest(a):
    b5 = load_bars(a.m5)
    h1 = load_bars(a.h1) if a.h1 else None
    sb = load_bars(a.struct) if a.struct else None
    desk = Desk(b5, h1, parse_set(a.set), sb)
    trades = run_backtest(desk, a.grade, a.risk, a.windows.split(",") if a.windows else None)
    out = {"parametres": parse_set(a.set), "note_min": a.grade, **stats(trades, len(desk.days) - 1)}
    by = {}
    for t in trades:
        by.setdefault(t["grade"], []).append(t["r"])
    out["r_moyen_par_note"] = {g: [len(v), round(sum(v) / len(v), 3)] for g, v in by.items()}
    if a.json:
        out["detail"] = trades
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if a.list:
            for t in trades:
                print(f"{t['date']} {t['time']} {t['window']:6} {t['dir']:5} {t['grade']:2} sc{t['score']} "
                      f"{t['pool']:4} rr{t['rr']:<5} {t['result']:9} {t['r']:+.2f}R {t['pnl']:+8.2f}$")


def cmd_account(a):
    print(json.dumps(account_status(a.day_pnl, a.peak, a.balance, best_day=a.best_day), ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--m5", required=True)
    s.add_argument("--h1")
    s.add_argument("--struct", help="bougies de structure (5m)")
    s.add_argument("--now", help="heure NY 'AAAA-MM-JJ HH:MM' pour rejouer un moment passé")
    s.add_argument("--block", help="heures NY des annonces fortes, ex. 08:30,10:00")
    s.add_argument("--risk", type=float, default=200.0)
    s.add_argument("--max-contracts", type=int, default=12)
    s.add_argument("--grade", default="A")
    s.add_argument("--set", action="append")
    s.add_argument("--json", action="store_true")
    bt = sub.add_parser("backtest")
    bt.add_argument("--m5", required=True)
    bt.add_argument("--h1")
    bt.add_argument("--struct", help="bougies de structure (5m) pour les sommets/creux égaux")
    bt.add_argument("--grade", default="A")
    bt.add_argument("--risk", type=float, default=200.0)
    bt.add_argument("--windows")
    bt.add_argument("--set", action="append")
    bt.add_argument("--list", action="store_true")
    bt.add_argument("--json", action="store_true")
    ac = sub.add_parser("account")
    ac.add_argument("--balance", type=float, required=True)
    ac.add_argument("--peak", type=float, required=True, help="plus haut solde de fin de journée")
    ac.add_argument("--day-pnl", type=float, default=0.0)
    ac.add_argument("--best-day", type=float, default=0.0)
    a = p.parse_args()
    return {"scan": cmd_scan, "backtest": cmd_backtest, "account": cmd_account}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
