"""Prepare the public Maven CRM corpus for HubSpot and retain source gold facts."""
import csv
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "maven"
SOURCE = ROOT / "source"
STAGES = {"Prospecting": "Appointment scheduled", "Engaging": "Qualified to buy",
          "Won": "Closed won", "Lost": "Closed lost"}


def read(name):
    with (SOURCE / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    accounts = read("accounts.csv")
    opportunities = read("sales_pipeline.csv")
    assert len(accounts) == 85 and len(opportunities) == 8800
    assert len({row["account"] for row in accounts}) == len(accounts)
    assert len({row["opportunity_id"] for row in opportunities}) == len(opportunities)
    by_name = {row["account"]: row for row in accounts}
    assert all(not row["account"] or row["account"] in by_name for row in opportunities)
    domains = {row["account"]: f"maven-crm-{index:03d}.example.com"
               for index, row in enumerate(accounts, 1)}
    headers = ["Company name", "Company domain name", "Number of employees", "Annual revenue",
               "Deal name", "Pipeline", "Deal stage", "Amount", "Maven source close date",
               "Deal description"]
    path = ROOT / "maven-companies-deals.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        for row in opportunities:
            company = by_name.get(row["account"])
            writer.writerow({
                "Company name": row["account"],
                "Company domain name": domains.get(row["account"], ""),
                "Number of employees": company["employees"] if company else "",
                "Annual revenue": str(Decimal(company["revenue"]) * 1000000) if company else "",
                "Deal name": f"Maven {row['opportunity_id']} - {row['product']}",
                "Pipeline": "Sales pipeline", "Deal stage": STAGES[row["deal_stage"]],
                "Amount": row["close_value"], "Maven source close date": row["close_date"],
                "Deal description": (
                    "Public fictional Maven CRM Sales Opportunities dataset. "
                    f"Source opportunity: {row['opportunity_id']}; source stage: {row['deal_stage']}; "
                    f"source sales agent: {row['sales_agent']}; product: {row['product']}; "
                    f"engage date: {row['engage_date'] or 'unavailable'}; "
                    f"source close date: {row['close_date'] or 'unavailable'}; "
                    "company domain is an artificial import identifier."
                ),
            })
    gold = {}
    for name in by_name:
        deals = [row for row in opportunities if row["account"] == name]
        gold[name] = {
            "company_domain": domains[name], "deal_count": len(deals),
            "source_stage_counts": dict(Counter(row["deal_stage"] for row in deals)),
            "open_deal_count": sum(row["deal_stage"] in ("Prospecting", "Engaging") for row in deals),
            "recorded_close_value_total": str(sum((Decimal(row["close_value"]) for row in deals
                                                   if row["close_value"]), Decimal(0))),
            "open_deal_values_available": False,
            "source_opportunity_ids": [row["opportunity_id"] for row in deals],
        }
    manifest = {
        "source": "https://mavenanalytics.io/data-playground/crm-sales-opportunities",
        "download_mirror": "https://github.com/ChitikaneniVarsha/Maven-Sales-Challenge",
        "license_as_listed_by_publisher": "Public Domain", "fictional": True,
        "companies": len(accounts), "deals": len(opportunities), "contacts": 0,
        "unassociated_deals": sum(not row["account"] for row in opportunities),
        "source_stage_counts": dict(Counter(row["deal_stage"] for row in opportunities)),
        "stage_mapping": STAGES,
        "limitations": ["No source customer contacts, renewals, or support tickets.",
                        "Source sales-agent names retained in description; no HubSpot users created.",
                        "Open deal values and close dates remain unavailable.",
                        "Fixture domains use reserved example.com solely as company identifiers.",
                        "Use the custom source close-date property to avoid HubSpot's automatic date defaults."],
        "files_sha256": {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
                         for name in ("accounts.csv", "sales_pipeline.csv", "data_dictionary.csv")},
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "source-gold-facts.json").write_text(json.dumps(gold, indent=2) + "\n")
    def deals_case(name, stage, label):
        selected = [row for row in opportunities if row["account"] == name and
                    (stage == "ALL" or row["deal_stage"] in ("Prospecting", "Engaging"))]
        count = len(selected)
        return {
            "id": label,
            "message": f"How many {stage} deals does {name} have? Report the count, stages, and whether recorded amounts are available. Do not list every deal or estimate missing amounts.",
            "required_calls": [
                {"tool_name": "crm_search_accounts", "arguments": {"company_name": name}},
                {"tool_name": "crm_get_deals", "arguments": {"stage": stage}}],
            "expected_records": [{"tool_name": "crm_get_deals", "id_field": "name",
                                  "ids": [f"Maven {row['opportunity_id']} - {row['product']}" for row in selected]}],
            "answer_patterns": [rf"\b{count}\b", "unavailable|missing|not .*record|not .*provided"] if stage == "OPEN" else [rf"\b{count}\b"],
            "forbidden_tools": ["crm_get_deals_batch"],
        }
    development = [deals_case("Acme Corporation", "OPEN", "maven-acme-open"), {
        "id": "maven-unknown-company", "message": "Find the company Definitely Absent Maven Fixture 987654.",
        "required_calls": [{"tool_name": "crm_search_accounts", "arguments": {"company_name": "Definitely Absent Maven Fixture 987654"}}],
        "expected_records": [{"tool_name": "crm_search_accounts", "id_field": "company_name", "ids": []}],
        "forbidden_tools": ["crm_get_deals", "crm_get_contacts"],
        "answer_patterns": ["no .*match|could not find|couldn't find|not found|no .*company"]}, {
        "id": "maven-no-customer-contacts", "message": "Who are the contacts for Acme Corporation? If none are recorded, say so without inventing anyone.",
        "required_calls": [{"tool_name": "crm_get_contacts", "arguments": {}}],
        "expected_records": [{"tool_name": "crm_get_contacts", "id_field": "contact_id", "ids": []}],
        "answer_patterns": ["no .*contact|not .*contact|couldn't find .*contact|none"],
        "forbidden_patterns": ["Moses Frase", "Darcel Schlecht"]},
        deals_case("Hottechi", "ALL", "maven-pagination-200-deals"),
        deals_case("Iselectrics", "ALL", "maven-small-company")]
    heldout = [deals_case(name, stage, label) for name, stage, label in (
        ("Cancity", "OPEN", "maven-heldout-cancity-open"),
        ("Isdom", "ALL", "maven-heldout-isdom-all"),
        ("Betatech", "OPEN", "maven-heldout-betatech-open"))]
    (ROOT / "evaluation-development.json").write_text(json.dumps(development, indent=2) + "\n")
    (ROOT / "evaluation-heldout.json").write_text(json.dumps(heldout, indent=2) + "\n")
    print(json.dumps({key: manifest[key] for key in ("companies", "deals", "contacts", "unassociated_deals", "source_stage_counts")}, indent=2))
    print("Import file:", path)


if __name__ == "__main__":
    main()
