"""Coletor SEC Form D Data Sets -> veiculos de Venture Capital (data/interim/sec_formd_vehicles.jsonl).

Fonte oficial: https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets
Cada trimestre e um ZIP com TSVs relacionais; usamos:
  FORMDSUBMISSION.tsv (ACCESSIONNUMBER, FILING_DATE, SUBMISSIONTYPE)
  OFFERING.tsv       (INVESTMENTFUNDTYPE == 'Venture Capital Fund'; valores e datas)
  ISSUERS.tsv        (CIK, ENTITYNAME, cidade/estado-pais, jurisdicao de incorporacao)

Contrato de saida: envelope de contract/interim_record.schema.json, source_id="sec_formd",
entity_kind="fund", uma linha por ACCESSIONNUMBER/offering VC, com payload congelado pelo Lead.

Uso:
  python pipeline/collect_sec.py                       # 2024q4..2026q3 (8 ZIPs)
  python pipeline/collect_sec.py --start-quarter 2025q1 --end-quarter 2026q3
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from net import fetch  # noqa: E402

DATA_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets"
OUT_PATH = ROOT / "data" / "interim" / "sec_formd_vehicles.jsonl"
SOURCES_PATH = ROOT / "data" / "interim" / "global_sources.json"

DEFAULT_START = "2024q4"
DEFAULT_END = "2026q3"

# Fallback caso a pagina da SEC mude/esteja indisponivel no momento da coleta.
FALLBACK_URLS = {
    "2026q3": "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q3_d.zip",
    "2026q2": "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q2_d.zip",
    "2026q1": "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2026q1_d.zip",
    "2025q4": "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q4_d.zip",
    "2025q3": "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q3_d.zip",
    "2025q2": "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q2_d.zip",
    "2025q1": "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q1_d.zip",
    "2024q4": "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2024q4_d.zip",
}

VC_FUND_TYPE = "Venture Capital Fund"

US_STATES = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT",
    "DELAWARE", "DISTRICT OF COLUMBIA", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS",
    "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS",
    "MICHIGAN", "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA",
    "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO", "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA",
    "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA",
    "SOUTH DAKOTA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON",
    "WEST VIRGINIA", "WISCONSIN", "WYOMING", "PUERTO RICO", "GUAM", "VIRGIN ISLANDS, U.S.",
    "AMERICAN SAMOA", "NORTHERN MARIANA ISLANDS",
}
# Territorios/estados associados aos EUA (ISO-3166-1 alpha-2).
US_TERRITORY_ISO = {"PUERTO RICO": "PR", "GUAM": "GU", "AMERICAN SAMOA": "AS",
                    "NORTHERN MARIANA ISLANDS": "MP", "VIRGIN ISLANDS, U.S.": "VI"}

CA_PROVINCES = {
    "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK",
    "NEWFOUNDLAND AND LABRADOR", "NOVA SCOTIA", "ONTARIO", "PRINCE EDWARD ISLAND",
    "QUEBEC", "SASKATCHEWAN", "YUKON", "NUNAVUT", "NORTHWEST TERRITORIES",
}

COUNTRY_ISO = {
    "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US", "US": "US",
    "CAYMAN ISLANDS": "KY", "VIRGIN ISLANDS, BRITISH": "VG", "BRITISH VIRGIN ISLANDS": "VG",
    "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB", "SCOTLAND": "GB",
    "LUXEMBOURG": "LU", "IRELAND": "IE", "BERMUDA": "BM", "CANADA": "CA",
    "SWITZERLAND": "CH", "GERMANY": "DE", "FRANCE": "FR", "NETHERLANDS": "NL",
    "SINGAPORE": "SG", "HONG KONG": "HK", "JAPAN": "JP", "AUSTRALIA": "AU",
    "ISRAEL": "IL", "SPAIN": "ES", "ITALY": "IT", "SWEDEN": "SE", "DENMARK": "DK",
    "NORWAY": "NO", "FINLAND": "FI", "BELGIUM": "BE", "AUSTRIA": "AT",
    "PORTUGAL": "PT", "BRAZIL": "BR", "INDIA": "IN", "CHINA": "CN", "SOUTH KOREA": "KR",
    "KOREA": "KR", "TAIWAN": "TW", "NEW ZEALAND": "NZ", "MARSHALL ISLANDS": "MH",
    "BAHAMAS": "BS", "ANGUILLA": "AI", "MONACO": "MC", "LIECHTENSTEIN": "LI",
    "MALTA": "MT", "CYPRUS": "CY", "ESTONIA": "EE", "LATVIA": "LV", "LITHUANIA": "LT",
    "POLAND": "PL", "CZECH REPUBLIC": "CZ", "CZECHIA": "CZ", "HUNGARY": "HU",
    "ROMANIA": "RO", "GREECE": "GR", "TURKEY": "TR", "UNITED ARAB EMIRATES": "AE",
    "SAUDI ARABIA": "SA", "RUSSIA": "RU", "UKRAINE": "UA", "MEXICO": "MX",
    "ARGENTINA": "AR", "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE", "URUGUAY": "UY",
    "PANAMA": "PA", "COSTA RICA": "CR", "DOMINICAN REPUBLIC": "DO", "ECUADOR": "EC",
    "VENEZUELA": "VE", "SOUTH AFRICA": "ZA", "NIGERIA": "NG", "KENYA": "KE",
    "EGYPT": "EG", "MOROCCO": "MA", "GHANA": "GH", "MAURITIUS": "MU",
    "SEYCHELLES": "SC", "INDONESIA": "ID", "MALAYSIA": "MY", "THAILAND": "TH",
    "VIETNAM": "VN", "PHILIPPINES": "PH", "PAKISTAN": "PK", "BANGLADESH": "BD",
    "SRI LANKA": "LK", "SLOVENIA": "SI", "SLOVAKIA": "SK", "CROATIA": "HR",
    "BULGARIA": "BG", "ICELAND": "IS", "GIBRALTAR": "GI", "JERSEY": "JE",
    "GUERNSEY": "GG", "ISLE OF MAN": "IM", "LIECHTENSTEIN ": "LI", "ANDORRA": "AD",
    "CHANNEL ISLANDS": "GG", "NETHERLANDS ANTILLES": "AN", "ARUBA": "AW",
    "CURACAO": "CW", "ST KITTS AND NEVIS": "KN", "ANTIGUA AND BARBUDA": "AG",
    "BARBADOS": "BB", "TRINIDAD AND TOBAGO": "TT", "BELIZE": "BZ", "PANAMA ": "PA",
}
# Codigos de estado/pais de 2 letras usados pelo EDGAR que nao sao estados dos EUA.
EDGAR_NONUS_CODES = {"E9": "KY", "N4": "BM", "A1": "AN", "B0": "VG", "K3": "KY", "G0": "GB", "H0": "HK",
                     "L3": "IL", "M2": "LU", "T3": "CH"}

# Abreviacoes de estados/territorios dos EUA (STATEORCOUNTRY usa 2 letras).
US_STATE_ABBR = {
    "AL": "US", "AK": "US", "AZ": "US", "AR": "US", "CA": "US", "CO": "US", "CT": "US", "DE": "US",
    "DC": "US", "FL": "US", "GA": "US", "HI": "US", "ID": "US", "IL": "US", "IN": "US", "IA": "US",
    "KS": "US", "KY": "US", "LA": "US", "ME": "US", "MD": "US", "MA": "US", "MI": "US", "MN": "US",
    "MS": "US", "MO": "US", "MT": "US", "NE": "US", "NV": "US", "NH": "US", "NJ": "US", "NM": "US",
    "NY": "US", "NC": "US", "ND": "US", "OH": "US", "OK": "US", "OR": "US", "PA": "US", "RI": "US",
    "SC": "US", "SD": "US", "TN": "US", "TX": "US", "UT": "US", "VT": "US", "VA": "US", "WA": "US",
    "WV": "US", "WI": "US", "WY": "US", "PR": "PR", "VI": "VI", "GU": "GU", "AS": "AS", "MP": "MP",
}

# ISO-3166-1 alpha-2 (conjunto oficial usado para validar o codigo final).
ISO2_CODES = set(
    """AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT
    BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER
    ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID
    IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV
    LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR
    NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL
    SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC
    VE VG VI VN VU WF WS YE YT ZA ZM ZW""".split()
)

AMOUNT_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
QUARTER_RE = re.compile(r"form-d-data-sets/(\d{4}q[1-4])_d(?:_0)?\.zip$")


def quarter_key(q: str) -> tuple[int, int]:
    return (int(q[:4]), int(q[5]))


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().upper())


def name_to_iso(value: str) -> str | None:
    """Converte nome de estado/provincia/pais (como no Form D) em ISO-3166-1 alpha-2."""
    name = normalize_name(value)
    if not name:
        return None
    if name in US_STATES:
        return US_TERRITORY_ISO.get(name, "US")
    if name.endswith(", CANADA"):
        name = name[: -len(", CANADA")].strip()
    if name.endswith(", USA") or name.endswith(", UNITED STATES"):
        name = name.split(",")[0].strip()
    if name in CA_PROVINCES:
        return "CA"
    if name in US_STATES:
        return US_TERRITORY_ISO.get(name, "US")
    if name in COUNTRY_ISO:
        return COUNTRY_ISO[name]
    if name in US_STATE_ABBR:
        return US_STATE_ABBR[name]
    if name in ISO2_CODES:
        return name
    return None


def resolve_country(jurisdiction: str, state_or_country: str, state_desc: str) -> str | None:
    for raw in (jurisdiction, state_desc, state_or_country):
        iso = name_to_iso(raw)
        if iso:
            return iso
    soc = normalize_name(state_or_country)
    if soc.startswith("US-") and len(soc) >= 5:
        return "US"
    if soc in EDGAR_NONUS_CODES:
        return EDGAR_NONUS_CODES[soc]
    return None


def parse_amount(raw: str) -> tuple[float | None, str]:
    """Retorna (valor_usd, motivo). motivo: 'value' | 'indefinite' | 'blank' | 'unparsed'."""
    value = (raw or "").strip().replace(",", "")
    if not value:
        return None, "blank"
    if value.lower() in ("indefinite", "indefinido", "indef"):
        return None, "indefinite"
    if AMOUNT_RE.match(value):
        return float(value), "value"
    return None, "unparsed"


def parse_bool(raw: str) -> bool | None:
    value = (raw or "").strip().lower()
    if value in ("true", "t", "yes", "y", "1"):
        return True
    if value in ("false", "f", "no", "n", "0"):
        return False
    return None


def parse_us_date(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def read_tsv(zf: zipfile.ZipFile, member: str) -> list[dict[str, str]]:
    text = zf.read(member).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [
        {(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None}
        for row in reader
        if any((v or "").strip() for v in row.values())
    ]


def resolve_quarter_urls() -> dict[str, str]:
    try:
        page = fetch(DATA_PAGE, source="sec_formd")
        if page.status == 200:
            found: dict[str, str] = {}
            for href in re.findall(r'href="([^"]*\.zip)"', page.text):
                match = QUARTER_RE.search(href)
                if match and match.group(1) not in found:
                    found[match.group(1)] = "https://www.sec.gov" + href
            if found:
                return found
    except Exception:  # noqa: BLE001 - fallback deterministico abaixo
        pass
    return dict(FALLBACK_URLS)


def build_records(quarter: str, data: bytes) -> tuple[list[dict], dict]:
    """Processa um ZIP trimestral e devolve (registros, estatisticas)."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    prefix = next((n.rsplit("/", 1)[0] for n in names if n.endswith("FORMDSUBMISSION.tsv")), quarter.upper() + "_d")

    submissions = {
        row["ACCESSIONNUMBER"]: row
        for row in read_tsv(zf, f"{prefix}/FORMDSUBMISSION.tsv")
        if row.get("ACCESSIONNUMBER")
    }
    issuers: dict[str, list[dict[str, str]]] = {}
    for row in read_tsv(zf, f"{prefix}/ISSUERS.tsv"):
        accn = row.get("ACCESSIONNUMBER")
        if accn:
            issuers.setdefault(accn, []).append(row)
    offerings = read_tsv(zf, f"{prefix}/OFFERING.tsv")

    stats = {
        "offerings_total": len(offerings),
        "vc_offerings": 0,
        "vc_amount_numeric": 0,
        "vc_amount_indefinite": 0,
        "vc_amount_missing": 0,
        "with_country": 0,
        "skipped_no_submission": 0,
        "skipped_no_issuer": 0,
    }
    records = []
    for off in offerings:
        if (off.get("INVESTMENTFUNDTYPE") or "").strip() != VC_FUND_TYPE:
            continue
        accn = off.get("ACCESSIONNUMBER", "").strip()
        if not accn:
            continue
        stats["vc_offerings"] += 1
        submission = submissions.get(accn)
        issuer_rows = issuers.get(accn, [])
        if not submission:
            stats["skipped_no_submission"] += 1
            continue
        if not issuer_rows:
            stats["skipped_no_issuer"] += 1
            continue

        primary = next(
            (r for r in issuer_rows if (r.get("IS_PRIMARYISSUER_FLAG") or "").strip().upper() == "YES"),
            None,
        )
        if primary is None:
            primary = min(issuer_rows, key=lambda r: int((r.get("ISSUER_SEQ_KEY") or "999999").strip() or 999999))

        cik_raw = (primary.get("CIK") or "").strip()
        cik = cik_raw.zfill(10) if cik_raw.isdigit() else None
        if cik and len(cik) > 10:  # CIK nao tem mais de 10 digitos
            cik = cik[-10:]

        total_offering, reason_offering = parse_amount(off.get("TOTALOFFERINGAMOUNT", ""))
        total_sold, _ = parse_amount(off.get("TOTALAMOUNTSOLD", ""))
        total_remaining, _ = parse_amount(off.get("TOTALREMAINING", ""))
        minimum_investment, _ = parse_amount(off.get("MINIMUMINVESTMENTACCEPTED", ""))

        if total_offering is not None:
            stats["vc_amount_numeric"] += 1
        elif reason_offering == "indefinite":
            stats["vc_amount_indefinite"] += 1
        else:
            stats["vc_amount_missing"] += 1

        jurisdiction = (primary.get("JURISDICTIONOFINC") or "").strip()
        state_or_country = (primary.get("STATEORCOUNTRY") or "").strip()
        state_desc = (primary.get("STATEORCOUNTRYDESCRIPTION") or "").strip()
        country = resolve_country(jurisdiction, state_or_country, state_desc)
        if country:
            stats["with_country"] += 1

        submission_type = (submission.get("SUBMISSIONTYPE") or "").strip().upper()
        is_amendment = submission_type.endswith("/A") or (off.get("ISAMENDMENT") or "").strip().lower() == "true"
        over100 = parse_bool(off.get("OVER100RECIPIENTFLAG", ""))
        sale_date = parse_us_date(off.get("SALE_DATE", ""))
        filing_date = parse_us_date(submission.get("FILING_DATE", ""))
        previous = (off.get("PREVIOUSACCESSIONNUMBER") or "").strip() or None

        _undisclosed = []
        if total_offering is None:
            _undisclosed.append("total_offering_amount_usd")
        if total_sold is None:
            _undisclosed.append("total_amount_sold_usd")
        if total_remaining is None:
            _undisclosed.append("total_remaining_usd")
        if minimum_investment is None:
            _undisclosed.append("minimum_investment_usd")
        if sale_date is None:
            _undisclosed.append("sale_date")
        if filing_date is None:
            _undisclosed.append("filing_date")
        if over100 is None:
            _undisclosed.append("over100_recipient_flag")
        if previous is None:
            _undisclosed.append("previous_accession_number")
        if cik is None:
            _undisclosed.append("cik")
        if not country:
            _undisclosed.append("country_hint")

        cik_path = str(int(cik)) if cik and cik.isdigit() else (cik or "")
        source_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_path}/"
            f"{accn.replace('-', '')}/{accn}-index.htm"
        )

        records.append(
            {
                "record_id": accn,
                "source_id": "sec_formd",
                "source_url": source_url,
                "retrieved_from": "",  # preenchido pelo chamador (URL do ZIP)
                "entity_kind": "fund",
                "country_hint": country,
                "payload": {
                    "accession_number": accn,
                    "cik": cik,
                    "entity_name": (primary.get("ENTITYNAME") or "").strip() or None,
                    "jurisdiction_of_inc": jurisdiction or None,
                    "state_or_country": state_or_country or None,
                    "city": (primary.get("CITY") or "").strip() or None,
                    "investment_fund_type": VC_FUND_TYPE,
                    "industry_group_type": (off.get("INDUSTRYGROUPTYPE") or "").strip() or None,
                    "total_offering_amount_usd": total_offering,
                    "total_amount_sold_usd": total_sold,
                    "total_remaining_usd": total_remaining,
                    "minimum_investment_usd": minimum_investment,
                    "sale_date": sale_date,
                    "filing_date": filing_date,
                    "is_amendment": is_amendment,
                    "over100_recipient_flag": over100,
                    "previous_accession_number": previous,
                    "_undisclosed": _undisclosed,
                },
            }
        )
    return records, stats


def update_sources(entries: list[dict], limitations: list[str]) -> None:
    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = {"sources": [], "wikidata_qid_used": {}, "limitations": []}
    if SOURCES_PATH.exists():
        try:
            doc.update(json.loads(SOURCES_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    names = {e["name"] for e in entries}
    sources = [s for s in doc.get("sources", []) if s.get("name") not in names]
    sources.extend(entries)
    doc["sources"] = sources
    # Cada coletor e dono das suas limitacoes (prefixo "[SEC Form D]"); remove texto legado/obsoleto.
    merged = [
        l for l in (doc.get("limitations") or [])
        if str(l).startswith(("[SEC Form D]", "[Wikidata]", "[BCB PTAX]"))
        and not str(l).startswith("[SEC Form D]")
    ]
    merged.extend(limitations)
    doc["limitations"] = merged
    SOURCES_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Coleta veiculos VC dos Form D Data Sets da SEC.")
    parser.add_argument("--start-quarter", default=DEFAULT_START, help="ex.: 2024q4")
    parser.add_argument("--end-quarter", default=DEFAULT_END, help="ex.: 2026q3")
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--force", action="store_true", help="ignora cache do net.py")
    args = parser.parse_args()

    start, end = quarter_key(args.start_quarter), quarter_key(args.end_quarter)
    if start > end:
        raise SystemExit("--start-quarter deve ser <= --end-quarter.")

    available = resolve_quarter_urls()
    quarters = sorted(
        (q for q in available if start <= quarter_key(q) <= end),
        key=quarter_key,
    )
    if not quarters:
        raise SystemExit(f"Nenhum trimestre encontrado entre {args.start_quarter} e {args.end_quarter}.")

    records: list[dict] = []
    seen_accessions: set[str] = set()
    duplicates = 0
    source_entries: list[dict] = []
    totals = {
        "offerings_total": 0,
        "vc_offerings": 0,
        "vc_amount_numeric": 0,
        "vc_amount_indefinite": 0,
        "vc_amount_missing": 0,
        "with_country": 0,
        "skipped_no_submission": 0,
        "skipped_no_issuer": 0,
    }
    per_quarter: dict[str, dict] = {}

    for quarter in quarters:
        url = available[quarter]
        fetched = fetch(url, source="sec_formd", force=args.force)
        quarter_records, stats = build_records(quarter, fetched.content)
        for record in quarter_records:
            record["retrieved_from"] = url
        new_records = [r for r in quarter_records if r["record_id"] not in seen_accessions]
        duplicates += len(quarter_records) - len(new_records)
        seen_accessions.update(r["record_id"] for r in new_records)
        records.extend(new_records)
        for key in totals:
            totals[key] += stats[key]
        pct = (stats["vc_amount_numeric"] + stats["vc_amount_indefinite"]) / stats["vc_offerings"] * 100 if stats["vc_offerings"] else 0.0
        per_quarter[quarter] = {**stats, "written": len(new_records), "pct_amount_disclosed": round(pct, 1)}
        source_entries.append(
            {
                "name": f"SEC Form D Data Set {quarter} ({url.rsplit('/', 1)[-1]})",
                "url": url,
                "status": fetched.status,
                "bytes": len(fetched.content),
                "sha256": hashlib.sha256(fetched.content).hexdigest(),
                "records": len(new_records),
                "notes": (
                    f"{stats['offerings_total']} offerings no trimestre; {stats['vc_offerings']} VC; "
                    f"{stats['vc_amount_numeric']} com TOTALOFFERINGAMOUNT numerico, "
                    f"{stats['vc_amount_indefinite']} 'Indefinite', {stats['vc_amount_missing']} sem valor; "
                    f"{stats['with_country']} com pais resolvido."
                ),
            }
        )
        print(f"[sec] {quarter}: {stats['vc_offerings']} VC offerings, {len(new_records)} linhas, "
              f"{pct:.1f}% amount numerico/indefinite")

    records.sort(key=lambda r: (r["payload"].get("filing_date") or "", r["record_id"]))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    output_bytes = out.stat().st_size
    output_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    disclosed = totals["vc_amount_numeric"] + totals["vc_amount_indefinite"]
    pct_total = disclosed / totals["vc_offerings"] * 100 if totals["vc_offerings"] else 0.0
    print(f"[sec] TOTAL: {len(records)} veiculos VC em {len(quarters)} trimestres "
          f"({totals['vc_offerings']} offerings VC, {duplicates} duplicatas ignoradas)")
    print(f"[sec] amount numerico: {totals['vc_amount_numeric']}, Indefinite: {totals['vc_amount_indefinite']}, "
          f"sem valor: {totals['vc_amount_missing']} -> {pct_total:.2f}% divulgado")
    print(f"[sec] pais resolvido: {totals['with_country']}/{len(records)}; "
          f"sem submission: {totals['skipped_no_submission']}, sem issuer: {totals['skipped_no_issuer']}")
    print(f"[sec] saida: {out} ({output_bytes} bytes, sha256={output_sha[:16]}...)")

    source_entries.append(
        {
            "name": "SEC Form D Data Sets - veiculos de VC (agregado 2024q4..2026q3)",
            "url": DATA_PAGE,
            "status": 200,
            "bytes": output_bytes,
            "sha256": output_sha,
            "records": len(records),
            "notes": (
                f"{len(records)} veiculos VC de {len(quarters)} ZIPs trimestrais (sem dedupe por CIK: "
                f"1 linha por ACCESSIONNUMBER). TOTALOFFERINGAMOUNT: {totals['vc_amount_numeric']} numericos, "
                f"{totals['vc_amount_indefinite']} 'Indefinite' -> {pct_total:.2f}% divulgado. "
                f"sha256/bytes acima referem-se ao JSONL final."
            ),
        }
    )
    limitations = [
        "[SEC Form D] Os campos OVER100RECIPIENTFLAG (OFFERING) e OVER100PERSONSFLAG "
        "(FORMDSUBMISSION) vem vazios em 100% das linhas VC em 2024q4..2026q3; "
        "payload.over100_recipient_flag=null e listado em payload._undisclosed.",
        "[SEC Form D] 1 registro possui JURISDICTIONOFINC/STATEORCOUNTRY='X0' (codigo de pais do "
        "EDGAR nao mapeado de forma inequivoca); country_hint=null + _undisclosed.",
        "[SEC Form D] 'Indefinite' em TOTALOFFERINGAMOUNT/TOTALREMAINING nao e convertido em numero; "
        "o campo USD equivalente fica null e entra em _undisclosed (valores so em USD absolutos, como na fonte).",
        "[SEC Form D] D e D/A sao tratados como offerings distintos (ACCESSIONNUMBER diferente); a "
        "deduplicacao por CIK/gestora e responsabilidade da etapa de transformacao.",
    ]
    update_sources(source_entries, limitations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
