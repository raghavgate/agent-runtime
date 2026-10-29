# Synthetic CRM sandbox

A fictional business-data snapshot for exercising the same harness on a larger, relational dataset. No customer export, external data source, model call, or real personal information was used to create it. The as-of date is fixed at **October 28, 2026**, so date-window tests remain reproducible.

## Contents

| Records | Count |
| --- | ---: |
| Accounts | 24 |
| Contacts | 69 |
| Deals | 48 |
| Support organizations | 23 |
| Support tickets | 88 |

Files are in `data/synthetic/`. `crm.json` contains accounts, contacts, and deals with explicit foreign keys; `support.json` uses separate organization IDs. `accounts.csv` is an account-only export compatible with the existing importer. `manifest.json` contains counts, hashes, the snapshot date, and intentional edge cases. Email addresses use `.example` domains.

The new configuration is `config.synthetic.json`. The original `config.json` and its small fixture files are preserved. Synthetic run records and evaluations are isolated under `.runs/synthetic/` and `.evaluations/synthetic/`.

## What you can ask

- “Prepare an account brief for Cedar & Finch, including open support tickets.”
- “Who is the primary contact for Cedar & Finch, and what is their email?”
- “List Cedar & Finch's OPEN deals and calculate the total pipeline in USD.”
- “Which accounts renew from 2026-11-05 through 2026-11-12 inclusive?”
- “Build a prioritized renewal-attention brief for that explicit window, including contacts, OPEN pipeline, support status, and recommended next actions.”

Browser conversations retain account and task context across follow-ups. CLI one-shot tasks remain independent. When a renewal date window has not been established in the conversation, specify it explicitly.

## New tools

- `crm_get_contacts`: primary designation, role, nullable email and last-contacted date. An unassigned primary contact or missing email is not guessed.
- `crm_get_deals`: explicit `OPEN`, `ALL`, or individual-stage filtering. OPEN excludes closed won and closed lost. All amounts in this snapshot are USD.
- `crm_get_contacts_batch` and `crm_get_deals_batch`: bounded multi-account reads that preserve each row's customer ID and avoid one model round trip per account.
- `support_get_open_tickets_batch`: bounded multi-organization read with an organization source even when that organization has no open tickets. Accounts without a support mapping are omitted and reported as unavailable.
- Meeting preparation combines the resolved account, designated primary contact, OPEN pipeline, and OPEN support issues into a cited brief with talking points. Its development evaluation verifies all four retrieval paths and the expected records.
- `crm_list_renewals`: inclusive ISO date range with validation and sources for both records and the query result.

The renewal-attention workflow uses one renewal search and three batch reads to build a portfolio view. It ranks attention from explicit support, commercial, contact, and access evidence; suggested actions are labeled as recommendations rather than stored facts or automated risk scores.

Existing account and support tools remain in use. The runtime has no CRM-specific orchestration branches. Contacts/deals are optional export collections, so the original account-only data still works; a missing collection does not expose a misleading empty lookup tool.

## Deliberate difficult cases

- Northstar Labs and Northstar Retail require disambiguation for “Northstar.”
- Bluewater has a valid support organization with no tickets.
- Meridian has no support mapping; this is not evidence of zero tickets. Its primary contact also lacks an email.
- Orchard has no contacts.
- Juniper has contacts but no designated primary.
- Cobalt has only closed deals and closed tickets.
- Closed critical tickets and pending tickets must not be counted as OPEN.

Reference facts used in development checks include Cedar & Finch's $90,000 open pipeline, its two open tickets, and the inclusive November 5–12 renewal set (Cedar & Finch, Bluewater Systems, Meridian Health). Expectations are manually authored independently of the generator. They are not a claim of realistic business distributions or real-world accuracy.

## Verify without API spending

```sh
.venv/bin/python scripts/generate_synthetic.py --check
.venv/bin/python main.py --config config.synthetic.json validate-data
.venv/bin/python -m unittest discover -s tests -v
```

The first command checks byte-for-byte reproducibility. The second validates schemas, dates, unique IDs, child-to-account references and available cross-system support mappings, then reports counts. Unit tests check independent expected totals, boundary dates, filtering, missing data, source provenance, and a scripted four-tool round trip. A scripted sequence is a protocol test, not LLM evaluation.

To regenerate the fictional files explicitly:

```sh
.venv/bin/python scripts/generate_synthetic.py
```

The generator only writes its four generated snapshot files and does not generate or rewrite evaluation answers.

## Use the live workspace

In the terminal already holding your exported API key, stop the previous server with Ctrl+C and run:

```sh
.venv/bin/python main.py --config config.synthetic.json serve \
  --provider openai --model gpt-5.4-mini --port 8766
```

Refresh `http://127.0.0.1:8766/`. The workspace label and counts identify the synthetic snapshot. It is still fictional data even in live-model mode. Keys and model credentials are not stored in this configuration.

A demo server can preview the dataset and run the existing account/ticket examples, but contact/deal questions require a real model. Demo evaluation is deliberately disabled for this richer suite; no deterministic pass rate is presented as model accuracy.

## Evaluation partitions

- **Development:** 12 cases in `evaluations/synthetic/development.json`. Quick mode selects the first three: account brief, unknown account, ambiguous name.
- **Held-out:** 6 separate cases in `evaluations/synthetic/heldout.json`. Quick mode selects the first three; full mode selects all six. Keep these for later measurement rather than prompt tuning.

The two partitions share the underlying fictional database; the held-out boundary is at the question/case level, not an independent sample of real organizations.

```sh
# Paid; run only when ready to measure the model.
.venv/bin/python main.py --config config.synthetic.json eval \
  --provider openai --model gpt-5.4-mini --profile quick

# Paid; held-out measurement should follow development review.
.venv/bin/python main.py --config config.synthetic.json eval \
  --provider openai --model gpt-5.4-mini --profile full --split heldout
```

The browser exposes both the coverage and case partition, with correct case counts. Reports keep the partition and do not compare development with held-out results as the same suite. Generic `expected_records` checks verify which contact, deal or account IDs were actually retrieved; wording and citations are checked separately.

No live evaluations on this larger dataset have been run during its construction. Automated pattern checks can still reject valid phrasing or miss unsupported reasoning; human review is required. These small, authored synthetic partitions do not establish statistical generalization to real CRM data.
