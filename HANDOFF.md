# HANDOFF — Fluxo A: Conciliação Semanal (n8n)

> Documento de continuidade. Se você está começando uma sessão nova (outra máquina, outro chat), leia isto primeiro — cobre tudo que já foi feito, o que funciona de verdade, o que está pendente, e os próximos passos técnicos exatos.

## O que é o projeto

Automação em n8n que roda semanalmente e concilia a Base de Pagamentos do Omie contra as notas fiscais da Qive, aplica ~15 regras de validação financeira, e manda um e-mail de resumo (rascunho no Gmail) + salva o detalhe numa planilha Google Sheets + snapshot no Supabase.

Dono do projeto: Kamilla Scheidt (Analista Financeira, Logcomex). Este fluxo também é o projeto de entrega do desafio interno **RRIA/Bluebelt** (ferramentas: N8N + Open Router).

## Acessos e IDs importantes

- **n8n**: workflow `Fluxo A - Conciliação Semanal`, ID `0aLLeM1ZcgF832Aj`, host `ramonlogcomex.app.n8n.cloud`. URL: `https://ramonlogcomex.app.n8n.cloud/workflow/0aLLeM1ZcgF832Aj`
- **Repositório**: `Kamilla-Log/Automa-o-concilia-o`, branch de trabalho `claude/n8n-logcomex-fluxo-a-rjejzw`
- **Supabase**: org `dhkjixuhqvjytfucgpxn`, projeto `jdrfudcfidnkeijtulhn`. Tabela `conciliacao_snapshots`. Acesso via HTTP Request (não credential nativa) com `apikey`/`Authorization: Bearer <service_role JWT>` **hardcoded no node** (pendência de segurança — mover pra credential).
- **Planilha de resultado**: "Fluxo A - Conciliação Semanal 2026" (Google Sheets, 8 abas) — link usado no e-mail: `https://docs.google.com/spreadsheets/d/13xa7-eu1nz-uivzzuXFgByCNsZi2yUPkMICHUpVysAw/edit`
- **Base de Colaboradores (real)**: `https://docs.google.com/spreadsheets/d/1woce2k0X7w7nBsPqDdPQJ3ZMeEO__EcK6-3pP813-L8` — aba "Base loggers" (gid `1197124543`). Cabeçalho real está na **linha 2**, não na 1. Colunas: D=CPF, E=Colaborador, F=Admissão, G=Nome do Gestor, H=Centro de Custo. Range atual no node: `D2:H300`.
- **CNPJ Logcomex**: `13.475.043/0001-75` (dígitos: `13475043000175`)
- **Credenciais Omie/Qive**: via `$vars` no n8n (cofre) — `OMIE_APP_KEY`, `OMIE_APP_SECRET`, `QIVE_API_ID`, `QIVE_API_KEY`. Não precisam ser recriadas.
- **GitHub Actions**: `.github/workflows/fluxo-a-conciliacao-semanal.yml` — dispara o webhook do n8n manualmente (workflow_dispatch). Campos do formulário: `ambiente` (teste/producao), `mes_referencia`, **`periodo_inicio`/`periodo_fim`** (novos, formato DD/MM/AAAA — controlam o período real da conciliação e o título do e-mail), `observacao`.

## O que JÁ FUNCIONA com dado 100% real (testado)

1. **Omie - Base de Pagamentos**: busca real, filtrada pelo `periodo_inicio`/`periodo_fim` vindos do trigger (paginação ainda pendente — só pega a 1ª página/50 registros).
2. **Omie - Saldos das Contas**: saldo real de cadastro (não é saldo bancário em tempo real — ver pendência).
3. **Qive - NF-e e NFS-e**: notas fiscais reais via API Arquivei.
4. **Departamento e Conta Corrente**: corrigidos — API real do Omie não tem esses campos como texto direto; departamento vem de `distribuicao[0].cDesDep`, conta corrente vem de `id_conta_corrente` cruzado com a lista `ListarContasCorrentes`.
5. **Base de Colaboradores real**: 277 registros carregados (não é mais mock).
6. **Fornecedor → CNPJ/Razão Social**: loop `ConsultarCliente` por fornecedor único (ver arquitetura abaixo) — **construído e testado, ainda não ligado ao node Python principal**.
7. **Categoria (código → nome)**: `ListarCategorias` — **em construção, teste ainda em andamento** (ver "Onde parei" abaixo).
8. **Regras implementadas de verdade no node Python real**: R1, R2, R12, N1, N2, Q1, Q2, Q3.
9. **Título do e-mail com período real**, formatação do e-mail (sem duplicação de assinatura, sem seção "Anexo" solta).

## Descoberta crítica (a mais importante deste projeto)

A conciliação Qive × Omie **nunca funcionou de verdade** até agora, mesmo reportando "0 divergências" toda semana. Motivo: a API real do Omie não retorna CNPJ do fornecedor em lugar nenhum na listagem de pagamentos — só um ID interno (`codigo_cliente_fornecedor`). Sem CNPJ, a chave de cruzamento (NF + CNPJ) nunca fecha, então a comparação sempre rodava "no vazio" e reportava falso-positivo de "tudo ok".

**Correção**: um loop que consulta `ConsultarCliente` só para os fornecedores únicos que aparecem nos lançamentos da semana (evita ter que baixar os ~12.684 clientes cadastrados via `ListarClientes`, que é inviável). Essa parte já foi construída e testada com sucesso (ver abaixo) — falta só ligar o resultado no node Python principal.

## Regras que existem só "no papel"

O arquivo `node5-python-enriquecimento.py` na raiz do repo é a **referência canônica completa** das regras (R1–R13, Extra-A, Extra-B, N1/N2, Q1/Q2/Q3), baseada num spec da Kamilla (`REGRAS_CONCILIACAO_LOGCOMEX_COMPLETAS.md v30/08/2026`, esse .md não está no repo, só o .py). Esse arquivo assume nomes de campo "simples" (`categoria`, `departamento`, `conta_corrente`, `nf_cf`, `cnpj_cpf`, `razao_social`, `multa`, `juros`, `projeto`, `observacao`, `tipo_documento`) que **não existem assim na API real do Omie**. O node Python real no n8n (`Python - Validacoes R1 a R15`, atualmente v19) só implementou até agora R1, R2, R12, N1, N2 + a conciliação Qive/Omie (Q1-Q3), com os campos já corrigidos pro formato real.

### Mapeamento de campos real (Omie `ListarContasPagar`) — descoberto testando com dados reais

| Campo que o código antigo espera | Campo real da API Omie | Observação |
|---|---|---|
| `conta_corrente` (texto) | `id_conta_corrente` (ID numérico) | Cruzar com `ListarContasCorrentes` (nCodCC→descricao) — **já feito** |
| `departamento` (texto) | `distribuicao[0].cDesDep` | **já feito** |
| `categoria` (texto) | `categorias[0].codigo_categoria` (código, ex "2.17.99") | Precisa `ListarCategorias` (código→nome) — **em construção** |
| `nf_cf`/`numero_documento` | `numero_documento_fiscal` | **NÃO corrigido ainda no Python** |
| `cnpj_cpf`/`razao_social` | não existe — só `codigo_cliente_fornecedor` (ID) | Precisa `ConsultarCliente` por fornecedor — **loop construído, não ligado ao Python ainda** |
| `multa`/`juros` | `cnab_integracao_bancaria.multa_boleto` / `.juros_boleto` | **NÃO corrigido ainda — R12 pode estar lendo sempre vazio** |
| `projeto` (texto "N/D") | `codigo_projeto` (presença/ausência do ID) | **NÃO corrigido ainda** |
| `tipo_documento` | `codigo_tipo_documento` | **Já vem como texto real** (ex: "NF") — mais fácil que o esperado |
| `numero_parcela` (regex em observação) | `numero_parcela` (ex: "021/025") | Campo dedicado, mais confiável que regex — **NÃO usado ainda** |
| `observacao` | **não existe no payload real** | R5/R6 que dependiam de observação livre não vão funcionar assim |

## Arquitetura do fluxo (nodes principais)

`GitHub Actions Trigger` alimenta em paralelo:
- `Omie BR - Base de Pagamentos` → `Omie BR - Saldos das Contas` → Merge idx 0 (pagamentos) e idx 5 (saldos)
- `Omie BR - Base de Pagamentos` → `R5-Fornecedores - Extrair Unicos` (Python, extrai `codigo_cliente_fornecedor` únicos) → `Loop Fornecedores` (splitInBatches) → `Omie BR - Consultar Cliente` (loop, 1 chamada por fornecedor) → volta pro Loop → saída "done" (**ainda não ligada ao Merge**)
- `Qive - NF-e e NFS-e` → Merge idx 1
- `Qive - NFS-e Servicos` → Merge idx 2
- `Sheets - Base Loggers` (real, 277 colaboradores) → Merge idx 3
- `GitHub Actions Trigger` (raw body, pro período) → Merge idx 4
- `Omie BR - Listar Categorias` → `Categorias - Gerar Paginas Restantes` (Python) → `Loop Categorias` (splitInBatches) → `Omie BR - Categorias Pagina N` (loop) → volta pro Loop → saída "done" (**ainda não ligada ao Merge**)

`Merge - Omie + Qive` (atualmente `numberInputs: 6`, precisa virar 8 quando os dois loops forem ligados) → `Python - Validacoes R1 a R15` (v19) → AI Agent (formata e-mail) → Gmail (rascunho).

**Pegadinha importante do n8n aprendida nesta sessão**: no node `splitInBatches` (Loop Over Items), a saída de índice **0 é "done"** e a saída de índice **1 é "loop"** — o contrário do que a intuição sugere. Isso já causou um bug (loop nunca executava) que foi corrigido nos dois loops construídos.

## Onde eu parei exatamente (para retomar)

Estava testando se `Loop Categorias` (o loop de páginas de categoria, construído do mesmo jeito que o de fornecedores) está funcionando — a execução de teste (`executionId: 16693`) rodou com sucesso, mas eu **não tive tempo de confirmar** se esse segundo loop também tinha o bug da porta invertida (done=0/loop=1) corrigido, porque a Kamilla mudou de prioridade pra fazer a migração de sessão e o formulário do desafio. **Isso é o primeiro passo a verificar** na próxima sessão: puxar a execução 16693 (ou rodar uma nova) e confirmar quantas páginas de categoria vieram (esperado: 20 páginas, ~987 categorias).

## Próximos passos técnicos (em ordem)

1. Confirmar se `Loop Categorias` está trazendo todas as ~20 páginas (987 categorias). Se a porta estiver invertida, aplicar a mesma correção que foi feita em `Loop Fornecedores` (trocar sourceIndex 0↔1 na conexão pro node `Omie BR - Categorias Pagina N`).
2. Ligar `Loop Fornecedores` (saída "done", índice 0) e `Loop Categorias` (saída "done", índice 0) como novos inputs do `Merge - Omie + Qive` (bump `numberInputs` pra 8).
3. Atualizar o node `Python - Validacoes R1 a R15` pra v20:
   - Construir `fornecedor_map` a partir dos itens de `ConsultarCliente` (chave `codigo_cliente_omie` → `{cnpj_cpf, razao_social, nome_fantasia}`).
   - Construir `categoria_map` a partir de todos os itens `categoria_cadastro` (chave `codigo` → `descricao`).
   - Trocar extração de `numero_documento_fiscal` (no lugar de `nf_cf`/`numero_documento`), `cnab_integracao_bancaria.multa_boleto`/`.juros_boleto` (no lugar de `multa`/`juros`), presença de `codigo_projeto` (no lugar de `projeto` texto).
   - Aplicar `fornecedor_map`/`categoria_map` durante o enriquecimento de cada pagamento (preencher `cnpj_cpf`, `razao_social`, `categoria` de verdade).
4. Portar a lógica completa de `node5-python-enriquecimento.py` (R3, R5, R6, R7, R10, R11, R13, Extra-A) pro node real, adaptada aos nomes de campo corretos da tabela acima. **Atenção**: R6 (eventos→depto marketing) e a detecção de reembolso por texto livre em R5 dependiam de um campo `observacao` que **não existe** na API real — precisa decidir com a Kamilla um substituto ou aceitar que essas ficam limitadas.
5. Testar execução completa e confirmar que a conciliação Qive×Omie agora bate de verdade (usar o `fornecedor_map` pra popular `cnpj_cpf` antes de chamar a função `conciliar()`).
6. Mover as credenciais hardcoded do node `Supabase - Snapshot Semanal` pra uma credential de verdade.
7. Perguntar ao Ramon se existe endpoint interno de saldo bancário em tempo real (Kamilla escolheu esse caminho, ainda não perguntado).
8. Aguardar sign-off da Carol antes de ativar o trigger de produção do GitHub Actions.

## Coisas que a Kamilla já decidiu (não perguntar de novo)

- Não rotacionar a chave da API do Omie nem o service_role JWT do Supabase que foram colados em texto puro no chat — ela já disse que não é pra se preocupar com isso.
- Período da conciliação vem de fora (campo do GitHub Actions), não é calculado a partir dos dados.
- Base de Colaboradores: usar sempre a aba real "Base loggers" (gid 1197124543), range D2:H300.
- Regras faltantes (R3/R5/R6/R7/R10/R11/R13): ela já forneceu o spec (o .py de referência é a fonte), não precisa reexplicar — só falta implementar com os nomes de campo certos.

## Desafio RRIA / Bluebelt

A Kamilla também precisa entregar este projeto no desafio interno RRIA (ferramentas: N8N + Open Router, projeto individual). Regras do desafio (FAQ):
- Testar comparando resultado COM e SEM IA (tempo, erros, clareza).
- Projeto "destaque" = real, relevante, aplicação clara de IA (não determinística), com métricas quantitativas e qualitativas, contado como história: problema → solução → resultado.
- Formulário de entrega: link do Google Forms (a sessão anterior não conseguiu acessar por bloqueio de rede/política — pedir pra Kamilla colar as perguntas do formulário, ou tentar de novo se o acesso mudar).

**Ângulo sugerido pra história do desafio** (rascunho, ainda não validado com a Kamilla):
- Problema: conciliação semanal Qive×Omie era manual, sujeita a erro, e a "IA" nem entrava — era planilha e paciência.
- Descoberta real durante o projeto: a comparação automatizada estava rodando "no vazio" (sem CNPJ) e reportando falso-positivo de "tudo OK" — um problema de dados que só apareceu ao testar com API real, não com mock.
- Solução: automação n8n + Python fazendo o trabalho pesado (dados/regras), com um Agente de IA (LLM via Open Router) só na ponta, redigindo o e-mail executivo em linguagem natural a partir do JSON pré-calculado — divisão clara entre "o que é determinístico" (Python) e "o que precisa de linguagem natural" (IA).
- Métricas possíveis: tempo de conciliação manual vs automatizado; nº de regras aplicadas automaticamente (8 implementadas de um total de ~15 mapeadas); descoberta do bug estrutural do CNPJ como resultado direto de testar com dado real em vez de mock.
