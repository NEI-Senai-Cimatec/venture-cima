"""Coletor do cambio oficial BRL/USD (Banco Central do Brasil - PTAX).

Fonte: API OData publica e gratuita do BCB (Olinda), serie CotacaoDolarPeriodo.
Contrato de saida (data/interim/fx_brl_usd.json):
  {"source","source_url","pair","retrieved_at","series":[{"date","cotacao_compra","cotacao_venda"}],"last"}

Uso:
  python pipeline/collect_fx.py                 # 2024-01-01 .. hoje
  python pipeline/collect_fx.py --start 2024-01-01 --end 2026-11-18

A coleta usa exclusivamente pipeline/net.py (User-Agent academico declarado,
rate-limit, retry/backoff e cache auditavel em data/raw + data/manifest.jsonl).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from net import fetch  # noqa: E402

OUT_PATH = ROOT / "data" / "interim" / "fx_brl_usd.json"
SOURCES_PATH = ROOT / "data" / "interim" / "global_sources.json"

BASE_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/CotacaoDolarPeriodo"
    "(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
)


def bcb_date(iso: str) -> str:
    """BCB espera MM-DD-YYYY; aceitamos YYYY-MM-DD."""
    d = date.fromisoformat(iso)
    return d.strftime("%m-%d-%Y")


def build_url(start: str, end: str) -> str:
    return (
        BASE_URL
        + f"?@dataInicial='{bcb_date(start)}'&@dataFinalCotacao='{bcb_date(end)}'"
        + "&$format=json&$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao"
    )


KNOWN_LIMITATION_PREFIXES = ("[SEC Form D]", "[Wikidata]", "[BCB PTAX]")


def update_sources(entry: dict, limitations: list[str]) -> None:
    """Atualiza (upsert) a secao deste coletor em data/interim/global_sources.json."""
    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = {"sources": [], "wikidata_qid_used": {}, "limitations": []}
    if SOURCES_PATH.exists():
        try:
            doc.update(json.loads(SOURCES_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    sources = [s for s in doc.get("sources", []) if s.get("name") != entry["name"]]
    sources.append(entry)
    doc["sources"] = sources
    # Cada coletor e dono das suas limitacoes (prefixo "[BCB PTAX]"); remove texto legado/obsoleto.
    merged = [
        l for l in (doc.get("limitations") or [])
        if str(l).startswith(KNOWN_LIMITATION_PREFIXES) and not str(l).startswith("[BCB PTAX]")
    ]
    merged.extend(limitations)
    doc["limitations"] = merged
    SOURCES_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Coleta PTAX (venda BRL/USD) do BCB.")
    parser.add_argument("--start", default="2024-01-01", help="data inicial ISO (YYYY-MM-DD)")
    parser.add_argument("--end", default=date.today().isoformat(), help="data final ISO (YYYY-MM-DD)")
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--force", action="store_true", help="ignora cache do net.py")
    args = parser.parse_args()

    url = build_url(args.start, args.end)
    fetched = fetch(url, source="bcb_ptax", force=args.force)
    payload = fetched.json()
    values = payload.get("value") or []
    if not values:
        raise SystemExit(f"PTAX vazio para {args.start}..{args.end}: {url}")

    series = []
    for row in values:
        stamp = str(row.get("dataHoraCotacao") or "")
        if not stamp:
            continue
        series.append(
            {
                "date": stamp[:10],
                "cotacao_compra": row.get("cotacaoCompra"),
                "cotacao_venda": row.get("cotacaoVenda"),
            }
        )
    series.sort(key=lambda r: r["date"])
    # Deduplica por data (o BCB pode publicar boletins extras no mesmo dia).
    dedup = {}
    for row in series:
        dedup[row["date"]] = row
    series = [dedup[d] for d in sorted(dedup)]
    if not series:
        raise SystemExit("PTAX sem datas validas apos normalizacao.")

    doc = {
        "source": "BCB PTAX",
        "source_url": url,
        "pair": "BRL/USD",
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "series": series,
        "last": {"date": series[-1]["date"], "cotacao_venda": series[-1]["cotacao_venda"]},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    first, last = series[0], series[-1]
    print(f"[fx] {len(series)} cotacoes PTAX {first['date']}..{last['date']} -> {out}")
    limitations = [
        "[BCB PTAX] A API publica serie apenas dias uteis com boletim PTAX; feriados/fins de semana "
        "nao possuem cotacao. O campo last e a cotacao de venda mais recente disponivel na data de coleta.",
        "[BCB PTAX] cotacao_compra/cotacao_venda preservadas exatamente como publicadas (sem arredondamento).",
    ]
    update_sources(
        {
            "name": "BCB PTAX - CotacaoDolarPeriodo (BRL/USD)",
            "url": url,
            "status": fetched.status,
            "bytes": len(fetched.content),
            "sha256": hashlib.sha256(fetched.content).hexdigest(),
            "records": len(series),
            "notes": (
                f"Serie de venda PTAX {first['date']}..{last['date']}, "
                f"{len(series)} cotacoes; campo last = cotacao_venda {last['cotacao_venda']} em {last['date']}."
            ),
        },
        limitations,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
