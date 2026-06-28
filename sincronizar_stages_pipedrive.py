#!/usr/bin/env python3
"""
Sincroniza o pipe BrandSpot do Pipedrive (pipeline 9) -> Odoo (equipe 17).

De-para POR ID (robusto a renomeações/typos dos dois lados):
  Pipedrive stage_id  ->  Odoo stage_id

Tratamento de status do deal no Pipedrive:
  - open  -> move para a etapa mapeada (ativo)
  - won   -> move para a etapa "Ganho" (ativo)
  - lost  -> marca como PERDIDO via status (active=False, probability=0),
             PRESERVANDO a etapa onde o lead estava quando foi perdido
             (assim dá para medir conversão/perda etapa a etapa).

Faz match do deal com o lead do Odoo pelo título (nome da empresa), entre os
leads da equipe BrandSpot importados do Meta. Leads de Landing Page ainda não
entram no Odoo (integração em standby), então não são casados aqui.
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
ODOO_TEAM_ID       = int(os.environ.get("ODOO_TEAM_ID", "17"))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# -------------------------------------------------------------------------
# De-para POR ID: Pipedrive stage_id (pipeline 9) -> Odoo crm.stage id
# -------------------------------------------------------------------------
PD_STAGE_TO_ODOO = {
    43: 68,   # Lead                -> Lead
    48: 51,   # MQL                 -> MQL
    49: 52,   # SQL (Call Agendada) -> SQL
    39: 63,   # Show  room          -> Visita Showroom
    75: 17,   # Oportuindade        -> Oportunidade
    40: 71,   # Negociação          -> Negociação
}

ODOO_STAGE_GANHO = 4   # etapa "Ganho" (is_won) para deals ganhos no Pipedrive


# ---------------------------------------------------------------------------
# Pipedrive
# ---------------------------------------------------------------------------

def pd_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params["api_token"] = PIPEDRIVE_TOKEN
    r = requests.get(f"{PIPEDRIVE_BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_pipedrive_deals() -> list[dict]:
    """Retorna SÓ os deals do pipeline BrandSpot.

    Atenção: o parâmetro `pipeline_id` em /v1/deals é ignorado pela API
    (devolve deals de todos os pipelines), então filtramos no cliente pelo
    campo `pipeline_id` de cada deal.
    """
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
    return [d for d in deals if d.get("pipeline_id") == PIPEDRIVE_PIPELINE]


# ---------------------------------------------------------------------------
# Odoo
# ---------------------------------------------------------------------------

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def get_odoo_leads(models, uid) -> dict[str, list[dict]]:
    """Leads BrandSpot (Meta + migrados do Pipedrive), agrupados por nome.

    Inclui tanto os vindos do Meta ("Lead ID (Meta):") quanto os migrados do
    Pipedrive ("Pipedrive ID:"), para que ambos sigam sincronizando por título.
    """
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[
            "|",
            ["description", "like", "Lead ID (Meta):"],
            ["description", "like", "Pipedrive ID:"],
            ["team_id", "=", ODOO_TEAM_ID],
            ["active", "in", [True, False]],
        ]],
        {"fields": ["id", "name", "stage_id", "active"], "limit": 0},
    )
    result: dict[str, list[dict]] = {}
    for l in leads:
        key = l["name"].strip().lower()
        result.setdefault(key, []).append(l)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_target(deal: dict) -> tuple[int | None, bool, str | None]:
    """Retorna (odoo_stage_id, ativo, data) para o deal.

    - odoo_stage_id None => não mexer na etapa (caso de lost sem mapeamento).
    - ativo False => marcar como perdido (status).
    """
    pd_status   = deal.get("status", "open")
    pd_stage_id = deal.get("stage_id")
    mapped      = PD_STAGE_TO_ODOO.get(pd_stage_id)

    if pd_status == "won":
        return ODOO_STAGE_GANHO, True, (deal.get("won_time") or deal.get("stage_change_time"))

    if pd_status == "lost":
        # Perdido como STATUS: arquiva e preserva a etapa de origem (se mapeada).
        return mapped, False, (deal.get("lost_time") or deal.get("stage_change_time"))

    # open
    return mapped, True, (deal.get("stage_change_time") or deal.get("add_time"))


def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será alterado ===")
    else:
        log.info("=== MODO REAL — leads serão atualizados no Odoo ===")

    log.info(f"Buscando deals do Pipedrive (pipeline {PIPEDRIVE_PIPELINE} - BrandSpot)...")
    pd_deals = get_pipedrive_deals()
    log.info(f"{len(pd_deals)} deal(s) encontrado(s).")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()
    odoo_leads = get_odoo_leads(models, uid)
    log.info(f"{len(odoo_leads)} oportunidade(s) BrandSpot (Meta) no Odoo.")

    matched = sem_lead = sem_stage = updated = errors = 0

    for deal in pd_deals:
        title = (deal.get("title") or "").strip()
        pd_status = deal.get("status", "open")
        odoo_stage_id, ativo, stage_date = resolve_target(deal)

        # Deal aberto cuja etapa não está mapeada: não sabemos para onde mover.
        if pd_status == "open" and odoo_stage_id is None:
            log.warning(f"Stage Pipedrive {deal.get('stage_id')} sem mapeamento — '{title}'")
            sem_stage += 1
            continue

        odoo_lead_list = odoo_leads.get(title.lower())
        if not odoo_lead_list:
            sem_lead += 1
            continue

        matched += len(odoo_lead_list)
        for odoo_lead in odoo_lead_list:
            atual = odoo_lead["stage_id"][1] if odoo_lead["stage_id"] else "?"
            destino = odoo_stage_id if odoo_stage_id is not None else atual
            estado = "PERDIDO(arquiva)" if not ativo else "ativo"
            log.info(f"'{title}' (#{odoo_lead['id']}) | {atual} -> stage={destino} [{estado}] ({stage_date})")

            if DRY_RUN:
                updated += 1
                continue

            try:
                write_vals: dict = {"active": ativo}
                if odoo_stage_id is not None:
                    write_vals["stage_id"] = odoo_stage_id
                if stage_date:
                    write_vals["date_last_stage_update"] = stage_date
                if pd_status == "lost":
                    write_vals["probability"] = 0
                elif pd_status == "won":
                    write_vals["probability"] = 100

                models.execute_kw(
                    ODOO_DB, uid, ODOO_API_KEY,
                    "crm.lead", "write",
                    [[odoo_lead["id"]], write_vals],
                )
                log.info(f"  Atualizado: Odoo#{odoo_lead['id']}")
                updated += 1
            except Exception as exc:
                log.error(f"  Erro ao atualizar '{title}' #{odoo_lead['id']}: {exc}")
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
