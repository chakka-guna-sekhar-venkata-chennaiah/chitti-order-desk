# Eval scorecard

Made by `python -m evals.report` from the latest run of each eval.
Change = latest run minus the run before it.

## 1. Chitti reader (vision model)

Run: 2026-10-27T12:41:51+00:00 - 15 cases - config: `{"model": "nvidia/Nemotron-3.5-Super-VL", "labels": 15}`

| Metric | Value | Change |
|---|---|---|
| line_recall | 1.0 |  |
| line_precision | 1.0 |  |
| text_similarity | 0.926 | -0.058 WORSE |
| qty_accuracy | 1.0 |  |
| item_accuracy | 0.942 | -0.058 WORSE |
| order_line_accuracy | 0.968 | -0.032 WORSE |
| clarify_recall | 0.625 |  |
| unsafe_accept_rate | 0.0 |  |
| decision_accuracy | 0.933 | +0.6 better |
| chitti_pass_rate | 0.733 | +0.4 better |
| avg_seconds | 25.1 | +9.7 WORSE |

## 2. Product matcher (code)

Run: 2026-10-27T04:58:53+00:00 - 41 cases - config: `{}`

| Metric | Value | Change |
|---|---|---|
| accuracy | 1.0 |  |
| accuracy_assumed | 1.0 |  |
| accuracy_ok | 1.0 |  |
| accuracy_unclear | 1.0 |  |
| accuracy_unmatched | 1.0 |  |

## 3. Owner questions (text-to-SQL)

Run: 2026-10-27T12:57:52+00:00 - 12 cases - config: `{"model": "nvidia/nemotron-3-super-120b-a12b"}`

| Metric | Value | Change |
|---|---|---|
| execution_accuracy | 0.5 |  |
| valid_sql_rate | 0.917 | -0.083 WORSE |
| refused_rate | 0.0 |  |

## 4. Dealer conversations (agent + simulated dealer)

Run: 2026-10-27T12:57:25+00:00 - 10 cases - config: `{"agent_model": "nvidia/nemotron-3-super-120b-a12b", "sim_model": "nvidia/Nemotron-3_5-Lightning"}`

| Metric | Value | Change |
|---|---|---|
| task_success_rate | 0.8 | +0.2 better |
| confirm_violations | 0 |  |
| invariant_failures | 0 |  |
| readback_fidelity | 0.944 | +0.148 better |
| avg_turns | 2.4 |  |
| avg_tool_calls | 4.3 |  |
| invalid_argument_calls | 0 |  |
| avg_seconds | 26.9 | +13.8 WORSE |

