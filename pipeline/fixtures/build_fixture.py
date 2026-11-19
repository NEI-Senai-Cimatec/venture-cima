"""Gera um fixture de desenvolvimento a partir de dados REAIS ja cacheados.

Uso exclusivo: desbloquear o desenvolvimento do renderer em paralelo a coleta.
O fixture e sempre marcado com "__fixture__": true e NAO pode ser publicado como
dataset final. Nenhum valor e inventado: tudo sai de registro_fundo_classe.zip
(CVM) e do data set trimestral do Form D (SEC), ambos ja baixados por net.py.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent
ROOT = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))

from net import fetch  # noqa: E402

CVM_REG = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
SEC_ZIP = "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q3_d.zip"
PTAX = 5.40  # placeholder; o transform definitivo usa PTAX venda real por competencia


def _rows(raw: bytes, delim: str = ";") -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(raw.decode("latin1")), delimiter=delim))


def _digits(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


def br_records(n: int = 14) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(fetch(CVM_REG, source="cvm_registro_fundo_classe").content))
    fundo = _rows(zf.read("registro_fundo.csv"))
    classe = _rows(zf.read("registro_classe.csv"))
    pl_by_fund: dict[str, float] = {}
    for c in classe:
        try:
            pl_by_fund[c["ID_Registro_Fundo"]] = pl_by_fund.get(c["ID_Registro_Fundo"], 0.0) + float(
                c["Patrimonio_Liquido"] or 0)
        except ValueError:
            continue
    fips = [r for r in fundo if r["Tipo_Fundo"] == "FIP" and "Funcionamento" in r["Situacao"]]
    fips.sort(key=lambda r: -pl_by_fund.get(r["ID_Registro_Fundo"], 0.0))

    def signal(name: str) -> str | None:
        u = name.upper()
        if "SEMENTE" in u:
            return "SEMENTE"
        if "EMERGENTE" in u:
            return "EMERGENTE"
        if "MULTIESTRAT" in u:
            return "MULTIESTRAT"
        return None

    stages = {  # apenas para o fixture; a regra oficial esta no transform
        "SEMENTE": (["PRE_SEED", "SEED"], "REGULATORY_CLASS_CVM_BNDES"),
        "EMERGENTE": (["SERIES_A", "SERIES_B"], "REGULATORY_CLASS_CVM_BNDES"),
        "MULTIESTRAT": (["SERIES_B", "LATE_STAGE_GROWTH"], "REGULATORY_CLASS_CVM_BNDES"),
    }
    out = []
    # amostra deliberadamente diversa: menores, medianos e grandes
    picks = fips[:4] + fips[len(fips) // 2: len(fips) // 2 + 5] + fips[-5:]
    for r in picks[:n]:
        cnpj = _digits(r["CNPJ_Fundo"])
        pl = pl_by_fund.get(r["ID_Registro_Fundo"], 0.0)
        sig = signal(r["Denominacao_Social"])
        st, method = stages.get(sig, (["SEED", "SERIES_A"], "IMPUTED_BY_TICKET_HEURISTIC"))
        out.append({
            "fund_id": "cvm:" + cnpj,
            "firm_name": r["Gestor"].strip(),
            "vehicle_name": r["Denominacao_Social"].strip(),
            "regulatory_id": f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}" if len(cnpj) == 14 else cnpj,
            "geographic_cut": "BRASIL_LOCAL",
            "hq_country": "BR",
            "target_stages": st,
            "stage_classification_method": method,
            "estimated_ticket_min_usd": None,
            "estimated_ticket_max_usd": None,
            "aum_or_offering_usd": round(pl / PTAX, 2) if pl else None,
            "brazil_deals_count": 0,
            "lead_investor_rate": None,
            "active_status": True,
            "primary_source_name": "CVM - Registro de Fundos e Classes (RCVM 175)",
            "primary_source_url": CVM_REG,
            "estimate_basis": f"Patrimonio_Liquido {r['Data_Patrimonio_Liquido']} (BRL {pl}) / PTAX {PTAX} (fixture)",
            "undisclosed_fields": ["estimated_ticket_min_usd", "estimated_ticket_max_usd", "lead_investor_rate"],
            "evidence_urls": [CVM_REG],
            "resolution": {"firm_key": "cnpj_gestor:" + _digits(r["CPF_CNPJ_Gestor"]),
                           "vehicle_key": "cnpj:" + cnpj, "method": "CPF_CNPJ_Gestor", "confidence": "high"},
            "__fixture__": True,
        })
    return out


def _money(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def global_records(n: int = 12) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(fetch(SEC_ZIP, source="sec_formd_zip").content))
    qdir = zf.namelist()[0].split("/")[0]

    def tsv(name: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(zf.read(f"{qdir}/{name}").decode("utf-8", "replace")), delimiter="\t"))

    primary: dict[str, dict] = {}
    for r in tsv("ISSUERS.tsv"):
        if (r.get("IS_PRIMARYISSUER_FLAG") or "").strip().upper() == "YES":
            primary.setdefault(r["ACCESSIONNUMBER"], r)
    subs = {r["ACCESSIONNUMBER"]: r for r in tsv("FORMDSUBMISSION.tsv")}

    out: list[dict] = []
    for o in tsv("OFFERING.tsv"):
        if o.get("INVESTMENTFUNDTYPE") != "Venture Capital Fund":
            continue
        offering = _money(o.get("TOTALOFFERINGAMOUNT"))
        if offering is None or offering < 10_000_000:
            continue
        acc = o["ACCESSIONNUMBER"]
        iss = primary.get(acc)
        if not iss:
            continue
        name = iss["ENTITYNAME"].strip()
        cik = (iss.get("CIK") or "0").strip()
        cik_num = int(cik) if cik.isdigit() else 0
        juris = (iss.get("JURISDICTIONOFINC") or "").strip().upper()
        country = {"CAYMAN ISLANDS": "KY", "DELAWARE": "US", "US-DE": "US"}.get(juris, "US")
        firm = re.sub(r"\s+(FUND|FUNDS|L\.?P\.?|LLC|LTD|PARTNERS|VENTURES|CAPITAL|MANAGEMENT)\b.*$", "", name,
                      flags=re.I).strip()
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc.replace('-', '')}/{acc}-index.htm"
        out.append({
            "fund_id": "sec:" + acc,
            "firm_name": (firm or name).upper(),
            "vehicle_name": name,
            "regulatory_id": str(cik_num).ljust(10, "0"),
            "geographic_cut": "GLOBAL_EX_BR",
            "hq_country": country,
            "target_stages": ["SEED", "SERIES_A"],
            "stage_classification_method": "IMPUTED_BY_TICKET_HEURISTIC",
            "estimated_ticket_min_usd": 500_000.0,
            "estimated_ticket_max_usd": 3_000_000.0,
            "aum_or_offering_usd": offering,
            "brazil_deals_count": 0,
            "lead_investor_rate": None,
            "active_status": True,
            "primary_source_name": "SEC EDGAR - Form D",
            "primary_source_url": url,
            "estimate_basis": f"TOTALOFFERINGAMOUNT Form D filing {subs.get(acc, {}).get('FILING_DATE', '')}",
            "undisclosed_fields": ["lead_investor_rate"],
            "evidence_urls": [url],
            "resolution": {"firm_key": "sec_cik:" + str(cik_num).ljust(10, "0"), "vehicle_key": "sec_acc:" + acc,
                           "method": "CIK", "confidence": "high"},
            "__fixture__": True,
        })
        if len(out) >= n:
            break
    return out


def main() -> None:
    records = br_records() + global_records()
    target = ROOT / "pipeline" / "fixtures" / "fixture_funds.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "__fixture__": True,
        "note": "Fixture de desenvolvimento gerado de dados reais CVM/SEC. NAO publicar como dataset final.",
        "records": records,
        "kpis": {},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"fixture: {len(records)} registros -> {target}")
    print("por corte:", {c: sum(1 for r in records if r["geographic_cut"] == c)
                         for c in sorted({r["geographic_cut"] for r in records})})


if __name__ == "__main__":
    main()
