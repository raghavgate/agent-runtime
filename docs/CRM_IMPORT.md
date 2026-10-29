# Bring a CRM export into the harness

Imports are local and make no model requests. The original file is never changed. Each import creates an isolated CRM-only workspace; sample support tickets are never attached to real accounts.

## Input contract

CSV and JSON are supported. JSON may be a list of account objects or an object containing an `accounts` list. Extra source columns are not copied.

| Canonical field | Requirement |
| --- | --- |
| `customer_id` | Unique integer or nonempty string. CSV IDs remain strings, preserving leading zeros. |
| `company_name` | Nonempty text. |
| `owner` | Nonempty text. Use an explicit value such as `Unassigned` when the source represents that state. |
| `renewal_date` | Valid date in `YYYY-MM-DD` format. |
| `annual_value` | Finite nonnegative number; no currency conversion or locale guessing. |
| `support_customer_id` | Text mapping to the other system. Leave empty when unavailable. No mapping is invented. |

This initial importer handles account briefs. Contacts and deals need their own data contracts/tools; arbitrary CRM exports are not universally compatible.

Copy `data/crm_mapping.example.json` and change the source column names to match your export. Supply all seven mappings. The included CSV is synthetic and demonstrates a string account ID and an unavailable support mapping.

Validate first:

```sh
.venv/bin/python main.py import-crm /path/to/accounts.csv \
  --mapping /path/to/mapping.json --dry-run
```

Import into the Git-ignored local directory:

```sh
.venv/bin/python main.py import-crm /path/to/accounts.csv \
  --mapping /path/to/mapping.json --output-dir local_data/my-crm
```

Start that workspace after setting your API key/model in the terminal:

```sh
.venv/bin/python main.py --config local_data/my-crm/config.json \
  serve --provider openai --port 8766
```

Stop any existing server on that port first. Imported account data is sent to the model when you explicitly run a live task. Anonymize the export before importing if needed. The importer itself is offline.

A new output directory is required each time, so an existing dataset is not accidentally overwritten. Imported files and configuration use owner-only permissions. Keep private source exports under `local_data/`, not under the tracked sample `data/` directory.

## Evaluate imported data

Bundled gold cases describe the sample dataset and are deliberately rejected for imported data. Create independent expectations matching your imported records, using `evaluations/cases.json` as a format reference. Start with three cases that you can verify yourself.

```sh
.venv/bin/python main.py --config local_data/my-crm/config.json \
  eval --provider openai --profile quick --cases /path/to/my-cases.json
```

For the browser, set `"evaluation_cases": "cases.json"` in the imported configuration, with that file beside it. The quick profile selects the first three custom cases; the full profile selects all of them. Case files should be local and excluded from Git if they contain private expectations.

A support integration can be added later to that configuration only after real support data and explicit customer mappings are available. Missing support access should be reported as a limitation rather than filled from the sample fixtures.
