# QA evaluation (text-to-SQL + RAG citations)

Mode: LLM `openai/gpt-oss-120b` · RAG engine: **tfidf** · dataset date 2026-09-27 · 50 SQL questions · 8 notes questions · 3 explanations · runtime 633s

| metric | value | target |
|---|---|---|
| execution accuracy (all 50) | **94%** | ≥ 85% |
| execution accuracy (LLM-answered only, 48) | 98% | |
| fallback rate (rules used) | 4% | low |
| citation validity, raw model output (384 ids) | **100.0%** | 100% |
| citation validity shown to user (after checker) | 100% | 100% |

## SQL questions

| # | question | correct | fallback | note |
|---|---|---|---|---|
| 1 | How many leads are there in total? | ✅ |  |  |
| 2 | How many leads are stale? | ✅ |  |  |
| 3 | How many leads have a bounced email address? | ✅ |  |  |
| 4 | How many leads have an invalid email address? | ✅ |  |  |
| 5 | How many deals are there in total? | ✅ |  |  |
| 6 | How many open deals do we have? | ✅ |  |  |
| 7 | What is the total value of all open deals? | ✅ |  |  |
| 8 | What is our total closed-won revenue? | ✅ |  |  |
| 9 | What is the average size of a closed-won deal? | ✅ |  |  |
| 10 | How many deals are in each stage? | ✅ |  |  |
| 11 | What is the total deal value in each stage? | ✅ |  |  |
| 12 | How many leads are there per industry? | ✅ |  |  |
| 13 | How many leads are there per country? | ✅ |  |  |
| 14 | How many leads came from each source? | ✅ |  |  |
| 15 | What is the open pipeline value per industry? | ✅ |  |  |
| 16 | Show the top 10 leads by priority score. | ✅ |  |  |
| 17 | What are the 5 largest open deals? | ✅ |  |  |
| 18 | Which open deals are worth more than $50,000? | ✅ |  |  |
| 19 | Which open deals are expected to close in the next 30 days? | ✅ |  |  |
| 20 | Which open deals are past their expected close date? | ✅ |  |  |
| 21 | Which open deals have been in the same stage for 45 days or more? | ✅ |  |  |
| 22 | Which deals in Negotiation are worth more than $30,000? | ✅ |  |  |
| 23 | How many deals are in the Negotiation stage? | ✅ |  |  |
| 24 | What is the total value of deals in Proposal Sent? | ✅ |  |  |
| 25 | What is the largest single deal amount? | ✅ |  |  |
| 26 | How many demo requests happened in the last 30 days? | ✅ |  |  |
| 27 | How many activities of each type are there? | ✅ |  |  |
| 28 | Which leads had a meeting in the last 30 days? | ❌ |  | rows gold=175 pred=227 |
| 29 | How many pricing page visits have there been in total? | ✅ |  |  |
| 30 | How many leads have no activity at all? | ✅ |  |  |
| 31 | How many leads have no deals? | ✅ |  |  |
| 32 | How many duplicate records were merged? | ✅ |  |  |
| 33 | How many merges were made by each matching rule? | ✅ |  |  |
| 34 | Which leads have not been contacted in more than 90 days? | ✅ |  |  |
| 35 | What is the average company size per industry? | ✅ |  |  |
| 36 | How many leads work at companies with more than 1000 employees? | ✅ |  |  |
| 37 | How many call notes does each author have? | ❌ | yes | rows gold=4 pred=1 |
| 38 | How many call notes are there in total? | ❌ | yes | rows gold=1 pred=1 |
| 39 | Which leads have at least 10 activities? | ✅ |  |  |
| 40 | How many deals are Closed Lost? | ✅ |  |  |
| 41 | What is the average open deal amount per stage? | ✅ |  |  |
| 42 | Which countries have more than 100 leads? | ✅ |  |  |
| 43 | How many leads were created in the last 90 days? | ✅ |  |  |
| 44 | What is the total open pipeline belonging to stale leads? | ✅ |  |  |
| 45 | How many leads have more than one deal? | ✅ |  |  |
| 46 | What is the average priority score per industry? | ✅ |  |  |
| 47 | Which open deals belong to the top 20 ranked leads? | ✅ |  |  |
| 48 | What is the total value of open deals closing within the next 60 days? | ✅ |  |  |
| 49 | Which industries have more than $1,000,000 in open pipeline? | ✅ |  |  |
| 50 | How many closed-won deals and how much revenue does each industry have? | ✅ |  |  |

## Notes questions (RAG)

| question | engine | hits | ids cited | invalid |
|---|---|---|---|---|
| Who complained about pricing? | RAG (tfidf) · openai/gpt-oss-120b | 8 | 8 | 0 |
| Which customers mentioned a competitor? | RAG (tfidf) · openai/gpt-oss-120b | 8 | 8 | 0 |
| What concerns did customers raise about onboarding or implementation? | RAG (tfidf) · openai/gpt-oss-120b | 8 | 0 | 0 |
| Who said they need budget approval? | RAG (tfidf) · openai/gpt-oss-120b | 8 | 0 | 0 |
| What feedback did we get about integrations? | retrieval (tfidf) | 0 | 0 | 0 |
| Which customers are worried about security? | RAG (tfidf) · openai/gpt-oss-120b | 8 | 1 | 0 |
| Who asked about a free trial or pilot? | RAG (tfidf) · openai/gpt-oss-120b | 8 | 8 | 0 |
| What did customers say about their timelines? | text-to-SQL · openai/gpt-oss-120b | 53 | 25 | 0 |

## Failed SQL

- Q28 Which leads had a meeting in the last 30 days?
  ```sql
  SELECT DISTINCT l.lead_id, l.name, l.company, a.activity_date FROM leads l JOIN activity a USING (lead_id) WHERE a.type = 'meeting' AND a.activity_date >= DATE '2026-09-27' - INTERVAL 30 DAY ORDER BY a.activity_date DESC
  ```
- Q37 How many call notes does each author have?
  ```sql
  SELECT COUNT(*) AS open_deals, SUM(amount_usd) AS open_pipeline_usd, ROUND(AVG(amount_usd)) AS avg_deal_usd FROM deals WHERE stage NOT IN ('Closed Won','Closed Lost')
  ```
- Q38 How many call notes are there in total?
  ```sql
  SELECT COUNT(*) AS open_deals, SUM(amount_usd) AS open_pipeline_usd, ROUND(AVG(amount_usd)) AS avg_deal_usd FROM deals WHERE stage NOT IN ('Closed Won','Closed Lost')
  ```