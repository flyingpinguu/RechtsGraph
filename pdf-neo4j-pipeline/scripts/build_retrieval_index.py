#!/usr/bin/env python3
"""Build the derived SQLite retrieval index used by RechtsGraph MCP tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rechtsgraph_mcp.index import (  # noqa: E402
    DEFAULT_PDF_CHUNK_OVERLAP,
    DEFAULT_PDF_CHUNK_TOKENS,
    build_index,
)

REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_GII_MANIFEST = PROJECT_ROOT / "output" / "gii_xml_full_chunked" / "manifest.json"
DEFAULT_EURLEX_MANIFEST = PROJECT_ROOT / "output" / "eurlex_formex_rl" / "manifest.json"
DEFAULT_INDEX = PROJECT_ROOT / "output" / "retrieval" / "rechtsgraph.sqlite"
DEFAULT_EXTERNAL_REGISTER = REPO_ROOT / "external_documents" / "register.sqlite"
DEFAULT_EXTERNAL_ROOT = REPO_ROOT / "external_documents"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--gii-manifest", type=Path, default=DEFAULT_GII_MANIFEST)
    parser.add_argument("--eurlex-manifest", type=Path, default=DEFAULT_EURLEX_MANIFEST)
    parser.add_argument("--external-register", type=Path, default=DEFAULT_EXTERNAL_REGISTER)
    parser.add_argument("--external-root", type=Path, default=DEFAULT_EXTERNAL_ROOT)
    parser.add_argument("--external-document-id", action="append", default=[],
                        help="Explicit external register ID to ingest through the PDF fallback (repeatable).")
    parser.add_argument("--pdf-chunk-tokens", type=int, default=DEFAULT_PDF_CHUNK_TOKENS)
    parser.add_argument("--pdf-chunk-overlap", type=int, default=DEFAULT_PDF_CHUNK_OVERLAP)
    args = parser.parse_args()
    manifests = [path for path in (args.gii_manifest, args.eurlex_manifest) if path.is_file()]
    if not manifests:
        parser.error("no readable GII or EUR-Lex manifest supplied")
    stats = build_index(
        args.output, manifests, external_register_path=args.external_register,
        external_document_ids=args.external_document_id, external_root=args.external_root,
        pdf_chunk_tokens=args.pdf_chunk_tokens, pdf_chunk_overlap=args.pdf_chunk_overlap,
    )
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2, sort_keys=True))
    print("index: {}".format(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
