#!/usr/bin/env python3
"""Run legal-competence-v1 through the logged-in Claude Code CLI.

The output intentionally matches the manifest/raw-response layout consumed by
``legal_competence_v1/benchmark.py prepare-judge``.  Live execution is opt-in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVALS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = EVALS_DIR.parent
BENCHMARK_DIR = EVALS_DIR / "legal_competence_v1"
FIXTURE_PATH = BENCHMARK_DIR / "fixture.json"
DEFINITION_FILES = (
    "fixture.json", "rubric.json", "judge_prompt.md", "RESEARCH_BASIS.md",
    "README.md", "benchmark.py", "source_passages.json",
)
PROFILES = ("lean", "web_only")
SIDES = {"lean": "left", "web_only": "right"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_path(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture(suite: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fixture = read_json(FIXTURE_PATH)
    selected_ids = fixture.get("suites", {}).get(suite)
    if not isinstance(selected_ids, list) or not selected_ids:
        raise ValueError(f"Unknown suite {suite!r}")
    cases_by_id = {case["id"]: case for case in fixture["cases"]}
    return fixture, [cases_by_id[case_id] for case_id in selected_ids]


def profile_order(cases: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, int]]:
    plan: list[tuple[dict[str, Any], str, int]] = []
    for index, case in enumerate(cases):
        ordered = PROFILES if index % 2 == 0 else tuple(reversed(PROFILES))
        plan.extend((case, profile, position) for position, profile in enumerate(ordered))
    return plan


def build_prompts() -> dict[str, str]:
    base = "\n\n".join((
        "You are a read-only legal research agent.",
        "Do not inspect project files, run shell commands, edit files, or use collaboration tools.",
        (PROJECT_DIR / "debug_ui" / "LEGAL_RESEARCH_GUIDE.md").read_text(encoding="utf-8").strip(),
    ))
    lean = " ".join((
        "Choose the available evidence source that best fits the question; live web research may be used whenever it helps.",
        "Useful local options include find_documents for ambiguous document names, get_structural_unit for known provisions, search_corpus and selected get_context expansions for topics.",
        "Start with provisions bearing on decisive issues and avoid a fixed tool-call quota. Use compact structural-unit results by default and get_document_outline only when hierarchy matters.",
        "Before graph navigation, call get_service_status for neo4j and stop early if it is unavailable.",
        "Avoid routine progress commentary; a useful clarification question is welcome.",
    ))
    web_only = " ".join((
        "Live web search is available when evidence would help.",
        "Do not use RechtsGraph, MCP tools, shell tools, file tools, browser/computer tools, apps, plugins, skills, or collaboration tools.",
        "Avoid routine progress commentary; a useful clarification question is welcome.",
    ))
    return {"lean": f"{base}\n\n{lean}", "web_only": f"{base}\n\n{web_only}"}


def ensure_configs(output: Path, prompts: dict[str, str]) -> dict[str, str]:
    config_dir = output / "runner-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    mcp = {
        "lean": {
            "mcpServers": {
                "rechtsgraph": {
                    "type": "stdio",
                    "command": str(PROJECT_DIR / ".venv-mcp" / "bin" / "python"),
                    "args": ["-m", "rechtsgraph_mcp.server"],
                    "env": {"PYTHONPATH": str(PROJECT_DIR)},
                }
            }
        },
        "web_only": {"mcpServers": {}},
    }
    hashes: dict[str, str] = {}
    for profile in PROFILES:
        prompt_path = config_dir / f"{profile}.system.md"
        mcp_path = config_dir / f"{profile}.mcp.json"
        expected_prompt = prompts[profile] + "\n"
        expected_mcp = json.dumps(mcp[profile], ensure_ascii=False, indent=2) + "\n"
        for path, expected in ((prompt_path, expected_prompt), (mcp_path, expected_mcp)):
            if path.exists() and path.read_text(encoding="utf-8") != expected:
                raise ValueError(f"Resume mismatch: {path.name} changed")
            if not path.exists():
                path.write_text(expected, encoding="utf-8")
        hashes[f"{profile}.system"] = digest_path(prompt_path)
        hashes[f"{profile}.mcp"] = digest_path(mcp_path)
    return hashes


def archive_definition(output: Path) -> dict[str, str]:
    frozen = output / "benchmark-definition"
    frozen.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for filename in DEFINITION_FILES:
        source = BENCHMARK_DIR / filename
        destination = frozen / filename
        data = source.read_bytes()
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError(f"Frozen benchmark definition changed: {filename}")
        if not destination.exists():
            destination.write_bytes(data)
        hashes[filename] = digest_bytes(data)
    metadata = {"schemaVersion": 1, "benchmarkVersion": "legal-competence-v1", "files": hashes}
    metadata_path = output / "benchmark-definition.json"
    if metadata_path.exists() and read_json(metadata_path) != metadata:
        raise ValueError("Frozen benchmark metadata changed")
    if not metadata_path.exists():
        write_json(metadata_path, metadata)
    return hashes


def run_command(command: list[str], *, cwd: Path, env: dict[str, str], stdin: str,
                stdout_path: Path, stderr_path: Path, timeout: int) -> int:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )

    def pump(stream: Any, path: Path) -> None:
        with path.open("wb") as handle:
            while chunk := stream.read1(65536):
                handle.write(chunk)
                handle.flush()

    out_thread = threading.Thread(target=pump, args=(process.stdout, stdout_path))
    err_thread = threading.Thread(target=pump, args=(process.stderr, stderr_path))
    out_thread.start()
    err_thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(stdin.encode("utf-8"))
        process.stdin.close()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise TimeoutError(f"Claude turn exceeded {timeout} seconds")
    finally:
        out_thread.join()
        err_thread.join()
    return process.returncode


def claude_command(args: argparse.Namespace, profile: str, output: Path,
                   session_id: str, resume: bool) -> list[str]:
    allowed = ["WebSearch", "WebFetch"]
    if profile == "lean":
        allowed.append("mcp__rechtsgraph__*")
    command = [
        args.claude_bin, "--print", "--verbose", "--output-format", "stream-json",
        "--model", args.model, "--effort", args.effort,
        "--tools", "WebSearch,WebFetch", "--allowedTools", *allowed,
        "--strict-mcp-config", "--mcp-config", str(output / "runner-config" / f"{profile}.mcp.json"),
        "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
    ]
    if resume:
        command.extend(("--resume", session_id))
    else:
        command.extend(("--session-id", session_id, "--system-prompt", (output / "runner-config" / f"{profile}.system.md").read_text(encoding="utf-8")))
    return command


def inspect_stream(path: Path, profile: str, requested_model: str, *, require_init: bool) -> dict[str, Any]:
    init = None
    result = None
    assistant_models: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant":
            message = event.get("message", {})
            if message.get("model"):
                assistant_models.append(message["model"])
            for block in message.get("content", []):
                if block.get("type") == "tool_use":
                    tool_calls.append({"name": block.get("name"), "input": block.get("input")})
    if result is None:
        raise ValueError("Claude result event missing")
    if result.get("is_error") is not False or result.get("permission_denials"):
        raise ValueError(f"Claude result error: {result.get('result') or result.get('permission_denials')}")
    answer = result.get("result")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Claude answer missing")
    if not assistant_models:
        raise ValueError("Claude assistant model missing")
    if require_init:
        if init is None:
            raise ValueError("Claude init event missing")
        tools = set(init.get("tools") or [])
        if not WEB_TOOLS <= tools:
            raise ValueError("WebSearch/WebFetch absent")
        mcp_tools = {tool for tool in tools if tool.startswith("mcp__")}
        if profile == "lean":
            if not mcp_tools or any(not tool.startswith("mcp__rechtsgraph__") for tool in mcp_tools):
                raise ValueError("lean RechtsGraph tool inventory invalid")
            servers = init.get("mcp_servers") or []
            if not any(server.get("name") == "rechtsgraph" and server.get("status") == "connected" for server in servers):
                raise ValueError("RechtsGraph MCP is not connected")
        elif mcp_tools:
            raise ValueError("web_only unexpectedly exposed MCP tools")
    usage = result.get("modelUsage") or {}
    if not usage:
        raise ValueError("Claude model usage missing")
    effective_models = set(assistant_models) | set(usage)
    if not any(requested_model in model or model in requested_model for model in effective_models):
        raise ValueError(f"Requested model {requested_model} not observed: {sorted(effective_models)}")
    return {
        "answer": answer.strip(),
        "sessionId": result.get("session_id"),
        "modelUsage": usage,
        "assistantModels": sorted(set(assistant_models)),
        "durationMs": result.get("duration_ms"),
        "toolCalls": tool_calls,
        "init": init,
    }


def should_continue(answer: str, dialogue: dict[str, Any]) -> bool:
    lowered = answer.casefold()
    if str(dialogue.get("finalHeading", "EINSCHÄTZUNG")).casefold() in lowered:
        return False
    return "?" in answer or "rückfragen" in lowered


def dialogue_reply(answer: str, dialogue: dict[str, Any], answered: set[str], round_number: int) -> tuple[str, list[str]]:
    lowered = answer.casefold()
    matched = [
        fact for fact in dialogue.get("facts", [])
        if fact["id"] not in answered and any(str(pattern).casefold() in lowered for pattern in fact.get("match", []))
    ]
    lines = [f"- {fact['answer']}" for fact in matched] or ["- Zu den neu angesprochenen Punkten liegen mir keine weiteren Informationen vor."]
    max_rounds = int(dialogue.get("maxClarificationRounds", 1))
    if round_number < max_rounds:
        instruction = f"Gib jetzt entweder unter {dialogue.get('finalHeading', 'EINSCHÄTZUNG')} deine abschließende Antwort oder stelle unter {dialogue.get('followUpHeading', 'LETZTE RÜCKFRAGEN')} nur noch wirklich entscheidende offene Fragen."
    else:
        instruction = f"Gib jetzt unter {dialogue.get('finalHeading', 'EINSCHÄTZUNG')} die abschließende Antwort. Es gibt keine weitere Rückfragerunde."
    rendered_lines = "\n".join(lines)
    prompt = f"Hier sind die verfügbaren Antworten:\n{rendered_lines}\n\nFalls eine Frage damit nicht beantwortet ist, ist mir die betreffende Information nicht bekannt.\n\n{instruction}"
    return prompt, [fact["id"] for fact in matched]


def aggregate_usage(turns: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
    cost = 0.0
    duration = 0
    for turn in turns:
        duration += int(turn.get("durationMs") or 0)
        for model_usage in turn.get("modelUsage", {}).values():
            totals["inputTokens"] += int(model_usage.get("inputTokens") or 0)
            totals["outputTokens"] += int(model_usage.get("outputTokens") or 0)
            totals["cacheReadInputTokens"] += int(model_usage.get("cacheReadInputTokens") or 0)
            totals["cacheCreationInputTokens"] += int(model_usage.get("cacheCreationInputTokens") or 0)
            cost += float(model_usage.get("costUSD") or 0)
    return {**totals, "costUsdReported": round(cost, 8), "durationMs": duration, "turnCount": len(turns)}


def execute_attempt(args: argparse.Namespace, output: Path, case: dict[str, Any], profile: str,
                    attempt: dict[str, Any], manifest: dict[str, Any]) -> None:
    side = SIDES[profile]
    raw = output / "raw" / case["id"] / side
    raw.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="claude-legal-eval-"))
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    session_id = str(uuid.uuid4())
    turns: list[dict[str, Any]] = []
    transcript_turns: list[dict[str, Any]] = []
    answered: set[str] = set()
    user_prompt = case["prompt"]
    max_turns = 1 + (int(case.get("dialogue", {}).get("maxClarificationRounds", 0)) if case.get("dialogue") else 0)
    try:
        for turn_number in range(1, max_turns + 1):
            stdout_path = raw / f"turn-{turn_number:02d}-stdout.jsonl"
            stderr_path = raw / f"turn-{turn_number:02d}-stderr.txt"
            command = claude_command(args, profile, output, session_id, resume=turn_number > 1)
            exit_code = run_command(command, cwd=work, env=env, stdin=user_prompt,
                                    stdout_path=stdout_path, stderr_path=stderr_path,
                                    timeout=args.timeout_seconds)
            if exit_code != 0:
                raise RuntimeError(f"Claude exited {exit_code}; see {stderr_path}")
            turn = inspect_stream(stdout_path, profile, args.model, require_init=turn_number == 1)
            if turn.get("sessionId") and turn["sessionId"] != session_id:
                raise ValueError("Claude returned a different session id")
            turns.append(turn)
            transcript_turns.append({
                "turnNumber": turn_number,
                "user": user_prompt,
                "assistant": turn["answer"],
                "remoteRunId": session_id,
                "matchedFactIds": [],
            })
            dialogue = case.get("dialogue")
            if not dialogue or not should_continue(turn["answer"], dialogue):
                break
            round_number = turn_number
            if round_number > int(dialogue.get("maxClarificationRounds", 1)):
                break
            user_prompt, matched = dialogue_reply(turn["answer"], dialogue, answered, round_number)
            answered.update(matched)
            transcript_turns[-1]["matchedFactIds"] = matched

        final_answer = turns[-1]["answer"]
        response = {
            "status": 200,
            "receivedAt": now(),
            "body": {
                "id": session_id,
                "status": "completed",
                "finalText": final_answer,
                "metrics": aggregate_usage(turns),
            },
        }
        write_json(raw / "final-response.json", response)
        if case.get("dialogue"):
            write_json(raw / "dialogue-transcript.json", {
                "schemaVersion": 1,
                "taskId": case["id"],
                "mode": case["dialogue"]["mode"],
                "maxClarificationRounds": case["dialogue"]["maxClarificationRounds"],
                "answeredFactIds": sorted(answered),
                "turns": transcript_turns,
            })
        write_json(raw / "claude-details.json", {
            "sessionId": session_id,
            "model": args.model,
            "effort": args.effort,
            "turns": [{key: value for key, value in turn.items() if key not in {"answer", "init"}} for turn in turns],
        })
        attempt.update({
            "state": "finished", "completedAt": now(),
            "remoteRunId": session_id,
            "final": {"id": session_id, "status": "completed", "metrics": aggregate_usage(turns)},
        })
    finally:
        shutil.rmtree(work, ignore_errors=True)
        write_json(output / "manifest.json", manifest)


def snapshot_environment(output: Path, args: argparse.Namespace, cases: list[dict[str, Any]]) -> None:
    path = output / "environment-snapshot.json"
    if path.exists():
        return
    auth = subprocess.run([args.claude_bin, "auth", "status", "--json"], cwd=PROJECT_DIR,
                          text=True, capture_output=True, check=False)
    auth_payload: Any = {"ok": False, "returncode": auth.returncode}
    if auth.returncode == 0:
        parsed = json.loads(auth.stdout)
        auth_payload = {key: parsed.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
    status = subprocess.run([
        str(PROJECT_DIR / ".venv-mcp" / "bin" / "python"), "-c",
        "import json; from rechtsgraph_mcp.server import _service_status; print(json.dumps(_service_status('all'), ensure_ascii=False))",
    ], cwd=PROJECT_DIR, text=True, capture_output=True, check=False)
    write_json(path, {
        "schemaVersion": 1, "capturedAt": now(),
        "execution": {"provider": "claude-cli-subscription", "model": args.model, "effort": args.effort,
                      "comparison": "tools", "selectedCaseIds": [case["id"] for case in cases]},
        "claudeAuth": auth_payload,
        "serviceStatus": json.loads(status.stdout) if status.returncode == 0 else {"ok": False, "stderr": status.stderr.strip()},
        "gitHead": subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_DIR.parent, text=True, capture_output=True).stdout.strip(),
    })


def build_manifest(args: argparse.Namespace, fixture: dict[str, Any], cases: list[dict[str, Any]], hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "comparison": "tools", "schemaVersion": 1, "evaluationId": str(uuid.uuid4()),
        "createdAt": now(), "fixturePath": str(FIXTURE_PATH), "fixtureSha256": digest_path(FIXTURE_PATH),
        "referenceStatus": fixture.get("referenceStatus", "unreviewed"),
        "selectedCaseIds": [case["id"] for case in cases],
        "left": {"profile": "lean", "provider": "claude-cli-subscription"},
        "right": {"profile": "web_only", "provider": "claude-cli-subscription"},
        "model": args.model, "effort": args.effort, "timeoutSeconds": args.timeout_seconds,
        "inputHashes": hashes,
        "plan": [
            {"caseId": case["id"], "side": SIDES[profile], "profile": profile,
             "position": position, "dialogue": bool(case.get("dialogue"))}
            for case, profile, position in profile_order(cases)
        ],
        "attempts": {},
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture, cases = load_fixture(args.suite)
    prompts = build_prompts()
    plan = profile_order(cases)
    if not args.execute:
        return {
            "dryRun": True, "provider": "claude-cli-subscription", "model": args.model,
            "effort": args.effort, "suite": args.suite, "selectedCaseIds": [case["id"] for case in cases],
            "plannedAgentRuns": len(plan), "plannedModelTurns": {
                "minimum": len(plan) + sum(bool(case.get("dialogue")) for case in cases) * 2,
                "maximum": len(plan) + sum(int(case.get("dialogue", {}).get("maxClarificationRounds", 0)) for case in cases) * 2,
            },
        }
    if args.output_dir is None:
        raise ValueError("--output-dir is required with --execute")
    if not shutil.which(args.claude_bin):
        raise ValueError("Claude CLI unavailable")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".runner-lock"
    lock.mkdir()
    try:
        definition_hashes = archive_definition(output)
        config_hashes = ensure_configs(output, prompts)
        verify = subprocess.run([
            str(PROJECT_DIR / ".venv-mcp" / "bin" / "python"), str(BENCHMARK_DIR / "benchmark.py"), "validate"
        ], cwd=PROJECT_DIR, text=True, capture_output=True, check=True)
        write_json(output / "benchmark-validation.json", json.loads(verify.stdout))
        snapshot_environment(output, args, cases)
        manifest_path = output / "manifest.json"
        expected_hashes = {**definition_hashes, **config_hashes}
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            checks = {
                "model": args.model, "effort": args.effort,
                "selectedCaseIds": [case["id"] for case in cases], "inputHashes": expected_hashes,
            }
            for key, value in checks.items():
                if manifest.get(key) != value:
                    raise ValueError(f"Resume mismatch at {key}")
        else:
            manifest = build_manifest(args, fixture, cases, expected_hashes)
            write_json(manifest_path, manifest)
        for case, profile, position in plan:
            side = SIDES[profile]
            key = f"{case['id']}:{side}"
            previous = manifest["attempts"].get(key)
            if previous and previous.get("final", {}).get("status") == "completed":
                continue
            if previous:
                raise RuntimeError(f"Refusing to repeat incomplete attempt {key}; inspect saved output")
            attempt = {
                "caseId": case["id"], "side": side, "profile": profile,
                "orderPosition": position, "prompt": case["prompt"],
                "referenceStatus": case.get("referenceStatus"), "state": "start_intent_recorded",
                "startedAt": now(), "polls": [],
            }
            manifest["attempts"][key] = attempt
            write_json(manifest_path, manifest)
            execute_attempt(args, output, case, profile, attempt, manifest)
        summary = {
            "comparison": "tools", "evaluationId": manifest["evaluationId"],
            "referenceStatus": manifest["referenceStatus"], "selectedCaseIds": manifest["selectedCaseIds"],
            "metricsAreUsageAndRuntimeOnly": True,
            "attempts": [
                {"caseId": attempt["caseId"], "side": attempt["side"], "profile": attempt["profile"],
                 "remoteRunId": attempt.get("remoteRunId"), "state": attempt["state"],
                 "metrics": attempt.get("final", {}).get("metrics")}
                for attempt in manifest["attempts"].values()
            ],
        }
        write_json(output / "summary.json", summary)
        return {"dryRun": False, "directory": str(output), "summary": summary}
    finally:
        lock.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--suite", choices=("smoke", "quick", "full", "protected", "stability"), default="smoke")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="high")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--claude-bin", default="claude")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"Claude legal-competence evaluation stopped: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
