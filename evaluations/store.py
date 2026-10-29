"""Local reports and a stable first-run baseline for each dataset/suite/model."""
import hashlib
import json
import re
from pathlib import Path


class ReportStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def get(self, report_id):
        if not re.fullmatch(r"[a-f0-9]{32}", report_id):
            raise ValueError("Invalid evaluation ID")
        return json.loads((self.directory / (report_id + ".json")).read_text(encoding="utf-8"))

    def save(self, report):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (report["evaluation_id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def list_reports(self, limit=20):
        if not self.directory.exists():
            return []
        paths = [path for path in self.directory.glob("*.json") if re.fullmatch(r"[a-f0-9]{32}", path.stem)]
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths[:limit]]

    def baseline_path(self, report):
        identity = [report[key] for key in ("dataset_hash", "suite_hash", "kind", "model")]
        identity.append(report.get("grader_hash", "legacy"))
        identity.append(report.get("split", "development"))
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return self.directory / ("baseline-" + digest + ".json")

    def baseline(self, report):
        pointer = self.baseline_path(report)
        if not pointer.exists():
            return None
        return self.get(json.loads(pointer.read_text())["evaluation_id"])

    def claim_baseline(self, report):
        """Never replace a baseline implicitly, including concurrent evaluations."""
        try:
            with self.baseline_path(report).open("x", encoding="utf-8") as handle:
                handle.write(json.dumps({"evaluation_id": report["evaluation_id"]}))
            self.baseline_path(report).chmod(0o600)
            return True
        except FileExistsError:
            return False
