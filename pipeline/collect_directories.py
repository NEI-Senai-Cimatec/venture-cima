"""Coletor de diretorios abertos de tese e atividade de deals (Fase 1 do pipeline VC).

Escopo desta etapa (ver spec_agente_mapeamento_vc.md, secao 1.1/1.3):
  * Shizune.co - tabelas Markdown publicas (``<chart>/table.md``) de charts de
    investidores. Delas extraimos gestoras, geografias de atuacao, estagios
    declarados (tese) e foco setorial. Fonte primaria para
    ``stage_classification_method = DECLARED_THESIS`` e para o indice cross-border.
  * OpenVC - tentativa de acesso registrada (2 URLs x 2 User-Agents). O site
    responde HTTP 403 (Cloudflare); nenhum dado OpenVC e inventado. O fallback
    aberto acionado sao os proprios charts de pais do Shizune.
  * Brouky - pagina aberta de dados (HTML Next.js). Nao existe export CSV/JSON
    parseavel; extraimos apenas os agregados textuais renderizados (evidencia),
    sem transforma-los em registros de gestora.
  * Benchmarks - ABVCAP "Consolidacao de Dados 2o Trimestre 2026" (+ PDF aberto)
    e LAVCA "2025 Latin American Startup Ecosystem Insights". Capturamos
    titulo/URL/trecho citavel; nenhum total e inventado (ABVCAP so publica os
    numeros em graficos do PDF; LAVCA nao expoe agregados no HTML).

Saidas (escopo de escrita exclusivo da task-3):
  * data/interim/shizune_firms.jsonl           (envelope contract/interim_record.schema.json)
  * data/interim/directories_availability.json (auditoria de disponibilidade/fallback/benchmark)

Contrato de payload do JSONL CONGELADO pelo Lead (uma linha por linha de tabela):
    investor_name, relevant_deals, geographies, geography_includes_brazil,
    listed_in_brazil_dataset, stage_raw, declared_stages, stage_unmapped,
    focus_sectors, table_url, table_title

O script e idempotente: reexecucoes com o mesmo cache produzem o mesmo JSONL.
Toda rede passa por pipeline/net.py (UA academico declarado, rate-limit >= 0.3 s,
retry/backoff, cache em data/raw e registro em data/manifest.jsonl).

Uso:
    python pipeline/collect_directories.py
    python pipeline/collect_directories.py --refresh        # forca re-download
    python pipeline/collect_directories.py --offline        # apenas cache (replay)
    python pipeline/collect_directories.py --out-dir data/interim
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sys
import unicodedata
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PIPELINE_DIR = Path(__file__).resolve().parent
ROOT = PIPELINE_DIR.parent
if str(PIPELINE_DIR) not in sys.path:  # permite `python pipeline/collect_directories.py`
    sys.path.insert(0, str(PIPELINE_DIR))

from net import ACADEMIC_UA, FetchError, fetch  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SHIZUNE_BASE = "https://shizune.co"
SHIZUNE_SITEMAP_CHARTS = f"{SHIZUNE_BASE}/sitemap-charts.xml"
DEFAULT_OUT_DIR = ROOT / "data" / "interim"

OPENVC_URLS = [
    ("https://www.openvc.app/investor-database", "investor_database"),
    ("https://www.openvc.app/blog/vc-list", "vc_list_blog"),
]
BROUKY_DATA_URL = "https://brouky.tech/data"
BROUKY_BRAZIL_URL = "https://brouky.tech/data/vc-activity-2026/brazil"
ABVCAP_BENCHMARK_URL = "https://abvcap.com.br/publicacao/consolidacao-de-dados-2o-trimestre-2026/"
LAVCA_BENCHMARK_URL = "https://www.lavca.org/research/2025-latin-american-startup-ecosystem-insights/"

# Enum canonico do contrato (spec 1.2). A ordenacao e a usada na saida.
STAGE_ENUM_ORDER = [
    "PRE_SEED",
    "SEED",
    "SERIES_A",
    "SERIES_B",
    "LATE_STAGE_GROWTH",
    "VENTURE_DEBT",
]
# Mapeamento DECLARADO -> enum. Regra explicita (docs/sources_directories.md):
# "Series C" e a declaracao de estagio tardio/growth na fonte; mapeamos para
# LATE_STAGE_GROWTH, a unica faixa compativel do enum. Nada fora desta tabela e
# inferido: tokens como "Venture - Series Unknown", "Angel", "Grant",
# "Convertible Note", "Private Equity", "Debt Financing" e "Equity Crowdfunding"
# vao para payload.stage_unmapped.
STAGE_MAP = {
    "Pre-Seed": "PRE_SEED",
    "Seed": "SEED",
    "Series A": "SERIES_A",
    "Series B": "SERIES_B",
    "Series C": "LATE_STAGE_GROWTH",
}
EXPECTED_COLUMNS = ["Investor", "Relevant Deals", "Geography", "Stage", "Focus"]
PAYLOAD_CONTRACT = [
    "investor_name",
    "relevant_deals",
    "geographies",
    "geography_includes_brazil",
    "listed_in_brazil_dataset",
    "stage_raw",
    "declared_stages",
    "stage_unmapped",
    "focus_sectors",
    "table_url",
    "table_title",
]

# Charts Shizune cujo table.md foi validado (HTTP 200 + tabela Markdown real) e
# que entram no dataset de gestoras. A ordem define a prioridade de leitura.
INGEST_TABLES: list[dict[str, str]] = [
    {
        "path": "/investors/venture-capital-vc-funds-brazil",
        "chart_scope": "brazil",
        "role": "canonical_brazil",
        "note": "Tabela citada na spec (Top 35 VC funds in Brazil).",
    },
    {
        "path": "/investors/vc-funds-brazil",
        "chart_scope": "brazil",
        "role": "brazil_generic",
        "note": "Top 50 VC funds in Brazil (recorte generico).",
    },
    {
        "path": "/investors/venture-capital-vc-funds",
        "chart_scope": "global",
        "role": "global_generic",
        "note": "Top 50 VC funds (sem recorte de pais; titulo com geografia vazia).",
    },
    {
        "path": "/investors/venture-capital-vc-funds-latam",
        "chart_scope": "latam",
        "role": "latam_generic",
        "note": "Top 50 VC funds in LATAM.",
    },
    {
        "path": "/investors/vc-funds-latam",
        "chart_scope": "latam",
        "role": "latam_generic",
        "note": "Top 50 VC funds in LATAM (recorte generico).",
    },
    {
        "path": "/investors/venture-capital-vc-funds-united-states",
        "chart_scope": "united_states",
        "role": "us_generic",
        "note": "Top 50 VC funds in United States.",
    },
    {
        "path": "/investors/vc-funds-united-states",
        "chart_scope": "united_states",
        "role": "us_generic",
        "note": "Top 50 VC funds in United States (recorte generico).",
    },
    {
        "path": "/investors/venture-capital-vc-funds-europe",
        "chart_scope": "europe",
        "role": "europe_generic",
        "note": "Top 50 VC funds in Europe.",
    },
    {
        "path": "/investors/vc-funds-europe",
        "chart_scope": "europe",
        "role": "europe_generic",
        "note": "Top 50 VC funds in Europe (recorte generico).",
    },
]

# Tabelas que respondem 200, mas foram deliberadamente NAO ingeridas no dataset de
# gestoras (contem majoritariamente anjos/individuos). Ficam registradas na
# auditoria de disponibilidade para transparencia e reprodutibilidade.
WORKING_EXCLUDED_TABLES: list[dict[str, str]] = [
    {
        "path": "/investors/investors-brazil",
        "chart_scope": "brazil",
        "reason": "Chart 'startup investors' mistura anjos/individuos e organizacoes; nao e recorte de gestoras.",
    },
    {
        "path": "/investors/angel-investors-brazil",
        "chart_scope": "brazil",
        "reason": "Chart de investidores-anjo (pessoas fisicas); fora do conceito de gestora/fundo.",
    },
    {
        "path": "/investors/venture-capital-investors",
        "chart_scope": "global",
        "reason": "Chart de 'investidores' (inclui anjos e individuos), nao o recorte de VC funds.",
    },
]

# URLs candidatas testadas que NAO existem (404). Registradas como evidencia de
# exploracao, incluindo as URLs de estagio sugeridas no briefing da task.
EXPECTED_404_PROBES: list[dict[str, str]] = [
    {"path": "/investors/vc-funds", "why": "URL de referencia que retorna 404 no briefing do Lead."},
    {"path": "/investors/pre-seed-investors-brazil", "why": "chart por estagio sugerido no briefing."},
    {"path": "/investors/seed-investors-brazil", "why": "chart por estagio sugerido no briefing."},
    {"path": "/investors/series-a-investors-brazil", "why": "chart por estagio sugerido no briefing."},
    {"path": "/investors/series-b-investors-brazil", "why": "chart por estagio sugerido no briefing."},
    {"path": "/investors/venture-debt-investors-brazil", "why": "chart por estagio sugerido no briefing."},
]


# --------------------------------------------------------------------------- #
# Utilitarios
# --------------------------------------------------------------------------- #


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(name: str) -> str:
    """Slug ASCII deterministico para o record_id (nomes preservados a parte)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
    if not slug:
        slug = "x" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return slug


def split_tokens(cell: str) -> list[str]:
    """Separa valores separados por virgula preservando o texto original (trim)."""
    return [tok.strip() for tok in (cell or "").split(",") if tok.strip()]


def parse_int(cell: str) -> int | None:
    text = (cell or "").strip().replace(",", "")
    return int(text) if text.isdigit() else None


def strip_html(text: str) -> str:
    """Remove tags/scripts e normaliza espacos para extracao textual citavel."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _title_tag(text: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    return strip_html(match.group(1)) if match else None


def _meta_description(text: str) -> str | None:
    match = re.search(
        r'<meta[^>]+(?:name|property)="(?:description|og:description)"[^>]+content="([^"]*)"',
        text,
        re.I,
    )
    return htmllib.unescape(match.group(1)).strip() if match else None


# --------------------------------------------------------------------------- #
# Rede auditavel (sempre via pipeline/net.py)
# --------------------------------------------------------------------------- #


def fetch_result(
    url: str,
    *,
    source: str,
    retries: int = 1,
    timeout: int = 90,
    force: bool = False,
    user_agent: str = ACADEMIC_UA,
    decode_text: bool = True,
) -> dict[str, Any]:
    """Executa um fetch via net.py e devolve um dicionario serializavel para auditoria."""
    result: dict[str, Any] = {
        "url": url,
        "ok": False,
        "status": None,
        "bytes": 0,
        "sha256": None,
        "encoding": None,
        "from_cache": None,
        "error": None,
        "text": None,
        "content": None,
    }
    try:
        fetched = fetch(
            url,
            source=source,
            user_agent=user_agent,
            retries=retries,
            timeout=timeout,
            force=force,
        )
    except FetchError as exc:
        message = str(exc)
        result["error"] = message
        status_match = re.search(r"HTTP (\d{3})", message)
        if status_match:
            result["status"] = int(status_match.group(1))
        elif "VC_OFFLINE" in message:
            result["status"] = "OFFLINE_NO_CACHE"
        return result

    content = fetched.content
    result.update(
        ok=True,
        status=fetched.status,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        from_cache=fetched.from_cache,
        content=content,
    )
    if decode_text:
        try:
            result["text"] = content.decode("utf-8")
            result["encoding"] = "utf-8"
        except UnicodeDecodeError:
            result["text"] = content.decode("latin1")
            result["encoding"] = "latin1"
    return result


# --------------------------------------------------------------------------- #
# Parsing das tabelas Markdown do Shizune
# --------------------------------------------------------------------------- #


def parse_markdown_table(text: str) -> dict[str, Any]:
    """Interpreta ``<chart>/table.md`` (titulo + tabela pipe + rodape institucional)."""
    title: str | None = None
    separator = re.compile(r"^:?-{2,}:?$")
    columns: list[str] | None = None
    rows: list[dict[str, str]] = []
    parse_errors: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if rows:
                break  # fim da tabela: o arquivo segue com "# About Shizune"
            continue
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
            continue
        if not line.startswith("|"):
            if columns is not None and rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if columns is None:
            columns = cells
            continue
        if all(separator.fullmatch(cell) for cell in cells):
            continue
        if len(cells) != len(columns):
            parse_errors.append(line[:200])
            continue
        rows.append(dict(zip(columns, cells)))

    expected_rows = None
    if title:
        title_match = re.search(r"Top\s+(\d+)", title)
        if title_match:
            expected_rows = int(title_match.group(1))
    return {
        "title": title,
        "columns": columns,
        "rows": rows,
        "expected_rows": expected_rows,
        "parse_errors": parse_errors,
    }


def map_declared_stages(raw_tokens: list[str]) -> tuple[list[str], list[str]]:
    """Mapeia apenas estagios realmente declarados; devolve (enum, nao-mapeados)."""
    mapped: list[str] = []
    unmapped: list[str] = []
    for token in raw_tokens:
        enum_value = STAGE_MAP.get(token)
        if enum_value is not None:
            if enum_value not in mapped:
                mapped.append(enum_value)
        elif token not in unmapped:
            unmapped.append(token)
    return [stage for stage in STAGE_ENUM_ORDER if stage in mapped], unmapped


# --------------------------------------------------------------------------- #
# Coleta das tabelas e construcao do dataset de gestoras
# --------------------------------------------------------------------------- #


def collect_shizune_tables(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Baixa (ou le do cache) todas as tabelas/probes do Shizune e parseia as tabelas."""
    results: dict[str, dict[str, Any]] = {}
    probes: list[tuple[dict[str, str], int]] = []
    for spec in INGEST_TABLES:
        probes.append((spec, 3))
    for spec in WORKING_EXCLUDED_TABLES:
        probes.append((spec, 1))
    for spec in EXPECTED_404_PROBES:
        probes.append((spec, 1))

    for spec, retries in probes:
        path = spec["path"]
        url = f"{SHIZUNE_BASE}{path}/table.md"
        result = fetch_result(
            url,
            source="shizune_brazil",
            retries=retries,
            timeout=90,
            force=args.refresh,
        )
        if result["ok"] and result["text"] is not None:
            result.update(parse_markdown_table(result["text"]))
        results[path] = result
    return results


def build_firm_records(
    table_results: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Gera uma linha de contrato por linha de tabela (contrato de payload congelado).

    A regra ``listed_in_brazil_dataset`` e ESTRUTURAL: verdadeira para linhas
    provenientes de charts com recorte de pais Brazil (por definicao, charts de
    fundos/gestoras ativos no Brasil), inclusive quando a coluna Geography nao
    lista 'Brazil' (ex.: NFX). Nunca inferimos um campo a partir do outro.
    """
    records: list[dict[str, Any]] = []
    pair_counter: Counter[tuple[str, str]] = Counter()

    for spec in INGEST_TABLES:
        result = table_results[spec["path"]]
        table_url = result["url"]
        table_title = result.get("title") or ""
        listed_in_brazil = spec["chart_scope"] == "brazil"
        table_slug = spec["path"].rsplit("/", 1)[-1]
        source_id = "shizune_brazil" if listed_in_brazil else "shizune_global"

        for row in result["rows"]:
            name = row["Investor"].strip()
            if not name:
                continue
            pair_counter[(name, spec["path"])] += 1
            occurrence = pair_counter[(name, spec["path"])]
            record_id = f"shizune:{slugify(name)}:{slugify(table_slug)}"
            if occurrence > 1:
                record_id = f"{record_id}-{occurrence}"

            geographies = split_tokens(row["Geography"])
            stage_raw = split_tokens(row["Stage"])
            declared_stages, stage_unmapped = map_declared_stages(stage_raw)

            payload = {
                "investor_name": name,
                "relevant_deals": parse_int(row["Relevant Deals"]),
                "geographies": geographies,
                "geography_includes_brazil": "Brazil" in geographies,
                "listed_in_brazil_dataset": listed_in_brazil,
                "stage_raw": stage_raw,
                "declared_stages": declared_stages,
                "stage_unmapped": stage_unmapped,
                "focus_sectors": split_tokens(row["Focus"]),
                "table_url": table_url,
                "table_title": table_title,
            }
            records.append(
                {
                    "record_id": record_id,
                    "source_id": source_id,
                    "source_url": table_url,
                    "entity_kind": "firm",
                    "payload": payload,
                }
            )

    records.sort(
        key=lambda rec: (
            rec["source_id"],
            rec["payload"]["investor_name"].casefold(),
            rec["payload"]["table_url"],
            rec["record_id"],
        )
    )

    # Estatisticas: nivel registro e nivel gestora (nome exato).
    firms: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        firms.setdefault(record["payload"]["investor_name"], []).append(record)

    stage_counter_record: Counter[str] = Counter()
    stage_counter_firm: Counter[str] = Counter()
    unmapped_counter: Counter[str] = Counter()
    geo_len_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    br_chart_without_br_geo: list[str] = []
    field_conflicts: list[dict[str, Any]] = []
    consistent_multi = 0

    for record in records:
        payload = record["payload"]
        source_counter[record["source_id"]] += 1
        for stage in payload["declared_stages"]:
            stage_counter_record[stage] += 1
        for token in payload["stage_unmapped"]:
            unmapped_counter[token] += 1
        geo_len_counter[str(len(payload["geographies"]))] += 1
        if payload["listed_in_brazil_dataset"] and not payload["geography_includes_brazil"]:
            br_chart_without_br_geo.append(payload["investor_name"])

    multi_table_firms = 0
    for name, firm_records in firms.items():
        firm_stages: set[str] = set()
        for record in firm_records:
            firm_stages.update(record["payload"]["declared_stages"])
        for stage in firm_stages:
            stage_counter_firm[stage] += 1
        if len(firm_records) > 1:
            multi_table_firms += 1
            variants = {
                (
                    tuple(record["payload"]["stage_raw"]),
                    tuple(record["payload"]["geographies"]),
                    tuple(record["payload"]["focus_sectors"]),
                )
                for record in firm_records
            }
            if len(variants) == 1:
                consistent_multi += 1
            else:
                field_conflicts.append(
                    {
                        "investor_name": name,
                        "tables": [record["payload"]["table_url"] for record in firm_records],
                    }
                )

    stats: dict[str, Any] = {
        "tables_ingested": len(INGEST_TABLES),
        "table_rows": sum(len(table_results[spec["path"]]["rows"]) for spec in INGEST_TABLES),
        "records": len(records),
        "unique_firms": len(firms),
        "unique_firms_with_mapped_stage": sum(
            1 for records_ in firms.values() if any(r["payload"]["declared_stages"] for r in records_)
        ),
        "unique_firms_without_mapped_stage": sum(
            1
            for records_ in firms.values()
            if not any(r["payload"]["declared_stages"] for r in records_)
        ),
        "unique_firms_geography_includes_brazil": sum(
            1
            for records_ in firms.values()
            if any(r["payload"]["geography_includes_brazil"] for r in records_)
        ),
        "unique_firms_listed_in_brazil_dataset": sum(
            1
            for records_ in firms.values()
            if any(r["payload"]["listed_in_brazil_dataset"] for r in records_)
        ),
        "records_with_mapped_stage": sum(1 for r in records if r["payload"]["declared_stages"]),
        "records_geography_includes_brazil": sum(
            1 for r in records if r["payload"]["geography_includes_brazil"]
        ),
        "records_listed_in_brazil_dataset": sum(
            1 for r in records if r["payload"]["listed_in_brazil_dataset"]
        ),
        "records_brazil_chart_without_brazil_geography": len(br_chart_without_br_geo),
        "brazil_chart_without_brazil_geography_examples": br_chart_without_br_geo[:20],
        "source_id_counts": dict(source_counter),
        "declared_stage_distribution_records": dict(stage_counter_record.most_common()),
        "declared_stage_distribution_firms": dict(stage_counter_firm.most_common()),
        "non_enum_stage_tokens": dict(unmapped_counter.most_common()),
        "multi_table_firms": multi_table_firms,
        "multi_table_firms_consistent_fields": consistent_multi,
        "multi_table_firms_with_field_conflicts": len(field_conflicts),
        "field_conflict_examples": field_conflicts[:10],
        "geography_tokens_per_row": dict(sorted(geo_len_counter.items())),
        "table_urls": [table_results[spec["path"]]["url"] for spec in INGEST_TABLES],
    }
    return records, stats


# --------------------------------------------------------------------------- #
# Descoberta no sitemap do Shizune
# --------------------------------------------------------------------------- #


def collect_shizune_discovery(args: argparse.Namespace) -> dict[str, Any]:
    result = fetch_result(
        SHIZUNE_SITEMAP_CHARTS,
        source="shizune_brazil",
        retries=2,
        timeout=120,
        force=args.refresh,
    )
    if not result["ok"]:
        return {"ok": False, "url": SHIZUNE_SITEMAP_CHARTS, "error": result["error"], "status": result["status"]}

    paths = re.findall(r"<loc>https://shizune\.co(/investors/[^<]*)</loc>", result["text"] or "")
    stage_specific = [
        path
        for path in paths
        if re.search(r"-(pre-seed|seed|series-[a-z]|venture-debt|late-stage)(-|$)", path)
    ]
    sector_brazil = sorted(path for path in paths if path.endswith("-vc-funds-brazil"))
    path_set = set(paths)
    ingested_present = {spec["path"]: spec["path"] in path_set for spec in INGEST_TABLES}
    return {
        "ok": True,
        "url": SHIZUNE_SITEMAP_CHARTS,
        "status": result["status"],
        "bytes": result["bytes"],
        "sha256": result["sha256"],
        "charts_total": len(paths),
        "stage_specific_charts_total": len(stage_specific),
        "stage_specific_charts_examples": stage_specific[:10],
        "sector_brazil_vc_fund_charts_total": len(sector_brazil),
        "sector_brazil_vc_fund_charts_examples": sector_brazil[:10],
        "ingested_paths_present_in_sitemap": ingested_present,
        "note": (
            "O Shizune nao publica charts por estagio (0 slugs). O estagio e o campo "
            "'Stage' dentro de cada chart; charts setoriais '-vc-funds-brazil' existem "
            "(superficie de expansao futura, nao ingeridos nesta etapa)."
        ),
    }


# --------------------------------------------------------------------------- #
# Fontes bloqueadas / fallback / benchmark
# --------------------------------------------------------------------------- #


def collect_openvc(args: argparse.Namespace) -> dict[str, Any]:
    """Tenta OpenVC com UA academico e de navegador; registra o bloqueio sem inventar dados."""
    attempts: list[dict[str, Any]] = []
    for url, kind in OPENVC_URLS:
        for ua_kind, user_agent in (("academic", ACADEMIC_UA), ("browser", BROWSER_UA)):
            result = fetch_result(
                url,
                source="openvc",
                retries=1,
                timeout=60,
                force=args.refresh,
                user_agent=user_agent,
                decode_text=False,
            )
            attempts.append(
                {
                    "url": url,
                    "page_kind": kind,
                    "user_agent_kind": ua_kind,
                    "user_agent_value": user_agent,
                    "ok": result["ok"],
                    "status": result["status"],
                    "bytes": result["bytes"],
                    "error": result["error"],
                }
            )
    blocked = all(not attempt["ok"] for attempt in attempts)
    return {
        "status": "BLOCKED" if blocked else "PARTIAL",
        "available": not blocked,
        "method": "HTTP GET com User-Agent academico declarado e, como retry, User-Agent de navegador.",
        "attempts": attempts,
        "blocked_by": "Cloudflare (HTTP 403). O Lead verificou corpo 'Just a moment...'.",
        "records_collected": 0,
        "fallback": {
            "fallback_type": "open_dataset_substitution",
            "replacement_source": "shizune_country_cuts",
            "replacement_urls": [f"{SHIZUNE_BASE}{spec['path']}/table.md" for spec in INGEST_TABLES],
            "reason": (
                "OpenVC nao entregou dados (403). Usamos os charts abertos de pais/tipo "
                "do Shizune como fonte de tese declarada, sem qualquer registro sintetico."
            ),
        },
    }


def _brouky_extract(text: str) -> dict[str, Any]:
    """Extrai apenas agregados textuais renderizados na pagina do Brouky (evidencia)."""
    plain = strip_html(text)
    extracted: dict[str, Any] = {
        "headline": None,
        "sectors": [],
        "stage_focus": [],
        "most_active_investors": [],
    }
    match = re.search(r"Brouky tracks ([\d,]+) investors and ([\d,]+) startups in Brazil", plain)
    if match:
        extracted["headline"] = {
            "investors_tracked": int(match.group(1).replace(",", "")),
            "startups_tracked": int(match.group(2).replace(",", "")),
            "geography": "Brazil",
        }

    sector_header = "Top Investor Sectors in Brazil"
    stage_header = "Investment Stage Focus in Brazil"
    active_header = "Most Active Investors in Brazil"
    sectors_start = plain.find(sector_header)
    stage_start = plain.find(stage_header)
    active_start = plain.find(active_header)

    if sectors_start >= 0 and stage_start > sectors_start:
        segment = plain[sectors_start + len(sector_header) : stage_start]
        for item in re.finditer(r"([A-Z][A-Za-z &,.\-]{2,55}?)\s+(\d+)\s+investors", segment):
            extracted["sectors"].append(
                {"sector": item.group(1).strip(), "investors": int(item.group(2))}
            )
        extracted["sectors"] = extracted["sectors"][:12]

    if stage_start >= 0 and active_start > stage_start:
        segment = plain[stage_start + len(stage_header) : active_start]
        for item in re.finditer(r"([a-z][a-z\-]{2,20})\s+(\d+)\s+investors", segment):
            extracted["stage_focus"].append(
                {"stage": item.group(1), "investors": int(item.group(2))}
            )
        extracted["stage_focus"] = extracted["stage_focus"][:12]

    if active_start >= 0:
        segment = plain[active_start + len(active_header) : active_start + 6000]
        pattern = re.compile(
            r"((?:[A-Z0-9][\w&.\-'+]*)(?:\s+[A-Z0-9][\w&.\-'+]*)*)"
            r"\s*·\s*([^·]{1,80}?)\s*·\s*([^·]{0,160}?)\s+(\d+)\s+investments"
        )
        for item in pattern.finditer(segment):
            extracted["most_active_investors"].append(
                {
                    "investor": item.group(1).strip(),
                    "deals": int(item.group(4)),
                    "sectors": [item.group(2).strip(), item.group(3).strip()],
                }
            )
        extracted["most_active_investors"] = extracted["most_active_investors"][:15]
    return extracted


def collect_brouky(args: argparse.Namespace) -> dict[str, Any]:
    data_page = fetch_result(BROUKY_DATA_URL, source="brouky", retries=2, timeout=90, force=args.refresh)
    brazil_page = fetch_result(BROUKY_BRAZIL_URL, source="brouky", retries=2, timeout=90, force=args.refresh)
    result: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "available": bool(data_page["ok"] or brazil_page["ok"]),
        "method": "HTTP GET da pagina aberta; extracao textual dos agregados renderizados.",
        "open_export": {"csv": False, "json": False, "next_data": False},
        "pages": [],
        "extracted_evidence": {},
        "not_ingested_reason": (
            "Nao existe export CSV/JSON publico parseavel; os numeros estao embutidos no HTML "
            "(self.__next_f) apenas como agregados. Nao convertemos agregados em registros de gestora."
        ),
    }
    for label, page in (("data_index", data_page), ("brazil_activity_2026", brazil_page)):
        page_info = {
            "url": page["url"],
            "status": page["status"],
            "ok": page["ok"],
            "bytes": page["bytes"],
            "sha256": page["sha256"],
            "error": page["error"],
        }
        if page["text"] is not None:
            text = page["text"]
            page_info["markers"] = {
                "__NEXT_DATA__": text.count("__NEXT_DATA__"),
                "self.__next_f": text.count("self.__next_f"),
                "csv_mentions": text.lower().count("csv"),
                "download_mentions": text.lower().count("download"),
            }
            links = re.findall(r'href="([^"]+)"', text)
            page_info["export_links_found"] = [
                link
                for link in links
                if any(key in link.lower() for key in ("csv", "json", "download", "dataset"))
            ][:20]
        result["pages"].append(page_info)

    if brazil_page["ok"] and brazil_page["text"] is not None:
        result["extracted_evidence"] = _brouky_extract(brazil_page["text"])
        result["status"] = "AVAILABLE_PAGE_ONLY"
    if result["extracted_evidence"]:
        result["open_export"]["next_data"] = (
            result["pages"][-1].get("markers", {}).get("__NEXT_DATA__", 0) > 0
        )
    return result


def _pdf_text_of_stream(data: bytes) -> str:
    parts: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^\\()])*\)", data):
        text = match.group(0)[1:-1].decode("latin1", "replace")
        text = re.sub(r"\\([0-7]{1,3})", lambda m: chr(int(m.group(1), 8)), text)
        text = text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
        parts.append(text)
    return " ".join(parts)


def extract_pdf_evidence(raw: bytes) -> dict[str, Any]:
    """Extracao stdlib (zlib) do texto do PDF: numero de paginas e trecho da secao VC."""
    streams = re.findall(rb"stream\r?\n(.*?)endstream", raw, re.S)
    pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    text_docs: list[str] = []
    for stream in streams:
        try:
            decompressed = zlib.decompress(stream.strip(b"\r\n"))
        except Exception:  # stream sem FlateDecode (imagem/fonte) — ignorado
            continue
        if b"Tj" in decompressed or b"TJ" in decompressed:
            text_docs.append(_pdf_text_of_stream(decompressed))

    overview_excerpt = None
    for doc in text_docs:
        compact = re.sub(r"\s+", "", doc)
        if "VENTURECAPITAL" in compact and "VISÃOGERAL" in compact:
            overview_excerpt = re.sub(r"\s+", " ", doc)[:700]
            break
    return {
        "pages": pages,
        "streams_total": len(streams),
        "text_streams": len(text_docs),
        "ve_visao_geral_text_excerpt": overview_excerpt,
        "extraction_note": (
            "O PDF e baseado em graficos: os totais trimestrais aparecem como rotulos de "
            "grafico, sem atribuicao textual inequivoca de trimestre. Por isso nenhum KPI "
            "numerico e normalizado a partir dele (evita erro de transcricao)."
        ),
    }


def collect_abvcap(args: argparse.Namespace) -> dict[str, Any]:
    landing = fetch_result(ABVCAP_BENCHMARK_URL, source="abvcap", retries=3, timeout=90, force=args.refresh)
    result: dict[str, Any] = {
        "name": "ABVCAP + TTR Data - Consolidacao de Dados 2o Trimestre 2026",
        "status": "UNAVAILABLE",
        "available": landing["ok"],
        "url": ABVCAP_BENCHMARK_URL,
        "http_status": landing["status"],
        "bytes": landing["bytes"],
        "sha256": landing["sha256"],
        "method": "HTTP GET da publicacao + download do PDF aberto; titulo/trecho citavel.",
        "title": None,
        "h1": None,
        "citable_excerpt": None,
        "published_year": None,
        "pdf": None,
        "normalized_metrics": None,
        "limitation": (
            "A pagina nao renderiza valores (R$ ausente no HTML; download completo e o PDF). "
            "Os totais do 2T2026 estao apenas em graficos do PDF, portanto o benchmark fica "
            "como referencia citavel (titulo/URL/trecho), sem KPI numerico inventado."
        ),
    }
    if not landing["ok"]:
        result["error"] = landing["error"]
        return result

    text = landing["text"] or ""
    plain = strip_html(text)
    result["title"] = _title_tag(text)
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I)
    result["h1"] = strip_html(h1_match.group(1)) if h1_match else None
    result["citable_excerpt"] = _meta_description(text)
    year_match = re.search(r"Ano da publica[^:]*:\s*(\d{4})", plain)
    result["published_year"] = int(year_match.group(1)) if year_match else None
    result["html_evidence"] = {
        "R$_in_html": plain.count("R$"),
        "2T26_in_html": plain.count("2T26"),
        "pdf_links_found": re.findall(r'href="([^"]+\.pdf)"', text)[:5],
    }

    pdf_match = re.search(r'href="([^"]+\.pdf)"', text)
    pdf_url = pdf_match.group(1) if pdf_match else None
    if pdf_url:
        pdf = fetch_result(
            pdf_url, source="abvcap", retries=2, timeout=180, force=args.refresh, decode_text=False
        )
        pdf_info: dict[str, Any] = {
            "url": pdf_url,
            "ok": pdf["ok"],
            "http_status": pdf["status"],
            "bytes": pdf["bytes"],
            "sha256": pdf["sha256"],
            "error": pdf["error"],
        }
        if pdf["ok"] and pdf["content"] is not None:
            pdf_info["is_pdf"] = pdf["content"][:5] == b"%PDF-"
            pdf_info.update(extract_pdf_evidence(pdf["content"]))
        result["pdf"] = pdf_info
        result["status"] = "AVAILABLE"
    return result


def collect_lavca(args: argparse.Namespace) -> dict[str, Any]:
    landing = fetch_result(LAVCA_BENCHMARK_URL, source="lavca", retries=2, timeout=90, force=args.refresh)
    result: dict[str, Any] = {
        "name": "LAVCA - 2025 Latin American Startup Ecosystem Insights",
        "status": "UNAVAILABLE",
        "available": landing["ok"],
        "url": LAVCA_BENCHMARK_URL,
        "http_status": landing["status"],
        "bytes": landing["bytes"],
        "sha256": landing["sha256"],
        "method": "HTTP GET da pagina oficial de pesquisa; titulo/descricao citaveis.",
        "title": None,
        "citable_excerpt": None,
        "normalized_metrics": None,
        "limitation": (
            "O HTML nao expoe agregados numericos de VC (sem R$/US$ no texto); o relatorio "
            "completo e o caminho oficial. Nenhum numero foi inventado."
        ),
    }
    if not landing["ok"]:
        result["error"] = landing["error"]
        return result
    text = landing["text"] or ""
    plain = strip_html(text)
    result["title"] = _title_tag(text)
    result["citable_excerpt"] = _meta_description(text)
    result["html_evidence"] = {
        "R$_in_html": plain.count("R$"),
        "US$_in_html": plain.count("US$"),
        "VC_mentions": plain.count("VC"),
        "Latin_America_mentions": plain.count("Latin America"),
    }
    result["status"] = "AVAILABLE"
    return result


# --------------------------------------------------------------------------- #
# Validacao (Gate 2 - contrato interim) e escrita
# --------------------------------------------------------------------------- #


def self_check(
    records: list[dict[str, Any]], *, min_records: int, min_brazil: int
) -> tuple[list[str], dict[str, int]]:
    """Valida o contrato interim_record.schema.json e o contrato congelado de payload."""
    errors: list[str] = []
    valid_sources = {"shizune_brazil", "shizune_global"}
    valid_stages = set(STAGE_ENUM_ORDER)
    seen_ids: set[str] = set()
    stats = {
        "records": len(records),
        "unique_firms": 0,
        "unique_firms_with_mapped_stage": 0,
        "unique_firms_geography_includes_brazil": 0,
        "unique_firms_listed_in_brazil_dataset": 0,
        "records_with_mapped_stage": 0,
        "records_geography_includes_brazil": 0,
        "records_listed_in_brazil_dataset": 0,
    }
    firms: dict[str, list[dict[str, Any]]] = {}

    for index, record in enumerate(records):
        label = f"registro[{index}] record_id={record.get('record_id')!r}"
        for key in ("record_id", "source_id", "source_url", "entity_kind", "payload"):
            if key not in record:
                errors.append(f"{label}: campo obrigatorio ausente: {key}")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or len(record_id) < 3:
            errors.append(f"{label}: record_id invalido")
        elif record_id in seen_ids:
            errors.append(f"{label}: record_id duplicado")
        if isinstance(record_id, str):
            seen_ids.add(record_id)
        if record.get("source_id") not in valid_sources:
            errors.append(f"{label}: source_id fora do enum: {record.get('source_id')!r}")
        if record.get("entity_kind") != "firm":
            errors.append(f"{label}: entity_kind deveria ser 'firm'")
        source_url = record.get("source_url", "")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            errors.append(f"{label}: source_url nao e https://")

        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"{label}: payload nao e objeto")
            continue
        missing = [field for field in PAYLOAD_CONTRACT if field not in payload]
        if missing:
            errors.append(f"{label}: payload sem campos do contrato: {missing}")
            continue

        name = payload["investor_name"]
        if not isinstance(name, str) or not name:
            errors.append(f"{label}: investor_name vazio")
        else:
            firms.setdefault(name, []).append(record)

        relevant = payload["relevant_deals"]
        if not isinstance(relevant, int) or isinstance(relevant, bool) or relevant < 0:
            errors.append(f"{label}: relevant_deals deveria ser inteiro >= 0: {relevant!r}")

        geographies = payload["geographies"]
        if not isinstance(geographies, list) or not all(isinstance(item, str) for item in geographies):
            errors.append(f"{label}: geographies invalidas")
            geographies = []
        geo_brazil = payload["geography_includes_brazil"]
        if not isinstance(geo_brazil, bool):
            errors.append(f"{label}: geography_includes_brazil deveria ser bool")
        elif geo_brazil != ("Brazil" in geographies):
            errors.append(f"{label}: geography_includes_brazil inconsistente com geographies")

        listed = payload["listed_in_brazil_dataset"]
        if not isinstance(listed, bool):
            errors.append(f"{label}: listed_in_brazil_dataset deveria ser bool")
        elif listed != (record.get("source_id") == "shizune_brazil"):
            errors.append(f"{label}: listed_in_brazil_dataset inconsistente com source_id")

        stage_raw = payload["stage_raw"]
        if not isinstance(stage_raw, list) or not stage_raw or not all(isinstance(item, str) for item in stage_raw):
            errors.append(f"{label}: stage_raw invalido: {stage_raw!r}")
            stage_raw = []
        declared = payload["declared_stages"]
        if not isinstance(declared, list) or not set(declared).issubset(valid_stages):
            errors.append(f"{label}: declared_stages invalido: {declared!r}")
            declared = []
        unmapped = payload["stage_unmapped"]
        if not isinstance(unmapped, list) or not all(isinstance(item, str) for item in unmapped):
            errors.append(f"{label}: stage_unmapped invalido: {unmapped!r}")
            unmapped = []
        mapped_expected, unmapped_expected = map_declared_stages(stage_raw)
        if declared != mapped_expected:
            errors.append(f"{label}: declared_stages nao corresponde ao mapeamento declarado de stage_raw")
        if sorted(unmapped) != sorted(unmapped_expected):
            errors.append(f"{label}: stage_unmapped nao cobre exatamente os tokens nao mapeados")

        focus = payload["focus_sectors"]
        if not isinstance(focus, list) or not all(isinstance(item, str) for item in focus):
            errors.append(f"{label}: focus_sectors invalido")

        table_url = payload["table_url"]
        if not isinstance(table_url, str) or not table_url.startswith("https://"):
            errors.append(f"{label}: table_url nao e https://")
        if table_url != source_url:
            errors.append(f"{label}: table_url difere de source_url")
        if not isinstance(payload["table_title"], str) or not payload["table_title"]:
            errors.append(f"{label}: table_title vazio")

        if declared:
            stats["records_with_mapped_stage"] += 1
        if geo_brazil:
            stats["records_geography_includes_brazil"] += 1
        if listed:
            stats["records_listed_in_brazil_dataset"] += 1

    for name, firm_records in firms.items():
        if any(record["payload"]["declared_stages"] for record in firm_records):
            stats["unique_firms_with_mapped_stage"] += 1
        if any(record["payload"]["geography_includes_brazil"] for record in firm_records):
            stats["unique_firms_geography_includes_brazil"] += 1
        if any(record["payload"]["listed_in_brazil_dataset"] for record in firm_records):
            stats["unique_firms_listed_in_brazil_dataset"] += 1
    stats["unique_firms"] = len(firms)

    if stats["unique_firms_with_mapped_stage"] < min_records:
        errors.append(
            f"aceite: {stats['unique_firms_with_mapped_stage']} gestoras com estagio mapeado "
            f"< minimo {min_records}"
        )
    if stats["unique_firms_geography_includes_brazil"] < min_brazil:
        errors.append(
            f"aceite: {stats['unique_firms_geography_includes_brazil']} gestoras com "
            f"geography_includes_brazil < minimo {min_brazil}"
        )
    return errors, stats


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    _atomic_write(path, "\n".join(lines) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def _benchmark_entry(source: dict[str, Any]) -> dict[str, Any]:
    """Monta o item do array ``benchmarks`` exigido pelo contrato congelado pelo Lead."""
    note_parts: list[str] = []
    if source.get("pdf"):
        pdf = source["pdf"]
        note_parts.append(
            f"PDF aberto: {pdf.get('url')} ({pdf.get('bytes')} bytes, sha256={pdf.get('sha256')}); "
            "totais apenas em graficos, sem KPI numerico normalizado."
        )
    if source.get("limitation"):
        note_parts.append(source["limitation"])
    if source.get("error"):
        note_parts.append(f"Erro de acesso: {source['error']}")
    # Contrato do Lead para o array benchmarks: exatamente name/url/status/excerpt/note.
    return {
        "name": source.get("name"),
        "url": source.get("url"),
        "status": source.get("status"),
        "excerpt": source.get("citable_excerpt"),
        "note": " ".join(note_parts).strip() or None,
    }


def build_availability(
    *,
    args: argparse.Namespace,
    table_results: dict[str, dict[str, Any]],
    stats: dict[str, Any],
    discovery: dict[str, Any],
    openvc: dict[str, Any],
    brouky: dict[str, Any],
    abvcap: dict[str, Any],
    lavca: dict[str, Any],
    selfcheck: dict[str, Any],
) -> dict[str, Any]:
    ingested_tables: list[dict[str, Any]] = []
    for spec in INGEST_TABLES:
        result = table_results[spec["path"]]
        ingested_tables.append(
            {
                "path": spec["path"],
                "url": result["url"],
                "role": spec["role"],
                "chart_scope": spec["chart_scope"],
                "source_id": "shizune_brazil" if spec["chart_scope"] == "brazil" else "shizune_global",
                "note": spec["note"],
                "http_status": result["status"],
                "ok": result["ok"],
                "bytes": result["bytes"],
                "sha256": result["sha256"],
                "from_cache": result["from_cache"],
                "title": result.get("title"),
                "columns": result.get("columns"),
                "rows_expected_from_title": result.get("expected_rows"),
                "rows_parsed": len(result.get("rows") or []),
                "parse_errors": len(result.get("parse_errors") or []),
                "ingested": True,
            }
        )

    excluded_tables: list[dict[str, Any]] = []
    for spec in WORKING_EXCLUDED_TABLES:
        result = table_results[spec["path"]]
        excluded_tables.append(
            {
                "path": spec["path"],
                "url": result["url"],
                "chart_scope": spec["chart_scope"],
                "http_status": result["status"],
                "ok": result["ok"],
                "bytes": result["bytes"],
                "title": result.get("title"),
                "rows_parsed": len(result.get("rows") or []),
                "ingested": False,
                "reason_not_ingested": spec["reason"],
            }
        )

    not_found: list[dict[str, Any]] = []
    for spec in EXPECTED_404_PROBES:
        result = table_results[spec["path"]]
        not_found.append(
            {
                "path": spec["path"],
                "url": result["url"],
                "why": spec["why"],
                "http_status": result["status"],
                "ok": result["ok"],
                "error": result["error"],
                "unexpectedly_available": bool(result["ok"]),
            }
        )

    # `sources` e uma LISTA plana (consumida por pipeline/transform.py no
    # sources_registry); o detalhamento rico fica em `source_details`.
    brouky_page = (brouky.get("pages") or [{}])[-1]
    shizune_bytes_total = sum(int(table["bytes"] or 0) for table in ingested_tables)
    availability_entries: list[dict[str, Any]] = [
        {
            "name": "Shizune - Investors Database (charts table.md)",
            "url": f"{SHIZUNE_BASE}{INGEST_TABLES[0]['path']}/table.md",
            "license": "Conteudo publico citavel (shizune.co)",
            "records": stats["records"],
            "bytes": shizune_bytes_total,
            "sha256": None,
            "status": "ok",
            "available": True,
            "http_status": 200,
            "method": "HTTP GET <chart>/table.md + parsing de tabela Markdown (stdlib).",
            "fallback": None,
            "notes": (
                f"{stats['tables_ingested']} tabelas HTTP 200; {stats['records']} registros; "
                f"{stats['unique_firms']} gestoras unicas. Detalhe em source_details.shizune."
            ),
        },
        {
            "name": "OpenVC - Investor Database / VC List of VC Lists",
            "url": OPENVC_URLS[0][0],
            "license": "Acesso aberto (bloqueado por Cloudflare na coleta)",
            "records": 0,
            "bytes": 0,
            "sha256": None,
            "status": "blocked" if not openvc.get("available") else "ok",
            "available": bool(openvc.get("available")),
            "http_status": 403 if not openvc.get("available") else 200,
            "method": openvc.get("method"),
            "fallback": "shizune_country_cuts (9 charts abertos de pais/tipo)",
            "notes": (
                "HTTP 403 nas 2 URLs x 2 User-Agents (academico e navegador). Nenhum dado "
                "OpenVC coletado ou sintetizado; fallback aberto Shizune. Tentativas em "
                "source_details.openvc.attempts."
            ),
        },
        {
            "name": "Brouky - VC & Startup Data Reports",
            "url": BROUKY_DATA_URL,
            "license": "Conteudo publico citavel (brouky.tech)",
            "records": None,
            "bytes": brouky_page.get("bytes"),
            "sha256": brouky_page.get("sha256"),
            "status": "ok_page_only" if brouky.get("available") else "unavailable",
            "available": bool(brouky.get("available")),
            "http_status": brouky_page.get("status"),
            "method": brouky.get("method"),
            "fallback": None,
            "notes": (
                "Paginas 200 (HTML Next.js) sem export CSV/JSON publico; apenas agregados "
                "textuais de pagina foram extraidos como evidencia (source_details.brouky), "
                "sem conversao em registros de gestora."
            ),
        },
        {
            "name": abvcap.get("name") or "ABVCAP + TTR Data",
            "url": abvcap.get("url"),
            "license": "Publicacao citavel (ABVCAP + TTR Data)",
            "records": None,
            "bytes": abvcap.get("bytes"),
            "sha256": abvcap.get("sha256"),
            "status": "ok" if abvcap.get("available") else "unavailable",
            "available": bool(abvcap.get("available")),
            "http_status": abvcap.get("http_status"),
            "method": abvcap.get("method"),
            "fallback": None,
            "notes": "Benchmark citavel (titulo/URL/trecho); totais apenas em graficos do PDF, sem KPI numerico normalizado.",
        },
        {
            "name": lavca.get("name") or "LAVCA",
            "url": lavca.get("url"),
            "license": "Publicacao citavel (LAVCA)",
            "records": None,
            "bytes": lavca.get("bytes"),
            "sha256": lavca.get("sha256"),
            "status": "ok" if lavca.get("available") else "unavailable",
            "available": bool(lavca.get("available")),
            "http_status": lavca.get("http_status"),
            "method": lavca.get("method"),
            "fallback": None,
            "notes": "Benchmark citavel (titulo/descricao); sem agregados numericos no HTML, nenhum numero inventado.",
        },
    ]
    openvc_warning = (
        "BLOQUEADO: HTTP 403 (Cloudflare) nas 4 tentativas (2 URLs x 2 User-Agents). "
        "Fallback acionado: charts abertos de pais/tipo do Shizune (9 tabelas). "
        "Nenhum registro OpenVC coletado ou sintetizado."
        if not openvc.get("available")
        else "OpenVC acessivel nesta coleta."
    )

    return {
        "generated_at_utc": now_utc(),
        "pipeline": "pipeline/collect_directories.py",
        "payload_contract_jsonl": PAYLOAD_CONTRACT,
        "network": {
            "client": "pipeline/net.py",
            "academic_user_agent": ACADEMIC_UA,
            "browser_user_agent_used_for_openvc": BROWSER_UA,
            "default_min_interval_s": 0.3,
            "offline_replay": bool(args.offline),
            "cache_dir": "data/raw",
            "manifest": "data/manifest.jsonl",
        },
        "sources": availability_entries,
        "openvc": openvc_warning,
        "source_details": {
            "shizune": {
                "status": "AVAILABLE",
                "available": True,
                "base_url": SHIZUNE_BASE,
                "method": "HTTP GET <chart>/table.md; parsing de tabela Markdown com stdlib (sem libs externas).",
                "discovery": discovery,
                "ingested_tables": ingested_tables,
                "working_excluded_tables": excluded_tables,
                "not_found_probes": not_found,
                "field_semantics": {
                    "columns": EXPECTED_COLUMNS,
                    "geography": (
                        "Geography lista as geografias de atuacao declaradas (no maximo 3 por "
                        "linha no export). Nao e pais-sede (HQ): hq_country nao e derivado."
                    ),
                    "geography_includes_brazil": (
                        "True quando 'Brazil' aparece na coluna Geography da propria linha "
                        "(comparacao exata). E o sinal de geografia declarada."
                    ),
                    "listed_in_brazil_dataset": (
                        "True para linhas de charts com recorte de pais Brazil (por definicao, "
                        "charts de gestoras ativas no Brasil), mesmo quando a Geography nao lista "
                        "'Brazil' (ex.: NFX na tabela canonica). E o criterio de atividade no "
                        "Brasil; NAO e inferido da Geography. Charts globais/outros paises -> false."
                    ),
                    "relevant_deals": (
                        "Coluna 'Relevant Deals' e escopada ao chart, NAO e contagem Brasil-only. "
                        "O mesmo nome pode ter valores diferentes por chart (ex.: Canary) — ver "
                        "field_semantics.relevant_deals_variance_example."
                    ),
                    "stage_raw": "Tokens da coluna Stage exatamente como na fonte.",
                    "declared_stages": (
                        "Subconjunto de stage_raw mapeado para o enum via STAGE_MAP "
                        "(Pre-Seed->PRE_SEED, Seed->SEED, Series A->SERIES_A, Series B->SERIES_B, "
                        "Series C->LATE_STAGE_GROWTH). Nenhum estagio nao declarado e criado."
                    ),
                    "stage_unmapped": (
                        "Tokens de Stage sem faixa no enum (Venture - Series Unknown, Angel, Grant, "
                        "Convertible Note, Private Equity, Debt Financing, Equity Crowdfunding)."
                    ),
                },
                "relevant_deals_variance_example": {
                    "firm": "Canary",
                    "values_by_table": {
                        f"{SHIZUNE_BASE}/investors/venture-capital-vc-funds-brazil/table.md": 2,
                        f"{SHIZUNE_BASE}/investors/vc-funds-brazil/table.md": 203,
                        f"{SHIZUNE_BASE}/investors/vc-funds-latam/table.md": 230,
                    },
                    "note": "Exemplo de que 'Relevant Deals' e escopado ao chart.",
                },
                "stats": stats,
            },
            "openvc": openvc,
            "brouky": brouky,
            "abvcap": abvcap,
            "lavca": lavca,
        },
        "benchmarks": [
            _benchmark_entry(abvcap),
            _benchmark_entry(lavca),
        ],
        "acceptance": selfcheck,
        "limitations": [
            "Shizune exporta 'Top N' por chart (35/50): o dataset e um recorte de topo, nao um censo.",
            "Geography lista no maximo 3 geografias por linha; geography_includes_brazil=false nao prova ausencia de atuacao no Brasil (usar listed_in_brazil_dataset em conjunto).",
            "Relevant Deals e escopado ao chart e varia entre charts; nao deve ser usado como contagem Brasil-only sem validacao.",
            "OpenVC respondeu 403 (Cloudflare) a 2 URLs x 2 User-Agents; nenhum dado OpenVC foi coletado ou sintetizado.",
            "Brouky expoe agregados em HTML (Next.js) sem export CSV/JSON publico; evidencias agregadas registradas, nao convertidas em registros de gestora.",
            "ABVCAP/LAVCA: paginas oficiais citaveis (titulo/descricao), mas sem agregados numericos normalizaveis no HTML; ABVCAP publica totais apenas em graficos do PDF. Nenhum numero foi inventado.",
            "Nao existem charts Shizune por estagio (0 de 11.763 slugs); 6 URLs de estagio testadas retornam 404.",
            "Charts setoriais Shizune ('-vc-funds-brazil', 60 disponiveis) ficam como superficie de expansao, nao ingeridos para manter recorte homogeneo.",
            "Stage 'Series C' e mapeado para LATE_STAGE_GROWTH (unica faixa compativel do enum); demais valores nao-enum vao para payload.stage_unmapped.",
            "Nomes preservados exatamente como na fonte (sem normalizacao de caixa/acentos/entidades HTML).",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta diretorios abertos (Shizune/OpenVC/Brouky) e benchmarks (ABVCAP/LAVCA)."
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="diretorio de saida para shizune_firms.jsonl e directories_availability.json",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="forca re-download (ignora cache do net.py) das tabelas e paginas de fonte",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="replay offline: usa apenas o cache de data/raw (VC_OFFLINE=1)",
    )
    parser.add_argument(
        "--min-records",
        type=int,
        default=35,
        help="minimo de gestoras com estagio declarado mapeado (aceite)",
    )
    parser.add_argument(
        "--min-brazil",
        type=int,
        default=10,
        help="minimo de gestoras com geography_includes_brazil=true (aceite)",
    )
    args = parser.parse_args(argv)
    if args.offline:
        os.environ["VC_OFFLINE"] = "1"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir: Path = args.out_dir
    jsonl_path = out_dir / "shizune_firms.jsonl"
    availability_path = out_dir / "directories_availability.json"

    print("== Coleta de diretorios abertos (tese declarada + atividade no Brasil) ==")
    print(f"offline={args.offline} refresh={args.refresh} out_dir={out_dir}")

    table_results = collect_shizune_tables(args)
    missing_ingested = [spec["path"] for spec in INGEST_TABLES if not table_results[spec["path"]]["ok"]]
    if missing_ingested:
        print("ERRO: tabelas obrigatorias indisponiveis:", missing_ingested)
        for path in missing_ingested:
            print("  -", path, "->", table_results[path]["error"])
        return 2

    for spec in INGEST_TABLES:
        result = table_results[spec["path"]]
        if result.get("columns") != EXPECTED_COLUMNS:
            print(f"ERRO: colunas inesperadas em {spec['path']}: {result.get('columns')!r}")
            return 2
        if result.get("expected_rows") is not None and result["expected_rows"] != len(result["rows"]):
            print(
                f"AVISO: {spec['path']} promete {result['expected_rows']} linhas, "
                f"parseou {len(result['rows'])}",
            )

    discovery = collect_shizune_discovery(args)
    records, stats = build_firm_records(table_results)
    openvc = collect_openvc(args)
    brouky = collect_brouky(args)
    abvcap = collect_abvcap(args)
    lavca = collect_lavca(args)

    errors, check_stats = self_check(records, min_records=args.min_records, min_brazil=args.min_brazil)
    selfcheck = {
        "passed": not errors,
        "errors": errors,
        "requirements": {
            "min_unique_firms_with_mapped_stage": args.min_records,
            "min_unique_firms_geography_includes_brazil": args.min_brazil,
        },
        "observed": check_stats,
    }

    availability = build_availability(
        args=args,
        table_results=table_results,
        stats=stats,
        discovery=discovery,
        openvc=openvc,
        brouky=brouky,
        abvcap=abvcap,
        lavca=lavca,
        selfcheck=selfcheck,
    )

    write_jsonl(jsonl_path, records)
    write_json(availability_path, availability)

    print("\n-- Resultado --")
    print(f"tabelas Shizune ingeridas: {stats['tables_ingested']} ({stats['table_rows']} linhas)")
    print(f"registros JSONL (linha de tabela): {stats['records']}")
    print(f"gestoras unicas: {stats['unique_firms']}")
    print(f"gestoras com estagio declarado mapeado: {stats['unique_firms_with_mapped_stage']}")
    print(f"gestoras com geography_includes_brazil: {stats['unique_firms_geography_includes_brazil']}")
    print(f"gestoras em chart com recorte Brasil: {stats['unique_firms_listed_in_brazil_dataset']}")
    print(f"distribuicao de estagios (gestoras): {stats['declared_stage_distribution_firms']}")
    print(f"tokens de estagio fora do enum: {stats['non_enum_stage_tokens']}")
    print(f"OpenVC: {openvc['status']} (tentativas registradas: {len(openvc['attempts'])})")
    print(f"Brouky: {brouky['status']}")
    print(f"ABVCAP: {abvcap['status']} | title={abvcap.get('title')!r}")
    print(f"LAVCA: {lavca['status']} | title={lavca.get('title')!r}")
    print(f"benchmarks citaveis: {len(availability['benchmarks'])}")
    print(f"self-check contrato/aceite: {'PASS' if not errors else 'FAIL'}")
    for error in errors[:20]:
        print("  !", error)
    if brouky.get("extracted_evidence", {}).get("headline"):
        print(f"Brouky headline: {brouky['extracted_evidence']['headline']}")
    print(f"\nJSONL: {jsonl_path}")
    print(f"Auditoria: {availability_path}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
