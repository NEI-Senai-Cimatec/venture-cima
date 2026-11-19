"""Transformacao canonica SDD: evidencias interim -> VentureCapitalFundRecord.

Aplica, de forma deterministica e auditavel:
  1. Entity resolution (veiculos -> gestoras) por CNPJ da gestora (CVM), CIK (SEC)
     e nome normalizado (Shizune / Wikidata).
  2. Stage heuristic engine com 3 prioridades da spec:
     DECLARED_THESIS > REGULATORY_CLASS_CVM_BNDES > IMPUTED_BY_TICKET_HEURISTIC.
  3. Cortes geograficos BRASIL_LOCAL / GLOBAL_ATIVO_BR / GLOBAL_EX_BR.
  4. Conversao BRL->USD pela PTAX de venda oficial do BCB (por competencia).
  5. KPIs derivados (dry powder, densidade por estagio, cross-border, concentracao).

Nenhum valor e inventado: quando a fonte nao divulga, o campo recebe null e o
nome entra em `undisclosed_fields`. Toda linha aponta para URL https de origem.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
INTERIM = ROOT / "data" / "interim"
CANON = ROOT / "data" / "canonical"

STAGES = ["PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "LATE_STAGE_GROWTH", "VENTURE_DEBT"]
STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}
CUT_BR = "BRASIL_LOCAL"
CUT_ACTIVE = "GLOBAL_ATIVO_BR"
CUT_EX = "GLOBAL_EX_BR"

CVM_REGISTRY_URL = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
CVM_INF_URL = "https://dados.cvm.gov.br/dados/FIP/DOC/INF_QUADRIMESTRAL/DADOS/inf_quadrimestral_fip_2026.csv"
SEC_FORMD_DATASETS_URL = "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets"

# Faixas empiricas Growth List/Digits (spec 1.3, prioridade 3).
GROWTH_BANDS: list[tuple[float | None, str]] = [
    (500_000.0, "PRE_SEED"),
    (3_000_000.0, "SEED"),
    (15_000_000.0, "SERIES_A"),
    (50_000_000.0, "SERIES_B"),
    (None, "LATE_STAGE_GROWTH"),
]

# Classificacao regulatoria CVM (spec 1.3, prioridade 2).
CVM_CLASS_MAP = {
    "SEMENTE": ["PRE_SEED", "SEED"],
    "EMERGENTE": ["SERIES_A", "SERIES_B"],
}
MULTI_PL_THRESHOLD_BRL = 500_000_000.0

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR", "VI", "GU",
}

JURISDICTION_ISO = {
    "DELAWARE": "US", "CAYMAN ISLANDS": "KY", "CALIFORNIA": "US", "NEW YORK": "US",
    "TEXAS": "US", "MASSACHUSETTS": "US", "FLORIDA": "US", "ILLINOIS": "US", "WASHINGTON": "US",
    "COLORADO": "US", "GEORGIA": "US", "MARYLAND": "US", "VIRGINIA": "US", "NEVADA": "US",
    "OREGON": "US", "PENNSYLVANIA": "US", "NEW JERSEY": "US", "CONNECTICUT": "US",
    "NORTH CAROLINA": "US", "UTAH": "US", "ARIZONA": "US", "MINNESOTA": "US", "OHIO": "US",
    "ONTARIO": "CA", "BRITISH COLUMBIA": "CA", "QUEBEC": "CA", "ALBERTA": "CA",
    "ENGLAND AND WALES": "GB", "UNITED KINGDOM": "GB", "SCOTLAND": "GB", "LUXEMBOURG": "LU",
    "IRELAND": "IE", "SINGAPORE": "SG", "ISRAEL": "IL", "NETHERLANDS": "NL", "FRANCE": "FR",
    "GERMANY": "DE", "BERMUDA": "BM", "BRITISH VIRGIN ISLANDS": "VG", "JERSEY": "JE",
    "GUERNSEY": "GG", "HONG KONG": "HK", "AUSTRALIA": "AU", "SWEDEN": "SE",
    "SWITZERLAND": "CH", "SPAIN": "ES", "PORTUGAL": "PT", "ESTONIA": "EE", "FINLAND": "FI",
    "DENMARK": "DK", "NORWAY": "NO", "ITALY": "IT", "JAPAN": "JP", "SOUTH KOREA": "KR",
    "INDIA": "IN", "CANADA": "CA", "BRAZIL": "BR", "MALTA": "MT", "CYPRUS": "CY",
    "UNITED ARAB EMIRATES": "AE", "MEXICO": "MX", "ARGENTINA": "AR", "CHILE": "CL",
    "COLOMBIA": "CO", "URUGUAY": "UY", "PANAMA": "PA", "NIGERIA": "NG", "KENYA": "KE",
    "SOUTH AFRICA": "ZA", "NEW ZEALAND": "NZ", "ICELAND": "IS", "AUSTRIA": "AT",
    "BELGIUM": "BE", "POLAND": "PL", "CZECH REPUBLIC": "CZ", "HUNGARY": "HU",
    "ROMANIA": "RO", "GREECE": "GR", "TURKEY": "TR", "INDONESIA": "ID", "MALAYSIA": "MY",
    "THAILAND": "TH", "VIETNAM": "VN", "PHILIPPINES": "PH", "TAIWAN": "TW",
    "PHILIPPINES ": "PH", "LIECHTENSTEIN": "LI", "MONACO": "MC", "SAUDI ARABIA": "SA",
}

LEGAL_TOKENS = {
    "LTDA", "LTD", "LIMITED", "SA", "S", "A", "S/A", "CIA", "COMPANHIA", "DTVM",
    "DISTRIBUIDORA", "CORRETORA", "CORRETAGEM", "GESTORA", "GESTAO", "GESTOR",
    "ADMINISTRADORA", "ADMINISTRACAO", "SERVICOS", "RECURSOS", "PARTICIPACOES",
    "PARTICIPACAO", "INVESTIMENTOS", "INVESTIMENTO", "INVESTMENT", "INVESTMENTS",
    "CAPITAL", "ASSET", "MANAGEMENT", "ADVISORS", "ADVISERS", "GROUP", "HOLDINGS",
    "HOLDING", "FINANCEIRA", "FUNDO", "FUNDOS", "FUND", "FUNDS", "FIP", "FIC", "FII",
    "VEICULO", "THE", "SOCIEDADE", "EMPRESA", "DE", "DA", "DO", "DAS", "DOS", "E",
    "LLC", "L", "P", "LP", "LLP", "INC", "CORP", "CO", "PARTNERS", "PARTNERSHIP",
    "VENTURE", "VENTURES", "VC", "TECH", "TECHNOLOGY", "BRASIL", "BRAZIL",
}

ROMAN = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XVI|XVII|XVIII|XIX|XX)$")


# --------------------------------------------------------------------------- io
def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def fmt_cnpj(value: Any) -> str | None:
    d = digits(value)
    if len(d) != 14:
        return d or None
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def num(value: Any) -> float | None:
    """Converte valores monetarios/percentuais preservando ausencia (None)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("R$", "").replace("US$", "").replace("$", "").strip()
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(",") == 1 and text.count(".") >= 1:
        text = text.replace(".", "").replace(",", ".")
    text = text.replace(" ", "")
    try:
        out = float(text)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def pick(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


# ------------------------------------------------------------------ normalizacao
def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_name(text: Any, drop_legal: bool = True) -> str:
    raw = strip_accents(str(text or "")).upper().replace("&", " E ")
    raw = re.sub(r"[^A-Z0-9 ]+", " ", raw)
    tokens = [t for t in raw.split() if t]
    if drop_legal:
        tokens = [t for t in tokens if t not in LEGAL_TOKENS and not ROMAN.match(t) and not t.isdigit()]
    return " ".join(tokens)


GENERIC_FIRST_TOKENS = {
    "GLOBAL", "NEW", "THE", "FIRST", "NEXT", "EARLY", "START", "SMALL", "OPEN",
    "BLUE", "GREEN", "NORTH", "SOUTH", "EAST", "WEST", "ALPHA", "BETA", "OMEGA",
    "PRIME", "PRINCIPAL", "DIRECT", "UNITED", "AMERICAN", "EUROPEAN", "ASIAN",
}

STAGE_LIKE_NAMES = {
    "SEED", "PRE SEED", "PRESSEED", "SERIES A", "SERIES B", "SERIES C", "ANGEL",
    "VENTURE CAPITAL", "CONVERTIBLE NOTE", "GRANT", "PRIVATE EQUITY",
    "DEBT FINANCING", "EQUITY CROWDFUNDING", "VENTURE SERIES UNKNOWN", "IPO",
    "VENTURE", "GROWTH", "LATE STAGE", "FUND", "VC",
}


def is_stage_like_name(name: str) -> bool:
    """Nomes que nao identificam gestora (linhas de rotulo/estagio em diretorios)."""
    norm = normalize_name(name, drop_legal=False)
    return (not norm or len(norm) <= 2 or norm in STAGE_LIKE_NAMES
            or re.fullmatch(r"[0-9 ]+", norm) is not None)


SPV_MARKERS = ("SERIES OF", "SPV", "ROLL UP VEHICLE", "ROLL-UP", "SERIES OF ROLL",
               "ACCESS FUND", "CO-INVEST", "COINVEST")


def firm_key_from_issuer(entity_name: str) -> str:
    """Extrai o radical da gestora a partir do nome do veiculo na SEC."""
    raw = strip_accents(str(entity_name or "")).upper()
    raw = re.sub(r"[^A-Z0-9 ]+", " ", raw)
    tokens = raw.split()
    cut = len(tokens)
    for i, tok in enumerate(tokens):
        if tok in {"FUND", "FUNDS", "VEHICLE", "PARTNERSHIP"}:
            cut = i
            break
    head = tokens[:cut] if cut > 0 else tokens
    return normalize_name(" ".join(head))


def firm_display_from_issuer(entity_name: str) -> str:
    """Nome de exibicao da gestora preservando o texto original da SEC."""
    name = str(entity_name or "").strip()
    match = re.search(r"\b(fund|funds)\b", name, flags=re.I)
    if match and match.start() > 2:
        name = name[: match.start()]
    name = re.sub(r"[,\s]+(L\.?P\.?|LLC|Ltd\.?|Inc\.?|Corp\.?|S\.A\.?|plc)\s*$", "", name,
                  flags=re.I)
    name = re.sub(r"[,\s]+\b(I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV)\b\s*$", "",
                  name, flags=re.I)
    name = re.sub(r"\s{2,}", " ", name).strip(" ,;.-")
    return name


def is_spv_shell(entity_name: str) -> bool:
    upper = strip_accents(str(entity_name or "")).upper()
    return any(marker in upper for marker in SPV_MARKERS)


def token_set(text: Any) -> set[str]:
    return set(normalize_name(text).split())


def name_match(a: str, b: str) -> tuple[bool, str, str]:
    """Compara nomes normalizados de gestoras.

    Regras (conservadoras, para nao aproximar empresas diferentes):
      exact      -> igualdade apos normalizacao
      jaccard    -> interseccao/ uniao >= 0.75
      subset     -> um lado e 1 token distintivo (>=4 chars) contido no outro
      containment-> um nome contem a sequencia completa do outro com >=2 tokens
    """
    if not a or not b:
        return False, "", ""
    if a == b:
        return True, "exact", "high"
    ta, tb = a.split(), b.split()
    sa, sb = set(ta), set(tb)
    if not sa or not sb:
        return False, "", ""
    inter = sa & sb
    if not inter:
        return False, "", ""
    jaccard = len(inter) / len(sa | sb)
    if jaccard >= 0.75:
        return True, "token_jaccard", "medium"
    # um dos lados com um unico token distintivo contido no outro
    if len(sa) == 1:
        token = next(iter(sa))
        if token in sb and len(token) >= 4 and token not in GENERIC_FIRST_TOKENS:
            return True, "single_token_subset", "medium"
    if len(sb) == 1:
        token = next(iter(sb))
        if token in sa and len(token) >= 4 and token not in GENERIC_FIRST_TOKENS:
            return True, "single_token_subset", "medium"
    # sequencia completa de um nome ocorre no outro (>= 2 tokens)
    if len(ta) >= 2 and len(tb) >= 2:
        if " ".join(ta) in " ".join(tb) or " ".join(tb) in " ".join(ta):
            return True, "containment", "medium"
    return False, "", ""


# --------------------------------------------------------------------------- FX
class FX:
    """Converte BRL->USD pela PTAX de venda oficial do BCB."""

    def __init__(self, path: Path):
        data = load_json(path, {}) or {}
        series = data.get("series") or []
        parsed: list[tuple[date, float]] = []
        for row in series:
            d = parse_date(row.get("date") or row.get("data"))
            rate = num(row.get("cotacao_venda") or row.get("rate") or row.get("ask"))
            if d and rate and rate > 0:
                parsed.append((d, rate))
        parsed.sort()
        self.points = parsed
        self.raw = data
        last = data.get("last") or {}
        self.last_date = parse_date(last.get("date"))
        self.last_rate = num(last.get("cotacao_venda"))
        if not parsed and self.last_rate:
            self.points = [(self.last_date or date.today(), self.last_rate)]
        if not self.points:
            raise RuntimeError("PTAX indisponivel: data/interim/fx_brl_usd.json vazio ou invalido.")

    def rate_for(self, when: date | None) -> tuple[float, date]:
        if when is None:
            return self.points[-1][1], self.points[-1][0]
        best = None
        for d, rate in self.points:
            if d <= when:
                best = (d, rate)
            else:
                break
        if best is None:
            return self.points[0][1], self.points[0][0]
        return best[1], best[0]

    @property
    def source(self) -> str:
        return str(self.raw.get("source") or "BCB PTAX - cotacao de venda")

    @property
    def source_url(self) -> str:
        return str(self.raw.get("source_url") or "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/")


def brl_to_usd(amount_brl: float | None, when: date | None, fx: FX) -> tuple[float | None, date | None, str]:
    if amount_brl is None:
        return None, None, ""
    rate, used = fx.rate_for(when)
    return round(amount_brl / rate, 2), used, f"PTAX venda {used.isoformat()} = {rate:.4f}"


# ------------------------------------------------------------- stage heuristic
def heuristic_stage(amount_usd: float | None) -> tuple[list[str], str]:
    """Prioridade 3: faixas empiricas aplicadas ao tamanho disponivel (rodada ou fundo)."""
    if amount_usd is None:
        return ["SEED", "SERIES_A"], "IMPUTED_BY_TICKET_HEURISTIC"
    for ceiling, stage in GROWTH_BANDS:
        if ceiling is None or amount_usd <= ceiling:
            return [stage], "IMPUTED_BY_TICKET_HEURISTIC"
    return ["LATE_STAGE_GROWTH"], "IMPUTED_BY_TICKET_HEURISTIC"


def order_stages(stages: Iterable[str]) -> list[str]:
    valid = {s for s in stages if s in STAGE_ORDER}
    return sorted(valid, key=lambda s: STAGE_ORDER[s])


# ------------------------------------------------------------------- shizune
class DirectoryIndex:
    """Indice de teses declaradas (Shizune) com precedencia explicita entre charts.

    Uma mesma gestora aparece em varios recortes com `Stage` e `Relevant Deals`
    divergentes (escopo do chart). Precedencia documentada em CHART_PRIORITY:
    o chart canonico do Brasil vence, depois o Brasil amplo, LATAM, global, EUA e
    Europa. O numero de deals externado no registro final usa SEMPRE um chart do
    Brasil (unico escopo em que a contagem e de deals brasileiros).
    """

    CHART_PRIORITY = [
        "venture-capital-vc-funds-brazil",
        "vc-funds-brazil",
        "venture-capital-vc-funds-latam",
        "vc-funds-latam",
        "venture-capital-vc-funds",
        "venture-capital-vc-funds-united-states",
        "vc-funds-united-states",
        "venture-capital-vc-funds-europe",
        "vc-funds-europe",
    ]

    def __init__(self, records: list[dict[str, Any]]):
        self.by_key: dict[str, list[dict[str, Any]]] = {}
        for rec in records:
            p = rec.get("payload") or {}
            name = p.get("investor_name") or rec.get("record_id")
            if not name:
                continue
            key = normalize_name(name)
            if not key or is_stage_like_name(name):
                continue
            url = str(p.get("table_url") or rec.get("source_url") or "")
            priority = len(self.CHART_PRIORITY)
            for i, slug in enumerate(self.CHART_PRIORITY):
                if slug in url:
                    priority = i
                    break
            self.by_key.setdefault(key, []).append({
                "name": name,
                "stages": order_stages(p.get("declared_stages") or []),
                "stage_raw": p.get("stage_raw") or [],
                "deals": int(num(p.get("relevant_deals")) or 0),
                "listed_in_brazil": bool(p.get("listed_in_brazil_dataset")),
                "geography_includes_brazil": bool(p.get("geography_includes_brazil")),
                "url": rec.get("source_url") or p.get("table_url"),
                "source_id": rec.get("source_id"),
                "priority": priority,
                "is_brazil_chart": "brazil" in url,
            })
        for key, entries in self.by_key.items():
            entries.sort(key=lambda e: (e["priority"], -e["deals"]))
        self._keys = list(self.by_key)

    def _entry(self, key: str) -> dict[str, Any] | None:
        entries = self.by_key.get(key)
        if not entries:
            return None
        chosen = dict(entries[0])
        # consolida sinais de Brasil em TODOS os charts (uniao de evidencias)
        chosen["has_brazil_activity"] = any(
            e["listed_in_brazil"] or e["geography_includes_brazil"] for e in entries)
        br_charts = [e for e in entries if e["is_brazil_chart"]]
        chosen["brazil_deals"] = br_charts[0]["deals"] if br_charts else None
        chosen["brazil_deals_basis"] = br_charts[0]["url"] if br_charts else None
        chosen["charts"] = [e["url"] for e in entries]
        # estagio: primeiro chart (por precedencia) que declare estagios
        for e in entries:
            if e["stages"]:
                chosen["stages"] = e["stages"]
                chosen["stage_raw"] = e["stage_raw"]
                chosen["url"] = e["url"]
                break
        return chosen

    def match(self, firm_name: str) -> tuple[dict[str, Any] | None, str, str]:
        key = normalize_name(firm_name)
        if not key:
            return None, "", ""
        if key in self.by_key:
            return self._entry(key), "exact", "high"
        best = None
        for cand in self._keys:
            if is_stage_like_name(cand):
                continue
            ok, method, conf = name_match(key, cand)
            if ok:
                entry = self._entry(cand)
                if best is None or (entry and entry["deals"] > best[0]["deals"]):
                    best = (entry, method, conf)
        if best:
            return best
        return None, "", ""

    def __len__(self) -> int:
        return len(self.by_key)


# ------------------------------------------------------------------- wikidata
class WikidataIndex:
    def __init__(self, records: list[dict[str, Any]]):
        self.by_key: dict[str, dict[str, Any]] = {}
        for rec in records:
            p = rec.get("payload") or {}
            label = p.get("label") or rec.get("record_id")
            key = normalize_name(label)
            country = str(p.get("country_iso2") or rec.get("country_hint") or "").upper()
            if not key or len(country) != 2:
                continue
            self.by_key.setdefault(key, {
                "qid": p.get("qid") or rec.get("record_id"),
                "label": label,
                "country_iso2": country,
                "website": p.get("website"),
                "founded": p.get("founded"),
                "source_url": rec.get("source_url"),
            })
        self._keys = list(self.by_key)

    def match(self, firm_name: str) -> tuple[dict[str, Any] | None, str, str]:
        key = normalize_name(firm_name)
        if not key:
            return None, "", ""
        direct = self.by_key.get(key)
        if direct:
            return direct, "exact", "high"
        for cand in self._keys:
            ok, method, conf = name_match(key, cand)
            if ok:
                return self.by_key[cand], method, conf
        return None, "", ""


# --------------------------------------------------------------------- builder
class Transform:
    def __init__(self, interim: Path = INTERIM, canon: Path = CANON):
        self.interim = interim
        self.canon = canon
        self.fx = FX(interim / "fx_brl_usd.json")
        self.shizune = DirectoryIndex(load_jsonl(interim / "shizune_firms.jsonl"))
        self.wikidata = WikidataIndex(load_jsonl(interim / "wikidata_firms.jsonl"))
        self.br_funds = load_jsonl(interim / "br_funds.jsonl")
        self.br_firms = load_jsonl(interim / "br_firms.jsonl")
        self.bndes = load_jsonl(interim / "br_bndes_portfolio.jsonl")
        self.sec = load_jsonl(interim / "sec_formd_vehicles.jsonl")
        self.report: dict[str, Any] = {
            "inputs": {
                "br_funds": len(self.br_funds),
                "br_firms": len(self.br_firms),
                "br_bndes": len(self.bndes),
                "sec_formd": len(self.sec),
                "wikidata_firms": len(self.wikidata.by_key),
                "shizune_firms": len(self.shizune.by_key),
                "fx_points": len(self.fx.points),
            },
            "dropped": [],
            "warnings": [],
        }
        self.records: list[dict[str, Any]] = []

    # -- BR ---------------------------------------------------------------
    def build_br(self) -> None:
        bndes_by_cnpj: dict[str, dict[str, Any]] = {}
        for rec in self.bndes:
            p = rec.get("payload") or {}
            cnpj = digits(p.get("cnpj"))
            if cnpj:
                bndes_by_cnpj.setdefault(cnpj, p)

        for rec in self.br_funds:
            p = rec.get("payload") or {}
            cnpj = digits(p.get("cnpj_fundo"))
            if len(cnpj) != 14:
                self.report["dropped"].append({"source": "cvm", "reason": "cnpj_invalido",
                                               "record": rec.get("record_id")})
                continue
            name = p.get("denominacao_social") or rec.get("record_id")
            gestor = p.get("gestor") or p.get("firm_name") or ""
            gestor_cnpj = digits(p.get("cnpj_gestor"))
            pl_brl = num(p.get("pl_brl"))
            pl_date = parse_date(p.get("data_patrimonio_liquido") or p.get("pl_data"))
            # PL <= 0 nao e um "valor de patrimonio" utilizavel (fundos em liquidacao
            # podem reportar PL negativo): o campo vira null e o valor bruto fica
            # preservado em aum_raw_brl para auditoria, conforme a regra de zero alucinacao.
            pl_usable = pl_brl if (pl_brl is not None and pl_brl > 0) else None
            aum_usd, fx_date, fx_note = brl_to_usd(pl_usable, pl_date, self.fx)

            # Prioridade 1: tese declarada (Shizune) por nome da gestora
            match, match_method, match_conf = self.shizune.match(gestor)
            if match and match["stages"]:
                stages, method = match["stages"], "DECLARED_THESIS"
                basis = f"Estagios declarados pela gestora no Shizune ({match['name']})"
                declared = True
            else:
                declared = False
                signal = str(p.get("class_signal") or "").upper()
                legacy = str(p.get("legacy_cvm_class") or "").upper()
                if not signal and legacy:
                    signal = {"FIP CS": "SEMENTE", "FIP EE": "EMERGENTE",
                              "FIP MULTI": "MULTIESTRAT"}.get(legacy, "")
                if signal in CVM_CLASS_MAP:
                    stages, method = CVM_CLASS_MAP[signal], "REGULATORY_CLASS_CVM_BNDES"
                    basis = (f"Classe FIP '{signal}' registrada na CVM "
                             f"(denominacao social / campo legado CLASSE)")
                elif signal == "MULTIESTRAT" and pl_usable and pl_usable > MULTI_PL_THRESHOLD_BRL:
                    stages, method = ["SERIES_B", "LATE_STAGE_GROWTH"], "REGULATORY_CLASS_CVM_BNDES"
                    basis = (f"FIP Multiestrategia com PL R$ {pl_usable:,.0f} acima de R$ 500 mi "
                             f"(regra 1.3 da spec)")
                else:
                    stages, method = heuristic_stage(aum_usd)
                    basis = (f"Sem classe declarada: porte do fundo US$ {aum_usd:,.0f} "
                             f"mapeado nas faixas empiricas Growth List"
                             if aum_usd else "Sem classe declarada e sem PL: faixa padrao Seed/Serie A")

            bndes_hit = bndes_by_cnpj.get(cnpj)
            evidence = [CVM_REGISTRY_URL]
            if p.get("pl_history"):
                evidence.append(CVM_INF_URL)

            history = []
            for h in p.get("pl_history") or []:
                d = parse_date(h.get("data_competencia"))
                v = num(h.get("pl_brl"))
                if d and v is not None:
                    history.append({"date": d.isoformat(), "pl_brl": v})
            history.sort(key=lambda x: x["date"])
            pl_trend = None
            if len(history) >= 2:
                pl_trend = "up" if history[-1]["pl_brl"] > history[0]["pl_brl"] else (
                    "down" if history[-1]["pl_brl"] < history[0]["pl_brl"] else "flat")

            committed_brl = num(p.get("capital_comprometido_brl"))
            invested_brl = num(p.get("capital_integralizado_brl"))
            committed_usd, _, _ = brl_to_usd(committed_brl, pl_date, self.fx)
            invested_usd, _, _ = brl_to_usd(invested_brl, pl_date, self.fx)
            # Auditoria de consistencia do Informe Quadrimestral: 19,6% das linhas
            # declaram capital integralizado > comprometido, 658 linhas vem com
            # VL_CAP_COMPROM=0 e ha erro de escala conhecido (ex.: HMV reportou
            # R$ 305,9 bi com PL de R$ 0,4 bi). Por isso o capital declarado NAO e
            # usado no KPI de dry powder: fica preservado com flag de qualidade e o
            # indicador de liquidez do Brasil usa PL crescente (regra 1.3 da spec).
            data_quality_flags: list[str] = []
            capital_consistente = True
            if committed_brl is None or invested_brl is None:
                capital_consistente = False
            elif invested_brl > committed_brl + 0.01:
                capital_consistente = False
                data_quality_flags.append("cvm_capital_integralizado_maior_que_comprometido")
            elif committed_brl > 1000 * max(invested_brl, 1.0):
                capital_consistente = False
                data_quality_flags.append("cvm_capital_comprometido_escala_suspeita")
            if pl_brl is not None and pl_brl <= 0:
                data_quality_flags.append("cvm_pl_nao_positivo")
            dry_powder = None  # indisponivel de forma confiavel na fonte (ver metodologia)

            undisclosed = []
            if aum_usd is None:
                undisclosed.append("aum_or_offering_usd")
            undisclosed.extend(["estimated_ticket_min_usd", "estimated_ticket_max_usd",
                                "lead_investor_rate"])
            if dry_powder is None:
                undisclosed.append("dry_powder_usd")
            brazil_deals = (int(match["brazil_deals"])
                            if match and match.get("brazil_deals") is not None else None)
            if brazil_deals is None:
                undisclosed.append("brazil_deals_count")
            estimate_basis = (
                f"PL R$ {pl_brl:,.2f} ({iso(pl_date) or 'data nao divulgada'}) / {fx_note}"
                if pl_usable else
                (f"PL R$ {pl_brl:,.2f} nao positivo (fundo em liquidacao/zerado): campo "
                 f"aum_or_offering_usd recebe null; valor bruto preservado em aum_raw_brl"
                 if pl_brl is not None else basis)
            )

            record = {
                "fund_id": f"cvm:{cnpj}",
                "firm_name": gestor or "Nao divulgado na fonte oficial",
                "vehicle_name": name,
                "regulatory_id": fmt_cnpj(cnpj),
                "geographic_cut": CUT_BR,
                "hq_country": "BR",
                "target_stages": order_stages(stages),
                "stage_classification_method": method,
                "estimated_ticket_min_usd": None,
                "estimated_ticket_max_usd": None,
                "aum_or_offering_usd": aum_usd,
                "lead_investor_rate": None,
                "active_status": "funcionamento" in str(p.get("situacao") or "").lower(),
                "primary_source_name": "CVM - Registro de Fundos e Classes (RCVM 175)",
                "primary_source_url": rec.get("source_url") or CVM_REGISTRY_URL,
                "estimate_basis": estimate_basis,
                "undisclosed_fields": sorted(set(undisclosed)),
                "evidence_urls": sorted(set(evidence)),
                "resolution": {
                    "firm_key": f"cvm_gestor:{gestor_cnpj}" if gestor_cnpj else f"name:{normalize_name(gestor)}",
                    "vehicle_key": f"cnpj:{cnpj}",
                    "method": "CNPJ_GESTOR" if gestor_cnpj else "NOME_NORMALIZADO",
                    "confidence": "high" if gestor_cnpj else "medium",
                },
                "stage_basis": basis,
                "class_signal": p.get("class_signal"),
                "legacy_cvm_class": p.get("legacy_cvm_class"),
                "pl_trend": pl_trend,
                "pl_history": history[-4:],
                "aum_raw_brl": pl_brl,
                "capital_committed_usd": committed_usd,
                "capital_invested_usd": invested_usd,
                "capital_consistente": capital_consistente,
                "data_quality_flags": data_quality_flags,
                "dry_powder_usd": dry_powder,
                "bndes_backed": bool(bndes_hit),
                "brazil_deals_basis": match.get("brazil_deals_basis") if match else None,
                "declared_thesis_charts": match.get("charts") if match else None,
                "reporting_date": iso(pl_date),
                "country_source": "CVM: gestora registrada no Brasil",
                "declared_thesis_source": match["url"] if declared else None,
            }
            if brazil_deals is not None:
                record["brazil_deals_count"] = brazil_deals
            self.records.append(record)

    # -- Global -----------------------------------------------------------
    def _dedupe_sec(self) -> list[dict[str, Any]]:
        """Entity resolution: 1 registro por CIK (veiculo legal), melhor filing."""
        best: dict[str, dict[str, Any]] = {}
        for rec in self.sec:
            p = rec.get("payload") or {}
            cik = digits(p.get("cik")) or normalize_name(p.get("entity_name"))
            if not cik:
                continue
            offering = num(pick(p, "total_offering_amount_usd", "offering_usd", "total_offering_amount"))
            filed = parse_date(p.get("filing_date"))
            score = (offering or -1.0, filed or date.min)
            prev = best.get(cik)
            if prev is None:
                best[cik] = rec
                continue
            prev_p = prev.get("payload") or {}
            prev_offering = num(pick(prev_p, "total_offering_amount_usd", "offering_usd", "total_offering_amount"))
            prev_filed = parse_date(prev_p.get("filing_date"))
            if score >= (prev_offering or -1.0, prev_filed or date.min):
                best[cik] = rec
        deduped = list(best.values())
        self.report["dedupe_sec"] = {"raw": len(self.sec), "unique_vehicles": len(deduped)}
        return deduped

    def build_global(self) -> None:
        today = date.today()
        excluded_micro: list[dict[str, Any]] = []
        excluded_spv: list[dict[str, Any]] = []
        for rec in self._dedupe_sec():
            p = rec.get("payload") or {}
            entity = p.get("entity_name") or rec.get("record_id")
            cik = digits(p.get("cik"))

            # Escopo analitico: filings com oferta declarada < US$ 1 mi sao veiculos
            # nominais/micro-sindicatos e nao entram no universo canonico (contados no
            # relatorio). Veiculos com oferta 'Indefinite' (sem valor) PERMANECEM,
            # pois podem ser fundos materiais em captacao.
            offering_raw = num(pick(p, "total_offering_amount_usd", "offering_usd",
                                    "total_offering_amount"))
            if offering_raw is not None and 0 < offering_raw < 1_000_000:
                excluded_micro.append({"record": entity, "offering_usd": offering_raw})
                continue
            # SPVs de deal unico nao sao "fundos" para fins de mapeamento por estagio:
            # ficam fora do universo canonico e sao contabilizados no relatorio.
            if is_spv_shell(entity):
                excluded_spv.append({"record": entity,
                                     "offering_usd": offering_raw})
                continue

            firm_key = firm_key_from_issuer(entity) if entity else ""
            firm_name = firm_display_from_issuer(entity) if entity else ""
            if len((firm_name or "").strip()) < 2:
                firm_name = str(entity or "").split(",")[0].strip()
            if len((firm_name or "").strip()) < 2:
                firm_name = str(entity or "Nao divulgado na fonte oficial").strip()
            jurisdiction = str(p.get("jurisdiction_of_inc") or "").strip().upper()
            state_country = str(p.get("state_or_country") or "").strip().upper()

            # hq_country: Wikidata (mais preciso) > jurisdicao do emissor
            country, country_source, confidence = "", "", "medium"
            wd, wd_method, wd_conf = self.wikidata.match(firm_name)
            if wd and wd.get("country_iso2"):
                country = wd["country_iso2"]
                country_source = f"Wikidata {wd['qid']}:P17 ({wd_method})"
                confidence = wd_conf
            if not country:
                iso_code = ""
                # (a) codigo ISO2 de endereco do filing (EDGAR), ja normalizado pelo coletor
                hint = str(rec.get("country_hint") or "").strip().upper()
                if len(hint) == 2 and hint.isalpha() and hint not in US_STATES:
                    iso_code = hint
                    country_source = f"SEC/EDGAR address country '{hint}'"
                # (b) jurisdicao de constituicao do veiculo
                if not iso_code:
                    iso_code = JURISDICTION_ISO.get(jurisdiction, "")
                    if iso_code:
                        country_source = f"SEC jurisdiction of incorporation '{jurisdiction}'"
                # (c) UF dos EUA / codigo composto -> US (nao confundir UF com pais)
                if not iso_code and state_country:
                    if state_country.startswith("US-") or state_country in US_STATES:
                        iso_code = "US"
                        country_source = f"SEC state code '{state_country}' -> US"
                    elif state_country in JURISDICTION_ISO:
                        iso_code = JURISDICTION_ISO[state_country]
                        country_source = f"SEC state/country '{state_country}'"
                    elif len(state_country) == 2 and state_country.isalpha():
                        iso_code = state_country
                        country_source = f"SEC state/country '{state_country}'"
                if iso_code:
                    country = iso_code
                    confidence = "medium"
            if not country:
                self.report["dropped"].append({"source": "sec", "reason": "pais_indeterminado",
                                               "record": entity})
                continue

            offering = num(pick(p, "total_offering_amount_usd", "offering_usd", "total_offering_amount"))
            sold = num(pick(p, "total_amount_sold_usd", "sold_usd", "total_amount_sold"))
            remaining = num(pick(p, "total_remaining_usd", "remaining_usd", "total_remaining"))
            if remaining is None and offering is not None and sold is not None:
                remaining = max(offering - sold, 0.0)
            filed = parse_date(p.get("filing_date"))
            sale = parse_date(p.get("sale_date"))

            # Prioridade 1: tese declarada via Shizune (nao aplicavel a SPVs)
            if is_spv_shell(entity):
                match, match_method, match_conf = None, "", ""
            else:
                match, match_method, match_conf = self.shizune.match(firm_key or firm_name)
            if match and match["stages"]:
                stages, method = match["stages"], "DECLARED_THESIS"
                basis = f"Estagios declarados no Shizune ({match['name']})"
                declared = True
            else:
                declared = False
                offering_usable = offering if (offering is not None and offering > 0) else None
                stages, method = heuristic_stage(offering_usable)
                basis = (f"Form D sem estagio declarado: porte do veiculo US$ {offering_usable:,.0f} "
                         f"mapeado nas faixas empiricas Growth List"
                         if offering_usable else "Form D sem valor numerico: faixa padrao Seed/Serie A")

            # Corte: gestora com atividade no Brasil (Shizune Brazil) => GLOBAL_ATIVO_BR
            if match and match.get("has_brazil_activity") and country != "BR":
                cut = CUT_ACTIVE
            elif country == "BR":
                cut = CUT_BR
            else:
                cut = CUT_EX

            undisclosed = ["estimated_ticket_min_usd", "estimated_ticket_max_usd",
                           "lead_investor_rate"]
            if offering is None:
                undisclosed.append("aum_or_offering_usd")
            if remaining is None:
                undisclosed.append("dry_powder_usd")

            url = rec.get("source_url")
            if not (url or "").startswith("https://"):
                url = SEC_FORMD_DATASETS_URL
            brazil_deals = (int(match["brazil_deals"])
                            if match and match.get("brazil_deals") is not None else None)
            if brazil_deals is None:
                undisclosed.append("brazil_deals_count")

            record = {
                "fund_id": f"sec:{rec.get('record_id') or cik}",
                "firm_name": firm_name or entity,
                "vehicle_name": entity,
                "regulatory_id": cik.ljust(10, "0") if cik else None,
                "geographic_cut": cut,
                "hq_country": country,
                "target_stages": order_stages(stages),
                "stage_classification_method": method,
                "estimated_ticket_min_usd": None,
                "estimated_ticket_max_usd": None,
                "aum_or_offering_usd": offering,
                "lead_investor_rate": None,
                "active_status": bool(filed and (today - filed).days <= 36 * 31),
                "primary_source_name": "SEC EDGAR - Form D",
                "primary_source_url": url,
                "estimate_basis": (f"TOTALOFFERINGAMOUNT US$ {offering:,.0f} (Form D filed "
                                   f"{iso(filed) or 'n/d'})" if offering else basis),
                "undisclosed_fields": sorted(set(undisclosed)),
                "evidence_urls": sorted({url, SEC_FORMD_DATASETS_URL}),
                "resolution": {
                    "firm_key": f"sec_cik:{cik}" if cik else f"name:{normalize_name(entity)}",
                    "vehicle_key": f"sec_acc:{rec.get('record_id')}",
                    "method": "CIK" if cik else "NOME_NORMALIZADO",
                    "confidence": "high" if cik else "medium",
                },
                "stage_basis": basis,
                "jurisdiction_of_inc": p.get("jurisdiction_of_inc"),
                "country_source": country_source,
                "country_confidence": confidence,
                "filing_date": iso(filed),
                "first_sale_date": iso(sale),
                "capital_committed_usd": offering,
                "capital_invested_usd": sold,
                "dry_powder_usd": remaining,
                "reporting_date": iso(filed),
                "wikidata_qid": wd["qid"] if wd else None,
                "brazil_deals_basis": match.get("brazil_deals_basis") if match else None,
                "declared_thesis_charts": match.get("charts") if match else None,
                "declared_thesis_source": match["url"] if declared else None,
                "declared_thesis_match": {
                    "name": match["name"], "method": match_method,
                    "confidence": match_conf,
                } if match else None,
                "vehicle_type": "FUND",
            }
            if brazil_deals is not None:
                record["brazil_deals_count"] = brazil_deals
            self.records.append(record)

        self.report["scope"] = {
            "br": "todos os FIPs com situacao 'Em Funcionamento Normal' no registro CVM (RCVM 175)",
            "global": ("veiculos 'Venture Capital Fund' do Form D (SEC) deduplicados por CIK, "
                       "exceto filings com oferta declarada < US$ 1.000.000"),
            "global_min_offering_usd": 1_000_000,
            "excluded_micro_filings": len(excluded_micro),
            "excluded_micro_offering_usd_total": round(
                sum(x["offering_usd"] for x in excluded_micro if x["offering_usd"]), 2),
            "excluded_spv_vehicles": len(excluded_spv),
            "excluded_spv_offering_usd_total": round(
                sum(x["offering_usd"] for x in excluded_spv if x["offering_usd"]), 2),
            "exclusion_rule": ("fora do universo: filings VC com oferta declarada < US$ 1 mi "
                               "(veiculos nominais) e SPVs de deal unico identificados pelo nome "
                               "legal (SPV / series of / co-invest / access fund)"),
        }

    # -- KPIs -------------------------------------------------------------
    def compute(self) -> dict[str, Any]:
        recs = self.records
        today = date.today()

        def primary(rec: dict[str, Any]) -> str:
            st = rec.get("target_stages") or []
            return st[0] if st else "NA"

        by_cut = {c: sum(1 for r in recs if r["geographic_cut"] == c)
                  for c in (CUT_BR, CUT_ACTIVE, CUT_EX)}
        by_method: dict[str, int] = {}
        for r in recs:
            by_method[r["stage_classification_method"]] = by_method.get(
                r["stage_classification_method"], 0) + 1
        by_primary_stage: dict[str, int] = {}
        by_membership: dict[str, int] = {}
        for r in recs:
            by_primary_stage[primary(r)] = by_primary_stage.get(primary(r), 0) + 1
            for s in r["target_stages"]:
                by_membership[s] = by_membership.get(s, 0) + 1

        capital = {"by_primary_stage_usd": {s: 0.0 for s in STAGES}, "by_cut_usd": {
            CUT_BR: 0.0, CUT_ACTIVE: 0.0, CUT_EX: 0.0}, "total_usd": 0.0}
        aum_values = []
        for r in recs:
            v = r.get("aum_or_offering_usd")
            if v is None:
                continue
            capital["by_primary_stage_usd"][primary(r)] = capital["by_primary_stage_usd"].get(
                primary(r), 0.0) + v
            capital["by_cut_usd"][r["geographic_cut"]] += v
            capital["total_usd"] += v
            aum_values.append(v)
        capital["by_primary_stage_usd"] = {k: round(v, 2) for k, v in capital["by_primary_stage_usd"].items()}
        capital["by_cut_usd"] = {k: round(v, 2) for k, v in capital["by_cut_usd"].items()}
        capital["total_usd"] = round(capital["total_usd"], 2)

        # Liquidez: Brasil pela regra 1.3 da spec (PL crescente nos 2 informes);
        # global pelo dry powder estrutural do Form D (offering - sold).
        dry_global = sum(r["dry_powder_usd"] for r in recs if r.get("dry_powder_usd"))
        dry_coverage = sum(1 for r in recs if r.get("dry_powder_usd") is not None)
        br_capital_inconsistent = sum(1 for r in recs if r.get("capital_consistente") is False)
        br_capital_scale = sum(1 for r in recs
                               if "cvm_capital_comprometido_escala_suspeita"
                               in (r.get("data_quality_flags") or []))

        br_recs = [r for r in recs if r["geographic_cut"] == CUT_BR]
        active_recs = [r for r in recs if r["geographic_cut"] == CUT_ACTIVE]
        ex_recs = [r for r in recs if r["geographic_cut"] == CUT_EX]
        non_br = [r for r in recs if r["hq_country"] != "BR"]
        cross_border = (len(active_recs) / len(non_br)) if non_br else None

        pl_up = sum(1 for r in br_recs if r.get("pl_trend") == "up")
        pl_known = sum(1 for r in br_recs if r.get("pl_trend"))
        liquidity_br = (pl_up / pl_known) if pl_known else None
        raising_global = sum(1 for r in (active_recs + ex_recs)
                             if r.get("dry_powder_usd") and r["dry_powder_usd"] > 0)
        raising_denom = sum(1 for r in (active_recs + ex_recs)
                            if r.get("dry_powder_usd") is not None)
        reported_committed_brl = 0.0
        reported_invested_brl = 0.0
        for r in br_recs:
            if r.get("capital_committed_usd"):
                reported_committed_brl += r["capital_committed_usd"] * self.fx.points[-1][1]
            if r.get("capital_invested_usd"):
                reported_invested_brl += r["capital_invested_usd"] * self.fx.points[-1][1]

        def median(values: list[float]) -> float | None:
            return round(statistics.median(values), 2) if values else None

        sorted_aum = sorted(aum_values, reverse=True)
        top10_share = (sum(sorted_aum[:10]) / sum(sorted_aum)) if sorted_aum and sum(sorted_aum) else None

        kpis = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "fx": {
                "pair": "BRL/USD",
                "source": self.fx.source,
                "source_url": self.fx.source_url,
                "reference_date": self.fx.points[-1][0].isoformat(),
                "rate": round(self.fx.points[-1][1], 6),
                "points": len(self.fx.points),
                "method": "cotacao de venda PTAX da competencia mais proxima <= data do informe",
            },
            "counts": {
                "total": len(recs),
                "by_cut": by_cut,
                "by_primary_stage": {s: by_primary_stage.get(s, 0) for s in STAGES},
                "by_stage_membership": {s: by_membership.get(s, 0) for s in STAGES},
                "by_primary_stage_and_cut": {
                    cut: {s: sum(1 for r in recs if r["geographic_cut"] == cut
                                 and primary(r) == s) for s in STAGES}
                    for cut in (CUT_BR, CUT_ACTIVE, CUT_EX)
                },
                "by_stage_membership_and_cut": {
                    cut: {s: sum(1 for r in recs if r["geographic_cut"] == cut
                                 and s in r["target_stages"]) for s in STAGES}
                    for cut in (CUT_BR, CUT_ACTIVE, CUT_EX)
                },
                "by_classification_method": by_method,
                "active": sum(1 for r in recs if r["active_status"]),
            },
            "capital": {**capital,
                        "dry_powder_usd": round(dry_global, 2),
                        "dry_powder_global_usd": round(dry_global, 2),
                        "dry_powder_br_usd": None,
                        "dry_powder_br_status": "nao_publicado",
                        "dry_powder_br_reason": (
                            "Os campos VL_CAP_COMPROM/VL_CAP_INTEGR do Informe Quadrimestral da CVM "
                            "sao internamente inconsistentes em 19,6% das linhas (capital "
                            "integralizado > comprometido) e contem erro de escala conhecido; por isso "
                            "nao sustentam um indicador de dry powder. A liquidez brasileira e medida "
                            "por PL crescente entre os dois ultimos informes (regra 1.3 da spec)."),
                        "dry_powder_coverage_records": dry_coverage,
                        "cvm_capital_reporting": {
                            "committed_brl_reportado": round(reported_committed_brl, 2),
                            "integralizado_brl_reportado": round(reported_invested_brl, 2),
                            "registros_com_inconsistencia": br_capital_inconsistent,
                            "registros_com_escala_suspeita": br_capital_scale,
                            "usado_em_kpi": False,
                            "motivo": ("fonte com 1.130 de 5.754 linhas (19,6%) declarando "
                                       "integralizado > comprometido, 658 linhas com comprometido=0 "
                                       "e erro de escala (ex.: HMV R$ 305,9 bi de comprometido com PL "
                                       "de R$ 0,4 bi)"),
                        },
                        "median_aum_usd": median(aum_values),
                        "top10_share": round(top10_share, 4) if top10_share is not None else None},
            "kpis": {
                "brasil_funds": len(br_recs),
                "brasil_median_aum_usd": median([r["aum_or_offering_usd"] for r in br_recs
                                                 if r.get("aum_or_offering_usd")]),
                "brasil_capital_usd": round(capital["by_cut_usd"][CUT_BR], 2),
                "global_active_br": len(active_recs),
                "global_ex_br": len(ex_recs),
                "cross_border_index": round(cross_border, 4) if cross_border is not None else None,
                "cross_border_definition": ("fundos com hq_country != BR classificados como "
                                            "GLOBAL_ATIVO_BR (deal no Brasil via Shizune) sobre o "
                                            "total de fundos nao-brasileiros"),
                "brasil_liquidez_pl_em_alta_share": round(liquidity_br, 4) if liquidity_br is not None else None,
                "brasil_liquidez_pl_em_alta_fundos": pl_up,
                "brasil_liquidez_base_fundos": pl_known,
                "brasil_liquidez_definicao": ("share de fundos com patrimonio liquido crescente entre "
                                              "os dois ultimos informes quadrimestrais (regra 1.3 da spec)"),
                "global_funds_still_raising_share": round(raising_global / raising_denom, 4)
                if raising_denom else None,
                "global_funds_still_raising": raising_global,
                "declared_thesis_records": by_method.get("DECLARED_THESIS", 0),
                "regulatory_class_records": by_method.get("REGULATORY_CLASS_CVM_BNDES", 0),
                "ticket_heuristic_records": by_method.get("IMPUTED_BY_TICKET_HEURISTIC", 0),
            },
            "density_by_stage": {
                cut: {s: {
                    "funds": sum(1 for r in recs if r["geographic_cut"] == cut and s in r["target_stages"]),
                    "capital_usd": round(sum(r["aum_or_offering_usd"] or 0 for r in recs
                                             if r["geographic_cut"] == cut and s in r["target_stages"]), 2),
                } for s in STAGES}
                for cut in (CUT_BR, CUT_ACTIVE, CUT_EX)
            },
            "sources": [],
            "notes": [],
        }
        kpis["notes"] = [
            "Contagem por estagio primario soma o total de registros; a contagem por participacao "
            "(by_stage_membership) soma mais que o total porque um fundo pode atuar em mais de um estagio.",
            "Capital por estagio usa o estagio primario para evitar dupla contagem do mesmo patrimonio.",
            "Valores BRL convertidos pela PTAX de venda do BCB da competencia mais proxima anterior.",
            "Liquidez Brasil: percentual de fundos com PL crescente entre os dois ultimos informes "
            "quadrimestrais (regra 1.3 da spec). Dry powder estrutural e publicado apenas para o Form D "
            "da SEC (offering - sold), onde o campo e consistente; no Informe Quadrimestral da CVM os "
            "campos de capital comprometido/integralizado sao internamente inconsistentes e por isso "
            "foram preservados por registro com flag de qualidade, sem compor KPI.",
            "Estagios sem tese declarada nem classe regulatoria sao imputados pelo porte do veiculo "
            "nas faixas empiricas Growth List (registrado em stage_classification_method).",
            "Nenhum valor ausente na fonte oficial foi estimado: campos nao divulgados recebem null "
            "e constam em undisclosed_fields de cada registro.",
        ]
        return kpis

    # -- sources registry --------------------------------------------------
    def build_sources_registry(self) -> dict[str, Any]:
        def read(name: str) -> Any:
            return load_json(self.interim / name, {})

        br = read("br_sources.json")
        gl = read("global_sources.json")
        di = read("directories_availability.json")
        sources: list[dict[str, Any]] = []

        def add(name, url, license_, records=None, bytes_=None, sha=None, notes=None, status="ok"):
            sources.append({"name": name, "url": url, "license": license_, "records": records,
                            "bytes": bytes_, "sha256": sha, "status": status, "notes": notes})

        add("CVM - Registro de Fundos e Classes (RCVM 175)",
            "https://dados.cvm.gov.br/dataset/fi-cad",
            "Licenca Aberta de Bases de Dados (ODbL) - dados.cvm.gov.br", self.report["inputs"]["br_funds"])
        add("CVM - Informe Quadrimestral de FIP",
            "https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral",
            "Licenca Aberta de Bases de Dados (ODbL) - dados.cvm.gov.br")
        add("BNDES - Renda Variavel / Carteira de Fundos",
            "https://dadosabertos.bndes.gov.br/dataset/renda-variavel",
            "Dados abertos BNDES", self.report["inputs"]["br_bndes"])
        add("SEC EDGAR - Form D Data Sets", SEC_FORMD_DATASETS_URL, "Dominio publico (SEC)", None)
        add("Wikidata Query Service (SPARQL)",
            "https://query.wikidata.org/", "CC0 1.0 (Wikidata)", len(self.wikidata.by_key))
        add("Shizune - Investors Database",
            "https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md",
            "Conteudo publico citavel (shizune.co)", len(self.shizune.by_key))
        add("Banco Central do Brasil - PTAX", self.fx.source_url,
            "Dados abertos BCB (OData)", len(self.fx.points), notes="Serie de cambio oficial")
        for bloco, origem in (("sources", "br_sources"), ("sources", "global_sources")):
            for item in (br.get(bloco) or []) if isinstance(br, dict) else []:
                if isinstance(item, dict) and item.get("url"):
                    add(f"CVM - {item.get('name') or item['url']}", item["url"],
                        "Dados abertos CVM", item.get("rows") or item.get("records"),
                        item.get("bytes"), item.get("sha256"), item.get("notes"))
        if isinstance(gl, dict):
            for item in gl.get("sources") or []:
                if isinstance(item, dict) and item.get("url"):
                    add(item.get("name") or item["url"], item["url"],
                        item.get("license") or "Dominio publico / aberto",
                        item.get("records"), item.get("bytes"), item.get("sha256"),
                        item.get("notes"), item.get("status", "ok"))
            for lim in gl.get("limitations") or []:
                self.report["warnings"].append({"source": "global", "note": lim})
        if isinstance(di, dict):
            for item in (di.get("sources") or di.get("availability") or []):
                if isinstance(item, dict) and item.get("url"):
                    add(item.get("name") or item["url"], item["url"],
                        item.get("license") or "Conteudo publico",
                        item.get("records"), item.get("bytes"),
                        notes=item.get("fallback") or item.get("notes"),
                        status=item.get("status", "ok"))
            if di.get("openvc"):
                self.report["warnings"].append({"source": "openvc", "note": di["openvc"]})
        return {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "manifest": "data/manifest.jsonl",
            "sources": sources,
            "benchmarks": (di or {}).get("benchmarks") or [],
            "limitations": self.report["warnings"],
        }

    # -- run ---------------------------------------------------------------
    def run(self, write: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        self.build_br()
        self.build_global()
        kpis = self.compute()
        registry = self.build_sources_registry()
        kpis["sources"] = [{"name": s["name"], "url": s["url"], "license": s["license"],
                            "records": s["records"]} for s in registry["sources"]]
        report = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            **self.report,
            "outputs": {
                "records": len(self.records),
                "by_cut": kpis["counts"]["by_cut"],
                "by_primary_stage": kpis["counts"]["by_primary_stage"],
                "data_quality": {
                    "records_with_flags": sum(1 for r in self.records if r.get("data_quality_flags")),
                    "cvm_capital_inconsistente": sum(
                        1 for r in self.records if r.get("capital_consistente") is False),
                    "aum_null": sum(1 for r in self.records
                                    if r.get("aum_or_offering_usd") is None),
                    "dry_powder_null": sum(1 for r in self.records
                                           if r.get("dry_powder_usd") is None),
                    "negativos": sum(1 for r in self.records
                                     if (r.get("aum_or_offering_usd") or 0) < 0
                                     or (r.get("dry_powder_usd") or 0) < 0),
                },
            },
        }
        if write:
            self.canon.mkdir(parents=True, exist_ok=True)
            payload = {
                "generated_at": report["generated_at"],
                "contract": "contract/venture_capital_fund.schema.json",
                "dataset": "VentureCapitalFundRecord",
                "count": len(self.records),
                "records": self.records,
            }
            (self.canon / "funds.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            (self.canon / "kpis.json").write_text(
                json.dumps(kpis, ensure_ascii=False, indent=1), encoding="utf-8")
            (self.canon / "transform_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            (ROOT / "pipeline" / "sources_registry.json").write_text(
                json.dumps(registry, ensure_ascii=False, indent=1), encoding="utf-8")
        return self.records, kpis, report


def main() -> int:
    ap = argparse.ArgumentParser(description="Transforma evidencia interim em dataset canonico SDD.")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--validate", action="store_true", help="valida o resultado contra o contrato")
    args = ap.parse_args()
    t = Transform()
    records, kpis, report = t.run(write=not args.no_write)
    print(json.dumps(report["outputs"], ensure_ascii=False, indent=1))
    print("inputs:", json.dumps(report["inputs"], ensure_ascii=False))
    if report["dropped"]:
        print("dropped:", len(report["dropped"]), "exemplos:", json.dumps(report["dropped"][:3], ensure_ascii=False))
    if args.validate:
        from schema_validate import load_schema, validate_records
        result = validate_records(records, load_schema())
        print("gate2_schema:", json.dumps({k: v for k, v in result.items() if k != "failures"}))
        if result["failures"]:
            print(json.dumps(result["failures"][:3], ensure_ascii=False, indent=1))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
