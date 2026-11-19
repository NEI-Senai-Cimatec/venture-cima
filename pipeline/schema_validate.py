"""Validador minimo de JSON Schema (draft 2020-12) sem dependencias externas.

Cobre o subconjunto usado pelo contrato SDD: type (inclusive lista de tipos),
required, properties, enum, pattern, minLength, minItems, items, minimum,
maximum e format "uri" (checagem estrutural). Foi escrito para ser reutilizado
pelo Gate 2 e por qualquer auditor, sem instalar `jsonschema`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = ROOT / "contract" / "venture_capital_fund.schema.json"

_TYPE_MAP = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}

_URI_RE = re.compile(r"^https?://[^\s]+$")


def _is_type(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    py = _TYPE_MAP.get(expected)
    return py is not None and isinstance(value, py)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Retorna lista de erros (vazia = valido)."""
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(instance, t) for t in types):
            errors.append(f"{path}: tipo {type(instance).__name__} nao e {types}")
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: valor {instance!r} fora do enum {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: string menor que minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: string {instance!r} nao casa com {schema['pattern']!r}")
        if schema.get("format") == "uri" and not _URI_RE.match(instance):
            errors.append(f"{path}: {instance!r} nao e URI http(s) valida")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: valor {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: valor {instance} > maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: lista com {len(instance)} itens < minItems {schema['minItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{i}]"))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: campo obrigatorio ausente '{key}'")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{path}.{key}"))
    return errors


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_records(records: list[dict[str, Any]], schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Valida 100% dos registros e devolve relatorio agregado."""
    schema = schema or load_schema()
    failures: list[dict[str, Any]] = []
    for idx, rec in enumerate(records):
        errs = validate(rec, schema)
        if errs:
            failures.append({"index": idx, "fund_id": rec.get("fund_id"), "errors": errs[:8]})
    return {
        "total": len(records),
        "valid": len(records) - len(failures),
        "invalid": len(failures),
        "failures": failures[:50],
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Valida dataset contra o contrato SDD.")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    ap.add_argument("--data", required=True, help="JSON com {'records': [...]} ou lista")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    payload = json.loads(Path(args.data).read_text(encoding="utf-8"))
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    report = validate_records(records, load_schema(Path(args.schema)))
    print(json.dumps({k: v for k, v in report.items() if k != "failures"}, ensure_ascii=False))
    if report["failures"]:
        print(json.dumps(report["failures"][:5], ensure_ascii=False, indent=1))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if report["invalid"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
