"""Read-only support export adapter; system IDs are explicitly distinct from CRM IDs."""
import json
from pathlib import Path
from schema import json_schema, validate
from tool import Tool, ToolCallError
from integrations.common import envelope_schema, result, source

TICKET_SCHEMA = {"ticket_id": "integer", "organization_id": "string", "status": "string",
                 "priority": "string", "subject": "string"}


class SupportIntegration:
    def __init__(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        validate(payload, json_schema({"organizations": ["string"], "tickets": [TICKET_SCHEMA]}))
        self.organizations = set(payload["organizations"])
        self.tickets = payload["tickets"]
        if any(row["organization_id"] not in self.organizations for row in self.tickets):
            raise ValueError("Support ticket references an unknown organization")
        if len({row["ticket_id"] for row in self.tickets}) != len(self.tickets):
            raise ValueError("Support ticket IDs must be unique")

    def get_open_tickets(self, organization_id):
        if organization_id not in self.organizations:
            raise ToolCallError("Support organization not found; use the CRM support_customer_id mapping")
        matches = [row for row in self.tickets if row["organization_id"] == organization_id and row["status"] == "OPEN"]
        # The organization source supports an empty result too.
        sources = [source("support", "organizations", organization_id, f"Open tickets for {organization_id}")]
        sources.extend(source("support", "tickets", row["ticket_id"], row["subject"]) for row in matches)
        return result(matches, sources)

    def tools(self):
        return [Tool("support_get_open_tickets", "Get OPEN tickets using an organization_id from a CRM account's support_customer_id.",
                     {"organization_id": "string"}, envelope_schema([TICKET_SCHEMA]), self.get_open_tickets)]
