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

## Pricing reference

Rates checked October 28, 2026: $0.75 per million input tokens and $4.50 per million output tokens. See the [official model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini). They are model-scoped, editable estimates in `config.json`, not a billing statement.
