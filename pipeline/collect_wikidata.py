"""Coletor Wikidata (SPARQL) -> gestoras de Venture Capital (data/interim/wikidata_firms.jsonl).

CORRECAO DOCUMENTADA DO QID DA SPEC
-----------------------------------
A spec do projeto cita ``wd:Q1961622`` como "Venture Capital Firm". Verificacao via
wbgetentities mostra que Q1961622 = rotulo "NUW", descricao "Wikimedia disambiguation page",
P31 = Q4167410 (pagina de desambiguacao). O item correto, obtido via
``action=wbsearchentities&search=venture capital firm``, e **Q3487908** ("Venture capital firm").
Este coletor usa Q3487908 como classe principal.

Descoberta (todos os QIDs vem de consultas reais, nada sintetizado):
  1. SPARQL paginado: ``?f wdt:P31 wd:Q3487908`` (instancias da classe "venture capital firm").
  2. SPARQL paginado: union de ``?f wdt:P452 wd:Q219409`` (industria = venture capital) com
     ``?f wdt:P31/wdt:P279* wd:Q5418962`` (classe "private equity firm" e subclasses).
  3. Categorias Wikimedia (en.wikipedia) "Venture capital firms" + subcategorias por pais,
     mapeadas para QIDs via prop=pageprops/wikibase_item e enriquecidas por SPARQL.

Enriquecimento: batches de VALUES em https://query.wikidata.org/sparql com label service
(en,pt,es,fr,de) e propriedades P17 (pais), P856 (website), P571 (fundacao), P159 (sede) e
P31 (tipos, usados para excluir humanos/listas/artigos). So entram no JSONL itens com P17
resolvido para ISO-3166-1 alpha-2 via P297.

Uso:
  python pipeline/collect_wikidata.py                 # descoberta completa + enriquecimento
  python pipeline/collect_wikidata.py --no-categories  # apenas SPARQL de classe/industria
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from net import FetchError, fetch  # noqa: E402

OUT_PATH = ROOT / "data" / "interim" / "wikidata_firms.jsonl"
SOURCES_PATH = ROOT / "data" / "interim" / "global_sources.json"

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
ENWIKI_API = "https://en.wikipedia.org/w/api.php"

QID_VC_FIRM = "Q3487908"          # "Venture capital firm" (correto)
QID_VC_INDUSTRY = "Q219409"       # "venture capital" (usado em P452)
QID_PE_FIRM = "Q5418962"          # "private equity firm" (classe ampliada)
QID_SPEC_WRONG = "Q1961622"       # citado na spec; desambiguacao "NUW"

# Tipos (P31) que nao representam gestoras: humanos, desambiguacoes, listas, artigos,
# categorias/predefinicoes Wikimedia. Usados para excluir ruido da descoberta.
EXCLUDED_INSTANCE_TYPES = {
    "Q5",          # human
    "Q4167410",    # Wikimedia disambiguation page
    "Q13442814",   # scholarly article
    "Q13406463",   # Wikimedia list article
    "Q4167836",    # Wikimedia category
    "Q11266439",   # Wikimedia template
    "Q17362920",   # duplicated page
    "Q22808320",   # Wikimedia human name disambiguation page
    "Q167037",     # business? (mantido fora de exclusao - nao excluir)
}
EXCLUDED_INSTANCE_TYPES.discard("Q167037")

CATEGORY_SEEDS = ["Category:Venture capital firms"]
DISCOVERY_PAGE_SIZE = 200
ENRICH_BATCH = 80
TODAY = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sparql_query(query: str, *, timeout: int = 180) -> tuple[list[dict], int]:
    """Executa consulta SPARQL no WDQS e devolve (bindings, bytes_da_resposta)."""
    url = SPARQL_ENDPOINT + "?format=json&query=" + urllib.parse.quote(query)
    fetched = fetch(url, source="wikidata_sparql", timeout=timeout)
    payload = fetched.json()
    return payload.get("results", {}).get("bindings", []), len(fetched.content)


def discover_class_paginated(class_qid: str, tag: str, stats: dict, seen: dict[str, set[str]]) -> None:
    """Pagina LIMIT/OFFSET as instancias de uma classe e registra a descoberta."""
    offset = 0
    while True:
        query = (
            f"SELECT DISTINCT ?f WHERE {{ ?f wdt:P31 wd:{class_qid} }} "
            f"ORDER BY ?f LIMIT {DISCOVERY_PAGE_SIZE} OFFSET {offset}"
        )
        try:
            rows, nbytes = sparql_query(query)
        except FetchError as exc:
            stats["errors"].append(f"descoberta {tag} offset {offset}: {exc}")
            break
        stats["bytes"] += nbytes
        if not rows:
            break
        for row in rows:
            qid = row["f"]["value"].rsplit("/", 1)[-1]
            seen.setdefault(qid, set()).add(tag)
        stats["per_channel"][tag] = stats["per_channel"].get(tag, 0) + len(rows)
        offset += len(rows)
        if len(rows) < DISCOVERY_PAGE_SIZE:
            break


def discover_union_paginated(stats: dict, seen: dict[str, set[str]]) -> None:
    """Union SPARQL paginada: industria venture capital + classe private equity firm (subclasses)."""
    offset = 0
    while True:
        query = (
            "SELECT DISTINCT ?f WHERE { "
            f"{{ ?f wdt:P452 wd:{QID_VC_INDUSTRY} }} UNION "
            f"{{ ?f wdt:P31/wdt:P279* wd:{QID_PE_FIRM} }} "
            f"}} ORDER BY ?f LIMIT {DISCOVERY_PAGE_SIZE} OFFSET {offset}"
        )
        try:
            rows, nbytes = sparql_query(query)
        except FetchError as exc:
            stats["errors"].append(f"descoberta union offset {offset}: {exc}")
            break
        stats["bytes"] += nbytes
        if not rows:
            break
        for row in rows:
            qid = row["f"]["value"].rsplit("/", 1)[-1]
            seen.setdefault(qid, set()).add("sparql_industry_or_pe")
        stats["per_channel"]["sparql_industry_or_pe"] = stats["per_channel"].get("sparql_industry_or_pe", 0) + len(rows)
        offset += len(rows)
        if len(rows) < DISCOVERY_PAGE_SIZE:
            break


def api_json(url: str, stats: dict) -> dict | None:
    try:
        fetched = fetch(url, source="wikidata_sparql")
        stats["bytes"] += len(fetched.content)
        return fetched.json()
    except FetchError as exc:
        stats["errors"].append(f"{url}: {exc}")
        return None


def discover_categories(stats: dict, seen: dict[str, set[str]]) -> None:
    """Descoberta auxiliar: membros de categorias enwiki de gestoras VC -> QIDs via pageprops."""

    def category_pages(category: str, depth: int) -> list[str]:
        titles: list[str] = []
        cont: str | None = None
        while True:
            url = (
                ENWIKI_API + "?action=query&list=categorymembers&cmtitle="
                + urllib.parse.quote(category)
                + "&cmlimit=500&format=json&cmtype=page|subcat"
            )
            if cont:
                url += "&cmcontinue=" + urllib.parse.quote(cont)
            payload = api_json(url, stats)
            if not payload:
                break
            members = payload.get("query", {}).get("categorymembers", [])
            for member in members:
                if member.get("ns") == 0:
                    titles.append(member["title"])
                elif member.get("ns") == 14 and depth < 2:
                    titles.extend(category_pages(member["title"], depth + 1))
            cont = payload.get("continue", {}).get("cmcontinue")
            if not cont:
                break
        return titles

    titles: list[str] = []
    for seed in CATEGORY_SEEDS:
        titles.extend(category_pages(seed, 0))
    titles = sorted({t for t in titles if ":" not in t})
    stats["category_titles"] = len(titles)

    qids: list[str] = []
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        url = (
            ENWIKI_API + "?action=query&prop=pageprops&ppprop=wikibase_item&format=json&titles="
            + urllib.parse.quote("|".join(batch))
        )
        payload = api_json(url, stats)
        if not payload:
            continue
        for page in payload.get("query", {}).get("pages", {}).values():
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                qids.append(qid)
    for qid in qids:
        seen.setdefault(qid, set()).add("wikimedia_category_enwiki")
    stats["per_channel"]["wikimedia_category_enwiki"] = len(qids)


def enrich(qids: list[str], stats: dict) -> dict[str, dict]:
    """Busca propriedades dos QIDs em batches de VALUES via SPARQL."""
    details: dict[str, dict] = {}
    for start in range(0, len(qids), ENRICH_BATCH):
        batch = qids[start:start + ENRICH_BATCH]
        values = " ".join("wd:" + qid for qid in batch)
        query = f"""
SELECT ?f ?fLabel ?fDescription ?country ?countryIso ?website ?founded ?hq ?hqLabel ?type WHERE {{
  VALUES ?f {{ {values} }}
  OPTIONAL {{ ?f wdt:P17 ?country . OPTIONAL {{ ?country wdt:P297 ?countryIso }} }}
  OPTIONAL {{ ?f wdt:P856 ?website }}
  OPTIONAL {{ ?f wdt:P571 ?founded }}
  OPTIONAL {{ ?f wdt:P159 ?hq . OPTIONAL {{ ?hq wdt:P17 ?hqCountry . OPTIONAL {{ ?hqCountry wdt:P297 ?hqCountryIso }} }} }}
  OPTIONAL {{ ?f wdt:P31 ?type }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,pt,es,fr,de". }}
}}"""
        rows = None
        for attempt_batch in (batch, batch[: max(1, len(batch) // 2)]):
            try:
                rows, nbytes = sparql_query(query if attempt_batch is batch else query, timeout=180)
                stats["bytes"] += nbytes
                break
            except FetchError as exc:
                stats["errors"].append(f"enriquecimento offset {start}: {exc}")
                rows = None
                if attempt_batch is not attempt_batch:  # pragma: no cover
                    break
        if rows is None:
            continue
        for row in rows:
            qid = row["f"]["value"].rsplit("/", 1)[-1]
            entry = details.setdefault(
                qid,
                {"labels": set(), "descriptions": set(), "countries": set(), "country_isos": set(),
                 "websites": set(), "founded": set(), "hq": set(), "hq_labels": set(), "types": set()},
            )
            if "fLabel" in row:
                entry["labels"].add(row["fLabel"]["value"])
            if "fDescription" in row:
                entry["descriptions"].add(row["fDescription"]["value"])
            if "country" in row:
                entry["countries"].add(row["country"]["value"].rsplit("/", 1)[-1])
            if "countryIso" in row:
                entry["country_isos"].add(row["countryIso"]["value"].strip().upper())
            if "website" in row:
                entry["websites"].add(row["website"]["value"].strip())
            if "founded" in row:
                entry["founded"].add(row["founded"]["value"])
            if "hq" in row:
                entry["hq"].add(row["hq"]["value"].rsplit("/", 1)[-1])
            if "hqLabel" in row:
                entry["hq_labels"].add(row["hqLabel"]["value"])
            if "type" in row:
                entry["types"].add(row["type"]["value"].rsplit("/", 1)[-1])
    return details


DATE_RE = re.compile(r"^(-?\d{4}-\d{2}-\d{2})")


def normalize_founded(raw_values: set[str]) -> str | None:
    dates = []
    for value in raw_values:
        match = DATE_RE.match(value)
        if match:
            dates.append(match.group(1))
    return min(dates) if dates else None


def build_record(qid: str, entry: dict, channels: set[str]) -> dict | None:
    country_iso = sorted(entry["country_isos"])[0] if entry["country_isos"] else None
    if not country_iso:
        return None
    types = entry["types"]
    if types & EXCLUDED_INSTANCE_TYPES:
        return None
    if types & {"Q5"}:
        return None

    label = sorted(entry["labels"], key=lambda s: (len(s), s))[0] if entry["labels"] else qid
    description = sorted(entry["descriptions"], key=lambda s: (len(s), s))[0] if entry["descriptions"] else None
    websites = sorted(w for w in entry["websites"] if w.startswith("http"))
    website = next((w for w in websites if w.startswith("https")), websites[0] if websites else None)
    founded = normalize_founded(entry["founded"])
    hq_qids = sorted(entry["hq"])
    hq_qid = hq_qids[0] if hq_qids else None
    hq_label = None
    if entry["hq_labels"]:
        hq_label = sorted(entry["hq_labels"], key=lambda s: (len(s), s))[0]

    undisclosed = []
    if not country_iso:
        undisclosed.append("country_iso2")
    if not website:
        undisclosed.append("website")
    if not founded:
        undisclosed.append("founded")
    if not hq_qid:
        undisclosed.append("hq_location_qid")
    if not description:
        undisclosed.append("description")
    if not label:
        undisclosed.append("label")

    return {
        "record_id": qid,
        "source_id": "wikidata_sparql",
        "source_url": f"https://www.wikidata.org/wiki/{qid}",
        "retrieved_from": SPARQL_ENDPOINT,
        "entity_kind": "firm",
        "country_hint": country_iso,
        "payload": {
            "qid": qid,
            "label": label,
            "country_iso2": country_iso,
            "country_qid": sorted(entry["countries"])[0] if entry["countries"] else None,
            "website": website,
            "founded": founded,
            "hq_location_qid": hq_qid,
            "hq_label": hq_label,
            "description": description,
            "_undisclosed": undisclosed,
        },
    }


def update_sources(entry: dict, qid_used: dict, limitations: list[str]) -> None:
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
    doc["wikidata_qid_used"] = qid_used
    # Cada coletor e dono das suas limitacoes (prefixo "[Wikidata]"); remove texto legado/obsoleto.
    merged = [
        l for l in (doc.get("limitations") or [])
        if str(l).startswith(("[SEC Form D]", "[Wikidata]", "[BCB PTAX]"))
        and not str(l).startswith("[Wikidata]")
    ]
    merged.extend(limitations)
    doc["limitations"] = merged
    SOURCES_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Coleta gestoras de VC no Wikidata via SPARQL.")
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--max-firms", type=int, default=1200, help="teto de QIDs enriquecidos")
    parser.add_argument("--no-categories", action="store_true", help="nao usa categorias enwiki na descoberta")
    args = parser.parse_args()

    stats = {"bytes": 0, "errors": [], "per_channel": {}}
    seen: dict[str, set[str]] = {}

    print("[wikidata] descoberta SPARQL: P31=Q3487908 ...")
    discover_class_paginated(QID_VC_FIRM, "sparql_class_vc_firm", stats, seen)
    print(f"[wikidata]   -> {stats['per_channel'].get('sparql_class_vc_firm', 0)} linhas")
    print("[wikidata] descoberta SPARQL: P452=Q219409 OR P31/P279* Q5418962 ...")
    discover_union_paginated(stats, seen)
    print(f"[wikidata]   -> {stats['per_channel'].get('sparql_industry_or_pe', 0)} linhas")
    if not args.no_categories:
        print("[wikidata] descoberta auxiliar: categorias enwiki ...")
        discover_categories(stats, seen)
        print(f"[wikidata]   -> {stats['category_titles']} titulos -> "
              f"{stats['per_channel'].get('wikimedia_category_enwiki', 0)} QIDs")

    qids = sorted(seen)[: args.max_firms]
    print(f"[wikidata] QIDs unicos descobertos: {len(seen)}; enriquecendo {len(qids)}")
    details = enrich(qids, stats)

    records = []
    excluded_type = 0
    no_country = 0
    missing_detail = 0
    for qid in qids:
        entry = details.get(qid)
        if not entry:
            missing_detail += 1
            continue
        if entry["types"] & EXCLUDED_INSTANCE_TYPES:
            excluded_type += 1
            continue
        if not entry["country_isos"]:
            no_country += 1
            continue
        record = build_record(qid, entry, seen[qid])
        if record is not None:
            records.append(record)
    records.sort(key=lambda r: r["record_id"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    output_bytes = out.stat().st_size
    output_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    with_country = sum(1 for r in records if r["payload"]["country_iso2"])
    print(f"[wikidata] TOTAL: {len(records)} gestoras com pais em {len(qids)} QIDs enriquecidos "
          f"(sem P17: {no_country}, excluidos por tipo: {excluded_type}, sem detalhes: {missing_detail})")
    print(f"[wikidata] saida: {out} ({output_bytes} bytes, sha256={output_sha[:16]}...)")

    limitations = [
        "[Wikidata] Q1961622 citado na spec e uma pagina de desambiguacao ('NUW', P31=Q4167410); "
        "substituido por Q3487908 ('Venture capital firm') obtido via wbsearchentities.",
        "[Wikidata] Classe Q3487908 tem base curada pequena; uniao com P452=Q219409, "
        "P31/P279*=Q5418962 e categorias enwiki foi necessaria para cobertura.",
        f"[Wikidata] {no_country} itens descobertos foram descartados por nao terem P17 (pais) "
        f"resolvivel em ISO2; {excluded_type} itens excluidos por tipo (humano/lista/desambiguacao/"
        f"artigo) e {missing_detail} nao retornaram detalhes no enriquecimento.",
        "[Wikidata] P571 (fundacao) pode ter precisao apenas de ano; a data canonica do repositorio "
        "(ex.: 1999-01-01) e preservada sem inferencia.",
        "[Wikidata] Categorias Wikimedia sao usadas apenas como canal de descoberta de QIDs; todos os "
        "atributos vem de declaracoes do Wikidata via SPARQL.",
        "[Wikidata] country_iso2 reflete P17 (pais da entidade no Wikidata) e pode divergir da sede "
        "declarada em P159 (preservada em hq_location_qid/hq_label); nenhum ajuste e inferido.",
        "[Wikidata] websites sao preservados exatamente como declarados no repositorio (podem ser http://).",
    ]
    entry = {
        "name": "Wikidata Query Service (SPARQL) - gestoras de VC",
        "url": SPARQL_ENDPOINT,
        "status": 200,
        "bytes": stats["bytes"],
        "sha256": output_sha,
        "records": len(records),
        "notes": (
            f"Descoberta: P31=Q3487908 ({stats['per_channel'].get('sparql_class_vc_firm', 0)} linhas), "
            f"P452=Q219409 OR P31/P279*=Q5418962 ({stats['per_channel'].get('sparql_industry_or_pe', 0)} linhas), "
            f"categorias enwiki ({stats['per_channel'].get('wikimedia_category_enwiki', 0)} QIDs). "
            f"{len(qids)} QIDs enriquecidos, {len(records)} com pais ISO2. "
            f"sha256 acima = hash do JSONL final; bytes = soma dos bytes das respostas SPARQL/API."
        ),
    }
    qid_used = {
        "venture_capital_firm": QID_VC_FIRM,
        "venture_capital_industry": QID_VC_INDUSTRY,
        "private_equity_firm": QID_PE_FIRM,
        "spec_qid_invalido": QID_SPEC_WRONG,
        "nota_correcao": (
            "Q1961622 = 'NUW' (Wikimedia disambiguation page, P31=Q4167410); "
            "QID correto de 'venture capital firm' = Q3487908 (wbsearchentities)."
        ),
        "coletado_em_utc": TODAY,
    }
    update_sources(entry, qid_used, limitations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
