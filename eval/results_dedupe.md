# Dedupe evaluation (entity resolution)

Data: `data` · 2,246 raw leads → 2,050 clean leads · 200 injected duplicate pairs · target recall ≥ 98%, 0 false merges

| **recall** (auto-merged) | **precision** | **false merges** | recall incl. review list | true pairs merged | pairs merged in total | planted leads merged away |
|---|---|---|---|---|---|---|
| **98.0%** | **100.0%** | **0** | 100.0% | 196/200 | 196 | 0 |

**PASS**

Possible duplicates listed for review: 12 pairs, of which 4 are real duplicates and 8 are different people with similar names (a person decides).

## Robustness: freshly generated datasets

Same generator and rules, different random seeds (sample data without planted leads).

| seed | duplicate pairs | **recall** | recall incl. review | precision | **false merges** | review pairs |
|---|---|---|---|---|---|---|
| 1 | 200 | **98.5%** | 100.0% | 100.0% | **0** | 11 |
| 2 | 200 | **99.0%** | 100.0% | 100.0% | **0** | 9 |
| 3 | 200 | **99.0%** | 100.0% | 100.0% | **0** | 11 |
| 4 | 200 | **99.5%** | 100.0% | 100.0% | **0** | 14 |
| 5 | 200 | **100.0%** | 100.0% | 100.0% | **0** | 13 |
| 6 | 200 | **98.5%** | 100.0% | 100.0% | **0** | 12 |
| 7 | 200 | **99.5%** | 100.0% | 100.0% | **0** | 13 |
| 8 | 200 | **97.0%** | 100.0% | 100.0% | **0** | 11 |
| 9 | 200 | **98.5%** | 100.0% | 100.0% | **0** | 12 |
| 10 | 200 | **99.0%** | 100.0% | 100.0% | **0** | 9 |

Recall 97.0%–100.0% (mean 98.8%); false merges in total: 0; 9/10 datasets reach the 98% target.

## Recall by how the duplicate was mangled

Each duplicate has a company, a name, an email and a phone mangle, so it is counted once in each group.

| mangle | found | total | recall |
|---|---|---|---|
| company_lower_no_suffix | 31 | 31 | 100% |
| company_new_suffix | 66 | 66 | 100% |
| company_nospace_new_suffix | 31 | 31 | 100% |
| company_renamed | 16 | 20 | 80% |
| company_upper_new_suffix | 52 | 52 | 100% |
| name_initial | 88 | 90 | 98% |
| name_trailing_space | 50 | 51 | 98% |
| name_upper | 58 | 59 | 98% |
| email_blank | 65 | 69 | 94% |
| email_missing_in_both | 5 | 5 | 100% |
| email_new_domain | 10 | 10 | 100% |
| email_typo | 18 | 18 | 100% |
| email_typo_invalid | 11 | 11 | 100% |
| email_upper | 87 | 87 | 100% |
| phone_changed | 65 | 68 | 96% |
| phone_same | 131 | 132 | 99% |

## Merges by rule

| rule | merges |
|---|---|
| same company + similar name | 95 |
| same email | 85 |
| same phone + similar name | 15 |
| similar name + same email name + same firmographics | 1 |

## Missed duplicates

- L00379: Karan Parmer @ Collins Group <karan.parmer@collins.com>  ~  L02195: Karan Parmer @ Buch LLC <>  (company_renamed|name_trailing_space|email_blank|phone_changed)  → in the review list
- L01375: Roger Brown @ Rees Corp <roger.brown@rees.com>  ~  L02183: R. Brown @ Brown Pvt Ltd <>  (company_renamed|name_initial|email_blank|phone_changed)  → in the review list
- L00855: Denise Simpson @ Duncan Pvt Ltd <denise.simpson@duncan.com>  ~  L02147: D. Simpson @ Stephenson LLC <>  (company_renamed|name_initial|email_blank|phone_changed)  → in the review list
- L01178: Kelly Burnett @ Dalal Technologies <kelly.burnett@dalal.com>  ~  L02177: KELLY BURNETT @ Vig Technologies <>  (company_renamed|name_upper|email_blank|phone_same)  → in the review list

## False merges

None.
