# Ingestão Global — SEC Form D, Wikidata SPARQL e câmbio oficial BCB PTAX

Documentação de reprodutibilidade dos coletores da **camada global** do pipeline de
mapeamento de fundos de Venture Capital. Todos os dados são reais, extraídos de fontes
públicas oficiais, e cada linha dos JSONL aponta para a URL da fonte primária.

| Artefato | Coletor | Fonte oficial |
| :--- | :--- | :--- |
| `data/interim/sec_formd_vehicles.jsonl` | `pipeline/collect_sec.py` | [SEC Form D Data Sets](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets) |
| `data/interim/wikidata_firms.jsonl` | `pipeline/collect_wikidata.py` | [Wikidata Query Service (SPARQL)](https://query.wikidata.org/) |
| `data/interim/fx_brl_usd.json` | `pipeline/collect_fx.py` | [BCB PTAX — CotacaoDolarPeriodo](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/) |
| `data/interim/global_sources.json` | os três coletores | metadados de proveniência (bytes, sha256, contagens, limitações) |

O envelope de cada linha segue `contract/interim_record.schema.json`
(`record_id`, `source_id`, `source_url`, `entity_kind`, `payload`), com o payload
congelado pelo Lead do projeto.

---

## 1. Contrato de rede e auditoria (Gate 1)

Toda e qualquer chamada HTTP passa por `pipeline/net.py` (não modificado):

- **User-Agent declarado**: `PesquisaAcademicaVC/1.0 (pesquisador@universidade.edu.br)`
  em 100% das requisições do módulo global (374 entradas no `data/manifest.jsonl`).
- **Rate-limit SEC**: intervalo mínimo de `0.2 s` por host (limite SEC = 10 req/s; margem 2x).
  Todas as 61 requisições a `www.sec.gov` registradas têm `min_interval_s >= 0.2`.
- **Retry/backoff** com descompressão gzip handler e cache por SHA-1 da URL em `data/raw/`.
- **Manifesto auditável** `data/manifest.jsonl`: `ts_utc`, `source`, `url`, `status`,
  `bytes`, `sha256`, `user_agent`, `min_interval_s`, `gate1_ok`.
- Nenhum `curl`, `Invoke-WebRequest` ou processo externo foi usado nas coletas finais.

Resultado da auditoria do manifesto para as fontes desta camada:

| `source` no manifesto | requisições | HTTP 200 (`gate1_ok`) | falhas |
| :--- | ---: | ---: | :--- |
| `sec_formd` | 44 | 44 | 0 |
| `bcb_ptax` | 5 | 5 | 0 |
| `wikidata_sparql` | 203 | 195 | 8 × HTTP 504 (queries exploratórias pesadas, descartadas) |
| `probe` (sondagem) | 122 | 122 | 0 |

As 8 falhas HTTP 504 em `wikidata_sparql` são de **sondagens descartadas** (varreduras
`FILTER(CONTAINS(schema:description, ...))` sobre toda a base e busca de classes por rótulo),
que não alimentam nenhum artefato. A descoberta final usa apenas consultas leves por
classe/indústria e `VALUES` por QID.

---

## 2. SEC Form D Data Sets — veículos de Venture Capital

### 2.1 Fonte e janela coletada

A página oficial lista ZIPs trimestrais (`..._d.zip`) com TSVs relacionais. Foram baixados
**8 trimestres consecutivos (2024q4 a 2026q3)**, identificados a partir dos `href` da própria
página (com fallback determinístico de URL no script):

| Trimestre | ZIP oficial | bytes | offerings VC | TOTALOFFERINGAMOUNT numérico | `Indefinite` |
| :--- | :--- | ---: | ---: | ---: | ---: |
| 2024q4 | `https://www.sec.gov/files/structureddata/data/form-d-data-sets/2024q4_d.zip` | 3.278.867 | 1.907 | 1.583 | 324 |
| 2025q1 | `https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q1_d.zip` | 3.519.235 | 1.976 | 1.606 | 370 |
| 2025q2 | `https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q2_d.zip` | 3.171.828 | 1.924 | 1.614 | 310 |
| 2025q3 | `https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q3_d.zip` | 3.358.303 | 2.148 | 1.756 | 392 |
| 2025q4 | `https://www.sec.gov/files/structureddata/data/form-d-data-sets/2025q4_d.zip` | 3.490.382 | 2.454 | 2.064 | 390 |
| 2026q1 | `https://www.sec.gov/files/structureddata/data/form-d-data-sets/2026q1_d.zip` | 3.648.126 | 2.547 | 2.119 | 428 |
| 2026q2 | `https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q2_d.zip` | 3.805.493 | 2.983 | 2.421 | 562 |
| 2026q3 | `https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q3_d.zip` | 3.652.689 | 2.872 | 2.259 | 613 |
| **Total** | 8 ZIPs | 28.924.923 | **18.811** | **15.422** | **3.389** |

Os `sha256` de cada ZIP estão em `data/interim/global_sources.json` (`sources[]`).

### 2.2 Parsing e regras de transformação

TSVs internos usados, todos unidos por `ACCESSIONNUMBER`:

- `FORMDSUBMISSION.tsv` → `FILING_DATE`, `SUBMISSIONTYPE` (D / D/A), flags do emissor;
- `OFFERING.tsv` → `INVESTMENTFUNDTYPE`, `INDUSTRYGROUPTYPE`, valores e datas da oferta;
- `ISSUERS.tsv` → `CIK`, `ENTITYNAME`, `CITY`, `STATEORCOUNTRY`,
  `STATEORCOUNTRYDESCRIPTION`, `JURISDICTIONOFINC` e flag de emissor primário.

Regras aplicadas (uma linha por `ACCESSIONNUMBER`, sem dedupe por CIK — responsabilidade do
transform):

1. **Filtro**: `INVESTMENTFUNDTYPE == "Venture Capital Fund"` (2.872 só no 2026Q3; 18.811 no total).
2. **Emissor primário**: `IS_PRIMARYISSUER_FLAG == "YES"`; senão menor `ISSUER_SEQ_KEY`.
3. **CIK**: 10 dígitos com zeros à esquerda (`0001609775`), como no TSV.
4. **Valores monetários**: em **USD absolutos** (a fonte já é USD). `Indefinite` ou vazio →
   `null` + nome do campo em `payload._undisclosed`; nunca convertido por inferência.
5. **Jurisdição/país** (`country_hint`, envelope): ISO-3166-1 alpha-2, com precedência
   `JURISDICTIONOFINC` → `STATEORCOUNTRYDESCRIPTION` → `STATEORCOUNTRY`. A normalização cobre
   estados dos EUA → `US`, províncias canadenses → `CA`, territórios (`PR`, `VI`, `GU`…) e
   códigos de país do EDGAR (`E9`→`KY`, `L3`→`IL`, `N4`→`BM`…), validando o resultado contra o
   conjunto oficial ISO-3166-1 alpha-2. `payload.jurisdiction_of_inc` e
   `payload.state_or_country` preservam o valor bruto da fonte.
6. **Proveniência** (`source_url`): `https://www.sec.gov/Archives/edgar/data/{CIK sem zeros}/
   {ACCESSION sem hífens}/{ACCESSION}-index.htm`. Foram verificadas 4 URLs por HTTP GET
   (uma por trimestre distinto): todas HTTP 200 e conteúdo "Form D".
7. **Datas**: `FILING_DATE` no formato `30-SEP-2026` convertido para ISO; `SALE_DATE` já ISO.

`payload` congelado: `accession_number`, `cik`, `entity_name`, `jurisdiction_of_inc`,
`state_or_country`, `city`, `investment_fund_type`, `industry_group_type`,
`total_offering_amount_usd`, `total_amount_sold_usd`, `total_remaining_usd`,
`minimum_investment_usd`, `sale_date`, `filing_date`, `is_amendment`,
`over100_recipient_flag` (veja limitações), `previous_accession_number`, `_undisclosed`.

### 2.3 Cobertura e distribuição

- **18.811 veículos** VC reais; **100%** com `TOTALOFFERINGAMOUNT` numérico ou `Indefinite`
  (15.422 numéricos, 3.389 `Indefinite`; 0 vazios).
- **18.810/18.811** com país resolvido: `US` 18.244 · `KY` 251 · `VG` 99 · `CA` 44 · `LU` 38 ·
  `IN` 25 · `GB` 22 · `SG` 14 · `IL` 13 · `JE` 12 (26 países/territórios no total).
- Soma dos `TOTALOFFERINGAMOUNT` numéricos: **US$ 326.087.181.500** (não somado com
  `Indefinite`).
- `is_amendment=true` em 2.472 offerings (D/A contados como offerings distintos).

---

## 3. Wikidata SPARQL — gestoras de Venture Capital

### 3.1 Correção do QID da spec (obrigatória e documentada)

A spec cita `wd:Q1961622` como "Venture Capital Firm". A verificação direta
(`action=wbgetentities&ids=Q1961622`) retorna:

- rótulo (en): **"NUW"**; descrição: **"Wikimedia disambiguation page"**;
- `P31 = Q4167410` → **página de desambiguação**, não uma classe de empresa.

A busca `action=wbsearchentities&search=venture capital firm&language=en` retorna
**Q3487908 — "Venture capital firm"**, que é a classe efetivamente usada nas declarações
`P31` das gestoras (ex.: Sequoia Capital Q1852025, Accel Q2578125, Greylock Q1392009,
Kleiner Perkins Q4346838). **O coletor usa Q3487908**; a correção também é registrada em
`global_sources.json → wikidata_qid_used.nota_correcao`.

### 3.2 Descoberta (todos os QIDs são reais e rastreáveis)

| Canal | Consulta | QIDs/linhas |
| :--- | :--- | ---: |
| Classe VC firm | `?f wdt:P31 wd:Q3487908` (SPARQL paginado `LIMIT 200 OFFSET n`) | 355 |
| Indústria + PE | `{ ?f wdt:P452 wd:Q219409 } UNION { ?f wdt:P31/wdt:P279* wd:Q5418962 }` (paginado) | 168 |
| Categorias Wikimedia | `Category:Venture capital firms` + 16 subcategorias por país (en.wikipedia, recursivo até 2 níveis) → QIDs via `prop=pageprops` | 327 |
| **União única** | deduplicação por QID | **640** |

A base curada do Wikidata para "venture capital firm" é pequena na data da coleta
(2026-11-18): **355 instâncias da classe Q3487908, 270 delas com `P17`**; `P452=Q219409`
tem 69 itens. Por isso a união com a classe `Q5418962` ("private equity firm") e com as
categorias Wikimedia foi necessária para cobertura — sempre com QIDs reais, sem sintetizar
entidades. Categorias Wikimedia são usadas **apenas como canal de descoberta**; todos os
atributos vêm de declarações do Wikidata via SPARQL.

### 3.3 Enriquecimento e saída

Enriquecimento em batches de `VALUES` no endpoint `https://query.wikidata.org/sparql`, com
`SERVICE wikibase:label` (en, pt, es, fr, de) e as propriedades:

- `P17` país → `P297` (ISO-3166-1 alpha-2) → `payload.country_iso2` / `country_qid`;
- `P856` website → `payload.website`;
- `P571` fundação → `payload.founded` (data canônica do repositório, `YYYY-MM-DD`);
- `P159` sede → `payload.hq_location_qid` / `hq_label`;
- `P31` tipos → usados para excluir humanos, listas e artigos.

Critério de inclusão: **somente itens com `P17` resolvido** (gestoras com país), conforme o
aceite. Resultado: **407 gestoras**, de **42 países/territórios** — `US` 236 · `GB` 32 ·
`SE` 12 · `FR` 11 · `IL` 10 · `DE` 10 · `CN` 9 · `CA` 7 · `JP` 7 · `FI` 5 (demais 28 países).
Cobertura de atributos: website 351 (86,2%), fundação 341 (83,8%), sede 348 (85,5%),
descrição 373 (91,6%), `country_qid` 407 (100%). Itens descobertos sem `P17`: 215;
excluídos por tipo (humano/lista/desambiguação/artigo): 18.

`payload` congelado: `qid`, `label`, `country_iso2`, `country_qid`, `website`, `founded`,
`hq_location_qid`, `hq_label`, `description`, `_undisclosed`.
`source_url` por linha: `https://www.wikidata.org/wiki/{QID}`.

Notas de fidelidade à fonte: `country_iso2` reflete **P17** (país da entidade no Wikidata) e
pode divergir da sede declarada em `P159` — preservada em `hq_location_qid`/`hq_label` (ex.:
Q2578125 Accel tem P17=GB e sede em Palo Alto, ambos mantidos como no repositório). Websites
são mantidos exatamente como declarados (podem ser `http://`).

---

## 4. Câmbio oficial BRL/USD — BCB PTAX

Endpoint OData público do Banco Central (sem autenticação):

```
https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/CotacaoDolarPeriodo(
  dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)
  ?@dataInicial='01-01-2024'&@dataFinalCotacao='11-18-2026'
  &$format=json&$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao
```

Saída `data/interim/fx_brl_usd.json` no shape exigido:

```json
{"source": "BCB PTAX", "source_url": "https://olinda.bcb.gov.br/...", "pair": "BRL/USD",
 "retrieved_at": "2026-11-18T16:...", 
 "series": [{"date": "2024-01-02", "cotacao_compra": 4.891, "cotacao_venda": 4.8916}, "..."],
 "last": {"date": "2026-11-17", "cotacao_venda": 5.7851}}
```

- **724 cotações de venda**, de **2024-01-02** a **2026-11-17** (todos os dias úteis com
  boletim PTAX; feriados/fins de semana não têm cotação).
- Série ordenada por data e sem duplicatas; `last` = cotação de venda mais recente.
- Regra de uso no transform (definida pelo Lead): para cada competência, usar a cotação de
  venda mais recente com `date <= data` (BRL→USD = valor BRL ÷ `cotacao_venda`).

---

## 5. Artefatos finais, bytes e sha256

| Arquivo | Linhas/registros | bytes | sha256 |
| :--- | ---: | ---: | :--- |
| `data/interim/sec_formd_vehicles.jsonl` | 18.811 | 18.855.508 | `6e2b18d41a4f2918ff6444543d7ac80ab447f301e33ef348d03ebcc1ed7c48c8` |
| `data/interim/wikidata_firms.jsonl` | 407 | 210.736 | `379531919ec496274aedd0ea0d6b7101ad41ba629ea7969da3c483c92b66dd36` |
| `data/interim/fx_brl_usd.json` | 724 cotações | 69.087 | `f4b8ca698b02590e4ee68d762a87f98f984a62381fcbb42dc90934d1844cf1f5` |
| `data/interim/global_sources.json` | 11 fontes + 11 limitações | — | ver arquivo (muda a cada coleta: `retrieved_at`) |

`global_sources.json` contém 11 entradas em `sources[]` (8 ZIPs trimestrais, 1 agregado SEC,
1 Wikidata, 1 BCB PTAX), a seção `wikidata_qid_used` (QIDs usados + correção) e `limitations[]`.

## 6. Limitações conhecidas

Estão registradas de forma estruturada em `data/interim/global_sources.json → limitations[]`:

1. **[SEC Form D]** `OVER100RECIPIENTFLAG` (OFFERING) e `OVER100PERSONSFLAG`
   (FORMDSUBMISSION) vêm **vazios em 100% das linhas VC** em 2024q4..2026q3;
   `over100_recipient_flag = null` + `_undisclosed` (não inferimos "false").
2. **[SEC Form D]** 1 registro com `JURISDICTIONOFINC/STATEORCOUNTRY = "X0"` (código de país
   do EDGAR não mapeado sem ambiguidade) → `country_hint = null`.
3. **[SEC Form D]** `Indefinite` em `TOTALOFFERINGAMOUNT`/`TOTALREMAINING` não é convertido;
   campo USD equivalente fica `null` + `_undisclosed`.
4. **[SEC Form D]** D e D/A são offerings distintos; dedupe por CIK/gestora é do transform.
5. **[Wikidata]** Q1961622 da spec é desambiguação; QID correto Q3487908 (ver §3.1).
6. **[Wikidata]** Base curada pequena (355 instâncias; 270 com P17); união com
   Q219409/Q5418962/categorias foi necessária para cobertura.
7. **[Wikidata]** 215 itens descobertos sem `P17` foram descartados; 18 excluídos por tipo.
8. **[Wikidata]** `P571` pode ter precisão apenas de ano; data canônica preservada sem inferência.
9. **[Wikidata]** Categorias Wikimedia só como canal de descoberta; atributos vêm do Wikidata.
10. **[BCB PTAX]** Série só em dias úteis com boletim; `last` é a cotação mais recente disponível.
11. **[BCB PTAX]** `cotacao_compra`/`cotacao_venda` preservadas como publicadas (sem arredondamento).

## 7. Reprodução

```powershell
# câmbio oficial (>=400 cotações 2024..hoje)
python pipeline/collect_fx.py

# SEC Form D: 8 trimestres (2024q4..2026q3), 1 linha por ACCESSIONNUMBER VC
python pipeline/collect_sec.py
python pipeline/collect_sec.py --start-quarter 2024q4 --end-quarter 2026q3

# Wikidata: descoberta + enriquecimento (Q3487908 + Q219409 + Q5418962 + categorias)
python pipeline/collect_wikidata.py
python pipeline/collect_wikidata.py --no-categories   # somente os canais SPARQL
```

Os coletores são **idempotentes**: `net.py` serve do cache `data/raw/` quando a URL já foi
baixada (replay sem rede com `VC_OFFLINE=1`), e cada execução reescreve os artefatos de forma
determinística (exceto `retrieved_at`).
