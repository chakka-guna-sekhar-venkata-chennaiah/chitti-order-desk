# 7. Evals

An AI product is only as good as the way you measure it. This guide explains
what we measure, why, how to run it, and how to use the results to improve.

## 7.1 Approach

1. **Look at real traces first.** Every reference-runtime turn is saved to
   `data/traces/<role>.jsonl` and every eval case to `evals/results/.../results.jsonl`.
   Read the failures before you change a prompt.
2. **Name the failure modes.** Group the failures (see 7.6). Fix the largest group first.
3. **Measure each part on its own.** A bad answer can come from the vision
   model, the matcher, the agent or the SQL. One eval per part shows where
   the problem is.
4. **Check with code, not with another model.** The output of every step is
   structured (JSON, SKU, state). So almost every check is an exact comparison.
   The only extra model in the evals plays the dealer; it never grades.
5. **Some numbers must be zero.** "Stock removed without approval" is not a
   percentage to improve. It is a rule. One failure fails the run.

## 7.2 The four evals

| # | Eval | Tests | Golden set | Needs a key |
|---|---|---|---|---|
| 1 | `run_chit_reader` | Vision model: handwriting -> lines | 15 chitti images + human labels | yes |
| 2 | `run_matching` | Code: line -> SKU, quantity, status | 41 cases in `matching.jsonl` | no |
| 3 | `run_text2sql` | Agent model: owner question -> SQL -> result | 12 questions in `text2sql.jsonl` | yes |
| 4 | `run_conversations` | Agent: full dealer chat with a simulated dealer | 10 scenarios in `conversations.jsonl` | yes |

```bash
python -m evals.run_matching          # offline, seconds
python -m evals.run_chit_reader       # labels: evals/golden/labels/*.checked.json
python -m evals.run_text2sql
python -m evals.run_conversations
python -m evals.report                # -> evals/results/SCORECARD.md
```

Each run saves `results.jsonl` (one line per case, with the reason for a
failure) and `summary.json`, and adds a line to `history.jsonl`. The scorecard
shows the latest numbers and the change since the run before.

## 7.3 Metrics

### Eval 1: chitti reader
| Metric | Definition | Why it matters |
|---|---|---|
| line_recall | labelled lines that the model found | A missed line is a missed sale. |
| line_precision | model lines that are on the paper | An invented line is a wrong delivery. |
| text_similarity | 1 - edit distance / length of `raw_text` | Shows reading quality, even when matching saves the line. |
| qty_accuracy | exact quantity on found lines | The most expensive error. |
| item_accuracy | category + spec + brand all correct | |
| **order_line_accuracy** | after matching: same SKU and quantity as the label | The number the shop cares about. |
| clarify_recall | lines that need a question and got one | Safety. |
| **unsafe_accept_rate** | needed a question, got none, AND wrong | **Must be 0.** A sure, wrong line goes to the owner unnoticed. |
| decision_accuracy | confirm-all vs clarify, per chitti | |

### Eval 2: matcher
Accuracy overall and per expected status (ok, assumed, unclear, unmatched).
Current result: **41 / 41**. A change to `matching.py` or the catalog must keep it.

### Eval 3: text-to-SQL
| Metric | Definition |
|---|---|
| execution_accuracy | the model's query returns the same rows as the reference query (column names and row order ignored) |
| valid_sql_rate | the query ran without error |
| refused_rate | the guard blocked the query (should be 0 for honest questions) |

### Eval 4: conversations
| Metric | Definition |
|---|---|
| task_success_rate | every check of the scenario passed (final state, lines, tools, invariants) |
| **confirm_violations** | `confirm_order` succeeded in a turn where the dealer did not say yes. **Must be 0.** |
| **invariant_failures** | broken database rules after the chat. **Must be 0.** |
| readback_fidelity | read-back lines copied exactly into the reply |
| avg_turns, avg_tool_calls, avg_seconds | cost and speed |
| invalid_argument_calls | tool calls with wrong argument types or names |

## 7.4 Golden sets

| Set | Source | Notes |
|---|---|---|
| `golden/chits/*.png` | 15 synthetic chittis (5 easy, 5 medium, 5 hard), made with an image model and cut into single photos | Shops did not share real chittis yet. Add real ones after the shop demos. |
| `golden/labels/*.checked.json` | Human labels, one per image | Format and rules: [`evals/golden/README.md`](../evals/golden/README.md). Only checked labels count. |
| `golden/matching.jsonl` | Written from the patterns on the 15 chittis | Codes (HV, Fin, Sch), missing brand, ditto, strike, units, not-sold items |
| `golden/text2sql.jsonl` | 12 owner questions with reference SQL | Runs on a fixed shop history (`evals/scenario.py`) |
| `golden/conversations.jsonl` | 10 scenarios with a persona, a hidden truth and a goal | The reading is fixed, so the eval tests the agent, not the vision model |

**Synthetic data limit.** We asked shops for real chittis, and they did not
share them. The 15 images come from an image model, based on patterns from
real shops. Real chittis have other handwriting, paper, light and
abbreviations. So the numbers on synthetic images show that the pipeline and
the evals work. They do not prove the accuracy on real handwriting.

**Plan for real data.** Show the working demo to shop owners, ask again for
real chittis (names removed), label them with the same rules, and run the
same evals. Report synthetic and real results in separate rows.

## 7.5 The improvement loop

```text
run evals -> read failures in results.jsonl -> tag each failure (7.6)
-> count tags -> fix the largest group (prompt, matcher, catalog alias, tool text)
-> run again -> compare in SCORECARD.md -> keep the change only if the numbers go up
   and every "must be 0" stays 0
```

## 7.6 Failure tags (start list)

| Part | Tag | Typical fix |
|---|---|---|
| reader | missed_line, merged_lines, wrong_qty, ditto_misread, brand_code_unknown, telugu_header, strike_ignored | prompt rule, image size, model choice |
| matcher | alias_missing, unit_conversion, default_brand_wrong | catalog alias, `pack_size`, `is_default_brand` |
| agent | confirmed_without_yes, readback_changed, wrong_change_mapping, extra_question, wrong_tool | instructions, tool description |
| text2sql | wrong_view, wrong_filter, money_units | view notes, prompt |

## 7.7 Not measured yet

- Real chittis from shops (planned after the shop demos; shops did not share them yet).
- Telugu-script lines.
- Hermes-specific behaviour (the conversation eval uses the reference agent with the same tools and instructions).
- Long-term drift: run the evals weekly on new real traces.
