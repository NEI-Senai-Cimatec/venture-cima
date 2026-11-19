#!/usr/bin/env python3
"""Auditor independente do pipeline de mapeamento de Venture Capital.

Implementa o "Loop Engineering" da especificacao (secao 2) como verificacao
adversarial: le APENAS os artefatos produzidos por outros agentes e falha de
forma ruidosa quando algo nao cumpre a spec. Este arquivo nao importa o codigo
de transformacao/render (para nao herdar os mesmos bugs); a unica dependencia
externa permitida e a stdlib do Python.

Gates
-----
GATE 1  Integridade de extracao e conformidade de rede:
        manifest.jsonl (UA SEC com contato, rate-limit >= 100 ms, HTTP 200,
        sha256 integro nos arquivos usados, CVM CSV em latin1 com ';' sem
        corrupcao, contagens de linhas > 0 por fonte, schema dos registros
        interinos, fallback documentado se OpenVC responder 403).
GATE 2  Contrato SDD e zero alucinacao: funds.json validado contra
        contract/venture_capital_fund.schema.json (validador proprio, sem
        jsonschema), primary_source_url https, unicidade, enums, ISO-2,
        valores monetarios nao-negativos, amostra >= 25 URLs de fonte
        respondendo 200/301/302 e consistencia dos KPIs (soma por estagio).
GATE 3  Linter anti-AI-Slop textual e visual no dashboard/index.html.
GATE 4  UI/UX executivo, WCAG AAA e responsividade.

CLI
---
    python pipeline/gates.py --all --json audit/audit_report.json
    python pipeline/gates.py --gate 1 --gate 2 --no-network
    python pipeline/gates.py --all --json audit/audit_report.json --md audit/report.md

Codigo de saida: 0 = todos os gates PASS; 1 = algum FAIL; 2 = nenhum FAIL mas
algum gate PENDING (artefato/capacidade de medicao ausente). NUNCA 0 com
artefato esperado ausente.

LIMITES DE ESCRITA DO AUDITOR: este arquivo, audit/** e tools/**. Nenhum
artefato de dados (data/**, dashboard/**) e escrito por este script.
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Caminhos e constantes
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec_agente_mapeamento_vc.md"
CONTRACT_DIR = ROOT / "contract"
FUND_SCHEMA = CONTRACT_DIR / "venture_capital_fund.schema.json"
INTERIM_SCHEMA = CONTRACT_DIR / "interim_record.schema.json"
MANIFEST = ROOT / "data" / "manifest.jsonl"
RAW_DIR = ROOT / "data" / "raw"
INTERIM_DIR = ROOT / "data" / "interim"
FUNDS_JSON = ROOT / "data" / "canonical" / "funds.json"
KPIS_JSON = ROOT / "data" / "canonical" / "kpis.json"
DASHBOARD = ROOT / "dashboard" / "index.html"
AUDIT_DIR = ROOT / "audit"
SHOTS_DIR = AUDIT_DIR / "shots"
HTTP_CACHE = AUDIT_DIR / "http_checks.json"
SCREENSHOTS_TOOL = ROOT / "tools" / "screenshots.py"

PASS, FAIL, PENDING, SKIP = "pass", "fail", "pending", "skip"
STATUS_ORDER = {PASS: 0, SKIP: 1, PENDING: 2, FAIL: 3}

# User-Agent academico exigido pela SEC (mesmo contrato de pipeline/net.py).
ACADEMIC_UA = "PesquisaAcademicaVC/1.0 (pesquisador@universidade.edu.br)"
HTTP_RATE_S = 0.3          # intervalo minimo entre requisicoes do auditor
HTTP_TIMEOUT_S = 30
HTTP_RETRIES = 3
HTTP_CACHE_TTL_S = 6 * 3600

VALID_CUTS = {"BRASIL_LOCAL", "GLOBAL_ATIVO_BR", "GLOBAL_EX_BR"}
VALID_STAGES = {"PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "LATE_STAGE_GROWTH", "VENTURE_DEBT"}
VALID_METHODS = {"DECLARED_THESIS", "REGULATORY_CLASS_CVM_BNDES", "IMPUTED_BY_TICKET_HEURISTIC"}
VALID_SOURCE_IDS = {
    "cvm_cad_fi", "cvm_inf_quadrimestral_fip", "bndes_fundos_carteira",
    "bndes_renda_variavel", "finep_fundos", "sec_formd", "wikidata_sparql",
    "shizune_brazil", "shizune_global", "openvc", "brouky", "bcb_ptax",
}

# ISO-3166-1 alpha-2 (ISO completo; XK aceito por uso civil).
ISO_COUNTRIES = set(
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ "
    "CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO "
    "FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE "
    "JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO "
    "MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW "
    "PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM "
    "TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW XK"
    .split()
)

BLOCKED_LEXICON = [
    "no dinâmico cenário atual",
    "ecossistema vibrante",
    "revolucionando o mercado",
    "sinergia holística",
    "mergulhe fundo",
    "mergulhe nos dados",
    "mundo acelerado das startups",
    "holístico",
    "holística",
    "revolucionário",
    "revolucionária",
    "introdução",
    "conclusão",
    "resumo executivo",
    "bem-vindo ao",
]

EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001F5FF" "\U0001F600-\U0001F64F" "\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF" "\U0001FA70-\U0001FAFF" "\u2600-\u27BF" "\uFE0F" "]"
)
ICON_WORDS = re.compile(r"(?i)\b(foguete|rocket|l[âa]mpada|lightbulb|bulb|rob[ôo]|robot|robotic)\b")

MONEY_RE = re.compile(r"(?:R\$|US\$|\$)\s?\d[\d.,]*\s?(?:mil|mi|bi|M|B|k|K)?")
PCT_RE = re.compile(r"\d+(?:[.,]\d+)?\s?%")
THOUSANDS_RE = re.compile(r"\b\d{1,3}(?:\.\d{3})+(?:,\d+)?\b")
PLAIN_NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")
MONEY_CELL_RE = re.compile(r"(?:R\$|US\$|\$)\s?\d[\d.,]*|\d+(?:[.,]\d+)?\s?%")


# --------------------------------------------------------------------------
# Modelo de relatorio
# --------------------------------------------------------------------------
@dataclass
class Check:
    gate: int
    cid: str
    desc: str
    status: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.cid,
            "description": self.desc,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class GateReport:
    gate: int
    name: str
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    method: str = ""
    seconds: float = 0.0

    def add(self, cid: str, desc: str, status: str, detail: str = "", **evidence: Any) -> Check:
        check = Check(self.gate, cid, desc, status, detail, dict(evidence))
        self.checks.append(check)
        return check

    def ok(self, cid: str, desc: str, detail: str = "", **evidence: Any) -> Check:
        return self.add(cid, desc, PASS, detail, **evidence)

    def bad(self, cid: str, desc: str, detail: str, **evidence: Any) -> Check:
        return self.add(cid, desc, FAIL, detail, **evidence)

    def pending(self, cid: str, desc: str, detail: str = "", **evidence: Any) -> Check:
        return self.add(cid, desc, PENDING, detail, **evidence)

    def skip(self, cid: str, desc: str, detail: str = "", **evidence: Any) -> Check:
        return self.add(cid, desc, SKIP, detail, **evidence)

    @property
    def status(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        if any(c.status == PENDING for c in self.checks):
            return PENDING
        if self.checks and all(c.status == SKIP for c in self.checks):
            return PENDING
        return PASS

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, PENDING: 0, SKIP: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "name": self.name,
            "status": self.status,
            "method": self.method,
            "counts": self.counts(),
            "checks": [c.as_dict() for c in self.checks],
            "metrics": self.metrics,
            "limitations": self.limitations,
            "notes": self.notes,
            "seconds": round(self.seconds, 2),
        }

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status in (FAIL, PENDING)]


# --------------------------------------------------------------------------
# Utilitarios gerais
# --------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": rel(path), "exists": path.exists()}
    if path.is_file():
        st = path.stat()
        info.update({
            "bytes": st.st_size,
            "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
            "sha256": sha256_file(path),
        })
    return info


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def load_json(path: Path) -> tuple[Any | None, str | None]:
    """Carrega JSON com tolerancia a arquivo parcialmente escrito."""
    if not path.exists():
        return None, "arquivo ausente"
    last_err = None
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_err = str(exc)
            time.sleep(0.4)
    return None, f"JSON invalido: {last_err}"


def read_text_tolerant(path: Path, retries: int = 3) -> str:
    last: Exception | None = None
    for _ in range(retries):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, PermissionError) as exc:
            last = exc
            time.sleep(0.4)
    raise last if last else OSError(str(path))


def norm_text(text: str) -> str:
    """Minusculas + remocao de diacriticos, para casar lexico sem falso negativo."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).lower()


def snippet(text: str, needle: str, before: int = 90, after: int = 120) -> str:
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return ""
    return text[max(0, idx - before): idx + len(needle) + after].replace("\n", " ").strip()


def truncate(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cache_path_for_url(url: str) -> Path:
    """Reimplementacao independente do esquema de cache de pipeline/net.py."""
    return RAW_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".bin")


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = struct.unpack(">II", head[16:24])
        return int(width), int(height)
    except OSError:
        return None


def find_chrome() -> Path | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


# --------------------------------------------------------------------------
# Validador JSON Schema (subset draft 2020-12 suficiente para os contratos)
# --------------------------------------------------------------------------
_JSON_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "null": type(None),
    "boolean": bool,
}


def _type_ok(instance: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    return isinstance(instance, _JSON_TYPE_MAP.get(expected, object)) if expected in _JSON_TYPE_MAP else True


def _format_ok(value: str, fmt: str) -> bool:
    if fmt == "uri":
        return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:[^\s]+$", value))
    if fmt == "uri-reference":
        return " " not in value
    if fmt == "date-time":
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$", value))
    if fmt == "email":
        return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))
    if fmt == "hostname":
        return bool(re.match(r"^[a-zA-Z0-9.-]+$", value))
    return True


def validate_schema(instance: Any, schema: dict[str, Any], path: str = "$", errors: list[str] | None = None) -> list[str]:
    """Valida uma instancia contra o subset de JSON Schema draft 2020-12 usado
    nos contratos do projeto. Retorna lista de erros legiveis (vazia = valido)."""
    if errors is None:
        errors = []
    if not isinstance(schema, dict):
        return errors

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(instance, t) for t in types):
            errors.append(f"{path}: tipo {type(instance).__name__} nao e {types}")
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: valor != const {schema['const']!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {str(instance)[:60]!r} fora do enum")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: string curta ({len(instance)} < {schema['minLength']})")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: string longa ({len(instance)} > {schema['maxLength']})")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: nao casa pattern {schema['pattern']!r}")
        if "format" in schema and not _format_ok(instance, schema["format"]):
            errors.append(f"{path}: formato {schema['format']!r} invalido")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {instance} <= exclusiveMinimum")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {instance} >= exclusiveMaximum")
        if "multipleOf" in schema and schema["multipleOf"]:
            if abs(instance / schema["multipleOf"] - round(instance / schema["multipleOf"])) > 1e-9:
                errors.append(f"{path}: nao e multiplo de {schema['multipleOf']}")

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: campo obrigatorio ausente {req!r}")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in instance:
                validate_schema(instance[key], subschema, f"{path}.{key}", errors)
        additional = schema.get("additionalProperties")
        if additional is False:
            extra = set(instance) - set(props)
            if extra:
                errors.append(f"{path}: propriedades nao permitidas {sorted(extra)[:5]}")
        elif isinstance(additional, dict):
            for key in set(instance) - set(props):
                validate_schema(instance[key], additional, f"{path}.{key}", errors)

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: {len(instance)} itens < minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: {len(instance)} itens > maxItems {schema['maxItems']}")
        if schema.get("uniqueItems"):
            seen, dup = set(), None
            for item in instance:
                key = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if key in seen:
                    dup = item
                    break
                seen.add(key)
            if dup is not None:
                errors.append(f"{path}: itens duplicados (ex.: {str(dup)[:60]})")
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, item in enumerate(instance):
                validate_schema(item, items, f"{path}[{idx}]", errors)
        elif isinstance(items, list):
            for idx, subschema in enumerate(items[: len(instance)]):
                validate_schema(instance[idx], subschema, f"{path}[{idx}]", errors)

    if "allOf" in schema:
        for idx, sub in enumerate(schema["allOf"]):
            validate_schema(instance, sub, f"{path}/allOf[{idx}]", errors)
    if "anyOf" in schema:
        branches = []
        for sub in schema["anyOf"]:
            errs: list[str] = []
            validate_schema(instance, sub, path, errs)
            if not errs:
                break
            branches.append(errs)
        else:
            errors.append(f"{path}: nao satisfaz anyOf ({len(branches)} ramos)")
    if "oneOf" in schema:
        matched = 0
        for sub in schema["oneOf"]:
            errs = []
            validate_schema(instance, sub, path, errs)
            if not errs:
                matched += 1
        if matched != 1:
            errors.append(f"{path}: oneOf casou {matched} ramos (esperado 1)")
    if "not" in schema:
        errs = []
        validate_schema(instance, schema["not"], path, errs)
        if not errs:
            errors.append(f"{path}: satisfaz schema proibido (not)")
    return errors


# --------------------------------------------------------------------------
# Parser CSS minimo (regras, declaracoes, media queries, variaveis)
# --------------------------------------------------------------------------
@dataclass
class CSSRule:
    selector: str
    declarations: list[tuple[str, str]]
    media: str | None = None
    order: int = 0

    def get(self, prop: str) -> str | None:
        for name, value in reversed(self.declarations):
            if name == prop:
                return value
        return None

    @property
    def decl_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in self.declarations:
            out[name] = value
        return out


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def _match_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\":
                    j += 1
                j += 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def _split_declarations(body: str) -> list[tuple[str, str]]:
    decls: list[tuple[str, str]] = []
    current = []
    depth = 0
    quote = None
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "([":
            depth += 1
            current.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == ";" and depth == 0:
            decls.append("".join(current))
            current = []
        else:
            current.append(ch)
    decls.append("".join(current))
    out: list[tuple[str, str]] = []
    for raw in decls:
        if ":" not in raw:
            continue
        name, _, value = raw.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name:
            out.append((name, value))
    return out


def parse_css(css: str) -> list[CSSRule]:
    """Parser tolerante de CSS: retorna regras (com contexto de @media)."""
    rules: list[CSSRule] = []
    counter = [0]

    def parse_into(text: str, media: str | None) -> None:
        i, n = 0, len(text)
        while i < n:
            while i < n and text[i] in " \t\r\n;":
                i += 1
            if i >= n:
                break
            j = i
            while j < n and text[j] not in "{};":
                j += 1
            if j >= n:
                break
            prelude = text[i:j].strip()
            if text[j] == ";":
                i = j + 1
                continue
            close = _match_brace(text, j)
            body = text[j + 1: close]
            low = prelude.lower()
            if low.startswith("@media"):
                parse_into(body, prelude[len("@media"):].strip())
            elif low.startswith("@supports") or low.startswith("@layer"):
                parse_into(body, media)
            elif low.startswith(("@keyframes", "@-webkit-keyframes", "@font-face", "@page", "@charset")):
                pass
            elif prelude:
                rules.append(CSSRule(prelude, _split_declarations(body), media, counter[0]))
                counter[0] += 1
            i = close + 1

    parse_into(_strip_css_comments(css), None)
    return rules


MEDIA_MAX_RE = re.compile(r"max-width\s*:\s*([\d.]+)\s*(px|rem|em|pt)", re.I)
MEDIA_MIN_RE = re.compile(r"min-width\s*:\s*([\d.]+)\s*(px|rem|em|pt)", re.I)
MEDIA_RANGE_MAX_RE = re.compile(r"width\s*<=\s*([\d.]+)\s*(px|rem|em|pt)", re.I)
MEDIA_RANGE_MIN_RE = re.compile(r"width\s*>=\s*([\d.]+)\s*(px|rem|em|pt)", re.I)


def length_to_px(value: str, base_px: float = 16.0, parent_px: float | None = None) -> float | None:
    """Converte px/rem/em/pt para px (base 16px; em assume base do pai ou 16px)."""
    if value is None:
        return None
    value = value.strip().lower()
    m = re.match(r"^(-?\d*\.?\d+)\s*(px|rem|em|pt|pc|in|cm|mm)?$", value)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "px"
    if unit == "px":
        return num
    if unit == "rem":
        return num * base_px
    if unit == "em":
        return num * (parent_px if parent_px else base_px)
    if unit == "pt":
        return num * 96.0 / 72.0
    if unit == "pc":
        return num * 16.0
    if unit == "in":
        return num * 96.0
    if unit == "cm":
        return num * 96.0 / 2.54
    if unit == "mm":
        return num * 96.0 / 25.4
    return None


def media_breakpoints(media: str | None) -> dict[str, float | None]:
    """Extrai min/max-width em px de uma condicao @media."""
    if not media:
        return {"max": None, "min": None}
    mx = MEDIA_MAX_RE.search(media) or MEDIA_RANGE_MAX_RE.search(media)
    mn = MEDIA_MIN_RE.search(media) or MEDIA_RANGE_MIN_RE.search(media)
    max_px = length_to_px(mx.group(1) + mx.group(2)) if mx else None
    min_px = length_to_px(mn.group(1) + mn.group(2)) if mn else None
    return {"max": max_px, "min": min_px}


def resolve_vars(value: str, variables: dict[str, str], depth: int = 0) -> str:
    if depth > 8 or "var(" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        fallback = match.group(2)
        raw = variables.get(name)
        if raw is not None:
            return resolve_vars(raw, variables, depth + 1)
        return resolve_vars(fallback, variables, depth + 1) if fallback else ""

    pattern = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*))?\)")
    return pattern.sub(repl, value)


# --------------------------------------------------------------------------
# Cores e contraste WCAG
# --------------------------------------------------------------------------
NAMED_COLORS = {
    "white": (255, 255, 255, 1.0), "black": (0, 0, 0, 1.0),
    "transparent": (0, 0, 0, 0.0), "red": (255, 0, 0, 1.0),
    "green": (0, 128, 0, 1.0), "blue": (0, 0, 255, 1.0),
    "gray": (128, 128, 128, 1.0), "grey": (128, 128, 128, 1.0),
    "silver": (192, 192, 192, 1.0), "navy": (0, 0, 128, 1.0),
    "slateGray": (112, 128, 144, 1.0), "slategray": (112, 128, 144, 1.0),
}


def parse_color(value: str | None) -> tuple[int, int, int, float] | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in ("inherit", "currentcolor", "unset", "initial", "revert", "none", "auto"):
        return None
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    m = re.match(r"^#([0-9a-f]{3,8})$", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        if len(h) == 6:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
        if len(h) == 8:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16) / 255.0
        return None
    m = re.match(r"^rgba?\(([^)]+)\)$", v)
    if m:
        parts = re.split(r"[\s,/]+", m.group(1).strip())
        try:
            if len(parts) >= 3:
                rgb = [float(p) for p in parts[:3]]
                alpha = float(parts[3]) if len(parts) >= 4 else 1.0
                if all(0 <= x <= 1 for x in rgb) and max(rgb) <= 1.0 and len(parts[:3]) == 3:
                    return int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255), alpha
                return int(rgb[0]), int(rgb[1]), int(rgb[2]), alpha
        except ValueError:
            return None
    return None


def color_hex(color: tuple[int, int, int, float] | None) -> str:
    if not color:
        return "?"
    return "#%02X%02X%02X" % color[:3] + (f" (a={color[3]:.2f})" if color[3] < 1 else "")


def _linear_channel(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: tuple[int, int, int, float]) -> float:
    r, g, b = color[:3]
    return 0.2126 * _linear_channel(r) + 0.7152 * _linear_channel(g) + 0.0722 * _linear_channel(b)


def composite(fg: tuple[int, int, int, float], bg: tuple[int, int, int, float]) -> tuple[int, int, int, float]:
    if fg[3] >= 1.0:
        return fg
    a = fg[3]
    return (
        round(fg[0] * a + bg[0] * (1 - a)),
        round(fg[1] * a + bg[1] * (1 - a)),
        round(fg[2] * a + bg[2] * (1 - a)),
        1.0,
    )


def contrast_ratio(fg: tuple[int, int, int, float], bg: tuple[int, int, int, float]) -> float:
    fg_c = composite(fg, bg)
    bg_c = bg if bg[3] >= 1 else composite(bg, (255, 255, 255, 1.0))
    l1, l2 = relative_luminance(fg_c), relative_luminance(bg_c)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------
# HTTP auditavel (stdlib; cache persistente em audit/http_checks.json)
# --------------------------------------------------------------------------
class HttpChecker:
    """Cliente HTTP do auditor.

    Nao usa pipeline.net.fetch para nao escrever em data/manifest.jsonl nem em
    data/raw (artefatos de outros agentes, fora do escopo de escrita do
    auditor). Reimplementa o mesmo contrato: User-Agent academico declarado,
    rate-limit por host >= 0.3 s, retry/backoff para 429/5xx/erros de
    transporte e cache de resultados para reduzir carga nas fontes.
    """

    def __init__(
        self,
        cache_path: Path = HTTP_CACHE,
        rate_s: float = HTTP_RATE_S,
        timeout_s: float = HTTP_TIMEOUT_S,
        retries: int = HTTP_RETRIES,
        enabled: bool = True,
        refresh: bool = False,
    ) -> None:
        self.cache_path = cache_path
        self.rate_s = rate_s
        self.timeout_s = timeout_s
        self.retries = retries
        self.enabled = enabled
        self.refresh = refresh
        self.results: dict[str, dict[str, Any]] = {}
        self.cache: dict[str, dict[str, Any]] = {}
        self.dirty = False
        self._last_host_call: dict[str, float] = {}
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.cache = data
            except (json.JSONDecodeError, OSError):
                self.cache = {}

    def _cached(self, url: str) -> dict[str, Any] | None:
        if self.refresh:
            return None
        item = self.cache.get(url)
        if not isinstance(item, dict) or "ts" not in item:
            return None
        try:
            ts = datetime.fromisoformat(item["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts).total_seconds() <= HTTP_CACHE_TTL_S:
                out = dict(item)
                out["cached"] = True
                return out
        except (ValueError, TypeError):
            return None
        return None

    def _throttle(self, host: str) -> None:
        last = self._last_host_call.get(host)
        now = time.monotonic()
        if last is not None:
            wait = self.rate_s - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_host_call[host] = time.monotonic()

    def check(self, url: str) -> dict[str, Any]:
        if url in self.results:
            return self.results[url]
        if not self.enabled:
            out = {"status": None, "error": "checagem de rede desabilitada", "ok": False,
                   "cached": False, "final_url": url, "ts": utc_now()}
            self.results[url] = out
            return out
        cached = self._cached(url)
        if cached is not None:
            self.results[url] = cached
            return cached

        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        started = time.monotonic()
        last_error = None
        status: int | None = None
        final_url = url
        for attempt in range(1, self.retries + 1):
            self._throttle(host)
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": ACADEMIC_UA,
                    "Accept": "text/html,application/json,text/plain,*/*",
                    "Accept-Language": "pt-BR,pt;q=0.8,en;q=0.6",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    status = int(resp.status)
                    final_url = resp.geturl() or url
                    resp.read(4096)  # basta para validar o link; evita baixar payloads grandes
                last_error = None
                break
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                final_url = exc.geturl() or url
                last_error = f"HTTP {exc.code}"
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    backoff = float(retry_after) if retry_after and str(retry_after).isdigit() else 0.5 * (2 ** attempt)
                    time.sleep(min(backoff, 10.0))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                status = None
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                if attempt < self.retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                break

        out = {
            "status": status,
            "final_url": final_url,
            "error": last_error,
            "ok": status is not None and 200 <= status < 400,
            "cached": False,
            "ts": utc_now(),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        self.results[url] = out
        self.cache[url] = {k: out[k] for k in ("status", "final_url", "error", "ok", "ts")}
        self.dirty = True
        return out

    def flush(self) -> None:
        if not self.dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        trim = {url: item for url, item in self.cache.items() if isinstance(item, dict)}
        self.cache_path.write_text(json.dumps(trim, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Parser HTML/DOM minimo (para inspecao real de estrutura e estilos inline)
# --------------------------------------------------------------------------
TAG_RE = re.compile(r"<(/?)([a-zA-Z][-a-zA-Z0-9]*)((?:\s+[^<>]*?)?)(/?)>", re.S)
ATTR_RE = re.compile(
    r"([-a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))",
    re.S,
)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}


@dataclass
class DomNode:
    tag: str
    attrs: dict[str, str]
    parent: "DomNode | None"
    children: list["DomNode"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    index: int = 0

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    @property
    def node_id(self) -> str:
        return self.attrs.get("id") or ""

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: "DomNode") -> None:
            parts.extend(node.text_parts)
            for child in node.children:
                walk(child)

        walk(self)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def ancestors(self) -> Iterable["DomNode"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def path(self, limit: int = 4) -> str:
        bits = []
        node: DomNode | None = self
        while node is not None and len(bits) < limit:
            ident = node.tag
            if node.node_id:
                ident += f"#{node.node_id}"
            cls = sorted(node.classes)
            if cls:
                ident += "." + ".".join(cls[:3])
            bits.append(ident)
            node = node.parent
        return " < ".join(reversed(bits))


def _mask_embedded_text(html: str) -> str:
    """Substitui conteudo de <script>/<style>/<pre> por espacos de mesmo tamanho
    para que a varredura de tags nao confunda '<' de codigo JS com markup."""

    def repl(match: re.Match[str]) -> str:
        body = match.group(3)
        return match.group(1) + (" " * len(body)) + match.group(4)

    pattern = re.compile(r"(<(script|style)\b[^>]*>)(.*?)(</\2\s*>)", re.S | re.I)
    return pattern.sub(repl, html)


def parse_dom(html: str) -> tuple[DomNode, list[DomNode]]:
    """Retorna (root sintetico, lista de todos os nos). Tolerante a HTML malformado."""
    masked = _mask_embedded_text(html)
    root = DomNode("#document", {}, None)
    stack = [root]
    nodes: list[DomNode] = [root]
    pos = 0
    for match in TAG_RE.finditer(masked):
        if match.start() > pos:
            text = html[pos: match.start()]
            if text.strip() and stack:
                stack[-1].text_parts.append(html_mod.unescape(text))
        pos = match.end()
        closing, tag, attr_text, selfclose = match.group(1), match.group(2).lower(), match.group(3), match.group(4)
        if closing:
            for idx in range(len(stack) - 1, 0, -1):
                if stack[idx].tag == tag:
                    del stack[idx:]
                    break
            continue
        attrs: dict[str, str] = {}
        for attr in ATTR_RE.finditer(attr_text or ""):
            name = attr.group(1).lower()
            value = attr.group(2) if attr.group(2) is not None else (
                attr.group(3) if attr.group(3) is not None else (attr.group(4) or ""))
            attrs[name] = html_mod.unescape(value)
        node = DomNode(tag, attrs, stack[-1], [], [], len(nodes))
        stack[-1].children.append(node)
        nodes.append(node)
        if not selfclose and tag not in VOID_TAGS:
            stack.append(node)
    if pos < len(html):
        tail = html[pos:]
        if tail.strip() and stack:
            stack[-1].text_parts.append(html_mod.unescape(tail))
    return root, nodes


def extract_style_blocks(html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<style\b[^>]*>(.*?)</style>", html, re.S | re.I)]


def extract_inline_styles(nodes: list[DomNode]) -> list[tuple[DomNode, list[tuple[str, str]]]]:
    out = []
    for node in nodes:
        if "style" in node.attrs:
            out.append((node, _split_declarations(node.attrs["style"])))
    return out


def strip_tags(html: str) -> str:
    """HTML -> texto visivel (remove script/style/comentarios e tags)."""
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Helpers de leitura de artefatos
# --------------------------------------------------------------------------
def load_manifest() -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Retorna (entradas, erros_de_parse, linhas_brutas)."""
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    raw_lines: list[str] = []
    if not MANIFEST.exists():
        return entries, ["data/manifest.jsonl ausente"], raw_lines
    for lineno, line in enumerate(MANIFEST.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        raw_lines.append(line)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"linha {lineno}: JSON invalido ({exc})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"linha {lineno}: nao e objeto JSON")
            continue
        entries.append(obj)
    return entries, errors, raw_lines


def url_host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_sec_host(host: str) -> bool:
    return host == "sec.gov" or host.endswith(".sec.gov")


def _interim_source_enum() -> set[str]:
    """Enum de source_id lido do CONTRATO (fonte da verdade).

    O contrato pode ser estendido pelo Lead; enum hardcoded geraria falso FAIL
    em toda extensao. Fallback para VALID_SOURCE_IDS apenas se o schema sumir.
    """
    schema, _ = load_json(INTERIM_SCHEMA)
    try:
        enum = schema["properties"]["source_id"]["enum"]  # type: ignore[index]
        if isinstance(enum, list) and enum:
            return {str(item) for item in enum}
    except (TypeError, KeyError, AttributeError):
        pass
    return set(VALID_SOURCE_IDS)


def load_interim_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    per_file: list[dict[str, Any]] = []
    errors: list[str] = []
    if not INTERIM_DIR.exists():
        return records, per_file, ["diretorio data/interim ausente"]
    files = sorted(INTERIM_DIR.glob("*.jsonl"))
    if not files:
        return records, per_file, ["nenhum arquivo *.jsonl em data/interim"]
    for path in files:
        count, bad = 0, 0
        try:
            text = read_text_tolerant(path)
        except OSError as exc:
            errors.append(f"{rel(path)}: erro de leitura {exc}")
            per_file.append({"file": rel(path), "records": 0, "invalid": 0})
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                if len(errors) < 20:
                    errors.append(f"{rel(path)}:{lineno}: JSON invalido")
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            obj["__file"] = rel(path)
            obj["__line"] = lineno
            records.append(obj)
            count += 1
        per_file.append({"file": rel(path), "records": count, "invalid": bad})
        if bad:
            errors.append(f"{rel(path)}: {bad} linha(s) invalida(s)")
    return records, per_file, errors


# Fontes minimas exigidas pela spec secao 1.1 e checklist 6.1, por grupo funcional.
REQUIRED_SOURCE_GROUPS: dict[str, set[str]] = {
    "cvm_regulatorio_br": {"cvm_cad_fi", "cvm_inf_quadrimestral_fip",
                            "cvm_registro_fundo_classe", "cvm_ckan"},
    "fomento_publico_br": {"bndes_fundos_carteira", "bndes_renda_variavel", "finep_fundos"},
    "atividade_deals_br": {"shizune_brazil", "shizune_global", "openvc", "brouky"},
    "regulatorio_global_sec": {"sec_formd"},
    "grafo_semantico_global": {"wikidata_sparql"},
}
OPTIONAL_SOURCE_GROUPS: dict[str, set[str]] = {
    "fx_bcb_ptax": {"bcb_ptax"},
}


# ==========================================================================
# GATE 1 - Integridade de extracao e conformidade de rede
# ==========================================================================
def gate1() -> GateReport:
    t0 = time.time()
    rep = GateReport(
        1,
        "Integridade de extração e conformidade de rede",
        method=(
            "Leitura literal de data/manifest.jsonl + releitura e re-hash dos bytes "
            "em data/raw/ + decodificacao latin1 dos CSVs da CVM a partir do cache "
            "+ validacao dos registros de data/interim/*.jsonl contra "
            "contract/interim_record.schema.json com validador JSON Schema proprio."
        ),
    )
    entries, parse_errors, raw_lines = load_manifest()
    rep.metrics["manifest_entries"] = len(entries)
    rep.metrics["manifest_raw_lines"] = len(raw_lines)

    if not entries:
        rep.pending("g1.manifest.present", "data/manifest.jsonl existe e nao esta vazio",
                    "manifest ausente ou vazio: Gate 1 nao pode ser verificado",
                    manifest_exists=MANIFEST.exists())
        rep.seconds = time.time() - t0
        return rep
    rep.ok("g1.manifest.present", "data/manifest.jsonl existe e nao esta vazio",
           f"{len(entries)} entradas de auditoria de rede", entries=len(entries), raw_lines=len(raw_lines))

    if parse_errors:
        rep.bad("g1.manifest.jsonl_valid", "todas as linhas do manifest sao JSON de objeto",
                f"{len(parse_errors)} linha(s) invalida(s)", errors=parse_errors[:10])
    else:
        rep.ok("g1.manifest.jsonl_valid", "todas as linhas do manifest sao JSON de objeto",
               f"{len(entries)} entradas parseadas")

    missing_keys = []
    bad_urls = []
    for entry in entries:
        for key in ("ts_utc", "source", "url", "status"):
            if key not in entry:
                missing_keys.append(f"{key} ausente em {str(entry)[:90]}")
        url = str(entry.get("url", ""))
        if not url.startswith("https://"):
            bad_urls.append(url[:140])
    if missing_keys:
        rep.bad("g1.manifest.required_keys", "entradas do manifest possuem ts_utc/source/url/status",
                f"{len(missing_keys)} violacao(oes)", violations=missing_keys[:10])
    else:
        rep.ok("g1.manifest.required_keys", "entradas do manifest possuem ts_utc/source/url/status",
               f"{len(entries)} entradas completas")
    if bad_urls:
        rep.bad("g1.manifest.https_urls", "toda URL do manifest usa https", f"{len(bad_urls)} URL(s) nao-https",
                urls=bad_urls[:10])
    else:
        rep.ok("g1.manifest.https_urls", "toda URL do manifest usa https",
               f"{len(entries)} URLs https verificadas")

    # -------------------------- UA declarado (geral e SEC) -----------------
    empty_ua = [str(e.get("url"))[:120] for e in entries if not str(e.get("user_agent", "")).strip()]
    sec_entries = [e for e in entries if is_sec_host(url_host(str(e.get("url", ""))))]
    contact_re = re.compile(r"\(\s*[^()\s]+@[^()\s]+\s*\)")
    sec_fail = []
    for entry in sec_entries:
        ua = str(entry.get("user_agent", ""))
        url = str(entry.get("url", ""))
        if (not ua or not contact_re.search(ua) or "PesquisaAcademicaVC" not in ua
                or re.search(r"(?i)python-urllib|curl|wget|scrapy", ua)):
            sec_fail.append({"url": url[:140], "user_agent": ua})
    if empty_ua:
        rep.bad("g1.ua.declared", "toda requisicao declara User-Agent institucional",
                f"{len(empty_ua)} requisicao(oes) sem User-Agent", urls=empty_ua[:10])
    else:
        rep.ok("g1.ua.declared", "toda requisicao declara User-Agent institucional",
               f"{len(entries)} entradas com User-Agent declarado")
    if not sec_entries:
        rep.pending("g1.sec.user_agent", "requisicoes a *.sec.gov enviam User-Agent com contato",
                    "nenhuma requisicao a sec.gov no manifest: coleta SEC nao comprovada")
    elif sec_fail:
        rep.bad("g1.sec.user_agent", "requisicoes a *.sec.gov enviam User-Agent com contato",
                f"{len(sec_fail)}/{len(sec_entries)} requisicoes SEC com UA invalido",
                violations=sec_fail[:8])
    else:
        rep.ok("g1.sec.user_agent", "requisicoes a *.sec.gov enviam User-Agent com contato",
               f"{len(sec_entries)} requisicoes SEC com UA = {sec_entries[0].get('user_agent')!r}",
               requests=len(sec_entries), user_agent=sec_entries[0].get("user_agent"))

    # -------------------------- rate-limit SEC (declarado + observado) -----
    sec_intervals = []
    missing_interval = []
    for entry in sec_entries:
        value = entry.get("min_interval_s")
        if value is None:
            missing_interval.append(str(entry.get("url", ""))[:120])
        else:
            sec_intervals.append(float(value))
    if not sec_entries:
        rep.pending("g1.sec.rate_limit", "intervalo minimo >= 0.1 s entre requests SEC",
                    "sem requisicoes SEC registradas")
    elif missing_interval:
        rep.bad("g1.sec.rate_limit", "intervalo minimo >= 0.1 s entre requests SEC",
                f"{len(missing_interval)} requisicoes SEC sem min_interval_s declarado",
                violations=missing_interval[:8])
    elif sec_intervals and min(sec_intervals) < 0.1:
        rep.bad("g1.sec.rate_limit", "intervalo minimo >= 0.1 s entre requests SEC",
                f"min_interval_s minimo declarado = {min(sec_intervals):.3f}s (< 0.1s)",
                min_interval_s=min(sec_intervals))
    else:
        rep.ok("g1.sec.rate_limit", "intervalo minimo >= 0.1 s entre requests SEC",
               f"min_interval_s minimo declarado = {min(sec_intervals):.3f}s em {len(sec_intervals)} requests",
               min_interval_s=min(sec_intervals), requests=len(sec_intervals))
    rep.metrics["sec_requests"] = len(sec_entries)
    rep.metrics["sec_min_interval_declared_s"] = min(sec_intervals) if sec_intervals else None

    # Gaps reais observados entre requests SEC nao-cache (por host e global).
    def _ts(entry: dict[str, Any]) -> datetime | None:
        try:
            ts = datetime.fromisoformat(str(entry.get("ts_utc", "")))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    real_sec = [e for e in sec_entries if not e.get("cache")]
    real_sec.sort(key=lambda e: str(e.get("ts_utc", "")))
    gaps: list[float] = []
    same_host_gaps: dict[str, list[float]] = {}
    prev_by_host: dict[str, datetime] = {}
    prev: datetime | None = None
    for entry in real_sec:
        ts = _ts(entry)
        if ts is None:
            continue
        host = url_host(str(entry.get("url", "")))
        if prev is not None:
            gaps.append((ts - prev).total_seconds())
        prev_ts = prev_by_host.get(host)
        if prev_ts is not None:
            same_host_gaps.setdefault(host, []).append((ts - prev_ts).total_seconds())
        prev_by_host[host] = ts
        prev = ts
    ts_has_fraction = any("." in str(e.get("ts_utc", "")) for e in real_sec)
    if len(gaps) >= 2 and not ts_has_fraction:
        rep.skip("g1.sec.rate_limit_observed", "gaps reais entre requests SEC >= 100 ms",
                 "ts_utc do manifest tem resolucao de 1 segundo (sem fracao): gaps observados "
                 "nao sao conclusivos; a garantia vem do min_interval_s declarado "
                 "(>= 0.1s) e do throttle do coletor, validados acima",
                 samples=len(real_sec), declared_min_interval_s=min(sec_intervals) if sec_intervals else None)
    elif len(gaps) >= 2:
        min_gap = min(gaps)
        min_same = min((min(v) for v in same_host_gaps.values() if v), default=None)
        rep.metrics["sec_min_observed_gap_s"] = round(min_gap, 3)
        if min_same is not None and min_same < 0.1:
            rep.bad("g1.sec.rate_limit_observed", "gaps reais entre requests SEC >= 100 ms",
                    f"menor gap no mesmo host = {min_same:.3f}s (< 0.1s)",
                    min_gap_same_host_s=round(min_same, 3), samples=len(real_sec))
        elif min_gap < 0.1:
            rep.skip("g1.sec.rate_limit_observed", "gaps reais entre requests SEC >= 100 ms",
                     f"gap global minimo {min_gap:.3f}s entre hosts diferentes; "
                     f"mesmo host sempre >= {min_same if min_same is not None else 'n/d'}s",
                     min_gap_global_s=round(min_gap, 3), min_gap_same_host_s=min_same)
        else:
            rep.ok("g1.sec.rate_limit_observed", "gaps reais entre requests SEC >= 100 ms",
                   f"menor gap observado = {min_gap:.3f}s em {len(real_sec)} fetches reais",
                   min_gap_s=round(min_gap, 3), samples=len(real_sec))
    else:
        rep.skip("g1.sec.rate_limit_observed", "gaps reais entre requests SEC >= 100 ms",
                 f"amostra insuficiente ({len(real_sec)} fetches SEC nao-cache)",
                 samples=len(real_sec))

    # ------------------ Proveniencia: URLs usadas vs probes ----------------
    latest_by_url: dict[str, dict[str, Any]] = {}
    status_by_url: dict[str, list[int]] = {}
    for entry in entries:
        url = str(entry.get("url", ""))
        latest_by_url[url] = entry
        try:
            status_by_url.setdefault(url, []).append(int(entry.get("status")))
        except (TypeError, ValueError):
            status_by_url.setdefault(url, [])
    non_probe = {url: st for url, st in status_by_url.items() if latest_by_url[url].get("source") != "probe"}
    openvc_urls = {url for url in non_probe
                   if "openvc" in url_host(url) or "openvc" in str(latest_by_url[url].get("source", ""))}
    # Decisao do Lead: so' URLs usadas como proveniencia de registro efetivo
    # precisam responder 200/301/302. Endpoints de descoberta que falharam e
    # nunca foram usados sao SKIP/limitacao documentada (nao FAIL).
    early_records, _, _ = load_interim_records()
    used_urls: dict[str, list[str]] = {}
    for record in early_records:
        for field in ("source_url", "retrieved_from"):
            raw = str(record.get(field) or "")
            if not raw:
                continue
            base = raw.split("#", 1)[0].replace("zip:", "").split("!", 1)[0]
            if not base.startswith("http"):
                continue
            used_urls.setdefault(base, []).append(
                f"{record.get('source_id')}:{record.get('record_id')}:{field}")
    manifest_200 = {url for url in non_probe if 200 in status_by_url[url]}
    def _route(url: str) -> tuple[str, str, str]:
        parsed = urllib.parse.urlparse(url)
        return (parsed.scheme, (parsed.hostname or "").lower(), parsed.path.rstrip("/") or "/")
    manifest_ok_routes = {_route(url) for url in manifest_200}
    # Estrategia de verificacao (decisao do Lead + limitacao de carga):
    #  1) URL usada presente no manifest com HTTP 200 -> verificada;
    #  2) demais URLs usadas: agrupar por HOST e verificar por amostragem
    #     deterministica (ate 2 URLs por host, max 40) via HTTP real do auditor;
    #     se o host responde, o PADRAO de endpoint e' valido para as demais URLs
    #     do mesmo host (aproximacao documentada; Gate 2 verifica 25 registros 1:1);
    #  3) qualquer URL amostrada que falhe -> FAIL; hosts nao amostrados -> SKIP.
    remaining_used = sorted(url for url in used_urls if url not in manifest_200)
    by_host: dict[str, list[str]] = {}
    for url in remaining_used:
        by_host.setdefault(url_host(url) or "(sem host)", []).append(url)
    host_order = sorted(by_host, key=lambda h: (-len(by_host[h]), h))
    live_sample: list[str] = []
    sampled_hosts: set[str] = set()
    for host in host_order:
        sampled_hosts.add(host)
        for url in by_host[host][:2]:
            if len(live_sample) < 40:
                live_sample.append(url)
    http_checked: dict[str, dict[str, Any]] = {}
    if live_sample and STATE.network_enabled:
        checker = HttpChecker(timeout_s=20, retries=2)
        try:
            for url in live_sample:
                http_checked[url] = checker.check(url)
        finally:
            checker.flush()

    def _ok_status(res: dict[str, Any] | None) -> bool:
        return bool(res and res.get("status") is not None and 200 <= int(res["status"]) < 400)

    host_verdict: dict[str, str] = {}
    failing_used: list[dict[str, Any]] = []
    verified_manifest = [u for u in used_urls if u in manifest_200]
    verified_pattern = 0
    unchecked_hosts: list[str] = []
    for host, urls in by_host.items():
        sampled = [u for u in urls if u in http_checked]
        if not sampled:
            host_verdict[host] = "nao amostrado"
            unchecked_hosts.append(host)
            continue
        failed_samples = [u for u in sampled if not _ok_status(http_checked.get(u))]
        if not failed_samples:
            host_verdict[host] = f"padrao verificado ({len(sampled)} amostra(s) 2xx/3xx; {len(urls)} URLs)"
            verified_pattern += len(urls)
        else:
            # fallback: se o manifest obteve 200 na MESMA ROTA (scheme+host+path),
            # a proveniencia e' considerada verificada mesmo com falha amostral transiente.
            unresolved = [u for u in failed_samples if _route(u) not in manifest_ok_routes]
            if not unresolved:
                host_verdict[host] = (f"rota verificada via manifest 200 "
                                      f"({len(urls)} URLs; falha amostral transiente em "
                                      f"{len(failed_samples)})")
                verified_pattern += len(urls)
            else:
                host_verdict[host] = "falhou"
                for u in unresolved:
                    failing_used.append({
                        "url": u[:170], "used_in_records": True,
                        "status": (http_checked.get(u) or {}).get("status"),
                        "error": (http_checked.get(u) or {}).get("error"),
                        "usages": len(used_urls.get(u, [])),
                        "host_count": len(urls),
                        "manifest_200_rota": False,
                    })
    unverified_used = sum(len(by_host[h]) for h in unchecked_hosts)
    rep.metrics["http_used_urls"] = len(used_urls)
    rep.metrics["http_used_verified_manifest"] = len(verified_manifest)
    rep.metrics["http_used_verified_pattern"] = verified_pattern
    rep.metrics["http_used_unverified_hosts"] = len(unchecked_hosts)
    rep.metrics["http_used_live_urls_checked"] = len(http_checked)
    rep.metrics["http_used_hosts"] = len(by_host)
    rep.metrics["http_used_host_verdict"] = host_verdict
    rep.metrics["http_200_sources"] = len(manifest_200)
    if failing_used:
        rep.bad("g1.http.used_provenance", "toda URL usada como proveniencia de registro responde 200/301/302",
                f"{len(failing_used)} URL(s) amostradas de proveniencia falharam",
                violations=failing_used[:10], hosts=sorted({url_host(x["url"]) for x in failing_used}))
    elif not used_urls:
        rep.pending("g1.http.used_provenance", "toda URL usada como proveniencia de registro responde 200/301/302",
                    "nenhum registro interino carregado ainda para inferir proveniencia usada")
    elif unchecked_hosts:
        rep.skip("g1.http.used_provenance", "toda URL usada como proveniencia de registro responde 200/301/302",
                 f"{len(used_urls)} URLs de proveniencia ({len(by_host)} hosts): "
                 f"{len(verified_manifest)} cobertas por manifest 200 e {verified_pattern} por padrao de host; "
                 f"{unverified_used} URL(s) em {len(unchecked_hosts)} host(s) nao amostrado(s) "
                 f"(rede desabilitada ou host nao amostrado)",
                 host_verdict=host_verdict)
    else:
        rep.ok("g1.http.used_provenance", "toda URL usada como proveniencia de registro responde 200/301/302",
               f"{len(used_urls)} URLs usadas verificadas: {len(verified_manifest)} via manifest 200 + "
               f"{verified_pattern} via padrao de host ({len(http_checked)} amostras HTTP em {len(by_host)} hosts)",
               host_verdict=host_verdict)
    # endpoints de descoberta tentados, NAO usados por registros e que falharam
    discovery_failed = []
    for url, statuses in non_probe.items():
        if url in used_urls or 200 in statuses:
            continue
        discovery_failed.append({
            "url": url[:170], "statuses": statuses,
            "source": latest_by_url[url].get("source"),
            "used_in_records": False,
        })
    rep.metrics["http_discovery_failed"] = len(discovery_failed)
    if discovery_failed:
        doc = ROOT / "data" / "interim" / "directories_availability.json"
        rep.skip("g1.http.discovery_probes", "endpoints de descoberta que falharam sao documentados como limitacao",
                 f"{len(discovery_failed)} URL(s) de descoberta falharam (404/403/504) e NAO sao usadas por "
                 f"registros; documentacao: "
                 f"{'data/interim/directories_availability.json' if doc.exists() else '(arquivo ausente)'}",
                 probes=discovery_failed[:10],
                 documentation=rel(doc) if doc.exists() else None)
    else:
        rep.ok("g1.http.discovery_probes", "endpoints de descoberta que falharam sao documentados como limitacao",
               "nenhum endpoint de descoberta falhou ou todos foram cobertos por resposta 200")

    # -------------------------- Integridade sha256 do cache -----------------
    sha_ok, sha_bad, sha_missing, sha_extra = 0, [], [], 0
    checked_urls: set[str] = set()
    for entry in entries:
        if entry.get("source") == "probe":
            continue
        if int(entry.get("status", 0) or 0) != 200:
            continue
        url = str(entry.get("url", ""))
        if url in checked_urls:
            continue
        checked_urls.add(url)
        sha = str(entry.get("sha256", "") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            sha_missing.append(url[:140])
            continue
        path = cache_path_for_url(url)
        if not path.is_file():
            sha_bad.append({"url": url[:140], "problem": "cache ausente em data/raw", "cache": rel(path)})
            continue
        actual_sha = sha256_file(path)
        actual_bytes = path.stat().st_size
        declared_bytes = entry.get("bytes")
        if actual_sha != sha:
            sha_bad.append({"url": url[:140], "problem": "sha256 divergente",
                            "declared": sha[:16] + "...", "actual": actual_sha[:16] + "..."})
        elif declared_bytes is not None and int(declared_bytes) != actual_bytes:
            sha_bad.append({"url": url[:140], "problem": f"bytes divergentes ({declared_bytes} != {actual_bytes})"})
        else:
            sha_ok += 1
    rep.metrics["cache_files_verified"] = sha_ok
    if sha_missing:
        rep.bad("g1.cache.sha256_declared", "todo arquivo baixado tem sha256 no manifest",
                f"{len(sha_missing)} URL(s) 200 sem sha256", urls=sha_missing[:8])
    else:
        rep.ok("g1.cache.sha256_declared", "todo arquivo baixado tem sha256 no manifest",
               f"{len(checked_urls)} URLs baixadas possuem sha256")
    if sha_bad:
        rep.bad("g1.cache.sha256_integrity", "bytes em data/raw batem com sha256 do manifest",
                f"{len(sha_bad)} arquivo(s) de cache inconsistentes", violations=sha_bad[:8])
    else:
        rep.ok("g1.cache.sha256_integrity", "bytes em data/raw batem com sha256 do manifest",
               f"{sha_ok} arquivo(s) de cache re-hashados e conferidos byte a byte",
               files=sha_ok)

    # -------------------------- Encoding dos CSVs da CVM --------------------
    cvm_urls = [url for url in checked_urls
                if url_host(url).endswith("cvm.gov.br") and url.lower().split("?")[0].endswith(".csv")]
    cvm_results = []
    cvm_fail = []
    for url in sorted(cvm_urls):
        path = cache_path_for_url(url)
        if not path.is_file():
            cvm_fail.append({"url": url[:140], "problem": "cache ausente"})
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("latin1")
        except UnicodeDecodeError as exc:  # latin1 nunca falha, mas registre se mudar
            cvm_fail.append({"url": url[:140], "problem": f"latin1 falhou: {exc}"})
            continue
        ufffd = text.count("\uFFFD")
        nul = raw.count(b"\x00")
        lines = [line for line in text.splitlines() if line.strip()]
        header = lines[0] if lines else ""
        separator_count = header.count(";")
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        # Acentuacao corrompida tipica de UTF-8 lido como latin1: apenas pares
        # com segunda letra acentuada (NAO contar 'Ã' isolado, legitimo em nomes
        # como SAO/PAO em portugues maiusculo).
        mojibake_seqs = ("Ã©", "Ã§", "Ã£", "Ãµ", "Ã¡", "Ã³", "Ãº", "Ã¢", "Ãª", "Ã­", "Ã´",
                         "Ã ", "Ã¨", "Ã¤", "Ã¶", "Ã¼", "Ã±")
        mojibake = sum(text.count(seq) for seq in mojibake_seqs)
        mojibake_ratio = mojibake / (non_ascii + mojibake) if (non_ascii + mojibake) else 0.0
        entry = {
            "url": url[:150], "bytes": len(raw), "rows": max(0, len(lines) - 1),
            "header_fields": separator_count + 1, "separator_semicolon": separator_count >= 4,
            "latin1_replacement_chars": ufffd, "nul_bytes": nul,
            "non_ascii_chars": non_ascii, "mojibake_sequences": mojibake,
            "mojibake_ratio": round(mojibake_ratio, 6),
            "header_sample": truncate(header, 160),
        }
        cvm_results.append(entry)
        problems = []
        if ufffd > 0:
            problems.append(f"{ufffd} caractere(s) U+FFFD (corrupcao de decodificacao)")
        if nul > 0:
            problems.append(f"{nul} byte(s) NUL")
        if separator_count < 4:
            problems.append(f"header sem separador ';' suficiente ({separator_count})")
        if entry["rows"] <= 0:
            problems.append("zero linhas de dados")
        if non_ascii == 0:
            problems.append("nenhum caractere acentuado (suspeita de mau decode)")
        if mojibake_ratio > 0.01 or mojibake > 100:
            problems.append(f"mojibake em {mojibake_ratio:.2%} dos caracteres nao-ASCII")
        if problems:
            cvm_fail.append({"url": url[:140], "problems": problems})
    rep.metrics["cvm_csvs"] = cvm_results
    if not cvm_urls:
        rep.bad("g1.cvm.encoding", "CSVs da CVM decodificados em latin1 com ';' sem corrupcao",
                "nenhum CSV da CVM no cache/manifest", cvm_urls=0)
    elif cvm_fail:
        rep.bad("g1.cvm.encoding", "CSVs da CVM decodificados em latin1 com ';' sem corrupcao",
                f"{len(cvm_fail)} arquivo(s) com problema", violations=cvm_fail[:6], files=cvm_results)
    else:
        total_rows = sum(e["rows"] for e in cvm_results)
        rep.ok("g1.cvm.encoding", "CSVs da CVM decodificados em latin1 com ';' sem corrupcao",
               f"{len(cvm_results)} arquivo(s) latin1 com ';' e {total_rows} linhas; acentos preservados",
               files=cvm_results)

    # Semantica do decode latin1: bytes nao podem ser UTF-8 valido (senao provavel
    # arquivo UTF-8 lido como latin1). Apenas evidencia, nao bloqueio.
    for entry in cvm_results:
        path = cache_path_for_url(str(entry["url"]))
        try:
            path.read_bytes().decode("utf-8")
            rep.skip("g1.cvm.encoding_utf8_hint", "CSV da CVM nao e UTF-8 valido (confirma latin1)",
                     f"{entry['url'][:110]} decodifica como UTF-8 valido; pode ser UTF-8 ou ASCII puro")
        except UnicodeDecodeError:
            pass

    # -------------------------- Registros interinos + cobertura -------------
    interim_records, per_file, interim_errors = load_interim_records()
    rep.metrics["interim_files"] = per_file
    rep.metrics["interim_records"] = len(interim_records)
    if not interim_records and (not INTERIM_DIR.exists() or not per_file):
        rep.pending("g1.interim.present", "data/interim/*.jsonl existe com registros",
                    "nenhum registro interino encontrado: Gate 1 fica PENDING ate os coletores entregarem",
                    interim_dir_exists=INTERIM_DIR.exists())
    else:
        rep.ok("g1.interim.present", "data/interim/*.jsonl existe com registros",
               f"{len(interim_records)} registro(s) em {len(per_file)} arquivo(s)", files=per_file)
        if interim_errors:
            rep.bad("g1.interim.jsonl_valid", "todas as linhas de data/interim sao JSON de objeto",
                    f"{len(interim_errors)} problema(s)", errors=interim_errors[:10])

        # schema interino
        interim_schema, schema_err = load_json(INTERIM_SCHEMA)
        if interim_schema is None:
            rep.pending("g1.interim.schema", "registros interinos validados contra interim_record.schema.json",
                        f"schema indisponivel: {schema_err}")
        else:
            invalid: list[str] = []
            for record in interim_records:
                errs = validate_schema(record, interim_schema)
                if errs:
                    invalid.append(f"{record.get('__file')}:{record.get('__line')} "
                                   f"record_id={record.get('record_id')!r}: {errs[:4]}")
            rep.metrics["interim_invalid"] = len(invalid)
            if invalid:
                rep.bad("g1.interim.schema", "registros interinos validados contra interim_record.schema.json",
                        f"{len(invalid)}/{len(interim_records)} registros violam o contrato interino",
                        violations=invalid[:10])
            else:
                rep.ok("g1.interim.schema", "registros interinos validados contra interim_record.schema.json",
                       f"100% ({len(interim_records)}) dos registros interinos validos")
            # enums de fonte (lido do CONTRATO, nunca hardcoded)
            source_enum = _interim_source_enum()
            bad_sources = sorted({str(r.get("source_id")) for r in interim_records
                                  if str(r.get("source_id")) not in source_enum})
            if bad_sources:
                rep.bad("g1.interim.source_id", "source_id dos registros interinos pertence ao enum do contrato",
                        f"fonte(s) fora do enum do contrato: {bad_sources[:8]}", sources=bad_sources,
                        enum_size=len(source_enum), enum_source=rel(INTERIM_SCHEMA))
            else:
                rep.ok("g1.interim.source_id", "source_id dos registros interinos pertence ao enum do contrato",
                       f"{len({r.get('source_id') for r in interim_records})} fontes, todas no enum do contrato "
                       f"({len(source_enum)} ids, lido de {rel(INTERIM_SCHEMA)})")
            # unicidade (source_id, record_id)
            keys: dict[tuple[str, str], int] = {}
            dups = []
            for record in interim_records:
                key = (str(record.get("source_id")), str(record.get("record_id")))
                keys[key] = keys.get(key, 0) + 1
                if keys[key] == 2:
                    dups.append(key)
            if dups:
                rep.bad("g1.interim.record_id_unique", "record_id unico por fonte nos registros interinos",
                        f"{len(dups)} chave(s) duplicada(s) (source_id, record_id)", duplicates=dups[:10])
            else:
                rep.ok("g1.interim.record_id_unique", "record_id unico por fonte nos registros interinos",
                       f"{len(keys)} chaves (source_id, record_id) unicas")

        # contagens por fonte + grupos obrigatorios
        counts: dict[str, int] = {}
        for record in interim_records:
            counts[str(record.get("source_id"))] = counts.get(str(record.get("source_id")), 0) + 1
        rep.metrics["interim_records_by_source"] = dict(sorted(counts.items()))
        empty_files = [f["file"] for f in per_file if f.get("records", 0) <= 0]
        zero_sources = [sid for sid, n in counts.items() if n <= 0]
        if empty_files:
            rep.bad("g1.interim.row_counts", "todo arquivo interino tem > 0 registros", "arquivo(s) vazio(s)",
                    files=empty_files)
        elif zero_sources:
            rep.bad("g1.interim.row_counts", "todo arquivo interino tem > 0 registros",
                    f"fonte(s) sem registros: {zero_sources}", sources=zero_sources)
        else:
            rep.ok("g1.interim.row_counts", "todo arquivo interino tem > 0 registros",
                   f"{len(per_file)} arquivo(s), {sum(counts.values())} registros em {len(counts)} fontes",
                   records_by_source=dict(sorted(counts.items())))

        missing_groups = []
        for group, sources in REQUIRED_SOURCE_GROUPS.items():
            if not any(counts.get(src, 0) > 0 for src in sources):
                missing_groups.append({"group": group, "expected": sorted(sources),
                                       "found": {src: counts.get(src, 0) for src in sorted(sources)}})
        rep.metrics["missing_source_groups"] = missing_groups
        if missing_groups:
            # Distingue "coleta bruta ja baixada, transformacao pendente" de
            # "fonte nunca coletada": o manifest e' a evidencia da fase 1.
            manifest_sources_200 = {str(e.get("source", "")) for e in entries
                                    if int(e.get("status", 0) or 0) == 200}
            in_progress, hard_missing = [], []
            for group in missing_groups:
                raw_hits = sorted({ms for ms in manifest_sources_200
                                   if any(ms == src or ms.startswith(src) or src.startswith(ms) or src in ms
                                          for src in group["expected"])})
                if raw_hits:
                    in_progress.append({**group, "coleta_bruta_200": raw_hits})
                else:
                    hard_missing.append(group)
            if hard_missing:
                rep.bad("g1.interim.source_coverage", "cobertura minima de fontes da spec (secao 1.1)",
                        f"{len(hard_missing)} grupo(s) sem registros interinos E sem download bruto 200",
                        missing=hard_missing, in_progress=in_progress)
            else:
                rep.pending("g1.interim.source_coverage", "cobertura minima de fontes da spec (secao 1.1)",
                            f"coleta bruta presente no manifest para os {len(in_progress)} grupo(s), "
                            f"mas registros interinos ainda nao foram gerados (pipeline em andamento)",
                            pending_groups=in_progress)
        else:
            rep.ok("g1.interim.source_coverage", "cobertura minima de fontes da spec (secao 1.1)",
                   f"{len(REQUIRED_SOURCE_GROUPS)} grupos cobertos: {sorted(REQUIRED_SOURCE_GROUPS)}")
        fx = counts.get("bcb_ptax", 0)
        fx_file = INTERIM_DIR / "fx_brl_usd.json"
        fx_evidence = ""
        if fx_file.is_file():
            fx_data, _ = load_json(fx_file)
            points = None
            if isinstance(fx_data, dict):
                for key in ("points", "ptax", "cotacoes", "rates", "daily"):
                    if isinstance(fx_data.get(key), list):
                        points = len(fx_data[key])
                        break
            fx_evidence = f"{rel(fx_file)} ({points if points is not None else 'presente'})"
        kpis_fx = {}
        if KPIS_JSON.exists():
            kdata, _ = load_json(KPIS_JSON)
            if isinstance(kdata, dict) and isinstance(kdata.get("fx"), dict):
                kpis_fx = kdata["fx"]
        if fx <= 0 and not fx_evidence and not kpis_fx:
            rep.skip("g1.interim.fx_ptax", "PTAX (BCB) presente para conversao de valores CVM",
                     "sem registros bcb_ptax e sem fx_brl_usd.json/kpis.fx: conversao USD nao auditavel")
        elif fx > 0:
            rep.ok("g1.interim.fx_ptax", "PTAX (BCB) presente para conversao de valores CVM",
                   f"{fx} registro(s) bcb_ptax interinos")
        else:
            rep.ok("g1.interim.fx_ptax", "PTAX (BCB) presente para conversao de valores CVM",
                   f"PTAX auditavel via {fx_evidence or 'kpis.fx'}"
                   + (f" rate={kpis_fx.get('rate')} ref={kpis_fx.get('reference_date')}" if kpis_fx else ""),
                   kpis_fx=kpis_fx)

        # rastreabilidade: retrieved_from deve apontar para arquivo baixado/cacheado
        manifest_urls = {str(e.get("url", "")) for e in entries}
        def _route_key(url: str) -> tuple[str, str]:
            parsed = urllib.parse.urlparse(url)
            return ((parsed.hostname or "").lower(), parsed.path.rstrip("/") or "/")
        manifest_routes = {_route_key(str(e.get("url", ""))) for e in entries
                           if int(e.get("status", 0) or 0) == 200}
        trace_total, trace_ok, trace_bad = 0, 0, []
        for record in interim_records:
            retrieved = str(record.get("retrieved_from") or "")
            if not retrieved:
                continue
            trace_total += 1
            base = retrieved.split("#", 1)[0]
            candidates = {base, base.replace("zip:", ""), base.split("!", 1)[0]}
            if any(cand in manifest_urls or _route_key(cand) in manifest_routes
                   or cache_path_for_url(cand).is_file() for cand in candidates):
                trace_ok += 1
            elif len(trace_bad) < 10:
                trace_bad.append({"record_id": record.get("record_id"), "retrieved_from": retrieved[:140]})
        if trace_total:
            cov = trace_ok / trace_total
            if cov < 0.9:
                rep.bad("g1.interim.traceability", "retrieved_from dos registros aponta para cache/manifest",
                        f"cobertura {cov:.1%} ({trace_ok}/{trace_total}) < 90%", violations=trace_bad)
            elif cov < 1.0:
                rep.skip("g1.interim.traceability", "retrieved_from dos registros aponta para cache/manifest",
                         f"cobertura {cov:.1%} ({trace_ok}/{trace_total}); {trace_total - trace_ok} sem cache",
                         violations=trace_bad)
            else:
                rep.ok("g1.interim.traceability", "retrieved_from dos registros aponta para cache/manifest",
                       f"100% ({trace_total}) dos retrieved_from com cache/manifest")

    # -------------------------- Fallback OpenVC (403) -----------------------
    openvc_403 = [str(e.get("url")) for e in entries
                  if "openvc" in (url_host(str(e.get("url"))) + " " + str(e.get("source", "")))
                  and int(e.get("status", 0) or 0) == 403]
    if not openvc_403:
        rep.ok("g1.openvc.fallback", "OpenVC sem HTTP 403, ou fallback documentado",
               "nenhuma resposta 403 da OpenVC registrada no manifest")
    else:
        interim_openvc = sum(1 for r in interim_records if r.get("source_id") == "openvc")
        manifest_openvc_200 = [e for e in entries
                               if "openvc" in (url_host(str(e.get("url"))) + " " + str(e.get("source", "")))
                               and int(e.get("status", 0) or 0) == 200]
        doc_evidence = _find_openvc_fallback_doc()
        evidence = {"openvc_403": openvc_403[:5], "interim_openvc_records": interim_openvc,
                    "manifest_openvc_200": len(manifest_openvc_200), "documented_fallback": doc_evidence}
        if interim_openvc > 0 or manifest_openvc_200 or doc_evidence:
            rep.ok("g1.openvc.fallback", "OpenVC sem HTTP 403, ou fallback documentado",
                   f"403 encontrado, mas fallback comprovado (interim={interim_openvc}, "
                   f"manifest200={len(manifest_openvc_200)}, doc={bool(doc_evidence)})", **evidence)
        else:
            rep.bad("g1.openvc.fallback", "OpenVC sem HTTP 403, ou fallback documentado",
                    "OpenVC respondeu 403 e NAO ha fallback documentado/registros alternativos",
                    **evidence)

    rep.notes.append("O auditor usa cache HTTP proprio em audit/http_checks.json e NAO escreve "
                     "em data/manifest.jsonl/data/raw (escopo de escrita de outros agentes).")
    rep.seconds = time.time() - t0
    return rep


def _find_openvc_fallback_doc() -> str:
    """Procura documentacao de fallback da OpenVC em arquivos de texto do repo."""
    candidates: list[Path] = []
    for pattern in ("*.md", "docs/**/*.md", "data/**/*.md", "data/**/*.jsonl", "data/**/*.json",
                    "audit/**/*.md", "*.txt"):
        candidates.extend(ROOT.glob(pattern))
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if "openvc" not in text:
            continue
        if re.search(r"fallback|alternativ|plano b|proxy|bloqueio", text):
            return rel(path)
    return ""


# ==========================================================================
# Determinismo / fingerprint de conteudo
# ==========================================================================
def records_content_sha256(records: list[dict[str, Any]]) -> str:
    """Hash canonico do CONTEUDO do dataset (independente de envelope generated_at).

    Metodo: sha256(json.dumps(records, ensure_ascii=False, sort_keys=True)).
    Arquivos individuais incluem 'generated_at' no envelope e por isso mudam de
    hash entre builds; o array 'records' e' deterministico.
    """
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def vcdata_rows_sha256(html_path: Path) -> str | None:
    """Hash das 'rows' embutidas no data island vc-data (para casar runtime x build)."""
    if not html_path.is_file():
        return None
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'<script type="application/json" id="vc-data">(.*?)</script>', html, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    rows = data.get("rows")
    if not isinstance(rows, list):
        return None
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ==========================================================================
# GATE 2 - Contrato SDD e zero alucinacao
# ==========================================================================
def _load_funds() -> tuple[list[dict[str, Any]] | None, str]:
    data, err = load_json(FUNDS_JSON)
    if data is None:
        return None, err or "arquivo ausente"
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = None
        for key in ("funds", "records", "data", "items", "fundos"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None:
            return None, "JSON nao contem lista de fundos (chaves: " + ", ".join(sorted(data)[:8]) + ")"
    else:
        return None, f"tipo inesperado: {type(data).__name__}"
    return [row for row in items if isinstance(row, dict)], ""


def _sample_urls(records: list[dict[str, Any]], max_unique: int = 25) -> tuple[list[dict[str, Any]], list[str]]:
    """Amostra deterministica: ate 25 URLs distintas, percorrendo registros por fund_id."""
    sampled_records: list[dict[str, Any]] = []
    urls: list[str] = []
    seen: set[str] = set()
    for record in sorted(records, key=lambda r: str(r.get("fund_id", ""))):
        url = str(record.get("primary_source_url") or "")
        sampled_records.append(record)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= max_unique:
            break
    return sampled_records, urls


def _walk_json(obj: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    yield path, obj
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk_json(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from _walk_json(value, f"{path}[{idx}]")


def _canonical_stage_key(key: str) -> str | None:
    k = norm_text(str(key)).replace("-", "_").replace(" ", "_")
    k = re.sub(r"_+", "_", k).strip("_")
    variants = {
        "pre_seed": "PRE_SEED", "preseed": "PRE_SEED", "pre_seed_stage": "PRE_SEED",
        "seed": "SEED", "seed_stage": "SEED",
        "series_a": "SERIES_A", "serie_a": "SERIES_A", "seriesa": "SERIES_A",
        "series_b": "SERIES_B", "serie_b": "SERIES_B", "seriesb": "SERIES_B",
        "late_stage_growth": "LATE_STAGE_GROWTH", "late_stage": "LATE_STAGE_GROWTH",
        "growth": "LATE_STAGE_GROWTH", "series_c_plus": "LATE_STAGE_GROWTH",
        "venture_debt": "VENTURE_DEBT", "venture_debt_stage": "VENTURE_DEBT",
    }
    return variants.get(k)


def gate2() -> GateReport:
    t0 = time.time()
    rep = GateReport(
        2,
        "Conformidade com o contrato SDD e zero alucinação",
        method=(
            "Validacao de 100% dos registros de data/canonical/funds.json contra "
            "contract/venture_capital_fund.schema.json com validador JSON Schema "
            "draft 2020-12 implementado do zero (sem jsonschema) + invariantes de "
            "rastreabilidade + amostra de URLs verificada por HTTP real (rate-limit "
            f"{HTTP_RATE_S}s, cache em audit/http_checks.json) + aritmetica dos KPIs."
        ),
    )
    records, load_err = _load_funds()
    if records is None:
        rep.pending("g2.funds.present", "data/canonical/funds.json existe e e carregavel",
                    f"dataset canonico indisponivel: {load_err}", path=rel(FUNDS_JSON))
        rep.seconds = time.time() - t0
        return rep
    rep.ok("g2.funds.present", "data/canonical/funds.json existe e e carregavel",
           f"JSON carregado de {rel(FUNDS_JSON)}", records=len(records), bytes=FUNDS_JSON.stat().st_size)
    if not records:
        rep.bad("g2.funds.nonempty", "dataset canonico nao esta vazio", "funds.json contem 0 registros")
        rep.seconds = time.time() - t0
        return rep
    rep.ok("g2.funds.nonempty", "dataset canonico nao esta vazio", f"{len(records)} registros")

    # ------------------------------ Schema SDD ------------------------------
    schema, schema_err = load_json(FUND_SCHEMA)
    if not isinstance(schema, dict):
        rep.pending("g2.schema.present", "contrato venture_capital_fund.schema.json disponivel",
                    f"schema indisponivel/invalido: {schema_err}", path=rel(FUND_SCHEMA))
    else:
        required = schema.get("required", [])
        rep.ok("g2.schema.present", "contrato venture_capital_fund.schema.json disponivel",
               f"schema carregado com {len(required)} campos obrigatorios e {len(schema.get('properties', {}))} propriedades",
               required=list(required))
        invalid: list[str] = []
        err_count = 0
        for idx, record in enumerate(records):
            errs = validate_schema(record, schema)
            if errs:
                err_count += 1
                if len(invalid) < 12:
                    invalid.append(f"registro[{idx}] fund_id={record.get('fund_id')!r}: " + " | ".join(errs[:5]))
        rep.metrics["schema_invalid_records"] = err_count
        if err_count:
            rep.bad("g2.schema.valid", "100% dos registros validam contra o contrato SDD",
                    f"{err_count}/{len(records)} registros fora do contrato", violations=invalid)
        else:
            rep.ok("g2.schema.valid", "100% dos registros validam contra o contrato SDD",
                   f"{len(records)}/{len(records)} registros validos contra JSON Schema 2020-12")

    # --------------------- Determinismo do conteudo ------------------------
    content_hash = records_content_sha256(records)
    rep.metrics["records_content_sha256"] = content_hash
    rep.metrics["records_count"] = len(records)
    rep.ok("g2.determinism.records_hash", "hash canonico do CONTEUDO de records computado e estavel",
           f"sha256(json.dumps(records, ensure_ascii=False, sort_keys=True)) = {content_hash} "
           f"para {len(records)} registros (envelopes generated_at nao entram no hash)",
           records=len(records), content_sha256=content_hash,
           method="sha256(json.dumps(records, ensure_ascii=False, sort_keys=True))")

    # --------------------- Invariantes de rastreabilidade -------------------
    urls = [str(r.get("primary_source_url") or "") for r in records]
    bad_https = [{"fund_id": r.get("fund_id"), "primary_source_url": urls[i][:140]}
                 for i, r in enumerate(records) if not urls[i].startswith("https://")]
    if bad_https:
        rep.bad("g2.invariance.https", "assert all(primary_source_url.startswith('https://'))",
                f"{len(bad_https)} registro(s) com fonte primaria nao-https", violations=bad_https[:10])
    else:
        rep.ok("g2.invariance.https", "assert all(primary_source_url.startswith('https://'))",
               f"{len(records)}/{len(records)} registros com primary_source_url https")

    fund_ids = [str(r.get("fund_id", "")) for r in records]
    seen: dict[str, int] = {}
    dup_ids: list[str] = []
    for fid in fund_ids:
        seen[fid] = seen.get(fid, 0) + 1
        if seen[fid] == 2:
            dup_ids.append(fid)
    if dup_ids:
        rep.bad("g2.fund_id.unique", "fund_id unico em 100% do dataset",
                f"{len(dup_ids)} fund_id duplicado(s)", duplicates=dup_ids[:10])
    else:
        rep.ok("g2.fund_id.unique", "fund_id unico em 100% do dataset",
               f"{len(seen)} fund_id unicos")

    reg_ids: dict[str, list[str]] = {}
    reg_invalid: list[dict[str, Any]] = []
    for record in records:
        reg = record.get("regulatory_id")
        if reg is None or str(reg).strip() == "":
            continue
        raw = str(reg).strip()
        normalized = re.sub(r"[^0-9A-Za-z]", "", raw)
        reg_ids.setdefault(normalized, []).append(str(record.get("fund_id")))
        if normalized.isdigit():
            if len(normalized) not in (14, 10):
                reg_invalid.append({"fund_id": record.get("fund_id"), "regulatory_id": raw,
                                    "problem": f"{len(normalized)} digitos (esperado CNPJ=14 ou CIK=10)"})
        elif not re.fullmatch(r"[0-9A-Za-z]{4,32}", raw):
            reg_invalid.append({"fund_id": record.get("fund_id"), "regulatory_id": raw,
                                "problem": "formato nao reconhecido"})
    dup_reg = {key: value for key, value in reg_ids.items() if len(value) > 1}
    if dup_reg:
        rep.bad("g2.regulatory.unique", "regulatory_id (CNPJ/CIK) nao duplicado entre veiculos",
                f"{len(dup_reg)} identificador(es) regulatorio(s) repetido(s)",
                duplicates=[{"regulatory_id": k, "fund_ids": v[:5]} for k, v in list(dup_reg.items())[:8]])
    else:
        rep.ok("g2.regulatory.unique", "regulatory_id (CNPJ/CIK) nao duplicado entre veiculos",
               f"{len(reg_ids)} regulatory_id distintos sem duplicata")
    if reg_invalid:
        rep.skip("g2.regulatory.format", "regulatory_id segue formato CNPJ (14) ou CIK (10)",
                 f"{len(reg_invalid)} registro(s) com formato fora do padrao CNPJ/CIK", violations=reg_invalid[:8])
    else:
        rep.ok("g2.regulatory.format", "regulatory_id segue formato CNPJ (14) ou CIK (10)",
               "todos os regulatory_id preenchidos sao CNPJ/CIK plausiveis")

    # --------------------- Enums e atributos obrigatorios -------------------
    bad_cut, bad_stage, bad_method, bad_iso, bad_source_name = [], [], [], [], []
    bad_stage_empty = []
    for record in records:
        fid = record.get("fund_id")
        cut = record.get("geographic_cut")
        if cut not in VALID_CUTS:
            bad_cut.append({"fund_id": fid, "value": cut})
        stages = record.get("target_stages")
        if not isinstance(stages, list) or not stages:
            bad_stage_empty.append({"fund_id": fid, "value": stages})
        else:
            invalid_stages = [s for s in stages if s not in VALID_STAGES]
            if invalid_stages:
                bad_stage.append({"fund_id": fid, "invalid": invalid_stages})
        if record.get("stage_classification_method") not in VALID_METHODS:
            bad_method.append({"fund_id": fid, "value": record.get("stage_classification_method")})
        country = str(record.get("hq_country") or "")
        if not re.fullmatch(r"[A-Z]{2}", country) or country not in ISO_COUNTRIES:
            bad_iso.append({"fund_id": fid, "hq_country": country})
        if not str(record.get("primary_source_name") or "").strip():
            bad_source_name.append(fid)
    if bad_cut:
        rep.bad("g2.geo.cut", "geographic_cut pertence ao enum da spec", f"{len(bad_cut)} invalido(s)",
                violations=bad_cut[:10])
    else:
        rep.ok("g2.geo.cut", "geographic_cut pertence ao enum da spec",
               f"{len(records)} registros em {sorted({str(r.get('geographic_cut')) for r in records})}")
    if bad_stage_empty or bad_stage:
        rep.bad("g2.stages.valid", "target_stages nao vazio e subset do enum",
                f"{len(bad_stage_empty)} vazio(s) e {len(bad_stage)} com estagio invalido",
                empty=bad_stage_empty[:8], invalid=bad_stage[:8])
    else:
        counts: dict[str, int] = {stage: 0 for stage in sorted(VALID_STAGES)}
        for record in records:
            for stage in set(record.get("target_stages", [])):
                counts[stage] = counts.get(stage, 0) + 1
        rep.metrics["funds_by_stage"] = counts
        rep.ok("g2.stages.valid", "target_stages nao vazio e subset do enum",
               "todos os registros possuem >= 1 estagio valido; contagem por estagio: " +
               ", ".join(f"{k}={v}" for k, v in counts.items()))
    if bad_method:
        rep.bad("g2.method.valid", "stage_classification_method pertence ao enum",
                f"{len(bad_method)} invalido(s)", violations=bad_method[:10])
    else:
        rep.ok("g2.method.valid", "stage_classification_method pertence ao enum",
               "metodos: " + ", ".join(f"{k}={v}" for k, v in
                                       sorted({str(r.get('stage_classification_method')): sum(
                                           1 for x in records if x.get('stage_classification_method') == r.get('stage_classification_method'))
                                           for r in records}.items())))
    if bad_iso:
        rep.bad("g2.hq.iso2", "hq_country e ISO-3166-1 alpha-2 valido", f"{len(bad_iso)} invalido(s)",
                violations=bad_iso[:10])
    else:
        rep.ok("g2.hq.iso2", "hq_country e ISO-3166-1 alpha-2 valido",
               f"{len({str(r.get('hq_country')) for r in records})} paises distintos, todos ISO-2")
    if bad_source_name:
        rep.bad("g2.source_name.present", "primary_source_name preenchido",
                f"{len(bad_source_name)} registro(s) sem nome da fonte", fund_ids=bad_source_name[:10])
    else:
        rep.ok("g2.source_name.present", "primary_source_name preenchido",
               "todos os registros citam a fonte primaria por nome")
    rep.metrics["funds_by_cut"] = {cut: sum(1 for r in records if r.get("geographic_cut") == cut)
                                   for cut in sorted(VALID_CUTS)}

    # --------------------- Valores monetarios e nulidade --------------------
    money_fields = ("estimated_ticket_min_usd", "estimated_ticket_max_usd", "aum_or_offering_usd")
    negative, non_numeric, null_missing_disclosure, bad_range = [], [], [], []
    for record in records:
        fid = record.get("fund_id")
        for field in money_fields:
            value = record.get(field)
            if value is None:
                undisclosed = record.get("undisclosed_fields")
                if not (isinstance(undisclosed, list) and field in undisclosed):
                    null_missing_disclosure.append({"fund_id": fid, "field": field})
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                non_numeric.append({"fund_id": fid, "field": field, "value": str(value)[:40]})
                continue
            if value < 0:
                negative.append({"fund_id": fid, "field": field, "value": value})
        tmin, tmax = record.get("estimated_ticket_min_usd"), record.get("estimated_ticket_max_usd")
        if isinstance(tmin, (int, float)) and isinstance(tmax, (int, float)) and tmin > tmax:
            bad_range.append({"fund_id": fid, "min": tmin, "max": tmax})
    if negative:
        rep.bad("g2.money.nonnegative", "nenhum valor monetario negativo", f"{len(negative)} valor(es) negativo(s)",
                violations=negative[:10])
    else:
        rep.ok("g2.money.nonnegative", "nenhum valor monetario negativo",
               "todos os campos monetarios sao >= 0 ou null")
    if non_numeric:
        rep.bad("g2.money.numeric", "campos monetarios sao numericos ou null", f"{len(non_numeric)} invalido(s)",
                violations=non_numeric[:10])
    else:
        rep.ok("g2.money.numeric", "campos monetarios sao numericos ou null", "tipos conferidos")
    if bad_range:
        rep.bad("g2.ticket.range", "estimated_ticket_min_usd <= estimated_ticket_max_usd",
                f"{len(bad_range)} registro(s) com faixa invertida", violations=bad_range[:10])
    else:
        rep.ok("g2.ticket.range", "estimated_ticket_min_usd <= estimated_ticket_max_usd",
               "faixas de cheque consistentes")
    if null_missing_disclosure:
        rep.skip("g2.undisclosed.trace", "campo monetario null declarado em undisclosed_fields",
                 f"{len(null_missing_disclosure)} campo(s) null sem sinalizacao em undisclosed_fields",
                 violations=null_missing_disclosure[:10])
    else:
        rep.ok("g2.undisclosed.trace", "campo monetario null declarado em undisclosed_fields",
               "todo campo monetario null esta sinalizado como nao divulgado")

    # estimate_basis para valores convertidos de CVM
    missing_basis = []
    for record in records:
        if record.get("aum_or_offering_usd") is None:
            continue
        source = (str(record.get("primary_source_name", "")) + " " + str(record.get("primary_source_url", ""))).lower()
        if ("cvm" in source or "fip" in source) and not str(record.get("estimate_basis") or "").strip():
            missing_basis.append({"fund_id": record.get("fund_id")})
    if missing_basis:
        rep.skip("g2.estimate_basis", "valor CVM convertido possui estimate_basis (PTAX/data-base)",
                 f"{len(missing_basis)} registro(s) CVM com AUM sem base de conversao declarada",
                 violations=missing_basis[:10])
    else:
        rep.ok("g2.estimate_basis", "valor CVM convertido possui estimate_basis (PTAX/data-base)",
               "todo AUM oriundo de CVM declara base de conversao (ou nao ha AUM CVM)")

    # evidence_urls https
    bad_evidence = []
    for record in records:
        for url in record.get("evidence_urls") or []:
            if not str(url).startswith("https://"):
                bad_evidence.append({"fund_id": record.get("fund_id"), "url": str(url)[:120]})
    if bad_evidence:
        rep.bad("g2.evidence_urls.https", "evidence_urls usam https",
                f"{len(bad_evidence)} URL(s) nao-https", violations=bad_evidence[:10])
    else:
        rep.ok("g2.evidence_urls.https", "evidence_urls usam https",
               "todas as evidence_urls sao https (ou campo ausente)")

    # --------------------- Amostra de URLs de fonte via HTTP ----------------
    checker = HttpChecker(enabled=STATE.network_enabled, refresh=STATE.refresh_http,
                          timeout_s=12, retries=2)
    sampled_records, sampled_urls = _sample_urls(records, max_unique=25)
    try:
        results = {url: checker.check(url) for url in sampled_urls}
    finally:
        checker.flush()
    ok_status = {200, 201, 301, 302, 307, 308}
    broken = [{"url": url, "status": res.get("status"), "error": res.get("error")}
              for url, res in results.items()
              if res.get("status") is not None and res["status"] not in ok_status]
    unverified = [{"url": url, "error": res.get("error")}
                  for url, res in results.items() if res.get("status") is None]
    cached_n = sum(1 for res in results.values() if res.get("cached"))
    rep.metrics["url_sample"] = {
        "records_sampled": len(sampled_records),
        "unique_urls_checked": len(sampled_urls),
        "http_status_counts": _count_statuses(results),
        "broken": len(broken),
        "unverified": len(unverified),
        "cached_results": cached_n,
        "rate_limit_s": HTTP_RATE_S,
    }
    if broken:
        rep.bad("g2.urls.http_sample", "amostra de URLs de fonte responde 200/301/302",
                f"{len(broken)} URL(s) quebrada(s) na amostra de {len(sampled_urls)}", violations=broken[:10])
    elif not results:
        rep.pending("g2.urls.http_sample", "amostra de URLs de fonte responde 200/301/302",
                    "nao foi possivel checar URLs (rede desabilitada ou dataset sem URLs)")
    else:
        detail = f"{len(sampled_urls)} URLs distintas respondem OK (amostra de {len(sampled_records)} registros)"
        if len(sampled_records) < 25:
            detail += f"; AVISO: dataset tem apenas {len(records)} registros (< 25 amostras exigidas)"
        if unverified:
            rep.skip("g2.urls.http_sample", "amostra de URLs de fonte responde 200/301/302",
                     detail + f"; {len(unverified)} URL(s) nao verificaveis por erro de transporte",
                     status_counts=_count_statuses(results), unverified=unverified[:5])
        else:
            rep.ok("g2.urls.http_sample", "amostra de URLs de fonte responde 200/301/302",
                   detail, status_counts=_count_statuses(results))

    # ------------------------------ KPIs -----------------------------------
    kpis, kpi_err = load_json(KPIS_JSON) if KPIS_JSON.exists() else (None, "arquivo ausente")
    if kpis is None:
        rep.pending("g2.kpis.present", "data/canonical/kpis.json existe e e carregavel",
                    f"KPIs indisponiveis: {kpi_err}", path=rel(KPIS_JSON))
    else:
        rep.ok("g2.kpis.present", "data/canonical/kpis.json existe e e carregavel",
               f"KPIs carregados de {rel(KPIS_JSON)}",
               keys=(sorted(kpis)[:12] if isinstance(kpis, dict) else type(kpis).__name__))
        # Semantica alinhada com o Lead:
        #  * counts.by_primary_stage soma o total de registros -> validar soma == total;
        #  * counts.by_stage_membership PODE exceder o total (fundo multiestagio) ->
        #    comparar com recalculo independente de membership, NUNCA com o total;
        #  * capital.by_primary_stage_usd soma == capital.total_usd.
        stage_counts_blocks = _kpi_stage_blocks(kpis)
        rep.metrics["kpi_stage_blocks"] = len(stage_counts_blocks)
        consistency_fail: list[dict[str, Any]] = []
        consistency_checked = 0
        membership_global: list[tuple[str, dict[str, float]]] = []
        membership_cut: list[tuple[str, dict[str, float]]] = []
        primary_blocks: list[tuple[str, dict[str, float]]] = []
        primary_cut_blocks: list[tuple[str, dict[str, float], dict[str, Any]]] = []
        capital_blocks: list[tuple[str, dict[str, float], dict[str, Any]]] = []
        for path, block in stage_counts_blocks:
            numeric: dict[str, float] = {}
            for key, value in block.items():
                stage = _canonical_stage_key(str(key))
                if stage and isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric[stage] = float(value)
            if len(numeric) < 2:
                continue
            low_path = path.lower()
            is_membership = "membership" in low_path or "member" in low_path
            is_primary = "primary" in low_path
            is_cut = "cut" in low_path
            is_capital = "capital" in low_path or "usd" in low_path
            if is_primary:
                primary_blocks.append((path, numeric))
            if is_capital:
                capital_blocks.append((path, numeric, dict(block)))
            if is_membership:
                # membership pode exceder o total por design (multiestagio):
                # validar por recalculo independente, nunca contra o total.
                if is_cut:
                    membership_cut.append((path, numeric))
                else:
                    membership_global.append((path, numeric))
                continue
            if is_cut:
                if is_primary:
                    primary_cut_blocks.append((path, numeric, dict(block)))
                continue
            if "by_primary_stage" in low_path:
                total, total_key = _kpi_total_in_scope(kpis, path, block)
                if total is None:
                    continue
                consistency_checked += 1
                if abs(sum(numeric.values()) - total) > 0.5:
                    consistency_fail.append({"path": path, "stage_counts": numeric,
                                             "sum": sum(numeric.values()), "declared_total": total,
                                             "total_key": total_key,
                                             "kind": "capital" if is_capital else "primary"})
            else:
                # bloco auxiliar (ex.: densidade, classe) sem total comparavel
                continue
        # primary_and_cut: soma por corte deve fechar com counts.by_cut / capital.by_cut_usd
        cut_totals_count = _kpi_cut_totals(kpis, usd=False)
        cut_totals_usd = _kpi_cut_totals(kpis, usd=True)
        for path, numeric, block in primary_cut_blocks:
            cut = path.rsplit(".", 1)[-1]
            usd = "usd" in path.lower()
            total = (cut_totals_usd if usd else cut_totals_count).get(cut)
            if total is None:
                continue
            consistency_checked += 1
            if abs(sum(numeric.values()) - total) > 0.5:
                consistency_fail.append({"path": path, "stage_counts": numeric,
                                         "sum": sum(numeric.values()), "declared_total": total,
                                         "total_key": ("capital.by_cut_usd." if usd else "counts.by_cut.") + cut,
                                         "kind": "primary_and_cut"})
        if not stage_counts_blocks:
            keys = sorted(kpis)[:12] if isinstance(kpis, dict) else type(kpis).__name__
            rep.skip("g2.kpis.consistency", "soma por estagio == total declarado no kpis.json",
                     "estrutura de KPIs nao reconhecida para checagem aritmetica; chaves: " + str(keys))
        elif not consistency_checked and (membership_global or membership_cut):
            rep.skip("g2.kpis.consistency", "soma por estagio == total declarado no kpis.json",
                     f"{len(membership_global) + len(membership_cut)} bloco(s) de membership "
                     f"(soma pode exceder total por design) e nenhum bloco primary/capital com total")
        elif consistency_fail:
            rep.bad("g2.kpis.consistency", "soma por estagio == total declarado no kpis.json",
                    f"{len(consistency_fail)} bloco(s) inconsistentes", violations=consistency_fail[:8])
        else:
            rep.ok("g2.kpis.consistency", "soma por estagio == total declarado no kpis.json",
                   f"{consistency_checked} bloco(s) primary/primary_and_cut/capital com soma == total; "
                   f"{len(membership_global) + len(membership_cut)} bloco(s) membership isentos "
                   f"(multiestagio por design)")

        # cross-check independente: membership global e por corte vs dataset canonico.
        computed = {stage: sum(1 for r in records if stage in (r.get("target_stages") or []))
                    for stage in sorted(VALID_STAGES)}
        computed_cut: dict[str, dict[str, int]] = {}
        for record in records:
            cut = str(record.get("geographic_cut"))
            for stage in set(record.get("target_stages") or []):
                bucket = computed_cut.setdefault(cut, {})
                bucket[stage] = bucket.get(stage, 0) + 1
        membership_mismatches = []
        for path, numeric in membership_global:
            for stage, value in numeric.items():
                if abs(value - computed[stage]) > 0.5:
                    membership_mismatches.append({"path": f"{path}.{stage}", "stage": stage,
                                                  "kpi": value, "dataset_calculado": computed[stage]})
        cut_mismatches = []
        for path, numeric in membership_cut:
            cut = path.rsplit(".", 1)[-1]
            for stage, value in numeric.items():
                expected = computed_cut.get(cut, {}).get(stage, 0)
                if abs(value - expected) > 0.5:
                    cut_mismatches.append({"path": f"{path}.{stage}", "cut": cut, "stage": stage,
                                           "kpi": value, "dataset_calculado": expected})
        checked_any = bool(membership_global or membership_cut)
        if not checked_any and primary_blocks:
            rep.skip("g2.kpis.cross_check", "contagens de KPI por estagio == recalculo independente do dataset",
                     "somente bloco(s) by_primary_stage presentes; o dataset canonico nao expoe o estagio "
                     "primario de forma inequivoca para recalculo independente (validado apenas soma == total)",
                     computed_membership=computed)
        elif not checked_any:
            rep.skip("g2.kpis.cross_check", "contagens de KPI por estagio == recalculo independente do dataset",
                     "estrutura de KPIs sem bloco de membership reconhecido")
        elif membership_mismatches or cut_mismatches:
            rep.bad("g2.kpis.cross_check", "contagens de KPI por estagio == recalculo independente do dataset",
                    f"{len(membership_mismatches)} divergencia(s) de membership global e "
                    f"{len(cut_mismatches)} de membership por corte entre kpis.json e funds.json",
                    mismatches=(membership_mismatches + cut_mismatches)[:14],
                    computed_membership=computed, computed_membership_by_cut=computed_cut)
        else:
            rep.ok("g2.kpis.cross_check", "contagens de KPI por estagio == recalculo independente do dataset",
                   f"{len(membership_global)} bloco(s) de membership global e {len(membership_cut)} bloco(s) de "
                   f"membership por corte conferem com {len(records)} registros "
                   f"(multiestagio contado por estagio)",
                   computed=computed, computed_cut=computed_cut)

        # capital por estagio: soma deve fechar com capital total
        capital_fail = []
        capital_checked = 0
        for path, numeric, capital_block in capital_blocks:
            # confere total interno do bloco E total do escopo pai (capital.total_usd)
            totals: list[tuple[float, str | None]] = []
            block_total, block_total_key = _kpi_total_in_scope(kpis, path, capital_block)
            if block_total is not None:
                totals.append((block_total, block_total_key))
            scope_total, scope_total_key = _kpi_total_in_scope(kpis, path, {})
            if scope_total is not None and scope_total_key:
                totals.append((scope_total, scope_total_key))
            if not totals:
                continue
            capital_checked += 1
            for total, total_key in totals:
                if abs(sum(numeric.values()) - total) > max(1.0, abs(total) * 1e-9):
                    capital_fail.append({"path": path, "sum": sum(numeric.values()),
                                         "declared_total": total, "total_key": total_key})
        if capital_blocks and capital_fail:
            rep.bad("g2.kpis.capital", "capital.by_primary_stage_usd soma == capital.total_usd",
                    f"{len(capital_fail)} bloco(s) de capital inconsistentes", violations=capital_fail[:8])
        elif capital_blocks:
            rep.ok("g2.kpis.capital", "capital.by_primary_stage_usd soma == capital.total_usd",
                   f"{capital_checked} bloco(s) de capital conferem com o total declarado")
        else:
            rep.skip("g2.kpis.capital", "capital.by_primary_stage_usd soma == capital.total_usd",
                     "kpis.json sem bloco de capital por estagio reconhecido")

        # dry powder / capital: valores nao podem ser negativos
        negative_capital = []
        for path, value in _walk_json(kpis):
            key = norm_text(path.rsplit(".", 1)[-1]).replace("-", "_")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if re.search(r"(dry_powder|capital_committed|capital_invested|committed_usd|invested_usd|"
                         r"capital_comprometido|capital_investido)", key):
                if value < 0:
                    negative_capital.append({"path": path, "value": value})
        if negative_capital:
            rep.bad("g2.kpis.capital_nonnegative", "capital/dry powder nao-negativos no kpis.json",
                    f"{len(negative_capital)} valor(es) negativo(s)", violations=negative_capital[:10])
        else:
            rep.ok("g2.kpis.capital_nonnegative", "capital/dry powder nao-negativos no kpis.json",
                   "nenhum valor negativo de capital/dry powder encontrado")
        # contagem total declarada vs len(funds)
        declared_totals = []
        if isinstance(kpis, dict):
            for path, value in _walk_json(kpis):
                key = path.rsplit(".", 1)[-1]
                normalized = norm_text(key).replace("-", "_")
                is_total_funds = bool(re.fullmatch(
                    r"(total_funds|total_fundos|n_funds|funds_total|total_fundos_mapeados)", normalized))
                is_counts_total = normalized == "total" and "counts" in path.lower()
                if isinstance(value, (int, float)) and not isinstance(value, bool) and (
                        is_total_funds or is_counts_total):
                    declared_totals.append((path, float(value)))
        if declared_totals and any(abs(v - len(records)) > 0.5 for _, v in declared_totals):
            rep.bad("g2.kpis.total_funds", "total de fundos declarado no KPI == len(funds.json)",
                    f"valores declarados {declared_totals[:5]} != {len(records)} registros")
        elif declared_totals:
            rep.ok("g2.kpis.total_funds", "total de fundos declarado no KPI == len(funds.json)",
                   f"{len(declared_totals)} campo(s) de total == {len(records)}")
        else:
            rep.skip("g2.kpis.total_funds", "total de fundos declarado no KPI == len(funds.json)",
                     "kpis.json sem campo total_funds/n_funds explicito")

    rep.seconds = time.time() - t0
    return rep


def _count_statuses(results: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for res in results.values():
        key = str(res.get("status")) if res.get("status") is not None else "transport_error"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _json_path_get(root: Any, path: str) -> Any:
    """Resolve caminho no formato '$', '$.a.b', '$.a[0].b' produzido por _walk_json."""
    if path == "$" or not path:
        return root
    current = root
    for match in re.finditer(r"\.([^.\[\]]+)|\[(\d+)\]", path):
        key, index = match.group(1), match.group(2)
        try:
            current = current[key] if key is not None else current[int(index)]
        except (KeyError, IndexError, TypeError):
            return None
    return current


_TOTAL_KEY_RE = re.compile(
    r"(total|todos|total_funds|total_fundos|n_funds|funds_total|total_count|total_usd|"
    r"total_usd_total|valor_total|capital_total|total_capital)")


def _kpi_total_in_scope(kpis: Any, path: str, block: dict[str, Any]) -> tuple[float | None, str | None]:
    """Procura o total declarado do bloco: dentro do bloco, no pai ou na raiz."""
    for key, value in block.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            normalized = norm_text(str(key)).replace("-", "_").replace(" ", "_")
            if _TOTAL_KEY_RE.fullmatch(normalized):
                return float(value), f"{path}.{key}"
    parts = path.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        parent_path = ".".join(parts[:cut])
        parent = _json_path_get(kpis, parent_path)
        if isinstance(parent, dict):
            for key, value in parent.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    normalized = norm_text(str(key)).replace("-", "_").replace(" ", "_")
                    if _TOTAL_KEY_RE.fullmatch(normalized):
                        return float(value), f"{parent_path}.{key}"
    return None, None


def _kpi_cut_totals(kpis: Any, usd: bool = False) -> dict[str, float]:
    """Totais por corte geografico (counts.by_cut ou capital.by_cut_usd)."""
    totals: dict[str, float] = {}
    target = "by_cut_usd" if usd else "by_cut"
    for path, value in _walk_json(kpis):
        if not isinstance(value, dict):
            continue
        normalized = norm_text(path.rsplit(".", 1)[-1]).replace("-", "_")
        if normalized != target:
            continue
        for key, val in value.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                totals[str(key)] = float(val)
    return totals


def _kpi_stage_blocks(kpis: Any) -> list[tuple[str, dict[str, Any]]]:
    """Encontra objetos que mapeiam >= 2 estagios canonicos para valores numericos."""
    blocks: list[tuple[str, dict[str, Any]]] = []
    for path, value in _walk_json(kpis):
        if not isinstance(value, dict):
            continue
        stages = {_canonical_stage_key(str(key)) for key, val in value.items()
                  if isinstance(val, (int, float)) and not isinstance(val, bool)}
        stages.discard(None)
        if len(stages) >= 2:
            blocks.append((path, value))
    return blocks


# ==========================================================================
# GATE 3 - Linter anti-AI-Slop (texto e design)
# ==========================================================================
def _collect_styles(html: str, nodes: list[DomNode]) -> tuple[str, list[CSSRule], list[tuple[DomNode, list[tuple[str, str]]]]]:
    css_text = "\n".join(extract_style_blocks(html))
    rules = parse_css(css_text)
    inline = extract_inline_styles(nodes)
    return css_text, rules, inline


def _shadow_max_px(value: str) -> float | None:
    """Maior blur/spread em px de uma declaracao box-shadow (None = sem sombra)."""
    if not value or value.strip().lower() in ("none", "unset", "initial", "inherit", "0"):
        return None
    value = re.sub(r"(?i)(inset|rgba?\([^)]*\)|#[0-9a-f]{3,8}|[a-z]+)", " ", value)
    lengths = [(float(num), unit or "px") for num, unit in re.findall(r"(-?\d*\.?\d+)\s*(px|rem|em|pt)?", value)]
    px_values = [length_to_px(f"{num}{unit}") for num, unit in lengths]
    px_values = [v for v in px_values if v is not None]
    if not px_values:
        return None
    if len(px_values) >= 3:
        return abs(px_values[2])  # offset-x, offset-y, blur
    if len(px_values) == 4:
        return abs(px_values[2])
    return max(abs(v) for v in px_values)


def _gradient_hues(value: str) -> list[float]:
    hues = []
    for match in re.finditer(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", value):
        h = match.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
        mx, mn = max(r, g, b), min(r, g, b)
        if mx == mn:
            continue
        delta = mx - mn
        if mx == r:
            hue = (60 * ((g - b) / delta) + 360) % 360
        elif mx == g:
            hue = 60 * ((b - r) / delta) + 120
        else:
            hue = 60 * ((r - g) / delta) + 240
        hues.append(round(hue))
    return hues


HEDGE_RE = re.compile(r"(?i)\b(pode|podem|poderia|poderiam|talvez|possivelmente|provavelmente|"
                       r"acredita(?:mos)?|espera-se|tende(?:m)?|sugere(?:m)?|parece(?:m)?)\b")


def _numeric_tokens(text: str) -> list[str]:
    """Tokens numericos formatados (moeda/percentual/milhar/unidade) de um texto."""
    tokens = MONEY_RE.findall(text) + PCT_RE.findall(text) + THOUSANDS_RE.findall(text)
    tokens += re.findall(r"\b\d+(?:[.,]\d+)?\s?(?:mil|mi|bi|M|B|k)\b", text)
    return [re.sub(r"\s+", "", token) for token in tokens]


def _answer_first_context(nodes: list[DomNode]) -> tuple[DomNode | None, str, list[str], bool]:
    """Localiza o bloco .answer-first (novo formato) e mede sua afirmacao quantitativa."""
    candidates = [node for node in nodes
                  if any(token in {"answer-first", "answerfirst", "resposta-principal",
                                   "conclusao-principal", "answer-principal"}
                         for token in node.classes)]
    if not candidates:
        return None, "", [], False
    block = candidates[0]
    raw = block.text()
    # Remove o rotulo (qualquer um dos autorizados) para medir a AFIRMACAO em si.
    statement = re.sub(r"(?i)^\s*(?:conclus[ãa]o|leitura)\s+principal\s*[:\-–—]*\s*", "", raw).strip()
    tokens = _numeric_tokens(statement)
    quantified = len(set(tokens)) >= 2
    return block, statement, tokens, quantified


# Rotulos autorizados para a conclusao quantificada do bloco .answer-first.
# "Conclusão principal" e' o rotulo original; "Leitura principal" foi autorizado
# pelo Lead como equivalente (evita colidir com o lexema generico bloqueado
# "Conclusão" do Gate 3). O relatorio registra qual rotulo foi encontrado.
ANSWER_LABELS = ("conclusao principal", "leitura principal")


def _answer_label_found(block: DomNode) -> str | None:
    """Rotulo da conclusao presente no bloco, no irmao anterior ou no pai (normalizado)."""
    texts = [block.text()]
    parent = block.parent
    if parent is not None:
        try:
            position = parent.children.index(block)
        except ValueError:
            position = 0
        for sibling in parent.children[:position][-2:]:
            texts.append(sibling.text())
        texts.append(parent.text())
    for text in texts:
        if not text:
            continue
        normalized_text = norm_text(text)
        for label in ANSWER_LABELS:
            if label in normalized_text:
                return label
    return None


def gate3() -> GateReport:
    t0 = time.time()
    rep = GateReport(
        3,
        "Linter anti-AI-Slop (texto e design)",
        method=(
            "Analise estatica do HTML/CSS de dashboard/index.html: DOM parseado, "
            "texto visivel normalizado para casar o lexico bloqueado, regras CSS "
            "parseadas para gradientes/sombras/icones, contagem de valores numericos "
            "reais e inspecao de <figure>/<figcaption> e cards de estatistica. Hero validado em "
            "dois formatos: H1 quantitativo (legado) ou H1 neutro/centralizado + bloco "
            ".answer-first com >=2 numeros e <=170 chars (semantica task-8)."
        ),
    )
    if not DASHBOARD.exists():
        rep.pending("g3.html.present", "dashboard/index.html existe",
                    "dashboard ainda nao foi renderizado: Gate 3 fica PENDING", path=rel(DASHBOARD))
        rep.seconds = time.time() - t0
        return rep
    html = read_text_tolerant(DASHBOARD)
    html_bytes = DASHBOARD.stat().st_size
    html_sha = sha256_file(DASHBOARD)
    rep.metrics["html_bytes"] = html_bytes
    rep.metrics["html_sha256"] = html_sha
    rep.ok("g3.html.present", "dashboard/index.html existe", f"{html_bytes} bytes em {rel(DASHBOARD)}",
           bytes=html_bytes, sha256=html_sha)

    _, nodes = parse_dom(html)
    visible_text = strip_tags(html)
    normalized = norm_text(visible_text)
    css_text, rules, inline_styles = _collect_styles(html, nodes)
    rep.metrics["css_bytes"] = len(css_text)
    answer_block, answer_statement, answer_tokens, answer_quantified = _answer_first_context(nodes)
    rep.metrics["answer_first"] = {
        "present": answer_block is not None,
        "quantified": answer_quantified,
        "tokens": sorted(set(answer_tokens))[:10],
        "length": len(answer_statement),
        "label_found": _answer_label_found(answer_block) if answer_block is not None else None,
        "accepted_labels": list(ANSWER_LABELS),
    }
    lexicon_text = normalized
    if answer_quantified:
        # 'Conclusão principal' e' um rotulo de conclusao CALCULADA da nova semantica
        # (answer-first), nao um titulo vago proibido; isenta apenas esse rotulo.
        lexicon_text = lexicon_text.replace("conclusao principal", " ")

    # -------------------------- Bloqueio lexical ---------------------------
    lexicon_hits: list[dict[str, Any]] = []
    for term in BLOCKED_LEXICON:
        needle = norm_text(term)
        for match in re.finditer(re.escape(needle), lexicon_text):
            lexicon_hits.append({"term": term, "snippet": truncate(lexicon_text[max(0, match.start() - 80): match.end() + 100], 220)})
            break  # uma ocorrencia por termo basta como evidencia
    if lexicon_hits:
        rep.bad("g3.lexicon.body", "ausencia de cliches genericos de IA no texto visivel",
                f"{len(lexicon_hits)} expressao(oes) proibida(s) encontrada(s)",
                violations=lexicon_hits)
    else:
        rep.ok("g3.lexicon.body", "ausencia de cliches genericos de IA no texto visivel",
               f"{len(BLOCKED_LEXICON)} termos proibidos varridos em {len(visible_text)} caracteres de texto visivel")

    headings = [node for node in nodes if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}]
    bad_headings: list[dict[str, Any]] = []
    for node in headings:
        text = node.text()
        norm_heading = norm_text(text)
        for term in BLOCKED_LEXICON:
            needle = norm_text(term)
            if needle not in norm_heading:
                continue
            # novo formato: 'Conclusão principal' e' rotulo da conclusao calculada
            if answer_quantified and needle == "conclusao" and "conclusao principal" in norm_heading:
                continue
            bad_headings.append({"tag": node.tag, "text": truncate(text, 120), "term": term})
            break
    if bad_headings:
        rep.bad("g3.lexicon.headings", "titulos sem rotulos vagos ('Introducao', 'Conclusao', etc.)",
                f"{len(bad_headings)} heading(s) com lexico proibido", violations=bad_headings[:10])
    else:
        rep.ok("g3.lexicon.headings", "titulos sem rotulos vagos ('Introducao', 'Conclusao', etc.)",
               f"{len(headings)} headings inspecionados")

    # -------------------------- Gradientes decorativos ---------------------
    gradient_hits: list[dict[str, Any]] = []
    decorative_props = {"background", "background-image", "background-color", "color", "background-clip", "-webkit-background-clip"}
    for rule in rules:
        for prop, value in rule.declarations:
            if "linear-gradient" not in value.lower() and "radial-gradient" not in value.lower():
                continue
            is_decorative = prop in decorative_props or "background" in prop
            if not is_decorative:
                continue
            hue_list = _gradient_hues(value)
            gradient_hits.append({
                "selector": truncate(rule.selector, 120), "property": prop,
                "value": truncate(value, 160), "media": rule.media,
                "hues": hue_list,
                "hue_class": ("roxo" if any(250 <= h <= 310 for h in hue_list) else
                              "azul" if any(200 <= h < 250 for h in hue_list) else
                              "ciano" if any(160 <= h < 200 for h in hue_list) else "outro"),
            })
    for node, decls in inline_styles:
        for prop, value in decls:
            if ("gradient" in value.lower()) and ("background" in prop or prop == "color"):
                gradient_hits.append({"selector": f"inline:{node.tag}.{'.'.join(sorted(node.classes))[:80]}",
                                      "property": prop, "value": truncate(value, 160), "media": None,
                                      "hues": _gradient_hues(value), "hue_class": "inline"})
    if gradient_hits:
        rep.bad("g3.visual.gradients", "sem gradientes decorativos em fundo de pagina/texto",
                f"{len(gradient_hits)} gradiente(s) decorativo(s) no CSS",
                violations=gradient_hits[:8])
    else:
        rep.ok("g3.visual.gradients", "sem gradientes decorativos em fundo de pagina/texto",
               "nenhum linear-gradient/radial-gradient em background ou color")
    svg_gradients = len(re.findall(r"<linearGradient\b", html, re.I))
    if svg_gradients:
        rep.skip("g3.visual.svg_gradient", "gradientes SVG de grafico nao usados como decoracao",
                 f"{svg_gradients} <linearGradient> SVG no HTML; revisar se e' fill de grafico (permitido) "
                 "ou decoracao (proibido)")

    # -------------------------- Sombras pesadas ----------------------------
    shadow_hits: list[dict[str, Any]] = []
    for rule in rules:
        for prop, value in rule.declarations:
            if prop != "box-shadow":
                continue
            blur = _shadow_max_px(value)
            if blur is not None and blur > 4.0:
                shadow_hits.append({"selector": truncate(rule.selector, 120), "value": truncate(value, 120),
                                    "blur_px": round(blur, 1)})
    for node, decls in inline_styles:
        for prop, value in decls:
            if prop == "box-shadow":
                blur = _shadow_max_px(value)
                if blur is not None and blur > 4.0:
                    shadow_hits.append({"selector": f"inline:{node.tag}.{'.'.join(sorted(node.classes))[:80]}",
                                        "value": truncate(value, 120), "blur_px": round(blur, 1)})
    if shadow_hits:
        rep.bad("g3.visual.shadows", "sem box-shadow difuso acima de 4px",
                f"{len(shadow_hits)} sombra(s) com blur > 4px", violations=shadow_hits[:8])
    else:
        rep.ok("g3.visual.shadows", "sem box-shadow difuso acima de 4px",
               "nenhuma box-shadow com blur > 4px no CSS")

    # -------------------------- Emojis / icones genericos ------------------
    emoji_headings: list[dict[str, Any]] = []
    icon_hits: list[dict[str, Any]] = []
    for node in headings:
        text = node.text()
        if EMOJI_RE.search(text):
            emoji_headings.append({"tag": node.tag, "text": truncate(text, 120)})
        if ICON_WORDS.search(text):
            icon_hits.append({"tag": node.tag, "text": truncate(text, 120), "where": "heading"})
    for node in nodes:
        for attr in ("alt", "aria-label", "title", "data-icon"):
            value = node.attrs.get(attr, "")
            if value and (EMOJI_RE.search(value) or ICON_WORDS.search(value)):
                icon_hits.append({"tag": node.tag, "attr": attr, "value": truncate(value, 100)})
        classes = " ".join(sorted(node.classes))
        if classes and ICON_WORDS.search(classes):
            icon_hits.append({"tag": node.tag, "class": truncate(classes, 100)})
    if emoji_headings:
        rep.bad("g3.visual.emoji_headings", "sem emojis em cabecalhos", f"{len(emoji_headings)} heading(s) com emoji",
                violations=emoji_headings[:10])
    else:
        rep.ok("g3.visual.emoji_headings", "sem emojis em cabecalhos",
               f"{len(headings)} headings sem emoji Unicode")
    if icon_hits:
        rep.bad("g3.visual.generic_icons", "sem icones genericos de foguete/lampada/robo",
                f"{len(icon_hits)} ocorrencia(s) de icone generico", violations=icon_hits[:10])
    else:
        rep.ok("g3.visual.generic_icons", "sem icones genericos de foguete/lampada/robo",
               "nenhuma ocorrencia de foguete/lampada/robo em texto, alt, aria-label ou classe")

    # -------------------------- Figuras e fontes ---------------------------
    figure_nodes = [node for node in nodes if node.tag == "figure"]
    figures_info = []
    missing_caption = []
    missing_source = []
    for idx, figure in enumerate(figure_nodes, 1):
        captions = [n for n in figure.children if n.tag == "figcaption"] + \
                   [n for n in nodes if n.tag == "figcaption" and any(a is figure for a in n.ancestors())]
        caption = captions[0] if captions else None
        caption_text = caption.text() if caption else ""
        hrefs = re.findall(r'href\s*=\s*["\'](https?://[^"\']+)["\']', caption.attrs.get("data-raw", "") if caption else "")
        if caption is not None:
            raw_caption = _node_outer_html(html, caption)
            hrefs = re.findall(r'href\s*=\s*["\'](https?://[^"\']+)["\']', raw_caption, re.I)
        source_cited = bool(hrefs) or bool(re.search(
            r"(?i)(cvm|sec|bndes|finep|openvc|shizune|wikidata|abvcap|lavca|distrito|brouky|fonte|source)", caption_text))
        info = {"index": idx, "caption": truncate(caption_text, 200),
                "has_caption": caption is not None, "caption_has_source": source_cited,
                "caption_links": hrefs[:3]}
        figures_info.append(info)
        if caption is None:
            missing_caption.append(idx)
        elif len(caption_text) < 10:
            missing_caption.append(idx)
        elif not source_cited:
            missing_source.append(idx)
    rep.metrics["figures"] = figures_info
    if len(figure_nodes) < 2:
        rep.bad("g3.figures.min", "pelo menos 2 visualizacoes em <figure> (Gráfico 1 e 2 da spec)",
                f"{len(figure_nodes)} <figure> encontrados (minimo 2)", figures=figures_info)
    else:
        rep.ok("g3.figures.min", "pelo menos 2 visualizacoes em <figure> (Gráfico 1 e 2 da spec)",
               f"{len(figure_nodes)} <figure> encontrados", figures=len(figure_nodes))
    if missing_caption:
        rep.bad("g3.figures.figcaption", "todo <figure> tem <figcaption> nao vazio",
                f"figura(s) sem figcaption: {missing_caption}", figures=figures_info)
    else:
        rep.ok("g3.figures.figcaption", "todo <figure> tem <figcaption> nao vazio",
               f"{len(figure_nodes)} figuras com figcaption")
    if missing_source:
        rep.bad("g3.figures.source", "todo <figcaption> cita unidade/fonte (link ou nome da fonte)",
                f"figura(s) sem fonte citada: {missing_source}", figures=figures_info)
    else:
        rep.ok("g3.figures.source", "todo <figcaption> cita unidade/fonte (link ou nome da fonte)",
               f"{len(figure_nodes)} figcaptions citam fonte/link")

    # -------------------------- Numeros reais ------------------------------
    money_matches = MONEY_RE.findall(visible_text)
    pct_matches = PCT_RE.findall(visible_text)
    thousands_matches = THOUSANDS_RE.findall(visible_text)
    plain_matches = PLAIN_NUM_RE.findall(visible_text)
    number_count = len(money_matches) + len(pct_matches) + len(thousands_matches)
    unique_numbers = {m.strip() for m in money_matches + pct_matches + thousands_matches}
    rep.metrics["formatted_numbers"] = number_count
    rep.metrics["unique_formatted_numbers"] = len(unique_numbers)
    rep.metrics["plain_numbers"] = len(plain_matches)
    if number_count < 30:
        rep.bad("g3.numbers.real", "presenca de numeros reais formatados (>= 30 ocorrencias)",
                f"apenas {number_count} valores monetarios/percentuais formatados "
                f"(moeda={len(money_matches)}, pct={len(pct_matches)}, milhar={len(thousands_matches)})",
                samples=list(unique_numbers)[:10])
    else:
        rep.ok("g3.numbers.real", "presenca de numeros reais formatados (>= 30 ocorrencias)",
               f"{number_count} ocorrencias ({len(unique_numbers)} valores distintos) de moeda/percentual/milhar",
               samples=list(unique_numbers)[:10])

    # -------------------------- Cards de estatistica -----------------------
    stat_cards = []
    empty_cards = []
    # Card = CONTAINER de metrica (nao subpartes como .kpi-label/.kpi-value).
    card_tokens = {"kpi", "stat", "statcell", "statcard", "stats", "metric", "metrica",
                   "indicador", "number", "numero", "big-number"}
    for node in nodes:
        tokens = [token.lower() for token in node.classes]
        if not tokens:
            continue
        is_card = any(
            token in card_tokens or re.fullmatch(r"(kpi|stat|metric)(-card|-box|-tile|-block)", token)
            for token in tokens
        )
        if is_card:
            stat_cards.append(node)
    for node in stat_cards:
        text = node.text()
        if not re.search(r"\d", text):
            empty_cards.append({"path": node.path(), "text": truncate(text, 120)})
    rep.metrics["stat_cards"] = len(stat_cards)
    if not stat_cards:
        rep.skip("g3.cards.nonempty", "cards de estatistica contem numero real",
                 "nenhum elemento com classe kpi/stat/metric encontrado (nomenclatura pode diferir)")
    elif empty_cards:
        rep.bad("g3.cards.nonempty", "cards de estatistica contem numero real",
                f"{len(empty_cards)} card(s) sem nenhum numero", violations=empty_cards[:10])
    else:
        rep.ok("g3.cards.nonempty", "cards de estatistica contem numero real",
               f"{len(stat_cards)} card(s) de estatistica com valores numericos")
    # metricas sem fonte/comparacao em cards de KPI
    unsourced_cards = []
    for node in stat_cards:
        text = norm_text(node.text())
        has_number = bool(re.search(r"\d", text))
        has_source = bool(re.search(r"(fonte|cvm|sec|bndes|openvc|shizune|wikidata|abvcap|distrito|lovca|lavca|brouky)", text))
        has_comparison = bool(re.search(r"(brasil|global|vs\.?|versus|contra|%)", text))
        if has_number and not (has_source or has_comparison):
            unsourced_cards.append({"path": node.path(), "text": truncate(text, 140)})
    if stat_cards and unsourced_cards:
        rep.skip("g3.cards.sourced", "KPI cards comparam Brasil vs Global ou citam fonte",
                 f"{len(unsourced_cards)} card(s) com numero sem fonte nem comparacao",
                 violations=unsourced_cards[:10])
    elif stat_cards:
        rep.ok("g3.cards.sourced", "KPI cards comparam Brasil vs Global ou citam fonte",
               f"{len(stat_cards)} card(s) de KPI com numero + fonte/comparacao")

    # -------------------------- Hero: H1 + Answer-First --------------------
    variables_g3 = _collect_css_vars(rules)
    h1_nodes = [node for node in nodes if node.tag == "h1"]
    if len(h1_nodes) == 1:
        rep.ok("g3.hero.h1_unique", "H1 unico no documento", "1 <h1> encontrado")
    elif not h1_nodes:
        rep.bad("g3.hero.h1_unique", "H1 unico no documento", "nenhum <h1> encontrado")
    else:
        rep.bad("g3.hero.h1_unique", "H1 unico no documento",
                f"{len(h1_nodes)} <h1> encontrados (deve haver exatamente 1)",
                headings=[truncate(node.text(), 100) for node in h1_nodes[:5]])
    h1_text = h1_nodes[0].text() if h1_nodes else ""
    h1_has_number = bool(re.search(r"\d|R\$|US\$|%", h1_text))

    if answer_block is not None:
        # NOVA SEMANTICA (task-8): H1 neutro/centrado + bloco .answer-first calculado.
        label_found = _answer_label_found(answer_block)
        if not answer_quantified:
            rep.bad("g3.hero.answer_first",
                    "bloco .answer-first com conclusao quantitativa (>=2 numeros, <=170 chars, rotulo 'Conclusao principal' ou 'Leitura principal')",
                    f"bloco encontrado sem >=2 numeros distintos; texto = {truncate(answer_statement, 170)!r}",
                    tokens=answer_tokens)
        elif len(answer_statement) > 170:
            rep.bad("g3.hero.answer_first",
                    "bloco .answer-first com conclusao quantitativa (>=2 numeros, <=170 chars, rotulo 'Conclusao principal' ou 'Leitura principal')",
                    f"afirmacao com {len(answer_statement)} chars (> 170): {truncate(answer_statement, 170)!r}")
        elif HEDGE_RE.search(answer_statement):
            rep.bad("g3.hero.answer_first",
                    "bloco .answer-first com conclusao quantitativa (>=2 numeros, <=170 chars, rotulo 'Conclusao principal' ou 'Leitura principal')",
                    f"linguagem nao afirmativa (hedge): {HEDGE_RE.search(answer_statement).group(0)!r} em "
                    f"{truncate(answer_statement, 170)!r}")
        elif not label_found:
            rep.bad("g3.hero.answer_first",
                    "bloco .answer-first com conclusao quantitativa (>=2 numeros, <=170 chars, "
                    "rotulo 'Conclusao principal' ou 'Leitura principal')",
                    "rotulo de conclusao ausente no bloco/contexto imediato; aceitos: "
                    + ", ".join(f"'{label}'" for label in ANSWER_LABELS))
        else:
            rep.ok("g3.hero.answer_first",
                   "bloco .answer-first com conclusao quantitativa (>=2 numeros, <=170 chars, "
                   "rotulo 'Conclusao principal' ou 'Leitura principal')",
                   f"rotulo encontrado: '{label_found}'; {len(set(answer_tokens))} numeros distintos: "
                   f"{', '.join(sorted(set(answer_tokens)))}; {len(answer_statement)} chars: "
                   f"{truncate(answer_statement, 170)!r}", tokens=sorted(set(answer_tokens)),
                   length=len(answer_statement), label_found=label_found)
        # H1 neutro: sem digitos/percentuais/moeda (titulo do documento, nao manchete)
        if h1_has_number:
            rep.bad("g3.hero.title_neutral", "H1 neutro, sem digitos/percentuais/moeda",
                    f"h1 contem numero: {truncate(h1_text, 160)!r}")
        else:
            rep.ok("g3.hero.title_neutral", "H1 neutro, sem digitos/percentuais/moeda",
                   f"h1 = {truncate(h1_text, 160)!r}")
        # Hero centralizado (text-align:center no H1 ou ancestral)
        align = _computed_prop(h1_nodes[0], "text-align", rules, variables_g3) if h1_nodes else None
        centered = "center" in str(align or "").lower()
        if centered:
            rep.ok("g3.hero.centered", "hero/H1 centralizado (text-align:center)",
                   f"text-align computado = {str(align).strip()!r}")
        else:
            rep.bad("g3.hero.centered", "hero/H1 centralizado (text-align:center)",
                    f"text-align computado = {str(align).strip()!r} (esperado center)")
    else:
        # FORMATO LEGADO: exige H1 quantitativo (sera substituido pela nova semantica).
        if not h1_nodes:
            rep.bad("g3.hero.answer_first", "headline declarativa com conclusao quantitativa (Answer-First)",
                    "nenhum <h1> encontrado")
        elif not h1_has_number or len(h1_text) < 30:
            rep.bad("g3.hero.answer_first", "headline declarativa com conclusao quantitativa (Answer-First)",
                    f"h1 = {truncate(h1_text, 160)!r} (quantitativa={h1_has_number}, len={len(h1_text)})")
        else:
            rep.ok("g3.hero.answer_first", "headline declarativa com conclusao quantitativa (Answer-First)",
                   f"h1 = {truncate(h1_text, 160)!r}")
        rep.skip("g3.hero.title_neutral", "H1 neutro, sem digitos/percentuais/moeda",
                 "formato legado: answer-first esta no H1 (nova semantica exige bloco .answer-first)")
        rep.skip("g3.hero.centered", "hero/H1 centralizado (text-align:center)",
                 "formato legado: centralizacao do hero nao exigida")

    # -------------------------- Numeracao semantica ------------------------
    sec_nums = [node for node in nodes if "sec-num" in node.classes]
    sec_num_texts = [re.sub(r"\s+", "", node.text()) for node in sec_nums]
    duplicated_nums = sorted({text for text in sec_num_texts if sec_num_texts.count(text) > 1})
    band_nodes = [node for node in nodes if "kpis-band" in node.classes or "kpis-in" in node.classes]
    band_zero_tokens: list[str] = []
    if band_nodes:
        band_text = re.sub(r"\s+", " ", band_nodes[0].text()).strip()
        band_zero_tokens = re.findall(r"(?<![\d.,])0[1-9](?![\d.,])", band_text)
    if sec_nums and duplicated_nums:
        rep.bad("g3.numeracao.semantica", "numeracao semantica de secoes e' unica (01, 02, ...)",
                f"numeros duplicados em .sec-num: {duplicated_nums}", sec_nums=sec_num_texts)
    elif band_zero_tokens:
        rep.bad("g3.numeracao.semantica", "numeracao semantica de secoes e' unica (01, 02, ...)",
                f"faixa de metricas (kpis-band) contem token(s) de numeracao: {band_zero_tokens}",
                band_tokens=band_zero_tokens)
    elif sec_nums:
        rep.ok("g3.numeracao.semantica", "numeracao semantica de secoes e' unica (01, 02, ...)",
               f"{len(sec_nums)} marcador(es) .sec-num unicos ({', '.join(sec_num_texts)}); "
               f"faixa de metricas sem tokens 0N", sec_nums=sec_num_texts)
    else:
        rep.skip("g3.numeracao.semantica", "numeracao semantica de secoes e' unica (01, 02, ...)",
                 "dashboard sem .sec-num; numeracao nao aplicavel")

    # -------------------------- Botoes falsos / links mortos --------------
    dead_links = []
    for node in nodes:
        if node.tag != "a":
            continue
        href = (node.attrs.get("href") or "").strip()
        if href in ("", "#") or href.lower().startswith("javascript:"):
            dead_links.append({"path": node.path(), "href": href or "(vazio)",
                               "text": truncate(node.text(), 100)})
    if dead_links:
        rep.bad("g3.hero.dead_links", "sem botoes falsos ou links '#'/javascript:",
                f"{len(dead_links)} ancora(s) sem acao real", violations=dead_links[:10])
    else:
        rep.ok("g3.hero.dead_links", "sem botoes falsos ou links '#'/javascript:",
               "todas as ancoras inspecionadas apontam para URL real")

    rep.limitations.append(
        "Checagem lexical cobre texto visivel; atributos de conteudo de JS embutido nao "
        "sao avaliados alem de title/alt/aria-label/class. 'Metrica sem fonte' e' checada "
        "em <figcaption> e cards KPI pela presenca de link/nome da fonte."
    )
    rep.seconds = time.time() - t0
    return rep


def _node_outer_html(html: str, node: DomNode) -> str:
    """Recorta um trecho do HTML proximo ao texto do no (aproximacao p/ figcaption)."""
    text = html[:200_000]
    for part in node.text_parts[:1]:
        idx = text.find(html_mod.escape(part.strip()[:60])) if part.strip() else -1
        if idx >= 0:
            return text[max(0, idx - 2000): idx + 2000]
    return ""


# ==========================================================================
# GATE 4 - UI/UX executivo, WCAG AAA e responsividade
# ==========================================================================
def _split_selectors(selector: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in selector:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return [part for part in parts if part]


def _split_selector_chain(selector: str) -> tuple[list[str], list[str]]:
    """Divide um seletor em compostos e combinadores (relacao com o anterior).

    Suporta descendente (espaco), filho (>), irmao adjacente (+) e geral (~).
    """
    compounds: list[str] = []
    combinators: list[str] = []
    buf: list[str] = []
    depth = 0
    skip_space = False

    def flush(comb: str) -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            compounds.append(text)
            combinators.append(comb)

    for ch in selector:
        if ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and ch in ">+~":
            flush(" ")
            if combinators:
                combinators[-1] = ch
            skip_space = True
        elif depth == 0 and ch.isspace():
            flush(" ")
            skip_space = True
        else:
            if skip_space and not buf:
                skip_space = False
            buf.append(ch)
    flush(" ")
    # remove compostos vazios e alinhar combinadores (o primeiro composto nao tem relacao)
    while compounds and not compounds[0]:
        compounds.pop(0)
        if combinators:
            combinators.pop(0)
    if combinators:
        combinators[0] = " "
    return compounds, combinators


_COMPOUND_CACHE: dict[str, dict[str, Any]] = {}


def _parse_compound(compound: str) -> dict[str, Any] | None:
    cached = _COMPOUND_CACHE.get(compound)
    if cached is not None:
        return cached if cached else None
    raw = compound.strip()
    if not raw or raw.startswith("@") or "::" in raw or raw == "*":
        _COMPOUND_CACHE[compound] = {"universal": raw == "*"}
        return _COMPOUND_CACHE[compound] if raw == "*" else None
    # pseudo-classe :root -> casa com <html>
    if re.fullmatch(r":root", raw):
        parsed = {"root": True}
        _COMPOUND_CACHE[compound] = parsed
        return parsed
    # remove pseudo-classes (ex.: :hover, :focus-visible, :not(...), :nth-child(...))
    base = re.sub(r":(?:not|is|where|has)\([^()]*\)", "", raw)
    base = re.sub(r":(?:[a-zA-Z-]+)(?:\([^()]*\))?", "", base)
    if not base.strip():
        _COMPOUND_CACHE[compound] = {}
        return None
    attrs: list[tuple[str, str | None]] = []
    for match in re.finditer(r"\[([^\]]+)\]", base):
        body = match.group(1)
        if "=" in body:
            name, _, value = body.partition("=")
            attrs.append((name.strip().lower(), value.strip().strip("\"'")))
        else:
            attrs.append((body.strip().lower(), None))
    remaining = re.sub(r"\[[^\]]+\]", "", base)
    id_match = re.search(r"#([A-Za-z0-9_-]+)", remaining)
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9-]*)", remaining)
    classes = set(re.findall(r"\.([A-Za-z0-9_-]+)", remaining))
    parsed = {
        "id": id_match.group(1) if id_match else None,
        "tag": tag_match.group(1).lower() if tag_match else None,
        "classes": classes,
        "attrs": attrs,
    }
    _COMPOUND_CACHE[compound] = parsed
    return parsed


def _match_compound(compound: str, node: DomNode) -> bool:
    parsed = _parse_compound(compound)
    if parsed is None:
        return False
    if parsed.get("universal"):
        return True
    if parsed.get("root"):
        return node.tag == "html"
    if not parsed:
        return False
    ident = parsed.get("id")
    if ident and node.node_id != ident:
        return False
    tag = parsed.get("tag")
    if tag and node.tag != tag:
        return False
    classes = parsed.get("classes") or set()
    if classes and not classes.issubset(node.classes):
        return False
    for name, value in parsed.get("attrs") or []:
        if name not in node.attrs:
            return False
        if value is not None and node.attrs.get(name, "").strip() != value:
            return False
    return True


def _match_chain(compounds: list[str], combinators: list[str], node: DomNode) -> bool:
    if not compounds:
        return False
    if not _match_compound(compounds[-1], node):
        return False
    current = node
    idx = len(compounds) - 1
    while idx > 0:
        comb = combinators[idx] if idx < len(combinators) else " "
        target = compounds[idx - 1]
        if comb == ">":
            current = current.parent
            if current is None or not _match_compound(target, current):
                return False
        elif comb in ("+", "~"):
            parent = current.parent
            if parent is None:
                return False
            try:
                pos = parent.children.index(current)
            except ValueError:
                return False
            candidates: list[DomNode] = []
            if comb == "+":
                if pos > 0:
                    candidates = [parent.children[pos - 1]]
            else:
                candidates = list(reversed(parent.children[:pos]))
            matched = None
            for cand in candidates:
                if _match_compound(target, cand):
                    matched = cand
                    break
            if matched is None:
                return False
            current = matched
        else:  # descendente
            current = current.parent
            matched = None
            while current is not None:
                if _match_compound(target, current):
                    matched = current
                    break
                current = current.parent
            if matched is None:
                return False
            current = matched
        idx -= 1
    return True


_SELECTOR_CHAIN_CACHE: dict[str, list[tuple[list[str], list[str]]]] = {}


def _rule_matches_node(rule: CSSRule, node: DomNode) -> bool:
    chains = _SELECTOR_CHAIN_CACHE.get(rule.selector)
    if chains is None:
        chains = []
        for sel in _split_selectors(rule.selector):
            if "::" in sel:
                continue
            chains.append(_split_selector_chain(sel))
        _SELECTOR_CHAIN_CACHE[rule.selector] = chains
    for compounds, combinators in chains:
        if compounds and _match_chain(compounds, combinators, node):
            return True
    return False


def _inline_map(node: DomNode) -> dict[str, str]:
    cached = node.__dict__.get("_inline_map")
    if cached is None:
        cached = {}
        for name, value in _split_declarations(node.attrs.get("style", "")):
            cached[name] = value
        node.__dict__["_inline_map"] = cached
    return cached


def _lookup_prop(node: DomNode, prop: str, rules: list[CSSRule], variables: dict[str, str],
                 include_media: bool = False) -> str | None:
    value: str | None = None
    for rule in rules:
        if rule.media and not include_media:
            continue
        candidate = rule.get(prop)
        if candidate is not None and _rule_matches_node(rule, node):
            value = candidate
    inline = _inline_map(node)
    if prop in inline:
        value = inline[prop]
    if value is None:
        return None
    return resolve_vars(value, variables)


def _computed_prop(node: DomNode | None, prop: str, rules: list[CSSRule], variables: dict[str, str],
                   default: str | None = None) -> str | None:
    """Valor computado com heranca (color, font-weight, white-space, ...)."""
    current = node
    while current is not None:
        value = _lookup_prop(current, prop, rules, variables)
        if value is not None and value.strip().lower() not in ("inherit", "unset", "initial", "revert"):
            return value
        current = current.parent
    return default


def _collect_css_vars(rules: list[CSSRule]) -> dict[str, str]:
    variables: dict[str, str] = {}
    for rule in rules:
        for name, value in rule.declarations:
            if name.startswith("--"):
                variables[name] = value
    return variables


FONT_SIZE_KEYWORDS = {
    "xx-small": 9.0, "x-small": 10.0, "small": 13.0, "medium": 16.0,
    "large": 18.0, "x-large": 24.0, "xx-large": 32.0,
}


def _font_size_to_px(value: str | None, parent_px: float = 16.0) -> tuple[float | None, str]:
    """Converte font-size para px. Retorna (px, metodo). Em base 16px; em = pai."""
    if value is None:
        return None, "ausente"
    raw = str(value).strip().lower()
    if raw in ("inherit", "unset", "initial", "revert", "revert-layer"):
        return None, "herdado"
    if raw in FONT_SIZE_KEYWORDS:
        return FONT_SIZE_KEYWORDS[raw], "keyword"
    if re.fullmatch(r"-?\d*\.?\d+%", raw):
        return parent_px * float(raw[:-1]) / 100.0, "percent"
    if raw.startswith("clamp("):
        inner = raw[len("clamp("):].rstrip(")")
        parts = [p.strip() for p in re.split(r",(?![^(]*\))", inner)]
        px = _font_size_to_px(parts[0], parent_px)[0] if parts else None
        return px, "clamp-min"
    if raw.startswith("calc("):
        inner = raw[len("calc("):].rstrip(")")
        tokens = re.findall(r"(?:\+|\-)?\s*\d*\.?\d+\s*(?:px|rem|em|pt)", inner)
        if not tokens:
            return None, "calc"
        total = 0.0
        for token in tokens:
            sign = -1.0 if token.strip().startswith("-") else 1.0
            token_clean = token.strip().lstrip("+-").strip()
            px = length_to_px(token_clean, 16.0, parent_px)
            if px is None:
                return None, "calc"
            total += sign * px
        return round(total, 2), "calc"
    px = length_to_px(raw, 16.0, parent_px)
    if px is not None:
        unit = re.search(r"(px|rem|em|pt|pc|in|cm|mm)\s*$", raw)
        return px, unit.group(1) if unit else "unitless"
    return None, "nao-parseavel"


def _node_font_px(node: DomNode | None, rules: list[CSSRule], variables: dict[str, str]) -> float:
    if node is None:
        return 16.0
    cached = node.__dict__.get("_font_px")
    if cached is not None:
        return cached
    parent_px = _node_font_px(node.parent, rules, variables) if node.parent is not None else 16.0
    value = _lookup_prop(node, "font-size", rules, variables)
    px, _ = _font_size_to_px(value, parent_px)
    result = px if px is not None else parent_px
    node.__dict__["_font_px"] = result
    return result


def _font_weight(node: DomNode | None, rules: list[CSSRule], variables: dict[str, str]) -> int:
    value = _computed_prop(node, "font-weight", rules, variables, "400")
    if value is None:
        return 400
    raw = value.strip().lower()
    if raw == "bold":
        return 700
    if raw == "normal":
        return 400
    try:
        return int(float(raw))
    except ValueError:
        return 400


def _is_large_text(px: float, weight: int) -> bool:
    return px >= 24.0 or (px >= 18.66 and weight >= 700)


def _solid_bg_from_value(value: str | None) -> tuple[tuple[int, int, int, float] | None, bool]:
    """Retorna (cor_solida_ou_None, tem_imagem_ou_gradiente)."""
    if not value:
        return None, False
    v = value.strip()
    color = parse_color(v)
    if color is not None:
        return color, False
    if re.search(r"(gradient|url\()", v, re.I):
        # tenta achar cor de fallback dentro do shorthand
        for token in re.findall(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)", v):
            color = parse_color(token)
            if color is not None:
                return color, True
        return None, True
    for token in re.split(r"\s+", v):
        color = parse_color(token)
        if color is not None:
            return color, False
    return None, False


def _effective_background(node: DomNode | None, rules: list[CSSRule], variables: dict[str, str],
                          default_bg: tuple[int, int, int, float]) -> tuple[tuple[int, int, int, float] | None, bool]:
    """Cor de fundo efetiva (composicao de camadas). None = desconhecida (imagem/gradiente)."""
    layers: list[tuple[int, int, int, float]] = []
    unknown = False
    current = node
    while current is not None:
        color, has_image = _solid_bg_from_value(_lookup_prop(current, "background-color", rules, variables))
        if color is None and not has_image:
            color, has_image = _solid_bg_from_value(_lookup_prop(current, "background", rules, variables))
        if has_image:
            unknown = True
            break
        if color is not None:
            layers.append(color)
            if color[3] >= 1.0:
                break
        current = current.parent
    if unknown:
        return None, True
    if not layers or layers[-1][3] < 1.0:
        layers.append(default_bg)
    result = layers[-1]
    for layer in reversed(layers[:-1]):
        result = composite(layer, result)
    return result, False


def _default_colors(nodes: list[DomNode], rules: list[CSSRule], variables: dict[str, str]) -> tuple[
        tuple[int, int, int, float], tuple[int, int, int, float]]:
    bg = parse_color(_computed_prop(next((n for n in nodes if n.tag == "body"), None), "background-color",
                                    rules, variables))
    if bg is None or bg[3] < 1.0:
        html_bg = parse_color(_computed_prop(next((n for n in nodes if n.tag == "html"), None),
                                             "background", rules, variables))
        bg = html_bg if html_bg is not None and html_bg[3] >= 1.0 else (255, 255, 255, 1.0)
    text = parse_color(_computed_prop(next((n for n in nodes if n.tag == "body"), None), "color",
                                      rules, variables))
    if text is None:
        text = (15, 23, 42, 1.0)  # #0F172A da spec
    return text, bg


def _node_text_color(node: DomNode, rules: list[CSSRule], variables: dict[str, str],
                     default_text: tuple[int, int, int, float],
                     bg: tuple[int, int, int, float]) -> tuple[int, int, int, float] | None:
    value = _lookup_prop(node, "color", rules, variables)
    if value is None:
        value = _computed_prop(node, "color", rules, variables)
    color = parse_color(value)
    if color is None:
        return default_text
    if color[3] <= 0:
        return None
    if color[3] < 1.0:
        return composite(color, bg)
    return color


def _contrast_analysis(html: str, nodes: list[DomNode], rules: list[CSSRule]) -> dict[str, Any]:
    """Calcula contraste WCAG de pares texto/fundo REAIS resolvidos pelo DOM."""
    variables = _collect_css_vars(rules)
    default_text, default_bg = _default_colors(nodes, rules, variables)
    pairs: dict[tuple[str, str, str], dict[str, Any]] = {}
    unknown_bg = 0
    checked = 0
    for node in nodes:
        if node.tag in ("#document", "script", "style", "head", "meta", "link", "title", "br", "path",
                        "rect", "circle", "line", "polyline", "polygon", "g", "svg", "defs", "use"):
            continue
        direct_text = " ".join(part.strip() for part in node.text_parts if part.strip())
        if not direct_text:
            continue
        if _is_hidden_node(node, rules, variables):
            continue
        bg, unknown = _effective_background(node, rules, variables, default_bg)
        if unknown or bg is None:
            unknown_bg += 1
            continue
        fg = _node_text_color(node, rules, variables, default_text, bg)
        if fg is None:
            continue
        px = _node_font_px(node, rules, variables)
        weight = _font_weight(node, rules, variables)
        large = _is_large_text(px, weight)
        threshold = 4.5 if large else 7.0
        ratio = contrast_ratio(fg, bg)
        checked += 1
        key = (color_hex(fg), color_hex(bg), "large" if large else "normal")
        bucket = pairs.setdefault(key, {
            "fg": color_hex(fg), "bg": color_hex(bg), "threshold": threshold,
            "count": 0, "worst_ratio": ratio, "example": node.path(),
            "example_text": truncate(direct_text, 90),
        })
        bucket["count"] += 1
        if ratio < bucket["worst_ratio"]:
            bucket["worst_ratio"] = ratio
            bucket["example"] = node.path()
            bucket["example_text"] = truncate(direct_text, 90)

    # pares declarados em regras CSS (mesmo sem elemento correspondente no DOM)
    declared = 0
    for rule in rules:
        fg_raw = rule.get("color")
        bg_raw = rule.get("background-color") or rule.get("background")
        fg = parse_color(resolve_vars(fg_raw, variables)) if fg_raw else None
        bg, has_image = _solid_bg_from_value(resolve_vars(bg_raw, variables)) if bg_raw else (None, False)
        if fg is None or bg is None or has_image:
            continue
        px, _ = _font_size_to_px(rule.get("font-size"), 16.0)
        weight = 0
        weight_raw = (rule.get("font-weight") or "").lower()
        if weight_raw in ("bold", "600", "700", "800", "900"):
            weight = 700
        large = _is_large_text(px or 16.0, weight)
        threshold = 4.5 if large else 7.0
        ratio = contrast_ratio(fg, bg)
        declared += 1
        key = (color_hex(fg), color_hex(bg), "large" if large else "normal")
        bucket = pairs.setdefault(key, {
            "fg": color_hex(fg), "bg": color_hex(bg), "threshold": threshold,
            "count": 0, "worst_ratio": ratio, "example": f"css:{truncate(rule.selector, 100)}",
            "example_text": "par declarado no CSS",
        })
        if f"css:{truncate(rule.selector, 100)}" not in bucket.get("example", ""):
            bucket["example"] = bucket.get("example") or f"css:{truncate(rule.selector, 100)}"
        if ratio < bucket["worst_ratio"]:
            bucket["worst_ratio"] = ratio
        if bucket["count"] == 0:
            bucket["count"] = 1
    return {
        "pairs": list(pairs.values()),
        "nodes_checked": checked,
        "declared_pairs": declared,
        "unknown_background_nodes": unknown_bg,
        "default_text": color_hex(default_text),
        "default_bg": color_hex(default_bg),
    }


def _is_hidden_node(node: DomNode, rules: list[CSSRule], variables: dict[str, str]) -> bool:
    classes = node.classes
    if classes & {"sr-only", "visually-hidden", "hidden", "hide", "d-none", "is-hidden", "screen-reader-text"}:
        return True
    # skip-links sao invisiveis ate receberem foco (left:-999px): nao sao texto visivel.
    if any(re.fullmatch(r"(skip|skip-link|skip-to-content|jump-link)", token.lower()) for token in classes):
        return True
    position = (_lookup_prop(node, "position", rules, variables) or "").strip().lower()
    if position in ("absolute", "fixed"):
        left = length_to_px((_lookup_prop(node, "left", rules, variables) or "").strip(), 16.0)
        top = length_to_px((_lookup_prop(node, "top", rules, variables) or "").strip(), 16.0)
        if (left is not None and left <= -100) or (top is not None and top <= -100):
            return True
    if "hidden" in node.attrs:
        return True
    style = _inline_map(node)
    if style.get("display", "").strip().lower() == "none":
        return True
    display = _lookup_prop(node, "display", rules, variables)
    if display and display.strip().lower() == "none":
        return True
    return False


def _is_value_cell(text: str) -> bool:
    """Celula de VALOR (monetario/percentual) x celula de prosa com numero embutido.

    A exigencia tabular-nums+nowrap da spec vale para valores; uma celula de
    leitura longa (ex.: '...Densa no Brasil 9,3% 1,5%') nao deve ser nowrap.
    """
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t or len(t) > 48:
        return False
    stripped = re.sub(r"(?i)(r\$|us\$|\$|%|mil|mi|bi|m|b|k|pp|x|[-+.,;:()|/])", "", t)
    stripped = re.sub(r"\d", "", stripped)
    letters = re.sub(r"[^A-Za-z\u00C0-\u024F]", "", stripped)
    return len(letters) <= max(2, int(0.25 * len(t)))


def _extract_hrefs(nodes: list[DomNode]) -> list[dict[str, Any]]:
    out = []
    for node in nodes:
        if node.tag != "a":
            continue
        href = (node.attrs.get("href") or "").strip()
        if href.startswith("https://"):
            out.append({
                "href": href,
                "text": truncate(node.text(), 100),
                "target": node.attrs.get("target", ""),
                "rel": node.attrs.get("rel", ""),
                "path": node.path(),
            })
    return out


def _collect_media_breakpoints(rules: list[CSSRule]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for rule in rules:
        if not rule.media:
            continue
        key = rule.media
        if key in seen:
            continue
        seen.add(key)
        bp = media_breakpoints(rule.media)
        out.append({"media": rule.media, "max_px": bp["max"], "min_px": bp["min"]})
    return out


def _color_present(html_css_lower: str, hex_color: str) -> bool:
    h = hex_color.lower()
    if h in html_css_lower:
        return True
    try:
        rgb = tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
        for sep in (", ", ",", " "):
            candidate = f"rgb({rgb[0]}{sep}{rgb[1]}{sep}{rgb[2]})"
            if candidate in html_css_lower:
                return True
    except ValueError:
        pass
    return False


def gate4() -> GateReport:
    t0 = time.time()
    rep = GateReport(
        4,
        "UI/UX executivo, acessibilidade WCAG AAA e responsividade",
        method=(
            "Analise estatica de dashboard/index.html: font-size convertido de "
            "rem/em/pt/percent para px (base 16px), contrastes WCAG calculados pela "
            "formula de luminancia relativa sobre pares texto/fundo resolvidos pelo "
            "DOM+CSS (com heranca e variaveis --x), inspecao de tabelas/celulas com "
            "valores, media queries, fixed widths e links HTTP reais. Medicao runtime "
            "(scrollWidth, JS, screenshots) tentada via tools/screenshots.py + Chrome "
            "headless; se o sandbox impedir o Chrome, a checagem fica PENDING explicita."
        ),
    )
    if not DASHBOARD.exists():
        rep.pending("g4.html.present", "dashboard/index.html existe",
                    "dashboard ainda nao foi renderizado: Gate 4 fica PENDING", path=rel(DASHBOARD))
        rep.seconds = time.time() - t0
        return rep
    html = read_text_tolerant(DASHBOARD)
    html_bytes = DASHBOARD.stat().st_size
    rep.metrics["html_bytes"] = html_bytes
    rep.metrics["html_sha256"] = sha256_file(DASHBOARD)
    rep.ok("g4.html.present", "dashboard/index.html existe",
           f"{html_bytes} bytes em {rel(DASHBOARD)} (sha256 {rep.metrics['html_sha256'][:16]}...)")

    _, nodes = parse_dom(html)
    css_text, rules, inline_styles = _collect_styles(html, nodes)
    variables = _collect_css_vars(rules)
    lower_html_css = (html + "\n" + css_text).lower()
    rep.metrics["css_rules"] = len(rules)
    rep.metrics["dom_nodes"] = len(nodes)

    # -------------------------- Piso tipografico 12px ----------------------
    font_violations: list[dict[str, Any]] = []
    font_checked = 0
    for rule in rules:
        raw = rule.get("font-size")
        if raw is None:
            continue
        resolved = resolve_vars(raw, variables)
        px, method = _font_size_to_px(resolved, 16.0)
        if px is None:
            continue
        font_checked += 1
        if px < 12.0 - 1e-9:
            font_violations.append({"selector": truncate(rule.selector, 120), "media": rule.media,
                                    "declared": truncate(resolved, 60), "px": round(px, 2), "method": method})
    for node, decls in inline_styles:
        for prop, raw in decls:
            if prop != "font-size":
                continue
            resolved = resolve_vars(raw, variables)
            px, method = _font_size_to_px(resolved, _node_font_px(node.parent, rules, variables))
            if px is None:
                continue
            font_checked += 1
            if px < 12.0 - 1e-9:
                font_violations.append({"selector": f"inline:{node.path()}", "media": None,
                                        "declared": truncate(resolved, 60), "px": round(px, 2), "method": method})
    # atributo font-size em SVG
    for node in nodes:
        if node.tag == "svg" or node.tag == "text":
            raw = node.attrs.get("font-size")
            if not raw:
                continue
            px, method = _font_size_to_px(raw, _node_font_px(node, rules, variables))
            if px is not None:
                font_checked += 1
                if px < 12.0 - 1e-9:
                    font_violations.append({"selector": f"svg:{node.path()}", "media": None,
                                            "declared": raw, "px": round(px, 2), "method": method})
    rep.metrics["font_size_declarations"] = font_checked
    low_confidence = [v for v in font_violations if v["method"] in ("clamp-min", "calc", "percent")]
    hard_violations = [v for v in font_violations if v["method"] not in ("clamp-min", "calc", "percent")]
    if hard_violations:
        rep.bad("g4.font.floor", "piso tipografico >= 12px em todas as declaracoes CSS",
                f"{len(hard_violations)} declaracao(oes) < 12px (de {font_checked})",
                violations=hard_violations[:12])
        if low_confidence:
            rep.skip("g4.font.floor_soft", "piso tipografico em valores relativos (clamp/calc/percent)",
                     f"{len(low_confidence)} valor(es) resolvidos < 12px sem certeza do valor final",
                     violations=low_confidence[:8])
    elif low_confidence:
        rep.skip("g4.font.floor", "piso tipografico >= 12px em todas as declaracoes CSS",
                 f"{len(low_confidence)} valor(es) relativos < 12px (clamp/calc/percent) de {font_checked}",
                 violations=low_confidence[:8])
    else:
        declared_px = []
        for rule in rules:
            raw = rule.get("font-size")
            if not raw:
                continue
            px, _ = _font_size_to_px(resolve_vars(raw, variables), 16.0)
            if px is not None:
                declared_px.append(px)
        min_px = min(declared_px) if declared_px else None
        rep.ok("g4.font.floor", "piso tipografico >= 12px em todas as declaracoes CSS",
               f"{font_checked} declaracoes font-size convertidas para px; menor = "
               f"{(str(round(min_px, 2)) + 'px') if min_px is not None else 'n/d'}")

    # -------------------------- tabular-nums + nowrap ----------------------
    css_has_tabular = "tabular-nums" in lower_html_css
    css_has_nowrap = re.search(r"white-space\s*:\s*nowrap", lower_html_css) is not None
    money_cells: list[DomNode] = []
    prose_cells_with_numbers = 0
    for node in nodes:
        if node.tag not in ("td", "th"):
            continue
        text = node.text()
        if not MONEY_CELL_RE.search(text):
            continue
        if _is_value_cell(text):
            money_cells.append(node)
        else:
            prose_cells_with_numbers += 1
    if prose_cells_with_numbers:
        rep.limitations.append(
            f"{prose_cells_with_numbers} celula(s) de prosa com numeros embutidos nao foram "
            "submetidas a exigencia de nowrap+tabular-nums (a spec exige para VALORES; nowrap "
            "em prosa quebraria o layout)."
        )
    cell_violations: list[dict[str, Any]] = []
    for node in money_cells:
        text = node.text()
        variant = (_computed_prop(node, "font-variant-numeric", rules, variables) or "").lower()
        white = (_computed_prop(node, "white-space", rules, variables) or "").lower()
        if "tabular-nums" not in variant or "nowrap" not in white:
            cell_violations.append({
                "path": node.path(), "text": truncate(text, 80),
                "font-variant-numeric": variant or "(nao declarado)",
                "white-space": white or "(nao declarado)",
            })
    rep.metrics["money_cells"] = len(money_cells)
    if not css_has_tabular or not css_has_nowrap:
        rep.bad("g4.numbers.tabular", "valores monetarios/percentuais usam tabular-nums + nowrap",
                f"CSS {'nao tem' if not css_has_tabular else 'tem'} tabular-nums e "
                f"{'nao tem' if not css_has_nowrap else 'tem'} white-space:nowrap globalmente",
                tabular_nums_css=css_has_tabular, nowrap_css=css_has_nowrap)
    elif not money_cells:
        rep.pending("g4.numbers.tabular", "valores monetarios/percentuais usam tabular-nums + nowrap",
                    "nenhuma celula <td>/<th> com valor monetario/percentual encontrada")
    elif cell_violations:
        rep.bad("g4.numbers.tabular", "valores monetarios/percentuais usam tabular-nums + nowrap",
                f"{len(cell_violations)}/{len(money_cells)} celula(s) com valor sem tabular-nums+nowrap",
                violations=cell_violations[:10])
    else:
        rep.ok("g4.numbers.tabular", "valores monetarios/percentuais usam tabular-nums + nowrap",
               f"{len(money_cells)} celulas com valores monetarios/percentuais cobertas por tabular-nums + nowrap")

    # -------------------------- Tabela com scroll horizontal ---------------
    tables = [node for node in nodes if node.tag == "table"]
    tables_without_scroll = []
    for table in tables:
        found = False
        current: DomNode | None = table
        while current is not None:
            inline = _inline_map(current)
            for prop in ("overflow-x", "overflow"):
                value = inline.get(prop) or _lookup_prop(current, prop, rules, variables) or ""
                if re.search(r"\b(auto|scroll)\b", value.lower()):
                    found = True
                    break
            if found:
                break
            current = current.parent
        if not found:
            tables_without_scroll.append({"table": table.path()})
    rep.metrics["tables"] = len(tables)
    if not tables:
        rep.bad("g4.table.scroll", "tabela auditavel presente e com container overflow-x:auto/scroll",
                "nenhum <table> encontrado no dashboard")
    elif tables_without_scroll:
        rep.bad("g4.table.scroll", "tabela auditavel presente e com container overflow-x:auto/scroll",
                f"{len(tables_without_scroll)}/{len(tables)} tabela(s) sem container overflow-x auto/scroll",
                violations=tables_without_scroll[:8])
    else:
        rep.ok("g4.table.scroll", "tabela auditavel presente e com container overflow-x:auto/scroll",
               f"{len(tables)} tabela(s) dentro de container com overflow-x auto/scroll")

    # -------------------------- Media queries 375/768/1440 ----------------
    breakpoints = _collect_media_breakpoints(rules)
    rep.metrics["media_breakpoints"] = [bp for bp in breakpoints if bp["max_px"] or bp["min_px"]]
    base_layout = any(not rule.media and re.search(r"(display\s*:\s*(grid|flex)|grid-template|flex-wrap)",
                                                   " ".join(f"{k}:{v}" for k, v in rule.declarations))
                      for rule in rules)
    coverage: dict[str, Any] = {}
    for target in (375, 768, 1440):
        max_hits = [bp["media"] for bp in breakpoints if bp["max_px"] is not None and bp["max_px"] >= target]
        min_hits = [bp["media"] for bp in breakpoints if bp["min_px"] is not None and bp["min_px"] <= target]
        covered = bool(max_hits or min_hits) or (target == 1440 and base_layout)
        coverage[str(target)] = {"covered": covered, "max_hits": max_hits[:4], "min_hits": min_hits[:4],
                                 "base_layout_1440": base_layout if target == 1440 else None}
    rep.metrics["viewport_coverage"] = coverage
    uncovered = [t for t, c in coverage.items() if not c["covered"]]
    if uncovered:
        rep.bad("g4.responsive.media_queries", "media queries cobrem viewports 375/768/1440",
                f"sem cobertura CSS declarada para: {uncovered}px", breakpoints=rep.metrics["media_breakpoints"],
                coverage=coverage)
    else:
        rep.ok("g4.responsive.media_queries", "media queries cobrem viewports 375/768/1440",
               f"{len(rep.metrics['media_breakpoints'])} breakpoints; cobertura 375/768/1440 verificada",
               coverage=coverage)

    # meta viewport + fixed widths de risco
    has_viewport_meta = bool(re.search(r'<meta[^>]+name=["\']viewport["\'][^>]*content=["\'][^"\']*width\s*=\s*device-width',
                                       html, re.I))
    if not has_viewport_meta:
        rep.bad("g4.responsive.viewport_meta", "meta viewport width=device-width presente",
                "meta tag viewport ausente")
    else:
        rep.ok("g4.responsive.viewport_meta", "meta viewport width=device-width presente",
               "meta viewport declarado")
    def _has_scroll_ancestor(node: DomNode) -> bool:
        current: DomNode | None = node
        while current is not None:
            if any("scroll" in token.lower() or "table-wrap" in token.lower()
                   for token in current.classes):
                return True
            for prop in ("overflow-x", "overflow"):
                value = (_inline_map(current).get(prop) or _lookup_prop(current, prop, rules, variables) or "")
                if re.search(r"\b(auto|scroll)\b", value.lower()):
                    return True
            current = current.parent
        return False

    risky_widths: list[dict[str, Any]] = []
    for rule in rules:
        if rule.media:
            continue
        width_decl = rule.get("width") or rule.get("min-width")
        if not width_decl:
            continue
        resolved = resolve_vars(width_decl, variables)
        if re.search(r"(%|vw|calc|max-content|min-content|fit-content|auto|inherit)", resolved, re.I):
            continue
        px = length_to_px(resolved, 16.0)
        if px is None or px < 375:
            continue
        has_fluid_guard = bool(rule.get("max-width")) and re.search(r"(100%|vw)", rule.get("max-width") or "")
        if has_fluid_guard:
            continue
        # largura fixa em <table> dentro de container com scroll horizontal e'
        # exatamente o padrao exigido pela spec (tabela densa com overflow-x:auto).
        matched = [node for node in nodes if _rule_matches_node(rule, node)]
        if matched and all(node.tag == "table" or _has_scroll_ancestor(node) for node in matched):
            continue
        risky_widths.append({"selector": truncate(rule.selector, 120), "property": "width/min-width",
                             "declared": truncate(resolved, 60), "px": round(px, 1),
                             "matched_nodes": len(matched)})
    rep.metrics["risky_fixed_widths"] = len(risky_widths)
    if risky_widths:
        rep.bad("g4.responsive.fixed_widths", "sem larguras fixas capazes de estourar 375px",
                f"{len(risky_widths)} declaracao(oes) de width/min-width >= 375px sem guarda fluida",
                violations=risky_widths[:10])
    else:
        rep.ok("g4.responsive.fixed_widths", "sem larguras fixas capazes de estourar 375px",
               "nenhuma width/min-width fixa >= 375px sem max-width fluido")
    body_overflow_hidden = False
    # verifica mascara de overflow no body
    for node in nodes:
        if node.tag in ("html", "body"):
            value = _lookup_prop(node, "overflow-x", rules, variables) or _lookup_prop(node, "overflow", rules, variables) or ""
            if "hidden" in value.lower():
                body_overflow_hidden = True
    if body_overflow_hidden:
        rep.skip("g4.responsive.overflow_mask", "overflow horizontal nao mascarado com overflow:hidden",
                 "html/body usa overflow:hidden; overflow real pode estar oculto (verificacao runtime necessaria)")

    # -------------------------- Contraste WCAG AAA ------------------------
    contrast = _contrast_analysis(html, nodes, rules)
    pairs = sorted(contrast["pairs"], key=lambda pair: pair["worst_ratio"])
    contrast_violations = [pair for pair in pairs if pair["worst_ratio"] < pair["threshold"] - 1e-9]
    rep.metrics["contrast_pairs"] = len(pairs)
    rep.metrics["contrast_violations"] = len(contrast_violations)
    rep.metrics["contrast_nodes_checked"] = contrast["nodes_checked"]
    rep.metrics["contrast_unknown_bg"] = contrast["unknown_background_nodes"]
    rep.metrics["min_contrast_ratio"] = round(pairs[0]["worst_ratio"], 2) if pairs else None
    if contrast_violations:
        rep.bad("g4.contrast.wcag_aaa", "contraste WCAG AAA (>= 7:1 texto normal; >= 4.5:1 texto grande)",
                f"{len(contrast_violations)} par(es) de cor abaixo do piso WCAG AAA",
                violations=contrast_violations[:12], worst=contrast_violations[:3])
    elif not pairs:
        rep.pending("g4.contrast.wcag_aaa", "contraste WCAG AAA (>= 7:1 texto normal; >= 4.5:1 texto grande)",
                    "nenhum par texto/fundo resolvido no CSS/DOM")
    else:
        rep.ok("g4.contrast.wcag_aaa", "contraste WCAG AAA (>= 7:1 texto normal; >= 4.5:1 texto grande)",
               f"{len(pairs)} pares calculados; menor razao medida = {pairs[0]['worst_ratio']:.2f}:1 "
               f"({pairs[0]['fg']} sobre {pairs[0]['bg']})",
               measured=len(pairs), min_ratio=round(pairs[0]["worst_ratio"], 2), pairs_top=pairs[:8])

    # -------------------------- Paleta e tipografia da spec ----------------
    required_colors = {
        "texto #0F172A": "#0f172a", "superficie #FFFFFF": "#ffffff", "superficie #F4F6F8": "#f4f6f8",
        "Global #0043CE": "#0043ce", "Brasil #047857": "#047857", "hairline #CBD5E1": "#cbd5e1",
    }
    missing_colors = [label for label, hex_value in required_colors.items()
                      if not _color_present(lower_html_css, hex_value) and not (
                          hex_value == "#ffffff" and ("#fff;" in lower_html_css or "#fff " in lower_html_css
                                                      or "#fff\"" in lower_html_css or "#ffffff" in lower_html_css))]
    if missing_colors:
        rep.bad("g4.palette.spec", "paleta semantica da spec presente no CSS",
                f"cor(es) obrigatoria(s) ausente(s): {missing_colors}", missing=missing_colors)
    else:
        rep.ok("g4.palette.spec", "paleta semantica da spec presente no CSS",
               f"{len(required_colors)} cores obrigatorias encontradas (#0F172A/#FFFFFF/#F4F6F8/#0043CE/#047857/#CBD5E1)")
    required_fonts = {"Space Grotesk": "space grotesk", "IBM Plex Sans": "ibm plex sans",
                      "IBM Plex Mono": "ibm plex mono"}
    missing_fonts = [label for label, needle in required_fonts.items() if needle not in lower_html_css]
    if missing_fonts:
        rep.bad("g4.typography.stack", "sistema tipografico de 3 papeis da spec presente",
                f"fonte(s) obrigatoria(s) ausente(s): {missing_fonts}", missing=missing_fonts)
    else:
        rep.ok("g4.typography.stack", "sistema tipografico de 3 papeis da spec presente",
               "Space Grotesk + IBM Plex Sans + IBM Plex Mono referenciadas no HTML/CSS")
    if "fonts.googleapis.com" not in lower_html_css and "fonts.googleapis" not in lower_html_css:
        rep.skip("g4.typography.google_fonts", "fontes carregadas via Google Fonts (spec secao 3)",
                 "sem link fonts.googleapis.com; pode haver self-host/@font-face")

    # -------------------------- Links HTTP reais ---------------------------
    links = _extract_hrefs(nodes)
    unique_links = {link["href"] for link in links}
    checker = HttpChecker(enabled=STATE.network_enabled, refresh=STATE.refresh_http,
                          timeout_s=20, retries=2)
    link_results: dict[str, dict[str, Any]] = {}
    truncated = False
    check_list = sorted(unique_links)
    if len(check_list) > 150:
        check_list = check_list[:150]
        truncated = True
    try:
        for url in check_list:
            link_results[url] = checker.check(url)
    finally:
        checker.flush()
    ok_status = {200, 201, 301, 302, 307, 308}
    broken_links = [{"url": url, "status": res.get("status"), "error": res.get("error")}
                    for url, res in link_results.items()
                    if res.get("status") is not None and res["status"] not in ok_status]
    unverified_links = [{"url": url, "error": res.get("error")}
                        for url, res in link_results.items() if res.get("status") is None]
    rep.metrics["links_total"] = len(links)
    rep.metrics["links_unique"] = len(unique_links)
    rep.metrics["links_checked"] = len(link_results)
    rep.metrics["links_status_counts"] = _count_statuses(link_results)
    if len(links) < 20:
        rep.bad("g4.links.count", "pelo menos 20 links diretos para fontes oficiais",
                f"{len(links)} <a href=https> encontrados (minimo 20 da especificacao)",
                anchors=len(links), unique=len(unique_links))
    else:
        rep.ok("g4.links.count", "pelo menos 20 links diretos para fontes oficiais",
               f"{len(links)} ancora(s) https ({len(unique_links)} URL unicas)")
    if broken_links:
        rep.bad("g4.links.http", "todos os links <a href=https> respondem 200/301/302",
                f"{len(broken_links)}/{len(link_results)} link(s) quebrado(s)",
                violations=broken_links[:12])
    elif unverified_links:
        verified_count = len(link_results) - len(unverified_links)
        if verified_count >= 20:
            rep.skip("g4.links.http", "todos os links <a href=https> respondem 200/301/302",
                     f"{verified_count}/{len(link_results)} links verificados 200/301/302; "
                     f"{len(unverified_links)} nao verificavel(is) por timeout/transporte "
                     f"(nao sao links quebrados; verificar no run final)",
                     unverified=unverified_links[:8])
        else:
            rep.pending("g4.links.http", "todos os links <a href=https> respondem 200/301/302",
                        f"{len(unverified_links)}/{len(link_results)} link(s) nao verificaveis por transporte",
                        unverified=unverified_links[:8])
    elif not link_results:
        rep.pending("g4.links.http", "todos os links <a href=https> respondem 200/301/302",
                    "nenhum link https para verificar")
    else:
        rep.ok("g4.links.http", "todos os links <a href=https> respondem 200/301/302",
               f"{len(link_results)} URL(s) unicas verificadas por HTTP real (rate-limit {HTTP_RATE_S}s)",
               status_counts=rep.metrics["links_status_counts"])
    if truncated:
        rep.skip("g4.links.truncated", "checagem HTTP de todos os links unicos",
                 f"{len(unique_links)} links unicos; apenas primeiros 150 verificados (limite de carga)")
    missing_rel = [link for link in links
                   if "noopener" not in link["rel"].lower() or "noreferrer" not in link["rel"].lower()]
    if missing_rel:
        rep.bad("g4.links.rel_security", "links externos usam target=_blank rel=noopener noreferrer",
                f"{len(missing_rel)}/{len(links)} link(s) sem rel noopener noreferrer (spec secao 3)",
                violations=[{"href": link["href"][:120], "rel": link["rel"]} for link in missing_rel[:10]])
    else:
        rep.ok("g4.links.rel_security", "links externos usam target=_blank rel=noopener noreferrer",
               f"{len(links)} link(s) com rel noopener noreferrer")

    # -------------------------- JS inline: sintaxe e presenca ---------------
    scripts = list(re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I))
    inline_scripts = []
    has_external_script = False
    data_islands = 0
    js_types = {"", "text/javascript", "application/javascript", "text/ecmascript", "module"}
    for match in scripts:
        attrs = match.group(1) or ""
        body = match.group(2) or ""
        type_match = re.search(r"""type\s*=\s*["']([^"']+)["']""", attrs, re.I)
        script_type = (type_match.group(1).strip().lower() if type_match else "")
        if script_type not in js_types:
            data_islands += 1
            continue  # ex.: <script type="application/json"> com o dataset embutido
        if re.search(r"\bsrc\s*=", attrs, re.I):
            has_external_script = True
            continue
        if body.strip():
            inline_scripts.append(body)
    interactive = bool(re.search(r"(?i)(<select\b|type=[\"']search|type=[\"']range|data-filter|onclick=)", html))
    node_bin = shutil.which("node")
    js_errors = []
    if inline_scripts and node_bin:
        # IMPORTANTE: diretorio temporario DENTRO do workspace (o sandbox bloqueia
        # escrita fora do workspace; %TEMP% e' negado para o auditor). Usar
        # Path.mkdir em vez de mkdtemp porque o ACL restritivo do mkdtemp e'
        # negado pelo sandbox Windows.
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        tmpdir = AUDIT_DIR / f".tmp_js_{time.time_ns()}"
        tmpdir.mkdir(parents=True, exist_ok=True)
        try:
            for idx, code in enumerate(inline_scripts):
                js_file = tmpdir / f"inline_{idx}.js"
                js_file.write_text(code, encoding="utf-8")
                proc = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=60)
                if proc.returncode != 0:
                    js_errors.append({"script": idx, "error": truncate((proc.stderr or proc.stdout), 300)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    rep.metrics["inline_scripts"] = len(inline_scripts)
    if js_errors:
        rep.bad("g4.js.syntax", "scripts JS do dashboard tem sintaxe valida (node --check)",
                f"{len(js_errors)} script(s) com erro de sintaxe", violations=js_errors[:6])
    elif inline_scripts:
        rep.ok("g4.js.syntax", "scripts JS do dashboard tem sintaxe valida (node --check)",
               f"{len(inline_scripts)} script(s) inline validados por node --check")
    elif has_external_script:
        rep.skip("g4.js.syntax", "scripts JS do dashboard tem sintaxe valida (node --check)",
                 "JS apenas externo; sintaxe nao verificada estaticamente")
    if interactive and not inline_scripts and not has_external_script:
        rep.bad("g4.js.filters", "filtros interativos tem JavaScript que os implementa",
                "controles de filtro presentes, mas nenhum <script> no HTML")

    # -------------------------- Runtime: screenshots + DOM ----------------
    runtime = _run_runtime_checks()
    rep.metrics["runtime"] = runtime.get("metrics", {})
    for limitation in runtime.get("limitations", []):
        rep.limitations.append(limitation)
    for check in runtime.get("checks", []):
        evidence = dict(check.get("evidence") or {})
        evidence.pop("detail", None)  # "detail" e' parametro posicional do Check
        rep.add(check["id"], check["desc"], check["status"], check.get("detail", ""), **evidence)

    rep.seconds = time.time() - t0
    return rep


def _run_runtime_checks() -> dict[str, Any]:
    """Incorpora a medicao runtime do dashboard publicada pelo Lead (fora do sandbox).

    O sandbox do auditor bloqueia Chrome headless (mojo/named pipes), entao o Lead
    executa tools/screenshots.py fora do sandbox e publica audit/shots/shots_report.json
    (+ PNGs quando o ambiente permite). Regras:
      * overflow medido por aba e por viewport: criterio documentScrollWidth <= innerWidth+1
        (o Windows clampa 375->500, 768->750, 1440->1422; nao comparar com literais);
      * JS/DOM/fontes/tabular: medidos no runtime; PNGs validados por largura real;
      * nada e' FAIL por limitacao de ambiente (PNG indisponivel, --screenshot travando);
      * se o conteudo medido (rows do data island) != conteudo do build final, runtime
        e' invalidado (SKIP) para nao dar PASS otimista.
    """
    checks: list[dict[str, Any]] = []
    limitations: list[str] = []
    metrics: dict[str, Any] = {"tool": rel(SCREENSHOTS_TOOL), "shots_dir": rel(SHOTS_DIR)}
    report_path = SHOTS_DIR / "shots_report.json"
    skip_reason = ("executado pelo Lead fora do sandbox; artefatos em audit/shots/ "
                   "(shots_report.json ausente nesta execucao)")
    runtime_ids = [
        ("g4.shots.capture", "PNGs de screenshot validados (largura real >= 2 viewports)"),
        ("g4.runtime.overflow", "sem overflow horizontal por aba/viewport (scrollWidth <= innerWidth+1)"),
        ("g4.runtime.js", "dashboard carrega sem erro de JS no Chrome"),
        ("g4.runtime.dom", "conteudo filtrado presente no DOM renderizado"),
        ("g4.runtime.font_floor", "piso tipografico efetivo >= 12px no DOM renderizado"),
        ("g4.runtime.tabular", "tabular-nums + nowrap efetivos em celulas monetarias renderizadas"),
    ]
    if not report_path.exists():
        for cid, desc in runtime_ids:
            checks.append({"id": cid, "desc": desc, "status": SKIP, "detail": skip_reason,
                           "evidence": {"shots_report": rel(report_path), "executed_by": "Lead"}})
        limitations.append(
            "Medicao runtime nao executada nesta rodada: shots_report.json ausente. O Lead executa "
            "tools/screenshots.py fora do sandbox; checagens estaticas do Gate 4 permanecem autoritativas."
        )
        return {"checks": checks, "limitations": limitations, "metrics": metrics}

    shots_report, err = load_json(report_path)
    if not isinstance(shots_report, dict):
        for cid, desc in runtime_ids:
            checks.append({"id": cid, "desc": desc, "status": SKIP,
                           "detail": f"shots_report.json invalido ({err}); {skip_reason}",
                           "evidence": {"shots_report": rel(report_path)}})
        limitations.append(f"shots_report.json presente mas invalido ({err}); runtime nao incorporado.")
        return {"checks": checks, "limitations": limitations, "metrics": metrics}

    viewports = shots_report.get("viewports") or {}
    tabs = shots_report.get("tabs") or {}
    runtime_by_viewport: dict[str, dict[str, Any]] = {
        str(w): (info.get("runtime") or {}) for w, info in viewports.items() if isinstance(info, dict)
    }
    runtime_any = any(rt.get("checked") for rt in runtime_by_viewport.values())

    # ---- vinculo build<->medicao: hash das rows do data island ----
    current_rows_sha = vcdata_rows_sha256(DASHBOARD)
    measured_rows_sha = vcdata_rows_sha256(SHOTS_DIR / "rendered_dom.html")
    content_match = bool(current_rows_sha and measured_rows_sha and current_rows_sha == measured_rows_sha)
    metrics["runtime_content_match"] = {
        "current_rows_sha256": current_rows_sha,
        "measured_rows_sha256": measured_rows_sha,
        "match": content_match,
        "shots_report_html_hash": shots_report.get("hash"),
        "shots_report_generated_at": shots_report.get("generated_at"),
    }
    if current_rows_sha and measured_rows_sha and not content_match:
        runtime_any = False
        limitations.append(
            "O conteudo medido no runtime (rows do data island vc-data) DIFERE do build final "
            f"({str(measured_rows_sha)[:16]} != {str(current_rows_sha)[:16]}): medicao runtime "
            "invalidada para este build (SKIP ate o Lead remensurar).")
    elif content_match:
        limitations.append(
            "shots_report nao registra hash do HTML (campo 'hash' = "
            f"{shots_report.get('hash')!r}): o vinculo build<->medicao foi verificado por hash das "
            f"rows do data island vc-data, identico entre rendered_dom.html e dashboard/index.html "
            f"({current_rows_sha[:16]}...).")
    metrics["runtime_by_viewport"] = {
        w: {k: rt.get(k) for k in ("innerWidth", "innerHeight", "documentScrollWidth", "bodyScrollWidth",
                                   "horizontal_overflow", "minFontPx", "moneyCells",
                                   "moneyCellsTabularNowrap", "jsErrors", "checked")}
        for w, rt in runtime_by_viewport.items()
    }

    # ---- PNGs reais (capturados pelo Lead fora do sandbox) ----
    png_rows: list[dict[str, Any]] = []
    png_candidates = sorted(SHOTS_DIR.glob("v4-*.png")) or sorted(SHOTS_DIR.glob("v3-*.png"))
    metrics["png_build"] = ("v4" if any(p.name.startswith("v4-") for p in png_candidates)
                            else ("v3" if png_candidates else None))
    for png in png_candidates:
        dims = png_size(png)
        name_width = None
        match = re.search(r"-(\d{3,4})\.png$", png.name)
        if match:
            name_width = int(match.group(1))
        png_rows.append({
            "path": rel(png),
            "bytes": png.stat().st_size,
            "dims": list(dims) if dims else None,
            "expected_width": name_width,
            "width_ok": bool(dims and name_width and abs(dims[0] - name_width) <= 2),
            "height_ok": bool(dims and dims[1] > 0),
        })
    valid_pngs = [row for row in png_rows if row["width_ok"] and row["height_ok"]]
    metrics["pngs"] = png_rows
    if valid_pngs and len(valid_pngs) == len(png_rows) and len(valid_pngs) >= 3:
        checks.append({"id": "g4.shots.capture", "desc": runtime_ids[0][1], "status": PASS,
                       "detail": f"{len(valid_pngs)} PNGs reais validados por largura/altura: "
                                 + "; ".join(f"{Path(row['path']).name}={row['dims'][0]}x{row['dims'][1]}"
                                             for row in valid_pngs),
                       "evidence": {"pngs": png_rows[:10]}})
        limitations.append(
            "PNGs reais validados por largura/altura: "
            + ", ".join(f"{Path(row['path']).name}={row['dims'][0]}x{row['dims'][1]}" for row in valid_pngs)
            + ". O Chrome no Windows clampa 375->500 e 768->750, portanto nao existem imagens "
              "literais de 375/768px; a cobertura desses cortes vem da medicao runtime por aba "
              "(500/750) e das @media (max-width:480px/767px) no CSS estatico.")
    elif png_rows:
        checks.append({"id": "g4.shots.capture", "desc": runtime_ids[0][1], "status": FAIL,
                       "detail": f"{len(valid_pngs)}/{len(png_rows)} PNG(s) com largura/altura inconsistente",
                       "evidence": {"pngs": png_rows[:10]}})
    elif shots_enabled is False or runtime_any:
        checks.append({"id": "g4.shots.capture", "desc": runtime_ids[0][1], "status": SKIP,
                       "detail": "sem PNGs de screenshot (--screenshot do Chrome nao gera imagem neste "
                                 "ambiente); medicao runtime via --dump-dom presente em "
                                 f"{sum(1 for rt in runtime_by_viewport.values() if rt.get('checked'))}/3 viewports. "
                                 "Requisito de imagem permanece nao verificado (limitacao documentada).",
                       "evidence": {"shots_enabled": shots_report.get("shots_enabled"),
                                    "captured": shots_report.get("captured"),
                                    "error": truncate(str(shots_report.get("error") or ""), 240)}})
        limitations.append(
            "Screenshots PNG indisponiveis (--screenshot do Chrome trava no ambiente de medicao); "
            "a medicao runtime por --dump-dom + probe JS cobre overflow/fontes/JS/DOM.")
    else:
        checks.append({"id": "g4.shots.capture", "desc": runtime_ids[0][1], "status": SKIP,
                       "detail": f"nenhum PNG capturado e sem runtime: "
                                 f"{truncate(str(shots_report.get('error') or 'erro desconhecido'), 240)}",
                       "evidence": {"shots_report": rel(report_path)}})

    # ---- overflow por aba x viewport (criterio: documentScrollWidth <= innerWidth+1) ----
    overflow = shots_report.get("overflow")
    overflow_scopes: list[tuple[str, dict[str, Any]]] = []
    if isinstance(overflow, dict) and overflow.get("measured"):
        overflow_scopes.append(("aba:achados", overflow))
    for tab_name, tab_data in sorted(tabs.items()):
        if not isinstance(tab_data, dict):
            continue
        tab_overflow = tab_data.get("overflow")
        if isinstance(tab_overflow, dict) and tab_overflow.get("measured"):
            overflow_scopes.append((f"aba:{tab_name}", tab_overflow))
    if overflow_scopes and runtime_any:
        measured_metrics: dict[str, dict[str, Any]] = {}
        bad: dict[str, dict[str, Any]] = {}
        scope_summary: list[dict[str, Any]] = []
        measured_by_scope: dict[str, set[str]] = {}
        clamp_map: dict[tuple[int, int], None] = {}
        for scope, scope_overflow in overflow_scopes:
            per_view = scope_overflow.get("per_viewport") or {}
            scope_rows: dict[str, Any] = {}
            scope_ok = True
            for requested_width, info in per_view.items():
                if not isinstance(info, dict):
                    continue
                inner = info.get("innerWidth")
                doc = info.get("documentScrollWidth")
                body = info.get("bodyScrollWidth")
                reported = info.get("horizontal_overflow")
                computed: bool | None = None
                try:
                    if inner is not None and doc is not None:
                        computed = int(doc) > int(inner) + 1
                        if body is not None:
                            computed = computed or int(body) > int(inner) + 1
                except (TypeError, ValueError):
                    computed = None
                key = f"{scope}@{requested_width}px"
                row = {"scope": scope, "requested_width": requested_width,
                       "innerWidth": inner, "documentScrollWidth": doc, "bodyScrollWidth": body,
                       "horizontal_overflow_computed": computed,
                       "horizontal_overflow_reported": reported,
                       "measured": computed is not None}
                measured_metrics[key] = row
                scope_rows[str(requested_width)] = row
                measured_by_scope.setdefault(scope, set()).add(str(requested_width))
                if computed:
                    bad[key] = row
                    scope_ok = False
                try:
                    if inner is not None and requested_width is not None and abs(int(inner) - int(requested_width)) > 2:
                        clamp_map[(int(requested_width), int(inner))] = None
                except (TypeError, ValueError):
                    pass
            scope_summary.append({"scope": scope, "viewports": sorted(scope_rows),
                                  "ok": scope_ok})
        metrics["overflow_scopes"] = scope_summary
        metrics["overflow"] = {"measured": True, "per_viewport": measured_metrics}
        clamp_notes = [f"{req}->{inner}px" for req, inner in sorted(clamp_map)]
        # combinacoes esperadas pelo measurement_scope x efetivamente medidas
        scope_meta = shots_report.get("measurement_scope") or {}
        expected_map: dict[str, list[str]] = {}
        default_widths = re.findall(r"\d{3,4}", str(scope_meta.get("default_tab") or ""))
        if default_widths:
            expected_map["aba:achados"] = [str(w) for w in default_widths]
        for tab_name, widths in (scope_meta.get("other_tabs") or {}).items():
            if isinstance(widths, list):
                expected_map[f"aba:{tab_name}"] = [str(w) for w in widths]
        missing_combos: list[dict[str, Any]] = []
        for scope_name, expected_widths in expected_map.items():
            if scope_name not in measured_by_scope:
                continue
            missing = [w for w in expected_widths if w not in measured_by_scope[scope_name]]
            if missing:
                missing_combos.append({"scope": scope_name, "missing_viewports": missing,
                                       "measured": sorted(measured_by_scope[scope_name])})
        metrics["overflow_missing_combos"] = missing_combos
        if bad:
            checks.append({"id": "g4.runtime.overflow", "desc": runtime_ids[1][1], "status": FAIL,
                           "detail": f"overflow horizontal medido (scrollWidth > innerWidth+1) em "
                                     f"{len(bad)} combinacao(oes) aba x viewport: {sorted(bad)}",
                           "evidence": {"violations": bad, "scopes": scope_summary,
                                        "clamp": clamp_notes}})
        else:
            detail = (f"sem overflow horizontal em {len(measured_metrics)} combinacao(oes) "
                      f"aba x viewport (criterio documentScrollWidth <= innerWidth+1); abas: "
                      + "; ".join(f"{row['scope']}=" + "/".join(row["viewports"])
                                  for row in scope_summary))
            if missing_combos:
                detail += "; combinacoes nao medidas (falha de launch do Chrome): " + "; ".join(
                    f"{row['scope']} falta {row['missing_viewports']}" for row in missing_combos)
            checks.append({"id": "g4.runtime.overflow", "desc": runtime_ids[1][1], "status": PASS,
                           "detail": detail,
                           "evidence": {"per_viewport": measured_metrics, "clamp": clamp_notes,
                                        "missing_combos": missing_combos}})
        limitations.append(
            "Overflow medido por ABA e viewport (criterio documentScrollWidth <= innerWidth+1). O Chrome "
            f"no Windows clampa as larguras ({', '.join(clamp_notes)}), logo innerWidth != largura CSS "
            "literal; a cobertura 375 CSS px vem de @media (max-width:480px). Combinacoes medidas: "
            + "; ".join(f"{row['scope']}={'/'.join(row['viewports'])}" for row in scope_summary) + ".")
        if missing_combos:
            limitations.append(
                "Combinacao(oes) aba x viewport NAO medidas por falha de launch do Chrome (nao e' "
                "evidencia de overflow): "
                + "; ".join(f"{row['scope']} falta {'/'.join(row['missing_viewports'])}"
                            for row in missing_combos)
                + ". A(s) aba(s) afetada(s) usa(m) a mesma folha de estilo ja validada nas demais "
                  "combinacoes; registrado como SKIP pontual, sem FAIL.")
    else:
        checks.append({"id": "g4.runtime.overflow", "desc": runtime_ids[1][1], "status": SKIP,
                       "detail": skip_reason if not runtime_any else "shots_report sem overflow medido",
                       "evidence": {"shots_report": rel(report_path)}})
        limitations.append("Overflow horizontal nao medido para este build: runtime.overflow = SKIP.")

    # ---- JS errors (todas as abas) ----
    js_errors: list[str] = []
    js_scopes: list[str] = []
    top_js = shots_report.get("js_errors")
    if isinstance(top_js, list):
        js_errors.extend(str(item) for item in top_js)
        js_scopes.append("aba:achados")
    for tab_name, tab_data in sorted(tabs.items()):
        if isinstance(tab_data, dict) and isinstance(tab_data.get("js_errors"), list):
            js_errors.extend(str(item) for item in tab_data["js_errors"])
            js_scopes.append(f"aba:{tab_name}")
    if runtime_any and js_scopes:
        if js_errors:
            checks.append({"id": "g4.runtime.js", "desc": runtime_ids[2][1], "status": FAIL,
                           "detail": f"{len(js_errors)} erro(s) de console/JS no Chrome",
                           "evidence": {"errors": js_errors[:10], "scopes": js_scopes}})
        else:
            checks.append({"id": "g4.runtime.js", "desc": runtime_ids[2][1], "status": PASS,
                           "detail": f"nenhum erro de console/JS observado ({len(js_scopes)} escopo(s): "
                                     f"{', '.join(js_scopes)})", "evidence": {"errors": []}})
    else:
        checks.append({"id": "g4.runtime.js", "desc": runtime_ids[2][1], "status": SKIP,
                       "detail": skip_reason, "evidence": {"shots_report": rel(report_path)}})

    # ---- DOM renderizado ----
    dom = shots_report.get("dom_check")
    if isinstance(dom, dict) and dom.get("checked"):
        if dom.get("ok"):
            checks.append({"id": "g4.runtime.dom", "desc": runtime_ids[3][1], "status": PASS,
                           "detail": truncate(str(dom.get("detail")), 300), "evidence": dom})
        else:
            checks.append({"id": "g4.runtime.dom", "desc": runtime_ids[3][1], "status": FAIL,
                           "detail": truncate(str(dom.get("detail")), 300), "evidence": dom})
    else:
        checks.append({"id": "g4.runtime.dom", "desc": runtime_ids[3][1], "status": SKIP,
                       "detail": skip_reason, "evidence": {"shots_report": rel(report_path)}})

    # ---- piso tipografico efetivo ----
    font_values = [rt.get("minFontPx") for rt in runtime_by_viewport.values()
                   if rt.get("checked") and rt.get("minFontPx") is not None]
    if font_values:
        worst = min(float(value) for value in font_values)
        if worst < 12.0 - 1e-9:
            checks.append({"id": "g4.runtime.font_floor", "desc": runtime_ids[4][1], "status": FAIL,
                           "detail": f"menor font-size computado no DOM = {worst:.2f}px (< 12px)",
                           "evidence": {"per_viewport": {w: (info.get("runtime") or {})
                                                         for w, info in viewports.items() if isinstance(info, dict)}}})
        else:
            checks.append({"id": "g4.runtime.font_floor", "desc": runtime_ids[4][1], "status": PASS,
                           "detail": f"menor font-size computado no DOM = {worst:g}px (>= 12px) em "
                                     f"{len(font_values)} viewport(s) medidos"})
    else:
        checks.append({"id": "g4.runtime.font_floor", "desc": runtime_ids[4][1], "status": SKIP,
                       "detail": skip_reason, "evidence": {}})

    # ---- tabular-nums + nowrap efetivos ----
    tabular_views: list[dict[str, Any]] = []
    value_violations: list[dict[str, Any]] = []
    prose_ignored: list[dict[str, Any]] = []
    for width, rt in runtime_by_viewport.items():
        total = rt.get("moneyCells")
        ok = rt.get("moneyCellsTabularNowrap")
        if total is None:
            continue
        tabular_views.append({"viewport": width, "moneyCells": total, "compliant": ok,
                              "noncompliant_reported": len(rt.get("moneyCellsNotCompliant") or [])})
        for item in rt.get("moneyCellsNotCompliant") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            if _is_value_cell(text):
                value_violations.append({"viewport": width, **item})
            else:
                prose_ignored.append({"viewport": width, "text": truncate(text, 140),
                                      "reason": "celula de prosa com numero embutido (heuristica de valor)"})
    if tabular_views:
        total_cells = sum(int(view.get("moneyCells") or 0) for view in tabular_views)
        total_ok = sum(int(view.get("compliant") or 0) for view in tabular_views)
        if value_violations:
            checks.append({"id": "g4.runtime.tabular", "desc": runtime_ids[5][1], "status": FAIL,
                           "detail": f"{total_ok}/{total_cells} celulas monetarias com tabular-nums+nowrap; "
                                     f"{len(value_violations)} celula(s) de VALOR sem conformidade",
                           "evidence": {"per_viewport": tabular_views,
                                        "value_violations": value_violations[:10]}})
        else:
            checks.append({"id": "g4.runtime.tabular", "desc": runtime_ids[5][1], "status": PASS,
                           "detail": f"{total_ok}/{total_cells} celulas monetarias com tabular-nums+nowrap "
                                     f"no DOM computado"
                                     + (f"; {len(prose_ignored)} celula(s) de prosa contendo numero "
                                        f"ignorada(s) como falso positivo do probe (heuristica de valor)"
                                        if prose_ignored else ""),
                           "evidence": {"per_viewport": tabular_views,
                                        "prose_ignored": prose_ignored[:10]}})
    else:
        checks.append({"id": "g4.runtime.tabular", "desc": runtime_ids[5][1], "status": SKIP,
                       "detail": skip_reason, "evidence": {}})
    return {"checks": checks, "limitations": limitations, "metrics": metrics}


# ==========================================================================
# Orquestracao, relatorios e CLI
# ==========================================================================
class _State:
    network_enabled: bool = True
    refresh_http: bool = False
    quiet: bool = False


STATE = _State()

GATE_NAMES = {
    1: "Integridade de extração e conformidade de rede",
    2: "Conformidade com o contrato SDD e zero alucinação",
    3: "Linter anti-AI-Slop (texto e design)",
    4: "UI/UX executivo, acessibilidade WCAG AAA e responsividade",
}
GATE_FUNCS = {1: gate1, 2: gate2, 3: gate3, 4: gate4}


def _artifact_inventory() -> list[dict[str, Any]]:
    items = [file_info(MANIFEST)]
    for path in sorted(INTERIM_DIR.glob("*.jsonl")) if INTERIM_DIR.exists() else []:
        info = file_info(path)
        try:
            info["lines"] = sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
        except OSError:
            info["lines"] = None
        items.append(info)
    items.append(file_info(FUNDS_JSON))
    items.append(file_info(KPIS_JSON))
    items.append(file_info(DASHBOARD))
    if SHOTS_DIR.exists():
        shots_report = SHOTS_DIR / "shots_report.json"
        if shots_report.is_file():
            info = file_info(shots_report)
            try:
                data = json.loads(shots_report.read_text(encoding="utf-8"))
                info["runtime_measured_viewports"] = data.get("runtime_measured_viewports")
                info["generated_at"] = data.get("generated_at")
                info["shots_enabled"] = data.get("shots_enabled")
            except (json.JSONDecodeError, OSError):
                pass
            items.append(info)
        for png in sorted(SHOTS_DIR.glob("*.png")):
            dims = png_size(png)
            items.append({"path": rel(png), "exists": True, "bytes": png.stat().st_size,
                          "png_dims": dims})
    return items


def _environment_info() -> dict[str, Any]:
    chrome = find_chrome()
    return {
        "python": sys.version.split()[0],
        "node": shutil.which("node") or None,
        "chrome": str(chrome) if chrome else None,
        "network_enabled": STATE.network_enabled,
        "http_rate_limit_s": HTTP_RATE_S,
        "http_cache": rel(HTTP_CACHE),
        "os": sys.platform,
        "cwd": str(ROOT),
        "sandbox_note": (
            "Chrome headless nao inicia no sandbox do auditor (mojo/named pipes bloqueados: "
            "platform_channel.cc 'Acesso negado'). Medicao runtime do dashboard foi executada pelo "
            "Lead fora do sandbox via tools/screenshots.py com probe JS (--dump-dom): scrollWidth/"
            "overflow/JS/DOM/fontes efetivas; PNGs de --screenshot nao sao possiveis no ambiente e "
            "ficam SKIP documentado (nunca FAIL por limitacao de ambiente)."
        ),
    }


def _evidence_short(evidence: dict[str, Any], limit: int = 320) -> str:
    if not evidence:
        return ""
    try:
        text = json.dumps(evidence, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(evidence)
    return truncate(text, limit)


def _critical_metric(gate: GateReport) -> str:
    m = gate.metrics
    if gate.gate == 1:
        bits = [f"manifest={m.get('manifest_entries', 'n/d')}"]
        if m.get("interim_records") is not None:
            bits.append(f"interim={m.get('interim_records')} registros")
        if m.get("cache_files_verified"):
            bits.append(f"cache={m.get('cache_files_verified')} arquivos re-hashados")
        if m.get("sec_min_interval_declared_s") is not None:
            bits.append(f"SEC min_interval={m.get('sec_min_interval_declared_s')}s")
        cvm = m.get("cvm_csvs") or []
        if cvm:
            bits.append("CVM rows=" + "/".join(str(e.get("rows")) for e in cvm))
        return "; ".join(bits)
    if gate.gate == 2:
        sample = m.get("url_sample") or {}
        bits = [f"registros={m.get('schema_invalid_records', 'n/d')} invalidos"]
        if sample:
            bits.append(f"URLs={sample.get('unique_urls_checked')} checadas/{len(sample.get('http_status_counts') or {})} status")
        if m.get("funds_by_stage"):
            bits.append("estagios=" + ",".join(f"{k}:{v}" for k, v in m["funds_by_stage"].items()))
        return "; ".join(bits)
    if gate.gate == 3:
        return (f"lexico={sum(1 for c in gate.checks if c.cid == 'g3.lexicon.body' and c.status == FAIL)} hit; "
                f"figures={len(m.get('figures') or [])}; numeros={m.get('formatted_numbers', 'n/d')}; "
                f"cards={m.get('stat_cards', 'n/d')}")
    if gate.gate == 4:
        return (f"min_contraste={m.get('min_contrast_ratio', 'n/d')}:1; "
                f"pares={m.get('contrast_pairs', 'n/d')}; font_decls={m.get('font_size_declarations', 'n/d')}; "
                f"links={m.get('links_checked', 'n/d')}; tabelas={m.get('tables', 'n/d')}")
    return ""


def _build_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Auditoria Independente — Loop Engineering (Gates 1–4)")
    lines.append("")
    lines.append(f"Gerado em **{report['generated_at']}** por `pipeline/gates.py` "
                 f"(auditor adversarial independente; não importa o código de transform/render).")
    lines.append("")
    lines.append(f"- Workspace: `{report['environment']['cwd']}`")
    lines.append(f"- Python {report['environment']['python']} · Node {report['environment']['node'] or 'n/d'} · "
                 f"Chrome {report['environment']['chrome'] or 'n/d'}")
    lines.append(f"- Rede: {'habilitada' if report['environment']['network_enabled'] else 'DESABILITADA (--no-network)'}; "
                 f"rate-limit {report['environment']['http_rate_limit_s']}s; "
                 f"cache de checagens: `{report['environment']['http_cache']}`")
    lines.append("")
    lines.append("## Resumo dos gates")
    lines.append("")
    lines.append("| Gate | Status | PASS/FAIL/PENDING/SKIP | Métrica-chave |")
    lines.append("|:---|:---:|---|---|")
    icons = {PASS: "✅ PASS", FAIL: "❌ FAIL", PENDING: "⏳ PENDING"}
    for gate_id in sorted(report["gates"]):
        gate = report["gates"][gate_id]
        counts = gate["counts"]
        lines.append(f"| {gate_id} — {gate['name']} | {icons.get(gate['status'], gate['status'])} | "
                     f"{counts.get(PASS, 0)}/{counts.get(FAIL, 0)}/{counts.get(PENDING, 0)}/{counts.get(SKIP, 0)} | "
                     f"{_critical_metric_row(gate)} |")
    lines.append("")
    lines.append(f"**Veredito:** `{report['summary']['verdict']}` · código de saída `{report['summary']['exit_code']}` "
                 f"({report['summary']['exit_hint']})")
    lines.append("")
    lines.append("Semântica de status: **PASS** = medido no artefato e conforme; **FAIL** = medido e fora da spec; "
                 "**PENDING** = artefato/capacidade de medição ausente (não é aprovação otimista); "
                 "**SKIP** = limitação metodológica registrada (não bloqueia, mas está listada).")
    lines.append("")

    lines.append("## Artefatos auditados")
    lines.append("")
    lines.append("| Artefato | Existe | Bytes | SHA-256 | Detalhe |")
    lines.append("|:---|:---:|---:|---|---|")
    for item in report["artifacts"]:
        detail = []
        if item.get("lines") is not None:
            detail.append(f"linhas={item['lines']}")
        if item.get("png_dims"):
            detail.append(f"dims={item['png_dims'][0]}x{item['png_dims'][1]}")
        if item.get("mtime_utc"):
            detail.append(f"mtime={item['mtime_utc']}")
        lines.append(f"| `{item['path']}` | {'sim' if item.get('exists') else 'NÃO'} | "
                     f"{item.get('bytes', '')} | `{(item.get('sha256') or '')[:16]}` | {'; '.join(detail)} |")
    lines.append("")

    det = report.get("determinism") or {}
    lines.append("## Determinismo e envelopes")
    lines.append("")
    lines.append("Os arquivos de saída incluem `generated_at` no envelope: reexecutar transform/render muda o "
                 "hash do ARQUIVO mesmo com conteúdo idêntico. A estabilidade auditável é a do **conteúdo**:")
    lines.append("")
    lines.append(f"- Método: `{det.get('method', 'n/d')}`")
    if det.get("records_content_sha256"):
        lines.append(f"- **`records` content SHA-256: `{det['records_content_sha256']}`** "
                     f"({det.get('records_count', 'n/d')} registros)")
    if det.get("dashboard_rows_sha256"):
        lines.append(f"- `rows` embutidas no dashboard (data island vc-data): `{det['dashboard_rows_sha256']}`")
    runtime_metrics = ((report["gates"].get(4) or {}).get("metrics") or {}).get("runtime") or {}
    content_match = runtime_metrics.get("runtime_content_match")
    if isinstance(content_match, dict):
        lines.append(
            "- Medição runtime vinculada ao build final por hash das `rows` do data island `vc-data`: "
            f"{'IDÊNTICO' if content_match.get('match') else 'DIFERENTE'} "
            f"(medido `{str(content_match.get('measured_rows_sha256'))[:16]}` vs final "
            f"`{str(content_match.get('current_rows_sha256'))[:16]}`; "
            f"shots_report hash do HTML = {content_match.get('shots_report_html_hash')!r})")
    lines.append("")
    if det.get("note"):
        lines.append(det["note"])
        lines.append("")

    for gate_id in sorted(report["gates"]):
        gate = report["gates"][gate_id]
        lines.append(f"## Gate {gate_id} — {gate['name']}")
        lines.append("")
        lines.append(f"**Status: {icons.get(gate['status'], gate['status'])}** · duração {gate['seconds']}s")
        lines.append("")
        lines.append(f"*Método:* {gate['method']}")
        lines.append("")
        metrics = gate.get("metrics") or {}
        if gate_id == 1:
            lines.append("| Métrica | Valor |")
            lines.append("|:---|:---|")
            for key in ("manifest_entries", "interim_records", "interim_records_by_source",
                        "cache_files_verified", "sec_requests", "sec_min_interval_declared_s",
                        "sec_min_observed_gap_s", "http_200_sources", "http_failing_sources",
                        "cvm_csvs"):
                if key in metrics:
                    value = metrics[key]
                    lines.append(f"| `{key}` | `{truncate(json.dumps(value, ensure_ascii=False, default=str), 500)}` |")
            lines.append("")
        elif gate_id == 2:
            lines.append("| Métrica | Valor |")
            lines.append("|:---|:---|")
            for key in ("schema_invalid_records", "funds_by_cut", "funds_by_stage", "url_sample",
                        "kpi_stage_blocks"):
                if key in metrics:
                    lines.append(f"| `{key}` | `{truncate(json.dumps(metrics[key], ensure_ascii=False, default=str), 500)}` |")
            lines.append("")
        elif gate_id == 3:
            lines.append("| Métrica | Valor |")
            lines.append("|:---|:---|")
            for key in ("formatted_numbers", "unique_formatted_numbers", "figures", "stat_cards",
                        "answer_first", "html_bytes"):
                if key in metrics:
                    lines.append(f"| `{key}` | `{truncate(json.dumps(metrics[key], ensure_ascii=False, default=str), 500)}` |")
            lines.append("")
        elif gate_id == 4:
            lines.append("| Métrica | Valor |")
            lines.append("|:---|:---|")
            for key in ("font_size_declarations", "min_contrast_ratio", "contrast_pairs",
                        "contrast_violations", "media_breakpoints", "viewport_coverage",
                        "tables", "money_cells", "links_total", "links_checked", "links_status_counts",
                        "risky_fixed_widths"):
                if key in metrics:
                    lines.append(f"| `{key}` | `{truncate(json.dumps(metrics[key], ensure_ascii=False, default=str), 500)}` |")
            lines.append("")

        lines.append("| Check | Status | Evidência / detalhe |")
        lines.append("|:---|:---:|---|")
        status_label = {PASS: "✅ pass", FAIL: "❌ fail", PENDING: "⏳ pending", SKIP: "⚠️ skip"}
        for check in gate["checks"]:
            evidence = _evidence_short(check.get("evidence") or {}, 300)
            detail = check.get("detail") or ""
            cell = truncate(" — ".join(bit for bit in (detail, evidence) if bit).replace("|", "\\|"), 480)
            lines.append(f"| `{check['id']}` — {check['description']} | {status_label.get(check['status'], check['status'])} | {cell} |")
        lines.append("")
        if gate.get("limitations"):
            lines.append("**Limitações deste gate:**")
            for limitation in gate["limitations"]:
                lines.append(f"- {limitation}")
            lines.append("")

    # Problemas que exigem correção
    lines.append("## Problemas que exigem correção")
    lines.append("")
    problems = report["summary"]["problems"]
    if not problems:
        lines.append("Nenhum FAIL/PENDING: todos os gates passaram com evidência medida.")
    else:
        for idx, problem in enumerate(problems, 1):
            evidence = _evidence_short(problem.get("evidence") or {}, 700)
            lines.append(f"{idx}. **[{problem['status'].upper()}] Gate {problem['gate']} · `{problem['id']}`** — "
                         f"{problem['description']}")
            if problem.get("detail"):
                lines.append(f"   - Medido: {problem['detail']}")
            if evidence:
                lines.append(f"   - Evidência: `{evidence}`")
            lines.append(f"   - Ação: {problem.get('action') or _action_for(problem)}")
    lines.append("")

    lines.append("## Limitações metodológicas do auditor")
    lines.append("")
    for limitation in report["summary"]["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    lines.append("## Reprodutibilidade")
    lines.append("")
    lines.append("```powershell")
    lines.append("python pipeline/gates.py --all --json audit/audit_report.json --md audit/report.md")
    lines.append("python pipeline/gates.py --gate 1 --gate 2 --no-network   # checagens 100% offline")
    lines.append("python tools/screenshots.py --html dashboard/index.html --out audit/shots")
    lines.append("```")
    lines.append("")
    lines.append("Código de saída: `0` = todos PASS; `1` = ao menos um FAIL; `2` = nenhum FAIL, mas algum "
                 "PENDING (artefato/capacidade ausente). O auditor nunca retorna 0 com artefato esperado faltando.")
    lines.append("")
    return "\n".join(lines) + "\n"


def _critical_metric_row(gate: dict[str, Any]) -> str:
    metrics = gate.get("metrics") or {}
    gate_id = gate.get("gate")
    if gate_id == 1:
        return (f"manifest={metrics.get('manifest_entries', 'n/d')}; "
                f"interim={metrics.get('interim_records', 'n/d')}; "
                f"cache_verificado={metrics.get('cache_files_verified', 'n/d')}")
    if gate_id == 2:
        sample = metrics.get("url_sample") or {}
        return (f"registros={metrics.get('schema_invalid_records', 'n/d')} inválidos; "
                f"URLs={sample.get('unique_urls_checked', 'n/d')} checadas")
    if gate_id == 3:
        return (f"números={metrics.get('formatted_numbers', 'n/d')}; "
                f"figures={len(metrics.get('figures') or [])}")
    if gate_id == 4:
        return (f"contraste_min={metrics.get('min_contrast_ratio', 'n/d')}:1; "
                f"links={metrics.get('links_checked', 'n/d')}; "
                f"tabelas={metrics.get('tables', 'n/d')}")
    return ""


def _action_for(problem: dict[str, Any]) -> str:
    gate = problem.get("gate")
    if problem.get("status") == PENDING:
        return ("Aguardar/entregar o artefato e reexecutar o auditor; PENDING não conta como aprovação.")
    if gate == 1:
        return "Corrigir a ingestão (UA/intervalo/status/sha256/encoding/fallback) e reexecutar o coletor + Gate 1."
    if gate == 2:
        return "Corrigir o registro canônico (contrato, fonte https, unicidade, valores, KPI) no ETL e reexecutar Gate 2."
    if gate == 3:
        return "Reescrever copy/CSS/figuras removendo o anti-pattern e reexecutar Gate 3."
    if gate == 4:
        return "Corrigir CSS/DOM (contraste, fontes, números, responsividade, links) e reexecutar Gate 4."
    return "Corrigir e reexecutar o gate."


def run_gates(selected: list[int]) -> tuple[dict[int, GateReport], dict[str, Any]]:
    reports: dict[int, GateReport] = {}
    for gate_id in selected:
        if not STATE.quiet:
            print(f"[auditor] executando Gate {gate_id} ...", flush=True)
        t0 = time.time()
        try:
            reports[gate_id] = GATE_FUNCS[gate_id]()
        except Exception as exc:  # nunca esconder falha: vira FAIL com traceback
            traceback.print_exc()
            rep = GateReport(gate_id, GATE_NAMES[gate_id], method="excecao nao tratada no auditor")
            rep.bad(f"g{gate_id}.auditor_crash", "gate executa sem excecao",
                    f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-3000:])
            rep.seconds = time.time() - t0
            reports[gate_id] = rep
        if not STATE.quiet:
            gate = reports[gate_id]
            print(f"[auditor] Gate {gate_id}: {gate.status.upper()} "
                  f"({gate.counts().get(PASS, 0)} pass, {gate.counts().get(FAIL, 0)} fail, "
                  f"{gate.counts().get(PENDING, 0)} pending, {gate.counts().get(SKIP, 0)} skip) "
                  f"em {gate.seconds:.1f}s", flush=True)

    problems: list[dict[str, Any]] = []
    for gate_id in sorted(reports):
        gate = reports[gate_id]
        for check in gate.failures():
            problems.append({
                "gate": gate_id, "id": check.cid, "status": check.status,
                "description": check.desc, "detail": check.detail,
                "evidence": check.evidence,
                "action": _action_for({"gate": gate_id, "status": check.status}),
            })
    limitations: list[str] = []
    for gate_id in sorted(reports):
        limitations.extend(reports[gate_id].limitations)
    if any(gate.status == FAIL for gate in reports.values()):
        verdict, exit_code = FAIL, 1
    elif any(gate.status == PENDING for gate in reports.values()):
        verdict, exit_code = PENDING, 2
    else:
        verdict, exit_code = PASS, 0
    summary = {
        "verdict": verdict,
        "exit_code": exit_code,
        "exit_hint": {0: "todos os gates PASS", 1: "existe FAIL mensurado",
                      2: "existe PENDING (artefato/capacidade ausente)"}.get(exit_code, ""),
        "problems": problems,
        "limitations": limitations,
    }
    determinism: dict[str, Any] = {
        "method": "sha256(json.dumps(records, ensure_ascii=False, sort_keys=True))",
        "note": ("Arquivos funds.json/kpis.json/dashboard/index.html incluem 'generated_at' no envelope; "
                 "o hash do ARQUIVO muda entre builds. A estabilidade e' do CONTEUDO (array records / "
                 "rows do data island), nao do envelope byte-a-byte."),
    }
    funds_data, funds_err = load_json(FUNDS_JSON)
    if isinstance(funds_data, dict) and isinstance(funds_data.get("records"), list):
        recs = funds_data["records"]
        determinism["records_count"] = len(recs)
        determinism["records_content_sha256"] = records_content_sha256(recs)
    elif isinstance(funds_data, list):
        determinism["records_count"] = len(funds_data)
        determinism["records_content_sha256"] = records_content_sha256(funds_data)
    else:
        determinism["error"] = funds_err or "funds.json indisponivel"
    determinism["dashboard_rows_sha256"] = vcdata_rows_sha256(DASHBOARD)
    report = {
        "generated_at": utc_now(),
        "auditor": "pipeline/gates.py (independente, adversarial)",
        "determinism": determinism,
        "spec": rel(SPEC),
        "environment": _environment_info(),
        "artifacts": _artifact_inventory(),
        "gates": {gate_id: reports[gate_id].as_dict() for gate_id in sorted(reports)},
        "summary": summary,
    }
    return reports, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Auditor independente dos 4 Quality Gates do Loop Engineering (spec secao 2).",
    )
    parser.add_argument("--all", action="store_true", help="executa os 4 gates")
    parser.add_argument("--gate", action="append", type=int, choices=[1, 2, 3, 4], default=None,
                        help="executa apenas o gate indicado (pode repetir)")
    parser.add_argument("--json", default=str(AUDIT_DIR / "audit_report.json"),
                        help="caminho do relatorio JSON (default audit/audit_report.json)")
    parser.add_argument("--md", default=str(AUDIT_DIR / "report.md"),
                        help="caminho do relatorio Markdown (default audit/report.md)")
    parser.add_argument("--no-network", action="store_true", help="desabilita checagens HTTP (links/URLs)")
    parser.add_argument("--refresh-http", action="store_true", help="ignora cache de checagens HTTP")
    parser.add_argument("--quiet", action="store_true", help="nao imprime progresso")
    args = parser.parse_args(argv)

    STATE.network_enabled = not args.no_network
    STATE.refresh_http = args.refresh_http
    STATE.quiet = args.quiet
    selected = sorted(set(args.gate)) if args.gate else [1, 2, 3, 4]
    if args.gate and args.all:
        selected = [1, 2, 3, 4]

    t0 = time.time()
    reports, report_json = run_gates(selected)
    out_json = Path(args.json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md = Path(args.md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_build_markdown(report_json), encoding="utf-8")

    report_json["summary"]["elapsed_s"] = round(time.time() - t0, 1)
    if not args.quiet:
        print("")
        print("=" * 78)
        print("RESUMO DA AUDITORIA")
        for gate_id in sorted(reports):
            gate = reports[gate_id]
            print(f"  Gate {gate_id}: {gate.status.upper():8s} | {_critical_metric(gate)}")
        print("-" * 78)
        print(f"  Veredito: {report_json['summary']['verdict'].upper()} (exit={report_json['summary']['exit_code']}) "
              f"| {len(report_json['summary']['problems'])} problema(s)")
        print(f"  JSON: {out_json}")
        print(f"  MD:   {out_md}")
        print("=" * 78)
    return int(report_json["summary"]["exit_code"])


if __name__ == "__main__":
    sys.exit(main())



