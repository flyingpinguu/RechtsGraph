import json
import shutil
from pathlib import Path

from evals.legal_competence_v1.benchmark import (
    FIXTURE_PATH,
    RUBRIC_PATH,
    _load_json,
    aggregate,
    aggregate_repetitions,
    compare_runs,
    create_snapshot,
    prepare_judge,
    validate,
    verify_snapshot,
)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _candidate_results(directory: Path) -> None:
    fixture = _load_json(FIXTURE_PATH)
    manifest = {
        "evaluationId": "legal-test",
        "selectedCaseIds": [case["id"] for case in fixture["cases"]],
        "attempts": {},
    }
    for case in fixture["cases"]:
        for side, profile in (("left", "lean"), ("right", "web_only")):
            manifest["attempts"][f"{case['id']}:{side}"] = {
                "caseId": case["id"],
                "side": side,
                "profile": profile,
                "remoteRunId": f"{case['id']}-{side}",
                "final": {
                    "status": "completed",
                    "metrics": {"totalTokens": 100, "toolCallCount": 1 if profile == "lean" else 0},
                },
            }
            _write_json(
                directory / "raw" / case["id"] / side / "final-response.json",
                {"body": {"status": "completed", "finalText": f"Juristische Antwort {case['id']} {side}."}},
            )
            if case.get("dialogue"):
                _write_json(
                    directory / "raw" / case["id"] / side / "dialogue-transcript.json",
                    {
                        "taskId": case["id"],
                        "turns": [
                            {
                                "turnNumber": 1,
                                "user": case["prompt"],
                                "assistant": "RÜCKFRAGEN: Ist das Original unterschrieben?",
                            },
                            {
                                "turnNumber": 2,
                                "user": "Das Original ist unterschrieben.",
                                "assistant": f"EINSCHÄTZUNG: Juristische Antwort {case['id']} {side}.",
                            },
                        ],
                    },
                )
    _write_json(directory / "manifest.json", manifest)


def test_definition_is_a_legal_work_benchmark_not_a_retrieval_test() -> None:
    result = validate()
    fixture = _load_json(FIXTURE_PATH)
    rubric = _load_json(RUBRIC_PATH)

    assert result["taskCount"] == 12
    assert result["plannedCandidateRuns"] == 24
    assert result["families"] == {
        "case_analysis": 4,
        "research_advice": 2,
        "document_review": 3,
        "drafting_communication": 2,
        "triage_judgment": 1,
    }
    assert all(case["artifact"] != "retrieval" for case in fixture["cases"])
    assert rubric["scoringPolicy"]["toolUse"] == "Never scored. Tool traces are diagnostic only."
    assert all(sum(criterion["weight"] for criterion in task["criteria"]) == 100 for task in rubric["tasks"])
    assert fixture["suites"]["smoke"] == fixture["suites"]["stability"]
    assert len(fixture["suites"]["quick"]) == 5
    assert len(fixture["suites"]["smoke"]) == 3
    compact = [case for case in fixture["cases"] if case["id"] in fixture["suites"]["smoke"]]
    assert all(not case.get("dialogue") for case in compact)
    assert {case["developmentPartition"] for case in fixture["cases"]} == {
        "known_solution", "development", "protected"
    }
    review_tasks = [task for task in rubric["tasks"] if task["family"] == "document_review"]
    assert all(any(item["id"] == "false_positive_control" for item in task["criteria"]) for task in review_tasks)


def test_compact_run_prepares_only_six_blind_answers(tmp_path: Path) -> None:
    _candidate_results(tmp_path)
    selected = _load_json(FIXTURE_PATH)["suites"]["smoke"]
    manifest = _load_json(tmp_path / "manifest.json")
    manifest["selectedCaseIds"] = selected
    manifest["attempts"] = {
        key: value for key, value in manifest["attempts"].items()
        if value["caseId"] in selected
    }
    _write_json(tmp_path / "manifest.json", manifest)
    prepared = prepare_judge(tmp_path)
    assert prepared["judgeInputCount"] == 6
    assert sorted(path.name for path in (tmp_path / "judge" / "inputs").iterdir()) == sorted(selected)


def test_prepare_is_blind_and_aggregate_unblinds_only_after_scoring(tmp_path: Path) -> None:
    _candidate_results(tmp_path)
    prepared = prepare_judge(tmp_path)
    assert prepared["judgeInputCount"] == 24

    key = _load_json(tmp_path / "judge" / "answer_key.json")
    rubric = _load_json(RUBRIC_PATH)
    for task in rubric["tasks"]:
        for label in ("A", "B"):
            judge_input_path = tmp_path / "judge" / "inputs" / task["id"] / f"{label}.json"
            serialized = judge_input_path.read_text(encoding="utf-8")
            assert '"profile"' not in serialized
            assert '"toolCallCount"' not in serialized
            assert "web_only" not in serialized
            profile = key["answers"][task["id"]][label]["profile"]
            judge_input = _load_json(judge_input_path)
            if task["id"] == "triage_01_employment_dismissal":
                assert len(judge_input["candidateDialogue"]) == 2
                assert "RÜCKFRAGEN" in judge_input["candidateAnswer"]
            score = {
                "taskId": task["id"],
                "answerLabel": label,
                "criteria": [
                    {"id": criterion["id"], "score": 4 if profile == "lean" else 3,
                     "reason": "Test reason", "answerEvidence": "Test evidence"}
                    for criterion in task["criteria"]
                ],
                "fatalErrors": [
                    {
                        "id": fatal["id"],
                        "present": bool(
                            profile == "lean"
                            and task["id"] == "case_01_representation"
                            and index == 0
                        ),
                        "reason": "Synthetic fatal decision",
                        "answerEvidence": "Synthetic evidence",
                    }
                    for index, fatal in enumerate(task["fatalErrors"])
                ],
                "overallAssessment": "Synthetic test score",
            }
            _write_json(
                tmp_path / "judge" / "scores" / task["id"] / label / "repeat-01.json",
                {"judgeModel": "test-judge", "score": score},
            )

    # Targeted judge rechecks are allowed without forcing the same repeat count
    # on every other answer.
    targeted = tmp_path / "judge" / "scores" / "document_01_employment_contract" / "A"
    _write_json(targeted / "repeat-02.json", _load_json(targeted / "repeat-01.json"))

    result = aggregate(tmp_path)
    report = _load_json(Path(result["reportJson"]))
    assert report["profiles"]["lean"]["macroAverage"] == 95.83
    assert report["profiles"]["web_only"]["macroAverage"] == 75
    assert report["profiles"]["lean"]["fatalErrorAnswers"] == 1
    assert report["winTieLoss"] == {"lean": 11, "web_only": 1, "ties": 0}
    assert report["judgeRepeatCounts"] == [1, 2]
    targeted_answer = next(
        answer for answer in report["answers"]
        if answer["taskId"] == "document_01_employment_contract" and answer["answerLabel"] == "A"
    )
    assert targeted_answer["fatalDecisionRule"] == "strict_majority"
    assert report["diagnostics"]["notPartOfLegalScore"] is True
    assert report["schemaVersion"] == 2
    assert report["benchmark"]["rubricSha256"]
    assert report["benchmark"]["judgePromptSha256"]
    assert report["judgeConfiguration"]["models"] == ["test-judge"]
    assert report["candidate"]["model"] is None
    assert (tmp_path / "legal-competence-report.md").is_file()


def test_candidate_repetition_aggregation_keeps_generation_variance_separate(tmp_path: Path) -> None:
    group = tmp_path / "stability"
    for repeat, lean, web in ((1, 82, 78), (2, 88, 79)):
        answers = []
        for task_id in ("case_02_factoring", "triage_01_employment_dismissal"):
            answers.extend([
                {"taskId": task_id, "profile": "lean", "score": lean,
                 "fatalErrorCapApplied": False},
                {"taskId": task_id, "profile": "web_only", "score": web,
                 "fatalErrorCapApplied": False},
            ])
        _write_json(
            group / f"candidate-repeat-{repeat:02d}" / "legal-competence-report.json",
            {
                "benchmarkId": "rechtsgraph-legal-competence-v1",
                "selectedCaseIds": ["case_02_factoring", "triage_01_employment_dismissal"],
                "profiles": {
                    "lean": {"macroAverage": lean},
                    "web_only": {"macroAverage": web},
                },
                "answers": answers,
            },
        )

    result = aggregate_repetitions(group)
    report = _load_json(Path(result["reportJson"]))
    assert report["candidateRepeatCount"] == 2
    assert report["profiles"]["lean"]["meanMacroAverage"] == 85
    assert report["pairedMacroDeltaLeanMinusWebOnly"]["candidateRepeatDeltas"] == [4, 9]


def test_partial_snapshot_copies_only_complete_pairs_and_detects_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "snapshot"
    _candidate_results(source)
    frozen = source / "benchmark-definition"
    frozen.mkdir()
    definition_files = (
        "fixture.json", "rubric.json", "judge_prompt.md", "source_passages.json",
    )
    for filename in definition_files:
        shutil.copy2(FIXTURE_PATH.parent / filename, frozen / filename)
    _write_json(
        source / "benchmark-definition.json",
        {
            "benchmarkVersion": "legal-competence-v1",
            "files": {
                filename: __import__("hashlib").sha256((frozen / filename).read_bytes()).hexdigest()
                for filename in definition_files
            },
        },
    )
    manifest_before = (source / "manifest.json").read_bytes()

    created = create_snapshot(source, target, task_count=5)
    assert created["ok"] is True
    assert (source / "manifest.json").read_bytes() == manifest_before
    snapshot_manifest = _load_json(target / "manifest.json")
    assert len(snapshot_manifest["selectedCaseIds"]) == 5
    assert len(snapshot_manifest["attempts"]) == 10
    assert len(list((target / "raw").glob("*/*/final-response.json"))) == 10
    assert verify_snapshot(target)["fileCount"] > 10
    assert prepare_judge(target)["judgeInputCount"] == 10

    # Derived judge files may be added, while candidate evidence remains locked.
    assert verify_snapshot(target)["ok"] is True
    manifest = _load_json(target / "manifest.json")
    manifest["selectedCaseIds"].pop()
    _write_json(target / "manifest.json", manifest)
    try:
        verify_snapshot(target)
    except ValueError as error:
        assert "changed=['manifest.json']" in str(error)
    else:
        raise AssertionError("snapshot mutation was not detected")


def test_cross_run_comparison_uses_absolute_scores_and_hard_gates(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    base = {
        "schemaVersion": 2,
        "benchmarkId": "rechtsgraph-legal-competence-v1",
        "evaluationId": "first",
        "benchmark": {
            "benchmarkVersion": "legal-competence-v1",
            "selectedCaseIds": ["case_01_representation"],
            "fixtureSha256": "fixture",
            "rubricSha256": "rubric",
            "judgePromptSha256": "prompt",
        },
        "candidate": {"model": "model-a", "effort": "low", "providers": {}},
        "profiles": {
            "lean": {"macroAverage": 80, "familyScores": {"case_analysis": 80}, "fatalErrorRate": 0},
            "web_only": {"macroAverage": 70, "familyScores": {"case_analysis": 70}, "fatalErrorRate": 0},
        },
        "answers": [
            {"taskId": "case_01_representation", "profile": "lean", "score": 80},
            {"taskId": "case_01_representation", "profile": "web_only", "score": 70},
        ],
        "judgeConfiguration": {
            "models": ["fixed-judge"],
            "repeatsByTaskProfile": {
                "case_01_representation:lean": 1,
                "case_01_representation:web_only": 1,
            },
            "fatalDecisionRules": ["single_judge_decision"],
        },
    }
    _write_json(first_dir / "legal-competence-report.json", base)
    second = json.loads(json.dumps(base))
    second["evaluationId"] = "second"
    second["candidate"]["model"] = "model-b"
    second["profiles"]["lean"]["macroAverage"] = 84
    second["answers"][0]["score"] = 84
    _write_json(second_dir / "legal-competence-report.json", second)

    output = tmp_path / "comparison"
    result = compare_runs([first_dir, second_dir], output)
    report = _load_json(Path(result["reportJson"]))
    assert report["runs"][1]["absoluteScoring"]["profiles"]["lean"]["macroScore"] == 84
    assert report["comparisons"][0]["status"] == "strictly_comparable"
    assert report["comparisons"][0]["differences"]["lean"]["macroRightMinusLeft"] == 4

    second["benchmark"]["judgePromptSha256"] = "different-prompt"
    _write_json(second_dir / "legal-competence-report.json", second)
    incompatible = compare_runs([first_dir, second_dir], tmp_path / "incompatible")
    incompatible_report = _load_json(Path(incompatible["reportJson"]))
    pair = incompatible_report["comparisons"][0]
    assert pair["status"] == "incompatible_descriptive_only"
    assert pair["differences"] is None
    assert [entry["field"] for entry in pair["mismatches"]] == ["judgePromptSha256"]
