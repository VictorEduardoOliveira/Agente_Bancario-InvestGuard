"""Guardrails de entrada e saída: regras + regex, sem depender da LLM principal."""
import difflib
import re
import unicodedata
from dataclasses import dataclass, field

RESPOSTA_PADRAO = "Posso ajudar com saldo, extrato, depósito e saque da sua conta."
MAX_CHARS = 1000
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
HOMOGLIFOS = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "і": "i", "0": "0"})

INJECAO = [r"ignor\w*\s+(as\s+|todas\s+|suas\s+)?(\w+\s+)?(instru|regra|orienta)", r"esque[cç]a\s+(as\s+|suas\s+)?(regras|instru)",
           r"modo\s+(desenvolvedor|dev|teste|auditoria|manuten|admin|deus)", r"voc[eê]\s+agora\s+[eé]", r"finja\s+(que|ser)",
           r"system\s*prompt", r"prompt\s+do\s+sistema", r"\bdan\b", r"jailbreak",
           r"sou\s+(o|a|do|da)\s+(administrador|admin|suporte|gerente|desenvolvedor|seguran)",
           r"(repita|mostre|revele|resuma|liste)\s+.{0,25}(instru|regras|prompt|tools|ferramentas|limites)",
           r"#+\s*system", r"<\|", r"\[(system|sistema)\]", r"(nota|mensagem)\s+do\s+sistema",
           r"ignore\s+(all|previous|the)", r"disregard", r"you\s+are\s+now", r"developer\s+mode", r"act\s+as",
           r"desligu\w*\s+.{0,20}(guardrail|seguran|regra|filtro|prote)", r"(gerente|sistema|banco)\s+(j[aá]\s+)?(autorizou|confirmou|aprovou)",
           r"j[aá]\s+(foi\s+)?(confirmad|autorizad|aprovad)", r"sem\s+(confirma|2fa|c[oó]digo|verifica)",
           r"(lembr|memoriz|grav)\w*\s+.{0,30}(limite|regra|autoriza)", r"meu\s+limite\s+(agora\s+)?([eé]|passou)",
           r"[A-Za-z0-9+/]{40,}={0,2}"]
CODIGO = [r"```", r"\bimport\s+\w+", r"\b(exec|eval|os\.system|subprocess)\s*\(", r"execut\w*\s+(este|esse|o)\s+(c[oó]digo|script|comando)",
          r"python\s+-c", r"<script"]
URL = [r"https?://", r"\bwww\.", r"\b\w+\.(com|net|org|io|xyz|ru)(\.br)?\b", r"abr[ae]\s+(este|esse|o)\s+(link|arquivo|anexo)"]
OUTRA_CONTA = [r"\bconta\s*(n[ºo°.]*\s*)?\d{3,}", r"\bc-\d{3}\b", r"customer_?id", r"ag[eê]ncia\s*\d", r"(do|da)\s+(meu|minha)\s+(pai|m[aã]e|esposa|marido|filho|filha|irm[aã]o?|amigo)",
               r"sou\s+(o|a)\s+(pai|m[aã]e|respons[aá]vel|procurador)", r"outra\s+(conta|pessoa)", r"em\s+nome\s+de", r"do\s+titular"]
PII = [r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", r"\bsenha\b", r"\btoken\b", r"\bcvv\b"]
FORA_ESCOPO = [r"\bpix\b", r"transfer", r"empr[eé]stimo", r"c[aâ]mbio", r"cripto", r"bitcoin", r"financiamento", r"cart[aã]o\s+de\s+cr[eé]dito", r"\bted\b", r"\bdoc\b", r"boleto"]


@dataclass
class GuardResult:
    ok: bool
    flags: list = field(default_factory=list)
    resposta_padrao: str | None = None
    texto: str = ""


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKC", texto).translate(ZERO_WIDTH).translate(HOMOGLIFOS)
    return re.sub(r"\s+", " ", t).strip()


def _luhn(num: str) -> bool:
    d = [int(c) for c in num][::-1]
    return sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(d)) % 10 == 0


def _algum(padroes, t):
    return any(re.search(p, t, re.IGNORECASE) for p in padroes)


def check_input(texto: str) -> GuardResult:
    if not isinstance(texto, str) or len(texto) > MAX_CHARS:
        return GuardResult(False, ["tamanho"], "Sua mensagem ficou muito longa. " + RESPOSTA_PADRAO)
    t = normalizar(texto)
    if not t:
        return GuardResult(False, ["vazio"], RESPOSTA_PADRAO)
    if _algum(INJECAO, t):
        return GuardResult(False, ["injection"], RESPOSTA_PADRAO, t)
    if _algum(CODIGO, t):
        return GuardResult(False, ["codigo"], "Não executo códigos ou comandos. " + RESPOSTA_PADRAO, t)
    if _algum(URL, t):
        return GuardResult(False, ["url"], "Não abro links ou arquivos. " + RESPOSTA_PADRAO, t)
    if _algum(OUTRA_CONTA, t):
        return GuardResult(False, ["outra_conta"], "Só consigo operar a sua própria conta. " + RESPOSTA_PADRAO, t)
    digitos = re.sub(r"[ .-]", "", t)
    cartao = any(_luhn(m) for m in re.findall(r"\d{13,19}", digitos))
    if _algum(PII, t) or cartao:
        return GuardResult(False, ["pii"], "Por segurança, nunca envie CPF, senha, token ou número de cartão aqui. " + RESPOSTA_PADRAO, t)
    flags = ["fora_de_escopo"] if _algum(FORA_ESCOPO, t) else []
    return GuardResult(True, flags, None, t)


NOMES_INTERNOS = [r"consultar_saldo", r"consultar_extrato", r"definir_rendimento", r"listar_rendimentos", r"simular_rendimento",
                  r"\bdepositar\s*\(", r"\bsacar\s*\(", r"customer_id", r"policy", r"max_sem_humano", r"max_com_humano", r"<<\s*/?dados"]


def check_output(mensagem: str, intent: str, system_prompt: str, valores_tool: list | None = None) -> GuardResult:
    if intent == "ataque":
        return GuardResult(False, ["intent_ataque"], RESPOSTA_PADRAO)
    if _algum(NOMES_INTERNOS, mensagem):
        return GuardResult(False, ["vazamento_interno"], RESPOSTA_PADRAO)
    linhas = [l.strip() for l in system_prompt.splitlines() if len(l.strip()) > 30]
    for frase in re.split(r"(?<=[.!?\n])\s+", mensagem):
        if len(frase) > 30 and any(difflib.SequenceMatcher(None, frase.lower(), l.lower()).ratio() > 0.6 for l in linhas):
            return GuardResult(False, ["vazamento_prompt"], RESPOSTA_PADRAO)
    if re.search(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", mensagem) or re.search(r"\b\d{8,}\b", re.sub(r"[ .]", "", mensagem)):
        return GuardResult(False, ["pii_saida"], RESPOSTA_PADRAO)
    if valores_tool is not None:
        citados = set(re.findall(r"R\$ ?[\d.]+,\d{2}", mensagem))
        if citados - set(valores_tool):
            return GuardResult(False, ["valor_divergente"], None)
    return GuardResult(True)
