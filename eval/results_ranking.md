# Ranking evaluation (precision@20)

Dataset date 2026-09-27 · 2,046 clean leads · 30 planted hot leads · 16 planted decoys · target ≥ 80%

| Weights | **precision@20** | strict (planted only) | stale hidden | decoys in top 20 | planted hot in top 50 | median planted-hot rank |
|---|---|---|---|---|---|---|
| Balanced (default) | **95%** | 75% | 95% | 0 | 24/30 | 20 |
| Close this quarter | **100%** | 85% | 100% | 0 | 25/30 | 16 |
| Engagement first | **85%** | 60% | 85% | 0 | 23/30 | 24 |
| Big deals only | **95%** | 90% | 95% | 0 | 25/30 | 16 |

Organic (not planted) leads that pass the hot checklist: 10. They count as truly hot in the main column and as misses in the strict column.

## Default weights – top 20

| rank | lead | company | score | truth |
|---|---|---|---|---|
| 1 | L02156 | Slater Group | 101.5 | hot_strong |
| 2 | L02141 | Browning Corp | 100.0 | hot_strong |
| 3 | L02142 | Garcia LLC | 98.0 | hot_strong |
| 4 | L02167 | Nanda Technologies | 96.5 | hot_strong |
| 5 | L02152 | Gray Inc. | 96.3 | hot_strong |
| 6 | L02146 | Vala Group | 93.5 | hot_strong |
| 7 | L01855 | Brown Corp | 92.8 | organic (passes checklist) |
| 8 | L02157 | Bullock Solutions | 92.0 | hot_strong |
| 9 | L02147 | French Technologies | 87.6 | hot_strong |
| 10 | L02166 | Dutt Group | 87.5 | hot_strong |
| 11 | L02162 | Reese Pvt Ltd | 87.0 | hot_strong |
| 12 | L02151 | Parmer Ltd | 85.5 | hot_strong |
| 13 | L01060 | Alexander Ltd | 83.3 | organic |
| 14 | L01063 | Lane Pvt Ltd | 83.2 | organic (passes checklist) |
| 15 | L00496 | Keer Pvt Ltd | 83.0 | organic (passes checklist) |
| 16 | L02153 | Simmons LLC | 82.7 | hot_medium |
| 17 | L02161 | Mckenzie LLC | 82.2 | hot_strong |
| 18 | L02163 | Daniel Ltd | 82.2 | hot_medium |
| 19 | L02168 | Weeks Corp | 82.2 | hot_medium |
| 20 | L00126 | Gutierrez Corp | 81.9 | organic (passes checklist) |

## Where the decoys rank (default weights)

| rank | lead | decoy type | score |
|---|---|---|---|
| 29 | L02172 | lost_to_competitor | 76.9 |
| 30 | L02180 | lost_to_competitor | 76.9 |
| 31 | L02184 | lost_to_competitor | 76.9 |
| 40 | L02176 | lost_to_competitor | 70.7 |
| 128 | L02171 | big_but_unreachable | 50.0 |
| 129 | L02179 | big_but_unreachable | 50.0 |
| 196 | L02175 | big_but_unreachable | 45.0 |
| 197 | L02183 | big_but_unreachable | 45.0 |
| 415 | L02178 | slipping_deal | 35.0 |
| 416 | L02182 | slipping_deal | 35.0 |
| 435 | L02173 | email_opener | 34.0 |
| 463 | L02185 | email_opener | 32.5 |
| 568 | L02186 | slipping_deal | 30.0 |
| 572 | L02174 | slipping_deal | 29.9 |
| 582 | L02177 | email_opener | 29.5 |
| 611 | L02181 | email_opener | 28.0 |
