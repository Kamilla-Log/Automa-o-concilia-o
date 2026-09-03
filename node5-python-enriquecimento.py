# ############################################################
# ############################################################
# ##                                                        ##
# ##   REGRA CANONICA - READ ONLY                           ##
# ##                                                        ##
# ############################################################
# ############################################################
#
# Este node NAO PODE, EM NENHUMA HIPOTESE:
#   - Escrever/alterar/excluir dados no Omie (BR ou Internacional)
#   - Enviar e-mails (Gmail sempre em RASCUNHO, aprovacao manual da Kamilla)
#   - Modificar dados em sistemas de terceiros (Qive, Google Drive files, etc.)
#   - Executar qualquer acao destrutiva ou irreversivel
#   - Chamar endpoints com metodos HTTP: POST (de criacao), PUT, PATCH, DELETE
#     em sistemas onde a Logcomex nao e "dona" dos dados
#
# Este node SO PODE:
#   - LER dados de qualquer fonte
#   - Transformar dados em memoria (calculos, classificacoes, formatacao)
#   - Emitir apontamentos, alertas, logs
#   - Escrever no Supabase (banco proprio - historico do fluxo)
#   - Escrever no Google Sheets de saida (planilha propria do fluxo)
#   - Criar RASCUNHO no Gmail (nunca enviar)
#
# Se algum dia essa regra precisar ser quebrada, PARE, abra discussao
# com a Kamilla, documente o motivo, e so entao proceda.
#
# ############################################################
#
# Fluxo A - Node 5 (Python) - Enriquecimento + Conciliacao + Validacoes
# ============================================================
# Recebe do Omie a base de pagamentos, do Qive as notas fiscais,
# do Supabase o historico da semana anterior, e:
#   1. Adiciona 3 colunas calculadas (Tipo, Anexo, Responsavel)
#      + DETECTA NOVIDADES (categorias novas, contas correntes novas)
#      -> apontamentos "N1", "N2"
#   2. Cruza Qive x Omie (skill conciliacao-qive-omie)
#      -> apontamentos "Q1" (NF pendente), "Q2" (divergencia), "Q3" (cancelada)
#      -> auto-ignora Devolucao/Remessa/Comodato via CFOP e Finalidade
#      -> marca Repetido? com contagem de semanas (do Supabase)
#   3. Aplica as regras de validacao da base de pagamentos:
#      LOTE 1 (implementado): R1, R2, R3 (v30/08), R6, R7, R10, R11,
#                             R12, R13, Extra A, Extra B
#      LOTE 2 PARCIAL:        R5 (Base Loggers) - implementado
#      LOTE 2 TODO:           R9 (14 CNPJs sempre_tax + dinamico)
#      LOTE 3 TODO:           R4, R8 (historico Supabase),
#                             Base Fornecedores (Vercel)
#   4. Retorna:
#      - pagamentos enriquecidos (33 colunas: 30 Omie + 3 calc)
#      - apontamentos (R + N + Q + Extra)
#      - conciliacao_qive completo (pra alimentar aba Conciliacao Qive)
#      - so_qive_nfs (pro Supabase salvar snapshot desta semana)
#      - resumo consolidado (pra AI Agent redigir e-mail)
# ============================================================

# --------------------------------------------------------------
# CONFIGURACOES - editar aqui se algo mudar
# --------------------------------------------------------------

# Categorias que NAO exigem documento fiscal.
# Anexo vira "Nao gera doc fiscal" (em vez de "Sem Anexo") quando NF/CF vazio.
CATEGORIAS_ISENTAS = [
    "Indenizacoes",
    "Comissoes",
    "Despesas Bancarias",
    "Despesas Bancarias - Cartao de credito",
    "Devolucao para clientes (Recebimento a maior)",
    "Ferias",
    "IOF cartao de credito",
    "OBRIGACOES POR AQUISICAO DE INVESTIMENTOS CP",
    "Salarios",
    "Salarios PJ",
    "Taxas diversas - Cartao de credito",
    "IOF cambio",
    "IRRF - Aplicacao",
    "IRRF cambio",
    "Devolucao de fornecedores - cartao de credito",
]

# Substrings que identificam contas correntes da Diretoria.
CARTOES_DIRETORIA = [
    "5901",
    "5903",
    "Helmuth 0903",
    "6128",
    "6956",
]

# --------------------------------------------------------------
# CONTAS A IGNORAR NA CONCILIACAO
# Lançamentos/saldos dessas contas sao removidos ANTES de qualquer
# processamento. Match case-insensitive por substring no nome da conta.
# Motivos:
#   - "PREVISAO"/"PREVISÃO": conta virtual de projecao, nao movimenta dinheiro real
#   - "caixinha": conta sem extrato bancario, controle interno
# --------------------------------------------------------------
CONTAS_IGNORAR = [
    "previsao",
    "previsão",
    "caixinha",
]


def deve_ignorar_conta(conta_corrente):
    """True se a conta esta na lista CONTAS_IGNORAR."""
    conta_norm = str(conta_corrente or "").strip().lower()
    if not conta_norm:
        return False
    for termo in CONTAS_IGNORAR:
        if termo in conta_norm:
            return True
    return False

# --------------------------------------------------------------
# CATALOGO DE CONTAS CORRENTES CONHECIDAS
# Se aparecer uma conta corrente cuja substring nao bate com NENHUMA
# destas, o codigo emite apontamento N1 "Conta corrente nova".
# Formato: (substring_match, nome_amigavel)
# Case-insensitive; use substring unica que distinga cada conta.
# --------------------------------------------------------------
CONTAS_CORRENTES_CATALOGADAS = [
    # ================================================================
    # CONTAS CORRENTES BR (1.1.1.02)
    # Baseado no arquivo "Relacao de contas bancarias.xlsx" do Omie.
    # Substrings unicas (numero de AG+CC) para match confiavel.
    # ================================================================
    ("AG: 3114 C/C: 130029855", "Santander CC 3114"),
    ("AG: 3833 C/C: 75418-1", "Itau CC 3833"),
    ("XP INVESTIMENTO", "XP CC"),           # sem AG/CC no cadastro
    ("CITIBANK S.A.", "Citibank BR"),
    ("AG: 5755 C/C: 0396168-0", "Bradesco CC 5755"),
    ("AG: 28800 C/C: 104830", "Safra CC 28800"),

    # ================================================================
    # APLICACOES FINANCEIRAS (1.1.1.03)
    # ================================================================
    ("APLICACAO CDB BRADESCO - AG: 00585", "CDB Bradesco 00585"),
    ("XP INVESTIMENTOS - CONTA: 3196982", "XP Aplicacao 3196982"),
    ("APLICACAO CDB SANTANDER - AG: 3114 C/C: 130029855", "CDB Santander 3114"),
    ("APLICACAO CDB SAFRA", "CDB Safra"),

    # ================================================================
    # RENDA VARIAVEL (1.1.1.04)
    # ================================================================
    ("XP INVESTIMENTOS - ACOES", "XP Acoes"),

    # ================================================================
    # CARTOES DE CREDITO BR - DIRETORIA
    # (Match por final do numero do cartao.)
    #
    # OBS: no filename do Drive esses cartoes aparecem como
    # "Fatura cartao de credito Bradesco XXXX" ou "Itau XXXX"
    # porque o Bradesco/Itau eh o banco emissor. Nao confundir com
    # a bandeira do cartao (AMEX, MasterCard, Visa).
    # ================================================================
    ("5901", "AMEX 5901 (Diretoria - emitido Bradesco)"),
    ("5903", "AMEX 5903 (Diretoria - emitido Bradesco)"),
    ("Helmuth 0903", "AMEX Helmuth 0903 (Diretoria - emitido Bradesco)"),
    ("6128", "MasterCard Itau 6128 (Diretoria)"),
    ("6956", "MasterCard Itau 6956 (Diretoria)"),

    # ================================================================
    # CARTOES DE CREDITO BR - CONTAS A PAGAR
    # ================================================================
    ("Visa Cartao", "Visa CP"),
    ("5907", "AMEX 5907 (CP - emitido Bradesco)"),
    ("5938", "MasterCard 5938 (CP - emitido Bradesco)"),
    ("3915", "Cartao 3915 (CP - emitido Bradesco - bandeira nao mapeada)"),
    ("Conta Simples Cartao", "Conta Simples Cartao CP"),
    ("0650", "MasterCard Santander 0650 (CP)"),
    ("2809", "MasterCard Santander 2809 (CP)"),
    ("3578", "MasterCard Santander 3578 (CP)"),

    # ================================================================
    # SEM EXTRATO OU ESPECIAIS
    # ================================================================
    ("Dock", "Dock (ContaSimples) - sempre R$0"),
    ("Onfly", "Onfly"),
    ("Caixinha", "Caixinha (sem extrato)"),
    ("SWILE", "SWILE (cruza com DP)"),
]

# ================================================================
# PADRAO DE ARQUIVOS NO DRIVE (Node 3a)
# ================================================================
# Como identificar cada tipo de arquivo pelo nome, e a qual conta
# do Omie ele pertence. Baseado no padrao real usado hoje.
#
# Padroes principais:
#   - Contas correntes BR:    "1.1.1.02.NNNN - BANCO NOME - AG XXXX CC YYYY"
#   - Aplicacoes financeiras: "1.1.1.03.NNNN - APLICACAO CDB BANCO - ..."
#   - Renda variavel:         "1.1.1.04.NNNN - XP INVESTIMENTOS - ACOES"
#   - Cartoes de credito:     "Fatura cartao de credito Banco XXXX"
#                             (Banco = emissor, nao a bandeira!)
#   - Conta Simples:          "Fatura conta simples fechada/em aberto"
#   - Offshore (Fluxo D3):    "Extrato ... International" (ignorar aqui)
# ================================================================

# Prefixos que identificam TIPO de arquivo do Drive.
DRIVE_FILE_TIPO = {
    "1.1.1.02.": "conta_corrente_br",
    "1.1.1.03.": "aplicacao_cdb",
    "1.1.1.04.": "renda_variavel",
    "Fatura cartao de credito": "cartao_credito",
    "Fatura cartão de crédito": "cartao_credito",
    "Fatura conta simples": "conta_simples",
    "International": "offshore_ignorar",
    "internacional": "offshore_ignorar",
}

# Mapa arquivo -> conta correspondente no Omie.
# Casa pelo codigo contabil ou nome do cartao no arquivo.
#
# NOTA: usa SUBSTRING match. Isso torna o codigo robusto contra
# sufixos "(1)", "(2)", " (copia)", numeros que vem do navegador
# quando o mesmo arquivo eh baixado varias vezes. Exemplo:
#   Arquivo: "Fatura cartao de credito Bradesco 0903 (1).PDF"
#   Padrao:  "Bradesco 0903"  <- casa mesmo com o "(1)" no final
#
DRIVE_ARQUIVO_PARA_OMIE = [
    # ---- Contas correntes e aplicacoes (por codigo contabil) ----
    ("1.1.1.02.0006", "AG: 3114 C/C: 130029855"),      # Santander CC
    ("1.1.1.02.0009", "AG: 3833 C/C: 75418-1"),        # Itau CC
    ("1.1.1.02.0010", "XP INVESTIMENTO"),               # XP CC
    ("1.1.1.02.0011", "CITIBANK S.A."),                 # Citibank BR
    ("1.1.1.02.0012", "AG: 5755 C/C: 0396168-0"),      # Bradesco CC
    ("1.1.1.02.0014", "AG: 28800 C/C: 104830"),        # Safra CC
    ("1.1.1.03.0005", "APLICACAO CDB BRADESCO"),
    ("1.1.1.03.0006", "XP INVESTIMENTOS - CONTA: 3196982"),
    ("1.1.1.03.0008", "APLICACAO CDB SANTANDER"),
    ("1.1.1.03.0029", "APLICACAO CDB SAFRA"),
    ("1.1.1.04.0001", "XP INVESTIMENTOS - ACOES"),
    # ---- Cartoes (por ultimos 4 digitos) ----
    ("Bradesco 5901", "5901"),   # AMEX (Diretoria)
    ("Bradesco 5903", "5903"),   # AMEX (Diretoria)
    ("Bradesco 0903", "0903"),   # AMEX Helmuth (Diretoria)
    ("Bradesco 5907", "5907"),   # AMEX (CP)
    ("Bradesco 5938", "5938"),   # MasterCard (CP)
    ("Bradesco 3915", "3915"),   # confirmar
    ("Itau 6128", "6128"),        # MasterCard Itau (Diretoria)
    ("Itau 6956", "6956"),        # MasterCard Itau (Diretoria)
    ("Santander 0650", "0650"),   # MasterCard Santander (CP)
    ("Santander 2809", "2809"),   # MasterCard Santander (CP)
    ("Santander 3578", "3578"),   # MasterCard Santander (CP)
    ("Fatura conta simples", "Conta Simples"),
]

# --------------------------------------------------------------
# CATALOGO DE CATEGORIAS CONHECIDAS
# Comeca com as isentas + algumas categorias comuns nao isentas
# (para que categorias legitimas nao sejam sinalizadas como novas).
# Sinalizamos N2 quando uma categoria nao esta em ISENTAS nem aqui.
# Voce pode ir crescendo essa lista com o tempo.
# --------------------------------------------------------------
CATEGORIAS_NAO_ISENTAS_CONHECIDAS = [
    # Adicione aqui categorias comuns que a Logcomex ja usa
    # e que TEM doc fiscal. Comeca vazia — vamos crescendo com o uso.
]

# ================================================================
# CONCILIACAO QIVE x OMIE - Configuracoes
# ================================================================
# Regras herdadas da skill "conciliacao-qive-omie":
#   - Chave de cruzamento = Numero NF + CNPJ (so digitos)
#   - Comparar valor bruto e liquido separadamente
#   - Merge sem pre-filtrar por Tipo de Documento nem data
#   - Notas informativas (devolucao/remessa/comodato) sao DESCONSIDERADAS
#     automaticamente via CFOP e Finalidade da NF-e
#   - NFS-e nao tem CFOP nacional: por enquanto sinaliza pra revisar
# ================================================================

# CFOPs de operacoes SEM impacto financeiro (nao viram pagamento).
# Se a nota tem CFOP nessa lista, o bot IGNORA e nao trata como pendencia.
CFOP_DEVOLUCAO = {
    "1201", "1202", "1411", "1503", "1553", "1660", "1661", "1662",
    "2201", "2202", "2411", "2503", "2553", "2660", "2661", "2662",
    "5201", "5202", "5410", "5411", "5412", "5413", "5503",
    "6201", "6202", "6410", "6411", "6412", "6413", "6503",
}

# Remessa: faixas 1901-1949, 2901-2949, 5901-5949, 6901-6949
CFOP_REMESSA = set()
for _prefix in ("19", "29", "59", "69"):
    for _suffix in range(1, 50):
        CFOP_REMESSA.add(_prefix + str(_suffix).zfill(2))

CFOP_COMODATO = {"5908", "5909", "6908", "6909"}

CFOP_IGNORAR = CFOP_DEVOLUCAO | CFOP_REMESSA | CFOP_COMODATO

# Finalidade da NF-e (campo padrao da SEFAZ):
# 1 = Normal, 2 = Complementar, 3 = Ajuste, 4 = Devolucao
FINALIDADES_IGNORAR = {"4", "devolucao", "devolução"}

# Situacoes que indicam nota cancelada
SITUACOES_CANCELADA = {"cancelada", "cancelled", "6", "101", "cancelamento"}

# Threshold para considerar valores "iguais" (evita erro de arredondamento)
TOLERANCIA_VALOR = 0.02  # R$ 0,02

# Limite pra sinalizar Repetido como CRITICO
REPETIDO_CRITICO_SEMANAS = 3

# ================================================================
# REGRAS R1-R13 + EXTRA A/B - Configuracoes
# ================================================================
# Baseado em REGRAS_CONCILIACAO_LOGCOMEX_COMPLETAS.md v30/08/2026
# ================================================================

# R2 - Excecoes de Departamento N/D
DEPTO_ND_EXCECOES = {"stripe"}  # razao social/fantasia normalizada

# R3 - Categorias "isentas" pra Pipefy + Outros (nao precisa NF)
R3_CATEGORIAS_ISENTAS_PIPEFY_OUTROS = {
    "tarifas", "iof", "despesas bancarias", "despesas bancárias",
    "ferias", "férias", "rescisao", "rescisão", "comissao", "comissões", "comissoes",
    "salarios", "salários",
}

# R3 - Categorias PROIBIDAS pra Pipefy + Outros (sempre erro)
R3_CATEGORIAS_PROIBIDAS_PIPEFY_OUTROS = {
    "fgts", "inss", "irrf", "impostos", "tributos",
}

# R6 - Eventos 2026 -> devem estar em Depto 16 (Marketing Institucional)
EVENTOS_2026 = [
    "Comex Hoje", "TLW Mexico", "TLW México", "Workshop Tendencias",
    "Workshop Tendências", "Compliance day", "Intermodal",
    "Maratona de Supply", "Congresso Aduaneiro", "Logistec Show",
    "Global Trade Summit", "Logtech day", "Multimodal NE",
    "Logistique", "Forum ILOS", "Fórum ILOS", "CTF26",
    "Logistica do Futuro", "Logística do Futuro",
    "Supply Chain Executive", "Forum de compras", "Fórum de compras",
    "Port Performance", "FNDA ADAB", "Seminario FIEP", "Seminário FIEP",
    "Porto Hack", "Exlog Bogota", "Exlog Bogotá", "AI Festival",
]
DEPTO_MARKETING_INSTITUCIONAL = "16"

# R7 - Admin Parana (nome exato no Omie, todo mes igual)
ADMIN_PARANA_NOME = "ADMINISTRADORA PARANA"
ADMIN_PARANA_QTD_ESPERADA = 6  # cada categoria (estacionamento + condominio)

# R10 - Categorias isentas de Projeto (nao precisam preenchido)
R10_CATEGORIAS_ISENTAS = {
    "despesas bancarias", "despesas bancárias",
    "folha de pagamento", "folha",
    "devolucao", "devolução",
    "inss", "fgts", "irrf", "irpj", "csll", "cofins",
}

# --------------------------------------------------------------
# IMPORTS - todos no topo pra evitar surpresa em runtime
# --------------------------------------------------------------
import re as _re
import datetime as _datetime
from collections import Counter as _Counter

# R11 - Regex pra detectar parcelamento na Observacao
# Aceita: "parcela X/Y", "parc X/Y", "X/Y" (isolado ou com espaco)
R11_REGEX_PARCELA = _re.compile(
    r"(?:parcela|parc\.?)?\s*\d+\s*/\s*\d+", _re.IGNORECASE
)

# R11 - Fornecedores com parcelamento historico conhecido (nao precisa regex)
R11_FORNECEDORES_PARCELAMENTO = {"dell", "pars"}

# R13 - Categorias Facilities -> devem estar em Depto 62
R13_CATEGORIAS_FACILITIES = {
    "manutencao de escritorio", "manutenção de escritório",
    "manutencao de escritorio - cartao de credito",
    "manutenção de escritório - cartão de crédito",
    "copa e cozinha",
    "copa e cozinha - cartao de credito",
    "copa e cozinha - cartão de crédito",
    "material de escritorio", "material de escritório",
    "material de escritorio - cartao de credito",
    "material de escritório - cartão de crédito",
}
DEPTO_FACILITIES = "62"

# R11 - Tolerancia pra "mes anterior" -> ultimo dia do mes anterior
# calculada dinamicamente com base em $now


# --------------------------------------------------------------
# FUNCOES DE CALCULO DAS 3 COLUNAS DERIVADAS
# --------------------------------------------------------------

def calcular_tipo(conta_corrente):
    """
    Tipo:
      - "Cartao de Credito" se o nome contem "Cartao"
      - "Pipefy" caso contrario
    """
    conta = str(conta_corrente or "").lower()
    if "cartao" in conta or "cartão" in conta:
        return "Cartao de Credito"
    return "Pipefy"


def calcular_anexo(nf_cf, categoria):
    """
    Anexo (status do documento fiscal):
      - "OK"                  se NF/CF preenchido
      - "Nao gera doc fiscal" se NF/CF vazio E categoria em CATEGORIAS_ISENTAS
      - "Sem Anexo"           se NF/CF vazio E categoria fora da lista
      - "Solicitado"          preenchido MANUALMENTE pela Thaiane
                              (nunca calculado aqui)
    """
    if nf_cf and str(nf_cf).strip():
        return "OK"

    categoria_norm = str(categoria or "").strip().lower()
    for isenta in CATEGORIAS_ISENTAS:
        if categoria_norm == isenta.lower():
            return "Nao gera doc fiscal"

    return "Sem Anexo"


def calcular_responsavel(conta_corrente):
    """
    Responsavel:
      - "Diretoria"      se conta contem substring da CARTOES_DIRETORIA
      - "Contas a Pagar" caso contrario
    """
    conta = str(conta_corrente or "")
    for marcador in CARTOES_DIRETORIA:
        if marcador in conta:
            return "Diretoria"
    return "Contas a Pagar"


# --------------------------------------------------------------
# DETECCAO DE NOVIDADES
# --------------------------------------------------------------

def eh_conta_catalogada(conta_corrente):
    """True se a conta corrente bate com alguma substring conhecida."""
    conta = str(conta_corrente or "")
    if not conta.strip():
        return True  # conta vazia nao e "novidade", e outro problema
    for marcador, _nome in CONTAS_CORRENTES_CATALOGADAS:
        if marcador.lower() in conta.lower():
            return True
    return False


def eh_categoria_catalogada(categoria):
    """
    True se a categoria esta em qualquer uma das listas conhecidas.
    Categoria vazia nao e sinalizada como novidade (e outro problema).
    """
    categoria_norm = str(categoria or "").strip().lower()
    if not categoria_norm:
        return True
    todas_conhecidas = CATEGORIAS_ISENTAS + CATEGORIAS_NAO_ISENTAS_CONHECIDAS
    for conhecida in todas_conhecidas:
        if categoria_norm == conhecida.lower():
            return True
    return False


# --------------------------------------------------------------
# FUNCOES DE CONCILIACAO QIVE x OMIE
# --------------------------------------------------------------

def _so_digitos(valor):
    """Extrai so os digitos de uma string. Uso: normalizar CNPJ e NF."""
    return "".join(c for c in str(valor or "") if c.isdigit())


def _valor_para_float(valor):
    """
    Converte string 'R$ 1.500,00', '1.500', '1500.00', '1500,00' em float.
    Retorna 0.0 se falhar.
    Heuristica BR: se so tem '.' e a parte apos tem 3 digitos -> milhar (1.500 = 1500).
    """
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    texto = texto.replace("R$", "").replace(" ", "").replace(" ", "")
    if not texto:
        return 0.0

    tem_ponto = "." in texto
    tem_virgula = "," in texto

    if tem_virgula and tem_ponto:
        # Formato BR classico: 1.500,00 -> 1500.00
        texto = texto.replace(".", "").replace(",", ".")
    elif tem_virgula:
        # 1500,00 -> 1500.00
        texto = texto.replace(",", ".")
    elif tem_ponto:
        # Ambiguo: '1.500' (BR milhar) ou '1500.5' (US decimal)?
        # Heuristica: se a parte APOS o ultimo ponto tem 3 digitos exatos
        # e nao ha outros pontos com menos de 3 digitos, e milhar BR.
        partes = texto.split(".")
        if len(partes) >= 2 and all(len(p) == 3 for p in partes[1:]):
            # '1.500' ou '1.500.000' -> tira os pontos
            texto = texto.replace(".", "")
        # Senao: mantem como US decimal (1500.5 fica 1500.5)

    try:
        return float(texto)
    except (ValueError, TypeError):
        return 0.0


def _motivo_informativa(nota):
    """Retorna motivo pelo qual a nota eh informativa (pra display)."""
    cfop = str(nota.get("cfop", "")).strip()
    finalidade = str(nota.get("finalidade", "")).strip().lower()
    if finalidade in FINALIDADES_IGNORAR:
        return "Devolucao (finalidade NF-e)"
    if cfop in CFOP_DEVOLUCAO:
        return "Devolucao (CFOP " + cfop + ")"
    if cfop in CFOP_COMODATO:
        return "Comodato (CFOP " + cfop + ")"
    if cfop in CFOP_REMESSA:
        return "Remessa (CFOP " + cfop + ")"
    return "Operacao sem impacto financeiro"


def eh_nota_informativa(nota):
    """
    True se a nota eh devolucao/remessa/comodato -- ignorar no cruzamento.
    Detecta via CFOP e Finalidade.
    """
    cfop = str(nota.get("cfop", "")).strip()
    finalidade = str(nota.get("finalidade", "")).strip().lower()
    if cfop and cfop in CFOP_IGNORAR:
        return True
    if finalidade in FINALIDADES_IGNORAR:
        return True
    return False


def eh_nota_cancelada(nota):
    """True se a nota esta cancelada."""
    situacao = str(nota.get("situacao", "")).strip().lower()
    cstat = str(nota.get("cstat", "")).strip()
    return situacao in SITUACOES_CANCELADA or cstat == "101"


def eh_nfse_sem_cfop(nota):
    """NFS-e nao tem CFOP nacional. Sinalizar pra revisao manual."""
    tipo = str(nota.get("tipo", "")).strip().lower()
    return tipo in ("nfs-e", "nfse", "servico") and not nota.get("cfop")


def _extrair_pagamentos_omie_do_input():
    """
    Tenta encontrar a base de pagamentos do Omie no input do node 5.
    O input pode vir de multiplos nodes (Omie Base, Omie Saldos, Qive).
    """
    pagamentos = []
    for item in _items:
        dados = item.json if hasattr(item, "json") else item.get("json", {})
        contas_pagar = dados.get("contas_pagar")
        if contas_pagar:
            if isinstance(contas_pagar, list):
                pagamentos.extend(contas_pagar)
    return pagamentos


def _extrair_notas_qive_do_input():
    """Tenta encontrar as notas fiscais da Qive no input."""
    notas = []
    for item in _items:
        dados = item.json if hasattr(item, "json") else item.get("json", {})
        for chave in ("notas", "notas_fiscais", "nfe", "nfse"):
            valor = dados.get(chave)
            if isinstance(valor, list):
                notas.extend(valor)
    return notas


def _extrair_historico_so_qive_do_supabase(historico_supabase):
    """
    Recebe o historico do Supabase (lista de snapshots anteriores)
    e retorna dict {nf_normalizada: [semanas em que apareceu]}.
    """
    historico = {}
    for snapshot in historico_supabase or []:
        snapshot_json = snapshot if isinstance(snapshot, dict) else {}
        semana = snapshot_json.get("semana") or snapshot_json.get("data")
        so_qive_lista = snapshot_json.get("so_qive_nfs", [])
        for nf in so_qive_lista:
            nf_key = _so_digitos(nf)
            if nf_key:
                historico.setdefault(nf_key, []).append(semana)
    return historico


def conciliar_qive_omie(notas_qive, pagamentos_omie, historico_repetidos):
    """
    Executa o cruzamento Qive x Omie e retorna um dict com todas as
    categorias (baseado na skill conciliacao-qive-omie).

    historico_repetidos: dict {nf: [semanas anteriores]} do Supabase
    """
    # Indexar pagamentos por (NF, CNPJ) - somando linhas repetidas
    pagamentos_por_chave = {}
    fornecedores_por_cnpj = {}
    for pag in pagamentos_omie:
        nf = _so_digitos(pag.get("nf_cf") or pag.get("numero_documento") or pag.get("numero_nf"))
        cnpj = _so_digitos(pag.get("cnpj_cpf") or pag.get("cnpj"))
        if nf and cnpj:
            chave = (nf, cnpj)
            pagamentos_por_chave.setdefault(chave, []).append(pag)
        if cnpj:
            razao = str(pag.get("razao_social") or pag.get("nome_fantasia") or "").strip().lower()
            if razao:
                fornecedores_por_cnpj.setdefault(cnpj, set()).add(razao)

    # Chaves da Qive pra descobrir "so no Omie" depois
    chaves_qive = set()
    for nota in notas_qive:
        nf = _so_digitos(nota.get("numero_nf") or nota.get("numero"))
        cnpj = _so_digitos(nota.get("cnpj_emitente") or nota.get("cnpj_prestador"))
        if nf and cnpj:
            chaves_qive.add((nf, cnpj))

    # Categorias de saida
    batem_ok = []
    divergencias = []
    mesma_nf_cnpj_dif = []
    so_qive = []
    informativas = []
    canceladas = []
    nfse_revisar = []

    for nota in notas_qive:
        # Skip informativas (auto-detecta por CFOP/finalidade)
        if eh_nota_informativa(nota):
            informativas.append({
                "numero_nf": nota.get("numero_nf"),
                "cnpj_emitente": nota.get("cnpj_emitente"),
                "razao_social": nota.get("razao_social"),
                "valor": nota.get("valor"),
                "motivo_informativa": _motivo_informativa(nota),
            })
            continue

        # Skip canceladas (aponta como alerta separado)
        if eh_nota_cancelada(nota):
            canceladas.append(nota)
            continue

        # NFS-e sem CFOP: sinaliza revisao manual
        if eh_nfse_sem_cfop(nota):
            nfse_revisar.append({
                **nota,
                "motivo_revisao": "NFS-e sem CFOP nacional - conferir manualmente se e informativa",
            })
            # NAO 'continue' - segue tentando cruzar

        nf = _so_digitos(nota.get("numero_nf") or nota.get("numero"))
        cnpj = _so_digitos(nota.get("cnpj_emitente") or nota.get("cnpj_prestador"))
        razao_qive = str(nota.get("razao_social") or nota.get("nome_emitente") or "").strip().lower()

        if not nf or not cnpj:
            continue

        chave = (nf, cnpj)
        pagamentos_matched = pagamentos_por_chave.get(chave, [])

        if pagamentos_matched:
            # Bateu por NF+CNPJ. Comparar valores.
            valor_omie_bruto = sum(_valor_para_float(p.get("valor_documento")) for p in pagamentos_matched)
            valor_omie_liquido = sum(_valor_para_float(p.get("valor_liquido")) for p in pagamentos_matched)
            valor_qive_bruto = _valor_para_float(nota.get("valor") or nota.get("valor_bruto"))
            valor_qive_liquido = _valor_para_float(nota.get("valor_liquido"))

            divergencia_msgs = []
            if abs(valor_omie_bruto - valor_qive_bruto) > TOLERANCIA_VALOR:
                divergencia_msgs.append(
                    "VALOR DIVERGENTE (Omie R$ " + str(round(valor_omie_bruto, 2)) +
                    " vs Qive R$ " + str(round(valor_qive_bruto, 2)) + ")"
                )
            if valor_qive_liquido > 0 and abs(valor_omie_liquido - valor_qive_liquido) > TOLERANCIA_VALOR:
                divergencia_msgs.append(
                    "VALOR LIQUIDO DIVERGENTE (Omie R$ " + str(round(valor_omie_liquido, 2)) +
                    " vs Qive R$ " + str(round(valor_qive_liquido, 2)) + ")"
                )

            registro = {
                "numero_nf": nf,
                "cnpj": cnpj,
                "razao_social": nota.get("razao_social"),
                "valor_qive": valor_qive_bruto,
                "valor_omie": valor_omie_bruto,
                "divergencias": divergencia_msgs,
            }
            if divergencia_msgs:
                divergencias.append(registro)
            else:
                batem_ok.append(registro)
        else:
            # Nao bateu por NF+CNPJ. Ver se e "mesma NF, CNPJ diferente"
            # (matriz vs filial). So aceita se razao social bater.
            candidatos_mesma_nf = [
                (chv, pags) for chv, pags in pagamentos_por_chave.items()
                if chv[0] == nf and chv[1] != cnpj
            ]
            fez_match_cnpj_dif = False
            for chv_cand, pags_cand in candidatos_mesma_nf:
                razoes_omie = fornecedores_por_cnpj.get(chv_cand[1], set())
                if razao_qive and any(razao_qive in r or r in razao_qive for r in razoes_omie):
                    mesma_nf_cnpj_dif.append({
                        "numero_nf": nf,
                        "cnpj_qive": cnpj,
                        "cnpj_omie": chv_cand[1],
                        "razao_social": nota.get("razao_social"),
                        "motivo": "Provavel matriz/filial - conferir CNPJ",
                    })
                    fez_match_cnpj_dif = True
                    break

            if not fez_match_cnpj_dif:
                # E "so na Qive" mesmo. Cruzar com historico do Supabase
                # pra marcar Repetido?
                semanas_anteriores = historico_repetidos.get(nf, [])
                qtd_semanas = len(semanas_anteriores) + 1  # inclui a atual
                repetido_msg = "Nao (1a vez)"
                gravidade = "medio"
                if qtd_semanas == 2:
                    repetido_msg = "Sim - 2a semana"
                    gravidade = "alto"
                elif qtd_semanas >= REPETIDO_CRITICO_SEMANAS:
                    repetido_msg = "CRITICO - " + str(qtd_semanas) + "a semana"
                    gravidade = "critico"

                so_qive.append({
                    "numero_nf": nf,
                    "cnpj_emitente": cnpj,
                    "razao_social": nota.get("razao_social"),
                    "valor": _valor_para_float(nota.get("valor")),
                    "data_emissao": nota.get("data_emissao"),
                    "repetido": repetido_msg,
                    "gravidade": gravidade,
                    "semanas_pendente": qtd_semanas,
                })

    # "So no Omie" - pagamentos com Tipo=Nota Fiscal sem NF correspondente na Qive
    so_omie = []
    for pag in pagamentos_omie:
        tipo_doc = str(pag.get("tipo_documento", "")).lower()
        if "nota fiscal" in tipo_doc or tipo_doc in ("nf-e", "nfe", "nfs-e", "nfse"):
            nf = _so_digitos(pag.get("nf_cf") or pag.get("numero_documento"))
            cnpj = _so_digitos(pag.get("cnpj_cpf") or pag.get("cnpj"))
            if nf and cnpj and (nf, cnpj) not in chaves_qive:
                so_omie.append({
                    "codigo_lancamento": pag.get("codigo_lancamento"),
                    "numero_nf": nf,
                    "cnpj": cnpj,
                    "razao_social": pag.get("razao_social") or pag.get("nome_fantasia"),
                    "valor": _valor_para_float(pag.get("valor_documento")),
                    "motivo": "Lancado como Nota Fiscal no Omie mas NAO existe na Qive",
                })

    return {
        "batem_ok": batem_ok,
        "divergencias": divergencias,
        "mesma_nf_cnpj_dif": mesma_nf_cnpj_dif,
        "so_qive": so_qive,
        "so_omie": so_omie,
        "informativas": informativas,
        "canceladas": canceladas,
        "nfse_revisar": nfse_revisar,
    }


# --------------------------------------------------------------
# 1. ENRIQUECIMENTO + DETECCAO
# --------------------------------------------------------------

# Extrai pagamentos: cada item de _items pode ter "contas_pagar": [...]
# (resposta Omie) OU ser um pagamento solto no top-level (mock flat).
# Aceita os dois formatos.
pagamentos_brutos = []
for _item in _items:
    _dados = _item.json if hasattr(_item, "json") else _item.get("json", {})
    if isinstance(_dados.get("contas_pagar"), list):
        # Formato Omie: {"contas_pagar": [pag1, pag2, ...]}
        pagamentos_brutos.extend(_dados["contas_pagar"])
    elif _dados.get("codigo_lancamento") or _dados.get("nCodTitulo") or _dados.get("cnpj_cpf"):
        # Formato flat: cada item ja e um pagamento
        pagamentos_brutos.append(_dados)

pagamentos_enriquecidos = []
pagamentos_ignorados = []  # log de auditoria - contas PREVISAO/caixinha filtradas
apontamentos = []

# Sets para deduplicar novidades (nao criar 20 apontamentos pra mesma
# categoria nova que apareceu em 20 lancamentos diferentes).
contas_novas_ja_sinalizadas = set()
categorias_novas_ja_sinalizadas = set()

for dados in pagamentos_brutos:
    # Le os campos do Omie que a gente usa nas regras.
    conta_corrente = dados.get("conta_corrente") or dados.get("nome_conta_corrente") or ""

    # FILTRO: ignora lancamentos de contas PREVISAO/caixinha (controle interno,
    # nao movimenta dinheiro real e nao entra na conciliacao bancaria).
    if deve_ignorar_conta(conta_corrente):
        pagamentos_ignorados.append({
            "codigo_lancamento": dados.get("codigo_lancamento") or dados.get("nCodTitulo"),
            "conta_corrente": conta_corrente,
            "motivo": "Conta ignorada (PREVISAO ou caixinha)",
        })
        continue

    categoria = dados.get("categoria") or dados.get("descricao_categoria") or ""
    nf_cf = dados.get("nf_cf") or dados.get("numero_documento") or dados.get("numero_nf") or ""
    codigo_lancamento = dados.get("codigo_lancamento") or dados.get("nCodTitulo") or "sem_codigo"
    valor = dados.get("valor_documento") or dados.get("valor") or 0
    fornecedor = dados.get("nome_fantasia") or dados.get("razao_social") or ""

    # Enriquecimento: as 3 colunas.
    dados["tipo"] = calcular_tipo(conta_corrente)
    dados["anexo"] = calcular_anexo(nf_cf, categoria)
    dados["responsavel"] = calcular_responsavel(conta_corrente)

    # Deteccao N1 - Conta corrente nova (nunca vista antes no catalogo)
    if not eh_conta_catalogada(conta_corrente):
        chave = str(conta_corrente).strip().lower()
        if chave and chave not in contas_novas_ja_sinalizadas:
            contas_novas_ja_sinalizadas.add(chave)
            apontamentos.append({
                "regra": "N1",
                "tipo_apontamento": "Novidade - Conta corrente nova",
                "gravidade": "medio",
                "codigo_lancamento": codigo_lancamento,
                "conta_corrente": conta_corrente,
                "fornecedor": fornecedor,
                "valor": valor,
                "descricao": (
                    "Conta corrente '" + str(conta_corrente) + "' nao esta "
                    "catalogada. Verifique se e uma conta legitima e adicione "
                    "em CONTAS_CORRENTES_CATALOGADAS no Node 5. "
                    "Tambem confira se essa conta deve ir para Diretoria "
                    "(CARTOES_DIRETORIA)."
                ),
                "acao_sugerida": "Kamilla adiciona no catalogo",
            })

    # Deteccao N2 - Categoria nova (nem isenta nem conhecida)
    if not eh_categoria_catalogada(categoria):
        chave = str(categoria).strip().lower()
        if chave and chave not in categorias_novas_ja_sinalizadas:
            categorias_novas_ja_sinalizadas.add(chave)
            apontamentos.append({
                "regra": "N2",
                "tipo_apontamento": "Novidade - Categoria nova",
                "gravidade": "medio",
                "codigo_lancamento": codigo_lancamento,
                "categoria": categoria,
                "fornecedor": fornecedor,
                "valor": valor,
                "descricao": (
                    "Categoria '" + str(categoria) + "' nao esta catalogada. "
                    "Verifique se ela DEVERIA ser isenta de doc fiscal (adiciona "
                    "em CATEGORIAS_ISENTAS) ou se e uma categoria comum que "
                    "GERA doc fiscal (adiciona em CATEGORIAS_NAO_ISENTAS_CONHECIDAS). "
                    "Enquanto nao decidir, o Anexo desses lancamentos vira 'Sem Anexo'."
                ),
                "acao_sugerida": "Kamilla classifica: isenta ou nao?",
            })

    pagamentos_enriquecidos.append(dados)


# --------------------------------------------------------------
# 2. CONCILIACAO QIVE x OMIE
# --------------------------------------------------------------
# Cruza notas fiscais da Qive com pagamentos do Omie e gera as
# categorias que vao alimentar a Aba 3 "Conciliacao Qive" do Sheets:
#
#   1. Batem OK              -> conciliadas 100%
#   1b. Divergencias         -> mesma NF+CNPJ mas valor/data/tipo diferentes
#   2. Mesma NF, CNPJ dif    -> matriz/filial ou erro de digitacao
#   3. So na Qive            -> NF sem lancamento no Omie (marca Repetido? via Supabase)
#   3b. Notas informativas   -> devolucao/remessa/comodato - DESCONSIDERADAS
#   4. So no Omie            -> lancamento Nota Fiscal sem NF na Qive
#   5. Canceladas            -> NF cancelada (alerta pra estorno se pago)
#   6. NFS-e sem CFOP        -> revisao manual (nao ha CFOP nacional)
#
# Logica herdada da skill "conciliacao-qive-omie":
#   - Chave = NF + CNPJ (so digitos)
#   - Somar linhas repetidas no Omie antes de comparar valor
#   - Ignorar Devolucao/Remessa/Comodato via CFOP e Finalidade
#   - Repetido? conta quantas semanas a NF ta pendente (do Supabase)
# --------------------------------------------------------------

# Puxa notas da Qive e historico do Supabase (semana anterior).
# Se os campos nao existirem no input, listas vazias evitam erros.
notas_qive = _extrair_notas_qive_do_input()
historico_supabase = []
for _item in _items:
    _dados = _item.json if hasattr(_item, "json") else _item.get("json", {})
    _hist = _dados.get("historico_so_qive") or _dados.get("supabase_snapshots")
    if isinstance(_hist, list):
        historico_supabase.extend(_hist)

historico_repetidos = _extrair_historico_so_qive_do_supabase(historico_supabase)

# Executa cruzamento.
conciliacao_qive = conciliar_qive_omie(
    notas_qive=notas_qive,
    pagamentos_omie=pagamentos_enriquecidos,
    historico_repetidos=historico_repetidos,
)

# Adiciona apontamentos automaticos para os casos criticos:
#   - So na Qive com gravidade critico
#   - Divergencias
#   - Canceladas (potencial estorno)
for _so_qv in conciliacao_qive["so_qive"]:
    if _so_qv.get("gravidade") in ("alto", "critico"):
        apontamentos.append({
            "regra": "Q1",
            "tipo_apontamento": "NF pendente de lancamento no Omie",
            "gravidade": _so_qv["gravidade"],
            "numero_nf": _so_qv["numero_nf"],
            "cnpj": _so_qv["cnpj_emitente"],
            "razao_social": _so_qv.get("razao_social"),
            "valor": _so_qv["valor"],
            "descricao": (
                "NF " + str(_so_qv["numero_nf"]) + " esta na Qive mas nao no Omie. "
                "Status: " + _so_qv["repetido"] + ". "
                "Cobrar lancamento com urgencia."
            ),
            "acao_sugerida": "Thai: lancar no Omie",
        })

for _div in conciliacao_qive["divergencias"]:
    apontamentos.append({
        "regra": "Q2",
        "tipo_apontamento": "Divergencia Qive x Omie",
        "gravidade": "alto",
        "numero_nf": _div["numero_nf"],
        "cnpj": _div["cnpj"],
        "razao_social": _div.get("razao_social"),
        "descricao": " | ".join(_div["divergencias"]),
        "acao_sugerida": "Conferir valor lancado no Omie",
    })

for _cancel in conciliacao_qive["canceladas"]:
    apontamentos.append({
        "regra": "Q3",
        "tipo_apontamento": "NF cancelada",
        "gravidade": "critico",
        "numero_nf": _cancel.get("numero_nf"),
        "cnpj": _cancel.get("cnpj_emitente"),
        "razao_social": _cancel.get("razao_social"),
        "valor": _valor_para_float(_cancel.get("valor")),
        "descricao": (
            "NF cancelada pelo fornecedor. Se ja foi paga, "
            "cobrar estorno."
        ),
        "acao_sugerida": "Verificar pagamento e solicitar estorno se aplicavel",
    })


# --------------------------------------------------------------
# 3. VALIDACOES - LOTE 1
# --------------------------------------------------------------
# Regras R1, R2, R3, R6, R7, R10, R11, R12, R13, Extra A, Extra B.
# NAO precisam de dado externo (so a base do Omie).
#
# Regras dependentes de fonte externa - LOTE 2 e 3:
#   R4  - Consistencia historica (Supabase)  [TODO Lote 3]
#   R5  - Reembolsos vs Base Loggers Sheets  [TODO Lote 2]
#   R8  - Fornecedor novo vs historico       [TODO Lote 3]
#   R9  - Imposto ausente + 14 CNPJs         [TODO Lote 2]
#   Base Fornecedores (Vercel)                [TODO Lote 3]
# --------------------------------------------------------------

def _norm_txt(texto):
    """Normaliza texto pra comparacao case-insensitive sem acentos."""
    if not texto:
        return ""
    s = str(texto).strip().lower()
    # Remove acentos comuns
    mapa = {
        "á": "a", "à": "a", "ã": "a", "â": "a", "ä": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "õ": "o", "ô": "o", "ö": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ç": "c", "ñ": "n",
    }
    for acento, sem in mapa.items():
        s = s.replace(acento, sem)
    return s


def _add_apontamento(regra, gravidade, pagamento, descricao, acao=""):
    """Helper pra criar apontamento uniforme."""
    apontamentos.append({
        "regra": regra,
        "gravidade": gravidade,
        "codigo_lancamento": pagamento.get("codigo_lancamento"),
        "conta_corrente": pagamento.get("conta_corrente"),
        "categoria": pagamento.get("categoria"),
        "departamento": pagamento.get("departamento"),
        "cnpj_cpf": pagamento.get("cnpj_cpf"),
        "fornecedor": pagamento.get("razao_social") or pagamento.get("nome_fantasia"),
        "valor": pagamento.get("valor_documento"),
        "descricao": descricao,
        "acao_sugerida": acao,
    })


# ===== R1: Cartao x Categoria =====
for pag in pagamentos_enriquecidos:
    tipo = pag.get("tipo", "")
    categoria = _norm_txt(pag.get("categoria"))
    conta_norm = _norm_txt(pag.get("conta_corrente"))
    tem_cartao_conta = "cartao" in conta_norm
    tem_cartao_categoria = "cartao de credito" in categoria

    if tem_cartao_conta and not tem_cartao_categoria:
        _add_apontamento(
            "R1", "alto", pag,
            "Conta e cartao mas categoria NAO contem 'Cartao de credito'.",
            "Corrigir categoria no Omie",
        )
    elif not tem_cartao_conta and tem_cartao_categoria:
        _add_apontamento(
            "R1", "alto", pag,
            "Conta bancaria mas categoria e 'Cartao de credito'.",
            "Corrigir categoria ou trocar conta",
        )


# ===== R2: Departamento N/D =====
for pag in pagamentos_enriquecidos:
    depto = _norm_txt(pag.get("departamento"))
    fornecedor = _norm_txt(pag.get("razao_social") or pag.get("nome_fantasia"))
    eh_excecao = any(exc in fornecedor for exc in DEPTO_ND_EXCECOES)

    if (depto in ("", "n/d", "nd") or not depto) and not eh_excecao:
        _add_apontamento(
            "R2", "medio", pag,
            "Departamento em branco ou N/D (nao e excecao Stripe).",
            "Preencher departamento no Omie",
        )


# ===== R3: Tipo Documento (Cartao vs Pipefy com "Outros") =====
# R3 CORRIGIDO em 30/08/2026:
#   Cartao + Outros + sem NF E sem anexo => OK
#   Cartao + Outros + tem anexo => ERRO
#   Cartao + Outros + tem NF => ERRO
#   Pipefy + Outros + categoria isenta => OK
#   Pipefy + Outros + categoria proibida => ERRO
for pag in pagamentos_enriquecidos:
    tipo_documento = _norm_txt(pag.get("tipo_documento"))
    if tipo_documento != "outros":
        continue

    tipo = pag.get("tipo", "")
    categoria = _norm_txt(pag.get("categoria"))
    nf_cf = str(pag.get("nf_cf") or "").strip()
    anexo = pag.get("anexo", "")

    if tipo == "Cartao de Credito":
        # Cartao + Outros
        if nf_cf and anexo == "OK":
            _add_apontamento(
                "R3", "critico", pag,
                "Cartao + Outros mas tem NF preenchido E anexo. Corrigir Tipo do Documento.",
                "Alterar Tipo pra 'NF' ou 'Fatura'",
            )
        elif nf_cf:
            _add_apontamento(
                "R3", "critico", pag,
                "Cartao + Outros mas tem NF preenchido. Corrigir Tipo do Documento.",
                "Alterar Tipo pra 'NF'",
            )
        elif anexo == "OK":
            _add_apontamento(
                "R3", "critico", pag,
                "Cartao + Outros mas tem anexo. Corrigir Tipo do Documento.",
                "Alterar Tipo pra 'Fatura' ou 'Recibo'",
            )
        # Se NF vazio E sem anexo -> OK (regra 30/08/2026)

    elif tipo == "Pipefy":
        eh_isenta = any(_norm_txt(c) in categoria for c in R3_CATEGORIAS_ISENTAS_PIPEFY_OUTROS)
        eh_proibida = any(_norm_txt(c) in categoria for c in R3_CATEGORIAS_PROIBIDAS_PIPEFY_OUTROS)

        if eh_proibida:
            _add_apontamento(
                "R3", "alto", pag,
                "Pipefy + Outros + categoria FGTS/Impostos/INSS/IRRF/Tributos. Tipo deve ser 'Imposto', 'Guia' ou similar.",
                "Alterar Tipo do Documento",
            )
        elif not eh_isenta and not nf_cf:
            _add_apontamento(
                "R3", "medio", pag,
                "Pipefy + Outros + categoria nao classificada. Revisar se Tipo esta correto.",
                "Verificar Tipo do Documento",
            )


# ===== R6: Eventos 2026 -> Depto 16 =====
_eventos_norm = [_norm_txt(e) for e in EVENTOS_2026]
for pag in pagamentos_enriquecidos:
    categoria = _norm_txt(pag.get("categoria"))
    observacao = _norm_txt(pag.get("observacao"))
    depto = str(pag.get("departamento") or "").strip()

    # Detecta evento na categoria ou observacao
    eh_evento = any(evento in categoria or evento in observacao for evento in _eventos_norm)

    if eh_evento and not depto.startswith(DEPTO_MARKETING_INSTITUCIONAL):
        _add_apontamento(
            "R6", "alto", pag,
            "Evento 2026 detectado mas departamento nao e '16 - MARKETING INSTITUCIONAL CORPORATE'.",
            "Corrigir departamento pra 16",
        )


# ===== R7: Admin Parana - 6 estacionamento + 6 condominio =====
_admin_parana_norm = _norm_txt(ADMIN_PARANA_NOME)
admin_parana_estacionamento = 0
admin_parana_condominio = 0

for pag in pagamentos_enriquecidos:
    fornecedor = _norm_txt(pag.get("razao_social") or pag.get("nome_fantasia"))
    if _admin_parana_norm not in fornecedor:
        continue
    categoria = _norm_txt(pag.get("categoria"))
    if "estacionamento" in categoria:
        admin_parana_estacionamento += 1
    elif "condominio" in categoria or "condomínio" in categoria:
        admin_parana_condominio += 1

# So sinaliza divergencia (nao trava semana a semana)
# Regra do doc: Admin Parana = 6 lancamentos cada
# Mas em semana individual pode ter menos ainda (fluxo semanal)
# So aponta se estamos rodando fechamento mensal com contagem errada
# Marca como INFO pra revisao humana confirmar
if admin_parana_estacionamento > 0 or admin_parana_condominio > 0:
    if admin_parana_estacionamento != ADMIN_PARANA_QTD_ESPERADA:
        apontamentos.append({
            "regra": "R7",
            "gravidade": "medio",
            "descricao": (
                "Admin Parana Estacionamento: " + str(admin_parana_estacionamento) +
                " lancamentos (esperado " + str(ADMIN_PARANA_QTD_ESPERADA) + " ao final do mes)."
            ),
            "acao_sugerida": "Aguardar fim do mes ou conferir se ha lancamento faltando",
            "categoria": "ESTACIONAMENTO",
            "fornecedor": ADMIN_PARANA_NOME,
        })
    if admin_parana_condominio != ADMIN_PARANA_QTD_ESPERADA:
        apontamentos.append({
            "regra": "R7",
            "gravidade": "medio",
            "descricao": (
                "Admin Parana Condominio: " + str(admin_parana_condominio) +
                " lancamentos (esperado " + str(ADMIN_PARANA_QTD_ESPERADA) + " ao final do mes)."
            ),
            "acao_sugerida": "Aguardar fim do mes ou conferir se ha lancamento faltando",
            "categoria": "CONDOMINIO",
            "fornecedor": ADMIN_PARANA_NOME,
        })


# ===== R10: Projeto N/D =====
for pag in pagamentos_enriquecidos:
    projeto = _norm_txt(pag.get("projeto"))
    tipo = pag.get("tipo", "")
    categoria = _norm_txt(pag.get("categoria"))

    if projeto in ("", "n/d", "nd") or not projeto:
        # Cartao eh isento de Projeto N/D
        if tipo == "Cartao de Credito":
            continue

        # Se categoria e isenta, tudo bem
        if any(_norm_txt(c) in categoria for c in R10_CATEGORIAS_ISENTAS):
            continue

        _add_apontamento(
            "R10", "medio", pag,
            "Projeto = N/D em Pipefy/conta bancaria com categoria nao isenta.",
            "Preencher Projeto/Solicitante no Omie",
        )


# ===== R11: Data Emissao Antiga =====
# Cartao: qualquer data OK (fatura pode ser de mes anterior)
# Pipefy: emissao >= mes anterior OK; < mes anterior = ERRO; ano anterior = CRITICO
# Excecao: DELL/PARS com "parcela X/Y" na observacao
_hoje = _datetime.datetime.now()
_ano_atual = _hoje.year
_mes_atual = _hoje.month
if _mes_atual == 1:
    _limite_mes_anterior_ano = _ano_atual - 1
    _limite_mes_anterior_mes = 12
else:
    _limite_mes_anterior_ano = _ano_atual
    _limite_mes_anterior_mes = _mes_atual - 1

def _parse_data_emissao(valor):
    """Tenta parsear formatos dd/mm/yyyy, yyyy-mm-dd, dd-mm-yyyy."""
    if not valor:
        return None
    s = str(valor).strip()[:10]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return _datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

for pag in pagamentos_enriquecidos:
    tipo = pag.get("tipo", "")
    if tipo == "Cartao de Credito":
        continue  # Cartao aceita qualquer data

    data_emissao = _parse_data_emissao(pag.get("data_emissao"))
    if not data_emissao:
        continue

    observacao = str(pag.get("observacao") or "").lower()
    fornecedor_norm = _norm_txt(pag.get("razao_social") or pag.get("nome_fantasia"))
    tem_parcelamento = bool(R11_REGEX_PARCELA.search(observacao))
    fornecedor_parcelavel = any(f in fornecedor_norm for f in R11_FORNECEDORES_PARCELAMENTO)

    if tem_parcelamento or fornecedor_parcelavel:
        continue  # Excecao DELL/PARS ou parcela X/Y documentada

    # Ano anterior = CRITICO
    if data_emissao.year < _ano_atual:
        _add_apontamento(
            "R11", "critico", pag,
            "Data de emissao de ANO ANTERIOR (" + data_emissao.strftime("%d/%m/%Y") + "). Verificar urgencia.",
            "Confirmar se e parcela antiga documentada",
        )
    elif (data_emissao.year, data_emissao.month) < (_limite_mes_anterior_ano, _limite_mes_anterior_mes):
        _add_apontamento(
            "R11", "alto", pag,
            "Data de emissao com mais de 1 mes de atraso (" + data_emissao.strftime("%d/%m/%Y") + ").",
            "Verificar motivo do atraso ou registrar parcelamento",
        )


# ===== R12: Multa e Juros != 0 =====
for pag in pagamentos_enriquecidos:
    multa = _valor_para_float(pag.get("multa"))
    juros = _valor_para_float(pag.get("juros"))

    if multa > 0 or juros > 0:
        _add_apontamento(
            "R12", "critico", pag,
            "Pagamento com Multa R$ " + str(round(multa, 2)) +
            " e/ou Juros R$ " + str(round(juros, 2)) + " - pagamento em atraso.",
            "Investigar motivo do atraso",
        )


# ===== R13: Facilities -> Depto 62 =====
_facilities_norm = [_norm_txt(c) for c in R13_CATEGORIAS_FACILITIES]
for pag in pagamentos_enriquecidos:
    categoria = _norm_txt(pag.get("categoria"))
    depto = str(pag.get("departamento") or "").strip()

    eh_facilities = any(cat in categoria for cat in _facilities_norm)

    if eh_facilities and not depto.startswith(DEPTO_FACILITIES):
        _add_apontamento(
            "R13", "medio", pag,
            "Categoria Facilities detectada mas departamento nao e '62 - FACILITIES CORPORATE'.",
            "Corrigir departamento pra 62",
        )


# ===== EXTRA A: Duplicatas vs Rateios =====
# Agrupa por (CNPJ, NF)
_grupos_nf_cnpj = {}
for pag in pagamentos_enriquecidos:
    nf = _so_digitos(pag.get("nf_cf"))
    cnpj = _so_digitos(pag.get("cnpj_cpf"))
    if not nf or not cnpj:
        continue
    chave = (cnpj, nf)
    _grupos_nf_cnpj.setdefault(chave, []).append(pag)

for chave, lancamentos in _grupos_nf_cnpj.items():
    if len(lancamentos) < 2:
        continue

    # Rateio legitimo: departamentos diferentes
    deptos = {_norm_txt(l.get("departamento")) for l in lancamentos}
    if len(deptos) == len(lancamentos):
        continue  # Cada linha em um depto = rateio OK

    # Duplicata: mesmo depto em 2+ linhas
    contagem_deptos = _Counter(_norm_txt(l.get("departamento")) for l in lancamentos)
    for depto_norm, qtd in contagem_deptos.items():
        if qtd < 2:
            continue
        lancs_duplicados = [l for l in lancamentos if _norm_txt(l.get("departamento")) == depto_norm]
        primeiro = lancs_duplicados[0]
        codigos = [str(l.get("codigo_lancamento")) for l in lancs_duplicados]
        apontamentos.append({
            "regra": "Extra-A",
            "tipo_apontamento": "DUPLICATA",
            "gravidade": "critico",
            "codigos_lancamentos": codigos,
            "cnpj_cpf": primeiro.get("cnpj_cpf"),
            "fornecedor": primeiro.get("razao_social") or primeiro.get("nome_fantasia"),
            "categoria": primeiro.get("categoria"),
            "departamento": primeiro.get("departamento"),
            "descricao": (
                "DUPLICATA detectada: mesmo CNPJ + NF + Depto em " +
                str(qtd) + " lancamentos. Codigos: " + ", ".join(codigos)
            ),
            "acao_sugerida": "Verificar se eh erro de lancamento duplo",
        })


# ===== EXTRA B: Documentos Ausentes (informativo) =====
# Ja coberto pela coluna Anexo, mas aponta para revisao
sem_nf_count = sum(1 for p in pagamentos_enriquecidos if not str(p.get("nf_cf") or "").strip())
sem_anexo_count = sum(1 for p in pagamentos_enriquecidos if p.get("anexo") == "Sem Anexo")

if sem_nf_count > 0:
    apontamentos.append({
        "regra": "Extra-B",
        "tipo_apontamento": "Documentos NF Ausentes",
        "gravidade": "medio",
        "descricao": "Total de lancamentos sem NF/CF preenchido: " + str(sem_nf_count),
        "acao_sugerida": "Ver aba 'Base de Pagamentos' filtrando NF vazio",
    })

if sem_anexo_count > 0:
    apontamentos.append({
        "regra": "Extra-B",
        "tipo_apontamento": "Anexos Ausentes",
        "gravidade": "medio",
        "descricao": "Total de lancamentos com Anexo='Sem Anexo': " + str(sem_anexo_count),
        "acao_sugerida": "Cobrar documentos dos fornecedores",
    })


# ===== R5: Reembolsos - Departamento = CC do colaborador =====
# Requer: input do node "Sheets - Base Loggers"
# Regra:
#   CPF encontrado + Depto = CC do colaborador -> OK
#   CPF encontrado + Depto != CC do colaborador -> ERRO
#   CPF NAO encontrado -> ATENCAO (pode ser colaborador novo)
# Aplicavel so em pagamentos identificados como REEMBOLSO
# ================================================================

def _extrair_base_loggers_do_input():
    """
    Le a Base Loggers do node Sheets - Base Loggers.
    Retorna dict indexado por CPF normalizado (so digitos):
      { "36388627803": {"nome": "...", "cc": "...", "matricula": ..., "cargo": "..."} }
    """
    idx = {}
    for item in _items:
        dados = item.json if hasattr(item, "json") else item.get("json", {})
        # Aceita tanto "CPF" quanto "cpf" quanto variacoes
        cpf_raw = dados.get("CPF") or dados.get("cpf") or dados.get("Cpf")
        if not cpf_raw:
            continue
        cpf_key = _so_digitos(cpf_raw)
        if len(cpf_key) != 11:
            continue  # CPF invalido, pula
        cc_raw = (dados.get("Centro de Custo") or dados.get("centro_de_custo") or
                  dados.get("Centro de custo") or dados.get("CC"))
        idx[cpf_key] = {
            "nome": dados.get("Colaborador") or dados.get("colaborador") or "",
            "cc": str(cc_raw or "").strip(),
            "matricula": dados.get("Mat") or dados.get("mat") or "",
            "cargo": dados.get("Cargo") or dados.get("cargo") or "",
            "gestor": dados.get("Nome do Gestor") or dados.get("gestor") or "",
        }
    return idx


base_loggers_idx = _extrair_base_loggers_do_input()

# Palavras-chave que indicam que o lancamento e um REEMBOLSO
_R5_INDICADORES_REEMBOLSO = (
    "reembolso", "reembol", "ressarcimento", "adiantamento devolvido",
)


def _eh_reembolso(pag):
    """Detecta se um pagamento e reembolso (por tipo doc ou categoria)."""
    tipo_doc = _norm_txt(pag.get("tipo_documento"))
    categoria = _norm_txt(pag.get("categoria"))
    observacao = _norm_txt(pag.get("observacao"))
    for kw in _R5_INDICADORES_REEMBOLSO:
        if kw in tipo_doc or kw in categoria or kw in observacao:
            return True
    return False


def _norm_cc(cc):
    """Normaliza Centro de Custo pra comparacao (maiuscula, sem espacos extras)."""
    return " ".join(str(cc or "").strip().upper().split())


for pag in pagamentos_enriquecidos:
    if not _eh_reembolso(pag):
        continue

    cpf_pag = _so_digitos(pag.get("cnpj_cpf"))
    # CPFs tem 11 digitos, CNPJs 14. Se nao for 11, pula (nao e pessoa fisica).
    if len(cpf_pag) != 11:
        continue

    logger = base_loggers_idx.get(cpf_pag)
    depto_lancamento = str(pag.get("departamento") or "").strip()

    if not logger:
        # CPF nao encontrado na Base Loggers
        _add_apontamento(
            "R5", "medio", pag,
            "CPF do reembolso NAO encontrado na Base Loggers. Pode ser colaborador novo ou CPF errado.",
            "Confirmar cadastro do colaborador",
        )
        continue

    # CPF encontrado. Comparar Depto do lancamento com CC do colaborador.
    cc_esperado = _norm_cc(logger["cc"])
    depto_lancamento_norm = _norm_cc(depto_lancamento)

    if not cc_esperado:
        # Colaborador sem CC preenchido na Base Loggers (raro)
        continue

    # Match tolerante: aceita se depto do lancamento contem o CC (com codigo tipo "62 - CC")
    # ou vice-versa. Considera equivalentes se um esta contido no outro.
    if cc_esperado in depto_lancamento_norm or depto_lancamento_norm in cc_esperado:
        # Match OK - nao apontar
        continue

    _add_apontamento(
        "R5", "alto", pag,
        (
            "Reembolso de " + str(logger["nome"]) +
            " (CPF " + str(pag.get("cnpj_cpf")) + "). " +
            "Depto lancado: '" + str(depto_lancamento) + "'. " +
            "CC do colaborador: '" + str(logger["cc"]) + "'. " +
            "DIVERGENTE."
        ),
        "Alterar departamento pro CC do colaborador",
    )


# ===== TODO Lote 2 =====
# R9 - Imposto ausente (14 CNPJs sempre_tax + logica dinamica)

# ===== TODO Lote 3 =====
# R4 - Consistencia historica (Supabase snapshots)
# R8 - Fornecedor novo vs historico 5 meses
# Base Fornecedores (Vercel)

# --------------------------------------------------------------
# 4. RETORNO
# --------------------------------------------------------------

# Estatisticas rapidas para o AI Agent usar no e-mail
total_cartao = sum(1 for p in pagamentos_enriquecidos if p.get("tipo") == "Cartao de Credito")
total_pipefy = sum(1 for p in pagamentos_enriquecidos if p.get("tipo") == "Pipefy")
total_ok = sum(1 for p in pagamentos_enriquecidos if p.get("anexo") == "OK")
total_sem_anexo = sum(1 for p in pagamentos_enriquecidos if p.get("anexo") == "Sem Anexo")
total_nao_gera = sum(1 for p in pagamentos_enriquecidos if p.get("anexo") == "Nao gera doc fiscal")
total_diretoria = sum(1 for p in pagamentos_enriquecidos if p.get("responsavel") == "Diretoria")
total_cp = sum(1 for p in pagamentos_enriquecidos if p.get("responsavel") == "Contas a Pagar")

# Novidades detectadas
qtd_contas_novas = len(contas_novas_ja_sinalizadas)
qtd_categorias_novas = len(categorias_novas_ja_sinalizadas)

# Snapshot pra salvar no Supabase (Node 6) - usado semana seguinte
# para calcular Repetido? nas NFs pendentes.
so_qive_nfs_atuais = [item["numero_nf"] for item in conciliacao_qive["so_qive"]]

return [{
    "json": {
        # Base enriquecida com as 3 colunas calculadas (Tipo, Anexo, Responsavel).
        # Usado pelo Node 8 (Google Sheets - aba Base de Pagamentos).
        "pagamentos": pagamentos_enriquecidos,

        # Log de auditoria: lancamentos filtrados (contas PREVISAO/caixinha).
        # Nao vai pra planilha destino, so pra debug se algo sumir do total.
        "pagamentos_ignorados": pagamentos_ignorados,

        # Apontamentos R1-R15 + N1/N2 + Q1/Q2/Q3.
        # Usado pelo Node 8 (aba Analise Critica) e pelo AI Agent.
        "apontamentos": apontamentos,

        # Resultado detalhado do cruzamento Qive x Omie.
        # Usado pelo Node 8 (aba Conciliacao Qive).
        "conciliacao_qive": conciliacao_qive,

        # Lista simples de NFs "So na Qive" desta semana - vira snapshot
        # no Supabase pra semana seguinte calcular Repetido?
        "so_qive_nfs": so_qive_nfs_atuais,

        # Resumo pra AI Agent redigir o e-mail e pra aba Resumo Executivo.
        "resumo": {
            "total_lancamentos": len(pagamentos_enriquecidos),
            "total_lancamentos_ignorados": len(pagamentos_ignorados),
            "total_apontamentos": len(apontamentos),
            "novidades": {
                "contas_novas": qtd_contas_novas,
                "categorias_novas": qtd_categorias_novas,
            },
            "por_tipo": {
                "cartao_credito": total_cartao,
                "pipefy": total_pipefy,
            },
            "por_anexo": {
                "ok": total_ok,
                "sem_anexo": total_sem_anexo,
                "nao_gera_doc_fiscal": total_nao_gera,
            },
            "por_responsavel": {
                "diretoria": total_diretoria,
                "contas_a_pagar": total_cp,
            },
            "qive_omie": {
                "batem_ok": len(conciliacao_qive["batem_ok"]),
                "divergencias": len(conciliacao_qive["divergencias"]),
                "mesma_nf_cnpj_dif": len(conciliacao_qive["mesma_nf_cnpj_dif"]),
                "so_qive": len(conciliacao_qive["so_qive"]),
                "so_omie": len(conciliacao_qive["so_omie"]),
                "informativas_ignoradas": len(conciliacao_qive["informativas"]),
                "canceladas": len(conciliacao_qive["canceladas"]),
                "nfse_revisar_manual": len(conciliacao_qive["nfse_revisar"]),
            },
            "por_regra": {
                _r: sum(1 for a in apontamentos if a.get("regra") == _r)
                for _r in ("R1", "R2", "R3", "R5", "R6", "R7", "R10", "R11",
                           "R12", "R13", "Extra-A", "Extra-B",
                           "N1", "N2", "Q1", "Q2", "Q3")
            },
            "base_loggers_carregados": len(base_loggers_idx),
            "por_gravidade": {
                _g: sum(1 for a in apontamentos if a.get("gravidade") == _g)
                for _g in ("critico", "alto", "medio", "baixo")
            },
        },
    }
}]
