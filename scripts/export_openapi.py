"""Export the versioned FastAPI contract with deterministic JSON formatting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from labbridge.api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "contracts/openapi-v1.json"


def render_openapi() -> bytes:
    document = create_app().openapi()
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_openapi()
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != rendered:
            raise SystemExit(f"OpenAPI contract drift: regenerate {args.output}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rendered)


if __name__ == "__main__":
    main()
