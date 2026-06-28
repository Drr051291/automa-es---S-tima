#!/usr/bin/env python3
"""
Diagnóstico SOMENTE-LEITURA do pipe BrandSpot.
Lista as etapas atuais nos dois CRMs (Pipedrive pipeline 9 e Odoo) via API.
Não cria, não altera e não apaga nada.

Execute via workflow ou local: python3 descobrir_etapas_brandspot.py
"""

import os
import xmlrpc.client

import requests
from dotenv import load_dotenv

load_dotenv()

ODOO_URL     = os.environ["ODOO_URL"]
ODOO_DB      = os.environ["ODOO_DB"]
ODOO_USER    = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

PIPEDRIVE_TOKEN    = os.environ["PIPEDRIVE_TOKEN"]
PIPEDRIVE_BASE     = "https://api.pipedrive.com/v1"
PIPEDRIVE_PIPELINE = int(os.environ.get("PIPEDRIVE_PIPELINE_ID", "9"))


def pd_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params["api_token"] = PIPEDRIVE_TOKEN
    r = requests.get(f"{PIPEDRIVE_BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


print("\n" + "=" * 60)
print(f"PIPEDRIVE — TODOS OS PIPELINES")
print("=" * 60)
pipelines = pd_get("pipelines").get("data") or []
for p in pipelines:
    marca = "  <== BrandSpot (em uso)" if p["id"] == PIPEDRIVE_PIPELINE else ""
    print(f"  ID={p['id']:>4}  ->  {p['name']}{marca}")

print("\n" + "=" * 60)
print(f"PIPEDRIVE — ETAPAS do pipeline {PIPEDRIVE_PIPELINE} (BrandSpot)")
print("=" * 60)
stages = pd_get("stages", {"pipeline_id": PIPEDRIVE_PIPELINE}).get("data") or []
for s in sorted(stages, key=lambda x: x.get("order_nr", 0)):
    print(f"  ordem={s.get('order_nr'):>2}  ID={s['id']:>4}  ->  [{s['name']}]")

print("\n" + "=" * 60)
print("ODOO — CONEXÃO")
print("=" * 60)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
if not uid:
    print("Autenticacao falhou.")
    raise SystemExit(1)
print(f"  Conectado (uid={uid})")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

print("\n" + "=" * 60)
print("ODOO — EQUIPES / PIPELINES (crm.team)")
print("=" * 60)
teams = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.team", "search_read", [[]], {"fields": ["id", "name"]})
for t in teams:
    print(f"  ID={t['id']:>4}  ->  {t['name']}")

print("\n" + "=" * 60)
print("ODOO — ETAPAS (crm.stage) — globais, ordenadas por sequence")
print("=" * 60)
ostages = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.stage", "search_read", [[]],
    {"fields": ["id", "name", "sequence"], "order": "sequence"})
for s in ostages:
    print(f"  seq={s.get('sequence'):>3}  ID={s['id']:>4}  ->  [{s['name']}]")

# Contagem de oportunidades por etapa na equipe BrandSpot (id 17) — só leitura
print("\n" + "=" * 60)
print("ODOO — Oportunidades por etapa na equipe 17 (BrandSpot)")
print("=" * 60)
try:
    grouped = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "read_group",
        [[["team_id", "=", 17], ["active", "in", [True, False]]]],
        {"fields": ["stage_id"], "groupby": ["stage_id"], "lazy": False})
    for g in grouped:
        st = g["stage_id"][1] if g.get("stage_id") else "(sem etapa)"
        print(f"  {g['__count']:>5}  ->  {st}")
except Exception as exc:
    print(f"  (nao foi possivel agrupar: {exc})")

print("\nFIM DO DIAGNOSTICO (nenhum dado foi alterado).\n")
