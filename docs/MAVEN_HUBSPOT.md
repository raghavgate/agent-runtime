# Larger live HubSpot evaluation dataset

The HubSpot sandbox is populated with the public fictional **Maven CRM Sales Opportunities** corpus: 85 companies and 8,800 sales opportunities from 2016–2017. The import completed with **8,885 new records, 7,375 associations, and zero errors** on October 29, 2026. The pre-existing HubSpot welcome company/contact records remain separate.

Publisher: https://mavenanalytics.io/data-playground/crm-sales-opportunities

The publisher lists the dataset as Public Domain. Original CSVs were downloaded from the public mirror https://github.com/ChitikaneniVarsha/Maven-Sales-Challenge because the publisher currently requires a separate login to download. `data/maven/manifest.json` records provenance and SHA-256 hashes. The original files are retained unchanged in `data/maven/source/`.

## Data meaning and import mapping

Source opportunity stages are mapped to the existing HubSpot sales pipeline:

| Source | HubSpot |
| --- | --- |
| Prospecting | Appointment scheduled |
| Engaging | Qualified to buy |
| Won | Closed won |
| Lost | Closed lost |

- All 8,800 source opportunity IDs appear in unique deal names, preserving identity without pretending source IDs are HubSpot IDs.
- 1,425 opportunities have no source company. They were imported as unassociated deals; no company was invented.
- Companies have reserved `maven-crm-NNN.example.com` identifiers for reliable import deduplication. These are artificial fixture domains, not source company websites.
- Annual company revenue is converted from millions of USD to base units. It is not annual contract value, and the adapter's `annual_value` mapping remains unset.
- Source `close_value` maps to Amount. All 2,089 open opportunities lack this value. Their total pipeline value is unknown, not zero and not a product-list-price estimate.
- Original close dates are preserved in the custom deal date property `maven_source_close_date`. The private workspace config maps `deal_close_date` to that field, preventing HubSpot-generated close dates from filling source gaps.
- The source contains sales agents, not customer contacts. Agent names remain in deal descriptions; no HubSpot users or customer contacts were fabricated.
- No source renewal dates or support tickets exist. These workflows remain covered by the separate synthetic fixture.

## Repeatable validation

`scripts/prepare_hubspot_maven.py` regenerates the import CSV, manifest, company gold facts, five development cases, and three held-out cases from the original CSVs. Do not import its output again into the same portal: without a stable HubSpot deal record ID, rerunning an import can duplicate opportunities.

Validate source and import integrity offline:

```sh
.venv/bin/python scripts/check_hubspot_maven.py --offline
```

In a terminal with `HUBSPOT_SERVICE_KEY`, directly compare live company searches, ALL/OPEN deal records, values, stages, dates, citations, and absent customer contacts with the original CSVs:

```sh
.venv/bin/python scripts/check_hubspot_maven.py
```

This check makes **zero model requests**. It covers Acme Corporation, Iselectrics, and Hottechi; Hottechi's 200 deals force the connector through multiple search pages. Reports are saved under `.runs/hubspot-maven-*-check.json`, outside the normal trace/evaluation listings.

The model evaluation cases compare retrieved deal names to original opportunity IDs, so expected results do not need hardcoded HubSpot-generated record IDs. Run a small paid model suite only after connector validation passes:

```sh
.venv/bin/python main.py --config config.hubspot.local.json eval --provider openai --model gpt-5.4-mini --profile quick --max-cost 0.03
```

The source provides scale and exact reference facts; source checks and live connector checks do not establish model answer quality. Paid evaluations remain a separate step and are bounded by the existing spend guard.

## Initial verification

The 9 source/import integrity checks and 11 mocked HubSpot connector tests passed. One live model question against the imported portal also passed 8 source-based checks: Acme Corporation returned exactly 10 OPEN opportunities, with 8 Qualified To Buy and 2 Appointment Scheduled, all with unavailable amounts. Its estimated OpenAI cost was $0.0034065. The saved verification report is `.runs/hubspot-maven-model-smoke-check.json` and is excluded from Git.

After loading the custom source-date mapping, the live connector validation passed **39/39 checks**, including Hottechi's 200-record pagination case, exact record membership, amounts, stages, dates, grouping, source references, and absent contacts. This check made zero model requests. The eight-case live model evaluation has not yet been run.

Start the configured workspace in the terminal holding both credentials:

```sh
.venv/bin/python main.py --config config.hubspot.local.json serve --provider openai --model gpt-5.4-mini --port 8768
```
