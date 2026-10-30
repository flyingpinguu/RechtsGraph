import json
from pathlib import Path

from evals.benchmark_v1.benchmark import (
    FIXTURE_PATH,
    RUBRIC_PATH,
    _json_digest,
    _load_json,
    _score_check,
    estimate_cost,
    prepare_review,
    score_review,
    validate_definition,
)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_benchmark_definition_is_complete_and_compact() -> None:
    fixture = _load_json(FIXTURE_PATH)
    rubric = _load_json(RUBRIC_PATH)

    result = validate_definition(fixture, rubric)

    assert result["ok"] is True
    assert result["caseCount"] == 7
    assert result["panels"] == {"end_to_end": 3, "no_harm": 1, "retrieval": 3}


def test_exact_result_set_uses_objective_f1_and_rejects_extra_items() -> None:
    check = {
        "id": "set",
        "type": "exact_result_set",
        "weight": 6,
        "prefix": "ERGEBNIS:",
        "expected": ["BGB § 412", "HGB § 354a", "ZPO § 851"],
    }

    points, result = _score_check(
        check,
        "ERGEBNIS: BGB § 412; HGB § 354a; falsches Gesetz § 1",
        {"toolItems": []},
    )

    assert points == 4
    assert result["observed"]["precision"] == 2 / 3
    assert result["observed"]["recall"] == 2 / 3


def _answer(case_id: str) -> str:
    if case_id.startswith("legal_"):
        return "Blind zu bewertende juristische Antwort."
    if case_id == "retrieval_01_bgb_130":
        return (
            "§ 130 Abs. 1 BGB: Eine Willenserklärung, die einem anderen gegenüber abzugeben ist, "
            "wird, wenn sie in dessen Abwesenheit abgegeben wird, in dem Zeitpunkt wirksam, in welchem "
            "sie ihm zugeht. Sie wird nicht wirksam, wenn dem anderen vorher oder gleichzeitig ein "
            "Widerruf zugeht. Die erste Regel betrifft den Zugang beim Abwesenden. Die zweite regelt den "
            "vorherigen oder gleichzeitigen Widerruf. Fundstelle: BGB § 130 Abs. 1."
        )
    if case_id == "retrieval_02_incoming_bgb_399":
        return (
            "Im Korpus wurden drei aufgelöste Verweise gefunden. Eine Kante belegt, dass die Norm § 399 "
            "BGB zitiert; sie beweist weder Anwendbarkeit noch Geltung im Einzelfall.\n"
            "ERGEBNIS: BGB § 412; HGB § 354a; ZPO § 851"
        )
    if case_id == "retrieval_03_ai_act_art_2":
        return (
            "Genannt werden die ePrivacy-Richtlinie 2002/58, die Datenschutz-Grundverordnung 2016/679 "
            "(DSGVO) und der Digital Services Act 2022/2065.\n"
            "ERGEBNIS_CELEX: 32002L0058; 32016R0679; 32022R2065"
        )
    if case_id == "control_01_email":
        return (
            "Betreff: Verschiebung unseres Termins\n\nSehr geehrte Frau Keller,\n\nleider muss ich unseren für "
            "Mittwoch, den 23. September 2026 um 10:00 Uhr vereinbarten Termin wegen einer internen "
            "Terminkollision verschieben. Als neuen Termin möchte ich Ihnen Freitag, den 25. September "
            "2026 um 14:30 Uhr vorschlagen. Bitte geben Sie mir kurz Bescheid, ob dieser Zeitpunkt für "
            "Sie passt, und bestätigen Sie den Ersatztermin. Ich entschuldige mich für die kurzfristige "
            "Änderung und danke Ihnen für Ihr Verständnis.\n\nMit freundlichen Grüßen\nMax Mustermann"
        )
    raise AssertionError(case_id)


def test_prepare_and_score_keep_profiles_blind_until_unblinding(tmp_path: Path) -> None:
    fixture = _load_json(FIXTURE_PATH)
    manifest = {
        "evaluationId": "eval-test",
        "fixtureSha256": _json_digest(fixture),
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
                    "metrics": {"totalTokens": 100, "toolCallCount": 0},
                },
            }
            _write_json(
                tmp_path / "raw" / case["id"] / side / "final-response.json",
                {
                    "body": {
                        "status": "completed",
                        "finalText": _answer(case["id"]),
                        "events": [],
                        "toolItems": [],
                    }
                },
            )
    _write_json(tmp_path / "manifest.json", manifest)

    prepared = prepare_review(tmp_path)
    blind_scorecard = _load_json(Path(prepared["scorecard"]))
    assert "lean" not in json.dumps(blind_scorecard)
    assert "web_only" not in json.dumps(blind_scorecard)

    for answer in blind_scorecard["answers"]:
        for criterion in answer.get("criteria") or []:
            criterion["score"] = 3
            criterion["note"] = "test"
    blind_scorecard["reviewer"] = "Test Reviewer"
    blind_scorecard["reviewedAt"] = "2026-09-15"
    _write_json(Path(prepared["scorecard"]), blind_scorecard)

    report = score_review(tmp_path)

    assert report["profiles"]["lean"]["panels"] == {
        "end_to_end": 100.0,
        "no_harm": 100.0,
        "retrieval": 100.0,
    }
    assert report["profiles"]["web_only"]["panels"] == report["profiles"]["lean"]["panels"]
    assert (tmp_path / "benchmark-report.md").is_file()


def test_cost_estimate_is_reproducible_from_archived_reference_runs() -> None:
    reference = FIXTURE_PATH.parent.parent / "results" / "recent-pilot-2026-09-11"

    result = estimate_cost(reference)

    assert result["assumptions"]["coreRuns"] == 14
    assert result["referenceUsage"]["runs"] == 6
    assert result["models"]["z-ai/glm-5.3-flash"]["estimatedFullCoreSuiteUSD"]["likely"] == 0.2545
