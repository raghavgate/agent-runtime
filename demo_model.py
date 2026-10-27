"""Deterministic demo, NOT an LLM. Only supports the documented account prompts."""
import re


class DemoModel:
    provider = "demo"
    model_name = "deterministic-demo"
    last_usage = None

    def reset(self):
        self.account = None
        self.account_sources = []
        self.stage = 0

    def generate(self, user_message, tool_result=None, tools=None):
        if self.stage == 0:
            self.stage = 1
            # Handles the documented prompts, not general natural language.
            query = re.sub(r"[?.!]$", "", user_message.strip())
            match = re.search(r"\b(?:for|about)\s+(.+)$", query, re.IGNORECASE)
            if not match:
                return self.final("Demo mode supports 'Find open tickets for COMPANY' or 'Prepare an account brief for COMPANY'. Use OpenAI mode for other tasks.")
            return self.call("crm_search_accounts", {"company_name": match.group(1).strip()})
        if isinstance(tool_result, dict) and "error" in tool_result:
            return self.final("I could not retrieve the records: " + tool_result["error"]["message"])
        if self.stage == 1:
            rows = tool_result["data"]
            if not rows:
                return self.final("Customer not found. Please check the company name.")
            self.account_sources = tool_result["sources"]
            if len(rows) > 1:
                choices = ", ".join(f"{row['company_name']} [crm:accounts/{row['customer_id']}]" for row in rows)
                return self.final("Which account did you mean? " + choices)
            self.account = rows[0]
            self.stage = 2
            return self.call("support_get_open_tickets", {"organization_id": self.account["support_customer_id"]})
        account = self.account
        tickets = tool_result["data"]
        name = account["company_name"]
        noun = "ticket" if len(tickets) == 1 else "tickets"
        lines = [f"{name} has {len(tickets)} open {noun}. [support:organizations/{account['support_customer_id']}]"]
        if "brief" in user_message.casefold() or "renewal" in user_message.casefold():
            lines.insert(0, f"{name}: owner {account['owner']}; renewal {account['renewal_date']}; annual value ${account['annual_value']:,}. [crm:accounts/{account['customer_id']}]")
        lines.extend(f"- #{row['ticket_id']} ({row['priority']}): {row['subject']} [support:tickets/{row['ticket_id']}]" for row in tickets)
        return self.final("\n".join(lines))

    @staticmethod
    def call(name, arguments):
        return {"type": "tool_call", "tool_name": name, "arguments": arguments}

    @staticmethod
    def final(content):
        return {"type": "final_answer", "content": content}
