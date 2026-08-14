#!/usr/bin/env python3
"""Build one canonical content graph per extracted EUR-Lex Formex document."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTRACTION_DIR = PROJECT_ROOT / "output" / "eurlex_formex_rl"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def build_one(task: Mapping[str, Any]) -> Dict[str, Any]:
    raw_path = Path(task["raw_path"])
    graph_path = Path(task["graph_path"])
    result = {
        "selection_index": task["selection_index"],
        "base_celex": task.get("base_celex"),
        "consolidated_celex": task.get("consolidated_celex"),
        "descriptor": task.get("descriptor"),
        "raw_path": task.get("raw_relative_path"),
        "graph_path": task.get("graph_relative_path"),
    }
    try:
        if graph_path.exists() and task.get("resume") and not task.get("force"):
            existing = json.loads(graph_path.read_text(encoding="utf-8"))
            if existing.get("phase") == "content_nodes":
                result.update({"status": "skipped_existing", **(existing.get("counts") or {})})
                result["output_bytes"] = graph_path.stat().st_size
                return result
        if graph_path.exists() and not task.get("force") and not task.get("resume"):
            raise FileExistsError("{} exists; use --resume or --force".format(graph_path))
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from extract_content_nodes import build_graph  # noqa: WPS433

        graph = build_graph(raw, str(raw_path))
        atomic_write_json(graph_path, graph)
        result.update({"status": "ok", **graph["counts"], "output_bytes": graph_path.stat().st_size})
    except BaseException as exc:
        result.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=20),
            }
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction-dir", type=Path, default=DEFAULT_EXTRACTION_DIR)
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = args.extraction_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    successful = [
        result
        for result in manifest.get("results") or []
        if result.get("status") in {"ok", "skipped_existing"}
    ]
    tasks = []
    for index, result in enumerate(successful):
        raw_relative = str(result["output_path"])
        raw_path = args.extraction_dir / raw_relative
        descriptor = str(result.get("descriptor") or "_")
        celex = str(result.get("consolidated_celex") or result.get("base_celex"))
        graph_relative = Path("content_graph") / descriptor / "{}_content_graph.json".format(celex)
        tasks.append(
            {
                "selection_index": index,
                "base_celex": result.get("base_celex"),
                "consolidated_celex": result.get("consolidated_celex"),
                "descriptor": descriptor,
                "raw_path": str(raw_path),
                "raw_relative_path": raw_relative,
                "graph_path": str(args.extraction_dir / graph_relative),
                "graph_relative_path": graph_relative.as_posix(),
                "resume": args.resume,
                "force": args.force,
            }
        )

    results = []
    if args.workers == 1:
        for task in tasks:
            results.append(build_one(task))
            print("[{}/{}] {} {}".format(len(results), len(tasks), results[-1]["status"], results[-1]["consolidated_celex"]), flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(build_one, task) for task in tasks]
            for future in as_completed(futures):
                results.append(future.result())
                print("[{}/{}] {} {}".format(len(results), len(tasks), results[-1]["status"], results[-1]["consolidated_celex"]), flush=True)
    results.sort(key=lambda item: int(item.get("selection_index") or 0))
    statuses = Counter(item.get("status") for item in results)
    graph_manifest = {
        "schema_version": "1.0",
        "phase": "content_nodes",
        "generated_at": utc_now(),
        "source_extraction_manifest": str(manifest_path),
        "summary": {
            "documents": len(results),
            "status_counts": dict(sorted(statuses.items())),
            "nodes": sum(int(item.get("nodes") or 0) for item in results if item.get("status") != "error"),
            "relationships": sum(int(item.get("relationships") or 0) for item in results if item.get("status") != "error"),
            "output_bytes": sum(int(item.get("output_bytes") or 0) for item in results if item.get("status") != "error"),
        },
        "results": results,
    }
    atomic_write_json(args.extraction_dir / "content_graph_manifest.json", graph_manifest)
    print(json.dumps(graph_manifest["summary"], ensure_ascii=False, indent=2))
    if statuses.get("error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
