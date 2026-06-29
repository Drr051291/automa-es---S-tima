#!/usr/bin/env python3
"""
Diagnóstico SOMENTE-LEITURA do pipe BrandSpot.
Lista as etapas atuais nos dois CRMs (Pipedrive pipeline 9 e Odoo) via API.
Não cria, não altera e não apaga nada.

Execute via workflow ou local: python3 descobrir_etapas_brandspot.py
(re-checagem pos-apply da sincronizacao - rev3)
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
print("PIPEDRIVE — TODAS as etapas (todos os pipelines)")
print("=" * 60)
all_stages = pd_get("stages").get("data") or []
stage_name_by_id = {s["id"]: s["name"] for s in all_stages}
for s in sorted(all_stages, key=lambda x: (x.get("pipeline_id", 0), x.get("order_nr", 0))):
    print(f"  pipeline={s.get('pipeline_id'):>3}  ID={s['id']:>4}  ->  [{s['name']}]")

print("\n" + "=" * 60)
print(f"PIPEDRIVE — Contagem de negócios por etapa/status (pipeline {PIPEDRIVE_PIPELINE})")
print("=" * 60)
deals, start = [], 0
while True:
    data = pd_get("deals", {"pipeline_id": PIPEDRIVE_PIPELINE,
                            "status": "all_not_deleted", "limit": 500, "start": start})
    items = data.get("data") or []
    deals.extend(items)
    pag = (data.get("additional_data") or {}).get("pagination", {})
    if not pag.get("more_items_in_collection"):
        break
    start = pag["next_start"]
# filtra SÓ o pipeline BrandSpot (a API ignora o pipeline_id em /deals)
deals = [d for d in deals if d.get("pipeline_id") == PIPEDRIVE_PIPELINE]
counts: dict = {}
for d in deals:
    key = (d.get("stage_id"), d.get("status"))
    counts[key] = counts.get(key, 0) + 1
print(f"  ({len(deals)} negócios no pipeline {PIPEDRIVE_PIPELINE})")
for (sid, status), n in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {n:>5}  stage_id={sid} [{stage_name_by_id.get(sid, '???')}]  status={status}")

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
print("ODOO — campos disponiveis em crm.stage")
print("=" * 60)
fields = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.stage", "fields_get", [], {"attributes": ["string"]})
tem_team = "team_id" in fields
print(f"  crm.stage tem campo team_id? {tem_team}")

print("\n" + "=" * 60)
print("ODOO — ETAPAS (crm.stage), ordenadas por sequence")
print("=" * 60)
read_fields = ["id", "name", "sequence", "is_won", "fold"]
if tem_team:
    read_fields.append("team_id")
ostages = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
    "crm.stage", "search_read", [[]],
    {"fields": read_fields, "order": "sequence"})
for s in ostages:
    team = ""
    if tem_team:
        team = f"  team={s['team_id'][1] if s.get('team_id') else 'GLOBAL (todos os funis)'}"
    won = "  [GANHO]" if s.get("is_won") else ""
    fold = "  (dobrada)" if s.get("fold") else ""
    print(f"  seq={s.get('sequence'):>3}  ID={s['id']:>4}  ->  [{s['name']}]{won}{fold}{team}")

# Quantos leads (todos os times) existem em cada etapa — para saber se etapas
# 'extras' do BrandSpot sao usadas por outros funis antes de cogitar exclusao.
print("\n" + "=" * 60)
print("ODOO — Total de leads POR ETAPA e POR EQUIPE (todos os funis)")
print("=" * 60)
try:
    grp = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "read_group",
        [[["active", "in", [True, False]]]],
        {"fields": ["stage_id", "team_id"],
         "groupby": ["stage_id", "team_id"], "lazy": False})
    for g in grp:
        st = g["stage_id"][1] if g.get("stage_id") else "(sem etapa)"
        tm = g["team_id"][1] if g.get("team_id") else "(sem equipe)"
        print(f"  {g['__count']:>5}  ->  etapa [{st}]  |  equipe [{tm}]")
except Exception as exc:
    print(f"  (nao foi possivel agrupar: {exc})")

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
