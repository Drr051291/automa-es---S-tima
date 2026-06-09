#!/usr/bin/env python3
"""
Descobre IDs e hashes de propriedades do pipeline Sétima no Odoo.
Execute: python3 descobrir_propriedades_setima.py
"""

import os
import xmlrpc.client
from dotenv import load_dotenv

load_dotenv()

ODOO_URL     = os.environ["ODOO_URL"]
ODOO_DB      = os.environ["ODOO_DB"]
ODOO_USER    = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

print(f"\nConectando em {ODOO_URL}...")
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
if not uid:
    print("Autenticação falhou.")
    exit(1)

print(f"Conectado! (uid={uid})\n")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Pipelines
print("=" * 50)
print("TODOS OS PIPELINES")
print("=" * 50)
pipelines = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.team", "search_read", [[]], {"fields": ["id", "name"]})
for p in pipelines:
    print(f"  ID={p['id']:>4}  →  {p['name']}")

# Estágios (Odoo 19 não tem team_id em crm.stage)
print("\n" + "=" * 50)
print("ESTÁGIOS")
print("=" * 50)
stages = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.stage", "search_read", [[]], {"fields": ["id", "name"], "order": "sequence"})
for s in stages:
    print(f"  ID={s['id']:>4}  →  {s['name']}")

# Busca um lead existente no pipeline Inbound Sétima (ID=16) para extrair hashes
print("\n" + "=" * 50)
print("HASHES DAS PROPRIEDADES (pipeline Inbound Sétima)")
print("=" * 50)
leads = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.lead", "search_read",
    [[["team_id", "=", 16]]],
    {"fields": ["name", "lead_properties"], "limit": 5},
)

found = False
for lead in leads:
    props = lead.get("lead_properties") or []
    if props:
        print(f"\nLead: {lead['name']}")
        print("\nLEAD_PROPERTIES_MAP = {")
        for p in props:
            print(f'    "{p["name"]}": "<coluna_no_sheets>",  # {p.get("string", p["name"])}')
        print("}")
        found = True
        break

if not found:
    print("Nenhum lead com properties encontrado no pipeline Sétima.")
    print("Crie um lead de teste com as properties preenchidas e rode novamente.")
