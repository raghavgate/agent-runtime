# Agent Runtime

A local, read-only prototype of a system-agnostic Python agent harness. A shared runtime coordinates models and tools; integrations provide system-specific data. Workflows include account research, renewal review, and customer-meeting preparation across CRM and support records.

**Implemented:** OpenAI Responses adapter, deterministic offline demo, two JSON-export integrations, a live read-only HubSpot CRM connector, input/output validation, execution limits, source references, saved run traces, repeatable workflow evaluations, CLI, and a local browser workspace.

**Prototype boundaries:** bundled data is synthetic. HubSpot can be connected explicitly with a scoped Service Key; other bundled integrations still read local exports. There is no hosted deployment, end-user authentication, multi-user isolation, background scheduling, webhook ingestion, or record mutation. The browser starts in demo mode unless you explicitly choose OpenAI.

## Start here

Python 3.9+:

```sh
cd ~/Desktop/agent-runtime
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py serve
```

Open **http://127.0.0.1:8765**. Stop with Ctrl+C. The server listens only on loopback. For local review use this server, not a public deployment.

The Workspace view presents formatted answers with numbered source links. Click a source to open readable record details. Quality checks and History have separate views; raw run IDs, tool records, and schemas are available only inside the collapsed Developer details section. The underlying traces and original citations are preserved.

Try:

- `Prepare an account brief for Acme`
- `Find open tickets for Globex`
- `Find open tickets for Initech`
- `Find open tickets for Northstar`

Demo mode is a small deterministic implementation of these example workflows, **not a language model**. It requires no key and makes no network requests. The browser supports persistent conversations and clarification follow-ups. One-shot CLI runs and evaluations remain independent. The scripted demo supports only its documented patterns; broader contact/deal reasoning needs a real model.

## CLI

```sh
.venv/bin/python main.py run "Prepare an account brief for Acme"
.venv/bin/python main.py run "Find open tickets for Acme" --json
.venv/bin/python main.py tools
.venv/bin/python main.py trace RUN_ID
.venv/bin/python main.py eval
.venv/bin/python main.py eval --profile quick --repeat 3
```

With no arguments, `main.py` runs the Acme ticket demo. `Runtime.run()` still returns a string; `Runtime.run_record()` returns the complete record. The original `FakeModel`, `ErrorModel`, `LoopModel`, and `integrations/support.py` learning examples remain available and are covered by compatibility tests.

## Use a real model

Set `OPENAI_API_KEY` securely in the terminal that starts the app, and set `OPENAI_MODEL` to an accessible Responses API model ID. `.env.example` documents the variables; files are **not** automatically loaded. Keys are never entered through the browser.

```sh
export OPENAI_MODEL="your-model-id"
.venv/bin/python main.py run "Prepare an account brief for Acme" --provider openai
.venv/bin/python main.py serve --provider openai
.venv/bin/python main.py eval --provider openai --repeat 3
```

Alternatively pass `--model your-model-id`. No model ID is silently selected. Live runs and evaluations incur API usage; a suite has 10 cases per repetition and can require multiple requests per case.

The adapter uses sequential function calls, carries all response items (including reasoning items) forward, pairs outputs using `call_id`, requests encrypted reasoning content for stateless continuation, and sends `store=False`. Incomplete outputs, malformed arguments, and unexpected multiple calls fail clearly. The SDK has a 30-second request timeout and no automatic retries, keeping repeated paid requests explicit. Normal runs allow up to 1,200 output tokens per response; quick evaluations use 700. Provider failures are recorded without echoing SDK request bodies.

Reference: [official OpenAI function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling).

Live API behavior must be verified with a configured key and model. Mocked adapter tests verify request construction and protocol handling; they do not establish model quality or account access.

## Architecture and review order

```text
CLI / local browser
        |
   Application -------- configuration + integrations
        |
     Runtime ---------- trace store
     /     \
 model     tool registry
 adapter     /      \
          CRM     support
```

Read these files in order:

1. `tool.py`, `schema.py`: tool definitions, compatibility conversion, JSON Schema validation.
2. `registry.py`: registration and lookup; duplicate names are rejected.
3. `runtime.py`: generic loop, validation, budgets, recoverable tool errors, final answers.
4. `openai_model.py`: provider protocol, conversation history, usage, source instructions.
5. `integrations/crm.py`, `integrations/ticketing.py`, `integrations/hubspot.py`: local and live system adapters behind the same neutral tools.
6. `app.py`, `config.json`: composition and configuration.
7. `evaluations/cases.json`, `evaluations/runner.py`: gold cases, graders, report comparisons.
8. `server.py`, `web/`: local workspace.

The runtime imports neither OpenAI nor any CRM/support integration. Model adapters implement `generate(user_message, tool_result=None, tools=None)` and optionally `reset()`, `last_usage`, `provider`, and `model_name`. One-shot runs reset model state before use. Conversation turns restore a private adapter snapshot and prior source references. Create a model per concurrent run; adapters are not shared mutable sessions.

A tool contains a name, description, input schema, output schema, callable, and read-only declaration. Original shorthand schemas remain accepted; full JSON Schema is supported too. New integrations should use the standard result envelope:

```json
{
  "data": [],
  "sources": [{"id": "system:collection/record-id", "label": "Record label", "system": "system"}]
}
```

The CRM account provides an explicit `support_customer_id`. The support adapter uses that ID instead of assuming the CRM integer ID is valid in another system. Unknown organizations produce a correctable error; known organizations with no open tickets produce an empty result with a source reference.

## Configuration and adding systems

Paths in `config.json` resolve relative to that configuration file. Use `--config /absolute/path/config.json` **before** the subcommand. The integration list selects adapters and export files. Set `max_tool_calls`, `require_sources`, and output directories there. An optional `instructions` string overrides the default OpenAI instructions; include the source and ambiguity rules when customizing it.

To add an integration:

1. Implement a class that loads/connects to its system and returns `Tool` objects from `tools()`.
2. Add its constructor to the integration factory map in `app.py`.
3. Add an entry in the configuration and independent evaluation fixtures/cases.

No runtime changes are required. The HubSpot connector is the first live example of that boundary. A test also exercises an unrelated inventory tool through the same runtime; this proves the extension design, not that arbitrary SaaS systems work without adapter development.

### Live HubSpot CRM

The read-only HubSpot adapter supports company search/read, associated contacts, associated ALL/OPEN deals, owner and stage label resolution, pagination, bounded retries, batch reads, optional renewal searches, and explicit property mapping. Credentials remain in `HUBSPOT_SERVICE_KEY`; no secret is stored in configuration.

Follow [docs/HUBSPOT.md](docs/HUBSPOT.md) for the free account/test-account path, least-privilege Service Key scopes, private local config, connection check, custom property mapping, and launch command. `check-connections` makes no model requests, and the automated connector suite uses mocks rather than a live account.

The connected sandbox now also has an imported public Maven CRM corpus with 85 companies and 8,800 sales opportunities. [docs/MAVEN_HUBSPOT.md](docs/MAVEN_HUBSPOT.md) records the source, import mappings, missing-data semantics, source gold facts, five development cases, three held-out cases, and a direct connector check that makes zero model requests.

For other datasets, provide your own evaluation cases; the bundled gold expectations intentionally apply only to the bundled fixtures.

## Validation and failure behavior

- Tool inputs are validated before the callable runs; extra/missing fields and invalid types are rejected.
- Unknown tools and expected input/domain errors go back to the model to permit correction. Invalid attempts consume the call budget.
- A tool output violating its declared schema, programming error, or disallowed write tool terminates the run.
- The last permitted tool may be followed by a final model answer; a further tool request is blocked before execution.
- Source IDs cited in the final answer must have been retrieved. When source requirements are enabled and evidence was retrieved, at least one source must be cited.
- Citation membership is **not factual verification**. The runtime does not prove that every sentence follows from its cited record. See evaluation limits below.
- `read_only` is a trusted integration declaration, not a sandbox for arbitrary Python callables. Only register reviewed integrations.

## Evaluation and validation are different

Validation enforces contracts during a run. Evaluation measures behavior across a dataset.

The full workflow suite checks required tool calls and arguments, exact retrieved ticket IDs where specified, expected answer facts using explicit patterns, forbidden facts/tools, and citation membership. Cases cover account briefs, no tickets, unknown accounts, ambiguity, case normalization, exact account matching, and a request to fabricate a ticket.

The **demo** suite validates deterministic workflow plumbing. It does not measure LLM quality. The **OpenAI** suite runs the same cases against a real model. Use repeated runs to observe variability. Unit tests separately cover invalid calls, tool failures, budgets, output contracts, provider protocol behavior, and HTTP endpoints.

The CLI defaults to the **quick** profile: three cases (brief, unknown customer, ambiguity), up to four tool calls per task, 700 output tokens per response, and a 12,000 cumulative-token threshold checked between responses. Use `--profile full` for all 10 bundled cases (six calls, 1,200 output tokens, 20,000 cumulative tokens). Custom quick suites use their first three cases. A default $0.05 estimated-spend threshold stops starting additional cases; it is not an exact billing cap, and the last case may cross it. Unknown pricing blocks a paid suite; provider failures without usage information stop further cases.

Reports include per-check results, final answers, trace IDs, model/provider, dataset/suite/config/code fingerprints, latency, tool-call counts, token usage when available, and estimated cost if configured. Reports live in `.evaluations/`; individual traces live in `.runs/`. CLI evaluation returns a nonzero exit code when any case fails or the suite is incomplete. Reports are browsable under Quality checks; opening a saved report makes no model requests. The first complete result becomes a stable baseline for its dataset, suite, model and grader version. Evaluation traces are excluded from normal question history.

```sh
.venv/bin/python main.py eval --compare .evaluations/PREVIOUS_EVALUATION_ID.json
```

Comparison requires the same dataset, suite, evaluation kind and grader rules; it reports pass-rate changes and cases that improved or regressed. It is descriptive, not a statistical significance test. Code/config fingerprints are retained separately so deployment changes can be traced. Grading-rule changes require regrading saved traces before score comparisons.

**Grading limits:** patterns can reject equivalent phrasing or miss unsupported claims. No general semantic grounding score, model-as-judge, or human rating is claimed. Review answers alongside tool results, and expand independent gold cases as real failure modes appear. The fabricated-ticket case is a narrow behavioral check, not a comprehensive prompt-injection benchmark.

Model-specific entries under `pricing` contain `input_usd_per_million`, `output_usd_per_million`, a source URL and check date. The bundled GPT-5.4 Mini estimate uses verified published rates. An unknown model has unknown/null cost rather than inheriting another model’s rates. Estimates conservatively use standard input pricing without cache discounts and are not billing statements. Update rates when needed.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use mocked OpenAI responses and temporary local files. HTTP tests bind a temporary loopback port. No paid API calls occur during the test suite.

## Data handling

Run records contain prompts, tool arguments/results, and customer data. They are local, ignored by Git, and saved with owner-only permissions; do not commit or distribute them inadvertently. The application does not log environment variables or API keys. In OpenAI mode, task text, tool definitions, and retrieved results are sent to OpenAI; `store=False` is not a blanket guarantee about all provider retention policies.

## Next product milestones

- Measure the new meeting-preparation workflow against the dedicated development case and establish a repeatable live baseline.
- Validate the HubSpot adapter against a controlled developer test account and author independent held-out cases for that account.
- Add per-user access controls and data isolation before shared deployment.
- Add approved write actions, durable jobs, and broader workflow support only as required.

## Expansion: live baseline and CRM exports

`workflow_instructions` in a workspace configuration defines domain-specific completion requirements and is appended to the model instructions. The synthetic workspace defines account briefs, renewal review, and meeting preparation while the runtime remains domain-neutral. Meeting preparation retrieves the account, designated primary contact, OPEN deals, and OPEN support tickets, then produces a cited brief with talking points. Changing these instructions requires a server restart.

The first three-case live baseline is documented in [docs/BASELINE.md](docs/BASELINE.md). All three saved answers pass after a punctuation-only grading fix; replay incurred no additional API charges. A redundant account retrieval was identified. The prompt/tool guidance change addressing it still needs a later live measurement.

Regrade saved runs without constructing a model or making API calls:

```sh
.venv/bin/python main.py eval --profile quick --replay .evaluations/quick-live-manifest.json
```

A replay manifest maps selected case IDs to saved run IDs. Messages and deployment fingerprints must match; limits are reported from the actual traces, never retroactively claimed.

For real data, use the offline CSV/JSON importer and column mapping described in [docs/CRM_IMPORT.md](docs/CRM_IMPORT.md). An import creates an isolated configuration and requires independent gold cases. No real customer data has been supplied or imported yet.

After backend changes, restart the live server in the **same terminal** (Ctrl+C, then rerun the command). The shell keeps the exported key/model; there is no need to paste them again for that restart:

```sh
.venv/bin/python main.py serve --provider openai --port 8766
```

The new frontend disables quick-check controls against an older server so a request for three cases cannot accidentally launch the old ten-case evaluation.

## Larger synthetic CRM workspace

A separate fictional snapshot now includes **24 accounts, 69 contacts, 48 deals and 88 support tickets**, with a fixed as-of date and reproducible generation. It adds contact lookup, open-deal filtering and inclusive renewal-date searches without changing the execution loop. The original small workspace remains available.

Start with offline validation:

```sh
.venv/bin/python main.py --config config.synthetic.json validate-data
.venv/bin/python scripts/generate_synthetic.py --check
```

See [docs/SYNTHETIC_WORKSPACE.md](docs/SYNTHETIC_WORKSPACE.md) for the data contract, launch command, example questions, intentional edge cases, and the separate 13-case development / 6-case held-out evaluation partitions. No paid model runs were made while building the snapshot.

## Conversations and combined workflows

The browser now has a persistent chat transcript, resumable conversation history, separate source cards per answer, and a new-conversation control. Clarifications such as “Northstar” → “Labs” and follow-ups can reuse the selected account. The customer-meeting starter requests a consistent executive snapshot, attendee, commercial position, support concerns, and evidence-backed talking points. The renewal-attention starter uses bounded batch reads to compare every account in an explicit renewal window and separates retrieved evidence from recommended actions.

See [docs/CONVERSATIONS.md](docs/CONVERSATIONS.md) for state boundaries, failure recovery, duplicate-submission handling, limits, cost behavior and the new API. Conversation files are local, owner-only, and excluded from Git.
