#!/usr/bin/env python3
"""Validate, blind, and aggregate the RechtsGraph legal-competence benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = BASE_DIR / "fixture.json"
RUBRIC_PATH = BASE_DIR / "rubric.json"
JUDGE_PROMPT_PATH = BASE_DIR / "judge_prompt.md"
SOURCE_PASSAGES_PATH = BASE_DIR / "source_passages.json"

EXPECTED_FAMILIES = {
    "case_analysis": 4,
    "research_advice": 2,
    "document_review": 3,
    "drafting_communication": 2,
    "triage_judgment": 1,
}
EXPECTED_PROFILES = {"lean", "web_only"}


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digests(directory: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    return {
        path.relative_to(directory).as_posix(): _file_digest(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.relative_to(directory).as_posix() not in excluded
    }


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


def _validate_definition(fixture: dict[str, Any], rubric: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if fixture.get("benchmarkId") != rubric.get("benchmarkId"):
        errors.append("fixture and rubric benchmarkId differ")
    if fixture.get("frozenAsOf") != rubric.get("frozenAsOf"):
        errors.append("fixture and rubric frozenAsOf differ")

    cases = fixture.get("cases")
    tasks = rubric.get("tasks")
    if not isinstance(cases, list) or len(cases) != 12:
        errors.append("fixture must contain exactly 12 cases")
        cases = cases if isinstance(cases, list) else []
    if not isinstance(tasks, list) or len(tasks) != 12:
        errors.append("rubric must contain exactly 12 tasks")
        tasks = tasks if isinstance(tasks, list) else []

    case_ids = [entry.get("id") for entry in cases]
    task_ids = [entry.get("id") for entry in tasks]
    if len(set(case_ids)) != len(case_ids) or None in case_ids:
        errors.append("fixture case IDs must be unique and non-empty")
    if len(set(task_ids)) != len(task_ids) or None in task_ids:
        errors.append("rubric task IDs must be unique and non-empty")
    if case_ids != task_ids:
        errors.append("fixture and rubric task IDs/order differ")

    suites = fixture.get("suites")
    if not isinstance(suites, dict) or not {"smoke", "full", "quick", "stability", "protected"}.issubset(suites):
        errors.append("fixture must define smoke, full, quick, stability, and protected suites")
        suites = {}
    for suite_name, suite_ids in suites.items():
        if not isinstance(suite_ids, list) or not suite_ids:
            errors.append(f"suite {suite_name} must be a non-empty list")
            continue
        if len(set(suite_ids)) != len(suite_ids) or any(case_id not in case_ids for case_id in suite_ids):
            errors.append(f"suite {suite_name} contains duplicates or unknown task IDs")
    if suites.get("full") != case_ids:
        errors.append("full suite must contain every task in fixture order")
    if suites.get("smoke") != suites.get("stability"):
        errors.append("smoke and stability suites must use the same fixed task subset")

    compact = [case for case in cases if case.get("id") in suites.get("smoke", [])]
    if len(compact) != 3 or any(case.get("dialogue") for case in compact):
        errors.append("smoke suite must contain exactly three single-turn tasks")
    if {case.get("family") for case in compact} != {"case_analysis", "document_review", "drafting_communication"}:
        errors.append("smoke must cover case analysis, document review, and drafting")

    family_counts = Counter(entry.get("family") for entry in cases)
    if dict(family_counts) != EXPECTED_FAMILIES:
        errors.append(f"family counts differ: {dict(family_counts)}")

    task_by_id = {entry.get("id"): entry for entry in tasks}
    for case in cases:
        case_id = case.get("id", "<missing>")
        if len(str(case.get("prompt") or "").strip()) < 120:
            errors.append(f"{case_id}: assignment is missing or too short")
        if not isinstance(case.get("maxWords"), int) or case["maxWords"] < 100:
            errors.append(f"{case_id}: maxWords must be an integer of at least 100")
        if not str(case.get("referenceStatus") or "").strip():
            errors.append(f"{case_id}: referenceStatus is missing")
        if case.get("developmentPartition") not in {"known_solution", "development", "protected"}:
            errors.append(f"{case_id}: invalid developmentPartition")
        dialogue = case.get("dialogue")
        if dialogue is not None:
            if dialogue.get("mode") != "deterministic_clarification":
                errors.append(f"{case_id}: unsupported dialogue mode")
            if dialogue.get("maxClarificationRounds") != 2:
                errors.append(f"{case_id}: dialogue must allow exactly two clarification rounds")
            facts = dialogue.get("facts")
            if not isinstance(facts, list) or not facts:
                errors.append(f"{case_id}: dialogue facts are missing")
            elif any(not fact.get("id") or not fact.get("match") or not fact.get("answer") for fact in facts):
                errors.append(f"{case_id}: every dialogue fact needs id, match, and answer")
        task = task_by_id.get(case_id, {})
        if task.get("family") != case.get("family"):
            errors.append(f"{case_id}: family differs between fixture and rubric")
        if len(str(task.get("referenceBrief") or "").strip()) < 200:
            errors.append(f"{case_id}: reference brief is missing or too short")
        sources = task.get("sources")
        if not isinstance(sources, list) or not sources or any(
            not str(source).startswith("https://") for source in sources
        ):
            errors.append(f"{case_id}: sources must contain HTTPS references")
        criteria = task.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            errors.append(f"{case_id}: scoring criteria are missing")
            continue
        criterion_ids = [criterion.get("id") for criterion in criteria]
        if len(set(criterion_ids)) != len(criterion_ids) or None in criterion_ids:
            errors.append(f"{case_id}: criterion IDs must be unique and non-empty")
        weights = [criterion.get("weight") for criterion in criteria]
        if any(not isinstance(weight, int) or weight <= 0 for weight in weights):
            errors.append(f"{case_id}: criterion weights must be positive integers")
        elif sum(weights) != 100:
            errors.append(f"{case_id}: criterion weights sum to {sum(weights)}, not 100")
        fatal_errors = task.get("fatalErrors")
        if not isinstance(fatal_errors, list) or not fatal_errors:
            errors.append(f"{case_id}: fatal-error rules are missing")
        elif len({entry.get("id") for entry in fatal_errors}) != len(fatal_errors):
            errors.append(f"{case_id}: fatal-error IDs must be unique")
        deliverables = task.get("mandatoryDeliverableElements")
        if deliverables is not None and (
            not isinstance(deliverables, list)
            or not deliverables
            or any(not str(item).strip() for item in deliverables)
        ):
            errors.append(f"{case_id}: mandatoryDeliverableElements must be a non-empty string list")

    scale = rubric.get("scoreScale") or {}
    if set(scale) != {"0", "1", "2", "3", "4"}:
        errors.append("scoreScale must define exactly 0 through 4")
    policy = rubric.get("scoringPolicy") or {}
    if policy.get("toolUse") != "Never scored. Tool traces are diagnostic only.":
        errors.append("tool use must explicitly be excluded from scoring")
    if policy.get("taskMaximum") != 100:
        errors.append("taskMaximum must be 100")
    if not JUDGE_PROMPT_PATH.is_file():
        errors.append("judge_prompt.md is missing")
    if not SOURCE_PASSAGES_PATH.is_file():
        errors.append("source_passages.json is missing")
    else:
        passages = _load_json(SOURCE_PASSAGES_PATH)
        passage_entries = passages.get("passages", [])
        if not isinstance(passage_entries, list) or not passage_entries:
            errors.append("source_passages.json must contain passages")
            passage_entries = []
        for entry in passage_entries:
            if (
                not str(entry.get("id") or "").strip()
                or not str(entry.get("sourceUrl") or "").startswith("https://")
                or not str(entry.get("locator") or "").strip()
                or not str(entry.get("passage") or "").strip()
            ):
                errors.append("every source passage needs id, HTTPS URL, locator, and text")
        covered = {task_id for entry in passage_entries for task_id in entry.get("taskIds", [])}
        missing_passages = [case_id for case_id in case_ids if case_id not in covered]
        if missing_passages:
            errors.append(f"source_passages.json does not cover: {', '.join(missing_passages)}")

    if errors:
        raise ValueError("Invalid benchmark definition:\n- " + "\n- ".join(errors))
    return {
        "ok": True,
        "benchmarkId": fixture["benchmarkId"],
        "frozenAsOf": fixture["frozenAsOf"],
        "taskCount": len(cases),
        "plannedCandidateRuns": len(cases) * 2,
        "families": dict(family_counts),
        "fixtureSha256": _file_digest(FIXTURE_PATH),
        "rubricSha256": _file_digest(RUBRIC_PATH),
        "judgePromptSha256": _file_digest(JUDGE_PROMPT_PATH),
        "sourcePassagesSha256": _file_digest(SOURCE_PASSAGES_PATH),
        "suites": {name: len(ids) for name, ids in suites.items()},
    }


def validate() -> dict[str, Any]:
    return _validate_definition(_load_json(FIXTURE_PATH), _load_json(RUBRIC_PATH))


def _definition_paths(results: Path) -> tuple[Path, Path]:
    frozen = results / "benchmark-definition"
    fixture_path = frozen / "fixture.json" if (frozen / "fixture.json").is_file() else FIXTURE_PATH
    rubric_path = frozen / "rubric.json" if (frozen / "rubric.json").is_file() else RUBRIC_PATH
    metadata_path = results / "benchmark-definition.json"
    if metadata_path.is_file() and frozen.is_dir():
        metadata = _load_json(metadata_path)
        for filename, expected in metadata.get("files", {}).items():
            path = frozen / filename
            if not path.is_file() or _file_digest(path) != expected:
                raise ValueError(f"Frozen benchmark definition failed hash check: {filename}")
    return fixture_path, rubric_path


def _definition_identity(results: Path) -> dict[str, Any]:
    fixture_path, rubric_path = _definition_paths(results)
    frozen = results / "benchmark-definition"
    prompt_path = frozen / "judge_prompt.md" if (frozen / "judge_prompt.md").is_file() else JUDGE_PROMPT_PATH
    metadata_path = results / "benchmark-definition.json"
    metadata = _load_json(metadata_path) if metadata_path.is_file() else {}
    return {
        "benchmarkVersion": metadata.get("benchmarkVersion") or _load_json(fixture_path).get("benchmarkId"),
        "fixtureSha256": _file_digest(fixture_path),
        "rubricSha256": _file_digest(rubric_path),
        "judgePromptSha256": _file_digest(prompt_path),
    }


def verify_snapshot(snapshot_directory: str | Path) -> dict[str, Any]:
    snapshot = Path(snapshot_directory).resolve()
    metadata_path = snapshot / "snapshot.json"
    if not metadata_path.is_file():
        raise ValueError(f"Snapshot metadata is missing: {metadata_path}")
    metadata = _load_json(metadata_path)
    expected = metadata.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("Snapshot file inventory is missing or empty")
    observed = _tree_digests(snapshot, exclude={"snapshot.json"})
    def allowed_derivative(path: str) -> bool:
        return path.startswith("judge/") or path in {
            "legal-competence-report.json", "legal-competence-report.md",
        }
    missing = sorted(set(expected) - set(observed))
    added = sorted(path for path in set(observed) - set(expected) if not allowed_derivative(path))
    changed = sorted(
        path for path in set(expected) & set(observed) if expected[path] != observed[path]
    )
    if missing or added or changed:
        raise ValueError(
            "Snapshot integrity check failed: "
            f"missing={missing}, added={added}, changed={changed}"
        )
    return {
        "ok": True,
        "snapshot": str(snapshot),
        "fileCount": len(expected),
        "selectedCaseIds": metadata.get("selectedCaseIds"),
        "sourceEvaluationId": metadata.get("sourceEvaluationId"),
    }


def create_snapshot(
    source_directory: str | Path,
    output_directory: str | Path,
    *,
    task_count: int = 5,
) -> dict[str, Any]:
    """Copy the first N complete pairs into a checksum-locked result snapshot.

    Source files are only read. The target must not exist, which prevents a
    rerun from silently changing an already named evaluation snapshot.
    """
    source = Path(source_directory).resolve()
    output = Path(output_directory).resolve()
    if output.exists():
        raise ValueError(f"Snapshot target already exists: {output}")
    if task_count < 1:
        raise ValueError("task_count must be positive")

    manifest = _load_json(source / "manifest.json")
    selected = manifest.get("selectedCaseIds")
    if not isinstance(selected, list) or len(selected) < task_count:
        raise ValueError("Source manifest does not contain enough selected tasks")
    task_ids = selected[:task_count]
    attempts = manifest.get("attempts") or {}
    retained: dict[str, Any] = {}
    retained_sides: list[tuple[str, str]] = []
    for task_id in task_ids:
        completed = [
            (key, attempt) for key, attempt in attempts.items()
            if attempt.get("caseId") == task_id
            and (attempt.get("final") or {}).get("status") == "completed"
        ]
        profiles = [attempt.get("profile") for _, attempt in completed]
        if len(completed) != 2 or set(profiles) != EXPECTED_PROFILES:
            raise ValueError(
                f"{task_id}: expected one completed lean and web_only answer, found {profiles}"
            )
        for key, attempt in completed:
            side = attempt.get("side")
            if not isinstance(side, str):
                raise ValueError(f"{task_id}: completed attempt has no side")
            # Validate that the exact judge-facing answer exists before copying.
            fixture_path, _ = _definition_paths(source)
            fixture_case = next(
                (case for case in _load_json(fixture_path)["cases"] if case["id"] == task_id),
                None,
            )
            if fixture_case is None:
                raise ValueError(f"{task_id}: task is absent from frozen fixture")
            _answer_text(source, fixture_case, side)
            retained[key] = attempt
            retained_sides.append((task_id, side))

    output.mkdir(parents=True)
    try:
        for name in ("benchmark-definition", "runner-config"):
            source_path = source / name
            if source_path.is_dir():
                shutil.copytree(source_path, output / name)
        for name in ("benchmark-definition.json", "benchmark-validation.json", "environment-snapshot.json"):
            source_path = source / name
            if source_path.is_file():
                shutil.copy2(source_path, output / name)
        for task_id, side in retained_sides:
            shutil.copytree(source / "raw" / task_id / side, output / "raw" / task_id / side)

        snapshot_manifest = dict(manifest)
        snapshot_manifest["schemaVersion"] = max(int(manifest.get("schemaVersion", 1)), 2)
        snapshot_manifest["evaluationId"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{manifest.get('evaluationId')}:{','.join(task_ids)}")
        )
        snapshot_manifest["selectedCaseIds"] = task_ids
        snapshot_manifest["attempts"] = retained
        snapshot_manifest["plan"] = [
            entry for entry in manifest.get("plan", []) if entry.get("caseId") in task_ids
        ]
        snapshot_manifest["snapshotSource"] = {
            "directory": str(source),
            "evaluationId": manifest.get("evaluationId"),
            "selectionRule": f"first_{task_count}_selected_tasks_with_complete_profile_pairs",
            "ignoredAttemptKeys": sorted(set(attempts) - set(retained)),
        }
        _write_json(output / "manifest.json", snapshot_manifest)

        identity = _definition_identity(output)
        retained_relative_directories = {
            f"raw/{task_id}/{side}" for task_id, side in retained_sides
        }
        source_raw_directories = {
            directory.relative_to(source).as_posix()
            for task_directory in (source / "raw").iterdir()
            if task_directory.is_dir()
            for directory in task_directory.iterdir()
            if directory.is_dir()
        } if (source / "raw").is_dir() else set()
        metadata = {
            "schemaVersion": 1,
            "snapshotType": "paired-candidate-evaluation",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "sourceDirectory": str(source),
            "sourceEvaluationId": manifest.get("evaluationId"),
            "snapshotEvaluationId": snapshot_manifest["evaluationId"],
            "selectedCaseIds": task_ids,
            "profiles": sorted(EXPECTED_PROFILES),
            "excludedSourceRawDirectories": sorted(
                source_raw_directories - retained_relative_directories
            ),
            "definition": identity,
            "immutability": "Verify all copied content against this SHA-256 inventory; create a new snapshot instead of editing this directory.",
            "files": _tree_digests(output),
        }
        _write_json(output / "snapshot.json", metadata)
        verified = verify_snapshot(output)
    except Exception:
        shutil.rmtree(output)
        raise
    return verified


def _answer_text(results: Path, case: dict[str, Any], side: str) -> tuple[str, list[dict[str, Any]] | None]:
    case_id = case["id"]
    transcript_path = results / "raw" / case_id / side / "dialogue-transcript.json"
    if case.get("dialogue"):
        if not transcript_path.is_file():
            raise ValueError(f"Dialogue transcript is missing: {case_id}/{side}")
        transcript = _load_json(transcript_path)
        turns = transcript.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"Dialogue transcript has no turns: {case_id}/{side}")
        rendered = []
        for turn in turns:
            rendered.extend([
                f"NUTZER TURN {turn.get('turnNumber')}:\n{turn.get('user', '')}",
                f"ASSISTENT TURN {turn.get('turnNumber')}:\n{turn.get('assistant', '')}",
            ])
        return "\n\n".join(rendered).strip(), turns
    path = results / "raw" / case_id / side / "final-response.json"
    payload = _load_json(path)
    body = payload.get("body") or {}
    if body.get("status") != "completed":
        raise ValueError(f"Candidate response is not completed: {case_id}/{side}")
    answer = body.get("finalText")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError(f"Candidate response has no finalText: {case_id}/{side}")
    return answer.strip(), None


def prepare_judge(results_directory: str | Path) -> dict[str, Any]:
    results = Path(results_directory).resolve()
    fixture_path, rubric_path = _definition_paths(results)
    frozen_judge_prompt = results / "benchmark-definition" / "judge_prompt.md"
    judge_prompt_path = frozen_judge_prompt if frozen_judge_prompt.is_file() else JUDGE_PROMPT_PATH
    fixture = _load_json(fixture_path)
    rubric = _load_json(rubric_path)
    _validate_definition(fixture, rubric)
    manifest = _load_json(results / "manifest.json")
    task_by_id = {entry["id"]: entry for entry in rubric["tasks"]}
    attempts = list((manifest.get("attempts") or {}).values())
    selected_case_ids = manifest.get("selectedCaseIds") or [case["id"] for case in fixture["cases"]]
    selected_cases = [case for case in fixture["cases"] if case["id"] in selected_case_ids]
    if [case["id"] for case in selected_cases] != selected_case_ids:
        raise ValueError("manifest selectedCaseIds are unknown or out of fixture order")
    answer_key: dict[str, Any] = {}
    input_files: list[str] = []

    for case in selected_cases:
        case_attempts = [entry for entry in attempts if entry.get("caseId") == case["id"]]
        completed = [
            entry for entry in case_attempts
            if (entry.get("final") or {}).get("status") == "completed"
        ]
        profiles = {entry.get("profile") for entry in completed}
        if len(completed) != 2 or profiles != EXPECTED_PROFILES:
            raise ValueError(
                f"{case['id']}: expected exactly one completed lean and web_only answer; "
                f"found {sorted(str(profile) for profile in profiles)}"
            )
        completed.sort(key=lambda entry: entry["profile"])
        blind_seed = f"{manifest.get('evaluationId', '')}:{case['id']}".encode()
        if hashlib.sha256(blind_seed).digest()[0] % 2:
            completed.reverse()
        task = task_by_id[case["id"]]
        answer_key[case["id"]] = {}
        for label, attempt in zip(("A", "B"), completed, strict=True):
            answer, dialogue_turns = _answer_text(results, case, attempt["side"])
            final_answer = dialogue_turns[-1]["assistant"] if dialogue_turns else answer
            judge_input = {
                "schemaVersion": 1,
                "benchmarkId": fixture["benchmarkId"],
                "taskId": case["id"],
                "answerLabel": label,
                "assignment": case["prompt"],
                "candidateAnswer": answer,
                "candidateDialogue": dialogue_turns,
                "responseConstraints": {
                    "maximumWords": case["maxWords"],
                    "observedWhitespaceSeparatedWords": len(str(final_answer).split()),
                    "wordLimitAppliesTo": "final assistant turn" if dialogue_turns else "candidate answer",
                },
                "reference": {
                    "legalAsOf": case.get("legalAsOf"),
                    "goldSource": task.get("goldSource"),
                    "sources": task["sources"],
                    "referenceBrief": task["referenceBrief"],
                    "acceptedAlternatives": task.get("acceptedAlternatives", []),
                    "mandatoryDeliverableElements": task.get("mandatoryDeliverableElements", []),
                    "dialogueEvaluation": {
                        "mode": case.get("dialogue", {}).get("mode"),
                        "maxClarificationRounds": case.get("dialogue", {}).get("maxClarificationRounds"),
                        "maximumInitialQuestions": case.get("dialogue", {}).get("maximumInitialQuestions"),
                    } if dialogue_turns else None,
                    "scoreScale": rubric["scoreScale"],
                    "criteria": task["criteria"],
                    "fatalErrors": task["fatalErrors"],
                },
            }
            path = results / "judge" / "inputs" / case["id"] / f"{label}.json"
            _write_json(path, judge_input)
            input_files.append(str(path))
            answer_key[case["id"]][label] = {
                "profile": attempt["profile"],
                "side": attempt["side"],
                "remoteRunId": attempt.get("remoteRunId"),
            }

    key_payload = {
        "schemaVersion": 1,
        "benchmarkId": fixture["benchmarkId"],
        "evaluationId": manifest.get("evaluationId"),
        "warning": "Keep this file away from the judge. It unblinds candidate profiles.",
        "answers": answer_key,
    }
    key_path = results / "judge" / "answer_key.json"
    _write_json(key_path, key_payload)
    preparation = {
        "ok": True,
        "benchmarkId": fixture["benchmarkId"],
        "preparedAt": datetime.now(timezone.utc).isoformat(),
        "judgeInputCount": len(input_files),
        "selectedCaseIds": selected_case_ids,
        "judgeInputDirectory": str(results / "judge" / "inputs"),
        "answerKey": str(key_path),
        "fixtureSha256": _file_digest(fixture_path),
        "rubricSha256": _file_digest(rubric_path),
        "judgePromptSha256": _file_digest(judge_prompt_path),
    }
    _write_json(results / "judge" / "preparation.json", preparation)
    return preparation


def _validate_score(
    payload: dict[str, Any], task: dict[str, Any], task_id: str, label: str
) -> dict[str, Any]:
    if payload.get("taskId") != task_id or payload.get("answerLabel") != label:
        raise ValueError(f"Judge identity mismatch for {task_id}/{label}")
    expected_criteria = [entry["id"] for entry in task["criteria"]]
    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or [entry.get("id") for entry in criteria] != expected_criteria:
        raise ValueError(f"Judge criterion IDs/order mismatch for {task_id}/{label}")
    for entry in criteria:
        score = entry.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not float(score).is_integer()
            or not 0 <= score <= 4
        ):
            raise ValueError(f"Invalid score for {task_id}/{label}/{entry.get('id')}")
        if not str(entry.get("reason") or "").strip():
            raise ValueError(f"Missing judge reason for {task_id}/{label}/{entry.get('id')}")
        if not str(entry.get("answerEvidence") or "").strip():
            raise ValueError(f"Missing answer evidence for {task_id}/{label}/{entry.get('id')}")
    expected_fatal = [entry["id"] for entry in task["fatalErrors"]]
    fatal = payload.get("fatalErrors")
    if not isinstance(fatal, list) or [entry.get("id") for entry in fatal] != expected_fatal:
        raise ValueError(f"Judge fatal-error IDs/order mismatch for {task_id}/{label}")
    for entry in fatal:
        if not isinstance(entry.get("present"), bool):
            raise ValueError(f"Invalid fatal-error decision for {task_id}/{label}/{entry.get('id')}")
        if not str(entry.get("reason") or "").strip():
            raise ValueError(f"Missing fatal-error reason for {task_id}/{label}/{entry.get('id')}")
        if not str(entry.get("answerEvidence") or "").strip():
            raise ValueError(f"Missing fatal-error evidence for {task_id}/{label}/{entry.get('id')}")
    return payload


def _diagnostics(manifest: dict[str, Any]) -> dict[str, Any]:
    by_profile: dict[str, dict[str, Any]] = {}
    for profile in sorted(EXPECTED_PROFILES):
        attempts = [
            attempt for attempt in (manifest.get("attempts") or {}).values()
            if attempt.get("profile") == profile
        ]
        metrics = [(attempt.get("final") or {}).get("metrics") or {} for attempt in attempts]
        sums: dict[str, int | float | None] = {}
        for key in (
            "totalTokens", "inputTokens", "cachedInputTokens", "uncachedInputTokens",
            "outputTokens", "reasoningOutputTokens", "toolCallCount", "webToolCallCount",
            "rechtsgraphToolCallCount", "durationMs",
        ):
            values = [entry.get(key) for entry in metrics]
            sums[key] = sum(values) if values and all(isinstance(value, (int, float)) for value in values) else None
        by_profile[profile] = {"completedRuns": len(attempts), "totals": sums}
    return {
        "notPartOfLegalScore": True,
        "note": "Usage and tool traces explain cost and behavior; they never contribute legal-quality points.",
        "profiles": by_profile,
    }


def _candidate_provider(manifest: dict[str, Any], profile: str) -> str | None:
    side = next(
        (
            manifest.get(name, {}) for name in ("left", "right")
            if manifest.get(name, {}).get("profile") == profile
        ),
        {},
    )
    return side.get("provider") or (side.get("config", {}).get("modelProvider") or {}).get("id")


def aggregate(results_directory: str | Path) -> dict[str, Any]:
    results = Path(results_directory).resolve()
    fixture_path, rubric_path = _definition_paths(results)
    fixture = _load_json(fixture_path)
    rubric = _load_json(rubric_path)
    _validate_definition(fixture, rubric)
    manifest = _load_json(results / "manifest.json")
    key = _load_json(results / "judge" / "answer_key.json")
    task_by_id = {entry["id"]: entry for entry in rubric["tasks"]}
    family_by_id = {entry["id"]: entry["family"] for entry in fixture["cases"]}
    fatal_cap = float(rubric["scoringPolicy"]["fatalErrorCap"])

    answers: list[dict[str, Any]] = []
    judge_models: set[str] = set()
    judge_runtime_configurations: dict[str, dict[str, Any]] = {}
    for task_id, mappings in key["answers"].items():
        task = task_by_id[task_id]
        for label, unblinded in mappings.items():
            score_dir = results / "judge" / "scores" / task_id / label
            score_files = sorted(score_dir.glob("repeat-*.json"))
            if not score_files:
                raise ValueError(f"No judge scores found for {task_id}/{label}")
            repeats = []
            for path in score_files:
                envelope = _load_json(path)
                payload = envelope.get("score") if isinstance(envelope.get("score"), dict) else envelope
                repeats.append(_validate_score(payload, task, task_id, label))
                if envelope.get("judgeModel"):
                    judge_models.add(envelope["judgeModel"])
                runtime_configuration = envelope.get("judgeConfiguration") or {
                    "provider": "unrecorded",
                    "model": envelope.get("judgeModel"),
                    "generationParametersRecorded": False,
                }
                judge_runtime_configurations[
                    json.dumps(runtime_configuration, sort_keys=True, ensure_ascii=False)
                ] = runtime_configuration

            criterion_results = []
            raw_score = 0.0
            for definition in task["criteria"]:
                entries = [
                    next(item for item in repeat["criteria"] if item["id"] == definition["id"])
                    for repeat in repeats
                ]
                average = statistics.fmean(item["score"] for item in entries)
                points = definition["weight"] * average / 4
                raw_score += points
                criterion_results.append({
                    "id": definition["id"],
                    "weight": definition["weight"],
                    "average0to4": round(average, 3),
                    "points": round(points, 2),
                    "repeatScores": [item["score"] for item in entries],
                })

            fatal_results = []
            fatal_decision_rule = "single_judge_decision" if len(repeats) == 1 else "strict_majority"
            for definition in task["fatalErrors"]:
                decisions = [
                    next(item for item in repeat["fatalErrors"] if item["id"] == definition["id"])["present"]
                    for repeat in repeats
                ]
                present = sum(decisions) > len(decisions) / 2
                fatal_results.append({
                    "id": definition["id"],
                    "present": present,
                    "decisionRule": fatal_decision_rule,
                    "repeatDecisions": decisions,
                })
            cap_applied = any(entry["present"] for entry in fatal_results)
            final_score = min(raw_score, fatal_cap) if cap_applied else raw_score
            answers.append({
                "taskId": task_id,
                "family": family_by_id[task_id],
                "answerLabel": label,
                "profile": unblinded["profile"],
                "repeatCount": len(repeats),
                "fatalDecisionRule": fatal_decision_rule,
                "rawScore": round(raw_score, 2),
                "fatalErrorCapApplied": cap_applied,
                "score": round(final_score, 2),
                "criteria": criterion_results,
                "fatalErrors": fatal_results,
            })

    repeat_counts = sorted({entry["repeatCount"] for entry in answers})
    if len(judge_models) > 1:
        raise ValueError(f"Judge models differ across answers: {sorted(judge_models)}")
    if len(judge_runtime_configurations) > 1:
        raise ValueError("Judge runtime configurations differ across answers")

    profiles: dict[str, Any] = {}
    observed_families = list(dict.fromkeys(entry["family"] for entry in answers))
    for profile in sorted(EXPECTED_PROFILES):
        entries = [entry for entry in answers if entry["profile"] == profile]
        family_scores = {
            family: _mean([entry["score"] for entry in entries if entry["family"] == family])
            for family in observed_families
        }
        profiles[profile] = {
            "macroAverage": _mean([entry["score"] for entry in entries]),
            "familyScores": family_scores,
            "fatalErrorAnswers": sum(entry["fatalErrorCapApplied"] for entry in entries),
            "fatalErrorRate": round(
                sum(entry["fatalErrorCapApplied"] for entry in entries) / len(entries), 4
            ),
        }

    tie_threshold = float(rubric["scoringPolicy"]["tieThresholdPoints"])
    paired = []
    wins = {"lean": 0, "web_only": 0, "ties": 0}
    selected_case_ids = manifest.get("selectedCaseIds") or list(key["answers"])
    selected_cases = [case for case in fixture["cases"] if case["id"] in selected_case_ids]
    if [case["id"] for case in selected_cases] != selected_case_ids:
        raise ValueError("selectedCaseIds are unknown or out of fixture order")
    for case in selected_cases:
        task_answers = {entry["profile"]: entry for entry in answers if entry["taskId"] == case["id"]}
        delta = task_answers["lean"]["score"] - task_answers["web_only"]["score"]
        if delta > tie_threshold:
            winner = "lean"
        elif delta < -tie_threshold:
            winner = "web_only"
        else:
            winner = "tie"
        wins["ties" if winner == "tie" else winner] += 1
        paired.append({
            "taskId": case["id"],
            "family": case["family"],
            "lean": task_answers["lean"]["score"],
            "web_only": task_answers["web_only"]["score"],
            "deltaLeanMinusWebOnly": round(delta, 2),
            "winner": winner,
        })

    report = {
        "schemaVersion": 2,
        "benchmarkId": fixture["benchmarkId"],
        "benchmark": {
            **_definition_identity(results),
            "selectedCaseIds": selected_case_ids,
        },
        "evaluationId": manifest.get("evaluationId"),
        "candidate": {
            "model": manifest.get("model"),
            "effort": manifest.get("effort"),
            "providers": {
                profile: _candidate_provider(manifest, profile)
                for profile in sorted(EXPECTED_PROFILES)
            },
        },
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "primaryMetric": "unweighted task macro-average",
        "judgeModels": sorted(judge_models),
        "judgeRepeatCounts": repeat_counts,
        "judgeConfiguration": {
            "models": sorted(judge_models),
            "runtime": next(iter(judge_runtime_configurations.values()), None),
            "repeatsByTaskProfile": {
                f"{entry['taskId']}:{entry['profile']}": entry["repeatCount"]
                for entry in sorted(answers, key=lambda item: (item["taskId"], item["profile"]))
            },
            "fatalDecisionRules": sorted({entry["fatalDecisionRule"] for entry in answers}),
        },
        "fatalErrorDecisionRule": rubric["scoringPolicy"]["fatalErrorDecisionRule"],
        "selectedCaseIds": selected_case_ids,
        "profiles": profiles,
        "paired": paired,
        "winTieLoss": wins,
        "answers": answers,
        "diagnostics": _diagnostics(manifest),
        "limitations": [
            "A single candidate run per profile and task does not estimate generation variance; use the fixed stability suite with candidate repetitions for that question.",
            "A fatal-error result based on one judge is explicitly a single model decision, not a majority finding.",
            "LLM-judge scores remain development signals until calibrated against blinded legally trained reviewers.",
            "The benchmark reuses some known development cases and is not a sealed held-out certification set.",
        ],
    }
    json_path = results / "legal-competence-report.json"
    md_path = results / "legal-competence-report.md"
    _write_json(json_path, report)
    rows = [
        "# RechtsGraph legal-competence report",
        "",
        f"Evaluation: `{report['evaluationId']}`  ",
        f"Judge model(s): {', '.join(report['judgeModels']) or 'not recorded'}",
        "",
        "## Primary result",
        "",
        "| Profile | Macro score | Fatal-error answers |",
        "| --- | ---: | ---: |",
    ]
    for profile in ("lean", "web_only"):
        entry = profiles[profile]
        rows.append(f"| `{profile}` | {entry['macroAverage']:.2f} | {entry['fatalErrorAnswers']} / {len(selected_case_ids)} |")
    rows += [
        "",
        f"Wins/ties/losses for `lean`: {wins['lean']} / {wins['ties']} / {wins['web_only']}",
        "",
        "## Paired tasks",
        "",
        "| Task | Family | lean | web_only | Difference | Winner |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for entry in paired:
        rows.append(
            f"| `{entry['taskId']}` | {entry['family']} | {entry['lean']:.2f} | "
            f"{entry['web_only']:.2f} | {entry['deltaLeanMinusWebOnly']:+.2f} | {entry['winner']} |"
        )
    rows += [
        "",
        "## Interpretation limits",
        "",
        *[f"- {item}" for item in report["limitations"]],
        "",
        "Token, latency, web, and RechtsGraph-call totals are available in the JSON report under `diagnostics`; they are not part of the legal-quality score.",
        "",
    ]
    md_path.write_text("\n".join(rows), encoding="utf-8")
    return {"ok": True, "reportJson": str(json_path), "reportMarkdown": str(md_path)}


def _comparison_report(path: str | Path) -> tuple[Path, dict[str, Any]]:
    candidate = Path(path).resolve()
    report_path = candidate / "legal-competence-report.json" if candidate.is_dir() else candidate
    if not report_path.is_file():
        raise ValueError(f"Aggregated report not found: {report_path}")
    report = _load_json(report_path)
    required = ("benchmark", "candidate", "profiles", "answers", "judgeConfiguration")
    missing = [key for key in required if key not in report]
    if missing:
        raise ValueError(
            f"{report_path}: report predates cross-run identity fields ({', '.join(missing)}); "
            "rerun benchmark.py aggregate"
        )
    return report_path, report


def _compatibility_identity(report: dict[str, Any]) -> dict[str, Any]:
    benchmark = report["benchmark"]
    return {
        "benchmarkVersion": benchmark.get("benchmarkVersion"),
        "selectedCaseIds": benchmark.get("selectedCaseIds"),
        "fixtureSha256": benchmark.get("fixtureSha256"),
        "rubricSha256": benchmark.get("rubricSha256"),
        "judgePromptSha256": benchmark.get("judgePromptSha256"),
        "judgeConfiguration": report.get("judgeConfiguration"),
    }


def _run_summary(report_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    answers_by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for answer in report["answers"]:
        answers_by_profile[answer["profile"]].append(answer)
    profiles: dict[str, Any] = {}
    for profile in sorted(answers_by_profile):
        answers = answers_by_profile[profile]
        profiles[profile] = {
            "macroScore": report["profiles"][profile]["macroAverage"],
            "familyScores": report["profiles"][profile]["familyScores"],
            "taskScores": {
                answer["taskId"]: answer["score"]
                for answer in sorted(answers, key=lambda item: item["taskId"])
            },
            "fatalErrorRate": report["profiles"][profile]["fatalErrorRate"],
        }
    return {
        "report": str(report_path),
        "evaluationId": report.get("evaluationId"),
        "candidate": report["candidate"],
        "absoluteScoring": {
            "scale": "0-100 rubric score per answer; unweighted macro-average across selected tasks",
            "profiles": profiles,
        },
        "judge": report["judgeConfiguration"],
        "benchmark": report["benchmark"],
    }


def _identity_mismatches(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches = []
    for field in (
        "benchmarkVersion", "selectedCaseIds", "fixtureSha256", "rubricSha256", "judgePromptSha256",
        "judgeConfiguration",
    ):
        if left.get(field) != right.get(field):
            mismatches.append({"field": field, "left": left.get(field), "right": right.get(field)})
    return mismatches


def compare_runs(
    run_or_report_paths: list[str | Path], output_directory: str | Path
) -> dict[str, Any]:
    if len(run_or_report_paths) < 2:
        raise ValueError("At least two aggregated run reports are required")
    loaded = [_comparison_report(path) for path in run_or_report_paths]
    summaries = [_run_summary(path, report) for path, report in loaded]
    comparisons = []
    for left_index in range(len(loaded)):
        for right_index in range(left_index + 1, len(loaded)):
            left_path, left = loaded[left_index]
            right_path, right = loaded[right_index]
            mismatches = _identity_mismatches(
                _compatibility_identity(left), _compatibility_identity(right)
            )
            comparison: dict[str, Any] = {
                "leftEvaluationId": left.get("evaluationId"),
                "rightEvaluationId": right.get("evaluationId"),
                "leftReport": str(left_path),
                "rightReport": str(right_path),
                "status": "strictly_comparable" if not mismatches else "incompatible_descriptive_only",
                "mismatches": mismatches,
                "differences": None,
            }
            if not mismatches:
                differences: dict[str, Any] = {}
                for profile in sorted(EXPECTED_PROFILES):
                    left_answers = {
                        entry["taskId"]: entry for entry in left["answers"] if entry["profile"] == profile
                    }
                    right_answers = {
                        entry["taskId"]: entry for entry in right["answers"] if entry["profile"] == profile
                    }
                    differences[profile] = {
                        "macroRightMinusLeft": round(
                            right["profiles"][profile]["macroAverage"]
                            - left["profiles"][profile]["macroAverage"], 2
                        ),
                        "familyRightMinusLeft": {
                            family: round(
                                right["profiles"][profile]["familyScores"][family]
                                - left["profiles"][profile]["familyScores"][family], 2
                            )
                            for family in left["profiles"][profile]["familyScores"]
                        },
                        "taskRightMinusLeft": {
                            task_id: round(right_answers[task_id]["score"] - left_answers[task_id]["score"], 2)
                            for task_id in left["benchmark"]["selectedCaseIds"]
                        },
                        "fatalErrorRateRightMinusLeft": round(
                            right["profiles"][profile]["fatalErrorRate"]
                            - left["profiles"][profile]["fatalErrorRate"], 4
                        ),
                    }
                comparison["differences"] = differences
            comparisons.append(comparison)

    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "scorePolicy": "Absolute rubric scores are primary. Blind A/B pairing prevents judge bias and does not define the scale.",
        "comparabilityPolicy": (
            "Numeric cross-run differences are emitted only when benchmark version, ordered task selection, "
            "rubric hash, judge-prompt hash, and judge configuration are identical."
        ),
        "runs": summaries,
        "comparisons": comparisons,
        "diagnosticMetricsExcluded": True,
    }
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "cross-run-comparison.json"
    md_path = output / "cross-run-comparison.md"
    _write_json(json_path, result)
    lines = [
        "# Legal-competence cross-run comparison",
        "",
        "Every answer is scored independently on the fixed 0–100 rubric. Pairing is used only for blind judging.",
        "Token and tool metrics are diagnostic and never enter these scores.",
        "",
        "## Absolute scores",
        "",
        "| Evaluation | Candidate | Profile | Macro | Fatal-error rate |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for summary in summaries:
        candidate = summary["candidate"]
        name = f"{candidate.get('model') or 'unknown'} ({candidate.get('effort') or 'unspecified'})"
        for profile, profile_result in summary["absoluteScoring"]["profiles"].items():
            lines.append(
                f"| `{summary['evaluationId']}` | {name} | `{profile}` | "
                f"{profile_result['macroScore']:.2f} | {profile_result['fatalErrorRate']:.1%} |"
            )
    lines += ["", "## Comparability gates", ""]
    for comparison in comparisons:
        lines.append(
            f"- `{comparison['leftEvaluationId']}` vs. `{comparison['rightEvaluationId']}`: "
            f"**{comparison['status']}**"
        )
        for mismatch in comparison["mismatches"]:
            lines.append(f"  - mismatch: `{mismatch['field']}`")
    lines += [
        "",
        "Detailed task, family, judge, hash, and compatible-difference data are in the JSON report.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "reportJson": str(json_path), "reportMarkdown": str(md_path)}


def aggregate_repetitions(group_directory: str | Path) -> dict[str, Any]:
    group = Path(group_directory).resolve()
    repetition_paths = sorted(group.glob("candidate-repeat-*/legal-competence-report.json"))
    if len(repetition_paths) < 2:
        raise ValueError("At least two aggregated candidate-repeat reports are required")
    reports = [_load_json(path) for path in repetition_paths]
    selected = reports[0].get("selectedCaseIds")
    if any(report.get("selectedCaseIds") != selected for report in reports):
        raise ValueError("Candidate repetitions use different task selections")
    task_profiles: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        for answer in report["answers"]:
            task_profiles[(answer["taskId"], answer["profile"])].append(answer)
    expected = len(reports)
    if any(len(entries) != expected for entries in task_profiles.values()):
        raise ValueError("Candidate repetition reports are incomplete")

    task_results = []
    for (task_id, profile), entries in sorted(task_profiles.items()):
        scores = [entry["score"] for entry in entries]
        task_results.append({
            "taskId": task_id,
            "profile": profile,
            "candidateRepeatScores": scores,
            "mean": _mean(scores),
            "sampleStandardDeviation": round(statistics.stdev(scores), 2) if len(scores) > 1 else 0.0,
            "fatalErrorRuns": sum(entry["fatalErrorCapApplied"] for entry in entries),
        })

    profile_results = {}
    for profile in sorted(EXPECTED_PROFILES):
        repeat_macros = [report["profiles"][profile]["macroAverage"] for report in reports]
        profile_results[profile] = {
            "candidateRepeatMacroScores": repeat_macros,
            "meanMacroAverage": _mean(repeat_macros),
            "sampleStandardDeviation": round(statistics.stdev(repeat_macros), 2),
        }
    deltas = [
        report["profiles"]["lean"]["macroAverage"] - report["profiles"]["web_only"]["macroAverage"]
        for report in reports
    ]
    result = {
        "schemaVersion": 1,
        "benchmarkId": reports[0]["benchmarkId"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "candidateRepeatCount": len(reports),
        "selectedCaseIds": selected,
        "profiles": profile_results,
        "pairedMacroDeltaLeanMinusWebOnly": {
            "candidateRepeatDeltas": [round(value, 2) for value in deltas],
            "mean": _mean(deltas),
            "sampleStandardDeviation": round(statistics.stdev(deltas), 2),
        },
        "tasks": task_results,
        "interpretation": "This report estimates answer-generation stability. Judge repeats remain a separate uncertainty axis.",
    }
    json_path = group / "candidate-stability-report.json"
    md_path = group / "candidate-stability-report.md"
    _write_json(json_path, result)
    lines = [
        "# RechtsGraph candidate-stability report",
        "",
        f"Candidate repetitions: {len(reports)}  ",
        f"Tasks per profile: {len(selected)}",
        "",
        "| Profile | Mean macro score | SD across candidate runs |",
        "| --- | ---: | ---: |",
    ]
    for profile in ("lean", "web_only"):
        entry = profile_results[profile]
        lines.append(f"| `{profile}` | {entry['meanMacroAverage']:.2f} | {entry['sampleStandardDeviation']:.2f} |")
    lines += [
        "",
        f"Mean paired macro difference (`lean - web_only`): {result['pairedMacroDeltaLeanMinusWebOnly']['mean']:+.2f} ",
        f"(SD {result['pairedMacroDeltaLeanMinusWebOnly']['sampleStandardDeviation']:.2f}).",
        "",
        "These repetitions measure candidate-answer variation. They do not substitute for targeted judge rechecks or legal-expert calibration.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "reportJson": str(json_path), "reportMarkdown": str(md_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the frozen benchmark definition")
    prepare = subparsers.add_parser("prepare-judge", help="Create blinded judge inputs")
    prepare.add_argument("results_directory")
    aggregate_parser = subparsers.add_parser("aggregate", help="Aggregate completed judge outputs")
    aggregate_parser.add_argument("results_directory")
    repetitions_parser = subparsers.add_parser(
        "aggregate-repetitions", help="Aggregate separately judged candidate repetitions"
    )
    repetitions_parser.add_argument("group_directory")
    snapshot_parser = subparsers.add_parser(
        "snapshot", help="Copy the first complete paired tasks into a checksum-locked snapshot"
    )
    snapshot_parser.add_argument("source_directory")
    snapshot_parser.add_argument("output_directory")
    snapshot_parser.add_argument("--task-count", type=int, default=5)
    verify_parser = subparsers.add_parser(
        "verify-snapshot", help="Verify a snapshot against its SHA-256 inventory"
    )
    verify_parser.add_argument("snapshot_directory")
    compare_parser = subparsers.add_parser(
        "compare-runs", help="Compare absolute scores across strictly compatible aggregated runs"
    )
    compare_parser.add_argument("output_directory")
    compare_parser.add_argument("runs", nargs="+")
    arguments = parser.parse_args()
    if arguments.command == "validate":
        result = validate()
    elif arguments.command == "prepare-judge":
        result = prepare_judge(arguments.results_directory)
    elif arguments.command == "aggregate":
        result = aggregate(arguments.results_directory)
    elif arguments.command == "aggregate-repetitions":
        result = aggregate_repetitions(arguments.group_directory)
    elif arguments.command == "snapshot":
        result = create_snapshot(
            arguments.source_directory, arguments.output_directory, task_count=arguments.task_count
        )
    elif arguments.command == "verify-snapshot":
        result = verify_snapshot(arguments.snapshot_directory)
    else:
        result = compare_runs(arguments.runs, arguments.output_directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
