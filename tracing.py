"""One JSON file per run. Never records environment variables or API keys."""
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRecord:
    user_message: str
    provider: str
    model: str
    run_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=utc_now)
    status: str = "running"
    answer: str = ""
    sources: list = field(default_factory=list)
    events: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    usage_available: bool = False
    estimated_cost_usd: object = None
    duration_ms: float = 0
    tool_calls: int = 0
    error: object = None
    metadata: dict = field(default_factory=dict)

    def event(self, kind, **details):
        self.events.append({"type": kind, "timestamp": utc_now(), **details})

    def to_dict(self):
        return asdict(self)


class TraceStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def save(self, run):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (run.run_id + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        return path

    def get(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run ID")
        return json.loads((self.directory / (run_id + ".json")).read_text(encoding="utf-8"))

    def list_runs(self, limit=50):
        if not self.directory.exists():
            return []
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        runs = []
        for path in paths[:limit]:
            record = json.loads(path.read_text(encoding="utf-8"))
            runs.append({key: record[key] for key in (
                "run_id", "user_message", "status", "provider", "model", "duration_ms", "started_at"
            )})
        return runs
