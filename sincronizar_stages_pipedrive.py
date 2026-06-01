#!/usr/bin/env python3
"""
Migração única: sincroniza stages do Pipedrive → Odoo.
Lê a stage atual de cada deal no Pipedrive + data de entrada,
faz match pelo nome da empresa (título do negócio) e atualiza o Odoo.
"""

import logging
import os
import xmlrpc.client

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

ODOO_URL        = os.environ["ODOO_URL"]
ODOO_DB         = os.environ["ODOO_DB"]
ODOO_USER       = os.environ["ODOO_USER"]
ODOO_API_KEY    = os.environ["ODOO_API_KEY"]
PIPEDRIVE_TOKEN    = os.environ["PIPEDRIVE_TOKEN"]
PIPEDRIVE_BASE     = "https://api.pipedrive.com/v1"
PIPEDRIVE_PIPELINE = int(os.environ.get("PIPEDRIVE_PIPELINE_ID", "9"))

# Mapeamento: nome da stage no Pipedrive → nome exato no Odoo
# Deals ganhos/perdidos no Pipedrive não entram no kanban e são ignorados.
STAGE_MAP = {
    "Lead":              "ID Oportunidade",
    "MQL":               "MQL",
    "SQL (Call Agendada)": "SQL",
    "Show  room":        "Visita Showroom",
    "Oportunidade":      "Oportunidade",
    "Negociação":        "Negociação",
}

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"


# ---------------------------------------------------------------------------
# Pipedrive
# ---------------------------------------------------------------------------

def pd_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params["api_token"] = PIPEDRIVE_TOKEN
    r = requests.get(f"{PIPEDRIVE_BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_pipedrive_stages() -> dict[int, str]:
    data = pd_get("stages", {"pipeline_id": PIPEDRIVE_PIPELINE})
    return {s["id"]: s["name"] for s in (data.get("data") or [])}


def get_pipedrive_deals() -> list[dict]:
    deals, start = [], 0
    while True:
        data = pd_get("deals", {
            "pipeline_id": PIPEDRIVE_PIPELINE,
            "status": "all_not_deleted",
            "limit": 500,
            "start": start,
        })
        items = data.get("data") or []
        deals.extend(items)
        pagination = (data.get("additional_data") or {}).get("pagination", {})
        if not pagination.get("more_items_in_collection"):
            break
        start = pagination["next_start"]
    return deals


# ---------------------------------------------------------------------------
# Odoo
# ---------------------------------------------------------------------------

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def get_odoo_stages(models, uid) -> dict[str, int]:
    stages = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.stage", "search_read",
        [[]],
        {"fields": ["id", "name"]},
    )
    return {s["name"]: s["id"] for s in stages}


def get_odoo_leads(models, uid) -> dict[str, dict]:
    # active=False inclui arquivados (perdidos); active em qualquer valor = ambos
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[["description", "like", "Lead ID (Meta):"], ["active", "in", [True, False]]]],
        {"fields": ["id", "name", "stage_id", "active"], "limit": 0},
    )
    return {l["name"].strip().lower(): l for l in leads}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será alterado ===")
    else:
        log.info("=== MODO REAL — stages serão atualizados no Odoo ===")

    log.info("Buscando stages do Pipedrive...")
    pd_stages = get_pipedrive_stages()
    log.info(f"{len(pd_stages)} stage(s): {list(pd_stages.values())}")

    log.info("Buscando deals do Pipedrive...")
    pd_deals = get_pipedrive_deals()
    log.info(f"{len(pd_deals)} deal(s) encontrado(s).")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    odoo_stages = get_odoo_stages(models, uid)
    log.info(f"Stages Odoo: {list(odoo_stages.keys())}")

    odoo_leads = get_odoo_leads(models, uid)
    log.info(f"{len(odoo_leads)} oportunidade(s) Meta Ads no Odoo.")

    matched = 0
    sem_stage = 0
    sem_lead = 0
    updated = 0
    errors = 0

    for deal in pd_deals:
        title       = (deal.get("title") or "").strip()
        pd_status   = deal.get("status", "open")   # open | won | lost
        pd_stage_id = deal.get("stage_id")
        pd_stage_name = pd_stages.get(pd_stage_id, "")

        # Determina stage destino e data conforme status do deal no Pipedrive
        if pd_status == "won":
            odoo_stage_name = "Ganho"
            stage_date = deal.get("won_time") or deal.get("stage_change_time")
            is_lost = False
        elif pd_status == "lost":
            odoo_stage_name = "Perdido (arquivado)"   # apenas para o log
            stage_date = deal.get("lost_time") or deal.get("stage_change_time")
            is_lost = True
        else:
            odoo_stage_name = STAGE_MAP.get(pd_stage_name)
            stage_date = deal.get("stage_change_time") or deal.get("add_time")
            is_lost = False
            if not odoo_stage_name:
                if pd_stage_name:
                    log.warning(f"Stage '{pd_stage_name}' sem mapeamento — '{title}'")
                sem_stage += 1
                continue

        odoo_lead = odoo_leads.get(title.lower())
        if not odoo_lead:
            sem_lead += 1
            continue

        if not is_lost:
            odoo_stage_id = odoo_stages.get(odoo_stage_name)
            if not odoo_stage_id:
                log.warning(f"Stage '{odoo_stage_name}' não encontrada no Odoo — '{title}'")
                sem_stage += 1
                continue

        matched += 1
        current = odoo_lead["stage_id"][1] if odoo_lead["stage_id"] else "?"
        log.info(f"'{title}' | {current} → {odoo_stage_name} ({stage_date})")

        if DRY_RUN:
            updated += 1
            continue

        try:
            if is_lost:
                write_vals = {"active": False}
                if stage_date:
                    write_vals["date_last_stage_update"] = stage_date
            else:
                write_vals = {"stage_id": odoo_stage_id}
                if stage_date:
                    write_vals["date_last_stage_update"] = stage_date

            models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY,
                "crm.lead", "write",
                [[odoo_lead["id"]], write_vals],
            )
            log.info(f"  Atualizado: Odoo#{odoo_lead['id']}")
            updated += 1
        except Exception as exc:
            log.error(f"  Erro ao atualizar '{title}': {exc}")
            errors += 1

    log.info("=" * 60)
    log.info(f"Total Pipedrive: {len(pd_deals)} | Matched: {matched} | "
             f"Stage não mapeada: {sem_stage} | Lead não encontrado: {sem_lead}")
    if DRY_RUN:
        log.info(f"Seriam atualizados: {updated} — rode com DRY_RUN=false para aplicar.")
    else:
        log.info(f"Atualizados: {updated} | Erros: {errors}")


if __name__ == "__main__":
    main()
