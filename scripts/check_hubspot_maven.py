"""Compare live HubSpot reads with the Maven CSVs, without constructing an LLM."""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import Application
from integrations.hubspot import HubSpotAPIError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.hubspot.local.json"))
    parser.add_argument("--offline", action="store_true", help="Verify source and import integrity only")
    args = parser.parse_args()
    base = ROOT / "data" / "maven"
    manifest = json.loads((base / "manifest.json").read_text())
    checks = {}
    for name, digest in manifest["files_sha256"].items():
        checks["source_hash_" + name] = hashlib.sha256((base / "source" / name).read_bytes()).hexdigest() == digest
    with (base / "source" / "sales_pipeline.csv").open(newline="") as stream:
        source = list(csv.DictReader(stream))
    with (base / "maven-companies-deals.csv").open(newline="") as stream:
        prepared = list(csv.DictReader(stream))
    checks["all_8800_source_opportunities_preserved"] = len(source) == len(prepared) == 8800
    checks["85_distinct_company_identifiers"] = len({row["Company domain name"] for row in prepared if row["Company domain name"]}) == 85
    checks["1425_unassociated_opportunities_preserved"] = sum(not row["Company name"] for row in prepared) == 1425
    checks["source_identifiers_unique"] = len({row["opportunity_id"] for row in source}) == 8800
    checks["stage_counts_preserved"] = Counter(row["Deal stage"] for row in prepared) == Counter(
        {manifest["stage_mapping"][key]: value for key, value in manifest["source_stage_counts"].items()})
    checks["2089_missing_open_values_preserved"] = sum(not row["Amount"] for row in prepared) == 2089
    if not all(checks.values()):
        raise ValueError("Maven source/import integrity check failed before API reads")
    if not args.offline:
        app = Application(args.config)
        invoke = lambda name, **kw: app.registry.get(name).invoke(kw)
        for company in ("Acme Corporation", "Hottechi", "Iselectrics"):
            matches = invoke("crm_search_accounts", company_name=company)["data"]
            if len(matches) != 1 or matches[0]["company_name"] != company:
                raise ValueError("Expected exactly one imported company: " + company)
            customer_id = matches[0]["customer_id"]
            checks[company + "_search"] = True
            for stage in ("ALL", "OPEN"):
                expected = [row for row in source if row["account"] == company and
                            (stage == "ALL" or row["deal_stage"] in ("Prospecting", "Engaging"))]
                response = invoke("crm_get_deals", customer_id=customer_id, stage=stage)
                rows = response["data"]
                actual = {row["name"]: row for row in rows}
                wanted = {f"Maven {row['opportunity_id']} - {row['product']}": row for row in expected}
                label = company + "_" + stage
                checks[label + "_exact_records"] = len(rows) == len(actual) and set(actual) == set(wanted)
                checks[label + "_grouping"] = all(row["customer_id"] == customer_id for row in rows)
                checks[label + "_values_and_dates"] = all(
                    name in wanted and
                    (None if row["amount"] is None else Decimal(str(row["amount"]))) ==
                    (Decimal(wanted[name]["close_value"]) if wanted[name]["close_value"] else None) and
                    row["expected_close_date"] == (wanted[name]["close_date"] or None) and
                    row["stage"].casefold() == manifest["stage_mapping"][wanted[name]["deal_stage"]].casefold()
                    for name, row in actual.items())
                checks[label + "_sources"] = {"hubspot:deals/" + row["deal_id"] for row in rows} <= {
                    item["id"] for item in response["sources"]}
            contacts = invoke("crm_get_contacts", customer_id=customer_id)
            checks[company + "_no_fabricated_contacts"] = contacts["data"] == []
    report = {"kind": "maven_source_integrity" if args.offline else "live_hubspot_connector_validation",
              "model_requests": 0, "passed": sum(checks.values()), "total": len(checks),
              "checks": checks}
    output = ROOT / ".runs" / ("hubspot-maven-source-check.json" if args.offline else "hubspot-maven-connector-check.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    output.chmod(0o600)
    print(json.dumps(report, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, HubSpotAPIError) as error:
        print("Validation failed: " + str(error), file=sys.stderr)
        sys.exit(1)
