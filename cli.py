"""Chat de terminal. Uso: py cli.py --customer c-001   (comandos: /operador, /aprovar AP-0001, /rejeitar AP-0001, /sair)"""
import argparse
import os
import uuid

from cofre.agent import AnthropicLLM, Cofre, FakeLLM

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ap = argparse.ArgumentParser()
ap.add_argument("--customer", default="c-001", help="vem do canal autenticado (simulado)")
ap.add_argument("--llm", default=os.getenv("COFRE_LLM", "fake"), choices=["fake", "anthropic"])
a = ap.parse_args()

if a.llm == "anthropic" and not os.getenv("ANTHROPIC_API_KEY", "").strip():
    raise SystemExit("ANTHROPIC_API_KEY vazia: preencha a chave no arquivo .env (na pasta do projeto), salve e rode de novo.")
llm = AnthropicLLM() if a.llm == "anthropic" else FakeLLM()
c = Cofre(llm=llm, canal_2fa=lambda conta, cod: print(f"   [SMS simulado para {conta}] código 2FA: {cod}"), audit_path="audit.jsonl")
s = c.nova_sessao(a.customer, str(uuid.uuid4()))
print(f"Cofre ({llm.nome}) — cliente {s.conta_mascarada}. Digite /sair para encerrar.")
while True:
    t = input("\nVocê: ").strip()
    if t == "/sair":
        break
    if t == "/operador":
        print([(p["id"], p["name"], p["args"]) for p in c.approvals.pendentes()] or "Fila vazia")
        continue
    if t.startswith("/aprovar "):
        print("Executado:", c.approvals.approve(t.split()[1]))
        continue
    if t.startswith("/rejeitar "):
        c.approvals.reject(t.split()[1]); print("Rejeitado.")
        continue
    print("Cofre:", c.run_turn(s, t))
