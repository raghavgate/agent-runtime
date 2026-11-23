# First live-model baseline

Captured October 28, 2026, using `gpt-5.4-mini` and the synthetic CRM/support fixtures. These are three tasks, involving multiple model requests as the tools run.

| Scenario | Automated result | Tool calls |
| --- | --- | --- |
| Acme account brief | Passed | 3 |
| Unknown customer (Umbrella) | Passed | 1 |
| Ambiguous company (Northstar) | Passed | 1 |

Total usage: **3,705 input tokens and 394 output tokens**. Estimated standard-rate cost: **$0.00455175**, excluding any cached-token discount. Mean task duration: **3.13 seconds**.

The live-model answers correctly retrieved Acme's owner, renewal, value and two open tickets; declined to invent an unknown customer; and asked which of two Northstar accounts to use. These were inspected by the coding agent; human review is still pending. Three sample cases do not establish general CRM accuracy.

## Findings

- The account brief fetched full account details again after search had already returned them. This is tracked as an efficiency note, separate from factual success.
- The initial grader rejected the correct phrase “couldn’t find” because it expected a straight apostrophe. Typography is now normalized before text-pattern checks. The same saved traces were replayed without further API calls and passed all three cases.
- Both the original and corrected reports are kept locally. Grader fingerprints prevent interpreting the scoring correction as a model improvement.

## Version boundary

The baseline was captured through the already-running server, whose traces record the previous **10-tool-call limit** and deployed code/config fingerprints. It did not run with the newly introduced four-call/700-output-token quick-profile controls. Replay reports explicitly record that distinction.

The redundant-call guidance and concise presentation instructions were improved afterward and have offline coverage. Their live effect has not been measured; a later quick check can compare against the saved baseline after restarting the server.

Local reports and the replay manifest are in `.evaluations/` and individual task evidence is in `.runs/`. They are excluded from Git. Regrade the existing baseline at no additional API cost:

```sh
.venv/bin/python main.py eval --profile quick --replay .evaluations/quick-live-manifest.json
```

## Follow-up: incomplete brief detected

The first post-optimization live quick check passed two of three cases (estimated $0.003276). The Acme answer retrieved account details but deferred ticket lookup, so it correctly failed the required tool, ticket, citation and priority checks. Unknown-customer and ambiguity handling passed. No grading expectations were relaxed.

The workspace configuration now explicitly defines an account brief as including open tickets when support access and a mapping exist. This clarification is appended at the application boundary, leaving the runtime generic. Its live effect has not yet been verified.

**Harness fix (2026-11-16).** The runtime now enforces this, rather than relying on instructions alone. A completion check rejects an account brief for a single mapped account if it skips the ticket lookup, and asks the model once to finish it. If the brief is still incomplete, the run fails instead of returning a partial brief. Identical repeated tool calls in a turn reuse the earlier result. `tests/test_completion_checks.py` reproduces the recorded failure and its correction offline.

## Live quick check with completion checks (2026-11-22)

Run on the `fix/completion-checks` branch with `gpt-5.4-mini`, the quick profile (4 tool calls, 700 output tokens per response) and a $0.03 spend threshold. Evaluation `71d2d1f8`.

| Scenario | Result | Tool calls | Duration |
| --- | --- | --- | --- |
| Acme account brief | Passed (11/11 checks) | 2 | 6.64 s |
| Unknown customer (Umbrella) | Passed | 1 | 3.85 s |
| Ambiguous company (Northstar) | Passed | 1 | 2.26 s |

Total usage: **5,174 input tokens and 280 output tokens**. Estimated standard-rate cost: **$0.0051405**. Mean task duration: **4.25 seconds**. No efficiency notes.

The Acme brief now includes the owner, renewal date, annual value and both open tickets (critical and high), each cited. The trace shows `crm_search_accounts` followed directly by `support_get_open_tickets`. The model did not repeat the account retrieval recorded in the October baseline (2 calls, previously 3).

The completion check did not need to intervene in this run: the model fetched tickets on its first attempt. This single run shows the full brief is reachable under the quick limits. It does not measure how often the model would skip the ticket lookup without the check. The check is the guarantee for those cases, and its behavior is covered offline. Three cases on one run do not establish general accuracy; human review of these answers is still pending.

The runner recorded this report as a new baseline rather than comparing it automatically with October's, because its dataset, suite, model and grader fingerprints don't all match that report's.

## Pricing reference

Rates checked October 28, 2026: $0.75 per million input tokens and $4.50 per million output tokens. See the [official model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini). They are model-scoped, editable estimates in `config.json`, not a billing statement.
