"""Banco mock em memória: catálogo imutável, tabela de permissões e tools com permissão mínima."""
import datetime as dt
import random
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import MappingProxyType

CDI_AA = Decimal("0.1065")
NIVEL_RISCO = MappingProxyType({"nenhum": 0, "baixissimo": 1, "baixo": 2, "baixo_moderado": 3})

CATALOGO = MappingProxyType({
    "PARADO": MappingProxyType({"nome": "Deixar na conta sem render", "pct_cdi": Decimal("0"), "taxa": "0%", "liquidez": "imediata", "risco": "nenhum"}),
    "CONTA_REMUNERADA": MappingProxyType({"nome": "Conta remunerada (padrão)", "pct_cdi": Decimal("1.00"), "taxa": "100% CDI", "liquidez": "imediata", "risco": "baixissimo"}),
    "CDB_DIARIO": MappingProxyType({"nome": "CDB liquidez diária", "pct_cdi": Decimal("1.02"), "taxa": "102% CDI", "liquidez": "D+0", "risco": "baixo"}),
    "TESOURO_SELIC": MappingProxyType({"nome": "Tesouro Selic", "pct_cdi": Decimal("1.00"), "taxa": "Selic", "liquidez": "D+1", "risco": "baixo"}),
    "FUNDO_RF": MappingProxyType({"nome": "Fundo renda fixa", "pct_cdi": Decimal("1.05"), "taxa": "105% CDI", "liquidez": "D+1", "risco": "baixo_moderado"}),
})
RENDIMENTO_PADRAO = "CONTA_REMUNERADA"

# Espelho do Canvas (seção 04). Nenhum limite fica "hardcoded" fora daqui.
PERMISSOES = MappingProxyType({
    "consultar_saldo": MappingProxyType({"le": True, "escreve": False, "args": frozenset()}),
    "consultar_extrato": MappingProxyType({"le": True, "escreve": False, "args": frozenset({"dias", "limite"}),
                                           "max_dias": 90, "max_itens": 50, "padrao_dias": 30, "padrao_itens": 10}),
    "depositar": MappingProxyType({"le": False, "escreve": True, "args": frozenset({"valor"}),
                                   "max_op": Decimal("5000"), "max_dia_sem_humano": Decimal("10000")}),
    "sacar": MappingProxyType({"le": False, "escreve": True, "args": frozenset({"valor"}),
                               "max_autonomo": Decimal("1000"), "max_sem_humano": Decimal("5000"),
                               "max_com_humano": Decimal("20000")}),
    "listar_rendimentos": MappingProxyType({"le": True, "escreve": False, "args": frozenset({"horizonte_dias"})}),
    "definir_rendimento": MappingProxyType({"le": False, "escreve": True, "args": frozenset({"codigo"}), "max_alteracoes_dia": 1}),
    "simular_rendimento": MappingProxyType({"le": True, "escreve": False, "args": frozenset({"valor", "codigo", "dias"}), "max_dias": 365}),
})


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class Ctx:
    customer_id: str
    session_id: str


def parse_valor(v) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise ToolError("valor_invalido")
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ToolError("valor_invalido")
    if not d.is_finite() or d <= 0 or d.as_tuple().exponent < -2 or "e" in str(v).lower():
        raise ToolError("valor_invalido")
    return d.quantize(Decimal("0.01"))


def brl(v: Decimal) -> str:
    s = f"{v.quantize(Decimal('0.01'), ROUND_HALF_UP):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class Conta:
    customer_id: str
    conta_mascarada: str
    saldo: Decimal
    lancamentos: list = field(default_factory=list)
    rendimento: str = RENDIMENTO_PADRAO
    rendimento_alterado_em: dt.date | None = None


class Store:
    def __init__(self, seed: int = 42):
        rnd = random.Random(seed)
        hoje = dt.date.today()
        self._contas: dict[str, Conta] = {}
        self._ledger: list[dict] = []
        self._idem: dict[str, dict] = {}
        for cid, final, saldo in [("c-001", "1234", "8500.00"), ("c-002", "5678", "1200.50"), ("c-003", "9012", "30000.00")]:
            c = Conta(cid, f"****{final}", Decimal(saldo))
            for _ in range(18):
                data = hoje - dt.timedelta(days=rnd.randint(1, 89))
                desc, sinal = rnd.choice([("Supermercado", -1), ("Salário", 1), ("Farmácia", -1), ("Depósito", 1), ("Restaurante", -1), ("Conta de luz", -1)])
                c.lancamentos.append({"data": data.isoformat(), "descricao": desc, "valor": Decimal(rnd.randint(20, 900)) * sinal})
            c.lancamentos.sort(key=lambda l: l["data"], reverse=True)
            self._contas[cid] = c

    @property
    def ledger(self) -> tuple:
        return tuple(MappingProxyType(dict(e)) for e in self._ledger)

    def conta(self, ctx: Ctx) -> Conta:
        if not isinstance(ctx, Ctx) or ctx.customer_id not in self._contas:
            raise ToolError("conta_invalida")
        return self._contas[ctx.customer_id]

    def depositos_hoje(self, ctx: Ctx) -> Decimal:
        hoje = dt.date.today().isoformat()
        return sum((e["valor"] for e in self._ledger if e["customer_id"] == ctx.customer_id and e["tipo"] == "deposito" and e["data"] == hoje), Decimal("0"))

    def _registrar(self, ctx, tipo, valor, idem):
        e = {"customer_id": ctx.customer_id, "session_id": ctx.session_id, "tipo": tipo, "valor": valor,
             "data": dt.date.today().isoformat(), "idempotency_key": idem}
        self._ledger.append(e)
        return e


def consultar_saldo(store: Store, ctx: Ctx) -> dict:
    c = store.conta(ctx)
    return {"conta_mascarada": c.conta_mascarada, "saldo": c.saldo, "rendimento_atual": c.rendimento}


def consultar_extrato(store: Store, ctx: Ctx, dias=None, limite=None) -> dict:
    p = PERMISSOES["consultar_extrato"]
    dias = p["padrao_dias"] if dias is None else dias
    limite = p["padrao_itens"] if limite is None else limite
    if not isinstance(dias, int) or isinstance(dias, bool) or not 1 <= dias <= p["max_dias"]:
        raise ToolError("periodo_invalido")
    if not isinstance(limite, int) or isinstance(limite, bool) or not 1 <= limite <= p["max_itens"]:
        raise ToolError("limite_invalido")
    c = store.conta(ctx)
    corte = (dt.date.today() - dt.timedelta(days=dias)).isoformat()
    itens = [l for l in c.lancamentos if l["data"] >= corte][:limite]
    return {"conta_mascarada": c.conta_mascarada, "dias": dias, "lancamentos": itens}


def depositar(store: Store, ctx: Ctx, valor, idempotency_key: str, aprovado_humano: bool = False) -> dict:
    p = PERMISSOES["depositar"]
    v = parse_valor(valor)
    if not idempotency_key:
        raise ToolError("idempotencia_obrigatoria")
    if idempotency_key in store._idem:
        return store._idem[idempotency_key]
    if v > p["max_op"]:
        raise ToolError("acima_limite_operacao")
    if store.depositos_hoje(ctx) + v > p["max_dia_sem_humano"] and not aprovado_humano:
        raise ToolError("requer_humano")
    c = store.conta(ctx)
    c.saldo += v
    c.lancamentos.insert(0, {"data": dt.date.today().isoformat(), "descricao": "Depósito (Cofre)", "valor": v})
    store._registrar(ctx, "deposito", v, idempotency_key)
    r = {"conta_mascarada": c.conta_mascarada, "valor": v, "saldo": c.saldo}
    store._idem[idempotency_key] = r
    return r


def sacar(store: Store, ctx: Ctx, valor, idempotency_key: str, aprovado_humano: bool = False) -> dict:
    p = PERMISSOES["sacar"]
    v = parse_valor(valor)
    if not idempotency_key:
        raise ToolError("idempotencia_obrigatoria")
    if idempotency_key in store._idem:
        return store._idem[idempotency_key]
    if v > p["max_com_humano"]:
        raise ToolError("acima_limite_operacao")
    if v > p["max_sem_humano"] and not aprovado_humano:
        raise ToolError("requer_humano")
    c = store.conta(ctx)
    if v > c.saldo:
        raise ToolError("saldo_insuficiente")
    c.saldo -= v
    c.lancamentos.insert(0, {"data": dt.date.today().isoformat(), "descricao": "Saque (Cofre)", "valor": -v})
    store._registrar(ctx, "saque", v, idempotency_key)
    r = {"conta_mascarada": c.conta_mascarada, "valor": v, "saldo": c.saldo}
    store._idem[idempotency_key] = r
    return r


def sugerir_rendimento(horizonte_dias: int | None) -> str | None:
    """Sugestão por REGRA determinística (não por LLM)."""
    if horizonte_dias is None:
        return None
    if horizonte_dias < 30:
        return "CONTA_REMUNERADA"
    if horizonte_dias <= 180:
        return "CDB_DIARIO"
    return "TESOURO_SELIC"


def listar_rendimentos(store: Store, ctx: Ctx, horizonte_dias=None) -> dict:
    c = store.conta(ctx)
    return {"atual": c.rendimento, "catalogo": {k: dict(v) for k, v in CATALOGO.items()},
            "sugestao": sugerir_rendimento(horizonte_dias), "horizonte_dias": horizonte_dias}


def definir_rendimento(store: Store, ctx: Ctx, codigo: str) -> dict:
    if not isinstance(codigo, str) or codigo not in CATALOGO:
        raise ToolError("codigo_invalido")
    c = store.conta(ctx)
    hoje = dt.date.today()
    if c.rendimento_alterado_em == hoje:
        raise ToolError("limite_alteracoes_dia")
    c.rendimento = codigo
    c.rendimento_alterado_em = hoje
    store._registrar(ctx, "rendimento", Decimal("0"), f"rend-{ctx.customer_id}-{hoje}")
    return {"conta_mascarada": c.conta_mascarada, "rendimento": codigo, "saldo": c.saldo}


def simular_rendimento(store: Store, ctx: Ctx, valor, codigo: str, dias: int = 30) -> dict:
    store.conta(ctx)
    v = parse_valor(valor)
    if codigo not in CATALOGO:
        raise ToolError("codigo_invalido")
    if not isinstance(dias, int) or not 1 <= dias <= PERMISSOES["simular_rendimento"]["max_dias"]:
        raise ToolError("periodo_invalido")
    taxa_aa = CDI_AA * CATALOGO[codigo]["pct_cdi"]
    fator = (1 + float(taxa_aa)) ** (dias / 365) - 1
    ganho = (v * Decimal(str(fator))).quantize(Decimal("0.01"))
    return {"valor": v, "codigo": codigo, "dias": dias, "ganho_estimado": ganho, "cdi_aa": CDI_AA}


TOOLS = MappingProxyType({
    "consultar_saldo": consultar_saldo, "consultar_extrato": consultar_extrato, "depositar": depositar,
    "sacar": sacar, "listar_rendimentos": listar_rendimentos, "definir_rendimento": definir_rendimento,
    "simular_rendimento": simular_rendimento,
})
