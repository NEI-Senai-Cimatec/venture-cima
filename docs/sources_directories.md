# Diretórios abertos — ingestão de teses declaradas e atividade de deals

Documentação de coleta, semântica de campos, auditoria de disponibilidade e limitações das
fontes de diretórios abertos do pipeline de mapeamento de VC (`task-3`). Todos os números
abaixo vêm de execuções reais registradas em `data/manifest.jsonl`, `data/raw/` e
`data/interim/directories_availability.json`; nenhum dado foi sintetizado.

- **Coletor:** `pipeline/collect_directories.py` (stdlib apenas; toda rede via `pipeline/net.py`)
- **Saídas:**
  - `data/interim/shizune_firms.jsonl` — 435 registros, envelope `contract/interim_record.schema.json`
  - `data/interim/directories_availability.json` — auditoria de disponibilidade, fallback, benchmarks e aceite
- **Coleta:** 2026-11-18 (UTC). **Cache cru:** `data/raw/` (sha256 de cada resposta no manifest).

---

## 1. Objetivo

Coletar, de diretórios **abertos e gratuitos**, a tese declarada das gestoras (estágios-alvo e
foco setorial) e a atividade de deals no Brasil, para alimentar:

1. `stage_classification_method = DECLARED_THESIS` (Prioridade 1 da Matriz de Imputação de Fase, spec §1.3.2);
2. o **Índice de Abertura Cross-Border** (gestoras estrangeiras com ao menos 1 deal no Brasil, spec §1.3.3);
3. benchmarks de calibração populacional (ABVCAP/LAVCA), sem conversão em registros de fundo.

Fontes-alvo desta etapa: **Shizune** (dados), **OpenVC** (tentativa + fallback), **Brouky**
(evidência) e **ABVCAP/LAVCA** (benchmark).

---

## 2. Como reproduzir

```bash
# coleta normal (usa cache do net.py quando existir)
python pipeline/collect_directories.py

# força re-download de todas as URLs (registra novo sha256 no manifest)
python pipeline/collect_directories.py --refresh

# replay offline (somente cache de data/raw; não faz requisições)
python pipeline/collect_directories.py --offline

# diretório de saída alternativo
python pipeline/collect_directories.py --out-dir data/interim
```

- O script é **idempotente**: duas execuções seguidas com o mesmo cache produziram
  `shizune_firms.jsonl` byte-a-byte idêntico
  (sha256 `2a674509f90c75e46a03d3ecbd4f3be0574944b06f6c89cff41fd41e2315fa32`).
- Ao final ele executa o **self-check** do contrato (`interim_record.schema.json`) e dos
  critérios de aceite; `exit code != 0` se qualquer verificação falhar.
- `--offline` regenera o JSONL a partir do cache. Probes que retornaram 403/404 em rede não
  têm cache (o `net.py` só persiste respostas HTTP 200): em modo offline essas evidências
  aparecem como `OFFLINE_NO_CACHE` na auditoria — para revalidar 403/404 é necessário rodar
  em rede.

### Auditoria de rede (Gate 1)

Todas as chamadas passam por `pipeline/net.py`:

- `User-Agent` acadêmico declarado: `PesquisaAcademicaVC/1.0 (pesquisador@universidade.edu.br)`;
- intervalo mínimo de **0,3 s** por host (padrão; 0,2 s para `sec.gov`);
- retry com backoff exponencial (403/429/5xx), cache por URL em `data/raw/` e um registro
  JSONL por requisição em `data/manifest.jsonl` (status, bytes, sha256, UA, intervalo, cache);
- TLS verificado (certifi).

---

## 3. Contrato de saída do JSONL (congelado pelo Lead)

Uma linha por **linha de tabela** do Shizune. Envelope do contrato
`contract/interim_record.schema.json`: `record_id`, `source_id`, `source_url`,
`entity_kind="firm"`, `payload`. Campos do `payload` (exatamente estes, nesta ordem):

| Campo | Tipo | Semântica |
| :--- | :--- | :--- |
| `investor_name` | string | Nome da gestora **exatamente como na fonte** (sem normalizar caixa/acentos/entidades HTML). |
| `relevant_deals` | int | Inteiro da coluna `Relevant Deals` **daquele chart** (ver §4.5 — é escopado ao chart, não é contagem Brasil-only). |
| `geographies` | string[] | Coluna `Geography` do chart, split por vírgula, texto original. |
| `geography_includes_brazil` | bool | `"Brazil"` presente na coluna `Geography` daquela linha (comparação exata). Sinal **declarado de geografia**. |
| `listed_in_brazil_dataset` | bool | `true` para linhas de charts com recorte de país **Brazil** (por definição: charts de gestoras ativas no Brasil), inclusive quando `Geography` não lista `Brazil` (ex.: NFX). Sinal **estrutural de atividade no Brasil**. Nunca inferido da geografia. |
| `stage_raw` | string[] | Coluna `Stage` como veio (ex.: `["Seed","Series A","Venture - Series Unknown"]`). |
| `declared_stages` | enum[] | Apenas o que mapeia para `[PRE_SEED, SEED, SERIES_A, SERIES_B, LATE_STAGE_GROWTH, VENTURE_DEBT]`; `[]` quando nada mapeia. |
| `stage_unmapped` | string[] | Tokens de `Stage` sem faixa no enum (ver §4.6). |
| `focus_sectors` | string[] | Coluna `Focus` (setores), split por vírgula, texto original. |
| `table_url` | string | URL exata da tabela Markdown de origem (`.../table.md`). |
| `table_title` | string | Título H1 da tabela de origem (ex.: `Top 35 VC (Venture Capital) Funds in Brazil in November 2026`). |

Exemplo real (linha 5 do JSONL):

```json
{"record_id": "shizune:aimores-investments:vc-funds-brazil", "source_id": "shizune_brazil",
 "source_url": "https://shizune.co/investors/vc-funds-brazil/table.md", "entity_kind": "firm",
 "payload": {"investor_name": "Aimorés Investments", "relevant_deals": 42,
  "geographies": ["Brazil"], "geography_includes_brazil": true, "listed_in_brazil_dataset": true,
  "stage_raw": ["Seed", "Pre-Seed", "Angel"], "declared_stages": ["PRE_SEED", "SEED"],
  "stage_unmapped": ["Angel"], "focus_sectors": ["Software", "E-Commerce", "Information Technology"],
  "table_url": "https://shizune.co/investors/vc-funds-brazil/table.md",
  "table_title": "Top 50 VC (Venture Capital) Funds in Brazil in November 2026"}}
```

> **Entity resolution fica na transformação.** A mesma gestora aparece em mais de um chart
> (104 gestoras aparecem em ≥2 charts; ver §4.7). Consolide por `investor_name` e decida a
> precedência de tabela no transform. A tabela canônica citada na spec é
> `venture-capital-vc-funds-brazil` (a primeira do JSONL, `source_id=shizune_brazil`).

---

### 3.1 Estrutura de `directories_availability.json`

| Chave | Conteúdo |
| :--- | :--- |
| `sources` | **Lista plana** por fonte (`name`, `url`, `license`, `records`, `bytes`, `sha256`, `status`, `http_status`, `method`, `fallback`, `notes`) — formato consumido por `pipeline/transform.py` para montar o `sources_registry.json`. |
| `openvc` | Aviso textual do bloqueio (string), consumido como warning na transformação. |
| `source_details` | Detalhamento auditável: `shizune` (9 tabelas + 3 excluídas + 6 probes 404 + discovery + estatísticas + semântica), `openvc` (4 tentativas), `brouky` (páginas + evidência extraída), `abvcap`, `lavca`. |
| `benchmarks` | Lista `[{name, url, status, excerpt, note}]` (formato exigido pelo Lead). |
| `acceptance` | Resultado do self-check (PASS/FAIL + métricas observadas). |
| `limitations` | Limitações operacionais/estatísticas. |
| `generated_at_utc`, `network`, `payload_contract_jsonl` | Data da coleta, parâmetros de rede (net.py) e o contrato de payload congelado do JSONL. |

---

## 4. Shizune — tabelas Markdown públicas

### 4.1 Descoberta (sitemap)

`https://shizune.co/robots.txt` → `Allow: /`; `Sitemap: https://shizune.co/sitemap.xml`.
O índice aponta para `sitemap-charts.xml`, que lista **11.763** páginas de chart em
`/investors/*`. Descobertas relevantes:

| Constatação | Evidência |
| :--- | :--- |
| Não existem charts **por estágio** no Shizune | 0 de 11.763 slugs casam com `pre-seed/seed/series-*/venture-debt/late-stage`; 6 URLs de estágio testadas retornam **404** |
| O estágio vem da coluna `Stage` de cada chart | 9/9 tabelas ingeridas têm as colunas `Investor \| Relevant Deals \| Geography \| Stage \| Focus` |
| Existem charts **setoriais por país** (`*-vc-funds-brazil`) | 60 disponíveis (ex.: `software-vc-funds-brazil`, `fintech-vc-funds-brazil`); documentados como superfície de expansão, não ingeridos nesta etapa |
| As 9 tabelas ingeridas existem no sitemap | `ingested_paths_present_in_sitemap` = todos `true` |

A publicação é um endpoint de exportação Markdown por chart: `https://shizune.co/<chart>/table.md`.
Cada arquivo traz H1 + tabela pipe + rodapé institucional (# About Shizune) e limita-se ao
**Top N** do chart (35 ou 50 linhas).

### 4.2 Tabelas ingeridas (9/9 HTTP 200 + tabela real)

| # | URL (tabela de origem) | Linhas | Título |
| :--- | :--- | :---: | :--- |
| 1 | https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md | 35 | Top 35 VC (Venture Capital) Funds in Brazil in November 2026 |
| 2 | https://shizune.co/investors/vc-funds-brazil/table.md | 50 | Top 50 VC (Venture Capital) Funds in Brazil in November 2026 |
| 3 | https://shizune.co/investors/venture-capital-vc-funds/table.md | 50 | Top 50 VC (Venture Capital) Funds in  in November 2026 (geografia vazia no título) |
| 4 | https://shizune.co/investors/venture-capital-vc-funds-latam/table.md | 50 | Top 50 VC (Venture Capital) Funds in LATAM in November 2026 |
| 5 | https://shizune.co/investors/vc-funds-latam/table.md | 50 | Top 50 VC (Venture Capital) Funds in LATAM in November 2026 |
| 6 | https://shizune.co/investors/venture-capital-vc-funds-united-states/table.md | 50 | Top 50 VC (Venture Capital) Funds in United States in November 2026 |
| 7 | https://shizune.co/investors/vc-funds-united-states/table.md | 50 | Top 50 VC (Venture Capital) Funds in United States in November 2026 |
| 8 | https://shizune.co/investors/venture-capital-vc-funds-europe/table.md | 50 | Top 50 VC (Venture Capital) Funds in Europe in November 2026 |
| 9 | https://shizune.co/investors/vc-funds-europe/table.md | 50 | Top 50 VC (Venture Capital) Funds in Europe in November 2026 |

**Total: 435 linhas → 435 registros**; `source_id = shizune_brazil` para as tabelas 1–2
(85 registros) e `shizune_global` para as demais (350 registros), conforme o recorte de país
do chart. Todos os downloads em UTF-8; nomes com acento preservados (`Aimorés`, `Grão`,
`Grão`). Arquivos de origem e sha256 estão na auditoria JSON.

### 4.3 Tabelas que funcionaram e NÃO foram ingeridas (3)

Ficam registradas na auditoria (status/linhas/título), mas fora do dataset de gestoras porque
o recorte é de "investidores"/anjos, não de gestoras/fundos:

| URL | Linhas | Motivo |
| :--- | :---: | :--- |
| https://shizune.co/investors/investors-brazil/table.md | 50 | Mistura anjos, indivíduos e organizações. |
| https://shizune.co/investors/angel-investors-brazil/table.md | 50 | Investidores-anjo (pessoas físicas). |
| https://shizune.co/investors/venture-capital-investors/table.md | 50 | "Startup investors" (inclui anjos/indivíduos). |

### 4.4 URLs testadas que retornam 404 (6)

| URL | Por quê foi testada |
| :--- | :--- |
| https://shizune.co/investors/vc-funds/table.md | URL citada como 404 no briefing. |
| https://shizune.co/investors/pre-seed-investors-brazil/table.md | Chart por estágio sugerido (não existe). |
| https://shizune.co/investors/seed-investors-brazil/table.md | idem. |
| https://shizune.co/investors/series-a-investors-brazil/table.md | idem. |
| https://shizune.co/investors/series-b-investors-brazil/table.md | idem. |
| https://shizune.co/investors/venture-debt-investors-brazil/table.md | idem. |

### 4.5 Semântica dos campos (atenção nas armadilhas)

- **`Geography` ≠ sede (HQ).** Lista as geografias de atuação declaradas e o export limita a
  **3 por linha** (387 de 435 linhas têm exatamente 3; 22 têm 2; 26 têm 1). Portanto
  `geography_includes_brazil=false` **não** prova ausência de atuação no Brasil, e nenhum
  `hq_country` é derivado (não é inventado).
- **`listed_in_brazil_dataset` é o critério de atividade no Brasil** (orientação do Lead):
  verdadeiro para linhas dos dois charts com recorte Brazil, inclusive para as **12 linhas**
  que não listam `Brazil` na `Geography`: Andreessen Horowitz, Antler, Base10 Partners,
  Circle, Everywhere Ventures, Global Founders Capital, HOF Capital, JAM Fund, Marathon
  Ventures, NFX, Stellar Development Foundation, Tribe Capital. Use **os dois campos
  separados** — eles não são intercambiáveis.
- **`Relevant Deals` é escopado ao chart** e varia entre charts para a mesma gestora. Exemplo
  real (Canary): `2` na tabela canônica Brasil, `203` em `vc-funds-brazil`, `230` em
  `vc-funds-latam`. Não use esse campo como `brazil_deals_count` sem validação; para
  comparação entre gestoras, prefira o valor da tabela em que a linha aparece (registrado em
  `table_url`).
- **Atributos da gestora variam um pouco entre charts.** De 104 gestoras em ≥2 charts, 97 têm
  `Stage`/`Geography`/`Focus` idênticos em todas as aparições e **7 apresentam divergência**
  (ABSeed Ventures, B Venture Capital, Valor Capital Group, Bankless Ventures, Founders Fund,
  Sequoia Capital, Unpopular Ventures). O JSONL preserva o valor de cada chart; a união
  (quando desejada) deve ser feita no transform.
- Nomes de setores/categorias são preservados como na fonte; não há tradução nem
  normalização.

### 4.6 Mapeamento de estágio (somente declarado → enum)

| Token na fonte (`stage_raw`) | `declared_stages` | Regra |
| :--- | :--- | :--- |
| `Pre-Seed` | `PRE_SEED` | literal |
| `Seed` | `SEED` | literal |
| `Series A` | `SERIES_A` | literal |
| `Series B` | `SERIES_B` | literal |
| `Series C` | `LATE_STAGE_GROWTH` | única faixa do enum compatível com estágio tardio declarado (nada acima de C aparece na base) |
| `Venture - Series Unknown` | — → `stage_unmapped` | estágio não especificado na fonte |
| `Angel` | — → `stage_unmapped` | tipo de investidor, não estágio |
| `Grant` | — → `stage_unmapped` | fomento, não estágio de VC |
| `Convertible Note`, `Private Equity`, `Debt Financing`, `Equity Crowdfunding` | — → `stage_unmapped` | instrumento/tipo de operação, sem faixa no enum |

`VENTURE_DEBT` **não** é atribuído a ninguém: a base não declara "Venture Debt" (só
`Debt Financing`, que é outro conceito). Zero invenção de estágio; `declared_stages=[]` em 2
linhas (1 gestora: Partner Ventures, cuja fonte só declara `Venture - Series Unknown` e
`Private Equity`).

Distribuição entre as **gestoras únicas** (284): `SEED` 262, `SERIES_A` 209, `PRE_SEED` 121,
`SERIES_B` 99, `LATE_STAGE_GROWTH` 11. Tokens não mapeados (ocorrências em 435 linhas):
`Venture - Series Unknown` 95, `Angel` 21, `Convertible Note` 13, `Grant` 10,
`Private Equity` 10, `Debt Financing` 10, `Equity Crowdfunding` 4.

### 4.7 Contagens finais (self-check PASS)

| Métrica | Valor | Critério de aceite |
| :--- | ---: | :--- |
| Registros (linha de tabela) | **435** | — |
| Gestoras únicas (`investor_name`) | **284** | — |
| Gestoras com ≥1 estágio mapeado | **283** | ≥ 35 ✅ |
| Gestoras com `geography_includes_brazil` | **75** | ≥ 10 ✅ |
| Gestoras em chart com recorte Brasil (`listed_in_brazil_dataset`) | **75** | — |
| União dos dois sinais de Brasil | **87** (63 em ambos; 12 só estruturais; 12 só geografia) | — |
| Registros sem estágio mapeado | 2 (1 gestora) | — |
| Gestoras em ≥2 charts | 104 | — |

---

## 5. OpenVC — bloqueado (HTTP 403) e fallback acionado

O OpenVC (`investor-database` e o blog "VC List of VC Lists") está atrás de desafio Cloudflare.
Foram feitas **4 tentativas** (2 URLs × 2 User-Agents) registradas em
`directories_availability.json → source_details.openvc.attempts`:

| URL | User-Agent | Resultado |
| :--- | :--- | :--- |
| https://www.openvc.app/investor-database | acadêmico declarado | **HTTP 403** |
| https://www.openvc.app/investor-database | navegador (Chrome 131) | **HTTP 403** |
| https://www.openvc.app/blog/vc-list | acadêmico declarado | **HTTP 403** |
| https://www.openvc.app/blog/vc-list | navegador (Chrome 131) | **HTTP 403** |

O `net.py` não persiste corpo de resposta de erro (HTTPError), então a evidência registrada é o
status 403; o corpo `Just a moment...` (desafio Cloudflare) foi verificado pelo Lead.
**Nenhum registro OpenVC foi coletado ou sintetizado.**

**Fallback aberto acionado:** substituição por datasets abertos equivalentes do Shizune
(charts de país/tipo das 9 tabelas do §4.2), que cobrem a mesma função — tese declarada por
gestora (`Stage`/`Focus`) e atividade no Brasil (`listed_in_brazil_dataset`) — sem custo e com
URL de origem rastreável por registro. O fallback está declarado em
`source_details.openvc.fallback` (e também no item correspondente da lista plana `sources`).

---

## 6. Brouky — página aberta, sem export de dataset

- `https://brouky.tech/data` → **200** (95.938 bytes); `https://brouky.tech/data/vc-activity-2026/brazil`
  → **200** (100.875 bytes). HTML Next.js (App Router): **sem** `__NEXT_DATA__`
  (`self.__next_f` presente), **sem** links de export CSV/JSON/Download.
- Classificação: `AVAILABLE_PAGE_ONLY` — **agregados renderizados** foram extraídos como
  evidência auditável, mas **não** convertidos em registros de gestora (não há dataset aberto
  parseável por gestora). Evidência real extraída da página do Brasil:
  - **473 investidores** e **1.384 startups** rastreados no Brasil (atualização semanal, segundo a página);
  - 8 setores com contagem de investidores (ex.: Software & Developer Tools 137; Finance & Blockchain 131; Healthcare & Life Sciences 71);
  - foco por estágio (contagem de investidores): `seed` 19, `series-a` 14, `pre-seed` 9, `series-b` 6, `series-c-plus` 3;
  - 15 gestoras mais ativas no Brasil (ex.: Monashees VC 172 investimentos; Kaszek VC 170; Canary VC 90).

Esses agregados servem de **contraprova populacional** (cross-check de cobertura) e ficam em
`source_details.brouky.extracted_evidence`; eles não são usados para derivar `declared_stages` de
gestoras individuais.

---

## 7. Benchmarks de calibração (ABVCAP / LAVCA)

Array `benchmarks` em `directories_availability.json`, com `name`, `url`, `status`, `excerpt`
e `note`, apenas com **texto citável real** (nenhum número inventado):

| # | Fonte | URL | Status | Trecho citável |
| :-: | :--- | :--- | :--- | :--- |
| 1 | ABVCAP + TTR Data — *Consolidação de Dados 2º Trimestre 2026* | https://abvcap.com.br/publicacao/consolidacao-de-dados-2o-trimestre-2026/ | `AVAILABLE` (HTTP 200, 165.588 bytes) | "A ABVCAP, em parceria com a TTR Data, publicou o relatório trimestral referente às transações realizadas em Private Equity, Venture Capital e Corporate Venture Capital realizadas no 2º trimestre de 2026." |
| 2 | LAVCA — *2025 Latin American Startup Ecosystem Insights* | https://www.lavca.org/research/2025-latin-american-startup-ecosystem-insights/ | `AVAILABLE` (HTTP 200, 85.416 bytes) | "LAVCA’s 2025 Latin American Startup Ecosystem Insights report offers a detailed view of the region’s evolving startup landscape." |

### 7.1 Por que não há KPI numérico normalizado (e por que isso é intencional)

- **ABVCAP:** a página HTML **não** renderiza valores (`R$` = 0 ocorrências; `2T26` = 0). O
  relatório completo está em PDF aberto:
  https://abvcap.com.br/wp-content/uploads/2026/08/report-abvcap-ttr-data-2026-2t.pdf
  (**2.521.400 bytes**, sha256 `3dc9521b702bba7e432a43f1237f1d74b67f6adfd73fec7ff017bfb338e550c7`,
  21 páginas, 74 streams, 22 streams de texto). O texto foi extraído com stdlib (`zlib`) e a
  seção *Venture Capital → Visão Geral* é legível, porém os totais trimestrais aparecem
  **apenas como rótulos de gráfico**, sem atribuição textual inequívoca de trimestre. Para
  não transcrever um número errado, **nenhum valor foi normalizado**. O briefing do Lead
  citava "R$ ~900 mi em VC no 2T26"; esse valor **não** foi confirmado no HTML público e,
  portanto, não é afirmado aqui — a citação recomendada é o título/URL/trecho acima.
- **LAVCA:** a página oficial responde 200 e é citável, mas o HTML não expõe agregados
  numéricos (`R$`/`US$` = 0); o relatório completo é o caminho oficial. Nenhum número
  inventado.

---

## 8. Aceite da task

| Critério | Resultado |
| :--- | :--- |
| `shizune_firms.jsonl` ≥ 35 gestoras com estágio declarado mapeado | **283** ✅ |
| ≥ 10 gestoras com atividade no Brasil | **75** (`geography_includes_brazil`) / **75** (`listed_in_brazil_dataset`) / **87** na união ✅ |
| OpenVC 403 documentado + fallback acionado | 4 tentativas 403 + fallback Shizune ✅ |
| ≥ 1 benchmark ABVCAP citável | ABVCAP (título/URL/trecho + PDF com sha256) e LAVCA ✅ |
| Contrato `interim_record.schema.json` | self-check **PASS** (435/435 registros) ✅ |

---

## 9. Limitações e próximos passos

1. **Recorte de topo, não censo:** cada chart exporta apenas Top 35/50; a base cobre as
   gestoras mais relevantes de cada recorte, não o universo completo.
2. **`Geography` truncada em 3 países:** use `listed_in_brazil_dataset` para atividade no
   Brasil e não derive HQ (`hq_country` permanece ausente; resolver via CVM/Wikidata/outras fontes).
3. **`Relevant Deals` escopado ao chart:** não é contagem Brasil-only (ver Canary 2/203/230).
4. **Divergências entre charts** em 7 gestoras (Stage/Focus): o transform deve definir
   precedência (sugestão: tabela canônica `venture-capital-vc-funds-brazil`, depois
   `vc-funds-brazil`).
5. **OpenVC bloqueado** (403): sem dados dessa fonte; fallback Shizune declarado.
6. **Brouky sem export** parseável: apenas evidência agregada de página.
7. **ABVCAP/LAVCA sem números normalizáveis no HTML** (ABVCAP só em gráfico de PDF): benchmark
   citável, não KPI numérico.
8. **Sem charts por estágio no Shizune** (0/11.763; 6 URLs 404): estágio é atributo de linha.
9. **Expansão documentada:** 60 charts setoriais `*-vc-funds-brazil` disponíveis (200,
   ~50 linhas cada; amostras verificadas em `software`, `fintech`, `health-care`,
   `artificial-intelligence`, `agriculture-agtech`). Não ingeridos nesta etapa para manter o
   recorte homogêneo (fund-level por país/tipo) e evitar viés de seleção setorial; podem ser
   adicionados em rodada futura com `INGEST_TABLES`.
10. **Uso e atribuição:** os dados do Shizune são uma tabela pública de exportação
    (`robots.txt` permite `/`); citar a URL exata da tabela por registro (campo `table_url`) e
    atribuir Shizune.co como fonte na publicação acadêmica. O coletor respeita rate-limit
    >= 0,3 s e UA declarado.

---

## 10. Referências (URLs reais usadas)

- Shizune — tabelas Markdown (9 ingeridas): ver §4.2. Sitemap: https://shizune.co/sitemap-charts.xml
- Shizune — tabela canônica citada na spec: https://shizune.co/investors/venture-capital-vc-funds-brazil
- OpenVC (bloqueado): https://www.openvc.app/investor-database · https://www.openvc.app/blog/vc-list
- Brouky: https://brouky.tech/data · https://brouky.tech/data/vc-activity-2026/brazil
- ABVCAP: https://abvcap.com.br/publicacao/consolidacao-de-dados-2o-trimestre-2026/ ·
  PDF https://abvcap.com.br/wp-content/uploads/2026/08/report-abvcap-ttr-data-2026-2t.pdf
- LAVCA: https://www.lavca.org/research/2025-latin-american-startup-ecosystem-insights/
- Auditoria completa (status HTTP, bytes, sha256, títulos, fallback, benchmarks):
  `data/interim/directories_availability.json`
