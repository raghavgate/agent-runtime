"""Application composition: this is where integrations are selected, not Runtime."""
import hashlib
import json
import math
import os
from pathlib import Path
from demo_model import DemoModel
from integrations.crm import CRMIntegration
from integrations.ticketing import SupportIntegration
from registry import ToolRegistry
from runtime import Runtime
from tracing import TraceStore

ROOT = Path(__file__).resolve().parent


class Application:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path or ROOT / "config.json").resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.base = self.config_path.parent
        self.registry = ToolRegistry()
        factories = {"crm_export": CRMIntegration, "support_export": SupportIntegration}
        fingerprint = hashlib.sha256()
        for entry in self.config["integrations"]:
            if entry["kind"] not in factories:
                raise ValueError("Unknown integration kind: " + entry["kind"])
            path = self.base / entry["path"]
            fingerprint.update(entry["kind"].encode())
            fingerprint.update(path.read_bytes())
            integration = factories[entry["kind"]](path)
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
        for key in ("input_usd_per_million", "output_usd_per_million"):
            value = self.config.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError(key + " must be a finite nonnegative number")

    def model(self, provider="demo", model_name=None):
        if provider == "demo":
            return DemoModel()
        if provider == "openai":
            from openai_model import OpenAIModel, DEFAULT_INSTRUCTIONS
            return OpenAIModel(model_name or os.environ.get("OPENAI_MODEL"),
                               instructions=self.config.get("instructions", DEFAULT_INSTRUCTIONS))
        raise ValueError("Unknown provider: " + provider)

    def runtime(self):
        return Runtime(self.registry, max_tool_calls=self.config.get("max_tool_calls", 10),
                       require_sources=self.config.get("require_sources", True), trace_store=self.traces,
                       input_rate=self.config.get("input_usd_per_million"),
                       output_rate=self.config.get("output_usd_per_million"),
                       metadata={"dataset_hash": self.dataset_hash, "config_hash": self.config_hash,
                                 "code_hash": self.code_hash})

    def run(self, message, provider="demo", model_name=None):
        model = self.model(provider, model_name)
        try:
            return self.runtime().run_record(model, message)
        finally:
            close = getattr(model, "close", None)
            if close:
                close()
