#!/usr/bin/env python3
"""NQ Signal Desk : moteur de détection des signaux NQ/MNQ.

Python standard uniquement. Aucune connexion réseau, aucun ordre : le moteur lit des bougies
(fichiers JSON du connecteur TradingView, format colonnes t/o/h/l/c/v) et renvoie des signaux.

Stratégie (references/strategie.md) : chasse de liquidité -> cassure de structure (clôture du
corps) -> FVG dans le sens de la cassure -> ordre limite au milieu du FVG, stop au-delà du FVG,
objectif sur la prochaine zone de liquidité opposée. Toutes les heures sont en heure de New York.

Règle anti-triche : une configuration n'utilise que des bougies CLÔTURÉES au moment où elle est
connue (bougie du FVG). Les pivots ne comptent qu'une fois confirmés, les niveaux de séance
qu'une fois la séance terminée.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from bisect import bisect_right
from dataclasses import dataclass, field, asdict
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
TICK = 0.25

DEFAULTS = {
    # fenêtres de détection (heure de New York) : la configuration doit être complète dedans
    "windows": {"london": ["02:00", "05:00"], "ny": ["08:30", "11:30"]},
    "flat_by": "15:45",          # sortie forcée (heure NY)
    "pivot_k": 2,                # pivot = plus haut/bas des k bougies de chaque côté
    "swing_lookback_min": 180,   # minutes max entre le pivot cassé et la chasse
    "mss_min": 40,               # minutes max entre la chasse et la cassure de structure
    "confirm": "cisd",           # "cisd" (clôture au-delà de l'ouverture de la jambe qui a chassé) ou "mss" (pivot)
    "fvg_after_mss": 2,          # le FVG peut se former jusqu'à 2 bougies après la cassure
    "atr_n": 14,                 # ATR des bougies de détection : tous les seuils s'y adaptent
    "gap_atr": 0.15,             # taille minimale du FVG = 0,15 ATR
    "wait_min": 60,              # minutes pendant lesquelles l'ordre limite reste valable
    "stop_mode": "fvg",          # "fvg" (au-delà du bord opposé du FVG) ou "sweep" (au-delà de l'extrême de la chasse)
    "buffer_atr": 0.10,          # marge au-delà du stop = 0,10 ATR
    "min_stop_atr": 0.75,        # stop minimum = 0,75 ATR (sinon balayé par le bruit)
    "max_stop_atr": 3.0,         # stop maximum = 3 ATR
    "max_stop_pts": 95.0,        # au-delà, même 1 MNQ dépasse 200 $ de risque
    "min_rr_gross": 2.0,         # R brut minimum vers l'objectif
    "min_rr_net": 1.5,           # R minimum après frais (règle Nouri)
    "max_rr": 0,                 # 0 = objectif sur la zone de liquidité ; sinon plafond en R
    "eq_atr": 0.10,              # deux sommets à moins de 0,10 ATR (structure) = sommets égaux
    "eq_lookback_days": 3,
    "fill": "through",           # "through" = le prix doit dépasser l'entrée d'un tick
    "slip_ticks": 1,             # slippage sur les stops et sorties au marché
    "vp_bin": 2.5,               # pas du profil de volume (points)
    "grade_a": 5, "grade_a_plus": 7,
}

COSTS = {"MNQ": {"pv": 2.0, "fee_rt": 1.50}, "NQ": {"pv": 20.0, "fee_rt": 5.00}}

MAJOR_POOLS = {"PDH", "PDL", "PWH", "PWL", "ONH", "ONL", "ASH", "ASL", "LOH", "LOL"}
POOL_LABEL = {
    "PDH": "plus haut de la veille", "PDL": "plus bas de la veille",
    "PWH": "plus haut de la semaine précédente", "PWL": "plus bas de la semaine précédente",
    "ONH": "plus haut de la nuit", "ONL": "plus bas de la nuit",
    "ASH": "plus haut de l'Asie", "ASL": "plus bas de l'Asie",
    "LOH": "plus haut de Londres", "LOL": "plus bas de Londres",
    "EQH": "sommets égaux", "EQL": "creux égaux",
}


# ----------------------------------------------------------------------------- données

@dataclass
class Bars:
    t: list
    o: list
    h: list
    l: list
    c: list
    v: list
    secs: int

    def __len__(self):
        return len(self.t)


def load_bars(path: str) -> Bars:
    """Lit un fichier du connecteur TradingView (JSON colonnes ou lignes) ou un export CSV de
    TradingView (colonnes time, open, high, low, close, Volume ; time en secondes unix ou ISO)."""
    if path.lower().endswith(".csv"):
        import csv
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        key = {k.lower(): k for k in rows[0]}
        def ts(x):
            try:
                return int(float(x))
            except ValueError:
                return int(dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp())
        b = {"t": [ts(r[key["time"]]) for r in rows]}
        for k, name in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
            b[k] = [float(r[key[name]]) if name in key and r[key[name]] not in ("", "NaN") else 0.0 for r in rows]
    else:
        with open(path) as f:
            d = json.load(f)
        b = d.get("bars", d)
    if isinstance(b, list):
        b = {k: [r[k] for r in b] for k in ("t", "o", "h", "l", "c", "v")}
    t = [int(x) for x in b["t"]]
    order = sorted(range(len(t)), key=lambda i: t[i])
    cols = {k: [float(b[k][i]) for i in order] for k in ("o", "h", "l", "c", "v")}
    t = [t[i] for i in order]
    gaps = sorted(t[i + 1] - t[i] for i in range(min(len(t) - 1, 500)))
    secs = gaps[len(gaps) // 4] if gaps else 300
    return Bars(t, cols["o"], cols["h"], cols["l"], cols["c"], cols["v"], secs)


def slice_bars(b: Bars, end: int) -> Bars:
    return Bars(b.t[:end], b.o[:end], b.h[:end], b.l[:end], b.c[:end], b.v[:end], b.secs)


def et(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, NY)


def hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def minute_of_day(d: dt.datetime) -> int:
    return d.hour * 60 + d.minute


def trading_date(d: dt.datetime) -> dt.date:
    """Journée Globex : de 18h00 (veille) à 17h00 NY. Le dimanche soir appartient au lundi."""
    day = d.date() + dt.timedelta(days=1) if d.hour >= 18 else d.date()
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def rtick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def tick_up(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def tick_down(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 2)


# ----------------------------------------------------------------------------- contexte

@dataclass
class DayInfo:
    date: dt.date
    start: int            # index de la première bougie de la journée
    end: int              # index après la dernière bougie
    high: float = float("nan")
    low: float = float("nan")
    close: float = float("nan")


def index_days(b: Bars) -> tuple[list, list]:
    """Retourne la date de trading de chaque bougie et la liste des journées."""
    tds, days = [], []
    for i, ts in enumerate(b.t):
        td = trading_date(et(ts))
        tds.append(td)
        if not days or days[-1].date != td:
            if days:
                days[-1].end = i
            days.append(DayInfo(td, i, i))
    if days:
        days[-1].end = len(b)
    for d in days:
        d.high = max(b.h[d.start:d.end])
        d.low = min(b.l[d.start:d.end])
        d.close = b.c[d.end - 1]
    return tds, days


def htf_series(hourly: Bars) -> dict:
    """Barres journalières et hebdomadaires (Globex) construites à partir des bougies 1h."""
    tds, days = index_days(hourly)
    daily = [(d.date, hourly.o[d.start], d.high, d.low, d.close) for d in days]
    weeks: dict = {}
    for date, o, h, l, c in daily:
        k = date.isocalendar()[:2]
        if k not in weeks:
            weeks[k] = [date, o, h, l, c]
        else:
            w = weeks[k]
            w[2], w[3], w[4] = max(w[2], h), min(w[3], l), c
    weekly = [tuple(w) for w in weeks.values()]
    return {"daily": daily, "weekly": weekly}


def ema(values: list, n: int) -> list:
    out, k, e = [], 2 / (n + 1), None
    for x in values:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def bias_of(bars: list, n: int) -> str:
    """Haussier si clôture > EMA(n) et EMA montante sur 3 barres ; baissier à l'inverse."""
    if len(bars) < n + 3:
        return "neutre"
    closes = [x[4] for x in bars]
    e = ema(closes, n)
    if closes[-1] > e[-1] and e[-1] > e[-4]:
        return "haussier"
    if closes[-1] < e[-1] and e[-1] < e[-4]:
        return "baissier"
    return "neutre"


def htf_bias(htf: dict, today: dt.date) -> dict:
    d = [x for x in htf["daily"] if x[0] < today]
    wk = today.isocalendar()[:2]
    w = [x for x in htf["weekly"] if x[0].isocalendar()[:2] < wk]
    return {"daily": bias_of(d, 20), "weekly": bias_of(w, 10)}


def volume_profile(b: Bars, i0: int, i1: int, bin_size: float) -> dict | None:
    """Profil de volume (POC, VAH, VAL) : volume de chaque bougie réparti sur son range."""
    if i1 <= i0:
        return None
    lo, hi = min(b.l[i0:i1]), max(b.h[i0:i1])
    nb = int((hi - lo) / bin_size) + 1
    vol = [0.0] * nb
    for i in range(i0, i1):
        a = int((b.l[i] - lo) / bin_size)
        z = int((b.h[i] - lo) / bin_size)
        share = b.v[i] / (z - a + 1)
        for k in range(a, z + 1):
            vol[k] += share
    total = sum(vol)
    if total <= 0:
        return None
    poc = max(range(nb), key=lambda k: vol[k])
    a = z = poc
    acc = vol[poc]
    while acc < 0.7 * total and (a > 0 or z < nb - 1):
        up = vol[z + 1] if z < nb - 1 else -1
        dn = vol[a - 1] if a > 0 else -1
        if up >= dn:
            z += 1
            acc += up
        else:
            a -= 1
            acc += dn
    mid = lambda k: rtick(lo + (k + 0.5) * bin_size)
    return {"poc": mid(poc), "vah": rtick(lo + (z + 1) * bin_size), "val": rtick(lo + a * bin_size)}


def session_range(b: Bars, i0: int, i1: int, start: str, end: str, day: dt.date):
    """Plus haut / plus bas d'une séance [start, end) de la journée de trading `day`,
    et l'index de la dernière bougie de la séance."""
    s, e = hm(start), hm(end)
    hi = lo = None
    last = None
    for i in range(i0, i1):
        d = et(b.t[i])
        m = minute_of_day(d)
        inside = (s <= m < e) if s < e else (m >= s or m < e)
        if inside:
            hi = b.h[i] if hi is None else max(hi, b.h[i])
            lo = b.l[i] if lo is None else min(lo, b.l[i])
            last = i
    return hi, lo, last


def pivots(b: Bars, i0: int, i1: int, k: int):
    """Pivots confirmés : (index du pivot, index de confirmation, prix, 'H' ou 'L')."""
    out = []
    for i in range(max(i0, k), min(i1, len(b) - k)):
        win_h = b.h[i - k:i + k + 1]
        win_l = b.l[i - k:i + k + 1]
        if b.h[i] == max(win_h) and b.h[i] > max(b.h[i - k:i]):
            out.append((i, i + k, b.h[i], "H"))
        if b.l[i] == min(win_l) and b.l[i] < min(b.l[i - k:i]):
            out.append((i, i + k, b.l[i], "L"))
    return out


# ----------------------------------------------------------------------------- détection

@dataclass
class Setup:
    direction: str                 # "long" | "short"
    date: str
    window: str
    pool: str
    pool_level: float
    sweep_idx: int
    sweep_extreme: float
    mss_idx: int
    mss_level: float
    box_idx: int                   # bougie qui complète le FVG
    fvg_top: float
    fvg_bottom: float
    entry: float
    stop: float
    target: float
    target_pool: str
    stop_pts: float
    rr_gross: float
    known_idx: int = -1            # max(cassure, FVG) : la configuration n'existe qu'à la clôture de cette bougie
    score: int = 0
    grade: str = "B"
    reasons: list = field(default_factory=list)
    extra_pools: list = field(default_factory=list)
    times: dict = field(default_factory=dict)


class Desk:
    """Construit le contexte de chaque journée et détecte les configurations."""

    def __init__(self, b5: Bars, h1: Bars | None = None, cfg: dict | None = None, struct: Bars | None = None):
        """b5 : bougies d'exécution (1m en direct). struct : bougies de structure (5m) pour les
        sommets/creux égaux. h1 : bougies 1h pour le biais journalier et hebdo."""
        self.b = b5
        self.struct = struct
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.tds, self.days = index_days(b5)
        self.day_pos = {d.date: k for k, d in enumerate(self.days)}
        self.htf = htf_series(h1) if h1 is not None and len(h1) else None
        self.bodies = [abs(b5.c[i] - b5.o[i]) for i in range(len(b5))]
        self.dts = [et(x) for x in b5.t]
        bm = max(1, b5.secs // 60)
        self.cfg["swing_lookback"] = max(6, self.cfg["swing_lookback_min"] // bm)
        self.cfg["mss_bars"] = max(3, self.cfg["mss_min"] // bm)
        self.cfg["max_wait"] = max(3, self.cfg["wait_min"] // bm)
        from collections import Counter
        self.why = Counter()   # motifs de rejet (diagnostic)
        self.atr = self._atr(self.cfg["atr_n"])
        sb = struct if struct is not None else b5
        self._satr = self._atr(self.cfg["atr_n"], sb)

    def _atr(self, n, bars=None):
        b, out, a = bars if bars is not None else self.b, [], None
        for i in range(len(b)):
            tr = b.h[i] - b.l[i] if i == 0 else max(b.h[i] - b.l[i], abs(b.h[i] - b.c[i - 1]), abs(b.l[i] - b.c[i - 1]))
            a = tr if a is None else a + (tr - a) / n
            out.append(a)
        return out

    # --- niveaux de liquidité disponibles pour la journée k (avec l'index à partir duquel ils existent)
    def pools_for_day(self, k: int) -> list:
        b, cfg, day = self.b, self.cfg, self.days[k]
        pools = []  # (nom, prix, côté 'H'/'L', index de disponibilité)
        if k >= 1:
            p = self.days[k - 1]
            pools += [("PDH", p.high, "H", day.start), ("PDL", p.low, "L", day.start)]
        # semaine précédente
        wk = day.date.isocalendar()[:2]
        prev = [d for d in self.days[:k] if d.date.isocalendar()[:2] < wk]
        if prev:
            last_wk = prev[-1].date.isocalendar()[:2]
            pw = [d for d in prev if d.date.isocalendar()[:2] == last_wk]
            pools += [("PWH", max(d.high for d in pw), "H", day.start),
                      ("PWL", min(d.low for d in pw), "L", day.start)]
        for name, (s, e) in (("AS", ("20:00", "00:00")), ("LO", ("02:00", "05:00")), ("ON", ("18:00", "09:30"))):
            hi, lo, last = session_range(b, day.start, day.end, s, e, day.date)
            if last is not None and (name != "ON" or minute_of_day(self.dts[last]) >= hm("09:25")):
                pools += [(name + "H", hi, "H", last + 1), (name + "L", lo, "L", last + 1)]
        # sommets / creux égaux (pivots confirmés de l'unité de structure)
        sb = self.struct if self.struct is not None else b
        t0 = b.t[self.days[max(0, k - cfg["eq_lookback_days"])].start]
        t1 = b.t[day.end - 1] + b.secs
        i0 = bisect_right(sb.t, t0 - 1)
        i1 = bisect_right(sb.t, t1)
        pv = pivots(sb, i0, i1, cfg["pivot_k"])
        for side in ("H", "L"):
            pts = [p for p in pv if p[3] == side and p[1] < i1]
            for x in range(len(pts)):
                for y in range(x + 1, len(pts)):
                    tol = cfg["eq_atr"] * self._satr[pts[y][1]]  # ATR connu à la confirmation
                    if abs(pts[x][2] - pts[y][2]) <= tol and pts[y][0] - pts[x][0] >= 3:
                        lvl = max(pts[x][2], pts[y][2]) if side == "H" else min(pts[x][2], pts[y][2])
                        # disponible à la clôture de la bougie de confirmation (en index d'exécution)
                        conf_close = sb.t[pts[y][1]] + sb.secs
                        avail = bisect_right(b.t, conf_close - b.secs)
                        pools.append(("EQ" + side, lvl, side, avail))
        return pools

    def context(self, k: int) -> dict:
        """Contexte de la journée k : biais, profil de volume de la veille, niveaux."""
        day = self.days[k]
        out = {"date": day.date.isoformat()}
        out["bias"] = htf_bias(self.htf, day.date) if self.htf else {"daily": "neutre", "weekly": "neutre"}
        if k >= 1:
            p = self.days[k - 1]
            # profil de volume de la séance régulière (9h30-16h00 NY) de la veille
            idx = [i for i in range(p.start, p.end) if hm("09:30") <= minute_of_day(self.dts[i]) < hm("16:00")]
            out["vp"] = volume_profile(self.b, idx[0], idx[-1] + 1, self.cfg["vp_bin"]) if idx else None
            out["pd_mid"] = rtick((p.high + p.low) / 2)
        else:
            out["vp"], out["pd_mid"] = None, None
        return out

    def vwap(self, k: int, upto: int) -> float | None:
        b, day = self.b, self.days[k]
        num = den = 0.0
        for i in range(day.start, upto + 1):
            tp = (b.h[i] + b.l[i] + b.c[i]) / 3
            num += tp * b.v[i]
            den += b.v[i]
        return num / den if den else None

    def in_window(self, i: int) -> str | None:
        m = minute_of_day(self.dts[i])
        for name, (s, e) in self.cfg["windows"].items():
            if hm(s) <= m < hm(e):
                return name
        return None

    def detect_day(self, k: int, upto: int | None = None) -> list:
        """Toutes les configurations de la journée k connues au plus tard à la bougie `upto`."""
        b, cfg, day = self.b, self.cfg, self.days[k]
        end = day.end if upto is None else min(day.end, upto + 1)
        if end - day.start < 10:
            return []
        ctx = self.context(k)
        pools = self.pools_for_day(k)
        pv = pivots(b, max(0, day.start - 60), end, cfg["pivot_k"])
        setups, seen = [], set()
        for s in range(day.start, end):
            win = self.in_window(s)
            if not win:
                continue
            for direction in ("short", "long"):
                side = "H" if direction == "short" else "L"
                swept = []
                for name, lvl, ps, avail in pools:
                    if ps != side or avail > s:
                        continue
                    lo_i = max(avail, day.start)
                    if direction == "short":
                        hit = b.h[s] >= lvl + TICK and (s == lo_i or max(b.h[lo_i:s]) < lvl + TICK)
                    else:
                        hit = b.l[s] <= lvl - TICK and (s == lo_i or min(b.l[lo_i:s]) > lvl - TICK)
                    if hit:
                        swept.append((name, lvl))
                if not swept:
                    continue
                st = self._build(k, s, direction, swept, pools, pv, end, ctx)
                if st and (st.direction, st.box_idx) not in seen:
                    seen.add((st.direction, st.box_idx))
                    setups.append(st)
        return setups

    def _build(self, k, s, direction, swept, pools, pv, end, ctx):
        b, cfg = self.b, self.cfg
        short = direction == "short"
        # 1. niveau à casser
        if cfg["confirm"] == "cisd":
            level = self._cisd_level(s, short)
            if level is None:
                self.why["pas de jambe à invalider"] += 1
                return None
        else:
            opp = "L" if short else "H"
            cands = [p for p in pv if p[3] == opp and p[1] <= s and s - p[0] <= cfg["swing_lookback"]]
            if not cands:
                self.why["pas de pivot"] += 1
                return None
            level = cands[-1][2]
        # 2. cassure de structure : clôture au-delà du pivot dans les mss_bars bougies
        mss = None
        ext = b.h[s] if short else b.l[s]
        for m in range(s, min(s + cfg["mss_bars"] + 1, end)):
            ext = max(ext, b.h[m]) if short else min(ext, b.l[m])
            if (short and b.c[m] < level) or (not short and b.c[m] > level):
                mss = m
                break
        if mss is None:
            self.why["pas de cassure"] += 1
            return None
        # 3. FVG dans le sens de la cassure, bougie centrale entre la chasse et mss+fvg_after
        box = None
        min_gap = cfg["gap_atr"] * self.atr[mss]
        for i in range(s + 2, min(mss + cfg["fvg_after_mss"] + 1, end)):
            if short and b.l[i - 2] > b.h[i] and b.l[i - 2] - b.h[i] >= min_gap:
                box = (i, b.l[i - 2], b.h[i])
            if not short and b.h[i - 2] < b.l[i] and b.l[i] - b.h[i - 2] >= min_gap:
                box = (i, b.l[i], b.h[i - 2])
        if box is None:
            self.why["pas de FVG"] += 1
            return None
        bi, top, bottom = box
        known = max(bi, mss)
        if not self.in_window(known):
            self.why["FVG hors fenêtre"] += 1
            return None
        entry = rtick((top + bottom) / 2)
        atr = self.atr[known]
        buf = max(2 * TICK, cfg["buffer_atr"] * atr)
        if cfg["stop_mode"] == "sweep":
            far = max(b.h[s:known + 1]) if short else min(b.l[s:known + 1])
        else:
            far = top if short else bottom
        stop = tick_up(far + buf) if short else tick_down(far - buf)
        stop_pts = abs(stop - entry)
        if stop_pts < cfg["min_stop_atr"] * atr:
            # stop élargi au minimum de volatilité plutôt que rejeté
            stop = tick_up(entry + cfg["min_stop_atr"] * atr) if short else tick_down(entry - cfg["min_stop_atr"] * atr)
            stop_pts = abs(stop - entry)
        if stop_pts > min(cfg["max_stop_atr"] * atr, cfg["max_stop_pts"]):
            self.why["stop trop grand"] += 1
            return None
        # 4. objectif : zone de liquidité opposée intacte la plus proche offrant au moins min_rr_gross
        tside = "L" if short else "H"
        targets = []
        for name, lvl, ps, avail in pools:
            if ps != tside or avail > known:
                continue
            lo_i = max(avail, self.days[k].start)
            if short:
                intact = lvl < entry and (known < lo_i or min(b.l[lo_i:known + 1]) > lvl)
            else:
                intact = lvl > entry and (known < lo_i or max(b.h[lo_i:known + 1]) < lvl)
            if intact:
                tgt = tick_down(lvl + TICK) if short else tick_up(lvl - TICK)  # juste avant le niveau
                rr = abs(tgt - entry) / stop_pts
                if rr >= cfg["min_rr_gross"]:
                    targets.append((abs(tgt - entry), tgt, name, rr))
        if not targets:
            self.why["pas d'objectif >= 2R"] += 1
            return None
        _, target, tname, rr = min(targets)
        if cfg["max_rr"] and rr > cfg["max_rr"]:
            rr = cfg["max_rr"]
            target = rtick(entry - rr * stop_pts) if short else rtick(entry + rr * stop_pts)
            tname += f" (plafonné à {cfg['max_rr']}R)"
        st = Setup(direction, self.days[k].date.isoformat(), self.in_window(s) or "", swept[0][0], swept[0][1],
                   s, ext, mss, level, bi, top, bottom, entry, stop, target, tname, stop_pts, round(rr, 2),
                   known_idx=known, extra_pools=[p[0] for p in swept[1:]])
        self._grade(st, k, ctx)
        close = lambda i: (self.dts[i] + dt.timedelta(seconds=b.secs)).strftime("%H:%M")  # heure de clôture
        st.times = {"chasse": close(s), "cassure": close(mss),
                    "fvg": (self.dts[bi] + dt.timedelta(seconds=b.secs)).strftime("%H:%M"),
                    "signal": (self.dts[known] + dt.timedelta(seconds=b.secs)).strftime("%H:%M")}
        return st

    def _cisd_level(self, s: int, short: bool):
        """Ouverture de la première bougie de la jambe qui a porté le prix jusqu'à la chasse.
        Jambe = suite de bougies dans le sens de la chasse (vertes pour une chasse haute) se
        terminant à la bougie de chasse, ou juste avant si la bougie de chasse est déjà opposée."""
        b = self.b
        with_leg = (lambda i: b.c[i] > b.o[i]) if short else (lambda i: b.c[i] < b.o[i])
        end = s if with_leg(s) else s - 1
        if end < 1 or not with_leg(end):
            return None
        j = end
        while j > max(0, s - self.cfg["swing_lookback"]) and with_leg(j - 1):
            j -= 1
        return b.o[j]

    def _grade(self, st: Setup, k: int, ctx: dict):
        """Note de qualité. A+ >= grade_a_plus, A >= grade_a, sinon B (pas de signal)."""
        b, cfg = self.b, self.cfg
        short = st.direction == "short"
        want = "baissier" if short else "haussier"
        against = "haussier" if short else "baissier"
        score, why = 0, []
        bias = ctx["bias"]
        agree = [tf for tf in ("daily", "weekly") if bias[tf] == want]
        oppose = [tf for tf in ("daily", "weekly") if bias[tf] == against]
        if len(oppose) == 2:
            st.score, st.grade = -9, "B"
            st.reasons = ["biais journalier ET hebdo contraires : rejeté"]
            return
        if len(agree) == 2:
            score += 2; why.append("biais journalier et hebdo alignés (+2)")
        elif len(agree) == 1 and not oppose:
            score += 1; why.append(f"biais {'journalier' if agree[0]=='daily' else 'hebdo'} aligné (+1)")
        if st.pool in MAJOR_POOLS or st.pool.startswith("EQ"):
            score += 1; why.append(f"chasse d'un niveau majeur : {POOL_LABEL.get(st.pool, st.pool)} (+1)")
        if st.extra_pools:
            score += 1; why.append("plusieurs niveaux chassés en même temps (+1)")
        avg_body = sum(self.bodies[max(0, st.mss_idx - 20):st.mss_idx]) / max(1, min(20, st.mss_idx))
        if avg_body and self.bodies[st.mss_idx] >= 1.5 * avg_body:
            score += 1; why.append("cassure avec déplacement (corps >= 1,5x la moyenne) (+1)")
        avg_vol = sum(b.v[max(0, st.mss_idx - 20):st.mss_idx]) / max(1, min(20, st.mss_idx))
        if avg_vol and b.v[st.mss_idx] >= 1.5 * avg_vol:
            score += 1; why.append("volume relatif fort sur la cassure (+1)")
        vw = self.vwap(k, st.mss_idx)
        if vw is not None and ((short and b.c[st.mss_idx] < vw) or (not short and b.c[st.mss_idx] > vw)):
            score += 1; why.append(f"cassure du bon côté du VWAP ({rtick(vw)}) (+1)")
        if ctx["pd_mid"] is not None and ((short and st.entry > ctx["pd_mid"]) or (not short and st.entry < ctx["pd_mid"])):
            score += 1; why.append(f"entrée en zone {'premium' if short else 'discount'} de la veille (+1)")
        vp = ctx.get("vp")
        if vp:
            ref = vp["vah"] if short else vp["val"]
            if (short and st.sweep_extreme >= ref) or (not short and st.sweep_extreme <= ref):
                score += 1; why.append(f"chasse au-delà de la {'VAH' if short else 'VAL'} de la veille ({ref}) (+1)")
        if st.rr_gross >= 3:
            score += 1; why.append("objectif à 3R ou plus (+1)")
        st.score, st.reasons = score, why
        st.grade = "A+" if score >= cfg["grade_a_plus"] else ("A" if score >= cfg["grade_a"] else "B")


# ----------------------------------------------------------------------------- risque

def size_position(stop_pts: float, target_pts: float, risk_usd: float = 200.0, max_contracts: int = 12,
                  slip_ticks: int = 1) -> dict:
    """Nombre de contrats pour un risque donné, frais et slippage du stop compris.
    Propose MNQ et, si le risque le permet, l'équivalent NQ."""
    out = {}
    for sym, c in COSTS.items():
        loss_per = stop_pts * c["pv"] + slip_ticks * TICK * c["pv"] + c["fee_rt"]
        n = int(risk_usd // loss_per)
        n = min(n, max_contracts)
        gain_per = target_pts * c["pv"] - c["fee_rt"]
        out[sym] = {"contrats": n, "risque_usd": round(n * loss_per, 2), "gain_usd": round(n * gain_per, 2),
                    "r_net": round(gain_per / loss_per, 2) if loss_per else 0}
    return out


def account_status(day_pnl: float, peak_eod_balance: float, balance: float, profit_target: float = 6000.0,
                   start_balance: float = 100000.0, max_dd: float = 3500.0, daily_limit: float = 2200.0,
                   personal_stop: float = 600.0, best_day: float = 0.0, consistency: float = 0.30) -> dict:
    """État du compte Earn2Trade TCP100 (drawdown de fin de journée suiveur)."""
    floor = peak_eod_balance - max_dd  # drawdown suiveur sur le solde de fin de journée
    total_profit = balance - start_balance
    out = {
        "drawdown_restant": round(balance - floor, 2),
        "perte_jour_restante_e2t": round(daily_limit + min(0.0, day_pnl), 2),
        "perte_jour_restante_perso": round(personal_stop + min(0.0, day_pnl), 2),
        "objectif_restant": round(profit_target - total_profit, 2),
        "arret_du_jour": day_pnl <= -personal_stop,
    }
    if total_profit > 0:
        out["part_meilleur_jour"] = round(max(best_day, day_pnl) / max(total_profit, 1e-9), 3)
        out["alerte_coherence"] = out["part_meilleur_jour"] >= consistency * 0.9
    return out


# ----------------------------------------------------------------------------- simulation

def simulate(desk: Desk, st: Setup, risk_usd=200.0, sym="MNQ") -> dict:
    """Rejoue l'ordre limite puis la sortie, avec des hypothèses pessimistes :
    le stop gagne si une bougie touche stop et objectif, l'objectif ne compte pas sur la bougie
    d'entrée, les stops paient du slippage."""
    b, cfg = desk.b, desk.cfg
    short = st.direction == "short"
    day = desk.days[desk.day_pos[dt.date.fromisoformat(st.date)]]
    flat = hm(cfg["flat_by"])
    through = TICK if cfg["fill"] == "through" else 0.0
    fill = None
    for i in range(st.known_idx + 1, min(st.known_idx + 1 + cfg["max_wait"], day.end)):
        if minute_of_day(desk.dts[i]) >= flat:
            break
        if (short and b.h[i] >= st.entry + through) or (not short and b.l[i] <= st.entry - through):
            fill = i
            break
        if (short and b.c[i] > st.fvg_top) or (not short and b.c[i] < st.fvg_bottom):
            return {"result": "annulé", "r": 0.0}
    if fill is None:
        return {"result": "non exécuté", "r": 0.0}
    slip = cfg["slip_ticks"] * TICK
    exit_px, why, j = None, None, fill
    for j in range(fill, day.end):
        if minute_of_day(desk.dts[j]) >= flat and j > fill:
            exit_px, why = (b.o[j] + slip) if short else (b.o[j] - slip), "heure"
            break
        hit_stop = b.h[j] >= st.stop if short else b.l[j] <= st.stop
        hit_tgt = (b.l[j] <= st.target if short else b.h[j] >= st.target) and j > fill
        if hit_stop:
            exit_px, why = (st.stop + slip) if short else (st.stop - slip), "stop"
            break
        if hit_tgt:
            exit_px, why = st.target, "objectif"
            break
    if exit_px is None:
        exit_px, why = (b.c[day.end - 1] + slip) if short else (b.c[day.end - 1] - slip), "fin"
    pts = (st.entry - exit_px) if short else (exit_px - st.entry)
    c = COSTS[sym]
    size = size_position(st.stop_pts, abs(st.target - st.entry), risk_usd)[sym]
    n = max(size["contrats"], 1)
    pnl = n * (pts * c["pv"] - c["fee_rt"])
    risk = n * (st.stop_pts * c["pv"] + c["fee_rt"])
    return {"result": why, "fill_idx": fill, "exit_idx": j, "pts": round(pts, 2), "contracts": n,
            "pnl": round(pnl, 2), "r": round(pnl / risk, 3), "r_pts": round(pts / st.stop_pts, 3)}
