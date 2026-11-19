"""Orquestrador do pipeline completo (Loop Engineering da secao 2 da spec).

Fases: INGEST -> TRANSFORM -> RENDER -> AUDIT, com os 4 Quality Gates no final.
Cada fase delega para o script correspondente; a execucao para no primeiro erro.

Uso:
  python pipeline/run_all.py                # pipeline completo + gates
  python pipeline/run_all.py --skip-ingest  # reaproveita data/interim e data/raw
  python pipeline/run_all.py --no-gates     # so gera dataset + dashboard
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(step: str, args: list[str]) -> tuple[bool, float]:
    started = time.time()
    print(f"\n=== [{step}] {' '.join(args)}", flush=True)
    proc = subprocess.run([PY, *args], cwd=ROOT)
    elapsed = time.time() - started
    ok = proc.returncode == 0
    print(f"=== [{step}] {'OK' if ok else 'FALHOU'} em {elapsed:.1f}s (exit {proc.returncode})", flush=True)
    return ok, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description="Pipeline ETL+render de VC com Quality Gates.")
    ap.add_argument("--skip-ingest", action="store_true", help="nao recoleta; usa data/interim existente")
    ap.add_argument("--no-gates", action="store_true", help="nao roda os 4 quality gates")
    ap.add_argument("--offline", action="store_true", help="exige cache de rede (VC_OFFLINE=1)")
    args = ap.parse_args()

    if args.offline:
        import os

        os.environ["VC_OFFLINE"] = "1"

    timings: dict[str, float] = {}
    steps: list[tuple[str, list[str]]] = []

    if not args.skip_ingest:
        steps += [
            ("INGEST-BR", ["pipeline/collect_br.py"]),
            ("INGEST-GLOBAL-SEC", ["pipeline/collect_sec.py"]),
            ("INGEST-GLOBAL-WIKIDATA", ["pipeline/collect_wikidata.py"]),
            ("INGEST-FX", ["pipeline/collect_fx.py"]),
            ("INGEST-DIRECTORIES", ["pipeline/collect_directories.py"]),
        ]
    steps += [
        ("TRANSFORM", ["pipeline/transform.py", "--validate"]),
        ("RENDER", ["pipeline/render.py", "--input", "data/canonical/funds.json",
                    "--kpis", "data/canonical/kpis.json", "--out", "dashboard/index.html"]),
    ]
    if not args.no_gates:
        steps.append(("AUDIT-GATES", ["pipeline/gates.py", "--all",
                                      "--json", "audit/audit_report.json"]))

    failures: list[str] = []
    for step, cmd in steps:
        if not (ROOT / cmd[0]).exists():
            print(f"=== [{step}] PULADO: {cmd[0]} ainda nao existe")
            failures.append(step)
            continue
        ok, elapsed = run(step, cmd)
        timings[step] = round(elapsed, 2)
        if not ok:
            failures.append(step)
            if step.startswith("INGEST"):
                continue  # segue para as proximas fontes; transform decide com o que houver
            if step in {"TRANSFORM", "AUDIT-GATES"}:
                break

    print("\n=== RESUMO ===")
    print(json.dumps({"timings_s": timings, "failures": failures}, ensure_ascii=False, indent=1))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
