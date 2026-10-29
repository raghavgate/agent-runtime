"""Application composition: this is where integrations are selected, not Runtime."""
import hashlib
import json
import math
import os
from uuid import UUID, uuid4
from pathlib import Path
from demo_model import DemoModel
from integrations.crm import CRMIntegration
from integrations.hubspot import HubSpotIntegration
from integrations.ticketing import SupportIntegration
from registry import ToolRegistry
from runtime import Runtime
from runtime import RunError
from tracing import TraceStore, RunRecord, utc_now
from pricing import rates_for
from conversations import ConversationStore, ConversationMismatch

ROOT = Path(__file__).resolve().parent


class Application:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path or ROOT / "config.json").resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        for key in ("instructions", "workflow_instructions"):
            if key in self.config and not isinstance(self.config[key], str):
                raise ValueError(key + " must be a string")
        self.base = self.config_path.parent
        self.registry = ToolRegistry()
        self.integrations = []
        self.dataset_counts = {}
        factories = {"crm_export": CRMIntegration, "support_export": SupportIntegration}
        fingerprint = hashlib.sha256()
        for entry in self.config["integrations"]:
            kind = entry.get("kind")
            if kind not in factories and kind != "hubspot_crm":
                raise ValueError("Unknown integration kind: " + entry["kind"])
            fingerprint.update(kind.encode())
            if kind == "hubspot_crm":
                # Live workspaces have no stable data snapshot. Fingerprint the connector
                # configuration without reading or storing the credential value.
                fingerprint.update(json.dumps(entry, sort_keys=True).encode())
                integration = HubSpotIntegration(entry)
            else:
                path = self.base / entry["path"]
                fingerprint.update(path.read_bytes())
                integration = factories[kind](path)
            self.integrations.append(integration)
            summary = getattr(integration, "summary", None)
            if summary:
                for key, count in summary().items():
                    self.dataset_counts[key] = self.dataset_counts.get(key, 0) + count
            for tool in integration.tools():
                self.registry.register(tool)
        self.dataset_hash = fingerprint.hexdigest()
        self.config_hash = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        code = hashlib.sha256()
        paths = sorted(ROOT.glob("*.py")) + sorted((ROOT / "integrations").glob("*.py")) + sorted((ROOT / "evaluations").glob("*.py"))
        for path in paths:
            code.update(str(path.relative_to(ROOT)).encode())
            code.update(path.read_bytes())
        self.code_hash = code.hexdigest()
        self.traces = TraceStore(self.base / self.config.get("trace_dir", ".runs"))
        self.evaluation_dir = self.base / self.config.get("evaluation_dir", ".evaluations")
        self.conversations = ConversationStore(self.base / self.config.get("conversation_dir", ".conversations"))
        self.conversation_max_turns = self.config.get("conversation_max_turns", 24)
        if isinstance(self.conversation_max_turns, bool) or not isinstance(self.conversation_max_turns, int) or not 1 <= self.conversation_max_turns <= 200:
            raise ValueError("conversation_max_turns must be between 1 and 200")
        for key in ("input_usd_per_million", "output_usd_per_million"):
            value = self.config.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError(key + " must be a finite nonnegative number")
        self.validate_data()

    @staticmethod
    def sample_dataset_hash():
        fingerprint = hashlib.sha256()
        for kind, filename in (("crm_export", "crm.json"), ("support_export", "support.json")):
            fingerprint.update(kind.encode())
            fingerprint.update((ROOT / "data" / filename).read_bytes())
        return fingerprint.hexdigest()

    def rates(self, model_name):
        return rates_for(self.config, model_name)

    def validate_data(self):
        """Structural and cross-system integrity checks, without constructing a model."""
        crm_entries = [entry for entry in self.config["integrations"] if entry["kind"] == "crm_export"]
        support_entries = [entry for entry in self.config["integrations"] if entry["kind"] == "support_export"]
        if support_entries:
            organizations = set()
            for entry in support_entries:
                organizations.update(json.loads((self.base / entry["path"]).read_text())["organizations"])
            for entry in crm_entries:
                accounts = json.loads((self.base / entry["path"]).read_text())["accounts"]
                if any(account["support_customer_id"] and account["support_customer_id"] not in organizations for account in accounts):
                    raise ValueError("A CRM support mapping references an unknown support organization")
        return {"valid": True, "workspace": self.config.get("workspace_name", "Sample workspace"),
                "data_origin": self.config.get("data_origin", "sample"), "as_of": self.config.get("as_of"),
                "counts": self.dataset_counts, "dataset_hash": self.dataset_hash, "model_requests": 0}

    def check_connections(self):
        """Perform explicit provider checks; normal construction and local validation do not call APIs."""
        checks = []
        for integration in self.integrations:
            check = getattr(integration, "check_connection", None)
            if check:
                checks.append(check())
            else:
                checks.append({"service": integration.__class__.__name__, "connected": True,
                               "mode": "local export"})
        return {"connected": all(check["connected"] for check in checks), "connections": checks}

    def model(self, provider="demo", model_name=None, max_output_tokens=None):
        if provider == "demo":
            return DemoModel()
        if provider == "openai":
            from openai_model import OpenAIModel, DEFAULT_INSTRUCTIONS
            instructions = self.config.get("instructions", DEFAULT_INSTRUCTIONS)
            if self.config.get("workflow_instructions"):
                instructions += "\nWorkspace workflow requirements:\n" + self.config["workflow_instructions"]
            return OpenAIModel(model_name or os.environ.get("OPENAI_MODEL"),
                               instructions=instructions,
                               max_output_tokens=max_output_tokens or self.config.get("max_output_tokens", 1200),
                               max_history_bytes=self.config.get("conversation_max_history_bytes", 120000))
        raise ValueError("Unknown provider: " + provider)

    def runtime(self, model_name=None, max_tool_calls=None, max_total_tokens=None, purpose="task"):
        rates = self.rates(model_name)
        return Runtime(self.registry, max_tool_calls=self.config.get("max_tool_calls", 10) if max_tool_calls is None else max_tool_calls,
                       require_sources=self.config.get("require_sources", True), trace_store=self.traces,
                       input_rate=rates["input_usd_per_million"] if rates else None,
                       output_rate=rates["output_usd_per_million"] if rates else None,
                       max_total_tokens=max_total_tokens,
                       metadata={"dataset_hash": self.dataset_hash, "config_hash": self.config_hash,
                                 "code_hash": self.code_hash, "purpose": purpose, "pricing": rates})

    def run(self, message, provider="demo", model_name=None, max_tool_calls=None,
            max_output_tokens=None, max_total_tokens=None, purpose="task"):
        model = self.model(provider, model_name, max_output_tokens)
        try:
            return self.runtime(model.model_name, max_tool_calls, max_total_tokens, purpose).run_record(model, message)
        finally:
            close = getattr(model, "close", None)
            if close:
                close()

    @staticmethod
    def resolved_model(provider, model_name=None):
        if provider == "demo":
            return DemoModel.model_name
        if provider == "openai":
            resolved = model_name or os.environ.get("OPENAI_MODEL")
            if not resolved:
                raise ValueError("Set OPENAI_MODEL or pass --model")
            return resolved
        raise ValueError("Unknown provider")

    def create_conversation(self, provider="demo", model_name=None):
        model_name = self.resolved_model(provider, model_name)
        conversation = self.conversations.create(provider, model_name, self.dataset_hash, self.config_hash)
        return self.conversation_detail(conversation['conversation_id'], provider, model_name)

    def _check_conversation(self, conversation, provider, model_name):
        expected = (provider, self.resolved_model(provider, model_name), self.dataset_hash, self.config_hash)
        actual = tuple(conversation[key] for key in ('provider', 'model', 'dataset_hash', 'config_hash'))
        if actual != expected:
            raise ConversationMismatch("Workspace or model settings changed. Start a new conversation.")

    def conversation_detail(self, conversation_id, provider="demo", model_name=None):
        conversation = self.conversations.get(conversation_id)
        self._check_conversation(conversation, provider, model_name)
        detail = self.conversations.summary(conversation)
        detail['turns'] = [self.traces.get(turn['run_id']) for turn in conversation['turns']]
        costs = [turn['estimated_cost_usd'] for turn in detail['turns']]
        detail['estimated_cost_usd'] = sum(cost for cost in costs if cost is not None) if costs and all(cost is not None for cost in costs) else None
        detail['max_turns'] = self.conversation_max_turns
        # Never expose the opaque provider transcript or encrypted reasoning state.
        return detail

    def list_conversations(self, provider="demo", model_name=None):
        return self.conversations.list_conversations(provider, self.resolved_model(provider, model_name),
                                                     self.dataset_hash, self.config_hash)

    def send_message(self, conversation_id, message, provider="demo", model_name=None, request_id=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ValueError("Enter a message between 1 and 8000 characters")
        try:
            request_id = UUID(request_id).hex if request_id is not None else uuid4().hex
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid message request ID") from None
        with self.conversations.locked(conversation_id) as conversation:
            self._check_conversation(conversation, provider, model_name)
            for turn in conversation['turns']:
                if turn['request_id'] == request_id:
                    previous = RunRecord(**self.traces.get(turn['run_id']))
                    if previous.user_message != message:
                        raise ValueError("Message request ID was already used for different text")
                    if previous.status == 'failed':
                        raise RunError(previous.error['message'], previous)
                    return previous
            if len(conversation['turns']) >= self.conversation_max_turns:
                raise ValueError("Conversation turn limit reached. Start a new conversation.")
            model = self.model(provider, conversation['model'])
            if not callable(getattr(model, 'export_state', None)) or not callable(getattr(model, 'start_turn', None)):
                close = getattr(model, 'close', None)
                if close:
                    close()
                raise ValueError("This model adapter does not support conversations")
            runtime = self.runtime(conversation['model'],
                                   max_tool_calls=self.config.get('conversation_max_tool_calls', 6),
                                   max_total_tokens=self.config.get('conversation_max_total_tokens', 20000),
                                   purpose='conversation')
            runtime.metadata.update({'conversation_id': conversation_id, 'request_id': request_id,
                                     'turn': len(conversation['turns']) + 1})
            failure = None
            try:
                try:
                    record = runtime.run_record(model, message, model_state=conversation['model_state'],
                                                prior_sources=conversation['sources'])
                except RunError as error:
                    failure = error
                    record = error.record
                if not failure:
                    conversation['model_state'] = model.export_state()
                    conversation['sources'] = record.sources
                conversation['turns'].append({'run_id': record.run_id, 'request_id': request_id})
                if len(conversation['turns']) == 1:
                    conversation['title'] = ' '.join(message.split())[:90]
                conversation['revision'] += 1
                conversation['updated_at'] = utc_now()
                self.conversations.save(conversation)
            finally:
                close = getattr(model, 'close', None)
                if close:
                    close()
            if failure:
                raise failure
            return record
