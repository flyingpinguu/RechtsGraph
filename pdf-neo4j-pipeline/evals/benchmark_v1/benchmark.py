#!/usr/bin/env python3
"""Validate, blind, score, verify, and price the compact RechtsGraph benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
EVALS_DIR = HERE.parent
PROJECT_ROOT = EVALS_DIR.parent
FIXTURE_PATH = HERE / "fixture.json"
RUBRIC_PATH = HERE / "rubric.json"
PRICING_PATH = HERE / "pricing.json"
GOLD_SOURCES_PATH = HERE / "gold_sources.json"
DEFAULT_REFERENCE_RESULTS = EVALS_DIR / "results" / "recent-pilot-2026-09-11"

AUTOMATIC_CHECK_TYPES = {
    "contains_all",
    "contains_any",
    "contains_normalized",
    "contains_all_groups",
    "regex",
    "word_count",
    "exact_result_set",
    "tool_call_count",
}
TOOL_ITEM_TYPES = {
    "mcpToolCall",
    "dynamicToolCall",
    "commandExecution",
    "fileChange",
    "webSearch",
    "collabToolCall",
    "imageView",
    "browserToolCall",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _definition_path(results: Path, filename: str) -> Path:
    frozen = results / "benchmark-definition" / filename
    return frozen if frozen.is_file() else HERE / filename


def validate_definition(
    fixture: dict[str, Any], rubric: dict[str, Any]
) -> dict[str, Any]:
    errors: list[str] = []
    cases = fixture.get("cases")
    tasks = rubric.get("tasks")
    if not isinstance(cases, list) or not cases:
        errors.append("fixture.cases must be a non-empty array")
        cases = []
    if not isinstance(tasks, list) or not tasks:
        errors.append("rubric.tasks must be a non-empty array")
        tasks = []

    case_ids = [entry.get("id") for entry in cases if isinstance(entry, dict)]
    task_ids = [entry.get("id") for entry in tasks if isinstance(entry, dict)]
    if len(case_ids) != len(set(case_ids)):
        errors.append("fixture contains duplicate case IDs")
    if len(task_ids) != len(set(task_ids)):
        errors.append("rubric contains duplicate task IDs")
    if set(case_ids) != set(task_ids):
        errors.append(
            "fixture/rubric task mismatch: "
            f"only_fixture={sorted(set(case_ids) - set(task_ids))}, "
            f"only_rubric={sorted(set(task_ids) - set(case_ids))}"
        )

    panels: defaultdict[str, int] = defaultdict(int)
    maximums: dict[str, float] = {}
    for case in cases:
        if not isinstance(case, dict):
            errors.append("fixture case is not an object")
            continue
        identifier = case.get("id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", str(identifier or "")):
            errors.append(f"invalid case ID: {identifier!r}")
        if not str(case.get("prompt") or "").strip():
            errors.append(f"{identifier}: prompt is empty")
        if case.get("referenceStatus") != "reviewed":
            errors.append(f"{identifier}: benchmark v1 requires reviewed fixtures")
        panels[str(case.get("panel"))] += 1

    for task in tasks:
        if not isinstance(task, dict):
            errors.append("rubric task is not an object")
            continue
        identifier = str(task.get("id") or "")
        scoring = task.get("scoring")
        maximum = 0.0
        if scoring == "manual_rubric":
            criteria = task.get("criteria")
            if not isinstance(criteria, list) or not criteria:
                errors.append(f"{identifier}: manual rubric needs criteria")
                continue
            for criterion in criteria:
                weight = criterion.get("weight")
                if not isinstance(weight, (int, float)) or weight <= 0:
                    errors.append(f"{identifier}: invalid manual criterion weight")
                    continue
                maximum += 3 * float(weight)
                if not criterion.get("description") or not criterion.get("gold"):
                    errors.append(f"{identifier}: manual criterion lacks description/gold")
        elif scoring == "automatic":
            checks = task.get("checks")
            if not isinstance(checks, list) or not checks:
                errors.append(f"{identifier}: automatic rubric needs checks")
                continue
            for check in checks:
                weight = check.get("weight")
                if not isinstance(weight, (int, float)) or weight <= 0:
                    errors.append(f"{identifier}: invalid automatic check weight")
                    continue
                maximum += float(weight)
                if check.get("type") not in AUTOMATIC_CHECK_TYPES:
                    errors.append(
                        f"{identifier}: unsupported check type {check.get('type')!r}"
                    )
        else:
            errors.append(f"{identifier}: invalid scoring type {scoring!r}")
        maximums[identifier] = maximum

    return {
        "ok": not errors,
        "errors": errors,
        "caseCount": len(cases),
        "panels": dict(sorted(panels.items())),
        "maximumPoints": maximums,
        "fixtureSha256": _json_digest(fixture),
        "rubricSha256": _json_digest(rubric),
    }


def _event_item(event: dict[str, Any]) -> dict[str, Any] | None:
    return (
        event.get("message", {}).get("params", {}).get("item")
        or event.get("params", {}).get("item")
    )


def _tool_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in run.get("events") or []:
        item = _event_item(event)
        if item and item.get("type") in TOOL_ITEM_TYPES and item.get("id"):
            by_id[item["id"]] = {**by_id.get(item["id"], {}), **item}
    for item in run.get("toolItems") or []:
        if item.get("type") in TOOL_ITEM_TYPES and item.get("id"):
            by_id[item["id"]] = {**by_id.get(item["id"], {}), **item}
    return list(by_id.values())


def _load_run(results: Path, case_id: str, side: str) -> dict[str, Any]:
    path = results / "raw" / case_id / side / "final-response.json"
    payload = _load_json(path)
    run = payload.get("body")
    if not isinstance(run, dict):
        raise ValueError(f"No run body in {path}")
    if run.get("status") != "completed":
        raise ValueError(f"Run is not completed: {case_id}:{side}")
    return run


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^0-9a-zäöüß§]+", " ", value)
    return " ".join(value.split())


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[0-9A-Za-zÄÖÜäöüß]+(?:[-'][0-9A-Za-zÄÖÜäöüß]+)*\b", value))


def _normalize_result_item(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().upper()
    value = value.replace("§§", "§").replace(".", "")
    value = re.sub(r"\s+", " ", value)
    return value


def _score_check(
    check: dict[str, Any], answer: str, run: dict[str, Any]
) -> tuple[float, dict[str, Any]]:
    check_type = check["type"]
    answer_folded = answer.casefold()
    fraction = 0.0
    observed: Any = None

    if check_type == "contains_all":
        values = [str(value) for value in check.get("values") or []]
        found = [value for value in values if value.casefold() in answer_folded]
        fraction = len(found) / len(values) if values else 0.0
        observed = {"found": found, "expected": values}
    elif check_type == "contains_any":
        values = [str(value) for value in check.get("values") or []]
        found = [value for value in values if value.casefold() in answer_folded]
        fraction = 1.0 if found else 0.0
        observed = {"found": found, "alternatives": values}
    elif check_type == "contains_normalized":
        expected = _normal(str(check.get("value") or ""))
        fraction = 1.0 if expected and expected in _normal(answer) else 0.0
        observed = {"matched": bool(fraction)}
    elif check_type == "contains_all_groups":
        groups = check.get("groups") or []
        matched = [
            any(str(value).casefold() in answer_folded for value in group)
            for group in groups
        ]
        fraction = sum(matched) / len(matched) if matched else 0.0
        observed = {"matchedGroups": matched}
    elif check_type == "regex":
        pattern = str(check.get("pattern") or "")
        matched = bool(re.search(pattern, answer))
        fraction = 1.0 if matched else 0.0
        observed = {"matched": matched}
    elif check_type == "word_count":
        observed = _word_count(answer)
        fraction = 1.0 if int(check["min"]) <= observed <= int(check["max"]) else 0.0
    elif check_type == "tool_call_count":
        observed = len(_tool_items(run))
        fraction = 1.0 if observed <= int(check["max"]) else 0.0
    elif check_type == "exact_result_set":
        prefix = str(check.get("prefix") or "")
        matches = re.findall(
            rf"(?im)^\s*{re.escape(prefix)}\s*(.*?)\s*$", answer
        )
        if len(matches) == 1:
            actual = {
                _normalize_result_item(item)
                for item in matches[0].split(";")
                if item.strip()
            }
        else:
            actual = set()
        expected = {
            _normalize_result_item(str(item)) for item in check.get("expected") or []
        }
        true_positive = len(actual & expected)
        precision = true_positive / len(actual) if actual else 0.0
        recall = true_positive / len(expected) if expected else 0.0
        fraction = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        observed = {
            "actual": sorted(actual),
            "expected": sorted(expected),
            "precision": precision,
            "recall": recall,
            "lineCount": len(matches),
        }
    else:  # guarded by definition validation
        raise ValueError(f"Unsupported check type: {check_type}")

    points = float(check["weight"]) * fraction
    return points, {
        "id": check["id"],
        "type": check_type,
        "points": round(points, 6),
        "maximum": float(check["weight"]),
        "fraction": round(fraction, 6),
        "observed": observed,
    }


def _automatic_score(
    task: dict[str, Any], answer: str, run: dict[str, Any]
) -> dict[str, Any]:
    checks = [_score_check(check, answer, run)[1] for check in task["checks"]]
    points = sum(item["points"] for item in checks)
    maximum = sum(item["maximum"] for item in checks)
    return {
        "points": round(points, 6),
        "maximum": maximum,
        "percent": round(100 * points / maximum, 3) if maximum else 0.0,
        "checks": checks,
    }


def _blind_order(evaluation_id: str, case_id: str, attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        attempts,
        key=lambda attempt: hashlib.sha256(
            f"{evaluation_id}\0{case_id}\0{attempt['side']}\0{attempt['profile']}".encode()
        ).hexdigest(),
    )


def prepare_review(results: Path) -> dict[str, Any]:
    fixture = _load_json(_definition_path(results, "fixture.json"))
    rubric = _load_json(_definition_path(results, "rubric.json"))
    validation = validate_definition(fixture, rubric)
    if not validation["ok"]:
        raise ValueError("Invalid benchmark definition: " + "; ".join(validation["errors"]))
    manifest = _load_json(results / "manifest.json")
    if manifest.get("fixtureSha256") != validation["fixtureSha256"]:
        raise ValueError("Result fixture hash does not match benchmark_v1/fixture.json")

    attempts_by_case: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in (manifest.get("attempts") or {}).values():
        if attempt.get("final", {}).get("status") == "completed":
            attempts_by_case[str(attempt["caseId"])].append(attempt)

    expected_cases = {case["id"] for case in fixture["cases"]}
    if set(attempts_by_case) != expected_cases:
        raise ValueError(
            "Completed result cases differ from fixture: "
            f"observed={sorted(attempts_by_case)}, expected={sorted(expected_cases)}"
        )
    if any(len(attempts_by_case[case_id]) != 2 for case_id in expected_cases):
        raise ValueError("Compact paired review requires exactly two completed answers per task")

    tasks = {task["id"]: task for task in rubric["tasks"]}
    blind_root = results / "review" / "blind"
    answers_root = blind_root / "answers"
    key_entries: list[dict[str, Any]] = []
    score_entries: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        case_id = case["id"]
        ordered = _blind_order(
            str(manifest.get("evaluationId") or ""),
            case_id,
            attempts_by_case[case_id],
        )
        for index, attempt in enumerate(ordered):
            label = chr(ord("A") + index)
            run = _load_run(results, case_id, attempt["side"])
            answer = str(run.get("finalText") or "").strip()
            if not answer:
                raise ValueError(f"Empty final answer: {case_id}:{attempt['side']}")
            answer_path = answers_root / case_id / f"{label}.md"
            answer_path.parent.mkdir(parents=True, exist_ok=True)
            answer_path.write_text(answer + "\n", encoding="utf-8")
            key_entries.append(
                {
                    "caseId": case_id,
                    "label": label,
                    "side": attempt["side"],
                    "profile": attempt["profile"],
                    "remoteRunId": attempt.get("remoteRunId"),
                }
            )
            task = tasks[case_id]
            if task["scoring"] == "manual_rubric":
                score_entries.append(
                    {
                        "caseId": case_id,
                        "label": label,
                        "scoring": "manual_rubric",
                        "criteria": [
                            {
                                "id": criterion["id"],
                                "description": criterion["description"],
                                "gold": criterion["gold"],
                                "weight": criterion["weight"],
                                "score": None,
                                "note": "",
                            }
                            for criterion in task["criteria"]
                        ],
                        "majorErrorRules": task.get("majorErrorRules") or [],
                        "majorErrors": [],
                        "overallNote": "",
                    }
                )
            else:
                score_entries.append(
                    {
                        "caseId": case_id,
                        "label": label,
                        "scoring": "automatic",
                        "automatic": _automatic_score(task, answer, run),
                        "majorErrors": [],
                        "overallNote": "Automatically recomputed during scoring.",
                    }
                )

    key = {
        "schemaVersion": 1,
        "evaluationId": manifest.get("evaluationId"),
        "warning": "Do not give this file to the blinded reviewer.",
        "answers": key_entries,
    }
    scorecard = {
        "schemaVersion": 1,
        "benchmarkId": rubric["benchmarkId"],
        "evaluationId": manifest.get("evaluationId"),
        "fixtureSha256": validation["fixtureSha256"],
        "rubricSha256": validation["rubricSha256"],
        "reviewer": "",
        "reviewedAt": "",
        "manualScale": rubric["manualScale"],
        "instructions": "Fill score 0-3 and a short note for every manual criterion. Do not inspect answer_key.json or tool traces first.",
        "answers": score_entries,
    }
    _write_json(results / "review" / "answer_key.json", key)
    _write_json(blind_root / "scorecard.json", scorecard)
    return {
        "ok": True,
        "blindDirectory": str(blind_root),
        "scorecard": str(blind_root / "scorecard.json"),
        "answerKey": str(results / "review" / "answer_key.json"),
        "answerCount": len(score_entries),
    }


def _manual_score(
    task: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    expected = {criterion["id"]: criterion for criterion in task["criteria"]}
    supplied = {criterion.get("id"): criterion for criterion in entry.get("criteria") or []}
    if set(expected) != set(supplied):
        raise ValueError(
            f"Manual criteria mismatch for {entry.get('caseId')}:{entry.get('label')}"
        )
    details = []
    points = 0.0
    maximum = 0.0
    for identifier, criterion in expected.items():
        score = supplied[identifier].get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or score not in {0, 1, 2, 3}:
            raise ValueError(
                f"Missing/invalid score for {entry.get('caseId')}:{entry.get('label')}:{identifier}"
            )
        weight = float(criterion["weight"])
        points += float(score) * weight
        maximum += 3 * weight
        details.append(
            {
                "id": identifier,
                "score": score,
                "weight": weight,
                "points": float(score) * weight,
                "maximum": 3 * weight,
                "note": str(supplied[identifier].get("note") or ""),
            }
        )
    return {
        "points": points,
        "maximum": maximum,
        "percent": round(100 * points / maximum, 3) if maximum else 0.0,
        "criteria": details,
    }


def _attempt_metrics(manifest: dict[str, Any], case_id: str, side: str) -> dict[str, Any]:
    attempt = (manifest.get("attempts") or {}).get(f"{case_id}:{side}")
    if not attempt:
        for candidate in (manifest.get("attempts") or {}).values():
            if candidate.get("caseId") == case_id and candidate.get("side") == side:
                attempt = candidate
                break
    return dict(attempt.get("final", {}).get("metrics") or {}) if attempt else {}


def score_review(results: Path, scorecard_path: Path | None = None) -> dict[str, Any]:
    fixture = _load_json(_definition_path(results, "fixture.json"))
    rubric = _load_json(_definition_path(results, "rubric.json"))
    validation = validate_definition(fixture, rubric)
    if not validation["ok"]:
        raise ValueError("Invalid benchmark definition: " + "; ".join(validation["errors"]))
    manifest = _load_json(results / "manifest.json")
    key = _load_json(results / "review" / "answer_key.json")
    scorecard_path = scorecard_path or results / "review" / "blind" / "scorecard.json"
    scorecard = _load_json(scorecard_path)
    if scorecard.get("evaluationId") != manifest.get("evaluationId"):
        raise ValueError("Scorecard belongs to another evaluation")
    if scorecard.get("fixtureSha256") != validation["fixtureSha256"]:
        raise ValueError("Scorecard fixture hash mismatch")
    if scorecard.get("rubricSha256") != validation["rubricSha256"]:
        raise ValueError("Scorecard rubric hash mismatch")
    if not str(scorecard.get("reviewer") or "").strip():
        raise ValueError("Scorecard reviewer is required")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(scorecard.get("reviewedAt") or "")):
        raise ValueError("Scorecard reviewedAt must use YYYY-MM-DD")

    tasks = {task["id"]: task for task in rubric["tasks"]}
    cases = {case["id"]: case for case in fixture["cases"]}
    mappings = {
        (entry["caseId"], entry["label"]): entry for entry in key["answers"]
    }
    score_entries = {
        (entry["caseId"], entry["label"]): entry
        for entry in scorecard.get("answers") or []
    }
    if set(mappings) != set(score_entries):
        raise ValueError("Scorecard answer set differs from answer key")

    rows: list[dict[str, Any]] = []
    for identity, mapping in mappings.items():
        case_id, label = identity
        entry = score_entries[identity]
        task = tasks[case_id]
        run = _load_run(results, case_id, mapping["side"])
        answer = str(run.get("finalText") or "")
        result = (
            _manual_score(task, entry)
            if task["scoring"] == "manual_rubric"
            else _automatic_score(task, answer, run)
        )
        major_errors = entry.get("majorErrors") or []
        if not isinstance(major_errors, list) or not all(
            isinstance(value, str) for value in major_errors
        ):
            raise ValueError(f"majorErrors must be a string array: {case_id}:{label}")
        metrics = _attempt_metrics(manifest, case_id, mapping["side"])
        rows.append(
            {
                "caseId": case_id,
                "label": label,
                "profile": mapping["profile"],
                "side": mapping["side"],
                "panel": cases[case_id]["panel"],
                "scoring": task["scoring"],
                "score": result,
                "majorErrors": major_errors,
                "metrics": metrics,
            }
        )

    panel_values: defaultdict[str, defaultdict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    profile_errors: defaultdict[str, int] = defaultdict(int)
    profile_metrics: defaultdict[str, defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for row in rows:
        profile = row["profile"]
        panel_values[profile][row["panel"]].append(row["score"]["percent"])
        profile_errors[profile] += len(row["majorErrors"])
        for field in (
            "totalTokens",
            "inputTokens",
            "cachedInputTokens",
            "uncachedInputTokens",
            "outputTokens",
            "reasoningOutputTokens",
            "toolCallCount",
            "webToolCallCount",
            "rechtsgraphToolCallCount",
            "rechtsgraphGraphCallCount",
            "rechtsgraphTextCallCount",
        ):
            value = row["metrics"].get(field)
            if isinstance(value, (int, float)):
                profile_metrics[profile][field] += value

    profiles: dict[str, Any] = {}
    for profile in sorted(panel_values):
        panel_means = {
            panel: round(sum(values) / len(values), 3)
            for panel, values in sorted(panel_values[profile].items())
        }
        profiles[profile] = {
            "panels": panel_means,
            "majorErrorCount": profile_errors[profile],
            "metrics": dict(sorted(profile_metrics[profile].items())),
        }

    case_profiles: defaultdict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        case_profiles[row["caseId"]][row["profile"]] = row["score"]["percent"]
    pairwise = []
    profile_order = [manifest.get("left", {}).get("profile"), manifest.get("right", {}).get("profile")]
    for case_id, values in sorted(case_profiles.items()):
        if len(values) == 2:
            ordered = [profile for profile in profile_order if profile in values]
            if len(ordered) != 2:
                ordered = sorted(values)
            pairwise.append(
                {
                    "caseId": case_id,
                    "leftProfile": ordered[0],
                    "rightProfile": ordered[1],
                    "deltaLeftMinusRight": round(values[ordered[0]] - values[ordered[1]], 3),
                }
            )

    report = {
        "schemaVersion": 1,
        "benchmarkId": rubric["benchmarkId"],
        "evaluationId": manifest.get("evaluationId"),
        "fixtureSha256": validation["fixtureSha256"],
        "rubricSha256": validation["rubricSha256"],
        "reviewer": scorecard["reviewer"],
        "reviewedAt": scorecard["reviewedAt"],
        "profiles": profiles,
        "pairwiseByTask": pairwise,
        "results": sorted(rows, key=lambda row: (row["caseId"], row["profile"])),
        "interpretationLimits": [
            "Known legal development cases are not held-out evidence.",
            "Automatic retrieval checks measure answer fidelity, not broader legal quality.",
            "Tool and token metrics explain outcomes but do not contribute quality points, except the explicit no-tool control.",
        ],
    }
    _write_json(results / "benchmark-report.json", report)
    (results / "benchmark-report.md").write_text(
        _render_markdown_report(report), encoding="utf-8"
    )
    return report


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# RechtsGraph benchmark result",
        "",
        f"Evaluation: `{report['evaluationId']}`",
        "",
        "## Profile summary",
        "",
        "| Profile | End-to-end | Retrieval | No-harm | Major errors | Tokens | Tool calls |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile, value in report["profiles"].items():
        panels = value["panels"]
        metrics = value["metrics"]
        lines.append(
            "| {profile} | {end} | {retrieval} | {control} | {errors} | {tokens} | {tools} |".format(
                profile=profile,
                end=f"{panels['end_to_end']:.1f}%" if "end_to_end" in panels else "—",
                retrieval=f"{panels['retrieval']:.1f}%" if "retrieval" in panels else "—",
                control=f"{panels['no_harm']:.1f}%" if "no_harm" in panels else "—",
                errors=value["majorErrorCount"],
                tokens=int(metrics.get("totalTokens", 0)),
                tools=int(metrics.get("toolCallCount", 0)),
            )
        )
    lines.extend(
        [
            "",
            "## Task results",
            "",
            "| Task | Profile | Panel | Score | Major errors |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for row in report["results"]:
        lines.append(
            f"| {row['caseId']} | {row['profile']} | {row['panel']} | "
            f"{row['score']['percent']:.1f}% | {len(row['majorErrors'])} |"
        )
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {item}" for item in report["interpretationLimits"])
    return "\n".join(lines) + "\n"


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def verify_gold() -> dict[str, Any]:
    sys.path.insert(0, str(PROJECT_ROOT))
    from rechtsgraph_mcp.index import RetrievalIndex
    from rechtsgraph_mcp.neo4j_store import Neo4jSettings, Neo4jStore

    index_path = PROJECT_ROOT / "output" / "retrieval" / "rechtsgraph.sqlite"
    with RetrievalIndex(index_path) as index:
        payload = index.get_structural_unit(
            "BGB", "§ 130 Abs. 1", include_descendants=True, max_passages=10
        )
        text = "\n".join(
            passage.get("text", "") for passage in payload.get("passages") or []
        )
        required = [
            "Eine Willenserklärung, die einem anderen gegenüber abzugeben ist",
            "Sie wird nicht wirksam, wenn dem anderen vorher oder gleichzeitig ein Widerruf zugeht",
        ]
        bgb_ok = all(_normal(value) in _normal(text) for value in required)
        index_status = index.status()

    env = {**_dotenv(PROJECT_ROOT / ".env.local"), **os.environ}
    settings = Neo4jSettings(
        url=env.get("RECHTSGRAPH_NEO4J_URL", "http://localhost:7474"),
        database=env.get("RECHTSGRAPH_NEO4J_DATABASE", "gii-xml-chunked"),
        username=env.get("RECHTSGRAPH_NEO4J_USERNAME") or env.get("NEO4J_USER"),
        password=env.get("RECHTSGRAPH_NEO4J_PASSWORD") or env.get("NEO4J_PASSWORD"),
        timeout_seconds=15,
    )
    store = Neo4jStore(settings)
    incoming = store.explore_references(
        "buergerliches_gesetzbuch_para_399", direction="incoming", limit=50
    )
    incoming_actual = {
        row.get("neighbor_node_global_key") for row in incoming["references"]
    }
    incoming_expected = {
        "buergerliches_gesetzbuch_para_412",
        "handelsgesetzbuch_para_354a",
        "zivilprozessordnung_para_851",
    }
    ai = store.explore_references(
        "celex_32024r1689_art_2", direction="outgoing", limit=50
    )
    ai_actual = {
        row.get("neighbor_node_global_key")
        for row in ai["references"]
        if "Document" in (row.get("neighbor_node_labels") or [])
        and row.get("neighbor_node_global_key") != "celex_32024r1689"
    }
    ai_expected = {"celex_32002l0058", "celex_32016r0679", "celex_32022r2065"}
    source_checks = []
    for source in _load_json(GOLD_SOURCES_PATH).get("sources") or []:
        local_path = PROJECT_ROOT / source["localPath"]
        actual_sha256 = _file_digest(local_path) if local_path.is_file() else None
        source_checks.append(
            {
                "taskId": source["taskId"],
                "ok": actual_sha256 == source["sha256"],
                "localPath": str(local_path),
                "expectedSha256": source["sha256"],
                "actualSha256": actual_sha256,
            }
        )
    checks = {
        "bgb130Text": {"ok": bgb_ok},
        "incomingBgb399": {
            "ok": incoming_actual == incoming_expected,
            "actual": sorted(value for value in incoming_actual if value),
            "expected": sorted(incoming_expected),
        },
        "aiActArt2ExternalDocuments": {
            "ok": ai_actual == ai_expected,
            "actual": sorted(value for value in ai_actual if value),
            "expected": sorted(ai_expected),
        },
        "legalGoldSources": {
            "ok": bool(source_checks) and all(item["ok"] for item in source_checks),
            "sources": source_checks,
        },
    }
    return {
        "ok": all(check["ok"] for check in checks.values()),
        "checks": checks,
        "retrievalIndex": {
            "sha256": _file_digest(index_path),
            "schemaVersion": index_status.get("schema_version"),
            "documents": index_status.get("documents"),
            "passages": index_status.get("passages"),
        },
        "neo4j": store.status(),
    }


def _observed_reference_usage(results: Path) -> dict[str, float]:
    summary = _load_json(results / "summary.json")
    selected = [
        row
        for row in summary.get("attempts") or []
        if row.get("profile") in {"lean", "web_only"}
    ]
    totals: defaultdict[str, float] = defaultdict(float)
    for row in selected:
        metrics = row.get("metrics") or {}
        for field in (
            "inputTokens",
            "cachedInputTokens",
            "uncachedInputTokens",
            "cacheWriteInputTokens",
            "outputTokens",
        ):
            value = metrics.get(field)
            if isinstance(value, (int, float)):
                totals[field] += value
        try:
            run = _load_run(results, row["caseId"], "left" if row.get("side") == "left" else "right")
        except (FileNotFoundError, ValueError):
            raw = results / "raw" / row["caseId"] / row["profile"] / "final-response.json"
            payload = _load_json(raw)
            run = payload.get("body") or {}
        totals["webSearchCalls"] += sum(
            1 for item in _tool_items(run) if item.get("type") == "webSearch"
        )
    totals["runs"] = len(selected)
    return dict(totals)


def _inference_cost(usage: dict[str, float], price: dict[str, float]) -> float:
    input_tokens = usage.get("inputTokens", 0)
    cached = usage.get("cachedInputTokens", 0)
    uncached = usage.get("uncachedInputTokens")
    if uncached is None:
        uncached = max(0.0, input_tokens - cached)
    cache_write = usage.get("cacheWriteInputTokens", 0)
    return (
        uncached * price["inputPerMillion"]
        + cached * price["cachedInputPerMillion"]
        + cache_write * price["cacheWritePerMillion"]
        + usage.get("outputTokens", 0) * price["outputPerMillion"]
    ) / 1_000_000


def estimate_cost(reference_results: Path) -> dict[str, Any]:
    pricing = _load_json(PRICING_PATH)
    usage = _observed_reference_usage(reference_results)
    reference_runs = int(usage["runs"])
    if reference_runs <= 0:
        raise ValueError("Reference results contain no lean/web_only runs")
    extra_runs = 8  # three retrieval tasks plus one control, two profiles each
    scenarios = {
        "lower": {"tokenRatio": 0.25, "webCallsPerExtraRun": 0.5},
        "likely": {"tokenRatio": 0.5, "webCallsPerExtraRun": 1.0},
        "upper": {"tokenRatio": 1.0, "webCallsPerExtraRun": 2.0},
    }
    models: dict[str, Any] = {}
    for model, price in pricing["models"].items():
        observed_inference = _inference_cost(usage, price)
        observed_web = usage.get("webSearchCalls", 0) * price["webSearchPerCall"]
        estimates = {}
        for name, scenario in scenarios.items():
            extra_factor = extra_runs / reference_runs * scenario["tokenRatio"]
            extra_inference = observed_inference * extra_factor
            extra_web = (
                extra_runs
                * scenario["webCallsPerExtraRun"]
                * price["webSearchPerCall"]
            )
            estimates[name] = round(
                observed_inference + observed_web + extra_inference + extra_web,
                4,
            )
        models[model] = {
            "referenceLegalRuns": reference_runs,
            "referenceInferenceUSD": round(observed_inference, 4),
            "referenceWebUSD": round(observed_web, 4),
            "estimatedFullCoreSuiteUSD": estimates,
            "source": price["source"],
        }
    return {
        "currency": pricing["currency"],
        "pricingAsOf": pricing["asOf"],
        "referenceResults": str(reference_results),
        "referenceUsage": usage,
        "assumptions": {
            "coreRuns": 14,
            "legalRunsTakenFromReference": reference_runs,
            "additionalRuns": extra_runs,
            "scenarios": scenarios,
            "warning": "Indicative only. Codex-to-OpenRouter web tool compatibility and actual routed prices must be verified by the integration gate.",
        },
        "models": models,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("verify-gold")
    prepare = subparsers.add_parser("prepare-review")
    prepare.add_argument("results", type=Path)
    score = subparsers.add_parser("score")
    score.add_argument("results", type=Path)
    score.add_argument("--scorecard", type=Path)
    cost = subparsers.add_parser("estimate-cost")
    cost.add_argument("--reference-results", type=Path, default=DEFAULT_REFERENCE_RESULTS)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_definition(
                _load_json(FIXTURE_PATH), _load_json(RUBRIC_PATH)
            )
        elif args.command == "verify-gold":
            result = verify_gold()
        elif args.command == "prepare-review":
            result = prepare_review(args.results.resolve())
        elif args.command == "score":
            result = score_review(
                args.results.resolve(),
                args.scorecard.resolve() if args.scorecard else None,
            )
        elif args.command == "estimate-cost":
            result = estimate_cost(args.reference_results.resolve())
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
