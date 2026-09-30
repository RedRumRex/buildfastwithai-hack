# Dedupe evaluation (entity resolution)

Data: `data` · 2,186 raw leads → 2,046 clean leads · 140 injected duplicate pairs · target recall ≥ 98%, 0 false merges

| **recall** | **precision** | **false merges** | true pairs merged | pairs merged in total | planted leads merged away |
|---|---|---|---|---|---|
| **100.0%** | **100.0%** | **0** | 140/140 | 140 | 0 |

**PASS**

## Recall by how the duplicate was mangled

Each duplicate has a company, a name and an email mangle, so it is counted once in each group.

| mangle | found | total | recall |
|---|---|---|---|
| company_lower_no_suffix | 24 | 24 | 100% |
| company_new_suffix | 48 | 48 | 100% |
| company_nospace_new_suffix | 26 | 26 | 100% |
| company_upper_new_suffix | 42 | 42 | 100% |
| name_initial | 62 | 62 | 100% |
| name_trailing_space | 36 | 36 | 100% |
| name_upper | 42 | 42 | 100% |
| email_blank | 56 | 56 | 100% |
| email_missing_in_both | 5 | 5 | 100% |
| email_upper | 79 | 79 | 100% |

## Merges by rule

| rule | merges |
|---|---|
| same email | 79 |
| same company + similar name | 61 |

## Missed duplicates

None.

## False merges

None.
