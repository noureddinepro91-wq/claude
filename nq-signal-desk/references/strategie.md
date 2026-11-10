# Fiche stratégie : NQ Signal Desk

Stratégie écrite par nous, à partir des idées (pas du code) de NQ Quant (Judas Swing, FVG,
exécutions pessimistes) et de Futures Edge (guide de l'ouverture de New York, discipline),
combinées au cadre de Nouri : profil de volume en contexte, VWAP en biais, chasse de liquidité
puis FVG en scénario. Le programme `scripts/engine.py` applique ces règles à la lettre.

Toutes les heures sont en heure de New York (NY). Paris = NY + 6 h la plupart de l'année
(+5 h pendant les quelques semaines de décalage des changements d'heure de mars et d'automne).

## 1. Données
- Bougies d'exécution : NQ 5 minutes (`CME_MINI:NQ1!`). Option : 1 minute.
- Bougies de structure : 15 minutes (pour les sommets et creux égaux). Option : 5 minutes si exécution en 1 minute.
- Bougies 1 heure : biais journalier et hebdo (barres Globex reconstruites, 18h00 à 17h00 NY).
- Seules les bougies **clôturées** comptent. La bougie en cours est ignorée.

## 2. Zones de liquidité (niveaux à chasser et objectifs)
| Code | Niveau | Disponible à partir de |
|---|---|---|
| PDH / PDL | plus haut / bas de la veille (journée Globex) | début de journée |
| PWH / PWL | plus haut / bas de la semaine précédente | début de journée |
| ASH / ASL | plus haut / bas de l'Asie (20h00-00h00) | fin de la séance Asie |
| LOH / LOL | plus haut / bas de Londres (02h00-05h00) | fin de la séance Londres |
| ONH / ONL | plus haut / bas de la nuit (18h00-09h30) | 09h30 |
| EQH / EQL | deux sommets (creux) de structure à moins de 0,10 ATR, à 3 bougies d'écart minimum, sur 3 jours | confirmation du 2e pivot |

Un niveau est « intact » tant qu'aucune bougie ne l'a dépassé d'au moins 1 tick depuis qu'il existe.

## 3. La configuration (vente ; l'achat est le miroir exact)
1. **Fenêtre** : Londres 02h00-05h00 ou New York 08h30-11h30 (14h30-17h30 Paris). La configuration doit être complète dans la fenêtre.
2. **Chasse** : une bougie dépasse d'au moins 1 tick un niveau haut intact (PDH, ONH, LOH, EQH…).
3. **Confirmation : CISD** (changement d'état de livraison). Dans les 40 minutes, une bougie **clôture** sous l'ouverture de la première bougie de la jambe haussière qui a porté le prix jusqu'à la chasse.
   (Variante disponible : cassure du dernier pivot, `confirm=mss`. Testée : moins de signaux, pas meilleure.)
4. **FVG** : un déséquilibre baissier de 3 bougies (plus bas de la bougie 1 > plus haut de la bougie 3), d'au moins 0,15 ATR, formé entre la chasse et 2 bougies après la confirmation. On prend le plus récent.
5. **Signal connu** à la clôture de la plus tardive des deux bougies (confirmation, FVG). Jamais avant.
6. **Entrée** : ordre limite au milieu du FVG (CE), arrondi au tick de 0,25.
7. **Stop** : au-dessus du bord haut du FVG + 0,10 ATR. S'il fait moins de 0,75 ATR, il est élargi à 0,75 ATR (sinon le bruit le balaie). S'il dépasse 3 ATR ou 95 points, la configuration est rejetée (95 points = limite pour 1 MNQ avec 200 $ de risque).
8. **Objectif** : 1 tick avant la zone de liquidité basse intacte la plus proche qui offre au moins 2R brut. Aucune ? Pas de signal.
9. **Validité** : l'ordre limite reste valable 60 minutes. Annulé si une bougie clôture au-dessus du FVG avant l'exécution.
10. **Sortie forcée** : 15h45 NY si ni stop ni objectif.

## 4. Note de qualité (seuls A et A+ sont envoyés)
| Critère | Points |
|---|---|
| Biais journalier ET hebdo dans le sens du trade | +2 (un seul, l'autre neutre : +1) |
| Biais journalier ET hebdo contraires | **rejet** |
| Niveau chassé majeur (veille, semaine, nuit, Asie, Londres, sommets égaux) | +1 |
| Plusieurs niveaux chassés en même temps | +1 |
| Bougie de confirmation avec déplacement (corps ≥ 1,5x la moyenne des 20 dernières) | +1 |
| Volume de la bougie de confirmation ≥ 1,5x la moyenne des 20 dernières | +1 |
| Confirmation du bon côté du VWAP de la journée | +1 |
| Entrée en zone premium (vente) / discount (achat) par rapport au milieu de la veille | +1 |
| Chasse au-delà de la VAH (vente) / VAL (achat) du profil de volume de la veille | +1 |
| Objectif à 3R ou plus | +1 |

**A+ : 7 points ou plus. A : 5 ou 6. B : moins de 5 (pas de signal).**

## 5. Filtres avant envoi
- R après frais ≥ 1,5 (frais estimés : 1,50 $ aller-retour par MNQ, 5 $ par NQ, + 1 tick de slippage sur le stop).
- Pas de signal de 5 minutes avant à 15 minutes après une annonce USD à fort impact.
- Une seule position à la fois. Arrêt du jour à -600 $. Arrêt après 3 pertes d'affilée dans la journée.
- Un signal déjà déclenché, invalidé ou expiré n'est **jamais** envoyé (pas d'entrée en retard).

## 6. Résultats des tests (10 novembre 2026), à lire honnêtement
Historique disponible via TradingView : 19 séances en 5 minutes, 55 en 15 minutes. Exécutions
pessimistes : le stop compte en premier si une bougie touche les deux, l'objectif ne compte pas
sur la bougie d'entrée, slippage d'un tick sur les stops.

| Échantillon | Trades | Réussite | R moyen | Intervalle 95 % |
|---|---|---|---|---|
| 4 marchés (NQ, ES, YM, RTY), toutes notes | 38 | 26 % | +0,48 | -0,48 à +1,69 |
| 4 marchés, A/A+ seulement | 23 | 22 % | +0,63 | -0,72 à +2,57 |
| **NQ seul, A/A+ (19 séances 5m)** | **4** | **0 %** | **-1,0** | — |

- Face à des entrées au hasard (même jour, même fenêtre, mêmes distances), la stratégie fait mieux dans plus de 97 % des tirages : le timing apporte quelque chose.
- Mais l'intervalle de confiance inclut zéro, le résultat dépend de quelques gros gagnants (surtout sur YM), et **sur le NQ seul, les 4 signaux A/A+ récents ont perdu**. Plusieurs variantes ont été essayées, ce qui rend le résultat encore moins certain.
- Conclusion : **stratégie en validation**. À suivre en conditions réelles (papier ou très petite taille) sur au moins 30 signaux avant de lui confier l'examen. Pour un vrai test sur plusieurs mois, exporter l'historique 5 minutes depuis TradingView en CSV : `desk.py backtest --m5 fichier.csv` le lit directement.
