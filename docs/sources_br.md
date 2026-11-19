# Fontes Brasil — ingestão regulatória oficial (CVM + BNDES)

Gerado por `pipeline/collect_br.py` em 2026-11-18T15:52:35+00:00 (UTC). Todo download passa por
`pipeline/net.py`: User-Agent acadêmico declarado (`PesquisaAcademicaVC/1.0 (pesquisador@universidade.edu.br)`), rate-limit,
retry/backoff, cache em `data/raw/` e registro auditável em `data/manifest.jsonl`
(evidência do Gate 1). Segunda execução funciona offline pelo cache (`--offline`).

**Resultado da coleta:** 2.147 veículos FIP ativos
(a partir de 2.175 linhas ativas do registro CVM;
27 casos de gestor duplicado foram deduplicados),
418 gestoras e 881
linhas de carteira de fundos do BNDESPAR. PL não nulo em 2.075 fundos
(soma R$ 989954876823.65); capital comprometido e integralizado em
2.076 fundos na competência `2026-08-31`.

## 1. Universo e recorte

- Base viva: **Registro de Fundos, Classes e Subclasses de Cotas (RCVM 175)** —
  dataset CVM `fi-cad`, arquivo `registro_fundo_classe.zip`.
- Filtro deterministico do universo: `registro_fundo.csv` com
  `Tipo_Fundo = 'FIP'` **e** `Situacao = 'Em Funcionamento Normal'`
  → **2.147 veículos FIP únicos** a partir de
  2.175 linhas ativas (registro completo:
  91.185 fundos; 37.154 classes;
  11.888 subclasses).

> **Dedupe de gestor:** 2.175 linhas ativas → **2.147 veículos únicos**
> após dedupe de gestor (27 casos); **418 gestoras** efetivas.
> A CVM não publica data de vigência de gestor no dataset; a escolha é a melhor inferência
> determinística disponível e todos os nomes alternativos ficam preservados.

- **Duplicidade na fonte**: 27 IDs de registro ativo aparecem em 2–3
  linhas idênticas exceto `Gestor`/`CPF_CNPJ_Gestor` (provável transição de gestor sem
  data de vigência no arquivo). Como o contrato exige 1 registro por veículo e
  `record_id` = CNPJ, o coletor deduplica por `ID_Registro_Fundo` com regra
  determinística: **c1** gestor ≠ `CNPJ_Administrador`; **c2** maior frequência de
  gestor no universo FIP ativo; **c3** menor CNPJ de gestor. A CVM **não publica data de
  vigência de gestor** neste dataset, portanto esta é a melhor inferência
  determinística disponível; os nomes alternativos ficam **preservados** em
  `data/interim/br_sources.json → registry_duplicates` (com `selected: true/false` na
  lista `rows` de cada caso e o `criterion` decisivo c1/c2/c3).

| ID registro | CNPJ fundo | gestor escolhido (critério) | alternativas descartadas |
|---|---|---|---|
| 2725 | `08277553000106` | OPPORTUNITY PRIVATE EQUITY GESTORA DE RECURSOS LTDA (**c2**) | MININVEST GESTORA DE RECURSOS LTDA (`16631215000140`) |
| 2806 | `09028916000124` | KPTL INVESTIMENTOS LTDA. (**c2**) | ANTERA GESTÃO DE RECURSOS LTDA (`08096466000144`) |
| 2818 | `09130400000196` | COPA GESTAO DE INVESTIMENTOS LTDA (**c2**) | PRINCIPAL ASSET MANAGEMENT LTDA. (`03987891000100`) |
| 2937 | `10597218000129` | OPPORTUNITY PRIVATE EQUITY GESTORA DE RECURSOS LTDA (**c2**) | OPPORTUNITY HDF ADMINISTRADORA DE RECURSOS LTDA. (`33857830000199`) |
| 3957 | `21669033000107` | CYPRESS ASSOCIATES GESTÃO E PARTICIPAÇÕES LTDA (**c1**) | OLIVEIRA TRUST SERVICER S/A (`02150453000120`) |
| 4489 | `31844283000181` | ASTELLA INVESTIMENTOS, ASSESSORIA, GESTÃO E PARTICIPAÇÕES LTDA (**c2**) | BTG PACTUAL GESTÃO E CONSULTORIA DE INVESTIMENTOS LTDA (`12695840000103`) |
| 4669 | `34846943000151` | FIDD ADMINISTRAÇÃO DE RECURSOS LTDA (**c1**) | OSLO CAPITAL DISTRIBUIDORA DE TÍTULOS E VALORES MOBILIÁRIOS S.A. (`13673855000125`) |
| 4731 | `35780284000160` | FIDD ADMINISTRAÇÃO DE RECURSOS LTDA (**c1**) | OSLO CAPITAL DISTRIBUIDORA DE TÍTULOS E VALORES MOBILIÁRIOS S.A. (`13673855000125`) |
| 4923 | `39857660000129` | KPTL INVESTIMENTOS LTDA. (**c2**) | CEDRO ASSET MANAGEMENT LTDA (`18190296000116`) |
| 5085 | `41395116000154` | EUQUEROINVESTIR GESTÃO DE RECURSOS LTDA (**c2**) | BURI ASSET MANAGEMENT LTDA (`10927936000116`) |
| 5091 | `41519970000185` | INVISTO GESTÃO DE INVESTIMENTOS S/A (**c2**) | DSK CAPITAL GESTORA DE RECURSOS E CONSULTORIA LTDA. (`41249846000147`) |
| 5205 | `42754355000116` | KINEA PRIVATE EQUITY INVESTIMENTOS S/A (**c2**) | KINEA INVESTIMENTOS LTDA. (`08604187000144`) |
| 5216 | `42847164000107` | EUQUEROINVESTIR GESTÃO DE RECURSOS LTDA (**c3**) | PIPO CAPITAL GESTÃO DE INVESTIMENTOS LTDA. (`42330305000101`) |
| 5221 | `42869937000148` | XP ADVISORY GESTAO DE RECURSOS LTDA (**c3**) | MONTE BRAVO GESTÃO DE RECURSOS LTDA. (`20710453000119`) |
| 5505 | `46641587000119` | CRESCERA VENTURE LTDA. (**c2**) | TRIAXIS CAPITAL LTDA (`15333310000103`) |
| 5609 | `48328531000134` | TREECORP PARTNERS GESTORA LTDA (**c2**) | OUTFIELD VENTURES S.A. (`49627380000188`) |
| 5670 | `49723694000184` | KINEA PRIVATE EQUITY INVESTIMENTOS S/A (**c2**) | KINEA INVESTIMENTOS LTDA. (`08604187000144`) |
| 5922 | `52923564000108` | FIDD ADMINISTRAÇÃO DE RECURSOS LTDA (**c1**) | OSLO CAPITAL DISTRIBUIDORA DE TÍTULOS E VALORES MOBILIÁRIOS S.A. (`13673855000125`) |
| 5931 | `53076417000102` | FIDD ADMINISTRAÇÃO DE RECURSOS LTDA (**c1**) | OSLO CAPITAL DISTRIBUIDORA DE TÍTULOS E VALORES MOBILIÁRIOS S.A. (`13673855000125`) |
| 6260 | `56616235000167` | LESTE FINANCIAL SERVICES GESTÃO DE RECURSOS LTDA. (**c2**) | ARTON GESTÃO DE RECURSOS LTDA (`08806258000191`) |
| 6396 | `58224396000130` | KINEA PRIVATE EQUITY INVESTIMENTOS S/A (**c2**) | KINEA INVESTIMENTOS LTDA. (`08604187000144`) |
| 6548 | `60145342000121` | CRESCERA VENTURE LTDA. (**c2**) | TRIAXIS CAPITAL LTDA (`15333310000103`) |
| 6791 | `63680293000115` | ORE INVESTMENTS PARTICIPAÇÕES LTDA. (**c2**) | REGIA CAPITAL LTDA (`54464258000178`); BB GESTAO DE RECURSOS DTVM S.A (`30822936000169`) |
| 6879 | `64776905000130` | HEDGE INVESTMENTS REAL ESTATE GESTÃO DE RECURSOS LTDA. (**c2**) | ARTON GESTÃO DE RECURSOS LTDA (`08806258000191`) |
| 6935 | `65881979000108` | KINEA PRIVATE EQUITY INVESTIMENTOS S/A (**c2**) | KINEA INVESTIMENTOS LTDA. (`08604187000144`) |
| 7000 | `67251103000103` | KINEA PRIVATE EQUITY INVESTIMENTOS S/A (**c2**) | KINEA INVESTIMENTOS LTDA. (`08604187000144`) |
| 7046 | `68498216000162` | HEDGE INVESTMENTS REAL ESTATE GESTÃO DE RECURSOS LTDA. (**c2**) | VGR GESTÃO DE RECURSOS LTDA. (`14569262000186`) |

- `cad_fi.csv` **não** é usado como base. A metadata do CKAN o descreve como
  *"Fundos de Investimento - Não Adaptados RCVM175"*: no arquivo, 46.590 de 46.805
  linhas estão `CANCELADA` — usá-lo como universo produziria dado morto. Ele entra
  apenas como evidência SECUNDÁRIA do campo `CLASSE` legado
  (`FIP CS`/`FIP EE`/`FIP Multi`/...) para os CNPJs de FIP ativo que também constam lá.
- Joins sempre por CNPJ **somente dígitos**: o registro usa `06033235000166` e o
  informe usa `06.033.235/0001-66` (sem normalização o join falha).

### Membros do ZIP (fonte primária)

| membro | linhas | bytes | sha256 |
|---|---:|---:|---|
| `registro_classe.csv` | 37154 | 16398210 | `d712aadf2bb57128b51e1fef3d5fcff282538a55d242eef33040b5e206dcf499` |
| `registro_fundo.csv` | 91185 | 27222962 | `2f0d6e4825661e56e67a48c7215492f6a69e4e54b631098e880141957296d8c9` |
| `registro_subclasse.csv` | 11888 | 1866937 | `980798cac1344c2c9659ff63d4f555429c59b5330b673d7db514a7339548756c` |

## 2. Campos e regras de transformação

- `pl_brl`: soma de `Patrimonio_Liquido` das **classes** do fundo (`registro_classe`)
  quando existe; senão `Patrimonio_Liquido` do `registro_fundo`. `pl_source` registra
  a origem. `data_patrimonio_liquido` é a maior data observada entre as classes somadas.
- `pl_history`: série do **Informe Quadrimestral de FIP** (2024/2025/2026) por fundo,
  somando o `VL_PATRIM_LIQ` das classes do fundo em cada competência. Competências
  `<= hoje`, ordenadas. O informe traz várias linhas por classe (detalhe por
  `CLASSE_COTA` e reapresentações); o coletor deduplica por
  `(competência, CNPJ_FUNDO_CLASSE)` mantendo a última ocorrência do arquivo; os
  valores de PL/capital são da classe e se repetem nas linhas — por isso **não** se
  soma por `CLASSE_COTA`. Competências disponíveis:
  `2024-03-31, 2024-04-30, 2024-08-31, 2024-12-31, 2025-04-30, 2025-08-31, 2025-12-31, 2026-04-30, 2026-08-31` (competência mais recente:
  `2026-08-31`; `2026-12-31` existe no arquivo mas é futura e foi excluída).
- `capital_comprometido_brl` / `capital_integralizado_brl`: soma de
  `VL_CAP_COMPROM` / `VL_CAP_INTEGR` das classes do fundo na competência mais recente
  disponível (`capital_competencia`). Base do KPI de dry powder
  (comprometido − integralizado); o coletor **não** calcula o KPI, só publica os dois
  valores oficiais. Dicionário CVM: *"Valor total do capital comprometido"* e
  *"Valor total do capital integralizado"*.
- `class_signal`: sinal **bruto** de subclasse extraído da `Denominacao_Social`
  registrada (`SEMENTE`, `EMERGENTE`, `MULTIESTRAT`, `null`), com
  `class_signal_source="denominacao_social"`. Sob a RCVM 175 **não existe campo
  estruturado** de tipo de FIP nos dados abertos; o coletor **não** decide estágio
  (isso é responsabilidade do transform, com `stage_classification_method`).
- `bndes_backed` / `bndes_evidencias`: cruzamento por CNPJ normalizado com a carteira
  de fundos do BNDESPAR (match pelo CNPJ do fundo **ou** pelo CNPJ da classe).
  `participacao_pp_final` é preservada como texto da fonte.
- `legacy_cvm_class`: valor histórico de `cad_fi.csv` (`CLASSE`) quando o CNPJ do FIP
  ativo aparece no arquivo legado (fundos adaptados mantêm o registro antigo
  cancelado). **Não** substitui o sinal de subclasse da fonte viva.
- Campos ausentes na fonte ficam `null` e são listados em `payload._undisclosed`.

### Cobertura observada (br_funds = 2147 FIPs ativos)

| métrica | valor |
|---|---:|
| PL não nulo | 2.075 |
| PL nulo | 72 |
| PL <= 0 (valor real da fonte, não truncado) | 54 |
| PL total somado (BRL) | 989954876823.65 |
| PL mediano (BRL) | 71.587.909,45 |
| `pl_history` com a competência mais recente | 2.060 |
| `pl_history` com >= 2 competências | 1.991 |
| capital comprometido disponível | 2.076 |
| capital comprometido > 0 | 1.799 |
| capital integralizado disponível | 2.076 |
| fundos com evidência BNDESPAR | 42 |
| fundos com `legacy_cvm_class` | 18 |

Fontes de PL: `{"registro_classe": 2073, "null": 72, "registro_fundo": 2}`.
Sinais de subclasse: `{"MULTIESTRAT": 1605, "EMERGENTE": 52, "SEMENTE": 56, "null": 434}`.

### Fontes de PL (`pl_source`)

- `registro_classe`: soma do PL das classes registradas.
- `registro_fundo`: fundo ainda sem classes no registro → PL do próprio fundo.

## 3. BNDES — carteira de fundos do BNDESPAR

Arquivo: *Fundos de Investimentos — Histórico da Carteira* (Renda Variável).
**881 linhas**, **95 CNPJs distintos**,
`tipo_de_ativo='COTAS DE FUNDO'`, `setor_de_atividade='N/A'`,
`participacao_pp_final` em % (texto com vírgula decimal, preservado; validado em [0,100]).
O CSV **não** traz classificação Capital Semente / Venture Capital nem valores em R$
para a carteira de fundos; nada disso é inventado. O cruzamento por CNPJ normalizado
identifica 42 FIPs ativos com coinvestimento público BNDESPAR
(`bndes_backed=true` em `br_funds.jsonl`, match por CNPJ do fundo ou da classe).

## 4. Fontes opcionais / indisponíveis

- `finep_fundos` — HTTP 200 — https://www.finep.gov.br/fundos-de-investimento: fonte responde HTML institucional, sem dataset tabular de fundos (Finep/MCTI); nao ha CSV/JSON oficial de carteira de fundos nesta URL - nao extraido, nada inventado
- `mcti_dados_abertos` — HTTP 200 — https://www.gov.br/mcti/pt-br/acesso-a-informacao/dados-abertos/dados-abertos: fonte responde HTML institucional, sem dataset tabular de fundos (Finep/MCTI); nao ha CSV/JSON oficial de carteira de fundos nesta URL - nao extraido, nada inventado

## 5. Encoding e integridade (Gate 1)

- Todos os CSV da CVM e do BNDES são **ISO-8859-1 (`latin1`)** com separador `;`;
  cada arquivo é validado: não decodifica como UTF-8 estrito, é lido como latin1 e o
  texto final é gravado em UTF-8 sem `U+FFFD` (checagem programática no coletor).
- Evidência de rede em `data/manifest.jsonl` (status HTTP, bytes, sha256, User-Agent,
  intervalo mínimo, `gate1_ok`) e cache binário em `data/raw/`.
- Nesta execução: 14 chamadas registradas, **todas HTTP 200**
  (ver `br_sources.json → gate1`); a segunda execução do coletor reutiliza 100% do cache
  (comprovado com `VC_OFFLINE=1`).

### Proveniência registrada nesta execução (detalhe em `data/interim/br_sources.json`)

| fonte | HTTP | bytes | sha256 | encoding | linhas | origem |
|---|---:|---:|---|---:|---:|---|
| cvm_ckan | 200 | 9.832 | `a6a4cfbb31a9bb2e7cde07bd24c9b9577a3514b147fbd33c0a9a5fda98ec3839` | utf-8 | - | cache |
| cvm_ckan | 200 | 5.270 | `c775ff547874936b0a2fd88df94c56e3f1cfa0167b4ecd0c757f05f7617415b2` | utf-8 | - | cache |
| cvm_ckan | 200 | 2.222 | `4a5e912031576fa49c1faf693461f8cf65bcdb6bcba4e0e6c952d2310ba9c415` | binary | - | cache |
| cvm_ckan | 200 | 7.608 | `dfc02ac21960c0666c8aa461cf88c0ca1ec31214662ef49f25582efd78957e06` | latin1 | - | cache |
| cvm_ckan | 200 | 13.084 | `1cab9fb27223c6615fb292f93b927654771b7977fcbb8947a7880a32be135d8e` | latin1 | - | cache |
| cvm_registro_fundo_classe | 200 | 6.903.816 | `ccfc64b4e45e32d61a51affe99c8701ec5bf352eae5b1f5958e54218c1c96086` | binary | 3 membros (ver br_sources) | cache |
| cvm_cad_fi | 200 | 17.920.929 | `da26418abd0dcf61d741ccb8d47c95323fe7d3b199f30400c6ef0637dc4709ce` | latin1 | 46805 | cache |
| cvm_inf_quadrimestral_fip | 200 | 4.685.908 | `d76d3484a01829d11f019a9b90e056c51f9c635260d16fb802087bc03cb08198` | latin1 | 6821 | cache |
| cvm_inf_quadrimestral_fip | 200 | 5.485.807 | `d39116b8fe33613a14a48a1d5d7b1d08ae20d28aaa281e269a83a99b79ece6d4` | latin1 | 7896 | cache |
| cvm_inf_quadrimestral_fip | 200 | 4.013.090 | `ac91f50c0e14f6a3de7b750cf5e70e19a22ae01a0f3ce9e12b9d3cd3c1c8094a` | latin1 | 5754 | cache |
| bndes_fundos_carteira | 200 | 16.801 | `2941681368b0aede161b5ef1fa2c05a5188b5fe80834701b464de90738be5189` | utf-8 | - | cache |
| bndes_fundos_carteira | 200 | 106.119 | `7695401c793c465a898a7de12ee6820d85fde13cd4e6b400e415cc5986e480d4` | latin1 | 881 | cache |
| finep_fundos | 200 | 213.539 | `cf4fe9fcff613843a92691ab90cd9351822a238df45a2266f04d81093468cd68` | utf-8 | - | cache |
| mcti_dados_abertos | 200 | 197.042 | `1f1eb34497c5fe8519bdc939435bbc2a61b7a86230baed934e5a7cd1ea5286b2` | utf-8 | - | cache |

## 6. Saídas

- `br_funds.jsonl` — **2.147** registros: 1 por veículo FIP ativo
  (envelope `contract/interim_record.schema.json`, `entity_kind='fund'`,
  `source_id='cvm_registro_fundo_classe'`). Enriquecido com PL/capital do informe
  quadrimestral, sinal de subclasse, evidência BNDES e classe legada.
- `br_firms.jsonl` — **418** gestoras (agrupamento por
  `CPF_CNPJ_Gestor` normalizado dos fundos ativos; 3 fundos
  estão sem gestor no registro e não entram no agrupamento).
- `br_bndes_portfolio.jsonl` — **881** linhas da carteira de
  fundos do BNDESPAR, exatamente como na fonte (sem classificação inventada).
- `br_sources.json` — proveniência completa (HTTP, bytes, sha256, encoding, linhas,
  membros do ZIP, competências, cobertura, duplicidades e validação).

| arquivo | linhas | bytes | sha256 |
|---|---:|---:|---|
| `data/interim/br_funds.jsonl` | 2.147 | 3.277.369 | `8c2144f246e00656728936ab3077ef9d777f5e7b5b02ac383a573fd526ce63be` |
| `data/interim/br_firms.jsonl` | 418 | 185.526 | `823fe8be6b305e1716d1908af0cc48b74e405fca8fdd33d4ffb9267fff08cf32` |
| `data/interim/br_bndes_portfolio.jsonl` | 881 | 679.237 | `10b0e019830d15ef5bd747da833e20010e8a0381c298d36353c9c87fddf15656` |
| `data/interim/br_sources.json` | 1.115 | 40.006 | `4685d323c8c056d55b10c177a61b0edf7a0fd1f716b16b4fd864791f6ca1f4b2` |

> `br_sources.json` contém `generated_at_utc`, então o sha256 dele muda a cada execução;
> os três JSONL são determinísticos (mesmo sha256 em execuções repetidas e no modo
> offline, comprovado no relatório do coletor).

## 7. Limitações conhecidas

- O tipo de FIP (Capital Semente / Empresas Emergentes / Multiestratégia) não existe
  como campo estruturado sob RCVM 175; só o sinal textual da denominação social.
- O registro vivo traz linhas ativas duplicadas (2–3 por ID) para 27 fundos, idênticas
  exceto `Gestor`; o coletor entrega 1 registro por CNPJ com regra determinística e
  expõe as alternativas em `br_sources.json` (ver seção 1). Resultado:
  **2.175 linhas ativas → 2.147 veículos únicos após dedupe de gestor (27 casos);
  418 gestoras**.
- `cad_fi.csv` é legado e defasado; serve só de evidência histórica de `CLASSE`.
- O informe quadrimestral agrega por classe e pode ter reapresentações; a regra de
  deduplicação é determinística (última linha do arquivo por classe/competência).
- Fundos com múltiplas classes: a soma por classe é feita apenas com classes que
  reportaram valor naquela competência (pode subestimar fundos com classe omissa).
- `br_bndes_portfolio.jsonl` reflete exatamente as 881 linhas do CSV; não há join com
  a CVM nesse arquivo (o vínculo está em `br_funds.payload.bndes_evidencias`).
- O KPI de dry powder (comprometido − integralizado) deve ser calculado no transform;
  aqui só os dois valores oficiais são publicados.

### Registros legados casados (evidência secundária)

| CNPJ fundo | denominação social | CLASSE legada (cad_fi) |
|---|---|---|
| `08277553000106` | OPPORTUNITY HOLDING FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA - RESP LIMITADA | FIP |
| `10947569000112` | LOFOTEN FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES - MULTIESTRATÉGIA RESPONSABILIDADE LIMITADA | FIC FIP |
| `15035021000110` | INFRA PORTFOLIO I FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FIP |
| `15798220000180` | ORCHID FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FII |
| `16728464000159` | OMEGA ENERGY FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FIP Multi |
| `17213759000155` | FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES FONTAINE VILLE URBANISMO MULTIESTRATÉGIA | FIP Multi |
| `19933445000152` | AVM III FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA RESPONSABILIDADE LIMITADA | FIC FIP |
| `20968803000141` | RDB PARTICIPAÇÕES FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA RESPONSABILIDADE LIMITADA | FIC FIP |
| `21331225000109` | EDUCAÇÃO BR FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES - MULTIESTRATÉGIA | FIP Multi |
| `22219214000196` | SGON FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FIP |
| `22219229000154` | TCG FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES | FIP Multi |
| `22219250000150` | ESPANHOL FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATEGIA | FIP Multi |
| `23700564000132` | LUNA FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA RESPONSABILIDADE LIMITADA | FIC FIP |
| `24571888000180` | ABAPORU FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES | FIC FIP |
| `26452143000128` | NEW META FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FIP Multi |
| `32300050000180` | SVEN FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FIP Multi |
| `41801466000173` | KOMBI AZUL II CONS FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATÉGIA | FIP |
| `43953460000147` | EUV DENALI FUNDO DE INVESTIMENTO EM PARTICIPAÇÕES MULTIESTRATEGIA | FIP Multi |
