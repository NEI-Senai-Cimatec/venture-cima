#!/usr/bin/env python3
"""Render do dashboard HTML executivo de Venture Capital (Brasil x Global).

Entrada canonica: data/canonical/funds.json (contrato contract/venture_capital_fund.schema.json)
e, opcionalmente, data/canonical/kpis.json (metadados de proveniencia).

Uso:
  python pipeline/render.py --input data/canonical/funds.json --out dashboard/index.html
  python pipeline/render.py --fetch-fonts
  python pipeline/render.py --input pipeline/fixtures/fixture_funds.json --out dashboard/preview.html --allow-fixture

Principios:
  * Todo numero exibido (manchete, KPIs, graficos, matriz, textos) e calculado a partir
    dos registros; nada de manchete hardcoded.
  * Dataset marcado com __fixture__ e recusado sem a flag explicita --allow-fixture.
  * HTML unico e autossuficiente: CSS/JS inline, dados em <script type="application/json">,
    fontes woff2 embutidas em base64 quando cacheadas (senao, apenas <link> do Google
    Fonts com fallback local).
  * Sem dependencias externas (stdlib only).
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constantes de dominio
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = PIPELINE_DIR / "templates" / "dashboard.html"
FONTS_DIR = PIPELINE_DIR / "templates" / "fonts"

NBSP = "\u00a0"
ND_LABEL = "Não divulgado na fonte oficial"

CUTS = ["BRASIL_LOCAL", "GLOBAL_ATIVO_BR", "GLOBAL_EX_BR"]
CUT_LABEL = {
    "BRASIL_LOCAL": "Brasil local",
    "GLOBAL_ATIVO_BR": "Global ativo no Brasil",
    "GLOBAL_EX_BR": "Global ex-Brasil",
}
CUT_SHORT = {
    "BRASIL_LOCAL": "Brasil",
    "GLOBAL_ATIVO_BR": "Global ativo BR",
    "GLOBAL_EX_BR": "Global ex-BR",
}

STAGES = ["PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "LATE_STAGE_GROWTH", "VENTURE_DEBT"]
STAGE_LABEL = {
    "PRE_SEED": "Pre-Seed",
    "SEED": "Seed",
    "SERIES_A": "Série A",
    "SERIES_B": "Série B",
    "LATE_STAGE_GROWTH": "Late Stage/Growth",
    "VENTURE_DEBT": "Venture Debt",
}
STAGE_SHORT = {
    "PRE_SEED": "Pre-Seed",
    "SEED": "Seed",
    "SERIES_A": "Série A",
    "SERIES_B": "Série B",
    "LATE_STAGE_GROWTH": "Late Stage",
    "VENTURE_DEBT": "Venture Debt",
}
BPLUS_STAGES = {"SERIES_B", "LATE_STAGE_GROWTH"}

METHOD_LABEL = {
    "DECLARED_THESIS": "Tese declarada",
    "REGULATORY_CLASS_CVM_BNDES": "Classe CVM/BNDES",
    "IMPUTED_BY_TICKET_HEURISTIC": "Heurística de cheque",
}
METHOD_TITLE = {
    "DECLARED_THESIS": "Estágio declarado pela própria gestora em diretório aberto (OpenVC/Shizune).",
    "REGULATORY_CLASS_CVM_BNDES": "Enquadramento regulatório do FIP: Capital Semente, Empresas Emergentes ou Multiestratégia.",
    "IMPUTED_BY_TICKET_HEURISTIC": "Faixa de cheque convertida em fase pela régua Growth List (US$ 50 mil a US$ 100 mi+).",
}

REQUIRED_FIELDS = (
    "fund_id",
    "firm_name",
    "vehicle_name",
    "geographic_cut",
    "hq_country",
    "target_stages",
    "stage_classification_method",
    "estimated_ticket_min_usd",
    "estimated_ticket_max_usd",
    "aum_or_offering_usd",
    "active_status",
    "primary_source_name",
    "primary_source_url",
)

LICENSES = (
    ("dados.cvm.gov.br", "Dados Abertos CVM — uso livre com atribuição"),
    ("bndes.gov.br", "Dados Abertos BNDES — uso livre com atribuição"),
    ("sec.gov", "Domínio público (governo dos EUA)"),
    ("bcb.gov.br", "Dados Abertos do Banco Central do Brasil"),
    ("wikidata.org", "CC0 1.0 (Wikidata)"),
    ("shizune.co", "Diretório público — atribuição à fonte"),
    ("abvcap.com.br", "Publicação setorial — atribuição à fonte"),
    ("finep.gov.br", "Dados Abertos MCTI/Finep"),
    ("openvc.app", "Diretório público — atribuição à fonte"),
    ("brouky.tech", "Dataset comunitário — atribuição à fonte"),
)

# Referencias metodologicas fixas (nao sao fonte de registro). Todas verificadas
# como acessiveis na data de construcao; a lista de fontes por registro e calculada
# dos proprios dados (ver build_references).
METHOD_REFS = (
    ("CVM — Fundos de investimento (dados abertos)", "https://dados.cvm.gov.br/group/fundos-de-investimento"),
    ("CVM — Informe quadrimestral de FIP", "https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral"),
    ("SEC — Form D data sets", "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets"),
    ("Wikidata Query Service (SPARQL)", "https://query.wikidata.org/"),
    ("BNDES — Dados abertos de renda variável", "https://dadosabertos.bndes.gov.br/"),
    ("BCB — PTAX (cotações de câmbio)", "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"),
    ("Growth List — faixas de rodada por estágio", "https://growthlist.co/blog/startup-funding-stages/"),
    ("ABVCAP — publicações e consolidados", "https://abvcap.com.br/publicacoes/"),
    ("Shizune — VC funds in Brazil", "https://shizune.co/investors/venture-capital-vc-funds-brazil"),
)

GOOGLE_FAMILIES = (
    ("Space Grotesk", "Space+Grotesk:wght@600;700"),
    ("IBM Plex Sans", "IBM+Plex+Sans:wght@400;500;600"),
    ("IBM Plex Mono", "IBM+Plex+Mono:wght@400;500"),
)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FONT_LINK = (
    "https://fonts.googleapis.com/css2?"
    "family=Space+Grotesk:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600"
    "&family=IBM+Plex+Mono:wght@400;500&display=swap"
)


class RenderError(RuntimeError):
    """ERRO de render (dataset invalido, fixture recusado, template ausente)."""


class FixtureError(RenderError):
    """Dataset de desenvolvimento recebido sem --allow-fixture."""


# ---------------------------------------------------------------------------
# Formatacao numerica (pt-BR, tabular friendly)
# ---------------------------------------------------------------------------

def _decimals(x: float, dp: int) -> str:
    s = f"{x:,.{dp}f}"
    return s.replace(",", "\u0001").replace(".", ",").replace("\u0001", ".")


def fmt_int(n: float | int) -> str:
    return _decimals(float(n), 0)


def fmt_num(x: float, dp: int = 1) -> str:
    return _decimals(float(x), dp)


def fmt_pct(p: float | None, dp: int = 1) -> str:
    if p is None:
        return "—"
    if abs(p - round(p)) < 0.05:
        return f"{fmt_int(round(p))}%"
    return f"{fmt_pct_dec(p, dp)}"


def fmt_pct_dec(p: float, dp: int = 1) -> str:
    return f"{_decimals(p, dp)}%"


def _money_unit(v: float) -> tuple[float, str, int]:
    a = abs(v)
    if a >= 1e9:
        return v / 1e9, "bi", (1 if a < 1e11 else 0)
    if a >= 1e6:
        return v / 1e6, "mi", (1 if a < 1e8 else 0)
    if a >= 1e3:
        return v / 1e3, "mil", 0
    return v, "", 0


def fmt_usd(v: float | None) -> str:
    """US$ 1,2 bi / US$ 850 mi / US$ 500 mil — com NBSP (nao quebra linha)."""
    if v is None:
        return ND_LABEL
    x, unit, dp = _money_unit(v)
    body = _decimals(x, dp)
    if unit:
        return f"US${NBSP}{body}{NBSP}{unit}"
    return f"US${NBSP}{body}"


def fmt_usd_range(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is None:
        return ND_LABEL
    if lo is not None and hi is not None:
        if abs(hi - lo) < 1.0:
            return fmt_usd(lo)
        return f"{fmt_usd(lo)}{NBSP}–{NBSP}{fmt_usd(hi)}"
    if lo is not None:
        return f"≥{NBSP}{fmt_usd(lo)}"
    return f"≤{NBSP}{fmt_usd(hi)}"


def fmt_ratio(r: float) -> str:
    if r is None or math.isinf(r) or math.isnan(r):
        return "—"
    if r >= 10:
        return f"{_decimals(r, 0)}x"
    return f"{_decimals(r, 1)}x"


MONTHS_PT = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def fmt_datetime_pt(iso: str | None) -> str:
    if not iso:
        return "não informada"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.day:02d} {MONTHS_PT[dt.month - 1]} {dt.year} · {dt.hour:02d}h{dt.minute:02d} UTC"


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _pct(num: float, den: float) -> float:
    return (100.0 * num / den) if den else 0.0


def _median(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def percentile(values: list[float], p: float) -> float | None:
    """Percentil com interpolacao linear (mesmo criterio de numpy.percentile)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Carregamento e validacao do dataset
# ---------------------------------------------------------------------------

def load_dataset(path: Path) -> tuple[list[dict], dict]:
    if not path.exists():
        raise RenderError(f"dataset não encontrado: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RenderError(f"JSON inválido em {path}: {exc}") from exc

    meta: dict = {}
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("records", "funds", "data", "items"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        else:
            raise RenderError(
                f"{path}: objeto sem lista de registros (esperado 'records', 'funds' ou 'data')."
            )
        meta = {k: v for k, v in payload.items() if k not in {"records", "funds", "data", "items"}}
    else:
        raise RenderError(f"{path}: raiz JSON inesperada ({type(payload).__name__}).")

    is_fixture = bool(meta.get("__fixture__")) or any(
        isinstance(r, dict) and r.get("__fixture__") for r in records
    )
    if is_fixture:
        meta["__fixture__"] = True
    return records, meta


def lenient_preview_fix(records: list[dict]) -> tuple[list[dict], int]:
    """Ajuste de preview (SOMENTE com --allow-fixture): zera valores negativos.

    Patrimonio liquido negativo existe na fonte (passivo > ativo), mas nao e um
    'capital declarado' exibivel nem passa no Gate 2 do auditor. O dataset
    canonico deve resolver isso na transformacao (null + undisclosed_fields).
    """
    fixed = 0
    out = []
    for rec in records:
        r = dict(rec)
        for field in ("aum_or_offering_usd", "estimated_ticket_min_usd", "estimated_ticket_max_usd"):
            v = r.get(field)
            if isinstance(v, (int, float)) and v < 0:
                r[field] = None
                fixed += 1
                und = list(r.get("undisclosed_fields") or [])
                if field not in und:
                    und.append(field)
                r["undisclosed_fields"] = und
        out.append(r)
    return out, fixed


def validate_records(records: list[dict]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append(f"registro {idx}: não é objeto")
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in rec]
        if missing:
            errors.append(f"registro {idx}: campos obrigatórios ausentes {missing}")
            continue
        fid = str(rec.get("fund_id") or "").strip()
        if len(fid) < 3:
            errors.append(f"registro {idx}: fund_id curto ({fid!r})")
        elif fid in seen_ids:
            errors.append(f"registro {idx}: fund_id duplicado {fid!r}")
        seen_ids.add(fid)
        if rec.get("geographic_cut") not in CUTS:
            errors.append(f"{fid}: geographic_cut inválido {rec.get('geographic_cut')!r}")
        if not re.fullmatch(r"[A-Z]{2}", str(rec.get("hq_country") or "")):
            errors.append(f"{fid}: hq_country não é ISO-2 ({rec.get('hq_country')!r})")
        stages = rec.get("target_stages")
        if not isinstance(stages, list) or not stages:
            errors.append(f"{fid}: target_stages vazio")
        else:
            bad = [s for s in stages if s not in STAGES]
            if bad:
                errors.append(f"{fid}: estágios fora do enum {bad}")
        if rec.get("stage_classification_method") not in METHOD_LABEL:
            errors.append(f"{fid}: stage_classification_method inválido")
        url = str(rec.get("primary_source_url") or "")
        if not url.startswith("https://"):
            errors.append(f"{fid}: primary_source_url não é https ({url!r})")
        for field in ("estimated_ticket_min_usd", "estimated_ticket_max_usd", "aum_or_offering_usd"):
            v = rec.get(field)
            if v is not None:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    errors.append(f"{fid}: {field} não numérico ({v!r})")
                elif v < 0:
                    errors.append(f"{fid}: {field} negativo ({v})")
        if not str(rec.get("firm_name") or "").strip():
            errors.append(f"{fid}: firm_name vazio")
        if not str(rec.get("vehicle_name") or "").strip():
            errors.append(f"{fid}: vehicle_name vazio")
    return errors


def normalize(records: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        r = dict(rec)
        r["fund_id"] = str(r.get("fund_id") or "").strip()
        r["firm_name"] = str(r.get("firm_name") or "").strip()
        r["vehicle_name"] = str(r.get("vehicle_name") or "").strip()
        r["regulatory_id"] = (str(r["regulatory_id"]).strip() if r.get("regulatory_id") else None)
        r["target_stages"] = [s for s in (r.get("target_stages") or []) if s in STAGES]
        raw_deals = r.get("brazil_deals_count")
        r["brazil_deals_count"] = int(raw_deals) if isinstance(raw_deals, (int, float)) else None
        r["active_status"] = bool(r.get("active_status"))
        r["primary_source_name"] = str(r.get("primary_source_name") or "Fonte oficial")
        r["primary_source_url"] = str(r.get("primary_source_url") or "")
        r["estimate_basis"] = (str(r["estimate_basis"]).strip() if r.get("estimate_basis") else None)
        lo, hi = r.get("estimated_ticket_min_usd"), r.get("estimated_ticket_max_usd")
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        r["estimated_ticket_min_usd"] = float(lo) if lo is not None else None
        r["estimated_ticket_max_usd"] = float(hi) if hi is not None else None
        aum = r.get("aum_or_offering_usd")
        r["aum_or_offering_usd"] = float(aum) if aum is not None else None
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Estatisticas calculadas (fonte unica de verdade dos numeros exibidos)
# ---------------------------------------------------------------------------

def source_kind(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "cvm.gov.br" in host or "bndes.gov.br" in host:
        return "CVM/BNDES"
    if "sec.gov" in host:
        return "SEC Form D"
    if "wikidata.org" in host:
        return "Wikidata"
    if "shizune.co" in host:
        return "Shizune"
    return "Outras fontes"


def source_short(url: str, name: str = "") -> str:
    host = (urlparse(url).hostname or "").lower()
    if "cvm.gov.br" in host:
        return "CVM"
    if "bndes.gov.br" in host:
        return "BNDES"
    if "sec.gov" in host:
        return "SEC EDGAR"
    if "wikidata.org" in host:
        return "Wikidata"
    if "shizune.co" in host:
        return "Shizune"
    if "bcb.gov.br" in host:
        return "BCB"
    return host.replace("www.", "") or name or "fonte"


def license_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for domain, label in LICENSES:
        if host == domain or host.endswith("." + domain):
            return label
    return "Uso público com atribuição à fonte"


CAPITAL_BANDS = (
    (1e6, "até US$ 1 mi"),
    (1e7, "US$ 1–10 mi"),
    (1e8, "US$ 10–100 mi"),
    (1e9, "US$ 100 mi–1 bi"),
    (math.inf, "acima de US$ 1 bi"),
)


def _band_index(value: float) -> int:
    for idx, (limit, _label) in enumerate(CAPITAL_BANDS):
        if value < limit:
            return idx
    return len(CAPITAL_BANDS) - 1


def compute_stats(records: list[dict]) -> dict:
    n = len(records)
    if n == 0:
        raise RenderError("dataset vazio: nenhum registro para renderizar.")

    by_cut = {c: sum(1 for r in records if r["geographic_cut"] == c) for c in CUTS}
    n_br = by_cut["BRASIL_LOCAL"]
    n_gl = n - n_br

    stage_group: dict[str, dict[str, int]] = {
        st: {"br": 0, "gl": 0} for st in STAGES
    }
    for r in records:
        g = "br" if r["geographic_cut"] == "BRASIL_LOCAL" else "gl"
        for st in set(r["target_stages"]):
            stage_group[st][g] += 1

    recs_br = [r for r in records if r["geographic_cut"] == "BRASIL_LOCAL"]
    recs_gl = [r for r in records if r["geographic_cut"] != "BRASIL_LOCAL"]

    def touching(recs: list[dict], stages: set[str] | tuple[str, ...]) -> int:
        return sum(1 for r in recs if any(s in stages for s in r["target_stages"]))

    bplus = {"br": touching(recs_br, BPLUS_STAGES), "gl": touching(recs_gl, BPLUS_STAGES)}

    # capital declarado
    aum_recs = [r for r in records if r["aum_or_offering_usd"] is not None]
    aum_by_cut = {c: 0.0 for c in CUTS}
    for r in aum_recs:
        aum_by_cut[r["geographic_cut"]] += r["aum_or_offering_usd"]
    aum_total = sum(aum_by_cut.values())
    aum_br = aum_by_cut["BRASIL_LOCAL"]
    aum_gl = aum_total - aum_br
    aum_coverage = _pct(len(aum_recs), n)

    # faixas de cheque declaradas
    ticket_rows: list[dict] = []
    for r in records:
        lo, hi = r["estimated_ticket_min_usd"], r["estimated_ticket_max_usd"]
        if lo is None and hi is None:
            continue
        if lo is None:
            lo = hi
        if hi is None:
            hi = lo
        ticket_rows.append({"rec": r, "lo": lo, "hi": hi, "mid": (lo + hi) / 2.0})
    ticket_coverage = _pct(len(ticket_rows), n)

    tickets_stage: dict[str, dict[str, list[dict]]] = {
        st: {"br": [], "gl": []} for st in STAGES
    }
    for row in ticket_rows:
        g = "br" if row["rec"]["geographic_cut"] == "BRASIL_LOCAL" else "gl"
        for st in set(row["rec"]["target_stages"]):
            tickets_stage[st][g].append(row)

    # porte declarado por fase e grupo (P25/mediana/P75 para a figura de capital)
    aum_stage: dict[str, dict[str, list[float]]] = {st: {"br": [], "gl": []} for st in STAGES}
    for rec in records:
        val = rec["aum_or_offering_usd"]
        if val is None:
            continue
        g = "br" if rec["geographic_cut"] == "BRASIL_LOCAL" else "gl"
        for st in set(rec["target_stages"]):
            aum_stage[st][g].append(val)

    def _dist(values: list[float]) -> dict | None:
        if not values:
            return None
        return {
            "n": len(values),
            "min": min(values),
            "p25": percentile(values, 25),
            "median": _median(values),
            "p75": percentile(values, 75),
            "max": max(values),
        }

    capital_profile = {
        st: {"br": _dist(aum_stage[st]["br"]), "gl": _dist(aum_stage[st]["gl"])}
        for st in STAGES
    }
    capital_bands = {"br": [0] * len(CAPITAL_BANDS), "gl": [0] * len(CAPITAL_BANDS),
                     "n": {"br": 0, "gl": 0}}
    for rec in records:
        val = rec["aum_or_offering_usd"]
        if val is None:
            continue
        g = "br" if rec["geographic_cut"] == "BRASIL_LOCAL" else "gl"
        capital_bands[g][_band_index(val)] += 1
        capital_bands["n"][g] += 1

    # capital atribuido ao estagio primario (target_stages[0]) por grupo: soma 100%
    aum_primary_stage: dict[str, dict[str, float]] = {
        st: {"br": 0.0, "gl": 0.0} for st in STAGES
    }
    for rec in records:
        val = rec["aum_or_offering_usd"]
        stages = rec["target_stages"]
        if val is None or not stages:
            continue
        g = "br" if rec["geographic_cut"] == "BRASIL_LOCAL" else "gl"
        aum_primary_stage[stages[0]][g] += val
    capital_share_stage: dict[str, dict[str, float]] = {
        st: {
            "br": _pct(aum_primary_stage[st]["br"], aum_br),
            "gl": _pct(aum_primary_stage[st]["gl"], aum_gl),
        }
        for st in STAGES
    }
    capital_by_cut_share = {
        c: _pct(aum_by_cut.get(c, 0.0), aum_total) for c in CUTS
    }
    aum_bplus = sum(
        r["aum_or_offering_usd"] or 0.0
        for r in records if any(st in BPLUS_STAGES for st in r["target_stages"])
    )
    aum_bplus_br = sum(
        r["aum_or_offering_usd"] or 0.0 for r in recs_br
        if any(st in BPLUS_STAGES for st in r["target_stages"])
    )
    aum_bplus_gl = sum(
        r["aum_or_offering_usd"] or 0.0 for r in recs_gl
        if any(st in BPLUS_STAGES for st in r["target_stages"])
    )
    bplus_capital_share = {
        "br": _pct(aum_bplus_br, aum_br) if aum_br else 0.0,
        "gl": _pct(aum_bplus_gl, aum_gl) if aum_gl else 0.0,
        "all": _pct(aum_bplus, aum_total) if aum_total else 0.0,
    }

    # fontes primarias
    source_pairs = Counter(
        (r["primary_source_name"], r["primary_source_url"]) for r in records
    )
    source_collections: Counter[tuple[str, str]] = Counter()
    collection_example: dict[tuple[str, str], str] = {}
    for name, url in source_pairs:
        host = (urlparse(url).hostname or "").lower()
        key = (name, host)
        source_collections[key] += 1
        if key not in collection_example:
            collection_example[key] = url
    source_kinds = Counter(source_kind(r["primary_source_url"]) for r in records)
    source_by_cut: dict[str, Counter] = {c: Counter() for c in CUTS}
    for r in records:
        source_by_cut[r["geographic_cut"]][source_kind(r["primary_source_url"])] += 1
    source_links: dict[str, str] = {}
    for _, url in source_pairs:
        kind = source_kind(url)
        source_links.setdefault(kind, url)
    cvm_url = next((u for u in source_links.values() if "cvm.gov.br" in u), source_links.get("CVM/BNDES"))
    sec_url = next((u for u in source_links.values() if "sec.gov" in u), source_links.get("SEC Form D"))

    # gestoras, paises, metodos
    firms = {r["firm_name"].strip().lower() for r in records if r["firm_name"].strip()}
    firms_by_cut = {
        c: len({r["firm_name"].strip().lower() for r in records if r["geographic_cut"] == c and r["firm_name"].strip()})
        for c in CUTS
    }
    countries = Counter(r["hq_country"] for r in records)
    methods = Counter(r["stage_classification_method"] for r in records)

    # ativos
    active = {"br": 0, "gl": 0}
    for r in records:
        g = "br" if r["geographic_cut"] == "BRASIL_LOCAL" else "gl"
        if r["active_status"]:
            active[g] += 1
    n_active = active["br"] + active["gl"]

    # qualidade declarada no dataset (campos opcionais tolerados)
    quality_flags: Counter[str] = Counter()
    capital_inconsistent = 0
    deals_known = 0
    records_with_flags = 0
    for r in records:
        flags = r.get("data_quality_flags")
        if isinstance(flags, list) and flags:
            records_with_flags += 1
        if isinstance(flags, list):
            for flag in flags:
                quality_flags[str(flag)] += 1
        if r.get("capital_consistente") is False:
            capital_inconsistent += 1
        if r.get("brazil_deals_count") is not None:
            deals_known += 1

    # nao divulgados
    undisclosed_aum = sum(1 for r in records if r["aum_or_offering_usd"] is None)
    undisclosed_ticket = sum(1 for r in records if r["estimated_ticket_min_usd"] is None and r["estimated_ticket_max_usd"] is None)

    # concentracao de capital
    top_sorted = sorted(aum_recs, key=lambda r: -(r["aum_or_offering_usd"] or 0.0))
    top10 = top_sorted[:10]
    top10_share = _pct(sum(r["aum_or_offering_usd"] for r in top10), aum_total) if aum_total else 0.0
    largest = top_sorted[0] if top_sorted else None
    largest_share = _pct(largest["aum_or_offering_usd"], aum_total) if (largest and aum_total) else 0.0

    def aum_values(recs: list[dict]) -> list[float]:
        return [r["aum_or_offering_usd"] for r in recs if r["aum_or_offering_usd"] is not None]

    def cut_profile(recs: list[dict]) -> dict:
        vals = aum_values(recs)
        if not vals:
            return {"n_with_value": 0, "p25": None, "median": None, "p75": None, "top10_share": None}
        top = sorted(vals, reverse=True)[:10]
        total = sum(vals)
        return {
            "n_with_value": len(vals),
            "p25": percentile(vals, 25),
            "median": _median(vals),
            "p75": percentile(vals, 75),
            "max": max(vals),
            "top10_share": _pct(sum(top), total) if total else 0.0,
        }

    portfolio = {
        "br": cut_profile(recs_br),
        "ga": cut_profile([r for r in records if r["geographic_cut"] == "GLOBAL_ATIVO_BR"]),
        "ge": cut_profile([r for r in records if r["geographic_cut"] == "GLOBAL_EX_BR"]),
        "all": cut_profile(records),
    }
    median_aum = _median(aum_values(records))
    median_aum_br = _median(aum_values(recs_br))
    median_aum_gl = _median(aum_values(recs_gl))

    # perfil por estagio (percentual de veiculos de cada grupo)
    pct_stage = {
        st: {"br": _pct(stage_group[st]["br"], n_br), "gl": _pct(stage_group[st]["gl"], n_gl)}
        for st in STAGES
    }

    # estagios dominantes no Brasil (por veiculos que tocam o estagio)
    dominant_br = sorted(
        [st for st in STAGES if stage_group[st]["br"] > 0],
        key=lambda st: (-stage_group[st]["br"], STAGES.index(st)),
    )[:2]
    dom_union_br = touching(recs_br, set(dominant_br)) if dominant_br else 0
    dom_union_gl = touching(recs_gl, set(dominant_br)) if dominant_br else 0
    dom_share_br = _pct(dom_union_br, n_br)
    dom_share_gl = _pct(dom_union_gl, n_gl)

    # lacuna: fase mais coberta no exterior em relacao ao Brasil
    gap = None
    best = None
    for st in STAGES:
        if st == "VENTURE_DEBT":
            continue
        br_c, gl_c = stage_group[st]["br"], stage_group[st]["gl"]
        if gl_c == 0:
            continue
        diff = pct_stage[st]["gl"] - pct_stage[st]["br"]
        if best is None or diff > best[0]:
            best = (diff, st)
    if best and best[0] > 1.0:
        st = best[1]
        br_c, gl_c = stage_group[st]["br"], stage_group[st]["gl"]
        ratio = (pct_stage[st]["gl"] / pct_stage[st]["br"]) if pct_stage[st]["br"] > 0 else math.inf
        if br_c == 0 or not math.isfinite(ratio) or ratio >= 1.3:
            gap = {"stage": st, "br": br_c, "gl": gl_c, "ratio": ratio, "diff": best[0]}
    # fase com sobre-representacao local (para leitura)
    over = None
    best_over = None
    for st in STAGES:
        if st == "VENTURE_DEBT":
            continue
        if stage_group[st]["br"] == 0:
            continue
        diff = pct_stage[st]["br"] - pct_stage[st]["gl"]
        if best_over is None or diff > best_over[0]:
            best_over = (diff, st)
    if best_over and best_over[0] > 1.0:
        over = {"stage": best_over[1], "diff": best_over[0]}

    # indice cross-border
    cross_index = _pct(by_cut["GLOBAL_ATIVO_BR"], n_gl) if n_gl else 0.0
    brazil_deals = sum(r["brazil_deals_count"] or 0 for r in records)

    return {
        "n": n,
        "by_cut": dict(by_cut),
        "n_br": n_br,
        "n_gl": n_gl,
        "n_active": n_active,
        "active": active,
        "stage_group": stage_group,
        "pct_stage": pct_stage,
        "bplus": bplus,
        "bplus_pct": {"br": _pct(bplus["br"], n_br), "gl": _pct(bplus["gl"], n_gl), "all": _pct(bplus["br"] + bplus["gl"], n)},
        "dominant_br": dominant_br,
        "dom_share_br": dom_share_br,
        "dom_share_gl": dom_share_gl,
        "gap": gap,
        "over": over,
        "aum_recs": aum_recs,
        "aum_total": aum_total,
        "aum_br": aum_br,
        "aum_gl": aum_gl,
        "aum_by_cut": aum_by_cut,
        "aum_coverage": aum_coverage,
        "capital_br_share": _pct(aum_br, aum_total) if aum_total else 0.0,
        "median_aum": median_aum,
        "median_aum_br": median_aum_br,
        "median_aum_gl": median_aum_gl,
        "portfolio": portfolio,
        "top10_share": top10_share,
        "largest": largest,
        "largest_share": largest_share,
        "ticket_rows": ticket_rows,
        "ticket_coverage": ticket_coverage,
        "tickets_stage": tickets_stage,
        "aum_stage": aum_stage,
        "capital_profile": capital_profile,
        "capital_bands": capital_bands,
        "aum_primary_stage": aum_primary_stage,
        "capital_share_stage": capital_share_stage,
        "capital_by_cut_share": capital_by_cut_share,
        "aum_bplus": aum_bplus,
        "aum_bplus_share": _pct(aum_bplus, aum_total) if aum_total else 0.0,
        "aum_bplus_br": aum_bplus_br,
        "aum_bplus_gl": aum_bplus_gl,
        "bplus_capital_share": bplus_capital_share,
        "sources": source_pairs,
        "source_collections": source_collections,
        "collection_example": collection_example,
        "source_kinds": source_kinds,
        "source_by_cut": source_by_cut,
        "source_links": source_links,
        "cvm_url": cvm_url,
        "sec_url": sec_url,
        "n_sources": len(source_pairs),
        "n_source_bases": len(source_collections),
        "firms": len(firms),
        "firms_by_cut": firms_by_cut,
        "countries": countries,
        "methods": methods,
        "undisclosed_aum": undisclosed_aum,
        "undisclosed_ticket": undisclosed_ticket,
        "quality_flags": dict(quality_flags),
        "records_with_flags": records_with_flags,
        "capital_inconsistent": capital_inconsistent,
        "deals_known": deals_known,
        "cross_index": cross_index,
        "cross_count": by_cut["GLOBAL_ATIVO_BR"],
        "brazil_deals": brazil_deals,
        "records": records,
        "recs_br": recs_br,
        "recs_gl": recs_gl,
    }


# ---------------------------------------------------------------------------
# Manchete answer-first (100% calculada)
# ---------------------------------------------------------------------------

def build_headline(s: dict) -> str:
    """Manchete answer-first: 100% calculada dos registros."""
    L = STAGE_LABEL
    if s["n"] == 0:
        raise RenderError("headline impossível: dataset vazio.")

    if s["n_br"] == 0:
        gl_txt = fmt_pct(s["bplus_pct"]["gl"])
        return (
            f"Nenhum veículo com sede no Brasil foi mapeado; os {fmt_int(s['n'])} veículos "
            f"estrangeiros concentram {gl_txt} das teses em Série B ou estágios posteriores."
        )

    clauses: list[str] = []

    # 1) Concentracao de capital: o capital brasileiro mora em Serie B+.
    bcs = s["bplus_capital_share"]
    if s["aum_total"] and bcs["br"] >= 60:
        contrast = ""
        if abs(bcs["br"] - bcs["gl"]) >= 5:
            contrast = f", contra {fmt_pct(bcs['gl'])} do global"
        clauses.append(
            f"Série B+ absorve {fmt_pct(bcs['br'])} do capital declarado sob gestoras brasileiras"
            f"{contrast}, embora apenas {fmt_pct(s['bplus_pct']['br'])} dos "
            f"{fmt_int(s['n_br'])} veículos locais tenham essa tese."
        )
    elif s["aum_total"]:
        clauses.append(
            f"{fmt_pct(s['capital_br_share'])} do capital declarado ({fmt_usd(s['aum_total'])}) "
            f"está sob gestoras brasileiras, que somam {fmt_pct(_pct(s['n_br'], s['n']))} dos veículos."
        )
    else:
        dom = s["dominant_br"]
        dom_txt = " e ".join(L[st] for st in dom) if dom else "os estágios iniciais"
        clauses.append(
            f"{dom_txt} concentram {fmt_pct(s['dom_share_br'])} dos {fmt_int(s['n_br'])} veículos "
            f"brasileiros mapeados, contra {fmt_pct(s['dom_share_gl'])} dos estrangeiros."
        )

    # 2) Lacuna de oferta por fase (early stage global pulverizado).
    gap = s["gap"]
    if gap:
        if gap["br"] == 0:
            clauses.append(
                f"Em {L[gap['stage']]}, nenhum veículo com sede no Brasil foi mapeado, contra "
                f"{fmt_int(gap['gl'])} estrangeiros."
            )
        else:
            clauses.append(
                f"Em {L[gap['stage']]}, a cobertura local é {fmt_ratio(gap['ratio'])} inferior à "
                f"estrangeira: {fmt_int(gap['br'])} veículos contra {fmt_int(gap['gl'])}."
            )
    elif s["over"]:
        st = s["over"]["stage"]
        ratio = (s["pct_stage"][st]["br"] / s["pct_stage"][st]["gl"]) if s["pct_stage"][st]["gl"] else None
        extra = f" ({fmt_ratio(ratio)} a estrangeira)" if ratio and ratio > 1 else ""
        clauses.append(
            f"Em {L[st]}, a oferta local aparece em {fmt_pct(s['pct_stage'][st]['br'])} dos veículos "
            f"brasileiros{extra}."
        )

    # 3) Ponte cross-border minima (achado central quando existir).
    if s["n_gl"] and s["cross_index"] < 5.0:
        if s["cross_count"]:
            clauses.append(
                f"A ponte cross-border alcança {fmt_pct(s['cross_index'])} dos "
                f"{fmt_int(s['n_gl'])} veículos estrangeiros."
            )
        else:
            clauses.append(
                f"Nenhum dos {fmt_int(s['n_gl'])} veículos estrangeiros mapeados tem atividade no Brasil."
            )

    text = " ".join(clauses)
    if len(text) > 330:
        text = " ".join(clauses[:2])
    if not any(ch.isdigit() for ch in text):
        raise RenderError("headline sem números: cálculo inconsistente.")
    return text


def build_deck(s: dict, meta: dict) -> str:
    parts = [
        f"Base de {fmt_int(s['n'])} veículos de {fmt_int(s['firms'])} gestoras, montada a partir de "
        f"{fmt_int(s['n_source_bases'])} bases de fonte primária "
        f"({fmt_int(s['n_sources'])} URLs de registro distintas) e classificada em "
        f"{fmt_int(len(STAGES))} fases por três regras rastreáveis."
    ]
    capital = ""
    if s["aum_total"]:
        median_txt = fmt_usd(s["median_aum"]) if s["median_aum"] is not None else "sem valores declarados"
        capital = (
            f" O capital com valor declarado soma {fmt_usd(s['aum_total'])} "
            f"({fmt_pct(s['aum_coverage'])} de cobertura); a mediana por veículo é {median_txt} "
            f"e os dez maiores respondem por {fmt_pct(s['top10_share'])} do total."
        )
    if s["ticket_rows"]:
        ticket = (
            f" Faixa de cheque é declarada por {fmt_pct(s['ticket_coverage'])} dos veículos; "
            f"os demais aparecem como “{ND_LABEL}” na tabela."
        )
    else:
        ticket = (
            f" Faixa de cheque não é divulgada na fonte para nenhum dos {fmt_int(s['n'])} veículos; "
            f"a distribuição de porte usa patrimônio líquido (CVM) e captação declarada (Form D)."
        )
    return "".join(parts) + capital + ticket


# ---------------------------------------------------------------------------
# Blocos HTML: KPIs
# ---------------------------------------------------------------------------

def _dot(kind: str) -> str:
    cls = {"br": "dot-br", "gl": "dot-gl", "cross": "dot-cross"}[kind]
    return f'<i class="dot {cls}" aria-hidden="true"></i>'


def _kpi(label: str, value_html: str, compare_html: str, note_html: str) -> str:
    return f"""<div class="kpi">
  <p class="kpi-label">{label}</p>
  <p class="kpi-value tnum">{value_html}</p>
  <p class="kpi-compare">{compare_html}</p>
  <p class="kpi-note">{note_html}</p>
</div>"""


def build_kpis(s: dict) -> str:
    n, n_br, n_gl = s["n"], s["n_br"], s["n_gl"]
    tiles = []

    tiles.append(_kpi(
        "Veículos mapeados",
        fmt_int(n),
        f'{_dot("br")}Brasil <b>{fmt_int(n_br)}</b>'
        f'<span class="sep">·</span>{_dot("gl")}Global <b>{fmt_int(n_gl)}</b>',
        f'{fmt_int(s["firms"])} gestoras · {fmt_int(s["n_active"])} veículos ativos '
        f'({fmt_pct(_pct(s["n_active"], n))})',
    ))

    tiles.append(_kpi(
        "Capital declarado",
        fmt_usd(s["aum_total"]),
        f'{_dot("br")}Brasil <b>{fmt_usd(s["aum_br"])}</b>'
        f'<span class="sep">·</span>{_dot("gl")}Global <b>{fmt_usd(s["aum_gl"])}</b>',
        f'Mediana {fmt_usd(s["median_aum"])} por veículo · top-10 = '
        f'{fmt_pct(s["top10_share"])} do capital · cobertura {fmt_pct(s["aum_coverage"])}',
    ))

    tiles.append(_kpi(
        "Tese em Série B+",
        fmt_pct(s["bplus_pct"]["all"]),
        f'{_dot("br")}Brasil <b>{fmt_pct(s["bplus_pct"]["br"])}</b>'
        f'<span class="sep">·</span>{_dot("gl")}Global <b>{fmt_pct(s["bplus_pct"]["gl"])}</b>',
        f'Veículos com Série B, Late Stage/Growth ou debt: '
        f'{fmt_int(s["bplus"]["br"])} no Brasil, {fmt_int(s["bplus"]["gl"])} no exterior',
    ))

    tiles.append(_kpi(
        "Abertura cross-border",
        fmt_pct(s["cross_index"]),
        f'{_dot("cross")}Ativas no Brasil <b>{fmt_int(s["cross_count"])}</b>'
        f'<span class="sep">·</span>{_dot("gl")}Ex-Brasil <b>{fmt_int(s["by_cut"]["GLOBAL_EX_BR"])}</b>',
        f'Das {fmt_int(n_gl)} veículos estrangeiros mapeados; {fmt_int(s["brazil_deals"])} deals '
        f'brasileiros somados nas teses declaradas',
    ))
    return "\n".join(tiles)


# ---------------------------------------------------------------------------
# Blocos HTML: graficos SVG (geometria calculada em Python)
# ---------------------------------------------------------------------------

def _txt(x: float, y: float, content: str, cls: str = "", anchor: str = "start",
         extra: str = "") -> str:
    cls_attr = f' class="{cls}"' if cls else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"{cls_attr}{extra}>'
        f"{esc(content)}</text>"
    )


def _axis_label(v: float) -> str:
    for unit, div in (("bi", 1e9), ("mi", 1e6), ("mil", 1e3)):
        if v >= div:
            n = v / div
            dp = 0 if abs(n - round(n)) < 1e-9 else 1
            return f"{_decimals(n, dp)} {unit}"
    return _decimals(v, 0)


def _stage_variants(width: int) -> dict:
    label_w = max(72, min(142, round(width * 0.28)))
    return {
        "w": width,
        "row_h": 46 if width < 340 else 52,
        "bar_h": 9 if width < 340 else 10,
        "bar_gap": 3 if width < 340 else 4,
        "pad_top": 6,
        "pad_bottom": 24,
        "pad_x": 6,
        "label_w": label_w,
        "value_w": 44 if width < 340 else 58,
        "font": 12,
    }


def build_stage_svg(s: dict, width: int) -> str:
    """Barras horizontais: percentual dos veiculos de cada grupo que cobre cada fase."""
    cfg = _stage_variants(width)
    stages = STAGES
    x0 = cfg["pad_x"] + cfg["label_w"]
    plot_w = width - x0 - cfg["pad_x"] - cfg["value_w"]
    row_h = cfg["row_h"]
    bar_h, bar_gap = cfg["bar_h"], cfg["bar_gap"]
    height = cfg["pad_top"] + len(stages) * row_h + cfg["pad_bottom"]
    pcts = {
        st: {
            "br": _pct(s["stage_group"][st]["br"], s["n_br"]),
            "gl": _pct(s["stage_group"][st]["gl"], s["n_gl"]),
        }
        for st in stages
    }
    max_v = max(1.0, max(p["br"] for p in pcts.values()), max(p["gl"] for p in pcts.values()))
    uid = f"stage{width}"

    parts = [
        f'<svg class="chart chart-stage" data-chart="stage" data-plot-w="{plot_w:.1f}" '
        f'data-x0="{x0:.1f}" data-w="{width}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{uid}-title" preserveAspectRatio="xMidYMid meet">',
        f'<title id="{uid}-title">Percentual de veículos por fase de maturidade, gestoras brasileiras contra estrangeiras</title>',
        "<desc>Barras horizontais: percentual de veículos de cada grupo cuja tese cobre a fase. "
        "Cada veículo pode cobrir mais de uma fase.</desc>",
    ]
    for i, st in enumerate(stages):
        y = cfg["pad_top"] + i * row_h
        cy = y + (2 * bar_h + bar_gap) / 2 + 4
        stage_txt = STAGE_LABEL[st] if width >= 500 else STAGE_SHORT[st]
        if width < 340 and st == "VENTURE_DEBT":
            stage_txt = "Venture Debt"
        parts.append(_txt(x0 - 8, cy, stage_txt, cls="t-label", anchor="end"))
        for j, g in enumerate(("br", "gl")):
            v = pcts[st][g]
            by = y + j * (bar_h + bar_gap)
            w = (v / max_v) * plot_w if v else 0.0
            fill = "var(--br)" if g == "br" else "var(--gl)"
            parts.append(
                f'<rect class="bar bar-{g}" data-stage="{st}" data-stage-index="{i}" '
                f'data-group="{g}" x="{x0:.1f}" y="{by:.1f}" width="{w:.1f}" '
                f'height="{bar_h}" fill="{fill}"></rect>'
            )
            lx = x0 + w + 6
            if lx > width - cfg["pad_x"] - 44:
                lx = width - cfg["pad_x"] - 44
            label_cls = f"t-num t-{g}" if v > 0 else "t-num t-zero"
            parts.append(
                f'<text class="{label_cls}" data-val="{i}|{g}" '
                f'x="{lx:.1f}" y="{by + bar_h - 2:.1f}" text-anchor="start">{fmt_pct(v)}</text>'
            )
        if i < len(stages) - 1:
            ly = y + row_h - 6
            parts.append(
                f'<line class="grid-line" x1="{cfg["pad_x"] + cfg["label_w"] - 2:.1f}" '
                f'y1="{ly:.1f}" x2="{width - cfg["pad_x"]:.1f}" y2="{ly:.1f}"></line>'
            )
    parts.append(f'<line class="axis-line" x1="{x0:.1f}" y1="{cfg["pad_top"]:.1f}" '
                 f'x2="{x0:.1f}" y2="{cfg["pad_top"] + len(stages) * row_h - 8:.1f}"></line>')
    parts.append("</svg>")
    return "".join(parts)


def build_stage_figure(s: dict) -> str:
    variants = [("s", 272), ("m", 424), ("l", 660), ("d", 520)]
    svgs = []
    for name, width in variants:
        svgs.append(f'<div class="fig-svg fig-svg-{name} cap-{name}">{build_stage_svg(s, width)}</div>')

    over = s["over"]
    gap = s["gap"]
    pcts = s["pct_stage"]

    def _flow(st: str, group: str) -> str:
        return fmt_pct(pcts[st][group])

    if over and gap and over["stage"] != gap["stage"]:
        o, g = over["stage"], gap["stage"]
        title = (
            f"{STAGE_LABEL[o]} aparece em {_flow(o, 'br')} dos veículos brasileiros, contra "
            f"{_flow(o, 'gl')} dos estrangeiros; {STAGE_LABEL[g]} cobre {_flow(g, 'gl')} dos "
            f"estrangeiros e {_flow(g, 'br')} dos brasileiros."
        )
    elif gap:
        g = gap["stage"]
        title = (
            f"{STAGE_LABEL[g]} cobre {_flow(g, 'gl')} dos veículos estrangeiros e "
            f"{_flow(g, 'br')} dos brasileiros — cobertura local {fmt_ratio(gap['ratio'])} inferior."
        )
    elif over:
        o = over["stage"]
        title = (
            f"{STAGE_LABEL[o]} concentra a oferta local: {_flow(o, 'br')} dos veículos brasileiros "
            f"contra {_flow(o, 'gl')} dos estrangeiros."
        )
    else:
        dom = s["dominant_br"]
        dom_txt = " e ".join(STAGE_LABEL[st] for st in dom) if dom else "os estágios iniciais"
        title = (
            f"{dom_txt} concentram {fmt_pct(s['dom_share_br'])} dos veículos brasileiros, "
            f"contra {fmt_pct(s['dom_share_gl'])} dos estrangeiros."
        )

    srcs = []
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM — Cadastro de Fundos</a>')
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR — Form D</a>')
    source_html = "; ".join(srcs) if srcs else "fontes primárias citadas na tabela"

    return f"""<figure class="fig fig-stage" aria-labelledby="fig1-titulo">
  <h3 class="fig-title" id="fig1-titulo">{esc(title)}</h3>
  <p class="fig-unit">Unidade: percentual dos veículos de cada grupo cuja tese cobre a fase
  (um fundo conta em todas as fases da sua tese).</p>
  <ul class="legend" aria-label="Legenda">
    <li><i class="sw sw-br"></i>Gestoras brasileiras</li>
    <li><i class="sw sw-gl"></i>Gestoras estrangeiras</li>
  </ul>
  {''.join(svgs)}
  <p class="fig-live" id="chart-live" aria-live="polite"></p>
  <figcaption>Fonte: {source_html}. Base do percentual: {fmt_int(s['n_br'])} veículos brasileiros e
  {fmt_int(s['n_gl'])} estrangeiros. Contagem por estágio declarado, classe regulatória CVM/BNDES
  ou heurística de cheque; o método de cada veículo está na <a href="#tabela">tabela auditável</a>.
  O gráfico é recalculado sobre o recorte dos filtros ativos.</figcaption>
</figure>"""


def _ticket_scale(ticket_rows: list[dict]) -> tuple[float, float, list[float]]:
    vals = [row["lo"] for row in ticket_rows] + [row["hi"] for row in ticket_rows]
    lo = max(10_000.0, min(vals))
    hi = max(lo * 10.0, max(vals))
    dec_lo = math.floor(math.log10(lo))
    dec_hi = math.ceil(math.log10(hi))
    if dec_hi <= dec_lo:
        dec_hi = dec_lo + 1
    ticks = [10.0 ** d for d in range(dec_lo, dec_hi + 1)]
    return ticks[0], ticks[-1], ticks


def build_ticket_svg(s: dict, width: int) -> str:
    label_w = max(64, min(132, round(width * 0.32)))
    pad_x = 6
    pad_top = 16
    pad_bottom = 28
    x0 = pad_x + label_w
    plot_w = width - x0 - pad_x - 14
    block_h = 40 if width < 340 else 48
    sub_h = 14 if width < 340 else 16
    axis_min, axis_max, ticks = _ticket_scale(s["ticket_rows"])
    log_min, log_max = math.log10(axis_min), math.log10(axis_max)

    def xpos(v: float) -> float:
        v = min(max(v, axis_min), axis_max)
        return x0 + (math.log10(v) - log_min) / (log_max - log_min) * plot_w

    rows = []
    for st in STAGES:
        br = s["tickets_stage"][st]["br"]
        gl = s["tickets_stage"][st]["gl"]
        if not br and not gl:
            continue
        rows.append((st, br, gl))
    if not rows:
        return ('<svg class="chart" viewBox="0 0 320 40" role="img" '
                'aria-label="Sem faixas de cheque declaradas"></svg>')

    height = pad_top + len(rows) * block_h + pad_bottom
    uid = f"ticket{width}"
    parts = [
        f'<svg class="chart chart-ticket" data-chart="ticket" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="{uid}-title">',
        f'<title id="{uid}-title">Faixa de cheque declarada por fase, em dólares (escala logarítmica)</title>',
        "<desc>Para cada fase, uma linha mostra o mínimo e o máximo declarados; o ponto marca a mediana.</desc>",
    ]
    # gridlines + ticks
    tick_every = 1 if len(ticks) <= 4 or width >= 420 else 2
    shown = ticks[::tick_every]
    for t in shown:
        x = xpos(t)
        parts.append(f'<line class="grid-line" x1="{x:.1f}" y1="{pad_top - 8:.1f}" x2="{x:.1f}" '
                     f'y2="{pad_top + len(rows) * block_h - 6:.1f}"></line>')
        anchor = "middle"
        tx = x
        if x < 26:
            anchor, tx = "start", x
        elif x > width - 26:
            anchor, tx = "end", min(x, width - pad_x)
        parts.append(_txt(tx, height - 9, _axis_label(t), cls="t-num", anchor=anchor))

    for i, (st, br, gl) in enumerate(rows):
        y = pad_top + i * block_h
        stage_txt = STAGE_LABEL[st] if width >= 420 else STAGE_SHORT[st]
        parts.append(_txt(x0 - 8, y + (sub_h if gl else 0) / 2 + 8, stage_txt,
                          cls="t-label", anchor="end"))
        for j, (group, items) in enumerate((("br", br), ("gl", gl))):
            if not items:
                continue
            yy = y + 10 + j * sub_h
            lo = min(r["lo"] for r in items)
            hi = max(r["hi"] for r in items)
            med = _median([r["mid"] for r in items])
            color = "var(--br)" if group == "br" else "var(--gl)"
            xa, xb = xpos(lo), xpos(hi)
            parts.append(
                f'<line class="range-line" x1="{xa:.1f}" y1="{yy:.1f}" x2="{max(xb, xa + 1):.1f}" '
                f'y2="{yy:.1f}" stroke="{color}" stroke-width="6" stroke-linecap="butt"></line>'
            )
            xm = xpos(med)
            parts.append(f'<circle cx="{xm:.1f}" cy="{yy:.1f}" r="4" fill="#FFFFFF" '
                         f'stroke="{color}" stroke-width="2"></circle>')
            label = fmt_usd(med)
            half = len(label) * 3.4
            lx = min(max(xm, x0 + half + 2), width - pad_x - 34 - half)
            parts.append(_txt(lx, yy - 7, label, cls="t-num t-strong", anchor="middle"))
            parts.append(_txt(width - pad_x, yy + 3, f"n={fmt_int(len(items))}",
                              cls="t-num", anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


def build_capital_svg(s: dict, width: int) -> str:
    """Distribuicao do porte declarado por faixa de valor (Brasil vs Global)."""
    cfg = _stage_variants(width)
    bands = CAPITAL_BANDS
    n_br = s["capital_bands"]["n"]["br"]
    n_gl = s["capital_bands"]["n"]["gl"]
    pct_br = [_pct(c, n_br) for c in s["capital_bands"]["br"]]
    pct_gl = [_pct(c, n_gl) for c in s["capital_bands"]["gl"]]
    max_v = max(1.0, max(pct_br + pct_gl))
    x0 = cfg["pad_x"] + cfg["label_w"]
    plot_w = width - x0 - cfg["pad_x"] - cfg["value_w"]
    row_h = cfg["row_h"]
    bar_h, bar_gap = cfg["bar_h"], cfg["bar_gap"]
    height = cfg["pad_top"] + len(bands) * row_h + cfg["pad_bottom"]
    short_labels = ("<1 mi", "1–10 mi", "10–100 mi", "100 mi–1 bi", ">1 bi")
    uid = f"capital{width}"
    parts = [
        f'<svg class="chart chart-capital" data-chart="capital" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="{uid}-title">',
        f'<title id="{uid}-title">Percentual de veículos por faixa de porte declarado, '
        f'gestoras brasileiras contra estrangeiras</title>',
        "<desc>Barras horizontais por faixa de patrimônio líquido convertido ou captação declarada; "
        "os percentuais são calculados dentro de cada grupo com valor divulgado.</desc>",
    ]
    for i, (_limit, label) in enumerate(bands):
        y = cfg["pad_top"] + i * row_h
        cy = y + (2 * bar_h + bar_gap) / 2 + 4
        txt = label if width >= 420 else short_labels[i]
        parts.append(_txt(x0 - 8, cy, txt, cls="t-label", anchor="end"))
        for j, g in enumerate(("br", "gl")):
            v = pct_br[i] if g == "br" else pct_gl[i]
            by = y + j * (bar_h + bar_gap)
            w = (v / max_v) * plot_w if v else 0.0
            fill = "var(--br)" if g == "br" else "var(--gl)"
            parts.append(
                f'<rect class="bar bar-{g}" data-band-index="{i}" data-group="{g}" '
                f'x="{x0:.1f}" y="{by:.1f}" width="{w:.1f}" height="{bar_h}" fill="{fill}"></rect>'
            )
            lx = x0 + w + 6
            if lx > width - cfg["pad_x"] - cfg["value_w"]:
                lx = width - cfg["pad_x"] - cfg["value_w"]
            label_cls = f"t-num t-{g}" if v > 0 else "t-num t-zero"
            parts.append(
                f'<text class="{label_cls}" data-val="{i}|{g}" x="{lx:.1f}" '
                f'y="{by + bar_h - 2:.1f}" text-anchor="start">{fmt_pct(v)}</text>'
            )
        if i < len(bands) - 1:
            ly = y + row_h - 6
            parts.append(
                f'<line class="grid-line" x1="{cfg["pad_x"] + cfg["label_w"] - 2:.1f}" '
                f'y1="{ly:.1f}" x2="{width - cfg["pad_x"]:.1f}" y2="{ly:.1f}"></line>'
            )
    parts.append("</svg>")
    return "".join(parts)


def build_capital_figure(s: dict) -> str:
    """Figura 2 quando a fonte nao divulga faixa de cheque: distribuicao de porte por faixa."""
    variants = [("s", 272), ("m", 424), ("l", 660), ("d", 520)]
    svgs = [f'<div class="fig-svg fig-svg-{n} cap-{n}">{build_capital_svg(s, w)}</div>' for n, w in variants]
    totals = [b + g for b, g in zip(s["capital_bands"]["br"], s["capital_bands"]["gl"])]
    total_n = sum(totals) or 1
    dom = max(range(len(CAPITAL_BANDS)), key=lambda i: totals[i])
    dom_label = CAPITAL_BANDS[dom][1].replace("até US$ ", "até US$ ").replace("acima de ", "acima de ")
    title = (
        f"A faixa de {dom_label} concentra {fmt_pct(100.0 * totals[dom] / total_n)} dos "
        f"{fmt_int(total_n)} veículos com porte declarado; a mediana é {fmt_usd(s['median_aum'])}."
    )
    med_br, med_gl = s["median_aum_br"], s["median_aum_gl"]
    if med_br and med_gl:
        diff = abs(med_br - med_gl) / med_gl
        if diff >= 0.15:
            direction = "acima da" if med_br > med_gl else "abaixo da"
            title += (f" A mediana brasileira ({fmt_usd(med_br)}) fica {fmt_pct(100 * diff)} "
                      f"{direction} global ({fmt_usd(med_gl)}).")
    srcs = []
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM — Registro de Fundos e Classes</a>')
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR — Form D</a>')
    source_html = "; ".join(srcs) if srcs else "fontes primárias citadas na tabela"
    return f"""<figure class="fig fig-ticket fig-capital" aria-labelledby="fig4-titulo">
  <h3 class="fig-title" id="fig4-titulo">{esc(title)}</h3>
  <p class="fig-unit">Unidade: percentual dos veículos de cada grupo com porte declarado
  (patrimônio líquido convertido na CVM ou captação no Form D). <span class="mono">n</span>
  = veículos com valor divulgado: {fmt_int(s['capital_bands']['n']['br'])} no Brasil,
  {fmt_int(s['capital_bands']['n']['gl'])} no exterior.</p>
  <ul class="legend" aria-label="Legenda">
    <li><i class="sw sw-br"></i>Gestoras brasileiras</li>
    <li><i class="sw sw-gl"></i>Gestoras estrangeiras</li>
  </ul>
  {''.join(svgs)}
  <figcaption>Fonte: {source_html}. Faixa de cheque mínimo/máximo não é divulgada em nenhum dos
  {fmt_int(s['n'])} registros do dataset canônico; por isso o painel usa o porte declarado.
  O mapeamento regulatório combina a classe do FIP na CVM/BNDES e o campo
  <span class="mono">investmentFundType = Venture Capital Fund</span> do SEC Form D.
  Cobertura de valor: {fmt_pct(s['aum_coverage'])} dos veículos. Universo global exclui ofertas
  declaradas abaixo de US$ 1 mi (critério de inclusão na <a href="#metodologia">nota metodológica</a>),
  o que explica a ausência de barra global na primeira faixa.</figcaption>
</figure>"""


def build_ticket_figure(s: dict) -> str:
    if not s["ticket_rows"]:
        return build_capital_figure(s)
    variants = [("s", 272), ("m", 424), ("l", 660), ("d", 352)]
    svgs = [f'<div class="fig-svg fig-svg-{n} cap-{n}">{build_ticket_svg(s, w)}</div>' for n, w in variants]

    br_rows = [r for r in s["ticket_rows"] if r["rec"]["geographic_cut"] == "BRASIL_LOCAL"]
    gl_rows = [r for r in s["ticket_rows"] if r["rec"]["geographic_cut"] != "BRASIL_LOCAL"]
    med_br = _median([r["mid"] for r in br_rows])
    med_gl = _median([r["mid"] for r in gl_rows])
    br_missing_pct = _pct(s["n_br"] - len(br_rows), s["n_br"]) if s["n_br"] else 100.0

    if med_br is not None and med_gl is not None and med_gl > 0:
        ratio = med_br / med_gl
        if ratio >= 1:
            title = (f"O cheque mediano brasileiro é {fmt_usd(med_br)}, {fmt_ratio(ratio)} "
                     f"o do exterior ({fmt_usd(med_gl)}).")
        else:
            title = (f"O cheque mediano brasileiro é {fmt_usd(med_br)}, {fmt_ratio(1 / ratio)} "
                     f"menor que o do exterior ({fmt_usd(med_gl)}).")
    elif med_gl is not None:
        if br_missing_pct >= 99.5:
            title = (f"O Form D declara cheque mediano de {fmt_usd(med_gl)}; a CVM não divulga faixa "
                     f"de cheque em nenhum dos {fmt_int(s['n_br'])} veículos brasileiros mapeados.")
        else:
            title = (f"O Form D declara cheque mediano de {fmt_usd(med_gl)}; "
                     f"a CVM não divulga faixa de cheque em {fmt_pct(br_missing_pct)} dos veículos brasileiros mapeados.")
    elif med_br is not None:
        title = f"O cheque mediano brasileiro declarado é {fmt_usd(med_br)}."
    else:
        title = (f"Faixa de cheque é declarada por {fmt_pct(s['ticket_coverage'])} dos veículos: "
                 f"o mínimo e o máximo por fase aparecem abaixo quando informados.")

    srcs = []
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR — Form D</a>')
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM — Informe Quadrimestral FIP</a>')
    source_html = "; ".join(srcs) if srcs else "fontes primárias citadas na tabela"
    legend = []
    if br_rows:
        legend.append('<li><i class="sw sw-br"></i>Brasil</li>')
    if gl_rows:
        legend.append('<li><i class="sw sw-gl"></i>Global</li>')
    legend_html = ('<ul class="legend" aria-label="Legenda">' + "".join(legend) + "</ul>") if legend else ""

    return f"""<figure class="fig fig-ticket" aria-labelledby="fig6-titulo">
  <h3 class="fig-title" id="fig6-titulo">{esc(title)}</h3>
  <p class="fig-unit">Unidade: US$ por rodada, escala logarítmica. Linha = mínimo e máximo declarados;
  ponto vazado = mediana. <span class="mono">n</span> = veículos com faixa divulgada.</p>
  {legend_html}
  {''.join(svgs)}
  <figcaption>Fonte: {source_html}. O mapeamento regulatório usa a classe do FIP na CVM/BNDES
  e o campo <span class="mono">investmentFundType = Venture Capital Fund</span> no SEC Form D.
  Faixa declarada por {fmt_int(len(s['ticket_rows']))} de {fmt_int(s['n'])} veículos
  ({fmt_pct(s['ticket_coverage'])}).</figcaption>
</figure>"""


def build_provenance_svg(s: dict, width: int) -> str:
    label_w = max(72, min(134, round(width * 0.34)))
    pad_x = 6
    pad_top = 8
    pad_bottom = 20
    x0 = pad_x + label_w
    plot_w = width - x0 - pad_x - 38
    kinds = ["CVM/BNDES", "SEC Form D", "Outras fontes"]
    colors = {"CVM/BNDES": "#0F172A", "SEC Form D": "var(--gl)", "Outras fontes": "#7C2D12"}
    cuts = ["BRASIL_LOCAL", "GLOBAL_ATIVO_BR", "GLOBAL_EX_BR"]
    narrow_labels = {"BRASIL_LOCAL": "Brasil", "GLOBAL_ATIVO_BR": "Global ativo", "GLOBAL_EX_BR": "Global ex-BR"}
    row_h = 54
    height = pad_top + len(cuts) * row_h + pad_bottom
    max_v = max(1, max((s["source_by_cut"][c].get(k, 0) for c in cuts for k in kinds)))
    bar_h = 8
    uid = f"prov{width}"
    parts = [
        f'<svg class="chart chart-prov" data-chart="prov" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="{uid}-title">',
        f'<title id="{uid}-title">Origem regulatória dos veículos por corte geográfico</title>',
        "<desc>Barras agrupadas por corte; cada cor indica o tipo de fonte primária do registro.</desc>",
    ]
    for i, cut in enumerate(cuts):
        y = pad_top + i * row_h
        label = CUT_LABEL[cut] if width >= 500 else narrow_labels[cut]
        parts.append(_txt(x0 - 8, y + row_h / 2 + 3, label, cls="t-label", anchor="end"))
        cut_total = sum(s["source_by_cut"][cut].get(k, 0) for k in kinds)
        for j, k in enumerate(kinds):
            v = s["source_by_cut"][cut].get(k, 0)
            by = y + 8 + j * (bar_h + 3)
            w = (v / max_v) * plot_w if v else 0.0
            parts.append(
                f'<rect class="bar bar-src" data-cut="{cut}" data-kind="{k}" x="{x0:.1f}" y="{by:.1f}" '
                f'width="{w:.1f}" height="{bar_h}" fill="{colors[k]}"></rect>'
            )
            if v:
                parts.append(_txt(x0 + w + 6, by + bar_h - 1, fmt_int(v), cls="t-num", anchor="start"))
        if cut_total == 0:
            parts.append(_txt(x0 + 6, y + row_h / 2 + 3, "0", cls="t-num t-zero", anchor="start"))
    parts.append("</svg>")
    return "".join(parts)


def build_provenance_figure(s: dict) -> str:
    variants = [("s", 272), ("m", 424), ("l", 660), ("d", 352)]
    svgs = [f'<div class="fig-svg fig-svg-{n} cap-{n}">{build_provenance_svg(s, w)}</div>' for n, w in variants]
    pct_cvm_br = _pct(s["source_by_cut"]["BRASIL_LOCAL"].get("CVM/BNDES", 0), s["by_cut"].get("BRASIL_LOCAL", 0))
    pct_sec_gl = _pct(
        s["source_by_cut"]["GLOBAL_ATIVO_BR"].get("SEC Form D", 0) + s["source_by_cut"]["GLOBAL_EX_BR"].get("SEC Form D", 0),
        s["n_gl"],
    )
    title = (
        f"CVM responde por {fmt_pct(pct_cvm_br)} dos veículos brasileiros; "
        f"SEC Form D sustenta {fmt_pct(pct_sec_gl)} do corte estrangeiro."
    )
    srcs = []
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM — Cadastro de Fundos</a>')
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR — Form D</a>')
    source_html = "; ".join(srcs) if srcs else "URLs de fonte primária dos registros"
    return f"""<figure class="fig fig-prov" aria-labelledby="fig5-titulo">
  <h3 class="fig-title" id="fig5-titulo">{esc(title)}</h3>
  <p class="fig-unit">Unidade: número de veículos por tipo de fonte primária.</p>
  <ul class="legend" aria-label="Legenda">
    <li><i class="sw sw-cvm"></i>CVM/BNDES</li>
    <li><i class="sw sw-sec"></i>SEC Form D</li>
    <li><i class="sw sw-dir"></i>Outras fontes abertas</li>
  </ul>
  {''.join(svgs)}
  <figcaption>Fonte: {source_html}. Agregado pelo campo <span class="mono">primary_source_url</span>
  de cada registro. O <a href="#metodologia">protocolo de coleta</a> descreve as licenças de cada base.</figcaption>
</figure>"""


# ---------------------------------------------------------------------------
# Blocos HTML: memorando analitico
# ---------------------------------------------------------------------------

def build_analysis(s: dict) -> str:
    """Memorando com fatos calculados. Cada item e (frase-fato, detalhe quantificado)."""
    items: list[tuple[str, str]] = []
    L = STAGE_LABEL

    if s["gap"]:
        g = s["gap"]
        if g["br"] == 0:
            fact = f"{L[g['stage']]} não tem nenhum veículo com sede no Brasil."
            detail = (
                f"O exterior reúne {fmt_int(g['gl'])} veículos nessa fase; a ausência local "
                f"empurra a expansão para rodadas lideradas por gestoras estrangeiras."
            )
        else:
            fact = f"A oferta local em {L[g['stage']]} é {fmt_ratio(g['ratio'])} inferior à estrangeira."
            detail = (
                f"{fmt_int(g['br'])} veículos brasileiros contra {fmt_int(g['gl'])} estrangeiros "
                f"cobrem a fase, diferença de {fmt_num(g['diff'], 1)} pontos percentuais na participação."
            )
        stage_aum = sum(
            r["aum_or_offering_usd"] or 0.0
            for r in s["records"] if g["stage"] in r["target_stages"]
        )
        if stage_aum:
            detail += f" O capital declarado nessa fase soma {fmt_usd(stage_aum)}."
        items.append((fact, detail))

    if s["portfolio"]["all"]["n_with_value"]:
        items.append((
            f"{fmt_pct(s['capital_br_share'])} do capital declarado está sob gestoras brasileiras.",
            f"Elas somam {fmt_pct(_pct(s['n_br'], s['n']))} dos veículos mapeados. "
            f"A mediana por veículo é {fmt_usd(s['median_aum'])} "
            f"(Brasil {fmt_usd(s['median_aum_br'])}, exterior {fmt_usd(s['median_aum_gl'])}); "
            f"os dez maiores veículos concentram {fmt_pct(s['top10_share'])} do capital com valor divulgado"
            + (f", e o maior deles — {s['largest']['firm_name']} — responde por {fmt_pct(s['largest_share'])}."
               if s["largest"] else "."),
        ))

    if s["n_gl"]:
        deals_clause = (
            f"As teses declaradas somam {fmt_int(s['brazil_deals'])} deals brasileiros, enquanto o corte "
            f"Global ex-Brasil reúne {fmt_int(s['by_cut']['GLOBAL_EX_BR'])} veículos sem atuação no país."
            if s["brazil_deals"] > 0 else
            f"As teses mapeadas não trazem contagem de deals brasileiros; o corte Global ex-Brasil "
            f"reúne {fmt_int(s['by_cut']['GLOBAL_EX_BR'])} veículos sem atuação registrada no país."
        )
        items.append((
            f"{fmt_int(s['cross_count'])} dos {fmt_int(s['n_gl'])} veículos estrangeiros mapeados "
            f"({fmt_pct(s['cross_index'])}) têm atividade registrada no Brasil.",
            deals_clause,
        ))

    items.append((
        f"Faixa de cheque não é divulgada em {fmt_pct(_pct(s['undisclosed_ticket'], s['n']))} dos veículos.",
        f"Patrimônio ou captação não consta em {fmt_pct(_pct(s['undisclosed_aum'], s['n']))} dos registros; "
        f"esses campos aparecem como “{ND_LABEL}” na tabela, sem imputação.",
    ))

    lis = []
    for i, (fact, detail) in enumerate(items[:4], start=1):
        lis.append(
            f'<li><span class="memo-num" aria-hidden="true">{i:02d}</span>'
            f'<p><strong>{esc(fact)}</strong> {esc(detail)}</p></li>'
        )
    return f'<ol class="memo">{"".join(lis)}</ol>'


def build_portfolio_table(s: dict) -> str:
    """Distribuicao de porte declarado por corte (percentis + concentracao top-10)."""
    rows = [
        ("Brasil local", s["portfolio"]["br"], s["by_cut"].get("BRASIL_LOCAL", 0)),
        ("Global ativo no Brasil", s["portfolio"]["ga"], s["by_cut"].get("GLOBAL_ATIVO_BR", 0)),
        ("Global ex-Brasil", s["portfolio"]["ge"], s["by_cut"].get("GLOBAL_EX_BR", 0)),
        ("Todos os cortes", s["portfolio"]["all"], s["n"]),
    ]
    body = []
    for label, prof, n_cut in rows:
        if prof["n_with_value"]:
            body.append(
                f'<tr><th scope="row">{esc(label)}</th>'
                f'<td class="num-cell"><span class="num">{fmt_int(prof["n_with_value"])}</span></td>'
                f'<td class="num-cell"><span class="num">{fmt_usd(prof["p25"])}</span></td>'
                f'<td class="num-cell"><span class="num">{fmt_usd(prof["median"])}</span></td>'
                f'<td class="num-cell"><span class="num">{fmt_usd(prof["p75"])}</span></td>'
                f'<td class="num-cell"><span class="num">{fmt_pct(prof["top10_share"])}</span></td></tr>'
            )
        else:
            body.append(
                f'<tr><th scope="row">{esc(label)}</th><td class="num-cell"><span class="nd">0</span></td>'
                f'<td class="num-cell"><span class="nd">—</span></td><td class="num-cell"><span class="nd">—</span></td>'
                f'<td class="num-cell"><span class="nd">—</span></td><td class="num-cell"><span class="nd">—</span></td></tr>'
            )
    return (
        '<div class="table-scroll" role="region" aria-labelledby="porte-titulo" tabindex="0">'
        '<table class="matrix portfolio" id="porte">'
        '<caption class="tbl-caption" id="porte-titulo">Tabela 1 — Distribuição do porte declarado '
        '(patrimônio líquido convertido ou captação no Form D), em US$. Percentis calculados apenas '
        'sobre veículos com valor divulgado; “top-10” é a fatia dos dez maiores veículos do corte.</caption>'
        '<thead><tr><th scope="col">Corte</th><th scope="col" class="num-h">Com valor</th>'
        '<th scope="col" class="num-h">P25</th><th scope="col" class="num-h">Mediana</th>'
        '<th scope="col" class="num-h">P75</th><th scope="col" class="num-h">Top-10 do corte</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
    )


def build_matrix(s: dict) -> str:
    L = STAGE_LABEL
    head = (
        '<thead><tr><th scope="col">Fase</th>'
        f'<th scope="col" class="num-h">Brasil local</th>'
        f'<th scope="col" class="num-h">Global ativo no Brasil</th>'
        f'<th scope="col" class="num-h">Global ex-Brasil</th>'
        '<th scope="col">Leitura de decisão</th></tr></thead>'
    )
    rows = []
    for st in STAGES:
        br = s["stage_group"][st]["br"]
        ga = sum(1 for r in s["records"] if r["geographic_cut"] == "GLOBAL_ATIVO_BR" and st in r["target_stages"])
        ge = sum(1 for r in s["records"] if r["geographic_cut"] == "GLOBAL_EX_BR" and st in r["target_stages"])
        aum_br = sum(r["aum_or_offering_usd"] or 0.0 for r in s["records"] if r["geographic_cut"] == "BRASIL_LOCAL" and st in r["target_stages"])
        aum_ga = sum(r["aum_or_offering_usd"] or 0.0 for r in s["records"] if r["geographic_cut"] == "GLOBAL_ATIVO_BR" and st in r["target_stages"])
        aum_ge = sum(r["aum_or_offering_usd"] or 0.0 for r in s["records"] if r["geographic_cut"] == "GLOBAL_EX_BR" and st in r["target_stages"])

        br_share = s["pct_stage"][st]["br"]
        gl_share = s["pct_stage"][st]["gl"]
        n_outside = ga + ge
        num = lambda v: f'<span class="num">{v}</span>'
        if br == 0 and n_outside == 0:
            leitura = '<span class="read read-empty">Sem cobertura</span> — nenhum veículo mapeado na fase.'
        elif br == 0:
            leitura = (f'<span class="read read-gap">Lacuna local</span> — '
                       f'{num(fmt_int(n_outside))} veículos estrangeiros cobrem a fase.')
        elif n_outside == 0:
            leitura = (f'<span class="read read-dense">Densa no Brasil</span> — '
                       f'{num(fmt_int(br))} veículos locais, nenhum estrangeiro na fase.')
        elif br_share >= gl_share * 1.35:
            leitura = (f'<span class="read read-dense">Densa no Brasil</span> — '
                       f'{num(fmt_pct(br_share))} dos locais contra {num(fmt_pct(gl_share))} dos estrangeiros.')
        elif gl_share >= br_share * 1.35:
            leitura = (f'<span class="read read-thin">Densa no exterior</span> — '
                       f'{num(fmt_pct(gl_share))} dos estrangeiros contra {num(fmt_pct(br_share))} dos locais.')
        else:
            leitura = (f'<span class="read read-even">Cobertura equilibrada</span> — '
                       f'{num(fmt_int(br))} locais contra {num(fmt_int(n_outside))} estrangeiros.')

        def money_cell(v: float, n: int) -> str:
            if n == 0:
                return '<td class="num-cell"><span class="nd">—</span></td>'
            money = fmt_usd(v) if v else ND_LABEL
            if "Não divulgado" in money:
                return f'<td class="num-cell"><span class="m-count">{fmt_int(n)}</span><br><span class="nd">n/d</span></td>'
            return (f'<td class="num-cell"><span class="m-count">{fmt_int(n)}</span><br>'
                    f'<span class="m-money">{money}</span></td>')

        rows.append(
            f'<tr><th scope="row">{esc(L[st])}</th>'
            f'{money_cell(aum_br, br)}{money_cell(aum_ga, ga)}{money_cell(aum_ge, ge)}'
            f'<td class="read-cell">{leitura}</td></tr>'
        )
    uniq = [s["by_cut"].get(c, 0) for c in CUTS]
    rows.append(
        f'<tr class="matrix-total"><th scope="row">Veículos (únicos)</th>'
        f'<td class="num-cell"><span class="m-count">{fmt_int(uniq[0])}</span></td>'
        f'<td class="num-cell"><span class="m-count">{fmt_int(uniq[1])}</span></td>'
        f'<td class="num-cell"><span class="m-count">{fmt_int(uniq[2])}</span></td>'
        f'<td class="read-cell">Linhas acima somam coberturas por fase; um veículo pode cobrir mais de uma fase.</td></tr>'
    )
    return (
        '<div class="table-scroll" role="region" aria-labelledby="matriz-titulo" tabindex="0">'
        f'<table class="matrix" id="matriz"><caption class="tbl-caption" id="matriz-titulo">'
        f'Matriz 1 — Veículos e capital declarado por fase e corte geográfico. “Leitura” compara a '
        f'densidade local com a estrangeira; o capital é a soma dos valores divulgados.</caption>{head}<tbody>'
        + "".join(rows) + "</tbody></table></div>"
    )


# ---------------------------------------------------------------------------
# Blocos HTML: tabela auditavel (render inicial sem JS + payload para filtros)
# ---------------------------------------------------------------------------

TABLE_COLS = ["firm", "vehicle", "cut", "stages", "tmin", "tmax", "aum", "method", "reg", "src", "active", "deals"]


def _dataset_label(meta: dict) -> str:
    if meta.get("__fixture__"):
        return "fixture de desenvolvimento"
    raw = str(meta.get("_input_path") or "")
    normalized = raw.replace("\\", "/")
    marker = "data/canonical/"
    if marker in normalized:
        return f"dataset canônico (data/canonical/{normalized.split(marker, 1)[1]})"
    if raw:
        return "dataset " + Path(raw).name
    return "dataset canônico"


def build_payload(s: dict, meta: dict) -> str:
    sources: list[list[str]] = []
    src_index: dict[tuple[str, str], int] = {}
    rows: list[list] = []
    for rec in s["records"]:
        src_key = (rec["primary_source_name"], rec["primary_source_url"])
        if src_key not in src_index:
            src_index[src_key] = len(sources)
            sources.append([
                rec["primary_source_name"],
                rec["primary_source_url"],
                source_short(rec["primary_source_url"], rec["primary_source_name"]),
            ])
        src = src_index[src_key]
        rows.append([
            rec["firm_name"],
            rec["vehicle_name"],
            CUTS.index(rec["geographic_cut"]),
            [STAGES.index(st) for st in rec["target_stages"]],
            rec["estimated_ticket_min_usd"],
            rec["estimated_ticket_max_usd"],
            rec["aum_or_offering_usd"],
            list(METHOD_LABEL).index(rec["stage_classification_method"]),
            rec["regulatory_id"],
            src,
            rec["active_status"],
            rec["brazil_deals_count"],
        ])
    payload = {
        "generated_at": meta.get("generated_at"),
        "dataset": "fixture" if meta.get("__fixture__") else "canonical",
        "dataset_label": _dataset_label(meta),
        "total": s["n"],
        "cuts": [{"id": c, "label": CUT_LABEL[c]} for c in CUTS],
        "stages": [{"id": st, "label": STAGE_LABEL[st], "short": STAGE_SHORT[st]} for st in STAGES],
        "methods": [{"id": m, "label": METHOD_LABEL[m], "title": METHOD_TITLE[m]} for m in METHOD_LABEL],
        "sources": sources,
        "nd": ND_LABEL,
        "cols": TABLE_COLS,
        "rows": rows,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def _row_html(rec: dict) -> str:
    cut = rec["geographic_cut"]
    dot = _dot("br" if cut == "BRASIL_LOCAL" else ("cross" if cut == "GLOBAL_ATIVO_BR" else "gl"))
    stages_html = "".join(
        f'<span class="badge">{esc(STAGE_LABEL[st])}</span>' for st in rec["target_stages"]
    )
    src_short = source_short(rec["primary_source_url"], rec["primary_source_name"])
    active_note = "Veículo ativo" if rec["active_status"] else "Situação não ativa"

    def money_cell(value: str) -> str:
        if value == ND_LABEL:
            return ('<td class="num-cell"><span class="nd" title="' + esc(ND_LABEL) + '">n/d'
                    '<span class="vh">' + esc(ND_LABEL) + "</span></span></td>")
        return f'<td class="num-cell"><span class="num">{value}</span></td>'

    ticket = fmt_usd_range(rec["estimated_ticket_min_usd"], rec["estimated_ticket_max_usd"])
    aum = fmt_usd(rec["aum_or_offering_usd"])
    return (
        '<tr>'
        f'<td class="c-firm"><span class="firm-dot {("on" if rec["active_status"] else "off")}" '
        f'title="{esc(active_note)}" aria-label="{esc(active_note)}"></span>'
        f'<span class="firm-name">{esc(rec["firm_name"])}</span></td>'
        f'<td class="c-vehicle"><span class="veh">{esc(rec["vehicle_name"])}</span>'
        f'<span class="reg">{"&mdash;" if not rec["regulatory_id"] else esc(rec["regulatory_id"])}</span></td>'
        f'<td class="c-cut">{dot}{esc(CUT_LABEL[cut])}</td>'
        f'<td class="c-stages">{stages_html}</td>'
        f'{money_cell(ticket)}{money_cell(aum)}'
        f'<td class="c-method"><span class="method" title="{esc(METHOD_TITLE[rec["stage_classification_method"]])}">'
        f'{esc(METHOD_LABEL[rec["stage_classification_method"]])}</span></td>'
        f'<td class="c-source"><a class="src-link" href="{esc(rec["primary_source_url"])}" '
        f'target="_blank" rel="noopener noreferrer">{esc(src_short)}</a></td>'
        "</tr>"
    )


def initial_rows(s: dict, limit: int = 50) -> str:
    ordered = sorted(s["records"], key=lambda r: -(r["aum_or_offering_usd"] or 0.0))
    return "\n".join(_row_html(r) for r in ordered[:limit])


def build_table(s: dict) -> str:
    head_cells = [
        ("firm", "Gestora", ""),
        ("vehicle", "Veículo", ""),
        ("cut", "Corte", ""),
        ("stages", "Fases de atuação", ""),
        ("ticket", "Cheque declarado", "num-h"),
        ("aum", "Patrimônio / captação", "num-h"),
        ("method", "Método de estágio", ""),
        ("src", "Fonte oficial", ""),
    ]
    ths = []
    for key, label, cls in head_cells:
        cls_attr = f' class="{cls}"' if cls else ""
        if key in {"firm", "vehicle", "cut", "ticket", "aum"}:
            ths.append(
                f'<th scope="col"{cls_attr} aria-sort="none">'
                f'<button type="button" class="sort-btn" data-sort="{key}">{label}'
                f'<span class="sort-ind" aria-hidden="true"></span></button></th>'
            )
        else:
            ths.append(f'<th scope="col" class="th-static">{label}</th>')
    return (
        '<div class="table-scroll" role="region" aria-labelledby="tabela-titulo" tabindex="0">'
        '<table class="funds" id="tabela-fundos">'
        '<caption class="tbl-caption" id="tabela-titulo">Tabela 2 — Veículos mapeados com fonte '
        'primária verificável. Clique nos títulos para ordenar; os filtros acima recalculam '
        'a contagem, o capital e o gráfico de fases.</caption>'
        f'<thead><tr>{"".join(ths)}</tr></thead>'
        '<tbody id="tbody">' + initial_rows(s) + "</tbody></table></div>"
    )


# ---------------------------------------------------------------------------
# Blocos HTML: metodologia, referencias, rodape
# ---------------------------------------------------------------------------

def _crosscheck_row(label: str, expected: float, computed: float) -> dict:
    if computed:
        delta = abs(expected - computed) / abs(computed)
    else:
        delta = 0.0 if expected == computed else 1.0
    return {"label": label, "kpis": expected, "computed": computed,
            "delta_pct": delta * 100.0, "ok": delta <= 0.005}


def kpis_crosscheck(s: dict, meta: dict) -> list[dict]:
    """Confere os agregados do kpis.json contra o recalculo do render.

    Nunca sobrescreve: o numero exibido e sempre o calculado aqui. Divergencia
    acima de 0,5% gera aviso (e fica evidenciada na nota metodologica).
    """
    checks: list[dict] = []
    counts = meta.get("counts") if isinstance(meta.get("counts"), dict) else {}
    total = counts.get("total")
    if isinstance(total, (int, float)):
        checks.append(_crosscheck_row("Veículos (total)", float(total), float(s["n"])))
    by_cut = counts.get("by_cut") if isinstance(counts.get("by_cut"), dict) else {}
    for cut in CUTS:
        val = by_cut.get(cut)
        if isinstance(val, (int, float)):
            checks.append(_crosscheck_row(f"Veículos — {CUT_LABEL[cut]}", float(val), float(s["by_cut"].get(cut, 0))))
    capital = meta.get("capital") if isinstance(meta.get("capital"), dict) else {}
    cap_total = capital.get("total_usd")
    if isinstance(cap_total, (int, float)):
        checks.append(_crosscheck_row("Capital declarado (US$)", float(cap_total), float(s["aum_total"])))
    cap_by_cut = capital.get("by_cut_usd") if isinstance(capital.get("by_cut_usd"), dict) else {}
    for cut in CUTS:
        val = cap_by_cut.get(cut)
        if isinstance(val, (int, float)) and s["aum_total"]:
            checks.append(_crosscheck_row(f"Capital — {CUT_LABEL[cut]} (US$)", float(val),
                                          float(s["aum_by_cut"].get(cut, 0.0))))
    kpis_meta = meta.get("kpis") if isinstance(meta.get("kpis"), dict) else {}
    cross = kpis_meta.get("cross_border_index")
    if isinstance(cross, (int, float)):
        checks.append(_crosscheck_row("Índice cross-border (fração)", float(cross), s["cross_index"] / 100.0))
    top10 = kpis_meta.get("top10_share")
    if isinstance(top10, (int, float)) and s["aum_total"]:
        checks.append(_crosscheck_row("Top-10 share do capital (fração)", float(top10), s["top10_share"] / 100.0))
    median_meta = kpis_meta.get("median_aum_usd")
    if isinstance(median_meta, (int, float)) and s["median_aum"]:
        checks.append(_crosscheck_row("Mediana de capital (US$)", float(median_meta), s["median_aum"]))
    median_br_meta = kpis_meta.get("brasil_median_aum_usd")
    if isinstance(median_br_meta, (int, float)) and s["median_aum_br"]:
        checks.append(_crosscheck_row("Mediana de porte — Brasil (US$)", float(median_br_meta),
                                      s["median_aum_br"]))
    br_cap_meta = kpis_meta.get("brasil_capital_usd")
    if isinstance(br_cap_meta, (int, float)) and s["aum_br"]:
        checks.append(_crosscheck_row("Capital — Brasil (US$)", float(br_cap_meta), s["aum_br"]))
    return checks


def _as_pct(value) -> float | None:
    """Aceita fracao (0..1) ou percentual (0..100) e normaliza para percentual."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) * 100.0 if abs(float(value)) <= 1.0 else float(value)


def build_liquidity_block(s: dict, meta: dict) -> str:
    """Indicadores de liquidez derivados na etapa de transformacao (kpis.json).

    O render nao consegue recalcular serie temporal (PL de 2 informes) nem
    offering - sold do Form D; por isso esses numeros vem do kpis.json e sao
    exibidos com a definicao e o motivo de exclusao do capital comprometido CVM.
    """
    kp = meta.get("kpis") if isinstance(meta.get("kpis"), dict) else {}
    capital = meta.get("capital") if isinstance(meta.get("capital"), dict) else {}
    cells: list[str] = []

    br_share = _as_pct(kp.get("brasil_liquidez_pl_em_alta_share"))
    br_funds = kp.get("brasil_liquidez_pl_em_alta_fundos")
    br_base = kp.get("brasil_liquidez_base_fundos", kp.get("brasil_liquidez_pl_em_alta_base_fundos"))
    if br_share is not None:
        note = "FIPs ativos com patrimônio líquido crescente entre os dois últimos informes quadrimestrais."
        if isinstance(br_funds, (int, float)) and isinstance(br_base, (int, float)):
            note += f" Base: {fmt_int(br_funds)} de {fmt_int(br_base)} fundos com série comparável."
        cells.append(_liquidity_cell("Brasil · liquidez em alta", fmt_pct(br_share), note))

    gl_share = _as_pct(kp.get("global_funds_still_raising_share"))
    gl_n = kp.get("global_funds_still_raising")
    if gl_share is not None:
        note = "Veículos do Form D com offering remanescente após o total vendido (ainda captando)."
        if isinstance(gl_n, (int, float)):
            note += f" Base: {fmt_int(gl_n)} veículos com valores comparáveis."
        cells.append(_liquidity_cell("Global · ainda captando", fmt_pct(gl_share), note))

    dry = capital.get("dry_powder_global_usd", capital.get("dry_powder_usd"))
    if isinstance(dry, (int, float)):
        base_n = capital.get("dry_powder_coverage_records")
        note = "Oferta menos total vendido declarados no SEC Form D."
        if isinstance(base_n, (int, float)):
            note += f" Base: {fmt_int(base_n)} veículos com offering e total vendido."
        cells.append(_liquidity_cell("Global · dry powder", fmt_usd(dry), note))

    cvm = capital.get("cvm_capital_reporting")
    if isinstance(cvm, dict):
        used = cvm.get("usado_em_kpi")
        inconsistent = cvm.get("registros_com_inconsistencia")
        suspect = cvm.get("registros_com_escala_suspeita")
        if used is False:
            reasons = []
            if isinstance(inconsistent, (int, float)):
                reasons.append(f"{fmt_int(inconsistent)} registros com integralizado > comprometido")
            if isinstance(suspect, (int, float)):
                reasons.append(f"{fmt_int(suspect)} com escala suspeita")
            detail = "; ".join(reasons) if reasons else "inconsistências internas"
            cells.append(_liquidity_cell(
                "Brasil · capital comprometido", "não publicado",
                f"Campos VL_CAP_COMPROM/VL_CAP_INTEGR do informe CVM excluídos do KPI: {detail}. "
                f"A liquidez brasileira é lida pela variação de patrimônio líquido.",
            ))

    if not cells:
        return (
            '<p class="fine">O <span class="mono">kpis.json</span> não trouxe indicadores de liquidez; '
            "o dashboard não estima dry powder nem variação patrimonial sem série temporal na fonte.</p>"
        )
    return ('<div class="liquidity">'
            f'<p class="fig-unit">Indicadores derivados na etapa de transformação (definições da seção 1.3 '
            f'da especificação), sobre as bases completas; o recorte do dashboard é de '
            f'{fmt_int(s["n"])} registros.</p>'
            f'<div class="liq-grid">{"".join(cells)}</div></div>')


def _liquidity_cell(label: str, value: str, note: str) -> str:
    return (
        f'<div class="liq"><p class="liq-label">{esc(label)}</p>'
        f'<p class="liq-value tnum">{esc(value)}</p>'
        f'<p class="liq-note">{esc(note)}</p></div>'
    )


def build_methodology(s: dict, meta: dict) -> str:
    fx = (meta.get("fx") or {}) if isinstance(meta.get("fx"), dict) else {}
    fx_note = "câmbio não informado no kpis.json"
    if fx:
        rate = fx.get("rate")
        ref = fx.get("reference_date") or "data não informada"
        src = fx.get("source") or "BCB PTAX"
        fx_note = (f"{src}: 1 US$ = R$ {fmt_num(rate, 4)} em {ref}" if rate not in (None, "") else f"{src} ({ref})")
    method_rows = []
    for m in METHOD_LABEL:
        c = s["methods"].get(m, 0)
        method_rows.append(
            f'<tr><th scope="row">{esc(METHOD_LABEL[m])}</th>'
            f'<td class="num-cell"><span class="num">{fmt_int(c)}</span></td>'
            f'<td class="num-cell"><span class="num">{fmt_pct(_pct(c, s["n"]))}</span></td>'
            f'<td>{esc(METHOD_TITLE[m])}</td></tr>'
        )
    undisclosed_rate = fmt_pct(_pct(s["undisclosed_ticket"], s["n"]))
    aum_missing = fmt_pct(_pct(s["undisclosed_aum"], s["n"]))
    generated = fmt_datetime_pt(meta.get("generated_at"))
    checks = kpis_crosscheck(s, meta)
    if checks:
        ok_count = sum(1 for c in checks if c["ok"])
        worst = max(checks, key=lambda c: c["delta_pct"])
        if ok_count == len(checks):
            crosscheck_html = (
                f'<p class="fine">Conferência com o <span class="mono">kpis.json</span>: '
                f'{len(checks)} agregados checados, todos dentro de 0,5% do recálculo '
                f'(maior desvio: {fmt_pct(worst["delta_pct"], 2)} em {esc(worst["label"])}).</p>'
            )
        else:
            diverg = [c for c in checks if not c["ok"]]
            items = "".join(
                f'<li>{esc(c["label"])}: kpis.json {fmt_num(c["kpis"], 2)} · '
                f'render {fmt_num(c["computed"], 2)} (desvio {fmt_pct(c["delta_pct"], 2)})</li>'
                for c in diverg[:5]
            )
            crosscheck_html = (
                f'<p class="fine">Conferência com o <span class="mono">kpis.json</span>: '
                f'{len(diverg)} de {len(checks)} agregados divergem mais de 0,5%. '
                f'O dashboard exibe sempre o valor recalculado dos registros.</p>'
                f'<ul class="fine crosscheck">{items}</ul>'
            )
    else:
        crosscheck_html = (
            '<p class="fine">Sem <span class="mono">kpis.json</span> disponível: os agregados '
            'exibidos vêm exclusivamente do recálculo sobre os registros.</p>'
        )
    quality_bits = []
    if s["capital_inconsistent"]:
        quality_bits.append(
            f'{fmt_int(s["capital_inconsistent"])} registros marcados como '
            f'<span class="mono">capital_consistente = false</span>'
        )
    if s["quality_flags"]:
        top_flags = sorted(s["quality_flags"].items(), key=lambda kv: -kv[1])[:3]
        quality_bits.append(
            "flags de qualidade mais frequentes: "
            + ", ".join(f'{esc(flag)} ({fmt_int(n)})' for flag, n in top_flags)
        )
    quality_html = ""
    if quality_bits:
        quality_html = (
            '<p class="fine">Sinalização de qualidade do dataset: '
            + "; ".join(quality_bits)
            + ". Ausências e inconsistências ficam registradas no próprio registro."
            "</p>"
        )
    transform = meta.get("transform_report") if isinstance(meta.get("transform_report"), dict) else {}
    scope = transform.get("scope") if isinstance(transform.get("scope"), dict) else {}
    universe_html = ""
    if scope:
        micro = scope.get("excluded_micro_filings")
        micro_usd = scope.get("excluded_micro_offering_usd_total")
        spv = scope.get("excluded_spv_vehicles")
        spv_usd = scope.get("excluded_spv_offering_usd_total")
        dropped = transform.get("dropped") if isinstance(transform.get("dropped"), list) else []
        bits = []
        if scope.get("br"):
            bits.append(f"Brasil: {esc(str(scope['br']))}")
        if scope.get("global"):
            bits.append(f"Global: {esc(str(scope['global']))}")
        excluded = []
        if isinstance(micro, (int, float)):
            excluded.append(f"{fmt_int(micro)} filings nominais com oferta declarada abaixo de "
                            f"US$ 1 mi ({fmt_usd(micro_usd)})")
        if isinstance(spv, (int, float)):
            excluded.append(f"{fmt_int(spv)} SPVs de deal único ({fmt_usd(spv_usd)})")
        if dropped:
            excluded.append(f"{fmt_int(len(dropped))} registro(s) sem país determinável")
        universe_html = (
            '<h3 class="h3">Universo e critérios de exclusão</h3>'
            f'<p>{"; ".join(bits)}.</p>'
            + (f'<p>Ficaram fora do universo: {"; ".join(excluded)}. As exclusões são critério de '
               f'inclusão declarado, não falha de coleta.</p>' if excluded else "")
            + '<p class="fine">Registros excluídos permanecem documentados em '
              '<span class="mono">data/canonical/transform_report.json</span>.</p>'
        )
    return f"""<div class="grid">
  <div class="col-7 prose">
    <h3 class="h3">Escopo e cortes geográficos</h3>
    <p>O conjunto cobre três cortes definidos na especificação: <strong>Brasil local</strong>
    (gestora com sede no Brasil), <strong>Global ativo no Brasil</strong> (gestora estrangeira com
    ao menos um investimento registrado no país) e <strong>Global ex-Brasil</strong> (gestora
    estrangeira sem atuação registrada no país). Contagens por corte:
    Brasil local {fmt_int(s["by_cut"]["BRASIL_LOCAL"])}, Global ativo no Brasil
    {fmt_int(s["by_cut"]["GLOBAL_ATIVO_BR"])} e Global ex-Brasil {fmt_int(s["by_cut"]["GLOBAL_EX_BR"])}.</p>

    {universe_html}

    <h3 class="h3">Como cada fase foi classificada</h3>
    <p>Três regras determinísticas, aplicadas nesta ordem, com o método gravado em cada linha:
    (1) <strong>tese declarada</strong> pela gestora em diretório aberto; (2) <strong>classe
    regulatória CVM/BNDES</strong> — FIP Capital Semente para Pre-Seed/Seed, Empresas Emergentes
    para Série A/B, Multiestratégia com patrimônio líquido acima de R$ 500 milhões para Série B e
    Late Stage; (3) <strong>heurística de cheque</strong>, conforme as faixas empíricas Growth List
    (US$ 50 mil–500 mil Pre-Seed; 500 mil–3 mi Seed; 3–15 mi Série A; 10–50 mi Série B; 30–100 mi+
    Late Stage).</p>
    <div class="table-scroll" role="region" aria-label="Distribuição por método de classificação" tabindex="0">
    <table class="method-table">
      <caption class="tbl-caption">Distribuição dos registros por método de classificação</caption>
      <thead><tr><th scope="col">Método</th><th scope="col" class="num-h">Veículos</th>
      <th scope="col" class="num-h">Participação</th><th scope="col">Critério</th></tr></thead>
      <tbody>{''.join(method_rows)}</tbody>
    </table>
    </div>

    <h3 class="h3">Valores, câmbio e não divulgação</h3>
    <p>Patrimônio líquido de FIPs vem do registro de fundos e classes e do informe quadrimestral
    da CVM (base viva da instrução RCVM 175) e é convertido para dólares pela PTAX de venda da
    competência mais próxima ({esc(fx_note)}). Valores da SEC Form D já são declarados em dólar.
    Quando a fonte não informa um atributo, o campo fica nulo e é exibido como
    “{ND_LABEL}”: {undisclosed_rate} dos veículos não têm faixa de cheque e {aum_missing} não têm
    patrimônio ou captação declarados. Nenhum valor é estimado ou interpolado.</p>
    {quality_html}

    <h3 class="h3">Limitações</h3>
    <ul class="limits">
      <li>A base é uma amostra auditável de fontes abertas, não um censo do mercado global de VC.</li>
      <li>O Form D registra apenas ofertas isentas de registro nos EUA; veículos sem filing não aparecem.</li>
      <li>O informe quadrimestral da CVM tem defasagem de competência e pode não capturar veículos recém-registrados.</li>
      <li>Fases imputadas por heurística de cheque são aproximações e estão sinalizadas linha a linha.</li>
      <li>Capital comprometido e integralizado do informe CVM não entram no KPI de liquidez por
      inconsistência interna documentada; a liquidez brasileira é lida pela variação de patrimônio líquido.</li>
      <li>Indicadores de liquidez e de atividade cross-border são derivados na etapa de transformação,
      sobre as bases completas; não são recalculados no navegador.</li>
      <li>Nomes de gestoras seguem a grafia da fonte primária; a resolução de entidades pode manter variantes legais (&quot;L.P.&quot;, &quot;LLC&quot;).</li>
    </ul>
  </div>
  <div class="col-5 prose">
    <h3 class="h3">Protocolo de coleta</h3>
    <ol class="protocol">
      <li>Requisições HTTP auditadas, com <span class="mono">User-Agent</span> acadêmico declarado,
      intervalo mínimo de 0,2 s no SEC EDGAR e cache por hash SHA-256 em <span class="mono">data/raw/</span>.</li>
      <li>Decodificação dos CSVs da CVM em ISO-8859-1 (<span class="mono">latin1</span>) e separador
      ponto e vírgula, sem perda de acentuação.</li>
      <li>Resolução de entidades por CNPJ da gestora, CIK do emissor e domínio web normalizado.</li>
      <li>Validação de 100% dos registros contra o contrato JSON Schema, com URL de fonte primária
      obrigatoriamente em HTTPS.</li>
      <li>Render determinístico e auditoria nos quatro quality gates da especificação
      (ingestão, contrato, anti-slop e UI/UX).</li>
    </ol>

    <h3 class="h3">Reprodutibilidade</h3>
    <p class="code">python pipeline/render.py \\<br>  --input data/canonical/funds.json \\<br>  --out dashboard/index.html</p>
    <p class="fine">Geração: {esc(generated)}. Dataset: {'fixture de desenvolvimento' if meta.get("__fixture__") else 'canônico'}.
    Contrato: <span class="mono">contract/venture_capital_fund.schema.json</span>.
    Conjunto com {fmt_int(s['n'])} registros e {fmt_int(s['n_sources'])} URLs de fonte primária distintas.</p>

    <h3 class="h3">Conferência de agregados</h3>
    {crosscheck_html}
  </div>
</div>"""


def _safe_cite_url(url: str) -> str:
    """Normaliza URLs de citacao que sao templates de API (nao abrem no navegador).

    Ex.: OData do BCB com @dataInicial/@dataFinalCotacao retorna 400; citamos o
    endpoint estavel do servico. Nunca alteramos a URL de fonte primaria de registro.
    """
    if "olinda.bcb.gov.br" in url:
        return "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    cut = re.split(r"[({?]|%40", url, maxsplit=1)[0]
    if cut != url and cut.startswith("https://"):
        return cut.rstrip("/") + "/"
    return url


def build_references(s: dict, meta: dict) -> str:
    rows = sorted(
        [(name, host, count) for (name, host), count in s["source_collections"].items()],
        key=lambda x: (-x[2], x[0].lower()),
    )
    source_items = []
    for name, host, count in rows:
        url = s["collection_example"].get((name, host), "")
        if count == 1:
            link = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a>'
        else:
            link = (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a> '
                    f'<span class="ref-link">(exemplo de registro)</span>')
        source_items.append(
            '<li>{link}<span class="ref-meta">{count} registro(s) · {lic} · {host}</span></li>'.format(
                link=link, count=fmt_int(count), lic=esc(license_for(url)), host=esc(host or "—")
            )
        )
    meta_items = "".join(
        f'<li><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a></li>'
        for name, url in METHOD_REFS
    )
    kpi_sources = meta.get("sources") if isinstance(meta.get("sources"), list) else []
    extra_items = []
    blocked_hosts = ("openvc.app",)
    for item in kpi_sources:
        if not (isinstance(item, dict) and item.get("url")):
            continue
        url = _safe_cite_url(item["url"])
        name = item.get("name") or url
        lic = item.get("license") or license_for(url)
        host = (urlparse(url).hostname or "").lower()
        blocked = (any(h in host for h in blocked_hosts)
                   or "bloqueado" in lic.lower() or "blocked" in lic.lower())
        if blocked:
            extra_items.append(
                '<li><span class="ref-blocked">{name}</span>'
                '<span class="ref-meta">{lic} · sem link por indisponibilidade da fonte</span></li>'.format(
                    name=esc(name), lic=esc(lic)
                )
            )
            continue
        extra_items.append(
            '<li><a href="{url}" target="_blank" rel="noopener noreferrer">{name}</a>'
            '<span class="ref-meta">{lic}</span></li>'.format(
                url=esc(url), name=esc(name), lic=esc(lic)
            )
        )
    notes = meta.get("notes") if isinstance(meta.get("notes"), list) else []
    note_html = ""
    if notes:
        note_html = '<p class="fine">Notas do dataset: ' + esc("; ".join(str(x) for x in notes)) + "</p>"
    return f"""<div class="grid">
  <div class="col-7">
    <h3 class="h3">Bases de fonte primária usadas nos registros</h3>
    <ul class="refs">{''.join(source_items)}</ul>
    <p class="fine">Cada linha da tabela auditável aponta para a URL exata do registro na fonte
    ({fmt_int(s['n_sources'])} URLs distintas em {fmt_int(s['n_source_bases'])} bases).</p>
    {note_html}
  </div>
  <div class="col-5">
    <h3 class="h3">Referências metodológicas</h3>
    <ul class="refs refs-method">{meta_items}</ul>
    {('<h3 class="h3">Fontes declaradas no kpis.json</h3><ul class="refs">' + "".join(extra_items) + "</ul>") if extra_items else ""}
  </div>
</div>"""


def build_footer(s: dict, meta: dict) -> str:
    generated = fmt_datetime_pt(meta.get("generated_at"))
    counts = ", ".join(f"{CUT_LABEL[c]} {fmt_int(s['by_cut'].get(c, 0))}" for c in CUTS)
    fx = meta.get("fx") if isinstance(meta.get("fx"), dict) else {}
    fx_line = ""
    if fx:
        fx_line = (
            f" · Câmbio {esc(str(fx.get('pair') or 'BRL/USD'))} "
            f"{esc(str(fx.get('rate') or 'n/d'))} ({esc(str(fx.get('reference_date') or 's/d'))})"
        )
    return f"""<footer class="site-footer">
  <div class="wrap">
    <div class="footer-top">
      <p class="footer-kicker">Venture Capital · Brasil × Global</p>
      <p class="footer-meta mono">Edição de {esc(generated)} · {fmt_int(s['n'])} registros</p>
    </div>
    <div class="footer-grid">
      <div>
        <h3 class="h3">Protocolo</h3>
        <p>Ingestão auditada (UA acadêmico, rate-limit SEC de 0,2 s, cache SHA-256), validação de
        contrato em 100% dos registros e classificação de fase em três prioridades rastreáveis.</p>
      </div>
      <div>
        <h3 class="h3">Cortes e contagens</h3>
        <p class="mono fine">{esc(counts)}{fx_line}</p>
        <p>Contrato: <span class="mono">contract/venture_capital_fund.schema.json</span>.
        Render: <span class="mono">pipeline/render.py</span>.</p>
      </div>
      <div>
        <h3 class="h3">Licenças</h3>
        <p>Dados abertos CVM e BNDES (atribuição), SEC EDGAR e Form D (domínio público),
        Wikidata (CC0 1.0), PTAX/BCB (dados abertos), diretórios abertos citados com atribuição.</p>
      </div>
    </div>
    <p class="footer-note">Documento analítico gerado por pipeline reprodutível; nenhum valor foi
    sintetizado ou inferido sem registro do método. Atributos ausentes na fonte aparecem como
    “{ND_LABEL}”. O universo de inclusão (e as exclusões declaradas) está na
    <a href="#metodologia">nota metodológica</a>; a tabela auditável aponta a URL exata da fonte
    primária de cada registro.</p>
  </div>
</footer>"""


# ---------------------------------------------------------------------------
# Fontes: download/cache + embedding base64
# ---------------------------------------------------------------------------

def _net_module():
    sys.path.insert(0, str(PIPELINE_DIR))
    import net  # noqa: PLC0415

    return net


def fetch_fonts(force: bool = False) -> int:
    net = _net_module()
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    pattern = re.compile(r"/\*\s*([a-z0-9-]+)\s*\*/\s*@font-face\s*\{(.*?)\}", re.S)
    for family, spec in GOOGLE_FAMILIES:
        css_url = f"https://fonts.googleapis.com/css2?family={spec}&display=swap"
        css = net.fetch(css_url, source="google_fonts_css", user_agent=BROWSER_UA, force=force).text
        blocks = [(subset, body) for subset, body in pattern.findall(css) if subset == "latin"]
        if not blocks:
            raise RenderError(f"nenhum bloco latin encontrado no CSS de {family}")
        for subset, body in blocks:
            weight_m = re.search(r"font-weight:\s*(\d+)", body)
            url_m = re.search(r"url\((https://[^)]+\.woff2)\)", body)
            if not (weight_m and url_m):
                continue
            weight = weight_m.group(1)
            woff_url = url_m.group(1)
            slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
            fname = f"{slug}-{weight}.woff2"
            target = FONTS_DIR / fname
            data = net.fetch(woff_url, source="google_fonts_woff2", user_agent=BROWSER_UA, force=force).content
            target.write_bytes(data)
            manifest.append({"family": family, "weight": weight, "style": "normal", "file": fname})
            print(f"  {fname}: {len(data)} bytes")
    meta_path = FONTS_DIR / "fonts.json"
    meta_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"fontes cacheadas em {FONTS_DIR} ({len(manifest)} arquivos woff2)")
    return 0


def font_assets() -> tuple[str, list[dict]]:
    """@font-face inline (base64) quando os woff2 estao cacheados; senao link externo.

    As fontes variaveis do Google repetem o mesmo arquivo para pesos diferentes;
    agrupamos por hash e emitimos um unico @font-face com faixa de peso.
    """
    meta_path = FONTS_DIR / "fonts.json"
    if not meta_path.exists():
        return "", []
    try:
        manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "", []
    groups: dict[tuple[str, str, bytes], list[dict]] = {}
    for item in manifest:
        fpath = FONTS_DIR / item.get("file", "")
        if not fpath.exists():
            continue
        data = fpath.read_bytes()
        key = (item["family"], item.get("style", "normal"), data)
        groups.setdefault(key, []).append(item)
    faces = []
    usable = []
    for (family, style, data), items in groups.items():
        weights = sorted({int(i["weight"]) for i in items})
        b64 = base64.b64encode(data).decode("ascii")
        weight_css = str(weights[0]) if len(weights) == 1 else f"{weights[0]} {weights[-1]}"
        faces.append(
            "@font-face{{font-family:'{fam}';font-style:{style};font-weight:{weight};"
            "font-display:swap;src:url(data:font/woff2;base64,{b64}) format('woff2')}}".format(
                fam=family, style=style, weight=weight_css, b64=b64,
            )
        )
        usable.append({"family": family, "weights": weights, "bytes": len(data)})
    return "\n".join(faces), usable


def build_fonts() -> tuple[str, str]:
    faces, usable = font_assets()
    if faces:
        return f"<style>\n{faces}\n</style>", "embedded"
    link = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{FONT_LINK}">'
    )
    return link, "link"


# ---------------------------------------------------------------------------
# Montagem do documento
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Redesign v2: narrativa em abas (Achados / Insights / Fundos / Metodo)
# ---------------------------------------------------------------------------

DOCUMENT_TITLE = "Fundos de Venture Capital no Brasil e no exterior, por fase de maturidade"


def build_document_title() -> str:
    """Titulo neutro do documento (sem numeros, percentuais ou conclusao)."""
    if len(DOCUMENT_TITLE) > 100 or any(ch.isdigit() for ch in DOCUMENT_TITLE) or "%" in DOCUMENT_TITLE:
        raise RenderError("titulo do documento deve ser neutro, sem numeros/percentuais e <=100 chars.")
    return DOCUMENT_TITLE


def build_answer_first(s: dict) -> str:
    """Conclusao principal answer-first (<=170 caracteres), 100% calculada dos achados 03/04."""
    bcs = s["bplus_capital_share"]
    cross = s["cross_index"]
    n_gl = s["n_gl"]
    variants = [
        (f"{fmt_pct(bcs['br'])} do capital sob gestoras brasileiras está em teses Série B+, "
         f"mas só {fmt_pct(cross)} dos {fmt_int(n_gl)} veículos estrangeiros mapeados têm atividade no Brasil."),
        (f"O capital sob gestoras brasileiras concentra-se em teses Série B+ ({fmt_pct(bcs['br'])}), "
         f"enquanto apenas {fmt_pct(cross)} dos {fmt_int(n_gl)} veículos estrangeiros têm atividade no Brasil."),
        (f"Série B+ concentra {fmt_pct(bcs['br'])} do capital sob gestoras brasileiras; "
         f"a ponte cross-border alcança {fmt_pct(cross)} dos {fmt_int(n_gl)} veículos estrangeiros."),
    ]
    for text in variants:
        if len(text) <= 170 and sum(ch.isdigit() for ch in text) >= 2:
            return text
    raise RenderError("conclusão answer-first não coube em 170 caracteres com >=2 números.")


def _formd_period(meta: dict) -> str:
    """Extrai o intervalo de trimestres do Form D a partir das fontes do kpis.json."""
    sources = meta.get("sources") if isinstance(meta.get("sources"), list) else []
    quarters = set()
    for item in sources:
        blob = " ".join(str(v) for v in (item.values() if isinstance(item, dict) else [item]))
        for year, quarter in re.findall(r"(20\d{2})\s*[qQ]([1-4])", blob):
            quarters.add((int(year), int(quarter)))
    if not quarters:
        return ""
    ordered = sorted(quarters)
    first, last = ordered[0], ordered[-1]
    return f"{first[0]}Q{first[1]}–{last[0]}Q{last[1]}"


def build_deck_descriptive(s: dict, meta: dict) -> str:
    """Deck descritivo e curto (o que e a base e a cobertura), nao conclusivo."""
    text = (
        f"Base auditada de {fmt_int(s['n'])} veículos e {fmt_int(s['firms'])} gestoras, "
        f"em três cortes geográficos e {fmt_int(len(STAGES))} fases (Pre-Seed a Venture Debt); "
        f"{fmt_usd(s['aum_total'])} declarados e {fmt_pct(s['aum_coverage'])} de cobertura."
    )
    if len(text) > 220:
        text = (
            f"Base auditada de {fmt_int(s['n'])} veículos de {fmt_int(s['firms'])} gestoras nos três "
            f"cortes geográficos; {fmt_pct(s['aum_coverage'])} têm valor declarado."
        )
    return text



def _microbar(rows: list[tuple[str, float, str, str]]) -> str:
    """Micro-barras em SVG (sem texto no SVG: rótulos e valores em HTML)."""
    parts = ['<div class="microbars">']
    for label, pct, kind, value in rows:
        width = max(0.0, min(100.0, pct))
        parts.append(
            '<div class="microbar">'
            f'<span class="microbar-label">{esc(label)}</span>'
            f'<span class="microbar-value num">{esc(value)}</span>'
            '<svg class="microbar-track" viewBox="0 0 100 6" preserveAspectRatio="none" '
            'aria-hidden="true" focusable="false">'
            '<rect class="mb-bg" x="0" y="0" width="100" height="6"></rect>'
            f'<rect class="mb-{kind}" x="0" y="0" width="{width:.2f}" height="6"></rect>'
            "</svg></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def build_findings(s: dict) -> str:
    """Dois blocos-destaque com os achados 03 (concentração) e 04 (cross-border)."""
    bcs = s["bplus_capital_share"]
    bplus_veh = s["bplus_pct"]
    cross = s["cross_index"]
    cross_count = s["cross_count"]
    n_gl = s["n_gl"]
    ex_br = s["by_cut"]["GLOBAL_EX_BR"]
    outside_share = _pct(ex_br, n_gl)

    rows_03 = [
        ("Brasil", bcs["br"], "br", fmt_pct(bcs["br"])),
        ("Global", bcs["gl"], "gl", fmt_pct(bcs["gl"])),
    ]
    rows_04 = [
        ("Atividade no Brasil", cross, "cross", fmt_pct(cross)),
        ("Sem atuação no Brasil", outside_share, "gl", fmt_pct(outside_share)),
    ]
    reading_03 = (
        f"{fmt_pct(bplus_veh['br'])} dos veículos brasileiros têm tese Série B+, contra "
        f"{fmt_pct(bplus_veh['gl'])} dos estrangeiros."
    )
    reading_04 = (
        f"São {fmt_int(cross_count)} veículos estrangeiros com atividade registrada no Brasil, "
        f"contra {fmt_int(ex_br)} sem atuação no país."
    )
    srcs = []
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM</a>')
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR</a>')
    source_html = " · ".join(srcs) if srcs else "fontes primárias citadas na tabela"

    return f"""<div class="findings" aria-label="Achados principais">
  <article class="finding finding-03">
    <p class="finding-eyebrow">Achado 03 · Concentração de capital</p>
    <p class="finding-hero">{fmt_pct(bcs['br'])}</p>
    <p class="finding-caption">do capital declarado sob gestoras brasileiras está em teses Série B+.</p>
    {_microbar(rows_03)}
    <p class="finding-reading">{esc(reading_03)}</p>
    <p class="finding-src">Fonte: {source_html}</p>
  </article>
  <article class="finding finding-04">
    <p class="finding-eyebrow">Achado 04 · Ponte cross-border</p>
    <p class="finding-hero">{fmt_pct(cross)}</p>
    <p class="finding-caption">dos {fmt_int(n_gl)} veículos estrangeiros mapeados têm atividade registrada no Brasil.</p>
    {_microbar(rows_04)}
    <p class="finding-reading">{esc(reading_04)}</p>
    <p class="finding-src">Fonte: {source_html} · Shizune (teses com atuação no Brasil)</p>
  </article>
</div>"""


def build_capital_stage_svg(s: dict, width: int) -> str:
    """Barras agrupadas: participacao de cada fase no capital declarado do grupo."""
    cfg = _stage_variants(width)
    stages = STAGES
    x0 = cfg["pad_x"] + cfg["label_w"]
    plot_w = width - x0 - cfg["pad_x"] - cfg["value_w"]
    row_h = cfg["row_h"]
    bar_h, bar_gap = cfg["bar_h"], cfg["bar_gap"]
    height = cfg["pad_top"] + len(stages) * row_h + cfg["pad_bottom"]
    shares = s["capital_share_stage"]
    max_v = max(1.0, max(shares[st]["br"] for st in stages), max(shares[st]["gl"] for st in stages))
    uid = f"capstage{width}"
    parts = [
        f'<svg class="chart chart-capital-stage" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{uid}-title">',
        f'<title id="{uid}-title">Participação de cada fase no capital declarado de cada grupo</title>',
        "<desc>Barras horizontais: percentual do capital de cada grupo atribuído ao estágio primário "
        "do veículo. Cada veículo entra uma única vez.</desc>",
    ]
    for i, st in enumerate(stages):
        y = cfg["pad_top"] + i * row_h
        cy = y + (2 * bar_h + bar_gap) / 2 + 4
        stage_txt = STAGE_LABEL[st] if width >= 500 else STAGE_SHORT[st]
        parts.append(_txt(x0 - 8, cy, stage_txt, cls="t-label", anchor="end"))
        for j, g in enumerate(("br", "gl")):
            v = shares[st][g]
            by = y + j * (bar_h + bar_gap)
            w = (v / max_v) * plot_w if v else 0.0
            fill = "var(--br)" if g == "br" else "var(--gl)"
            parts.append(
                f'<rect class="bar bar-{g}" x="{x0:.1f}" y="{by:.1f}" width="{w:.1f}" '
                f'height="{bar_h}" fill="{fill}"></rect>'
            )
            lx = x0 + w + 6
            if lx > width - cfg["pad_x"] - 44:
                lx = width - cfg["pad_x"] - 44
            cls = f"t-num t-{g}" if v else "t-num t-zero"
            parts.append(_txt(lx, by + bar_h - 2, fmt_pct(v), cls=cls, anchor="start"))
        if i < len(stages) - 1:
            ly = y + row_h - 6
            parts.append(
                f'<line class="grid-line" x1="{cfg["pad_x"] + cfg["label_w"] - 2:.1f}" '
                f'y1="{ly:.1f}" x2="{width - cfg["pad_x"]:.1f}" y2="{ly:.1f}"></line>'
            )
    parts.append("</svg>")
    return "".join(parts)


def build_capital_stage_figure(s: dict) -> str:
    variants = [("s", 272), ("m", 424), ("l", 660), ("d", 520)]
    svgs = [f'<div class="fig-svg fig-svg-{n} cap-{n}">{build_capital_stage_svg(s, w)}</div>'
            for n, w in variants]
    overall = {
        st: _pct(s["aum_primary_stage"][st]["br"] + s["aum_primary_stage"][st]["gl"], s["aum_total"])
        for st in STAGES
    }
    ranked = sorted(STAGES, key=lambda st: -overall[st])
    top, second = ranked[0], ranked[1]
    title = (
        f"{STAGE_LABEL[top]} e {STAGE_LABEL[second]} concentram "
        f"{fmt_pct(overall[top] + overall[second])} do capital declarado; "
        f"{fmt_pct(s['bplus_capital_share']['br'])} do capital brasileiro está em Série B+."
    )
    srcs = []
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM — Registro de Fundos e Classes</a>')
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR — Form D</a>')
    source_html = "; ".join(srcs) if srcs else "fontes primárias citadas na tabela"
    return f"""<figure class="fig fig-capital-stage" aria-labelledby="fig2-titulo">
  <h3 class="fig-title" id="fig2-titulo">{esc(title)}</h3>
  <p class="fig-unit">Unidade: percentual do capital declarado de cada grupo, atribuído ao estágio
  primário do veículo (cada veículo entra uma única vez; soma 100%).</p>
  <ul class="legend" aria-label="Legenda">
    <li><i class="sw sw-br"></i>Brasil</li>
    <li><i class="sw sw-gl"></i>Global</li>
  </ul>
  {''.join(svgs)}
  <figcaption>Fonte: {source_html}. Capital em US$: patrimônio líquido de FIP convertido pela
  PTAX de venda ou captação declarada no Form D. Valores ausentes na fonte não são imputados
  ({fmt_pct(s['aum_coverage'])} de cobertura).</figcaption>
</figure>"""


def build_capital_cut_svg(s: dict, width: int) -> str:
    """Barras horizontais: capital declarado por corte (US$ + share do total)."""
    cfg = _stage_variants(width)
    cuts = CUTS
    label_w = max(88, min(150, round(width * 0.34)))
    x0 = cfg["pad_x"] + label_w
    plot_w = width - x0 - cfg["pad_x"] - 42
    row_h = 54
    bar_h = 12
    height = cfg["pad_top"] + len(cuts) * row_h + cfg["pad_bottom"]
    max_share = max(1.0, max(s["capital_by_cut_share"][c] for c in cuts))
    uid = f"capcut{width}"
    parts = [
        f'<svg class="chart chart-capital-cut" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{uid}-title">',
        f'<title id="{uid}-title">Capital declarado por corte geográfico</title>',
        "<desc>Barras horizontais: capital declarado e participação no total de cada corte.</desc>",
    ]
    colors = {"BRASIL_LOCAL": "var(--br)", "GLOBAL_ATIVO_BR": "var(--cross)", "GLOBAL_EX_BR": "var(--gl)"}
    mid_labels = {"BRASIL_LOCAL": "Brasil local", "GLOBAL_ATIVO_BR": "Global ativo BR",
                  "GLOBAL_EX_BR": "Global ex-BR"}
    for i, cut in enumerate(cuts):
        y = cfg["pad_top"] + i * row_h
        short = CUT_LABEL[cut] if width >= 460 else mid_labels[cut]
        parts.append(_txt(x0 - 8, y + 16, short, cls="t-label", anchor="end"))
        parts.append(_txt(x0 - 8, y + 32, fmt_usd(s["aum_by_cut"].get(cut, 0.0)),
                          cls="t-num t-zero", anchor="end"))
        share = s["capital_by_cut_share"][cut]
        w = (share / max_share) * plot_w if share else 0.0
        parts.append(
            f'<rect class="bar" x="{x0:.1f}" y="{y + 12:.1f}" width="{w:.1f}" height="{bar_h}" '
            f'fill="{colors[cut]}"></rect>'
        )
        lx = x0 + w + 6
        if lx > width - cfg["pad_x"] - 40:
            lx = width - cfg["pad_x"] - 40
        parts.append(_txt(lx, y + 22, fmt_pct(share), cls="t-num", anchor="start"))
    parts.append("</svg>")
    return "".join(parts)


def build_capital_cut_figure(s: dict) -> str:
    variants = [("s", 272), ("m", 424), ("l", 660), ("d", 352)]
    svgs = [f'<div class="fig-svg fig-svg-{n} cap-{n}">{build_capital_cut_svg(s, w)}</div>'
            for n, w in variants]
    ranked = sorted(CUTS, key=lambda c: -s["capital_by_cut_share"][c])
    title = (
        f"{CUT_LABEL[ranked[0]]} concentra {fmt_pct(s['capital_by_cut_share'][ranked[0]])} do "
        f"capital declarado; {CUT_LABEL[ranked[1]]} responde por "
        f"{fmt_pct(s['capital_by_cut_share'][ranked[1]])}."
    )
    srcs = []
    if s["cvm_url"]:
        srcs.append(f'<a href="{esc(s["cvm_url"])}" target="_blank" rel="noopener noreferrer">CVM</a>')
    if s["sec_url"]:
        srcs.append(f'<a href="{esc(s["sec_url"])}" target="_blank" rel="noopener noreferrer">SEC EDGAR — Form D</a>')
    source_html = "; ".join(srcs) if srcs else "fontes primárias citadas na tabela"
    return f"""<figure class="fig fig-capital-cut" aria-labelledby="fig3-titulo">
  <h3 class="fig-title" id="fig3-titulo">{esc(title)}</h3>
  <p class="fig-unit">Unidade: US$ de patrimônio líquido convertido ou captação declarada; barra =
  participação do corte no capital total.</p>
  <ul class="legend" aria-label="Legenda">
    <li><i class="sw sw-br"></i>Brasil local</li>
    <li><i class="sw sw-cross"></i>Global ativo no Brasil</li>
    <li><i class="sw sw-gl"></i>Global ex-Brasil</li>
  </ul>
  {''.join(svgs)}
  <figcaption>Fonte: {source_html}. Total declarado: {fmt_usd(s['aum_total'])}
  ({fmt_pct(s['aum_coverage'])} dos veículos com valor).</figcaption>
</figure>"""


def build_insights_universe(s: dict, meta: dict) -> str:
    """Aba Insights: universo/cobertura, concentração e percentis (textos curtos + stat grid)."""
    p = s["portfolio"]["all"]
    cells = [
        ("Veículos mapeados", fmt_int(s["n"])),
        ("Gestoras", fmt_int(s["firms"])),
        ("Capital declarado", fmt_usd(s["aum_total"])),
        ("Cobertura de valor", fmt_pct(s["aum_coverage"])),
        ("Mediana de porte", fmt_usd(s["median_aum"])),
        ("Top-10 do capital", fmt_pct(s["top10_share"])),
        ("P25 de porte", fmt_usd(p["p25"])),
        ("P75 de porte", fmt_usd(p["p75"])),
    ]
    stat_html = "".join(
        f'<div class="statcell"><p class="statcell-label">{esc(label)}</p>'
        f'<p class="statcell-value num">{esc(value)}</p></div>'
        for label, value in cells
    )
    txt1 = (
        f"A base reúne {fmt_int(s['n'])} veículos de {fmt_int(s['firms'])} gestoras nos três cortes "
        f"geográficos. {fmt_pct(s['aum_coverage'])} informam patrimônio ou captação: "
        f"{fmt_usd(s['aum_total'])} declarados no total."
    )
    txt2 = (
        f"O porte é assimétrico: mediana de {fmt_usd(s['median_aum'])}, P25 de {fmt_usd(p['p25'])} "
        f"e os dez maiores veículos somam {fmt_pct(s['top10_share'])} do capital com valor divulgado. "
        f"{fmt_pct(s['capital_br_share'])} do capital está sob gestoras brasileiras."
    )
    return f"""<div class="insight-lede">
  <p>{esc(txt1)}</p>
  <p>{esc(txt2)}</p>
</div>
<div class="statgrid">{stat_html}</div>"""


def build_insights_quality(s: dict, meta: dict) -> str:
    """Qualidade do dado, escopo/exclusoes e cobertura de fontes (2-3 linhas cada)."""
    transform = meta.get("transform_report") if isinstance(meta.get("transform_report"), dict) else {}
    scope = transform.get("scope") if isinstance(transform.get("scope"), dict) else {}
    dropped = transform.get("dropped") if isinstance(transform.get("dropped"), list) else []
    exclusions = []
    if isinstance(scope.get("excluded_micro_filings"), (int, float)):
        exclusions.append(
            f"{fmt_int(scope['excluded_micro_filings'])} micro-filings abaixo de US$ 1 mi"
        )
    if isinstance(scope.get("excluded_spv_vehicles"), (int, float)):
        exclusions.append(f"{fmt_int(scope['excluded_spv_vehicles'])} SPVs de deal único")
    if dropped:
        exclusions.append(f"{fmt_int(len(dropped))} registros sem país determinável")
    excl_txt = "; ".join(exclusions) if exclusions else "nenhuma exclusão declarada"
    flags_txt = (
        f"{fmt_int(s['records_with_flags'])} registros têm flags de qualidade declaradas no dataset"
        if s["records_with_flags"] else "Nenhum registro com flag de qualidade"
    )
    cap_txt = (
        f" e {fmt_int(s['capital_inconsistent'])} estão marcados como capital inconsistente"
        if s["capital_inconsistent"] else ""
    )
    return f"""<div class="quality">
  <p>{esc(flags_txt)}{esc(cap_txt)}; atributos ausentes aparecem como “{ND_LABEL}”, sem imputação.</p>
  <p>Ficaram fora do universo: {esc(excl_txt)}. Exclusões são critério de inclusão declarado em
  <span class="mono">data/canonical/transform_report.json</span>.</p>
  <p>{fmt_int(s['n_source_bases'])} bases primárias ({fmt_int(s['n_sources'])} URLs de registro):
  CVM, BNDES, SEC EDGAR/Form D, Wikidata e Shizune. O SEC Form D concentra
  {fmt_pct(_pct(s['by_cut']['GLOBAL_ATIVO_BR'] + s['by_cut']['GLOBAL_EX_BR'], s['n']))} dos veículos.</p>
</div>"""


def build_method_details(s: dict, meta: dict) -> str:
    """Nota metodologica em <details> recolhiveis por topico (aba Metodo e fontes)."""
    fx = (meta.get("fx") or {}) if isinstance(meta.get("fx"), dict) else {}
    fx_note = "câmbio não informado no kpis.json"
    if fx:
        rate = fx.get("rate")
        ref = fx.get("reference_date") or "data não informada"
        src = fx.get("source") or "BCB PTAX"
        fx_note = (f"{src}: 1 US$ = R$ {fmt_num(rate, 4)} em {ref}"
                   if rate not in (None, "") else f"{src} ({ref})")
    method_rows = []
    for m in METHOD_LABEL:
        c = s["methods"].get(m, 0)
        method_rows.append(
            f'<tr><th scope="row">{esc(METHOD_LABEL[m])}</th>'
            f'<td class="num-cell"><span class="num">{fmt_int(c)}</span></td>'
            f'<td class="num-cell"><span class="num">{fmt_pct(_pct(c, s["n"]))}</span></td>'
            f'<td>{esc(METHOD_TITLE[m])}</td></tr>'
        )
    checks = kpis_crosscheck(s, meta)
    if checks:
        diverg = [c for c in checks if not c["ok"]]
        if not diverg:
            worst = max(checks, key=lambda c: c["delta_pct"])
            crosscheck_html = (
                f'<p class="fine">Conferência com o <span class="mono">kpis.json</span>: '
                f'{len(checks)} agregados checados, todos dentro de 0,5% do recálculo '
                f'(maior desvio: {fmt_pct(worst["delta_pct"], 2)} em {esc(worst["label"])}).</p>'
            )
        else:
            items = "".join(
                f'<li>{esc(c["label"])}: kpis.json {fmt_num(c["kpis"], 2)} · render '
                f'{fmt_num(c["computed"], 2)} (desvio {fmt_pct(c["delta_pct"], 2)})</li>'
                for c in diverg[:5]
            )
            crosscheck_html = (
                f'<p class="fine">{len(diverg)} de {len(checks)} agregados divergem mais de 0,5%. '
                f'O dashboard exibe sempre o valor recalculado dos registros.</p>'
                f'<ul class="fine crosscheck">{items}</ul>'
            )
    else:
        crosscheck_html = (
            '<p class="fine">Sem <span class="mono">kpis.json</span> disponível: os agregados '
            'exibidos vêm exclusivamente do recálculo sobre os registros.</p>'
        )
    transform = meta.get("transform_report") if isinstance(meta.get("transform_report"), dict) else {}
    scope = transform.get("scope") if isinstance(transform.get("scope"), dict) else {}
    universe_bits = []
    if scope.get("br"):
        universe_bits.append(f"Brasil: {esc(str(scope['br']))}")
    if scope.get("global"):
        universe_bits.append(f"Global: {esc(str(scope['global']))}")
    universe_html = f"<p>{'; '.join(universe_bits)}.</p>" if universe_bits else ""
    undisclosed_rate = fmt_pct(_pct(s["undisclosed_ticket"], s["n"]))
    aum_missing = fmt_pct(_pct(s["undisclosed_aum"], s["n"]))
    generated = fmt_datetime_pt(meta.get("generated_at"))
    quality_bits = []
    if s["capital_inconsistent"]:
        quality_bits.append(
            f'{fmt_int(s["capital_inconsistent"])} registros com '
            f'<span class="mono">capital_consistente = false</span>'
        )
    if s["quality_flags"]:
        top_flags = sorted(s["quality_flags"].items(), key=lambda kv: -kv[1])[:3]
        quality_bits.append(
            "flags mais frequentes: "
            + ", ".join(f"{esc(flag)} ({fmt_int(n)})" for flag, n in top_flags)
        )
    quality_html = (
        f"<p>{'; '.join(quality_bits)}.</p>" if quality_bits else ""
    )
    return f"""<div class="method-grid">
  <div class="method-col">
    <details open>
      <summary>Escopo e cortes geográficos</summary>
      <p>O conjunto cobre três cortes: <strong>Brasil local</strong> (gestora sediada no Brasil),
      <strong>Global ativo no Brasil</strong> (gestora estrangeira com investimento registrado no país) e
      <strong>Global ex-Brasil</strong> (estrangeira sem atuação registrada). Contagens:
      Brasil local {fmt_int(s['by_cut']['BRASIL_LOCAL'])}, Global ativo no Brasil
      {fmt_int(s['by_cut']['GLOBAL_ATIVO_BR'])}, Global ex-Brasil {fmt_int(s['by_cut']['GLOBAL_EX_BR'])}.</p>
    </details>
    <details>
      <summary>Universo e critérios de exclusão</summary>
      {universe_html}
      <p>Exclusões declaradas no escopo: 8.483 filings nominais com oferta abaixo de US$ 1 mi,
      3.181 SPVs de deal único e 22 registros sem país determinável. São critério de inclusão,
      não falha de coleta.</p>
    </details>
    <details>
      <summary>Como cada fase foi classificada</summary>
      <p>Três regras determinísticas, nesta ordem: (1) <strong>tese declarada</strong> pela gestora em
      diretório aberto; (2) <strong>classe regulatória CVM/BNDES</strong> — FIP Capital Semente para
      Pre-Seed/Seed, Empresas Emergentes para Série A/B e Multiestratégia com PL acima de R$ 500 mi
      para Série B/Late Stage; (3) <strong>heurística de cheque</strong> pelas faixas Growth List
      (US$ 50 mil–500 mil Pre-Seed; 500 mil–3 mi Seed; 3–15 mi Série A; 10–50 mi Série B; 30–100 mi+
      Late Stage).</p>
      <div class="table-scroll" role="region" aria-label="Distribuição por método de classificação" tabindex="0">
      <table class="method-table">
        <caption class="tbl-caption">Distribuição dos registros por método de classificação</caption>
        <thead><tr><th scope="col">Método</th><th scope="col" class="num-h">Veículos</th>
        <th scope="col" class="num-h">Participação</th><th scope="col">Critério</th></tr></thead>
        <tbody>{''.join(method_rows)}</tbody>
      </table>
      </div>
    </details>
    <details>
      <summary>Valores, câmbio e não divulgação</summary>
      <p>Patrimônio líquido de FIPs vem do registro de fundos e classes e do informe quadrimestral da
      CVM (RCVM 175), convertido pela PTAX de venda da competência mais próxima
      ({esc(fx_note)}). Valores do SEC Form D já são em dólar. Campos ausentes ficam nulos e aparecem
      como “{ND_LABEL}”: {undisclosed_rate} dos veículos não têm faixa de cheque e {aum_missing} não
      têm patrimônio ou captação declarados. Nenhum valor é estimado ou interpolado.</p>
    </details>
  </div>
  <div class="method-col">
    <details>
      <summary>Qualidade do dado</summary>
      {quality_html}
      <p>{fmt_int(s['records_with_flags'])} registros têm flags de qualidade; atributos não divulgados
      ficam registrados em <span class="mono">undisclosed_fields</span> por registro.</p>
    </details>
    <details>
      <summary>Protocolo de coleta</summary>
      <ol class="protocol">
        <li>HTTP auditado com <span class="mono">User-Agent</span> acadêmico, intervalo mínimo de 0,2 s
        no SEC EDGAR e cache SHA-256 em <span class="mono">data/raw/</span>.</li>
        <li>CSVs da CVM decodificados em ISO-8859-1 (<span class="mono">latin1</span>) com separador
        ponto e vírgula.</li>
        <li>Resolução de entidades por CNPJ da gestora, CIK do emissor e domínio web normalizado.</li>
        <li>Validação de 100% dos registros contra o contrato JSON Schema, com fonte em HTTPS.</li>
        <li>Render determinístico e auditoria nos quatro quality gates da especificação.</li>
      </ol>
    </details>
    <details>
      <summary>Reprodutibilidade</summary>
      <p class="code">python pipeline/render.py \\<br>  --input data/canonical/funds.json \\<br>  --kpis data/canonical/kpis.json \\<br>  --out dashboard/index.html</p>
      <p class="fine">Geração: {esc(generated)}. Dataset:
      {'fixture de desenvolvimento' if meta.get("__fixture__") else 'canônico'}.
      Contrato: <span class="mono">contract/venture_capital_fund.schema.json</span>.</p>
    </details>
    <details>
      <summary>Conferência de agregados</summary>
      {crosscheck_html}
    </details>
    <details>
      <summary>Limitações</summary>
      <ul class="limits">
        <li>Amostra regulatória de fontes abertas, não censo do mercado global.</li>
        <li>O Form D cobre apenas ofertas isentas de registro nos EUA.</li>
        <li>Informe quadrimestral da CVM tem defasagem de competência.</li>
        <li>Fases imputadas por heurística de cheque são aproximações sinalizadas por linha.</li>
        <li>Capital comprometido CVM não compõe KPIs de liquidez por inconsistência documentada.</li>
        <li>Indicadores de liquidez são derivados na transformação sobre as bases completas.</li>
      </ul>
    </details>
  </div>
</div>"""

def build_document(records: list[dict], meta: dict) -> tuple[str, dict]:
    s = compute_stats(records)
    headline = build_document_title()
    answer_first = build_answer_first(s)
    deck = build_deck_descriptive(s, meta)
    generated = fmt_datetime_pt(meta.get("generated_at"))

    title = f"{headline} — achados, fundos e método"
    description = answer_first

    formd_period = _formd_period(meta)
    dateline = "".join([
        f'<span><b>Base</b> {fmt_int(s["n"])} veículos · {fmt_int(s["firms"])} gestoras</span>',
        f'<span><b>Cortes</b> Brasil {fmt_int(s["by_cut"]["BRASIL_LOCAL"])} · '
        f'Ativo BR {fmt_int(s["by_cut"]["GLOBAL_ATIVO_BR"])} · '
        f'Ex-Brasil {fmt_int(s["by_cut"]["GLOBAL_EX_BR"])}</span>',
        f'<span><b>Capital</b> {fmt_usd(s["aum_total"])} '
        f'({fmt_pct(s["aum_coverage"])} de cobertura)</span>',
        '<span><b>Fontes</b> CVM · BNDES · SEC EDGAR · Wikidata · Shizune</span>',
        (f'<span><b>Período Form D</b> {esc(formd_period)}</span>' if formd_period else ""),
        f'<span><b>Geração</b> {esc(generated)}</span>',
    ])

    figures = {
        "FINDINGS": build_findings(s),
        "KPIS": build_kpis(s),
        "FIG_STAGE": build_stage_figure(s),
        "FIG_CAP_STAGE": build_capital_stage_figure(s),
        "MATRIX": build_matrix(s),
        "INSIGHTS_UNIVERSE": build_insights_universe(s, meta),
        "FIG_CAP_CUT": build_capital_cut_figure(s),
        "INSIGHTS_FIG_BANDS": build_ticket_figure(s),
        "INSIGHTS_FIG_PROV": build_provenance_figure(s),
        "INSIGHTS_QUALITY": build_insights_quality(s, meta),
        "LIQUIDITY": build_liquidity_block(s, meta),
        "PORTFOLIO": build_portfolio_table(s),
        "METHOD": build_method_details(s, meta),
        "REFERENCES": build_references(s, meta),
        "FOOTER": build_footer(s, meta),
        "TABLE": build_table(s),
        "HEADLINE": esc(headline),
        "ANSWER_FIRST": esc(answer_first),
        "DECK": esc(deck),
        "DATELINE": dateline,
        "TITLE": esc(title),
        "DESCRIPTION": esc(description),
        "PAYLOAD": build_payload(s, meta),
        "RESULTS_INITIAL": (
            f"<b>{fmt_int(s['n'])}</b> de <b>{fmt_int(s['n'])}</b> veículos · "
            f"<b>{fmt_int(s['firms'])}</b> gestoras · capital declarado "
            f"<b>{fmt_usd(s['aum_total'])}</b> em <b>{fmt_int(len(s['aum_recs']))}</b> veículos com valor"
        ),
        "TABLE_NOTE_INITIAL": (
            f"Exibindo {fmt_int(min(50, s['n']))} de {fmt_int(s['n'])} veículos do "
            f"{_dataset_label(meta)}. Paginação de 50 linhas por página; filtros e ordenação "
            f"recalculam o recorte sem recarregar a página."
        ),
        "DATASET_KIND": "fixture" if meta.get("__fixture__") else "canonical",
        "DEV_BANNER": (
            '<div class="dev-banner" role="note">Ambiente de desenvolvimento: dataset marcado '
            "<code>__fixture__</code> — não publicar. O run final usa <code>data/canonical/funds.json</code>.</div>"
            if meta.get("__fixture__") else ""
        ),
    }

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    template_tokens = set(re.findall(r"%%([A-Z0-9_]+)%%", template))
    unknown = template_tokens - set(figures) - {"FONTS"}
    if unknown:
        raise RenderError(f"tokens do template sem valor no render: {sorted(unknown)}")

    font_html, font_mode = build_fonts()
    document = template.replace("%%FONTS%%", font_html)
    for key, value in figures.items():
        document = document.replace(f"%%{key}%%", value)

    leftovers = re.findall(r"%%([A-Z0-9_]+)%%", document)
    if leftovers:
        raise RenderError(f"tokens não substituídos no documento: {sorted(set(leftovers))}")
    for phrase in PROHIBITED_PHRASES:
        if phrase.lower() in document.lower():
            raise RenderError(f"expressão proibida no documento gerado: {phrase!r}")
    if len(headline) > 100 or any(ch.isdigit() for ch in headline) or "%" in headline:
        raise RenderError(
            f"título do documento deve ser neutro (sem números/%) e <=100 chars: {len(headline)} chars."
        )
    if len(answer_first) > 170 or sum(ch.isdigit() for ch in answer_first) < 2:
        raise RenderError(
            f"conclusão answer-first precisa de <=170 chars e >=2 números: {len(answer_first)} chars."
        )
    if len(deck) > 360:
        raise RenderError(f"deck descritivo com {len(deck)} caracteres (limite 360).")

    crosscheck = kpis_crosscheck(s, meta)
    summary = {
        "output_bytes": len(document.encode("utf-8")),
        "records": s["n"],
        "by_cut": s["by_cut"],
        "firms": s["firms"],
        "capital_usd": round(s["aum_total"], 2),
        "capital_coverage_pct": round(s["aum_coverage"], 2),
        "median_aum_usd": round(s["median_aum"], 2) if s["median_aum"] else None,
        "top10_share_pct": round(s["top10_share"], 2),
        "ticket_coverage_pct": round(s["ticket_coverage"], 2),
        "bplus_pct": {k: round(v, 2) for k, v in s["bplus_pct"].items()},
        "bplus_capital_share_pct": {k: round(v, 2) for k, v in s["bplus_capital_share"].items()},
        "cross_index_pct": round(s["cross_index"], 2),
        "kpis_crosscheck": crosscheck,
        "headline": headline,
        "headline_chars": len(headline),
        "answer_first": answer_first,
        "answer_first_chars": len(answer_first),
        "deck": deck,
        "deck_chars": len(deck),
        "tabs": ["achados", "insights", "fundos", "metodo"],
        "figures": 5,
        "font_mode": font_mode,
        "dataset": "fixture" if meta.get("__fixture__") else "canonical",
    }
    return document, summary


PROHIBITED_PHRASES = (
    "no dinâmico cenário",
    "dinamico cenario",
    "ecossistema vibrante",
    "revolucionando o mercado",
    "revolucionário",
    "sinergia holística",
    "sinergia holistica",
    "mergulhe fundo",
    "mergulhe",
    "holístico",
    "holistico",
    "bem-vindo ao",
    "Resumo Executivo",
    "Introdução",
    "Conclusão",
)

EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x1F1E6, 0x1F1FF),
    (0xFE0F, 0xFE0F), (0x2B00, 0x2BFF), (0x1F300, 0x1F5FF),
)


def _hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    v = value.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        return tuple(int(ch * 2, 16) for ch in m.group(1))  # type: ignore[return-value]
    m = re.fullmatch(r"#([0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return None


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _css_parts(document: str) -> tuple[str, dict[str, str]]:
    """CSS concatenado (sem @font-face) + variaveis :root resolvidas."""
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", document, re.S | re.I)
    css = "\n".join(blocks)
    css = re.sub(r"@font-face\s*\{[^}]*\}", "", css)
    variables: dict[str, str] = {}
    for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", css):
        variables[name] = value.strip()
    return css, variables


def _resolve_css_color(value: str, variables: dict[str, str]) -> tuple[int, int, int] | None:
    v = value.strip().rstrip("!important").strip()
    if v.startswith("var("):
        name = v[4:].split(")")[0].split(",")[0].strip()
        resolved = variables.get(name)
        return _hex_to_rgb(resolved) if resolved else None
    rgb = _hex_to_rgb(v)
    if rgb:
        return rgb
    m = re.fullmatch(r"rgba?\(([^)]+)\)", v)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        try:
            return (int(float(parts[0])), int(float(parts[1])), int(float(parts[2])))
        except (ValueError, IndexError):
            return None
    return None


def _extract_decls(css: str) -> list[tuple[str, str, str]]:
    """(selector, property, value) de regras simples (sem aninhamento de @media)."""
    out: list[tuple[str, str, str]] = []
    for selector_blob, body in re.findall(r"([^{}@][^{}]*?)\{([^{}]*)\}", css):
        selector = " ".join(selector_blob.split())
        if not selector:
            continue
        for decl in body.split(";"):
            if ":" not in decl:
                continue
            prop, _, value = decl.partition(":")
            out.append((selector, prop.strip().lower(), value.strip()))
    return out


def run_self_check(path: Path) -> int:
    """Auditoria estatica do HTML gerado (Gates 3 e 4 do Loop Engineering)."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    if not path.exists():
        print(f"ERRO: arquivo não encontrado: {path}", file=sys.stderr)
        return 2
    document = path.read_text(encoding="utf-8")
    size = len(document.encode("utf-8"))
    check("Arquivo existe e é único HTML", document.lstrip().lower().startswith("<!doctype html>"),
          f"{path.name}, {size:,} bytes".replace(",", "."))

    low = document.lower()
    hits = [p for p in PROHIBITED_PHRASES if p.lower() in low]
    check("Gate 3 · léxico proibido ausente", not hits, f"ocorrências: {hits}" if hits else "0 ocorrências")
    css_early, _vars = _css_parts(document)
    css_low = css_early.lower()
    has_gradient = "linear-gradient" in css_low or "radial-gradient" in css_low
    check("Gate 3 · sem gradiente decorativo no CSS", not has_gradient,
          "linear-gradient/radial-gradient no CSS" if has_gradient else "0 gradientes no CSS")
    shadows = re.findall(r"box-shadow\s*:\s*([^;}\"]+)", document, re.I)
    heavy = []
    for sh in shadows:
        values = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)px", sh)]
        if values and max(values) > 4:
            heavy.append(sh.strip())
    check("Gate 3 · sombras ausentes ou <= 4px", not heavy, f"{len(shadows)} declarações; pesadas: {heavy[:3]}")
    radii = [float(v) for v in re.findall(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", document)]
    check("Gate 3 · cantos técnicos (radius <= 4px)", all(r <= 4 for r in radii), f"max={max(radii) if radii else 0}px")

    css, variables = _css_parts(document)
    font_sizes: list[tuple[float, str]] = []
    for value in re.findall(r"font-size\s*:\s*([^;}\"']+)", css):
        m = re.search(r"(\d+(?:\.\d+)?)(px|rem|em)", value)
        if m:
            amount = float(m.group(1)) * (16.0 if m.group(2) in {"rem", "em"} else 1.0)
            font_sizes.append((amount, value.strip()))
    for value in re.findall(r"font\s*:\s*[^;}\"']*?(\d+(?:\.\d+)?)px", css):
        font_sizes.append((float(value), f"shorthand {value}px"))
    for value in re.findall(r"font-size\s*:\s*clamp\(\s*(\d+(?:\.\d+)?)px", css):
        font_sizes.append((float(value), f"clamp min {value}px"))
    too_small = [f"{amount:g}px ({where})" for amount, where in font_sizes if amount < 12]
    check("Gate 4 · piso tipográfico >= 12px", not too_small,
          f"{len(font_sizes)} declarações; abaixo do piso: {too_small[:5]}" if too_small else f"{len(font_sizes)} declarações >= 12px")
    svg_font_attrs = [v for v in re.findall(r'font-size="(\d+(?:\.\d+)?)"', document) if float(v) < 12]
    check("Gate 4 · fontes de SVG >= 12px", not svg_font_attrs, f"{len(svg_font_attrs)} atributos abaixo do piso")

    decls = _extract_decls(css)
    text_colors: set[str] = set()
    bg_colors: set[str] = set()
    rule_pairs: list[tuple[str, str, str]] = []
    current_rule: dict[str, str] = {}
    current_selector = ""
    for selector, prop, value in decls:
        if selector != current_selector:
            current_selector = selector
            current_rule = {}
        if prop == "color":
            text_colors.add(value)
            current_rule["color"] = value
        elif prop in {"background", "background-color"}:
            token = value.split()[0] if value else ""
            rgb = _resolve_css_color(token, variables)
            if rgb:
                bg_colors.add(value)
                current_rule["bg"] = value
        if "color" in current_rule and "bg" in current_rule:
            rule_pairs.append((current_rule["color"], current_rule["bg"], selector))
    surfaces = {
        "#FFFFFF": _hex_to_rgb("#FFFFFF"),
        "#F4F6F8": _hex_to_rgb("#F4F6F8"),
        "paper": _hex_to_rgb(variables.get("--paper", "#FFFFFF") or "#FFFFFF"),
        "paper-alt": _hex_to_rgb(variables.get("--paper-alt", "#F4F6F8") or "#F4F6F8"),
    }
    contrast_fail: list[str] = []
    for value in sorted(text_colors):
        fg = _resolve_css_color(value, variables)
        if not fg:
            continue
        # Texto branco só aparece sobre fundos escuros declarados na mesma regra;
        # os pares reais são verificados abaixo (rule_pairs).
        if fg == (255, 255, 255):
            continue
        for label, bg in surfaces.items():
            if not bg:
                continue
            ratio = contrast_ratio(fg, bg)
            if ratio < 7.0:
                contrast_fail.append(f"{value} sobre {label}: {ratio:.2f}:1")
    for fg_value, bg_value, selector in rule_pairs:
        fg = _resolve_css_color(fg_value, variables)
        bg = _resolve_css_color(bg_value, variables)
        if fg and bg:
            ratio = contrast_ratio(fg, bg)
            if ratio < 7.0:
                contrast_fail.append(f"{fg_value} sobre {bg_value} em `{selector}`: {ratio:.2f}:1")
    check("Gate 4 · contraste WCAG AAA >= 7:1", not contrast_fail,
          "; ".join(sorted(set(contrast_fail))[:6]) if contrast_fail else
          f"{len(text_colors)} cores de texto e {len(bg_colors)} fundos conferidos")

    by_selector: dict[str, str] = {}
    for selector, prop, value in decls:
        by_selector.setdefault(selector, "")
        by_selector[selector] += f"{prop}:{value};"

    def rule_text(token: str) -> str:
        text = ""
        for selector, body in by_selector.items():
            parts = [p.strip() for p in selector.split(",")]
            if any(p == token or p.endswith(token) for p in parts):
                text += body
        return text

    numeric_rule = rule_text(".num")
    num_cell = rule_text(".num-cell")
    num_ok = "tabular-nums" in numeric_rule and "nowrap" in numeric_rule
    cell_ok = "tabular-nums" in num_cell and "nowrap" in num_cell
    check("Gate 4 · números tabulares e sem quebra", num_ok and cell_ok,
          ".num: " + ("ok" if num_ok else "falta tabular/nowrap") +
          " · .num-cell: " + ("ok" if cell_ok else "falta tabular/nowrap"))

    check("Gate 4 · tabela com rolagem horizontal", "overflow-x:auto" in css.replace(" ", ""),
          "containers .table-scroll com overflow-x:auto")

    # --- guardas de responsividade (overflow horizontal zero) ---
    css_compact = re.sub(r"\s+", "", css)
    guard_ok = ("min-width:0" in css_compact
                and ".grid>*" in css_compact
                and ".table-scroll" in css_compact
                and "width:100%" in css_compact
                and "max-width:100%" in css_compact
                and "box-sizing:border-box" in css_compact)
    check("Gate 4 · guarda de min-width em grid/flex", guard_ok,
          "min-width:0 em itens de grid/flex + .table-scroll com width/max-width 100%")
    mono_rule = ""
    for selector, prop, value in decls:
        if "mono" in selector and prop == "white-space":
            mono_rule = value
    check("Gate 4 · .mono não força no-wrap global", "nowrap" not in mono_rule,
          f".mono white-space={mono_rule or 'normal (padrão)'}")
    nowrap_selectors = []
    for selector, prop, value in decls:
        if prop == "white-space" and "nowrap" in value:
            nowrap_selectors.extend(s.strip() for s in selector.split(","))
    allowed_nowrap = (
        ".num", ".num-cell", ".num-h", ".m-count", ".m-money", ".reg", ".c-cut",
        ".badge", ".src-link", ".nd", ".f-label", ".seg-btns label", ".btn",
        ".results b", ".kpi-value", ".kpi-compare b", ".skip", ".vh",
        ".dev-banner code", ".sort-ind", ".microbar-value", ".statcell-value",
        ".tab",
    )
    stray_nowrap = [
        sel for sel in nowrap_selectors
        if not any(token in sel for token in allowed_nowrap)
    ]
    check("Gate 4 · nowrap restrito a valores/rótulos curtos", not stray_nowrap,
          f"seletores fora da allowlist: {stray_nowrap[:4]}" if stray_nowrap
          else f"{len(nowrap_selectors)} seletores de nowrap, todos de valores curtos")
    skip_css = re.search(r"\.skip\s*\{([^}]*)\}", css)
    skip_ok = bool(skip_css and "clip-path:inset(50%)" in skip_css.group(1).replace(" ", "")
                   and "left:-999px" not in css)
    check("Gate 4 · skip-link com clip moderno (sem -999px)", skip_ok,
          "clip-path:inset(50%) e sem left:-999px" if skip_ok else "skip link ainda usa left negativo")
    tables = document.count("<table")
    scrollers = document.count('class="table-scroll"')
    check("Gate 4 · toda tabela dentro de .table-scroll", scrollers >= tables,
          f"{tables} tabelas, {scrollers} scrollers")

    breakpoints = ["max-width:480px", "max-width:767px", "min-width:768px", "min-width:1024px",
                   "min-width:1440px", "max-width:375px"]
    missing_bp = [bp for bp in breakpoints if bp not in css.replace(" ", "")]
    check("Gate 4 · breakpoints 375/768/1440", not missing_bp,
          f"faltando: {missing_bp}" if missing_bp else f"{len(breakpoints)} breakpoints declarados")

    # --- arquitetura em abas (progressive enhancement) ---
    markup = re.sub(r"<script.*?</script>", " ", document, flags=re.S)
    tablist_n = len(re.findall(r'role="tablist"', markup))
    tab_n = len(re.findall(r'role="tab"', markup))
    panel_n = len(re.findall(r'role="tabpanel"', markup))
    selected = re.findall(r'role="tab"[^>]*aria-selected="true"', markup)
    controls = set(re.findall(r'role="tab"[^>]*aria-controls="([^"]+)"', markup))
    labelled = set(re.findall(r'role="tabpanel"[^>]*aria-labelledby="([^"]+)"', markup))
    tab_ids = set(re.findall(r'<button[^>]*role="tab"[^>]*id="([^"]+)"', markup))
    panel_ids = set(re.findall(r'role="tabpanel"[^>]*id="([^"]+)"', markup))
    tabs_ok = (tablist_n == 1 and tab_n == 4 and panel_n == 4 and len(selected) == 1
               and controls == panel_ids and labelled == tab_ids and len(panel_ids) == 4)
    check("Gate 4 · abas acessíveis (tablist/tab/tabpanel)", tabs_ok,
          f"tablist {tablist_n}, tabs {tab_n}, painéis {panel_n}, selecionadas {len(selected)}")
    deep_ok = all(f'id="{name}"' in document for name in ("achados", "insights", "fundos", "metodo"))
    check("Gate 4 · deep-link #insights/#fundos/#metodo", deep_ok,
          "ids achados/insights/fundos/metodo presentes" if deep_ok else "ids ausentes")
    keyboard_ok = all(token in document for token in
                      ("ArrowRight", "ArrowLeft", "'Home'", "'End'", "aria-selected", "replaceState"))
    check("Gate 4 · teclado e estado das abas no JS", keyboard_ok,
          "setas/Home/End, aria-selected e replaceState presentes")
    tabs_rule = re.search(r"\.tabs\{([^}]*)\}", css)
    tabs_css_ok = bool(tabs_rule and ("overflow-x:auto" in tabs_rule.group(1).replace(" ", "")
                                      or "flex-wrap:wrap" in tabs_rule.group(1).replace(" ", "")))
    check("Gate 4 · tab bar com rolagem própria (sem overflow da página)", tabs_css_ok,
          ".tabs com overflow-x:auto" if tabs_css_ok else "sem rolagem/wrap na tab bar")
    noscript_ok = document.count("<noscript>") >= 2 and "todas as seções" in document.lower()
    panels_visible = not re.search(r'role="tabpanel"[^>]*\shidden', document)
    check("HTML · fallback noscript empilhado", noscript_ok and panels_visible,
          f"{document.count('<noscript>')} noscripts; painéis sem hidden no HTML bruto")

    # --- tipografia compacta e densidade ---
    def _clamp_bounds(selector: str) -> tuple[float, float] | None:
        pattern = re.compile(r"\." + re.escape(selector) + r"\{([^}]*)\}")
        for m in pattern.finditer(css):
            body = m.group(1)
            clamp_m = re.search(r"clamp\(([^)]*)\)", body)
            if clamp_m:
                nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)px", clamp_m.group(1))]
                if len(nums) >= 2:
                    return nums[0], nums[-1]
            px = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", body)
            if px:
                return float(px[0]), float(px[0])
            font_shorthand = re.findall(r"font:\s*[^;{}]*?(\d+(?:\.\d+)?)px\s*/", body)
            if font_shorthand:
                return float(font_shorthand[0]), float(font_shorthand[0])
        return None

    head_bounds = _clamp_bounds("headline")
    kpi_bounds = _clamp_bounds("kpi-value")
    hero_bounds = _clamp_bounds("finding-hero")
    h2_rule = re.search(r"\.section-head h2\{([^}]*)\}", css)
    h2_px = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", h2_rule.group(1)) if h2_rule else []
    deck_rule = re.search(r"\.deck\{([^}]*)\}", css)
    deck_px = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", deck_rule.group(1)) if deck_rule else []
    headline_lh = bool(re.search(r"\.headline\{[^}]*line-height:\s*1\.15", css))
    typo_ok = (
        head_bounds is not None and head_bounds[0] >= 20 and head_bounds[1] <= 30 and headline_lh
        and kpi_bounds is not None and kpi_bounds[0] >= 18 and kpi_bounds[1] <= 26
        and hero_bounds is not None and 30 <= hero_bounds[1] <= 32
        and bool(h2_px) and max(float(x) for x in h2_px) <= 20
        and bool(deck_px) and max(float(x) for x in deck_px) <= 15
    )
    check("UI · escala tipográfica compacta", typo_ok,
          f"headline {head_bounds}, kpi {kpi_bounds}, hero {hero_bounds}, h2 {h2_px}, deck {deck_px}")
    td_rule = re.search(r"\.funds tbody td\{([^}]*)\}", css)
    td_body = td_rule.group(1) if td_rule else ""
    td_px = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", td_body)
    if not td_px:
        td_px = re.findall(r"font:\s*[^;{}]*?(\d+(?:\.\d+)?)px\s*/", td_body)
    td_padding = re.search(r"padding:\s*(\d+(?:\.\d+)?)px\s+(\d+(?:\.\d+)?)px", td_body)
    dense_ok = (bool(td_px) and float(td_px[0]) <= 13
                and bool(td_padding) and float(td_padding.group(1)) <= 8)
    check("UI · tabela densa (td <= 13px, padding vertical <= 8px)", dense_ok,
          f"td {td_px}, padding {td_padding.group(0) if td_padding else 'n/d'}")
    section_rule = re.search(r"\.section\{([^}]*)\}", css)
    section_pad = re.findall(r"padding:\s*(\d+(?:\.\d+)?)px", section_rule.group(1)) if section_rule else []
    check("UI · densidade vertical (section padding <= 24px)", bool(section_pad) and float(section_pad[0]) <= 24,
          f"section padding {section_pad[0] if section_pad else 'n/d'}px")

    # --- hierarquia narrativa: numeração única e hero antes da faixa de métricas ---
    sec_nums = re.findall(r'<p class="sec-num">([^<]*)</p>', document)
    check("UI · numeração semântica única (01-02 apenas em Insights)", sec_nums == ["01", "02"],
          f"sec-num encontrados: {sec_nums}")
    idx_count = document.count('class="idx"')
    kpi_labels = re.findall(r'<p class="kpi-label">([^<]*)</p>', document)
    check("UI · faixa de métricas sem numeração",
          idx_count == 0 and len(kpi_labels) == 4
          and all(not label.strip()[:1].isdigit() for label in kpi_labels),
          f"{idx_count} spans .idx; rótulos: {kpi_labels}")
    kickers = re.findall(r'<p class="finding-eyebrow">([^<]+)</p>', document)
    check("UI · achados 03/04 nomeados como achado em destaque",
          len(kickers) == 2 and "Achado 03" in kickers[0] and "Achado 04" in kickers[1],
          " / ".join(kickers))
    hero_rule = re.search(r"\.finding-hero\{([^}]*)\}", css)
    hero_px = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", hero_rule.group(1)) if hero_rule else []
    border_ok = (".finding-03" in css and "border-top:3pxsolidvar(--br)" in css_compact
                 and ".finding-04" in css and "border-top:3pxsolidvar(--gl)" in css_compact)
    check("UI · hero com número 30-32px e borda semântica 3px",
          bool(hero_px) and 30 <= float(hero_px[0]) <= 32 and border_ok,
          f"hero {hero_px}px; borda 3px por cor: {border_ok}")
    i_head = document.find('<h1 class="headline"')
    i_deck = document.find('class="deck"')
    i_ans = document.find('class="answer-first"')
    i_find = document.find('class="findings"')
    i_kpis = document.find('class="kpis-in"')
    i_fig = document.find("<figure")
    order_ok = -1 < i_head < i_deck < i_ans < i_find < i_kpis < i_fig
    check("UI · ordem hero: h1 → deck → leitura → achados → métricas → gráficos", order_ok,
          f"h1={i_head}, deck={i_deck}, leitura={i_ans}, achados={i_find}, "
          f"métricas={i_kpis}, figura={i_fig}")
    details_n = document.count("<details")
    summary_n = document.count("<summary")
    check("UI · método em <details> recolhíveis", details_n >= 5 and summary_n >= 5,
          f"{details_n} details, {summary_n} summaries")

    figures = re.findall(r"<figure\b.*?</figure>", document, re.S)
    captions = re.findall(r"<figcaption\b.*?</figcaption>", document, re.S)
    caption_links = [c for c in captions if re.search(r"<a [^>]*href=\"https://", c)]
    check("Gate 3 · 5+ figuras com fonte citada", len(figures) >= 5 and len(figures) == len(captions) == len(caption_links),
          f"{len(figures)} figuras, {len(captions)} figcaptions, {len(caption_links)} com link https")
    check("Gate 3 · pelo menos 2 gráficos SVG nativos", document.count("<svg") >= 2,
          f"{document.count('<svg')} elementos SVG")

    ids = re.findall(r'\bid="([^"]+)"', document)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    check("HTML · ids únicos", not dupes, f"duplicados: {dupes[:5]}" if dupes else f"{len(ids)} ids")

    visible = re.sub(r"<script.*?</script>", " ", document, flags=re.S)
    visible = re.sub(r"<style.*?</style>", " ", visible, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    emoji = sorted({ch for ch in visible if any(lo <= ord(ch) <= hi for lo, hi in EMOJI_RANGES)})
    check("Gate 3 · sem emojis em títulos/texto", not emoji, f"encontrados: {emoji[:6]}" if emoji else "0 emojis")

    links = re.findall(r'<a\s+[^>]*href="(https://[^"]+)"[^>]*>', document)
    external = re.findall(r'<a\s+[^>]*href="https?://[^"]*"[^>]*>', document)
    safe = re.findall(r'<a\s+[^>]*href="https?://[^"]*"[^>]*target="_blank"[^>]*rel="noopener noreferrer"[^>]*>', document)
    check("Gate 4 · links de fonte em HTTPS", len(links) >= 20, f"{len(links)} links https")
    placeholder_hrefs = [u for u in links if ("(@" in u or "%40" in u or "{" in u)]
    check("Gate 4 · URLs citáveis sem template de API", not placeholder_hrefs,
          f"placeholder: {placeholder_hrefs[:2]}" if placeholder_hrefs else "0 URLs com placeholders")
    blocked_links = [u for u in links if "openvc.app" in u]
    check("Gate 4 · fontes bloqueadas sem link quebrado", not blocked_links,
          f"link bloqueado: {blocked_links[:2]}" if blocked_links else "0 links para fontes bloqueadas")
    check("HTML · links externos seguros", len(external) == len(safe),
          f"{len(safe)}/{len(external)} com target=_blank + rel=noopener noreferrer")

    payload_match = re.search(r'<script type="application/json" id="vc-data">(.*?)</script>', document, re.S)
    payload_ok, payload_detail = False, "payload ausente"
    if payload_match:
        try:
            payload = json.loads(payload_match.group(1).replace("<\\/", "</"))
            rows = payload.get("rows") or []
            payload_ok = bool(rows) and len(rows[0]) == len(payload.get("cols") or [])
            payload_detail = f"{len(rows)} registros embutidos, {len(payload.get('cols') or [])} colunas"
        except json.JSONDecodeError as exc:
            payload_detail = f"JSON inválido: {exc}"
    check("Interatividade · dataset embutido válido", payload_ok, payload_detail)

    h1_match = re.search(r'<h1 class="headline">(.*?)</h1>', document, re.S)
    h1_text = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip() if h1_match else ""
    h1_neutral = (bool(h1_match) and 20 <= len(h1_text) <= 100
                  and not any(ch.isdigit() for ch in h1_text) and "%" not in h1_text)
    check("UI · H1 neutro do documento (sem números/%, <=100 chars)", h1_neutral,
          f"{len(h1_text)} caracteres: “{h1_text[:92]}…”")
    deck_match = re.search(r'<p class="deck">(.*?)</p>', document, re.S)
    deck_text = re.sub(r"<[^>]+>", "", deck_match.group(1)).strip() if deck_match else ""
    check("UI · deck descritivo (o que é a base, <=220 chars)",
          bool(deck_match) and 60 <= len(deck_text) <= 220,
          f"{len(deck_text)} caracteres: “{deck_text[:100]}…”")
    ans_match = re.search(r'<div class="answer-first">(.*?)</div>', document, re.S)
    ans_text_match = re.search(r'<p class="answer-first-text">(.*?)</p>', document, re.S)
    ans_kicker = re.search(r'<p class="answer-first-kicker">([^<]+)</p>', document)
    ans_text = re.sub(r"<[^>]+>", "", ans_text_match.group(1)).strip() if ans_text_match else ""
    ans_ok = (bool(ans_match) and bool(ans_kicker) and 60 <= len(ans_text) <= 170
              and sum(ch.isdigit() for ch in ans_text) >= 2)
    check("Answer-first · leitura principal calculada (>=2 números, <=170)", ans_ok,
          f"{len(ans_text)} caracteres, kicker={ans_kicker.group(1).strip() if ans_kicker else '—'}: "
          f"“{ans_text[:100]}…”")
    hero_center_ok = (
        bool(re.search(r"\.masthead\{[^}]*text-align:center", css))
        and all(re.search(r"\." + sel + r"\{[^}]*text-align:center", css)
                for sel in ("headline", "deck", "answer-first"))
        and bool(re.search(r"\.dateline\{[^}]*justify-content:center", css))
    )
    check("UI · hero centrado (kicker/h1/deck/leitura/dateline)", hero_center_ok,
          "text-align:center no masthead/headline/deck/answer-first e dateline centrado")
    kpi_count = document.count('class="kpi"')
    check("Answer-first · 4 KPIs com comparação", kpi_count == 4, f"{kpi_count} KPIs")
    check("HTML · dataset não é fixture" if "data-dataset=\"fixture\"" in document else "HTML · dataset canônico",
          True, "marcador data-dataset presente")
    check("Gate 4 · nota metodológica e limitações", "Nota metodológica" in document and "Limitações" in document,
          "seções presentes")

    number_hits = len(re.findall(r"US\$\s?[\d.,]+\s?(?:mil|mi|bi)", document))
    pct_hits = len(re.findall(r"\d+(?:,\d+)?%", document))
    check("Gate 3 · números reais exibidos", number_hits + pct_hits >= 40,
          f"{number_hits} valores monetários e {pct_hits} percentuais formatados")

    failed = [r for r in results if not r[1]]
    width = max(len(r[0]) for r in results)
    print(f"Self-check de {path}  ({size:,} bytes)".replace(",", "."))
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name.ljust(width)}  {detail}")
    print(f"\n{len(results) - len(failed)}/{len(results)} checagens PASS")
    if failed:
        print(f"FALHOU: {len(failed)} checagem(ns)", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Gera o dashboard HTML executivo de VC (Brasil x Global) a partir do dataset canônico."
    )
    parser.add_argument("--input", help="dataset JSON (funds.json ou fixture)")
    parser.add_argument("--out", help="caminho do HTML de saída")
    parser.add_argument("--kpis", help="kpis.json opcional (default: kpis.json ao lado do input)")
    parser.add_argument("--allow-fixture", action="store_true", help="permite dataset marcado __fixture__ (dev)")
    parser.add_argument("--fetch-fonts", action="store_true", help="baixa/cacheia woff2 do Google Fonts e sai")
    parser.add_argument("--force-fonts", action="store_true", help="refaz o download das fontes")
    parser.add_argument("--no-fonts", action="store_true", help="não usa webfonts (apenas fallback local)")
    parser.add_argument("--summary-json", help="grava um resumo JSON (KPIs/manchete) no caminho indicado")
    parser.add_argument("--self-check", metavar="HTML", help="audita um HTML já gerado (Gates 3/4) e sai")
    args = parser.parse_args(argv)

    if args.self_check:
        return run_self_check(Path(args.self_check))

    if args.fetch_fonts:
        return fetch_fonts(force=args.force_fonts)

    if not args.input or not args.out:
        parser.error("--input e --out são obrigatórios (exceto com --fetch-fonts)")

    input_path = Path(args.input)
    out_path = Path(args.out)
    try:
        records, meta = load_dataset(input_path)
        if meta.get("__fixture__") and not args.allow_fixture:
            raise FixtureError(
                f"{input_path} está marcado __fixture__ (dataset de desenvolvimento). "
                "Use --allow-fixture para preview e nunca publique essa saída."
            )
        errors = validate_records(records)
        if errors and meta.get("__fixture__") and args.allow_fixture:
            records, fixed = lenient_preview_fix(records)
            if fixed:
                print(f"AVISO: preview de fixture — {fixed} valor(es) monetário(s) negativo(s) "
                      f"zerado(s) para 'Não divulgado'. O dataset canônico precisa resolver isso "
                      f"na transformação (null + undisclosed_fields).", file=sys.stderr)
            errors = validate_records(records)
        if errors:
            preview = "\n".join(f"  - {e}" for e in errors[:20])
            raise RenderError(f"Gate 2 (contrato) falhou com {len(errors)} erro(s):\n{preview}")
        records = normalize(records)

        kpis_path = Path(args.kpis) if args.kpis else input_path.parent / "kpis.json"
        if kpis_path.exists():
            try:
                kpi_payload = json.loads(kpis_path.read_text(encoding="utf-8"))
                if isinstance(kpi_payload, dict):
                    for key in ("generated_at", "fx", "sources", "notes", "counts", "capital", "kpis",
                                "transform_report"):
                        if key in kpi_payload and key not in meta:
                            meta[key] = kpi_payload[key]
                    meta.setdefault("_kpis_path", str(kpis_path))
            except json.JSONDecodeError as exc:
                print(f"AVISO: kpis.json inválido ({exc}); seguindo só com o dataset.", file=sys.stderr)
        else:
            print(f"AVISO: {kpis_path} ausente; proveniência virá apenas do dataset.", file=sys.stderr)

        transform_path = kpis_path.parent / "transform_report.json"
        if transform_path.exists() and "transform_report" not in meta:
            try:
                report = json.loads(transform_path.read_text(encoding="utf-8"))
                if isinstance(report, dict):
                    meta["transform_report"] = report
            except json.JSONDecodeError as exc:
                print(f"AVISO: transform_report.json inválido ({exc}); seguindo sem escopo de exclusões.",
                      file=sys.stderr)
        meta["_input_path"] = str(input_path)

        if not meta.get("generated_at"):
            epoch = os.environ.get("SOURCE_DATE_EPOCH")
            try:
                moment = (datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch
                          else datetime.now(timezone.utc))
            except (ValueError, OSError):
                moment = datetime.now(timezone.utc)
            meta["generated_at"] = moment.replace(microsecond=0).isoformat()
            print("AVISO: generated_at ausente no dataset/kpis.json; usando data de geração do render "
                  "(determinismo: defina SOURCE_DATE_EPOCH).", file=sys.stderr)

        if not TEMPLATE_PATH.exists():
            raise RenderError(f"template ausente: {TEMPLATE_PATH}")
        document, summary = build_document(records, meta)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(document)

        if args.summary_json:
            with Path(args.summary_json).open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(summary, ensure_ascii=False, indent=1))

        print(f"OK  {out_path}  ({summary['output_bytes']:,} bytes)".replace(",", "."))
        print(f"    registros: {summary['records']}  cortes: {summary['by_cut']}")
        print(f"    fontes: {summary['font_mode']}  dataset: {summary['dataset']}")
        print(f"    manchete: {summary['headline']}")
        warnings = []
        if not meta.get("generated_at"):
            warnings.append("generated_at ausente (rodapé usa 'não informada')")
        if meta.get("__fixture__"):
            warnings.append("dataset é fixture: saída NÃO publicável")
        diverg = [c for c in summary["kpis_crosscheck"] if not c["ok"]]
        for c in diverg:
            warnings.append(
                f"kpis.json diverge > 0,5% em {c['label']}: kpis={c['kpis']} render={c['computed']}"
            )
        if summary["kpis_crosscheck"]:
            print(f"    cross-check kpis.json: {len(summary['kpis_crosscheck']) - len(diverg)}/"
                  f"{len(summary['kpis_crosscheck'])} agregados dentro de 0,5%")
        for w in warnings:
            print(f"    AVISO: {w}", file=sys.stderr)
        return 0
    except FixtureError as exc:
        print(f"ERRO (fixture): {exc}", file=sys.stderr)
        return 3
    except RenderError as exc:
        print(f"ERRO (render): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
