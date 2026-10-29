"""Validate a local account export and create an isolated CRM workspace.

No network calls, guessing of missing values, or changes to the original export.
"""
import csv
import json
import math
from datetime import date
from pathlib import Path
from integrations.crm import CRMIntegration, ACCOUNT_SCHEMA
from schema import json_schema, validate

FIELDS = tuple(ACCOUNT_SCHEMA)


def read_export(source, mapping=None):
    source = Path(source)
    mapping = mapping or {field: field for field in FIELDS}
    if set(mapping) != set(FIELDS) or not all(isinstance(value, str) for value in mapping.values()):
        raise ValueError("Mapping must provide one source column for each canonical account field")
    if source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = set(mapping.values()) - set(reader.fieldnames or [])
            if missing:
                raise ValueError("Export is missing mapped columns: " + ", ".join(sorted(missing)))
            rows = list(reader)
    elif source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload.get("accounts") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("JSON export must be an account list or an object containing accounts")
    else:
        raise ValueError("Export must be a CSV or JSON file")
    if not rows:
        raise ValueError("Export contains no accounts")
    accounts, seen = [], set()
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or any(column not in row for column in mapping.values()):
            raise ValueError(f"Account row {number} is missing mapped fields")
        account = {field: row[column] for field, column in mapping.items()}
        if isinstance(account["customer_id"], str):
            account["customer_id"] = account["customer_id"].strip()
        for field in ("company_name", "owner", "renewal_date", "support_customer_id"):
            if not isinstance(account[field], str):
                raise ValueError(f"Account row {number}: {field} must be text")
            account[field] = account[field].strip()
        if not account["company_name"] or not account["owner"]:
            raise ValueError(f"Account row {number} needs a company name and owner")
        value = account["annual_value"]
        if isinstance(value, bool):
            raise ValueError(f"Account row {number}: annual_value must be a nonnegative number")
        try:
            # No currency conversion or locale guessing; commas/$ are not silently removed.
            account["annual_value"] = float(value)
            if not math.isfinite(account["annual_value"]) or account["annual_value"] < 0:
                raise ValueError()
            date.fromisoformat(account["renewal_date"])
        except (TypeError, ValueError):
            raise ValueError(f"Account row {number} needs a finite nonnegative annual_value and ISO renewal_date (YYYY-MM-DD)") from None
        try:
            validate(account, json_schema(ACCOUNT_SCHEMA))
        except ValueError as error:
            raise ValueError(f"Account row {number}: {error}") from None
        identity = str(account["customer_id"])
        if identity in seen:
            raise ValueError(f"Account row {number} has a duplicate customer ID")
        seen.add(identity)
        accounts.append(account)
    return {"accounts": accounts}


def import_crm(source, output_dir=None, mapping_path=None, dry_run=False):
    mapping = json.loads(Path(mapping_path).read_text()) if mapping_path else None
    payload = read_export(source, mapping)
    if dry_run:
        return {"valid": True, "accounts": len(payload["accounts"]), "written": False}
    if output_dir is None:
        raise ValueError("Choose --output-dir (recommended: local_data/my-crm)")
    destination = Path(output_dir)
    if destination.exists():
        raise ValueError("Output directory already exists; choose a new directory to preserve existing data")
    destination.mkdir(parents=True, mode=0o700)
    account_path = destination / "accounts.json"
    account_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    account_path.chmod(0o600)
    CRMIntegration(account_path)
    # Import is CRM-only: never attach sample support tickets to real accounts.
    config = {"integrations": [{"kind": "crm_export", "path": "accounts.json"}],
              "max_tool_calls": 6, "max_output_tokens": 1200, "require_sources": True,
              "trace_dir": ".runs", "evaluation_dir": ".evaluations"}
    default_config = Path(__file__).resolve().parents[1] / "config.json"
    config["pricing"] = json.loads(default_config.read_text()).get("pricing", {})
    config_path = destination / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    config_path.chmod(0o600)
    return {"valid": True, "accounts": len(payload["accounts"]), "written": True, "config": str(config_path.resolve())}
