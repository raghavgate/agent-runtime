"""Read-only CRM export adapter. No orchestration or model dependencies."""
import json
from datetime import date
from pathlib import Path
from tool import Tool, ToolCallError
from schema import validate, json_schema
from integrations.common import envelope_schema, result, source

ACCOUNT_ID_SCHEMA = {"type": ["integer", "string"], "minLength": 1}
ACCOUNT_SCHEMA = {"customer_id": ACCOUNT_ID_SCHEMA, "company_name": "string", "owner": "string",
                  "renewal_date": "string", "annual_value": "number", "support_customer_id": "string"}
CONTACT_SCHEMA = {"contact_id": "string", "customer_id": ACCOUNT_ID_SCHEMA, "full_name": "string",
                  "role": "string", "email": {"type": ["string", "null"]}, "is_primary": "boolean",
                  "last_contacted_date": {"type": ["string", "null"]}}
DEAL_STAGES = ["PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"]
DEAL_SCHEMA = {"deal_id": "string", "customer_id": ACCOUNT_ID_SCHEMA, "name": "string",
               "amount": {"type": "number", "minimum": 0}, "currency": {"type": "string", "enum": ["USD"]},
               "stage": {"type": "string", "enum": DEAL_STAGES}, "expected_close_date": "string",
               "owner": "string", "updated_at": "string"}


class CRMIntegration:
    def __init__(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        validate(payload, {"type": "object", "properties": {
            "accounts": json_schema([ACCOUNT_SCHEMA]), "contacts": json_schema([CONTACT_SCHEMA]),
            "deals": json_schema([DEAL_SCHEMA])}, "required": ["accounts"], "additionalProperties": False})
        self.accounts = payload["accounts"]
        self.contacts = payload.get("contacts", [])
        self.deals = payload.get("deals", [])
        self.has_contacts = "contacts" in payload
        self.has_deals = "deals" in payload
        self.rich_export = "contacts" in payload or "deals" in payload
        if len({str(row["customer_id"]) for row in self.accounts}) != len(self.accounts):
            raise ValueError("CRM customer IDs must be unique")
        account_ids = {row["customer_id"] for row in self.accounts}
        for collection, id_key in ((self.contacts, "contact_id"), (self.deals, "deal_id")):
            if len({row[id_key] for row in collection}) != len(collection):
                raise ValueError("CRM child record IDs must be unique")
            if any(row["customer_id"] not in account_ids for row in collection):
                raise ValueError("CRM child record references an unknown account")
        for account in self.accounts:
            self._date(account["renewal_date"])
        for contact in self.contacts:
            if contact["last_contacted_date"] is not None:
                self._date(contact["last_contacted_date"])
        for deal in self.deals:
            self._date(deal["expected_close_date"])
            self._date(deal["updated_at"])

    @staticmethod
    def _date(value):
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("CRM dates must be valid YYYY-MM-DD values") from None

    def summary(self):
        return {"accounts": len(self.accounts),
                **({"contacts": len(self.contacts)} if self.has_contacts else {}),
                **({"deals": len(self.deals)} if self.has_deals else {})}

    def _known_account(self, customer_id):
        if not any(row["customer_id"] == customer_id for row in self.accounts):
            raise ToolCallError("CRM account not found; use a customer_id returned by search")

    @staticmethod
    def _validate_batch_ids(customer_ids):
        if not 1 <= len(customer_ids) <= 50:
            raise ToolCallError("Batch reads require between 1 and 50 customer IDs")
        if len({str(value) for value in customer_ids}) != len(customer_ids):
            raise ToolCallError("Batch customer IDs must be unique")

    def get_contacts(self, customer_id):
        self._known_account(customer_id)
        matches = sorted([row for row in self.contacts if row["customer_id"] == customer_id],
                         key=lambda row: (not row["is_primary"], row["contact_id"]))
        sources = [source("crm", "contact-search", customer_id, "Account contact search")]
        sources.extend(source("crm", "contacts", row["contact_id"], row["full_name"]) for row in matches)
        return result(matches, sources)

    def get_contacts_batch(self, customer_ids):
        self._validate_batch_ids(customer_ids)
        rows, sources = [], []
        for customer_id in customer_ids:
            response = self.get_contacts(customer_id)
            rows.extend(response["data"])
            sources.extend(response["sources"])
        return result(rows, sources)

    def get_deals(self, customer_id, stage):
        self._known_account(customer_id)
        matches = [row for row in self.deals if row["customer_id"] == customer_id]
        if stage == "OPEN":
            matches = [row for row in matches if row["stage"] not in ("CLOSED_WON", "CLOSED_LOST")]
        elif stage != "ALL":
            matches = [row for row in matches if row["stage"] == stage]
        sources = [source("crm", "deal-search", f"{customer_id}_{stage}", "Account deal search")]
        sources.extend(source("crm", "deals", row["deal_id"], row["name"]) for row in matches)
        return result(matches, sources)

    def get_deals_batch(self, customer_ids, stage):
        self._validate_batch_ids(customer_ids)
        rows, sources = [], []
        for customer_id in customer_ids:
            response = self.get_deals(customer_id, stage)
            rows.extend(response["data"])
            sources.extend(response["sources"])
        return result(rows, sources)

    def list_renewals(self, start_date, end_date):
        try:
            self._date(start_date)
            self._date(end_date)
        except ValueError as error:
            raise ToolCallError(str(error)) from error
        if start_date > end_date:
            raise ToolCallError("Renewal start date must be on or before end date")
        matches = [row for row in self.accounts if start_date <= row["renewal_date"] <= end_date]
        matches.sort(key=lambda row: (row["renewal_date"], str(row["customer_id"])))
        sources = [source("crm", "renewals", f"{start_date}_{end_date}", "Renewal date search")]
        sources.extend(source("crm", "accounts", row["customer_id"], row["company_name"]) for row in matches)
        return result(matches, sources)

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
        tools = [
            Tool("crm_search_accounts", "Find CRM accounts by company name. Returns full account details, owner, renewal, annual value and support mapping; reuse these without another lookup. Multiple matches require clarification.",
                 {"type": "object", "properties": {"company_name": {"type": "string", "minLength": 1, "pattern": r"\S"}},
                  "required": ["company_name"], "additionalProperties": False},
                 envelope_schema([ACCOUNT_SCHEMA]), self.search),
            Tool("crm_get_account", "Look up a known CRM customer_id when details are not already available from search. Search already returns the full account.",
                 {"type": "object", "properties": {"customer_id": ACCOUNT_ID_SCHEMA},
                  "required": ["customer_id"], "additionalProperties": False},
                 envelope_schema([ACCOUNT_SCHEMA]), self.get),
        ]
        if self.rich_export:
            account_input = {"type": "object", "properties": {"customer_id": ACCOUNT_ID_SCHEMA},
                             "required": ["customer_id"], "additionalProperties": False}
            account_ids_input = {"type": "array", "items": ACCOUNT_ID_SCHEMA}
            optional_tools = [
                Tool("crm_get_contacts", "List contacts for a known account, including primary designation and nullable email. Never infer a primary contact or missing email.",
                     account_input, envelope_schema([CONTACT_SCHEMA]), self.get_contacts),
                Tool("crm_get_deals", "List account deals. OPEN excludes CLOSED_WON and CLOSED_LOST; ALL includes all stages. Amounts are in USD. Use stage explicitly.",
                     {"type": "object", "properties": {"customer_id": ACCOUNT_ID_SCHEMA,
                      "stage": {"type": "string", "enum": ["ALL", "OPEN"] + DEAL_STAGES}},
                      "required": ["customer_id", "stage"], "additionalProperties": False},
                     envelope_schema([DEAL_SCHEMA]), self.get_deals),
                Tool("crm_get_contacts_batch", "List contacts for multiple known accounts in one read. Use this after a multi-account search instead of calling crm_get_contacts repeatedly. Primary designations and nullable emails retain their original meaning.",
                     {"type": "object", "properties": {"customer_ids": account_ids_input},
                      "required": ["customer_ids"], "additionalProperties": False},
                     envelope_schema([CONTACT_SCHEMA]), self.get_contacts_batch),
                Tool("crm_get_deals_batch", "List deals for multiple known accounts in one read. Use OPEN to exclude CLOSED_WON and CLOSED_LOST. Results retain customer_id for grouping.",
                     {"type": "object", "properties": {"customer_ids": account_ids_input,
                      "stage": {"type": "string", "enum": ["ALL", "OPEN"] + DEAL_STAGES}},
                      "required": ["customer_ids", "stage"], "additionalProperties": False},
                     envelope_schema([DEAL_SCHEMA]), self.get_deals_batch),
                Tool("crm_list_renewals", "List accounts renewing within an inclusive date range. Use explicit YYYY-MM-DD start and end dates; does not infer risk or support status.",
                     {"start_date": "string", "end_date": "string"}, envelope_schema([ACCOUNT_SCHEMA]), self.list_renewals),
            ]
            tools.extend(tool for tool in optional_tools
                         if (tool.name not in ("crm_get_contacts", "crm_get_contacts_batch") or self.has_contacts)
                         and (tool.name not in ("crm_get_deals", "crm_get_deals_batch") or self.has_deals))
        return tools
