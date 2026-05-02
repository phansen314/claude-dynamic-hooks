"""Spec → impl drift guard.

Walks every example in `docs/openapi.yaml` and validates it against the
schema declared at the same node. If the spec adds/removes/renames a wire
field without the impl knowing, jsonschema flags the example as invalid.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _REPO_ROOT / "docs" / "openapi.yaml"


def _load_spec() -> dict:
    return yaml.safe_load(_SPEC.read_text())


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a `#/...` JSON pointer against the spec root."""
    if not ref.startswith("#/"):
        raise ValueError(f"only local refs supported; got {ref!r}")
    node: Any = spec
    for segment in ref[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        node = node[segment]
    return node


def _inline_refs(spec: dict, schema: Any, _seen: frozenset[str] = frozenset()) -> Any:
    """Return *schema* with all `$ref` entries resolved against *spec*.

    Cycles short-circuit by returning ``{}`` (any-schema) the second time a
    ref is encountered. CDHP spec is acyclic; guard is defense-in-depth.
    """
    if isinstance(schema, dict):
        if "$ref" in schema and isinstance(schema["$ref"], str):
            ref = schema["$ref"]
            if ref in _seen:
                return {}
            target = _resolve_ref(spec, ref)
            return _inline_refs(spec, target, _seen | {ref})
        return {k: _inline_refs(spec, v, _seen) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_inline_refs(spec, item, _seen) for item in schema]
    return schema


def _iter_examples(spec: dict) -> Iterator[tuple[str, dict, Any]]:
    """Yield (label, schema, example_value) for every example in paths."""
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue

            req = op.get("requestBody", {})
            for media_type, media in (req.get("content") or {}).items():
                schema = media.get("schema")
                if not schema:
                    continue
                for ex_name, ex in (media.get("examples") or {}).items():
                    yield (
                        f"{method.upper()} {path} request {media_type} example={ex_name}",
                        schema,
                        ex.get("value"),
                    )

            for status, resp in (op.get("responses") or {}).items():
                for media_type, media in (resp.get("content") or {}).items():
                    schema = media.get("schema")
                    if not schema:
                        continue
                    for ex_name, ex in (media.get("examples") or {}).items():
                        yield (
                            f"{method.upper()} {path} response {status} {media_type} example={ex_name}",
                            schema,
                            ex.get("value"),
                        )


def test_all_spec_examples_validate_against_schema():
    spec = _load_spec()
    failures: list[str] = []
    seen = 0
    for label, schema, value in _iter_examples(spec):
        seen += 1
        resolved = _inline_refs(spec, schema)
        try:
            validator = Draft202012Validator(resolved)
        except SchemaError as e:
            failures.append(f"{label}: invalid schema — {e.message}")
            continue
        errs = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
        if errs:
            joined = "; ".join(e.message for e in errs)
            failures.append(f"{label}: {joined}")
    assert seen > 0, "no examples found in openapi.yaml — walker broken?"
    if failures:
        pytest.fail("spec examples fail their schemas:\n  " + "\n  ".join(failures))
