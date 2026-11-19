# Mapeamento de Venture Capital — Brasil e Global (pipeline SDD com Quality Gates)

Execução autônoma da especificação [`spec_agente_mapeamento_vc.md`](spec_agente_mapeamento_vc.md):
extração de fontes abertas reais → transformação em informação canônica → renderização de um
dashboard executivo em HTML único → auditoria adversarial em 4 Quality Gates.

**Entregáveis**

| Artefato | Conteúdo |
|---|---|
| [`dashboard/index.html`](dashboard/index.html) | Dashboard executivo autossuficiente em 4 abas (2,05 MB; CSS/JS inline, fontes woff2 embutidas, 5 figuras SVG, 7.521 registros embutidos, zero rede em runtime) |
| [`data/canonical/funds.json`](data/canonical/funds.json) | Dataset canônico `VentureCapitalFundRecord` — 7.521 veículos de 5.064 gestoras |
| [`data/canonical/kpis.json`](data/canonical/kpis.json) | KPIs derivados (contagens, capital, densidade por estágio, liquidez, cross-border) |
| [`audit/report.md`](audit/report.md) | Relatório de auditoria independente (Gates 1–4, evidências medidas) |
| [`audit/audit_report.json`](audit/audit_report.json) | Relatório estruturado por check (status, evidência, métrica) |

### Como navegar no dashboard

O arquivo abre na aba **Achados** (a conclusão), com quatro abas acessíveis por teclado (←/→/Home/End)
e por deep-link: `#achados`, `#insights`, `#fundos`, `#metodo`.

1. **Achados** — hero centrado com **título neutro** ("Fundos de Venture Capital no Brasil e no exterior,
   por fase de maturidade") e bloco **"Leitura principal"** com a conclusão quantitativa calculada (143
   caracteres); em seguida os dois achados em destaque (`Achado 03 · Concentração de capital` e
   `Achado 04 · Ponte cross-border`), faixa compacta de métricas, distribuição de veículos por fase,
   capital por fase e matriz de decisão.
2. **Insights** — desenvolvimento dos indicadores 01 (universo/cobertura) e 02 (capital, concentração e
   porte): cobertura de valor, top-10, percentis P25·mediana·P75, origem regulatória, qualidade do dado e liquidez.
3. **Fundos** — tabela auditável com filtros por corte/fase, busca, ordenação, paginação e link para a
   fonte primária de cada veículo.
4. **Método e fontes** — nota metodológica em blocos recolhíveis, câmbio, limitações, licenças e reprodutibilidade.

---

## 1. Resultado em uma página

- **Documento**: título neutro e centralizado — "Fundos de Venture Capital no Brasil e no exterior, por fase de maturidade" — com bloco **"Leitura principal"** (conclusão quantitativa calculada): "92,4% do capital sob gestoras brasileiras está em teses Série B+, mas só 1,8% dos 5.374 veículos estrangeiros mapeados têm atividade no Brasil."
- **7.521 veículos** de **5.064 gestoras** em três cortes: `BRASIL_LOCAL` 2.147 · `GLOBAL_ATIVO_BR` 99 · `GLOBAL_EX_BR` 5.275.
- **Capital declarado US$ 454 bi** (68,2% de cobertura de valor): Brasil US$ 191,2 bi · Global ativo no Brasil US$ 6,1 bi · Global ex-Brasil US$ 256,7 bi.
- **Série B+ absorve 92,4% do capital** sob gestoras brasileiras, embora só 44,6% dos 2.147 veículos locais tenham essa tese declarada/classe.
- **Seed é o gargalo local**: 479 veículos brasileiros com Seed contra 3.016 estrangeiros (~2,5× a menos), apesar de o Brasil concentrar capital de estágio tardio.
- **Índice de abertura cross-border: 1,84%** — apenas 99 dos 5.374 veículos estrangeiros mapeados têm atividade registrada no Brasil (Shizune).
- **Liquidez Brasil**: 63,4% dos 1.991 fundos com série comparável tiveram **patrimônio líquido crescente** entre os dois últimos informes quadrimestrais (1.262 fundos).
- **Dry powder global (Form D)**: **US$ 158,2 bi** ainda a captar em 3.108 veículos (offering − sold).
- **Rastreabilidade**: 524 registros com tese declarada, 309 com classe regulatória CVM, 6.688 imputados por porte; toda linha aponta para URL https de fonte primária.

## 2. Arquitetura (spec §1 e §2)

```
pipeline/net.py            cliente HTTP auditável (UA acadêmico, rate-limit, retry, cache, manifest)
pipeline/collect_br.py     CVM (registro RCVM 175 + informe quadrimestral) + BNDES
pipeline/collect_sec.py    SEC EDGAR — Form D data sets (8 trimestres), 18.811 veículos VC
pipeline/collect_wikidata.py  SPARQL (Q3487908 "venture capital firm") — 407 gestoras com país
pipeline/collect_fx.py     BCB PTAX — 724 cotações de venda (2024-01-02..2026-11-17)
pipeline/collect_directories.py  Shizune (9 charts, 435 linhas) + OpenVC/Brouky/benchmarks
pipeline/transform.py      entity resolution, stage engine (3 prioridades), FX, KPIs
pipeline/render.py         dashboard HTML (headline/KPIs/SVG/tabela/filtros calculados dos dados)
pipeline/gates.py          Gate 1–4 independentes (auditor adversarial)
pipeline/run_all.py        orquestrador: INGEST → TRANSFORM → RENDER → AUDIT
```

Fluxo: `Plan → Ingest → Transform → Render → Audit → Refine` com 4 gates determinísticos
(ingestão/rate-limit, contrato SDD/zero alucinação, anti-AI-slop, UI-UX WCAG AAA/responsividade).

**Reprodução**

```powershell
$env:PYTHONUTF8='1'
python pipeline/run_all.py                 # pipeline completo + gates
python pipeline/run_all.py --skip-ingest   # reusa data/interim e data/raw
$env:VC_OFFLINE='1'                        # replay sem rede (cache local)
```

Segunda execução em modo offline produz os mesmos `data/interim/*.jsonl`
(byte-idênticos, sha256 estáveis) — reprodutibilidade verificada pelos coletores.

## 3. Fontes abertas e licenças

| Fonte | Corte | O que foi extraído | Licença |
|---|---|---|---|
| [CVM — Registro de Fundos e Classes (RCVM 175)](https://dados.cvm.gov.br/dataset/fi-cad) | BR | 91.185 fundos / 4.521 FIPs → 2.147 FIPs ativos, gestor, PL, situação | ODbL |
| [CVM — Informe Quadrimestral de FIP](https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral) | BR | série de PL por competência (2024–2026) e capital declarado | ODbL |
| [BNDES — Renda Variável / Carteira de Fundos](https://dadosabertos.bndes.gov.br/dataset/renda-variavel) | BR | 881 posições da carteira → 42 FIPs com coinvestimento BNDESPAR | Dados abertos BNDES |
| [SEC EDGAR — Form D Data Sets](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets) | Global | 18.811 veículos `Venture Capital Fund` (2024Q4–2026Q3), CIK, offering, jurisdição | Domínio público |
| [Wikidata Query Service](https://query.wikidata.org/) | Global | 407 gestoras com país-sede (P17), website, fundação | CC0 |
| [Shizune Investor Database](https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md) | BR + Global | 9 charts, 435 linhas, estágios declarados e deals | Conteúdo público citável |
| [BCB PTAX](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/) | câmbio | 724 cotações de venda para conversão BRL→USD por competência | Dados abertos BCB |
| [ABVCAP](https://abvcap.com.br/publicacao/consolidacao-de-dados-2o-trimestre-2026/) / [LAVCA](https://www.lavca.org/research/) | benchmark | referência de calibração (sem agregado normalizável nesta coleta) | citável |

**OpenVC** retornou HTTP 403 (Cloudflare) em 4 tentativas — fallback aberto documentado em
[`data/interim/directories_availability.json`](data/interim/directories_availability.json); nenhum dado inventado.
**Finep/MCTI** responderam 200, mas são HTML institucional sem dataset tabular de fundos (registrado como indisponível).
**Brouky** é apenas página dinâmica sem export aberto (evidência agregada, sem registro de gestora).

## 4. Regras de transformação aplicadas (spec §1.3)

1. **Entity resolution** — CVM por CNPJ da gestora (`CPF_CNPJ_Gestor`), SEC por CIK, nomes normalizados
   contra Shizune/Wikidata. 2.175 linhas ativas de FIP → 2.147 veículos únicos (27 casos de troca de gestor
   sem data de vigência na fonte; regra determinística documentada em [`docs/sources_br.md`](docs/sources_br.md)).
2. **Stage engine (3 prioridades)** — `DECLARED_THESIS` (Shizune) > `REGULATORY_CLASS_CVM_BNDES`
   (FIP Capital Semente → Pre-Seed/Seed; Empresas Emergentes → Série A/B; Multiestratégia com PL > R$ 500 mi →
   Série B/Late) > `IMPUTED_BY_TICKET_HEURISTIC` (faixas Growth List aplicadas ao **porte do veículo**, explicitando a base).
3. **Cortes geográficos** — `BRASIL_LOCAL` (gestora registrada na CVM), `GLOBAL_ATIVO_BR` (gestora estrangeira
   listada nos charts Brasil do Shizune), `GLOBAL_EX_BR` (demais). País-sede derivado por cadeia
   Wikidata:P17 → país do endereço EDGAR → jurisdição de constituição → UF dos EUA → US.
4. **Câmbio** — PTAX de venda do BCB da competência mais próxima anterior à data do informe; base registrada
   em `estimate_basis` de cada registro.
5. **Zero invenção** — atributo não divulgado → `null` + nome em `undisclosed_fields`; nenhum ticket,
   patrimônio ou URL foi sintetizado.

## 5. Decisões de escopo e achados de qualidade de dados (transparência)

- **Universo global**: veículos `Venture Capital Fund` do Form D deduplicados por CIK, excluindo
  8.483 filings com oferta declarada < US$ 1 mi (veículos nominais) e 3.181 SPVs de deal único identificados
  pelo nome legal. 22 registros sem país determinável ficaram fora (contabilizados em `transform_report.json`).
  Regra e totais em `kpis`/`transform_report.scope`.
- **Dry powder Brasil não é publicado**: os campos `VL_CAP_COMPROM`/`VL_CAP_INTEGR` do Informe Quadrimestral
  são internamente inconsistentes — **1.130 de 5.754 linhas** declaram capital integralizado > comprometido,
  658 linhas vêm com comprometido = 0 e há erro de escala (HMV reporta R$ 305,9 bi com PL de R$ 0,4 bi).
  A liquidez brasileira é medida pela regra da própria spec: PL crescente entre os dois últimos informes.
  Os valores por fundo permanecem no dataset com `data_quality_flags`.
- **Faixa de cheque**: `estimated_ticket_min/max_usd` é `null` em 100% do dataset porque nenhuma fonte aberta
  divulga o cheque por startup. O dashboard mostra distribuição de **porte declarado** e declara a limitação,
  em vez de imputar um número.
- **Shizune**: `Relevant Deals` é escopado ao chart (Canary aparece com 2, 203 e 230 em charts diferentes);
  `brazil_deals_count` só é preenchido a partir de charts brasileiros. `Geography` é truncada em 3 países —
  por isso o critério de atividade no Brasil é estrutural (`listed_in_brazil_dataset`).

## 6. Quality Gates (executados sobre os artefatos finais)

| Gate | Veredito | Principais evidências |
|---|---|---|
| 1 — Ingestão/rate-limit | **PASS** | manifest 980 requisições; 202 arquivos de `data/raw` re-hashados == sha256; SEC com `User-Agent` acadêmico e intervalo 0,2 s (limite 10 req/s); CVM em latin1 com `;` sem U+FFFD (46.805/6.821/7.896/5.754 linhas); 22.664 URLs de proveniência; OpenVC 403 com fallback documentado |
| 2 — Contrato SDD/zero alucinação | **PASS** | 7.521/7.521 registros válidos; `fund_id` e `regulatory_id` únicos; `hq_country` 100% ISO-2; 25/25 URLs de fonte HTTP 200; KPIs reconciliados (capital US$ 454.047.433.797,69 = total); nenhum valor negativo |
| 3 — Anti-AI-Slop | **PASS** | 0 clichês, 0 gradientes decorativos, 0 `box-shadow` > 4 px, 0 emojis; 5 `<figure>` com `<figcaption>` e fonte clicável; 351 valores numéricos formatados; **H1 neutro sem números + hero centrado** e bloco "Leitura principal" com a conclusão quantitativa (143 caracteres, calculada) |
| 4 — UI/UX WCAG AAA/responsivo | **PASS** | contraste mínimo medido 7,68:1; piso tipográfico 12 px confirmado em runtime (`minFontPx=12`); 88/89 células monetárias com `tabular-nums`+`nowrap` (o 1 restante é prosa que menciona moeda); 4 tabelas em `overflow-x:auto`; **zero overflow horizontal nas 4 abas** (`scrollWidth == innerWidth` em 375→500, 768→750, 1440→1422 CSS px — clamp do Windows documentado); 0 erros de JS; 51 links únicos verificados; capturas reais em `audit/shots/v3-*.png` |

Relatórios completos em [`audit/report.md`](audit/report.md) e [`audit/audit_report.json`](audit/audit_report.json).

### Reprodutibilidade e hashes do build final

| Arquivo | SHA-256 | Tamanho |
|---|---|---|
| `dashboard/index.html` | `0e742e2bd005bfeecaa8f5fca8548e759e55e6331f9dd5143e58ecbe83ef7007` | 2.050.435 B |
| `data/canonical/funds.json` | `b55fc5fd884876566dccf822524a171654b99ac79e68e655e9647d83d647d2fd` | 15.827.134 B |
| `data/canonical/kpis.json` | `ca824d324bf0d632056eea20ea886e0fe12c99eaacf7895adc52daccf3796161` | 12.589 B |

O **conteúdo** é determinístico: duas execuções completas de extração+transformação produzem o mesmo
hash do array `records` (`972f12637b7168e8f1bd4da45de6f05e77c066ccc467281a94024456432e958e`, 7.521
registros, zero divergências). Apenas os envelopes `generated_at` variam entre builds — por isso os
hashes de arquivo acima identificam o build exato que foi auditado, não uma propriedade permanente.

## 7. Limitações conhecidas

1. `Relevant Deals`, `Stage` e `Focus` do Shizune são recortes de chart (Top-35/50), não censo do mercado.
2. A base Wikidata de "venture capital firm" é curada e pequena (355 instâncias; 270 com país) — 407 gestoras
   após união de canais reais; o QID `Q1961622` citado na especificação é uma página de desambiguação e foi
   corrigido para `Q3487908`.
3. Stage global sem tese declarada é imputado pelo porte do veículo (regra da spec §1.3 prioridade 3);
   o dashboard separa os métodos para não comparar populações heterogêneas sem aviso.
4. Sem screenshots de viewport no ambiente de execução original (Chrome bloqueado por sandbox); a medição
   de layout foi feita por probe JS em Chrome headless executado fora do sandbox, com `scrollWidth` e
   contraste calculados programaticamente — evidências em [`audit/shots/`](audit/shots/).
5. Nenhuma fonte paga, licenciada ou raspagem de dados privados foi utilizada.

## 8. Publicação (GitHub Pages)

O dashboard é um **arquivo único autossuficiente**, publicado pela workflow
[`.github/workflows/pages.yml`](.github/workflows/pages.yml) a cada push em `main`:

```powershell
# 1. criar o repositório vazio na organização (web UI), depois:
git remote add origin https://github.com/NEI-Senai-Cimatec/<repo>.git
git push -u origin main

# 2. habilitar Pages: Settings -> Pages -> Source: "GitHub Actions"
#    (ou, com o gh CLI autenticado:)
gh api -X POST repos/NEI-Senai-Cimatec/<repo>/pages -f build_type=workflow

# 3. acompanhar o deploy e obter a URL
gh run watch
```

URL final: `https://nei-senai-cimatec.github.io/<repo>/`

Como o HTML não tem caminhos absolutos de raiz nem recursos externos obrigatórios, ele funciona
igual servido da raiz do domínio ou de um subdiretório (`/<repo>/`).

**`data/raw/` não vai para o Git** (~160 MB de cache de rede, regenerável com
`python pipeline/run_all.py`). O repositório versiona o dataset canônico, as evidências
interinas, a auditoria e o dashboard (~44 MB).

## 9. Estrutura do repositório

```
contract/          contratos JSON Schema (registro canônico e envelope de evidência)
pipeline/          coletores, transformação, renderer, gates e orquestrador
data/raw/          cache binário dos downloads (hash da URL) + manifest de proveniência
data/interim/      evidência normalizada por fonte (JSONL, contrato de interim)
data/canonical/    dataset canônico, KPIs e relatório de transformação
dashboard/         HTML final + resumo de render + evidências de gráficos
audit/             relatórios de auditoria, cache HTTP e medições de runtime
docs/              documentação por fonte e sistema de design
tools/             medição de viewport/runtime (Chrome headless)
spec_agente_mapeamento_vc.md   especificação executada
```
