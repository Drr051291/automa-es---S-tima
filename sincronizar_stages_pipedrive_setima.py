#!/usr/bin/env python3
"""
Sincroniza o pipe Sétima do Pipedrive (pipeline 13) -> Odoo (equipe 16).

Tratamento de status do deal no Pipedrive:
  - open  -> move para a etapa mapeada (ativo)
  - won   -> move para a etapa "Ganho" (ativo)
  - lost  -> marca como PERDIDO via status (active=False, probability=0),
             PRESERVANDO a etapa onde o lead estava quando foi perdido.
             NÃO usa a etapa "Perdido": o perdido é um STATUS, não uma etapa,
             para não descaracterizar a métrica de conversão etapa a etapa.

Preserva a data de entrada na etapa (date_last_stage_update = stage_change_time
do Pipedrive) para não perder as métricas de conversão.

Match do deal com o lead do Odoo por título -> e-mail -> telefone, entre os
leads da equipe Sétima (tanto os vindos do Meta quanto os migrados do Pipedrive).
"""

import logging
import os
import re
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
PIPEDRIVE_PIPELINE = int(os.environ.get("PIPEDRIVE_PIPELINE_ID_SETIMA", "13"))
ODOO_TEAM_ID       = int(os.environ.get("ODOO_TEAM_ID_SETIMA", "16"))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# De-para por NOME: stage do Pipedrive (pipeline 13) -> nome exato da etapa no Odoo.
# Os nomes no Pipedrive vêm com espaços/plural ("Leads", "SQL ", "Oportunidade "),
# então o de-para é consultado com o nome normalizado (strip).
STAGE_MAP = {
    "Lead":         "Lead",
    "Leads":        "Lead",
    "MQL":          "MQL",
    "Discovery":    "Discovery",
    "SQL":          "SQL",
    "Oportunidade": "Oportunidade",
    "Negociação":   "Negociação",
}
ODOO_STAGE_GANHO_NAME = "Ganho"   # etapa de ganho (is_won) para deals ganhos


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
    """SÓ deals do pipeline Sétima.

    O parâmetro `pipeline_id` em /v1/deals é ignorado pela API (devolve deals de
    todos os pipelines), então filtramos no cliente pelo `pipeline_id` do deal.
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


def get_odoo_stages(models, uid) -> dict[str, int]:
    stages = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.stage", "search_read",
        [[]],
        {"fields": ["id", "name"]},
    )
    return {s["name"]: s["id"] for s in stages}


def _norm_phone(p: str) -> str:
    """Só dígitos; usa os últimos 9 (ignora DDI/zeros à esquerda)."""
    digits = re.sub(r"\D", "", p or "")
    return digits[-9:] if len(digits) >= 9 else digits


def extract_person_info(deal: dict) -> tuple[str, str]:
    """Email e telefone da pessoa ligada ao deal."""
    person = deal.get("person_id") or {}
    emails = person.get("email", []) if isinstance(person, dict) else []
    email = next((e["value"] for e in emails if e.get("primary")), "")
    if not email and emails:
        email = emails[0].get("value", "")
    phones = person.get("phone", []) if isinstance(person, dict) else []
    phone = next((p["value"] for p in phones if p.get("primary")), "")
    if not phone and phones:
        phone = phones[0].get("value", "")
    return (email or "").strip().lower(), (phone or "").strip()


def get_odoo_lead_indexes(models, uid) -> tuple[dict, dict, dict]:
    """Leads Sétima (Meta + migrados do Pipedrive) indexados por título,
    e-mail e telefone — para casar mesmo quando o título diverge."""
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
        {"fields": ["id", "name", "stage_id", "active", "email_from", "phone"], "limit": 0},
    )
    by_title: dict[str, list[dict]] = {}
    by_email: dict[str, list[dict]] = {}
    by_phone: dict[str, list[dict]] = {}
    for l in leads:
        t = (l.get("name") or "").strip().lower()
        if t:
            by_title.setdefault(t, []).append(l)
        e = (l.get("email_from") or "").strip().lower()
        if e:
            by_email.setdefault(e, []).append(l)
        ph = _norm_phone(l.get("phone"))
        if ph:
            by_phone.setdefault(ph, []).append(l)
    return by_title, by_email, by_phone


# ---------------------------------------------------------------------------
# Resolução do destino
# ---------------------------------------------------------------------------

def resolve_target(deal, pd_stages, odoo_stages, ganho_id):
    """Retorna (odoo_stage_id, ativo, data) para o deal.

    - odoo_stage_id None => não mexer na etapa (lost cuja etapa não tem mapeamento).
    - ativo False => marcar como perdido (status), preservando a etapa.
    """
    status        = deal.get("status", "open")
    pd_stage_name = pd_stages.get(deal.get("stage_id"), "")
    mapped_name   = STAGE_MAP.get(pd_stage_name.strip())
    mapped_id     = odoo_stages.get(mapped_name) if mapped_name else None

    if status == "won":
        return ganho_id, True, (deal.get("won_time") or deal.get("stage_change_time"))
    if status == "lost":
        # Perdido como STATUS: arquiva e preserva a etapa de origem (se mapeada).
        return mapped_id, False, (deal.get("stage_change_time") or deal.get("lost_time"))
    # open
    return mapped_id, True, (deal.get("stage_change_time") or deal.get("add_time"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será alterado ===")
    else:
        log.info("=== MODO REAL — leads serão atualizados no Odoo ===")

    log.info(f"Buscando stages do Pipedrive (pipeline {PIPEDRIVE_PIPELINE} - Sétima)...")
    pd_stages = get_pipedrive_stages()
    log.info(f"{len(pd_stages)} stage(s): {list(pd_stages.values())}")

    log.info("Buscando deals do Pipedrive...")
    pd_deals = get_pipedrive_deals()
    log.info(f"{len(pd_deals)} deal(s) encontrado(s).")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()
    odoo_stages = get_odoo_stages(models, uid)
    ganho_id = odoo_stages.get(ODOO_STAGE_GANHO_NAME)
    if not ganho_id:
        log.warning(f"Etapa '{ODOO_STAGE_GANHO_NAME}' não encontrada no Odoo — deals ganhos ficarão na etapa atual.")

    by_title, by_email, by_phone = get_odoo_lead_indexes(models, uid)
    log.info("Índices Odoo: "
             f"{len(by_title)} título(s), {len(by_email)} e-mail(s), {len(by_phone)} telefone(s).")

    matched = sem_lead = sem_stage = updated = errors = 0
    match_por = {"titulo": 0, "email": 0, "telefone": 0}

    for deal in pd_deals:
        title  = (deal.get("title") or "").strip()
        status = deal.get("status", "open")
        odoo_stage_id, ativo, stage_date = resolve_target(deal, pd_stages, odoo_stages, ganho_id)

        # Deal aberto cuja etapa não está mapeada: não sabemos para onde mover.
        if status == "open" and odoo_stage_id is None:
            log.warning(f"Stage Pipedrive '{pd_stages.get(deal.get('stage_id'), '?')}' sem mapeamento — '{title}'")
            sem_stage += 1
            continue

        # Casa por título; se não achar, tenta por e-mail; depois por telefone.
        email, phone = extract_person_info(deal)
        odoo_lead_list, via = [], None
        if title and by_title.get(title.lower()):
            odoo_lead_list, via = by_title[title.lower()], "titulo"
        elif email and by_email.get(email):
            odoo_lead_list, via = by_email[email], "email"
        elif _norm_phone(phone) and by_phone.get(_norm_phone(phone)):
            odoo_lead_list, via = by_phone[_norm_phone(phone)], "telefone"

        if not odoo_lead_list:
            sem_lead += 1
            continue

        match_por[via] += 1
        matched += len(odoo_lead_list)
        for odoo_lead in odoo_lead_list:
            atual = odoo_lead["stage_id"][1] if odoo_lead["stage_id"] else "?"
            destino = odoo_stage_id if odoo_stage_id is not None else atual
            estado = "PERDIDO(status)" if not ativo else "ativo"
            log.info(f"'{title}' (#{odoo_lead['id']}, via {via}) | {atual} -> stage={destino} [{estado}] ({stage_date})")

            if DRY_RUN:
                updated += 1
                continue

            try:
                write_vals: dict = {"active": ativo}
                if odoo_stage_id is not None:
                    write_vals["stage_id"] = odoo_stage_id
                if stage_date:
                    write_vals["date_last_stage_update"] = stage_date
                if status == "lost":
                    write_vals["probability"] = 0
                elif status == "won":
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
    log.info(f"Casamentos por: {match_por}")
    if DRY_RUN:
        log.info(f"Seriam atualizados: {updated} — rode com DRY_RUN=false para aplicar.")
    else:
        log.info(f"Atualizados: {updated} | Erros: {errors}")


if __name__ == "__main__":
    main()
