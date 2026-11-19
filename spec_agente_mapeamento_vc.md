# Especificação SDD, Loop Engineering e Master Prompt: Agente Autônomo de Mapeamento de Venture Capital (Brasil e Global)

Este documento define a arquitetura orientada a especificações (**Spec-Driven Development — SDD**), o ciclo de controle iterativo (**Loop Engineering**) e o **Master Prompt** de engenharia de prompt para que um agente autônomo execute, de ponta a ponta e sem custos de licenciamento, a extração, transformação e renderização executiva de dados de fundos de Venture Capital (VC) por fase de maturidade de startups nos recortes **Brasil** e **Global (Resto do Mundo)**.

---

## 1. Arquitetura SDD (Spec-Driven Development)

No paradigma de **Spec-Driven Development (SDD)**, o agente autônomo não improvisa estruturas de dados nem decisões visuais durante a geração: ele opera vinculado a contratos formais de entrada, transformação, validação e saída.

### 1.1. Escopo Funcional e Fontes de Dados Gratuitas (Contratos de Ingestão)

O módulo coletor do agente deve priorizar exclusivamente fontes governamentais abertas, APIs públicas sem custo e repositórios científicos/comunitários auditáveis:

| Camada de Dados | Fonte / Repositório | Corte Geográfico | Método de Extração | Atributos Chave Extraídos |
| :--- | :--- | :--- | :--- | :--- |
| **Regulatória Oficial (Brasil)** | [Portal de Dados Abertos da CVM](https://dados.cvm.gov.br/group/fundos-de-investimento) e [Informe Quadrimestral de FIPs](https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral) | Brasil (`BRASIL_LOCAL`) | Download HTTP direto de arquivos `.csv` / `.txt` (`cad_fi.csv` e informes de FIP) | `CNPJ_FUNDO`, `DENOM_SOCIAL`, `CNPJ_GESTOR`, `VL_PATRIM_LIQ`, classe CVM (`Capital Semente`, `Empresas Emergentes`, `Multiestratégia`) e carteira investida |
| **Fomento Público (Brasil)** | [Portal de Dados Abertos do BNDES (Renda Variável)](https://dadosabertos.bndes.gov.br/fa_IR/dataset/renda-variavel) e [Fundos de Investimento Finep](https://www.finep.gov.br/fundos-de-investimento) | Brasil (`BRASIL_LOCAL`) | Download de CSVs abertos no portal do BNDES e no [Portal Dados Abertos MCTI](https://www.gov.br/mcti/pt-br/acesso-a-informacao/dados-abertos/dados-abertos) | Vínculo de coinvestimento público, classificação de porte [Capital Semente até R$ 20 mi e VC até R$ 300 mi no BNDES](https://www.bndes.gov.br/wps/portal/site/home/mercado-de-capitais/fundos-de-investimentos/fundos-ativos-desinvestimento) e [53 fundos ativos na carteira BNDES](https://www.bndes.gov.br/wps/portal/site/home/mercado-de-capitais/fundos-de-investimentos/dados-agregados) |
| **Atividade de Deals no Brasil** | [Shizune — VC Funds in Brazil Dataset](https://shizune.co/investors/venture-capital-vc-funds-brazil) | Brasil (`BRASIL_LOCAL` e `GLOBAL_ATIVO_BR`) | Parsing direto da tabela aberta em Markdown via [table.md](https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md) e [vc-funds-brazil/table.md](https://shizune.co/investors/vc-funds-brazil/table.md) | Nome da gestora, contagem de investimentos no Brasil, estágios de atuação (`Stages`), tamanho de rodada e taxa de liderança (`Lead/Follow rate`) |
| **Regulatória Oficial (Global / EUA / Offshore)** | [SEC EDGAR RESTful API (`data.sec.gov`)](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) e [SEC Form D Data Sets](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets) | Global (`GLOBAL_EX_BR` e `GLOBAL_ATIVO_BR`) | Requisições REST JSON em [data.sec.gov](https://data.sec.gov/) (cabeçalho `User-Agent` obrigatório, máx. 10 req/s conforme [regras de acesso da SEC](https://dataimpulse.com/blog/sec-edgar-api/)) e pacotes ZIP trimestrais | `CIK`, nome legal do fundo, jurisdição (EUA, Cayman, Delaware), tag `<investmentFundType>Venture Capital Fund</investmentFundType>`, `totalOfferingAmount` e data do primeiro aporte |
| **Grafo Semântico Global** | [Wikidata Query Service (SPARQL)](https://query.wikidata.org/) | Global e Brasil | Consulta HTTP GET/POST ao endpoint SPARQL usando padrões de [consultas SPARQL do Wikidata](https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service/queries/examples) | Entidades instanciadas como `wd:Q1961622` (Venture Capital Firm), país-sede (`P17`), fundação (`P571`), website oficial (`P856`) e participações (`P355`/`P1830`) |
| **Diretórios Abertos de Tese e Cheque** | [OpenVC Investor Database](https://www.openvc.app/investor-database) e [The VC List of VC Lists (53 bases)](https://www.openvc.app/blog/vc-list) | Global e América Latina | Ingestão tabular das 53 planilhas públicas curadas e perfis abertos da [OpenVC](https://www.openvc.app/) | Tese setorial, estágios-alvo declarados (`Pre-Seed`, `Seed`, `Series A`, `Series B`), cheque mínimo/máximo em USD e mandato geográfico |
| **Repositórios Científicos e Acadêmicos** | [Harvard Dataverse](https://dataverse.org/), [Kaggle Startups & Funding (2024–2025)](https://www.kaggle.com/datasets/sufya6/startups-and-funding-dataset-20242025) e [Brouky VC Dataset](https://brouky.tech/data) | Global (140+ países) | Download de arquivos CSV/JSON acadêmicos, [repositório GitHub Crunchbase R](https://github.com/matthewquerdasi/Startups-Analysis) e licença gratuita via [Crunchbase Research Access](https://data.crunchbase.com/v4-legacy/docs/getting-started) | Histórico longitudinal de rodadas por estágio (`Funding Stage`), valores captados em USD e co-investidores |
| **Benchmarks de Calibração Populacional** | [Publicações ABVCAP](https://abvcap.com.br/publicacoes/), [LAVCA Research](https://www.lavca.org/research/2025-latin-american-startup-ecosystem-insights/) e [Relatórios Distrito](https://www.distrito.me/conteudo/reports) | Brasil e América Latina | Extração de totais agregados trimestrais (ex.: [Consolidação de Dados ABVCAP 2T26](https://abvcap.com.br/publicacao/consolidacao-de-dados-2o-trimestre-2026/)) | Volume total investido por estágio e número de fundos ativos para validação estatística de cobertura da amostra |

### 1.2. Contrato de Dados Alvo (JSON Schema / Modelo Canônico)

Todo registro processado pelo agente na Etapa 2 (Transformação de Dados em Informações) deve ser normalizado para o seguinte contrato estrito antes de alimentar o front-end:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "VentureCapitalFundRecord",
  "type": "object",
  "required": [
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
    "primary_source_url"
  ],
  "properties": {
    "fund_id": {
      "type": "string",
      "description": "Identificador determinístico (hash ou chave composta: domínio web canônico, CNPJ CVM ou CIK SEC)."
    },
    "firm_name": {
      "type": "string",
      "description": "Nome da gestora (Management Company / General Partner)."
    },
    "vehicle_name": {
      "type": "string",
      "description": "Nome oficial do fundo ou veículo (ex.: FIP Capital Semente X ou Fund IV, L.P.)."
    },
    "regulatory_id": {
      "type": ["string", "null"],
      "description": "CNPJ (formato XX.XXX.XXX/0001-XX) na CVM ou CIK de 10 dígitos na SEC EDGAR."
    },
    "geographic_cut": {
      "type": "string",
      "enum": ["BRASIL_LOCAL", "GLOBAL_ATIVO_BR", "GLOBAL_EX_BR"],
      "description": "Corte geográfico estratégico: gestora sediada no Brasil, gestora estrangeira com deal ativo no Brasil, ou gestora global sem atuação no Brasil."
    },
    "hq_country": {
      "type": "string",
      "description": "Código ISO-3166-1 alpha-2 do país-sede da gestora (ex.: BR, US, GB, KY)."
    },
    "target_stages": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "string",
        "enum": ["PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "LATE_STAGE_GROWTH", "VENTURE_DEBT"]
      }
    },
    "stage_classification_method": {
      "type": "string",
      "enum": ["DECLARED_THESIS", "REGULATORY_CLASS_CVM_BNDES", "IMPUTED_BY_TICKET_HEURISTIC"],
      "description": "Rastreabilidade metodológica de como a fase foi atribuída ao fundo."
    },
    "estimated_ticket_min_usd": { "type": ["number", "null"], "minimum": 0 },
    "estimated_ticket_max_usd": { "type": ["number", "null"], "minimum": 0 },
    "aum_or_offering_usd": {
      "type": ["number", "null"],
      "description": "Patrimônio Líquido (CVM convertido para USD) ou Total Offering Amount (SEC Form D)."
    },
    "brazil_deals_count": { "type": "integer", "minimum": 0 },
    "lead_investor_rate": { "type": ["number", "null"], "minimum": 0, "maximum": 1 },
    "active_status": { "type": "boolean" },
    "primary_source_name": { "type": "string" },
    "primary_source_url": { "type": "string", "format": "uri" }
  }
}
```

### 1.3. Regras de Transformação de Dados em Informações Decisórias

Para transformar dados brutos em **informações acionáveis para tomada de decisão**, o agente deve aplicar quatro regras determinísticas:

1. **Deduplicação e Resolução de Entidades (Entity Resolution)**:
   - Agrupar múltiplos veículos (FIPs na [CVM](https://dados.cvm.gov.br/group/fundos-de-investimento) ou emissões [Form D na SEC](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets)) sob a mesma gestora utilizando o `CNPJ_GESTOR` (no Brasil), o domínio web normalizado (extraído via [OpenVC](https://www.openvc.app/investor-database) e [Wikidata](https://query.wikidata.org/)) ou o radical do nome legal do emissor na SEC.
2. **Matriz de Imputação de Fase (Stage Heuristic Engine)**:
   - Prioridade 1 (`DECLARED_THESIS`): Estágio explicitamente declarado pela gestora no [OpenVC](https://www.openvc.app/investor-database), no [Shizune Brazil Dataset](https://shizune.co/investors/venture-capital-vc-funds-brazil) ou no [Brouky Dataset](https://brouky.tech/data).
   - Prioridade 2 (`REGULATORY_CLASS_CVM_BNDES`): Enquadramento oficial na [CVM](https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral) ou no [BNDES](https://www.bndes.gov.br/wps/portal/site/home/mercado-de-capitais/fundos-de-investimentos/fundos-ativos-desinvestimento):
     - `FIP Capital Semente` ou fundo BNDES para empresas com faturamento até R$ 20 milhões → `PRE_SEED` e `SEED`.
     - `FIP Empresas Emergentes` ou fundo BNDES VC para empresas com faturamento até R$ 300 milhões → `SERIES_A` e `SERIES_B`.
     - `FIP Multiestratégia` com patrimônio líquido superior a R$ 500 milhões → `SERIES_B` e `LATE_STAGE_GROWTH`.
   - Prioridade 3 (`IMPUTED_BY_TICKET_HEURISTIC`): Quando apenas o tamanho da rodada ou do fundo está disponível, classificar segundo as faixas empíricas de mercado documentadas pelo [Growth List](https://growthlist.co/blog/startup-funding-stages/) e pelo [Digits](https://digits.com/blog/startup-funding-stages/):
     - Cheque de **US$ 50 mil a US$ 500 mil** → `PRE_SEED`
     - Cheque de **US$ 500 mil a US$ 3 milhões** → `SEED`
     - Cheque de **US$ 3 milhões a US$ 15 milhões** → `SERIES_A`
     - Cheque de **US$ 10 milhões a US$ 50 milhões** → `SERIES_B`
     - Cheque de **US$ 30 milhões a US$ 100 milhões+** → `LATE_STAGE_GROWTH`
3. **Cálculo de Indicadores Executivos (KPIs Derivados)**:
   - **Índice de Liquidez / Dry Powder Ativo**: Identificar fundos com captação registrada nos últimos 36 meses ([Form D](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets)) ou com patrimônio líquido crescente nos últimos 2 informes quadrimestrais ([CVM](https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral)).
   - **Densidade de Cobertura por Estágio (Gap Analysis)**: Calcular a razão entre o número de fundos ativos e o capital total disponível em cada estágio no Brasil versus Global, evidenciando gargalos de financiamento (como o *Series A/B Crunch* no mercado brasileiro).
   - **Índice de Abertura Cross-Border**: Percentual de fundos de porte global (`hq_country != "BR"`) que possuem pelo menos 1 investimento registrado no Brasil segundo o [Shizune Brazil VC Dataset](https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md).

---

## 2. Loop Engineering (Arquitetura de Execução Autônoma e Auto-Verificação)

O agente autônomo deve executar seu trabalho dentro de uma máquina de estados finitos (**Plan → Ingest → Transform → Render → Audit → Refine**) governada por **4 Quality Gates determinísticos**. O agente é proibido de encerrar a execução enquanto qualquer verificação nos Gates retornar falha.

```
[INÍCIO]
   │
   ▼
[FASE 1: INGESTÃO MULTI-FONTE (CVM + BNDES + SHIZUNE + SEC + WIKIDATA + OPENVC)]
   │
   ├──► [GATE 1: AUDITORIA DE INGESTÃO E RATE-LIMIT]
   │       ├─ Falha (HTTP 403/429 ou CSV vazio)? ──► Ajusta User-Agent / Backoff / Usa Fallback Aberto ──► Repete Fase 1 (Máx. 3 tentativas)
   │       └─ Sucesso ──► Avança
   ▼
[FASE 2: TRANSFORMAÇÃO, ENTITY RESOLUTION E CLASSIFICAÇÃO DE ESTÁGIO]
   │
   ├──► [GATE 2: VALIDAÇÃO DE SCHEMA JSON E RASTREABILIDADE ACADÊMICA]
   │       ├─ Falha (Registro sem primary_source_url, duplicata de CNPJ/CIK ou estágio nulo)? ──► Corrige / Imputa via Heurística ──► Repete Fase 2
   │       └─ Sucesso ──► Avança
   ▼
[FASE 3: SÍNTESE EXECUTIVA E GERAÇÃO DO FRONT-END HTML RESPONSIVO]
   │
   ├──► [GATE 3: AUDITORIA ANTI-AI-SLOP (TEXTUAL E VISUAL)]
   │       ├─ Falha (Clichês de IA, gradientes neon, cards ocos, falta de números reais)? ──► Reescreve Copy e CSS ──► Repete Fase 3
   │       └─ Sucesso ──► Avança
   │
   ├──► [GATE 4: AUDITORIA DE UI/UX, ACESSIBILIDADE WCAG AAA E RESPONSIVIDADE]
   │       ├─ Falha (Contraste < 7:1, fonte < 12px, tabela sem scroll horizontal no mobile, links quebrados)? ──► Corrige DOM/CSS ──► Repete Fase 3
   │       └─ Sucesso ──► Avança
   ▼
[ENTREGA FINAL: DATASET AUDITADO + DASHBOARD/RELATÓRIO HTML EXECUTIVO]
```

### 2.1. Especificação dos Quality Gates (Verificações Programáticas)

- **Gate 1 — Integridade de Extração e Conformidade de Rede**:
  - Verifica se todas as chamadas à [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) enviaram o cabeçalho obrigatório `User-Agent: NomeInstituicaoAcad/1.0 (pesquisador@universidade.edu.br)` e respeitaram o intervalo mínimo de `100ms` entre chamadas (máximo de 10 req/s conforme [regras da SEC](https://www.kaggle.com/code/wirelessgus/extracting-data-from-sec-edgar-restful-apis)).
  - Verifica se os arquivos CSV do [Portal de Dados Abertos da CVM](https://dados.cvm.gov.br/group/fundos-de-investimento) foram decodificados corretamente em `ISO-8859-1` (`latin1`) com separador `;` sem corrupção de acentuação.
- **Gate 2 — Conformidade com o Contrato SDD e Zero Alucinação**:
  - Executa validação de schema em 100% dos registros transformados.
  - **Regra de Invariância Acadêmica**: `assert all(row["primary_source_url"].startswith("https://") for row in dataset)`. Nenhum fundo, cifra de Patrimônio Líquido ou faixa de cheque pode ser sintetizado ou inventado; se um atributo secundário não constar na fonte primária, deve receber `null` e ser sinalizado como `Não divulgado na fonte oficial`.
- **Gate 3 — Linter Anti-AI-Slop (Texto e Design)**:
  - **Bloqueio Léxico**: Falha imediatamente se o HTML contiver expressões genéricas de IA (ex.: *"No dinâmico cenário atual"*, *"ecossistema vibrante"*, *"revolucionando o mercado"*, *"sinergia holística"*, *"mergulhe fundo"*, *"Conclusão"*, *"Resumo Executivo"* genérico sem tese).
  - **Bloqueio Visual**: Falha se o CSS contiver gradientes decorativos roxo/azul/ciano (`linear-gradient` em fundos de página ou texto), sombras difusas pesadas (`box-shadow` > 4px), ícones de foguete/lâmpada/robô, ou caixas de estatísticas vazias sem comparação nem fonte.
- **Gate 4 — Validação de UI/UX Executivo e Responsividade**:
  - Verifica se o piso tipográfico é `>= 12px` em todos os elementos (incluindo eixos de gráficos SVG, notas de rodapé e cabeçalhos de tabela).
  - Verifica se todos os valores monetários e percentuais em tabelas possuem `font-variant-numeric: tabular-nums` e `white-space: nowrap` (nunca quebrando `R$ 150,0 mi` ou `US$ 15,0M` em duas linhas).
  - Verifica se o layout adapta-se sem sobreposição em viewports de `375px` (mobile), `768px` (tablet) e `1440px` (desktop widescreen).

---

## 3. Diretrizes Estritas de UI/UX Executivo e Prevenção de AI Slop

Para garantir que o HTML gerado tenha o padrão de uma publicação analítica de referência (como relatórios do *Financial Times*, *Bloomberg Terminal Web* ou *Stripe Press*) e sirva diretamente à tomada de decisão:

| Dimensão | Proibido (Padrão AI Slop) | Obrigatório (Padrão Executivo de Decisão) |
| :--- | :--- | :--- |
| **Abertura da Página (Hero)** | Banner genérico com frase vaga (*"Bem-vindo ao Dashboard de Venture Capital"*) e botões falsos sem ação. | **Headline Assertiva (Answer-First)**: Uma frase declarativa que entrega a principal conclusão quantitativa da coleta logo no topo (ex.: *"Fundos locais concentram 68% dos veículos em Seed e Série A, enquanto o capital para Série B+ no Brasil depende de gestoras globais cross-border"*). |
| **Paleta de Cores** | Tema escuro neon (`#0f0c29`, `#302b63`), roxo elétrico (`#8A2BE2`), ciano brilhante ou fundos bege/creme pasteurizados em todos os blocos. | Superfície clara de alta precisão (`#FFFFFF` e cinza técnico `#F4F6F8`), texto em ardósia profunda (`#0F172A`, contraste WCAG AAA `>= 7:1`), e codificação semântica funcional: **Azul Cobalto (`#0043CE`)** para o corte Global e **Verde Esmeralda (`#047857`)** para o corte Brasil. |
| **Tipografia** | Uso de uma única fonte padrão do sistema sem hierarquia ou textos centralizados longos. | Sistema tipográfico em 3 papéis via Google Fonts: **Display/Títulos** em `Space Grotesk` (peso 600–700, tracking `-0.02em`), **Prosa Analítica** em `IBM Plex Sans` (`16px`, largura máxima de leitura `68ch`) e **Dados/Tabelas/Códigos** em `IBM Plex Mono` (`tabular-nums`). |
| **Arquitetura de Layout** | Grade monótona de 3 ou 4 cartões iguais com bordas arredondadas (`border-radius: 16px`) e `box-shadow` repetidos ao longo de toda a página. | **Hierarquia Editorial Assimétrica**: Separação estrutural por linhas de `1px solid #CBD5E1` (hairlines), espaço em branco generoso, faixas de métricas alinhadas à base (*baseline-aligned*) e tabelas abertas de alta densidade. |
| **Visualização de Dados** | Gráficos de pizza/rosca com mais de 4 fatias, barras sem rótulos diretos ou gráficos com eixos cortados/sobrepostos. | **Gráficos SVG Nativos ou Canvas Semântico**: Gráficos de barras horizontais empilhadas/agrupadas (Brasil vs. Global por estágio), curva de distribuição de tíquete médio e matriz de cobertura, sempre com título conclusivo (verbo ativo) e `<figcaption>` citando a unidade e a fonte ([CVM](https://dados.cvm.gov.br/group/fundos-de-investimento), [SEC](https://data.sec.gov/), [OpenVC](https://www.openvc.app/investor-database)). |
| **Interatividade de Decisão** | Filtros quebrados ou excesso de animações (`bounce`, `pulse`, transições lentas). | Controles rápidos e instantâneos (zero latência): filtro por **Corte Geográfico** (`Todos`, `Brasil Local`, `Global Ativo no Brasil`, `Global Ex-Brasil`), filtro por **Fase de Startup** (`Pre-Seed`, `Seed`, `Série A`, `Série B`, `Late Stage`), busca por nome/CNPJ/CIK e ordenação por Patrimônio/Cheque. |

---

## 4. Master Prompt para o Agente Autônomo (Pronto para Uso)

Copie o bloco abaixo e utilize-o como **System Prompt / Instrução Mestra** no seu orquestrador de agentes (LangGraph, CrewAI, AutoGen, Claude Code, Gemini Agent Mode ou cursor/openhands):

```markdown
# ROLE & OBJECTIVE
Você é um Engenheiro de Dados Sênior, Pesquisador Quantitativo de Venture Capital e Arquiteto de Front-End Executivo.
Sua missão é executar de forma autônoma um pipeline completo de Extração, Transformação e Renderização (ETR) para mapear fundos de investimento de Venture Capital (VC) por fase de financiamento de startups (Pre-Seed, Seed, Série A, Série B, Late Stage/Growth e Venture Debt) em dois cortes geográficos:
1. **Brasil** (Gestoras nacionais registradas na CVM/BNDES e gestoras globais com investimentos ativos no Brasil);
2. **Global / Resto do Mundo** (Gestoras internacionais atuando nos demais mercados).

O levantamento tem finalidade acadêmica e estratégica: utilize EXCLUSIVAMENTE APIs públicas gratuitas, microdados governamentais abertos e repositórios científicos/comunitários gratuitos.

---

# SPEC-DRIVEN DEVELOPMENT (SDD) — CONTRATOS TÉCNICOS

## 1. Fontes Obrigatórias de Extração (Etapa 1)
Você deve extrair, cruzar e referenciar dados reais a partir das seguintes fontes abertas:
- **Brasil (Regulatório e Fomento)**:
  - Portal de Dados Abertos da CVM (`https://dados.cvm.gov.br/group/fundos-de-investimento`) e Informe Quadrimestral de FIPs (`https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral`) -> extrair CNPJ do fundo, denominação social, CNPJ da gestora, patrimônio líquido e classe (`Capital Semente`, `Empresas Emergentes`, `Multiestratégia`).
  - Portal de Dados Abertos do BNDES — Renda Variável BNDESPAR (`https://dadosabertos.bndes.gov.br/fa_IR/dataset/renda-variavel`) e Carteira de Fundos BNDES (`https://www.bndes.gov.br/wps/portal/site/home/mercado-de-capitais/fundos-de-investimentos/dados-agregados`) -> distinguir fundos de Capital Semente (empresas com receita até R$ 20 mi) e Venture Capital (receita até R$ 300 mi).
  - Finep / MCTI Dados Abertos (`https://www.gov.br/mcti/pt-br/acesso-a-informacao/dados-abertos/dados-abertos` e `https://www.finep.gov.br/fundos-de-investimento`).
- **Brasil (Atividade de Mercado e Co-investimento)**:
  - Shizune Brazil VC Dataset (`https://shizune.co/investors/venture-capital-vc-funds-brazil` e exportação direta `https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md`) -> extrair número de deals no Brasil, estágios de atuação, tamanho de rodada e taxa de liderança (lead/follow).
  - Séries agregadas de validação: Publicações ABVCAP (`https://abvcap.com.br/publicacoes/`) e Relatórios Distrito (`https://www.distrito.me/conteudo/reports`).
- **Global (APIs Públicas e Bases Abertas)**:
  - SEC EDGAR API (`https://data.sec.gov/` e `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`) + SEC Form D Data Sets (`https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets`) -> enviar cabeçalho HTTP `User-Agent: PesquisaAcademicaVC/1.0 (contato@universidade.edu.br)`, respeitar limite de 10 req/s e filtrar `<investmentFundType>Venture Capital Fund</investmentFundType>`.
  - Wikidata Query Service SPARQL (`https://query.wikidata.org/`) -> consultar entidades `wd:Q1961622` (venture capital firm), país-sede (`P17`), fundação (`P571`) e website (`P856`).
  - OpenVC Database (`https://www.openvc.app/investor-database`) e The VC List of VC Lists (`https://www.openvc.app/blog/vc-list`) -> extrair cheque mínimo/máximo e estágios declarados.
  - Repositórios científicos: Harvard Dataverse (`https://dataverse.org/`), Kaggle Startups & Funding Dataset (`https://www.kaggle.com/datasets/sufya6/startups-and-funding-dataset-20242025`) e Brouky VC Dataset (`https://brouky.tech/data`).

## 2. Regras de Transformação em Informação Executiva (Etapa 2)
Não despeje dados brutos. Transforme os dados coletados aplicando:
1. **Entity Resolution**: Unifique múltiplos veículos (FIPs ou Form D LP funds) sob a mesma Gestora usando CNPJ da gestora, CIK ou domínio web canônico.
2. **Harmonização e Imputação de Estágio**:
   - Padronize os estágios no enum: `["PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "LATE_STAGE_GROWTH", "VENTURE_DEBT"]`.
   - Use o estágio declarado (OpenVC/Shizune) como Prioridade 1; a classe regulatória CVM/BNDES como Prioridade 2 (`FIP Capital Semente` -> `PRE_SEED`/`SEED`; `FIP Empresas Emergentes` -> `SERIES_A`/`SERIES_B`; `FIP Multiestratégia` -> `SERIES_B`/`LATE_STAGE_GROWTH`); e a heurística de cheque médio (Growth List: Pre-Seed US$ 50k–500k; Seed US$ 500k–3M; Série A US$ 3M–15M; Série B US$ 10M–50M; Série C+ US$ 30M–100M+) como Prioridade 3, registrando sempre o método utilizado no campo `stage_classification_method`.
3. **Síntese para Tomada de Decisão**:
   - Calcule a concentração de fundos por estágio no Brasil vs. Global.
   - Identifique lacunas de financiamento (*funding gaps* por fase).
   - Destaque a participação de fundos internacionais (`GLOBAL_ATIVO_BR`) nas rodadas de expansão (Série B+) de startups brasileiras.

## 3. Especificação do Front-End HTML Responsivo e Executivo (Etapas 3, 4, 5 e 6)
Gere um único arquivo `.html` autossuficiente, responsivo e orientado à tomada de decisão executiva e acadêmica:
- **Arquitetura Visual e Tipográfica**:
  - Carregue `Space Grotesk` (títulos e KPIs), `IBM Plex Sans` (prosa analítica e rótulos) e `IBM Plex Mono` (valores financeiros, CNPJs, CIKs e cabeçalhos técnicos) via Google Fonts.
  - Paleta de alto contraste (WCAG AAA >= 7:1): fundo `#FFFFFF` e `#F4F6F8`, texto `#0F172A`, linhas divisórias finas `1px solid #CBD5E1`, Azul Cobalto `#0043CE` (Global) e Verde Esmeralda `#047857` (Brasil).
  - Piso absoluto de tamanho de fonte: `12px` em qualquer elemento da página (incluindo eixos de gráficos SVG e notas de rodapé).
  - Todos os números em tabelas devem usar `font-variant-numeric: tabular-nums` e `white-space: nowrap`.
- **Componentes de UI/UX Obrigatórios**:
  1. **Header Answer-First**: Título conclusivo resumindo a descoberta central + linha de 4 KPIs principais com comparação direta Brasil vs. Global.
  2. **Barra de Filtragem e Busca Executiva**: Controles limpos para filtrar a visualização e a tabela por Corte Geográfico (`Todos`, `Brasil Local`, `Global Ativo no Brasil`, `Global Ex-Brasil`), por Fase (`Pre-Seed`, `Seed`, `Série A`, `Série B`, `Late Stage`) e busca textual instantânea.
  3. **Visualizações Quantitativas em SVG Nativo**:
     - Gráfico 1: Distribuição comparativa de fundos por fase de maturidade (Brasil vs. Global).
     - Gráfico 2: Faixas de cheque (ticket size em USD) e mapeamento regulatório CVM/BNDES vs. SEC Form D.
     - Todo gráfico deve estar dentro de `<figure>` com um título declarativo contendo verbo ativo (conclusão do gráfico) e `<figcaption>` informando unidade de medida e fonte clicável.
  4. **Matriz de Tomada de Decisão e Tabela Auditável de Fundos**: Tabela responsiva com ordenação por coluna, exibindo Gestora, Veículo/Fundo, Corte Geográfico, Fases de Atuação (badges semânticos sóbrios), Faixa de Cheque / Patrimônio, Método de Classificação e Link Direto para a Fonte Oficial (`<a href="..." target="_blank" rel="noopener noreferrer">`).
  5. **Nota Metodológica e Referências**: Seção final documentando o protocolo de coleta para reprodutibilidade científica.

---

# GUARDRAILS ANTI-AI-SLOP (PROIBIÇÕES ESTRITAS)
1. **Zero Clichês Textuais**: É terminantemente proibido usar frases vazias como "No mundo acelerado das startups", "ecossistema vibrante", "mergulhe nos dados", "revolucionário", "holístico", "sinergia" ou títulos vagos como "Introdução" e "Conclusão". Escreva com precisão analítica, voz ativa e dados concretos.
2. **Zero AI Slop Visual**: Proibido usar gradientes roxos/cianos, sombras pesadas, cards decorativos sem dados, gráficos de pizza com múltiplas fatias, emojis em cabeçalhos ou ícones genéricos.
3. **Zero Dados Inventados**: Nunca invente nomes de fundos, CNPJs, valores de patrimônio ou URLs. Todo registro deve apontar para sua URL real de origem.

---

# LOOP ENGINEERING — PROTOCOLO DE AUTO-VERIFICAÇÃO ANTES DA ENTREGA
Execute internamente o ciclo abaixo e só entregue o arquivo final quando todos os itens passarem:
1. **[Check de Extração]**: Os dados dos cortes `BRASIL_LOCAL`, `GLOBAL_ATIVO_BR` e `GLOBAL_EX_BR` foram carregados e normalizados segundo o JSON Schema?
2. **[Check de Transformação]**: Todos os fundos possuem pelo menos uma fase atribuída (`PRE_SEED` a `LATE_STAGE_GROWTH`) e a coluna `stage_classification_method` preenchida?
3. **[Check Anti-Slop]**: Há alguma frase genérica de IA, gradiente neon ou métrica sem fonte? (Se sim, remova e reescreva).
4. **[Check de UI/UX & Responsividade]**: O HTML possui contraste >= 7:1, fontes >= 12px, tabelas com `overflow-x: auto` para telas móveis e todos os links `<a href="https://...">` funcionais?
```

---

## 5. Exemplo de Script Orquestrador do Loop de Extração e Validação (Python)

Para acoplar ao agente autônomo na Etapa 1 e Etapa 2, o esqueleto abaixo implementa os contratos de extração gratuita ([CVM](https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral), [Shizune Brazil](https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md), [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) e [Wikidata SPARQL](https://query.wikidata.org/)) junto com o validador determinístico do **Gate 2**:

```python
import time
import urllib.request
import json
from typing import List, Dict, Any

SEC_USER_AGENT = "PesquisaAcademicaVC/1.0 (pesquisador@universidade.edu.br)"

def fetch_with_retry(url: str, headers: Dict[str, str], retries: int = 3, delay: float = 0.2) -> bytes:
    """Executa requisição HTTP respeitando rate-limit da SEC EDGAR (máx 10 req/s) e retry com backoff."""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                time.sleep(delay)
                return response.read()
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f"Falha na extração de {url} após {retries} tentativas: {exc}") from exc
            time.sleep(delay * (2 ** attempt))
    return b""

def classify_stage_by_cvm_and_ticket(cvm_class: str, ticket_usd: float | None) -> tuple[List[str], str]:
    """Aplica a régua SDD de conversão de fase (CVM/BNDES + Heurística Growth List)."""
    cvm_upper = (cvm_class or "").upper()
    if "SEMENTE" in cvm_upper:
        return (["PRE_SEED", "SEED"], "REGULATORY_CLASS_CVM_BNDES")
    if "EMERGENTE" in cvm_upper:
        return (["SERIES_A", "SERIES_B"], "REGULATORY_CLASS_CVM_BNDES")
    if "MULTIESTRAT" in cvm_upper:
        return (["SERIES_B", "LATE_STAGE_GROWTH"], "REGULATORY_CLASS_CVM_BNDES")

    if ticket_usd is not None:
        if ticket_usd <= 500_000:
            return (["PRE_SEED"], "IMPUTED_BY_TICKET_HEURISTIC")
        if ticket_usd <= 3_000_000:
            return (["SEED"], "IMPUTED_BY_TICKET_HEURISTIC")
        if ticket_usd <= 15_000_000:
            return (["SERIES_A"], "IMPUTED_BY_TICKET_HEURISTIC")
        if ticket_usd <= 50_000_000:
            return (["SERIES_B"], "IMPUTED_BY_TICKET_HEURISTIC")
        return (["LATE_STAGE_GROWTH"], "IMPUTED_BY_TICKET_HEURISTIC")

    return (["SEED", "SERIES_A"], "IMPUTED_BY_TICKET_HEURISTIC")

def gate_2_validate_records(records: List[Dict[str, Any]]) -> None:
    """Gate 2 do Loop Engineering: bloqueia registros sem fonte verificável ou fora do contrato SDD."""
    valid_cuts = {"BRASIL_LOCAL", "GLOBAL_ATIVO_BR", "GLOBAL_EX_BR"}
    valid_stages = {"PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "LATE_STAGE_GROWTH", "VENTURE_DEBT"}

    if not records:
        raise ValueError("Gate 2 Falhou: O dataset transformado está vazio.")

    for idx, rec in enumerate(records):
        assert rec.get("fund_id"), f"Registro {idx} sem fund_id."
        assert rec.get("geographic_cut") in valid_cuts, f"Corte inválido no registro {rec.get('fund_id')}."
        stages = rec.get("target_stages", [])
        assert stages and set(stages).issubset(valid_stages), f"Estágios inválidos em {rec.get('fund_id')}."
        url = rec.get("primary_source_url", "")
        assert url.startswith("https://"), f"Fonte primária ausente ou inválida em {rec.get('fund_id')}."
```

---

## 6. Checklist de Auditoria Final (Critérios de Aceite do Entregável)

Antes de encerrar o loop de execução, o agente autônomo valida os 6 requisitos objetivos do projeto:

1. **Extração Multi-Fonte Concluída**: Dados coletados gratuitamente cobrindo Brasil ([CVM FIPs](https://dados.cvm.gov.br/dataset/fip-doc-inf_quadrimestral), [BNDESPAR](https://dadosabertos.bndes.gov.br/fa_IR/dataset/renda-variavel), [Shizune Brazil](https://shizune.co/investors/venture-capital-vc-funds-brazil/table.md)) e Global ([SEC EDGAR API](https://data.sec.gov/), [Form D](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets), [Wikidata SPARQL](https://query.wikidata.org/), [OpenVC](https://www.openvc.app/investor-database)).
2. **Transformação em Informação**: Deduplicação por gestora concluída, fases padronizadas com rastreabilidade (`stage_classification_method`) e KPIs comparativos calculados.
3. **Renderização HTML Responsiva**: Layout fluído em grid de 12 colunas com pontos de quebra para mobile/tablet e tabelas com rolagem horizontal limpa.
4. **Tom Executivo Orientado à Decisão**: Estrutura *Answer-First* destacando onde estão as oportunidades de captação, a liquidez (*dry powder*) e os gargalos por estágio.
5. **Zero AI Slop**: Ausência total de jargões genéricos de IA, gradientes decorativos, sombras pesadas ou métricas fictícias.
6. **Boas Práticas de UI/UX**: Contraste WCAG AAA (`>= 7:1`), tipografia hierárquica (`Space Grotesk`, `IBM Plex Sans`, `IBM Plex Mono`), números tabulares alinhados e links diretos para as fontes oficiais.
