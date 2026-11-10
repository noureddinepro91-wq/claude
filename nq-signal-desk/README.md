# NQ Signal Desk

Skill Claude de signaux NQ/MNQ pour le compte Earn2Trade TCP100 de Nouri.
Donne des signaux A/A+ ou NO TRADE. **Ne passe jamais d'ordre.**

- `SKILL.md` : instructions du skill (déroulé, format des signaux, règles de risque).
- `references/strategie.md` : règles exactes de la stratégie et résultats des tests.
- `scripts/engine.py` : moteur de détection (Python standard, sans réseau).
- `scripts/desk.py` : `scan` (analyse en direct), `backtest`, `account`.
- `scripts/research.py` : banc d'essai multi-marchés (NQ, ES, YM, RTY) et test contre le hasard.
- `tests/` : tests automatiques, dont le test anti-triche (aucune bougie future utilisée).
- `data/` : historiques TradingView utilisés pour les tests du 10 novembre 2026.

```bash
python3 -m pytest -q tests
python3 scripts/desk.py backtest --m5 data/nq_5m.json --struct data/nq_15m.json --h1 data/nq_1h.json --grade A --list
python3 scripts/research.py --grade A --by sym,tf --baseline
```

Stratégie en validation : voir la section 6 de `references/strategie.md`.
