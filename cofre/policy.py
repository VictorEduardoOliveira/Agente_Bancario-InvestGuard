"""Policy engine determinístico (intent gate), 2FA, fila humana, disjuntor, rate limit e auditoria."""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from .bank import CATALOGO, NIVEL_RISCO, PERMISSOES, TOOLS, Ctx, Store, ToolError, brl, parse_valor

APROVADO, NEGADO, REQUER_CONFIRMACAO, REQUER_HUMANO = "APROVADO", "NEGADO", "REQUER_CONFIRMACAO", "REQUER_HUMANO"
CAMPOS_PROIBIDOS = {"customer_id", "conta", "account", "cliente", "client_id", "conta_destino", "aprovado_humano", "idempotency_key"}
AVISO_RISCO = ("Aviso: este produto tem risco maior que a conta remunerada e a rentabilidade não é garantida. "
               "Valores são simulações, não promessa de ganho.")
CONFIRMACAO_TURNOS = 2
OTP_TENTATIVAS = 3
RE_CONFIRMO = re.compile(r"^\s*confirmo\s+(\d{6})\s*$", re.IGNORECASE)


@dataclass
class Decision:
    kind: str
    motivo: str = ""
    flags: list = field(default_factory=list)
    humano: bool = False
    aviso: str = ""


def proposal_hash(name: str, args: dict) -> str:
    return hashlib.sha256(json.dumps({"n": name, "a": args}, sort_keys=True, default=str).encode()).hexdigest()


def decide(proposal: dict, ctx: Ctx, store: Store, turno: dict) -> Decision:
    name, args = proposal.get("name"), proposal.get("args") or {}
    if name not in TOOLS or not isinstance(args, dict):
        return Decision(NEGADO, "tool_desconhecida", ["tool_desconhecida"])
    if CAMPOS_PROIBIDOS & set(args):
        return Decision(NEGADO, "tentativa_outra_conta", ["tentativa_outra_conta"])
    if not set(args) <= PERMISSOES[name]["args"]:
        return Decision(NEGADO, "args_fora_do_schema", ["args_fora_do_schema"])
    if turno["tool_calls"] >= 3:
        return Decision(NEGADO, "orcamento_tool_calls", ["orcamento"])
    p = PERMISSOES[name]
    if p["escreve"] and turno["escritas"] >= 1:
        return Decision(NEGADO, "uma_escrita_por_turno", ["orcamento"])
    try:
        if name == "consultar_extrato":
            d, l = args.get("dias", p["padrao_dias"]), args.get("limite", p["padrao_itens"])
            if not (isinstance(d, int) and 1 <= d <= p["max_dias"]) or not (isinstance(l, int) and 1 <= l <= p["max_itens"]):
                return Decision(NEGADO, "periodo_invalido")
            return Decision(APROVADO)
        if name == "simular_rendimento":
            parse_valor(args.get("valor"))
            if args.get("codigo") not in CATALOGO:
                return Decision(NEGADO, "codigo_invalido")
            return Decision(APROVADO)
        if not p["escreve"]:
            return Decision(APROVADO)
        if name == "sacar":
            v = parse_valor(args.get("valor"))
            if v > p["max_com_humano"]:
                return Decision(NEGADO, "acima_limite_operacao", ["acima_limite"])
            if v > store.conta(ctx).saldo:
                return Decision(NEGADO, "saldo_insuficiente")
            flags = ["acima_limite_autonomo"] if v > p["max_autonomo"] else []
            if v > p["max_sem_humano"]:
                return Decision(REQUER_CONFIRMACAO, "2fa+humano", flags + ["requer_humano"], humano=True)
            return Decision(REQUER_CONFIRMACAO, "2fa", flags)
        if name == "depositar":
            v = parse_valor(args.get("valor"))
            if v > p["max_op"]:
                return Decision(NEGADO, "acima_limite_operacao", ["acima_limite"])
            if store.depositos_hoje(ctx) + v > p["max_dia_sem_humano"]:
                return Decision(REQUER_CONFIRMACAO, "2fa+humano", ["requer_humano", "prevencao_lavagem"], humano=True)
            return Decision(REQUER_CONFIRMACAO, "2fa")
        if name == "definir_rendimento":
            cod = args.get("codigo")
            if cod not in CATALOGO:
                return Decision(NEGADO, "codigo_invalido")
            if store.conta(ctx).rendimento_alterado_em == dt.date.today():
                return Decision(NEGADO, "limite_alteracoes_dia")
            aviso = AVISO_RISCO if NIVEL_RISCO[CATALOGO[cod]["risco"]] > NIVEL_RISCO["baixissimo"] else ""
            return Decision(REQUER_CONFIRMACAO, "2fa", ["aviso_risco"] if aviso else [], aviso=aviso)
    except ToolError as e:
        return Decision(NEGADO, str(e))
    return Decision(NEGADO, "regra_ausente")


class SegundoFator:
    """2FA simulado: código de 6 dígitos de uso único, enviado fora do chat (SMS/app)."""

    def __init__(self, canal=None):
        self.canal = canal or (lambda conta, codigo: None)

    def emitir(self, conta_mascarada: str) -> str:
        codigo = f"{secrets.randbelow(10**6):06d}"
        self.canal(conta_mascarada, codigo)
        return hashlib.sha256(codigo.encode()).hexdigest()

    @staticmethod
    def verificar(codigo: str, codigo_hash: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(codigo.encode()).hexdigest(), codigo_hash)


class Approvals:
    def __init__(self, store: Store, audit=None):
        self.store, self.audit, self.itens = store, audit, {}

    def submit(self, ctx: Ctx, name: str, args: dict, idem: str) -> str:
        aid = f"AP-{len(self.itens) + 1:04d}"
        self.itens[aid] = {"id": aid, "ctx": ctx, "name": name, "args": args, "idem": idem, "status": "pendente"}
        return aid

    def pendentes(self):
        return [i for i in self.itens.values() if i["status"] == "pendente"]

    def approve(self, aid: str) -> dict:
        i = self.itens[aid]
        if i["status"] != "pendente":
            raise ToolError("aprovacao_ja_processada")
        r = TOOLS[i["name"]](self.store, i["ctx"], idempotency_key=i["idem"], aprovado_humano=True, **i["args"])
        i["status"] = "aprovado"
        if self.audit:
            self.audit.write({"evento": "aprovacao_humana", "id": aid, "tool": i["name"], "customer_id": i["ctx"].customer_id})
        return r

    def reject(self, aid: str):
        self.itens[aid]["status"] = "rejeitado"


class Breaker:
    def __init__(self, max_erros=5, max_bloqueios=3, janela=60, aberto_por=300, clock=time.monotonic):
        self.max_erros, self.max_bloqueios, self.janela, self.aberto_por, self.clock = max_erros, max_bloqueios, janela, aberto_por, clock
        self.erros, self.bloqueios, self.aberto_ate = deque(), deque(), 0.0

    def _limpa(self, q):
        while q and self.clock() - q[0] > self.janela:
            q.popleft()

    def registrar(self, tipo: str):
        q = self.erros if tipo == "erro" else self.bloqueios
        q.append(self.clock())
        self._limpa(q)
        if len(self.erros) >= self.max_erros or len(self.bloqueios) >= self.max_bloqueios:
            self.aberto_ate = self.clock() + self.aberto_por
            self.erros.clear(); self.bloqueios.clear()

    def aberto(self) -> bool:
        return os.getenv("AGENT_ENABLED", "true").lower() == "false" or self.clock() < self.aberto_ate


class RateLimiter:
    def __init__(self, max_msgs=8, janela=10, clock=time.monotonic):
        self.max_msgs, self.janela, self.clock, self.hist = max_msgs, janela, clock, {}

    def permitir(self, session_id: str) -> bool:
        q = self.hist.setdefault(session_id, deque())
        while q and self.clock() - q[0] > self.janela:
            q.popleft()
        q.append(self.clock())
        return len(q) <= self.max_msgs


def _mascara(obj):
    if isinstance(obj, dict):
        return {k: _mascara(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_mascara(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, str):
        return re.sub(r"\d{5,}", lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], obj)
    return obj


class AuditLog:
    """Append-only em JSONL; valores sensíveis mascarados."""

    def __init__(self, path: str | None = "audit.jsonl"):
        self.path, self.memoria = path, []

    def write(self, evento: dict):
        e = _mascara({"ts": dt.datetime.now().isoformat(timespec="seconds"), **evento})
        self.memoria.append(e)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
