"""Read-only CRM export adapter. No orchestration or model dependencies."""
import json
from pathlib import Path
from tool import Tool
from schema import validate, json_schema
from integrations.common import envelope_schema, result, source

ACCOUNT_SCHEMA = {"customer_id": "integer", "company_name": "string", "owner": "string",
                  "renewal_date": "string", "annual_value": "number", "support_customer_id": "string"}


class CRMIntegration:
    def __init__(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        validate(payload, json_schema({"accounts": [ACCOUNT_SCHEMA]}))
        self.accounts = payload["accounts"]
        if len({row["customer_id"] for row in self.accounts}) != len(self.accounts):
            raise ValueError("CRM customer IDs must be unique")

    def search(self, company_name):
        query = company_name.strip().casefold()
        exact = [row for row in self.accounts if row["company_name"].casefold() == query]
        matches = exact or [row for row in self.accounts if query in row["company_name"].casefold()]
        return result(matches, [source("crm", "accounts", row["customer_id"], row["company_name"]) for row in matches])

    def get(self, customer_id):
        matches = [row for row in self.accounts if row["customer_id"] == customer_id]
        # Empty data is an explicit not-found result; never guesses an account.
        return result(matches, [source("crm", "accounts", row["customer_id"], row["company_name"]) for row in matches])

    def tools(self):
        return [
            Tool("crm_search_accounts", "Find CRM accounts by company name. Multiple matches require clarification.",
                 {"type": "object", "properties": {"company_name": {"type": "string", "minLength": 1, "pattern": r"\S"}},
                  "required": ["company_name"], "additionalProperties": False},
                 envelope_schema([ACCOUNT_SCHEMA]), self.search),
            Tool("crm_get_account", "Get account details, renewal date and support_customer_id mapping from a known CRM customer_id.",
                 {"customer_id": "integer"}, envelope_schema([ACCOUNT_SCHEMA]), self.get),
        ]
