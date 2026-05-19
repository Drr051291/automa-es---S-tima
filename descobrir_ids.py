#!/usr/bin/env python3
"""
Roda este script uma vez para descobrir os IDs de pipelines e estágios do Odoo.
Execute: python3 descobrir_ids.py
"""

import os
import xmlrpc.client
from dotenv import load_dotenv

load_dotenv()

ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

print(f"\nConectando em {ODOO_URL}...")
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
if not uid:
    print("❌ Autenticação falhou. Verifique as credenciais no .env")
    exit(1)

print(f"✅ Conectado! (uid={uid})\n")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Pipelines
print("=" * 50)
print("PIPELINES (ODOO_TEAM_ID)")
print("=" * 50)
pipelines = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.team", "search_read", [[]], {"fields": ["id", "name"]})
for p in pipelines:
    print(f"  ID={p['id']:>4}  →  {p['name']}")

# Estágios
print("\n" + "=" * 50)
print("ESTÁGIOS (ODOO_STAGE_ID)")
print("=" * 50)
stages = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.stage", "search_read", [[]], {"fields": ["id", "name", "team_id"], "order": "sequence"})
for s in stages:
    team = s["team_id"][1] if s["team_id"] else "todos os pipelines"
    print(f"  ID={s['id']:>4}  →  {s['name']}  [{team}]")

print("\nCopie os IDs desejados para o seu arquivo .env:")
print("  ODOO_TEAM_ID=<id do pipeline>")
print("  ODOO_STAGE_ID=<id do estágio inicial>\n")
