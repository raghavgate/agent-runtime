"""Small deterministic demo, not a language model or model-accuracy benchmark."""
import re
from copy import deepcopy


class DemoModel:
    provider = 'demo'
    model_name = 'deterministic-demo'
    last_usage = None

    def __init__(self):
        self.reset()

    def reset(self):
        self.account = None
        self.matches = []
        self.intent = 'tickets'
        self.stage = 0

    def start_turn(self, user_message, state):
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError('Incompatible demo conversation state')
        self.reset()
        self.account = deepcopy(state.get('account'))
        self.matches = deepcopy(state.get('matches', []))
        self.intent = state.get('intent', 'tickets')

    def export_state(self):
        return {'version': 1, 'account': deepcopy(self.account), 'matches': deepcopy(self.matches), 'intent': self.intent}

    def _request_tickets(self, tools):
        account = self.account
        self.stage = 2
        if not account.get('support_customer_id') or not any(tool['name'] == 'support_get_open_tickets' for tool in tools or []):
            return self.final(f"Support tickets could not be checked for {account['company_name']}. [crm:accounts/{account['customer_id']}]")
        return self.call('support_get_open_tickets', {'organization_id': account['support_customer_id']})

    def generate(self, user_message, tool_result=None, tools=None):
        if self.stage == 0:
            self.stage = 1
            text = user_message.strip()
            if re.search(r'\b(contact|contacts|deal|deals|pipeline|meeting)\b', text, re.IGNORECASE):
                return self.final('The scripted demo supports account briefs, open tickets, and basic account follow-ups. Contact, pipeline and meeting-preparation questions require live-model mode; no model request was made.')
            query = re.sub(r'[?.!]$', '', text)
            match = re.search(r'\b(?:for|about)\s+(.+)$', query, re.IGNORECASE)
            if match:
                self.account = None
                self.matches = []
                self.intent = 'brief' if 'brief' in text.casefold() else 'tickets'
                return self.call('crm_search_accounts', {'company_name': match.group(1).strip()})
            if self.matches:
                selected = [row for row in self.matches if query.casefold() in row['company_name'].casefold()]
                if len(selected) == 1:
                    self.account = selected[0]
                    self.matches = []
                    return self._request_tickets(tools)
                choices = ', '.join(f"{row['company_name']} [crm:accounts/{row['customer_id']}]" for row in self.matches)
                return self.final('Which account did you mean? ' + choices)
            if self.account:
                account = self.account
                citation = f"[crm:accounts/{account['customer_id']}]"
                if re.search(r'\b(owner|owns)\b', text, re.IGNORECASE):
                    return self.final(f"{account['company_name']}'s owner is {account['owner']}. {citation}")
                if 'renewal' in text.casefold():
                    return self.final(f"{account['company_name']}'s renewal date is {account['renewal_date']}. {citation}")
                if re.search(r'\b(ticket|tickets|brief)\b', text, re.IGNORECASE):
                    self.intent = 'brief' if 'brief' in text.casefold() else 'tickets'
                    return self._request_tickets(tools)
            return self.final("Try 'Find open tickets for COMPANY' or 'Prepare an account brief for COMPANY'. Live-model mode supports broader questions.")
        if isinstance(tool_result, dict) and 'error' in tool_result:
            citation = f" [crm:accounts/{self.account['customer_id']}]" if self.account else ''
            return self.final('The records could not be retrieved.' + citation)
        if self.stage == 1:
            rows = tool_result['data']
            self.matches = rows if len(rows) > 1 else []
            if not rows:
                return self.final('Customer not found. Please check the company name.')
            if len(rows) > 1:
                choices = ', '.join(f"{row['company_name']} [crm:accounts/{row['customer_id']}]" for row in rows)
                return self.final('Which account did you mean? ' + choices)
            self.account = rows[0]
            return self._request_tickets(tools)
        account = self.account
        tickets = tool_result['data']
        name = account['company_name']
        noun = 'ticket' if len(tickets) == 1 else 'tickets'
        lines = [f"{name} has {len(tickets)} open {noun}. [support:organizations/{account['support_customer_id']}]"]
        if self.intent == 'brief':
            lines.insert(0, f"{name}: owner {account['owner']}; renewal {account['renewal_date']}; annual value ${account['annual_value']:,}. [crm:accounts/{account['customer_id']}]")
        lines.extend(f"- #{row['ticket_id']} ({row['priority']}): {row['subject']} [support:tickets/{row['ticket_id']}]" for row in tickets)
        return self.final('\n'.join(lines))

    @staticmethod
    def call(name, arguments):
        return {'type': 'tool_call', 'tool_name': name, 'arguments': arguments}

    @staticmethod
    def final(content):
        return {'type': 'final_answer', 'content': content}
