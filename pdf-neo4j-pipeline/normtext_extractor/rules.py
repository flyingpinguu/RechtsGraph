"""Optional external rule loading for extractor quality checks."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class RuleSet:
    source_text_anomaly_patterns: List[Dict[str, Any]] = field(default_factory=list)


def load_rules(rules_dir: Optional[str]) -> RuleSet:
    if not rules_dir:
        return RuleSet()
    if not os.path.isdir(rules_dir):
        raise FileNotFoundError("Rules directory not found: {}".format(rules_dir))

    patterns: List[Dict[str, Any]] = []
    for filename in sorted(os.listdir(rules_dir)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(rules_dir, filename)
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        patterns.extend(payload.get("source_text_anomaly_patterns") or [])
    return RuleSet(source_text_anomaly_patterns=patterns)
