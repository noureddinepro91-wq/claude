"""Tests du moteur : absence de triche (aucune bougie future), CISD, taille de position, profil de volume."""
import os, sys, json, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
from engine import Desk, load_bars, slice_bars, size_position, volume_profile, Bars, simulate, account_status

DATA = os.path.join(HERE, "..", "data")


def _desk(n=None):
    b = load_bars(os.path.join(DATA, "nq_5m.json"))
    if n:
        b = slice_bars(b, n)
    return Desk(b, load_bars(os.path.join(DATA, "nq_1h.json")), None, load_bars(os.path.join(DATA, "nq_15m.json")))


def test_no_lookahead():
    """Chaque configuration doit être identique si l'on coupe les données juste après sa bougie FVG."""
    full = _desk()
    found = 0
    for k in range(1, len(full.days)):
        for st in full.detect_day(k):
            cut = _desk(st.known_idx + 1)
            again = [x for x in cut.detect_day(k) if x.box_idx == st.box_idx and x.direction == st.direction]
            assert again, f"configuration du {st.date} absente sans les bougies futures"
            a = again[0]
            assert (a.entry, a.stop, a.target, a.grade) == (st.entry, st.stop, st.target, st.grade)
            found += 1
    assert found > 0


def test_cisd_level():
    # jambe haussière de 3 bougies vertes qui chasse : le niveau CISD est l'ouverture de la 1re
    b = Bars(list(range(0, 600, 60)), [10, 9, 10, 11, 12, 13, 14, 15, 16, 17], [11, 10, 11, 12, 13, 14, 15, 16, 17, 18],
             [9, 8, 9, 10, 11, 12, 13, 14, 15, 16], [9, 9.5, 11, 12, 13, 14, 15, 16, 17, 18], [1] * 10, 60)
    d = Desk(b)
    assert d._cisd_level(4, short=True) == b.o[2] or d._cisd_level(4, short=True) == 9


def test_size_position():
    s = size_position(stop_pts=40, target_pts=100, risk_usd=200)
    # MNQ : 40 pts x 2 $ + 1 tick de slippage (0,5 $) + 1,50 $ de frais = 82 $ -> 2 contrats
    assert s["MNQ"]["contrats"] == 2
    assert s["NQ"]["contrats"] == 0
    assert s["MNQ"]["risque_usd"] <= 200


def test_volume_profile():
    b = Bars([0, 60, 120], [100, 100, 100], [102, 101, 110], [98, 99, 100], [100, 100, 100], [100, 1000, 10], 60)
    vp = volume_profile(b, 0, 3, 1.0)
    assert vp["val"] <= vp["poc"] <= vp["vah"]
    assert 99 <= vp["poc"] <= 101


def test_csv_loader():
    rows = "time,open,high,low,close,Volume\n1794300000,1,2,0.5,1.5,10\n1794300300,1.5,2.5,1,2,20\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(rows)
    b = load_bars(f.name)
    assert len(b) == 2 and b.c == [1.5, 2.0] and b.secs == 300


def test_account():
    a = account_status(day_pnl=-650, peak_eod_balance=101000, balance=100350)
    assert a["arret_du_jour"] is True
    assert a["drawdown_restant"] == 2850
