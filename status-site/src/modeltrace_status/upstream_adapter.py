"""The only upstream-code boundary. No fingerprint algorithm is copied here.

The verdict policy is versioned separately from upstream's mathematical scorer.
It follows Guard's compatible/difference thresholds. These are relative closed-set weights, not authentication confidence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import subprocess
from pathlib import Path

POLICY = "guard-thresholds-v1-batch3"
# Fields that determine how a check is scored. The upstream Git revision is
# provenance only: commits that leave scorer and bank untouched keep the method.
METHOD_KEYS = ("scorer_sha256", "bank_sha256", "policy", "probe_profile")


def method_version(provenance):
    fields = {k: provenance[k] for k in METHOD_KEYS if k in provenance}
    if len(fields) != len(METHOD_KEYS):
        return None
    return hashlib.sha256(str(sorted(fields.items())).encode()).hexdigest()[:16]


class UpstreamAdapter:
    def __init__(self, root: Path):
        source = root / "fingerprint.py"
        bank_file = root / "data/unified_bank.json"
        spec = importlib.util.spec_from_file_location("modeltrace_status_upstream", source)
        if not spec or not spec.loader:
            raise ValueError("Upstream fingerprint module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("generate_challenges", "analyze_global_outputs", "load_bank"):
            if not callable(getattr(module, name, None)):
                raise ValueError(f"Upstream adapter incompatible: missing {name}")
        self.module = module
        self.bank = module.load_bank(bank_file)
        self.models = {m["id"] for m in self.bank["models"]}
        if not self.models:
            raise ValueError("Upstream reference library is empty")
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()[:12]
        self.provenance = {
            "upstream_commit": revision or "unversioned",
            "scorer_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "bank_sha256": hashlib.sha256(bank_file.read_bytes()).hexdigest(),
            "policy": POLICY,
            "probe_profile": "upstream-python-challenges/codex-fresh-v1",
        }
        self.version = method_version(self.provenance)

    def plan(self):
        plan = self.module.generate_challenges(3)
        if len(plan) != 3 or any(not isinstance(p.get("prompt"), str) or not p.get("expected_count") for p in plan):
            raise ValueError("Upstream challenge schema changed")
        return plan

    def assess(self, outputs, expected):
        if not outputs:
            return {"identity": "unknown", "candidates": [], "valid_samples": 0, "diagnostics": []}
        try:
            result = self.module.analyze_global_outputs(outputs, self.bank)
        except ValueError:
            # Upstream found no usable answer; record that each one was rejected.
            return {"identity": "inconclusive", "candidates": [], "valid_samples": 0,
                    "diagnostics": [{"index": i, "accepted": False} for i in range(len(outputs))]}
        candidates = [{"model": r["model"], "weight": float(r["probability"])} for r in result["results"]]
        if not candidates or any(not math.isfinite(c["weight"]) for c in candidates):
            raise ValueError("Upstream result schema or weights changed")
        valid = result["used_outputs"]
        top = candidates[0]
        expected_weight = next((r["weight"] for r in candidates if r["model"] == expected), None)
        verdict = "inconclusive"
        if expected not in self.models:
            verdict = "not_in_library"
        elif valid >= 3:
            if top["model"] == expected and top["weight"] >= .5:
                verdict = "consistent"
            elif top["model"] != expected and top["weight"] >= .8 and expected_weight <= .15 and top["weight"] - expected_weight >= .65:
                verdict = "mismatch_signal"
        return {"identity": verdict, "candidates": candidates, "valid_samples": valid,
                "expected_weight": expected_weight,
                "diagnostics": [{k: d[k] for k in ("index", "parsed_numbers", "minimum_numbers", "accepted") if k in d} for d in result["diagnostics"]]}
