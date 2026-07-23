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
    """Leads BrandSpot (Meta + migrados do Pipedrive) indexados por
    título, e-mail e telefone — para casar mesmo quando o título diverge.

    Inclui tanto os vindos do Meta ("Lead ID (Meta):") quanto os migrados do
    Pipedrive ("Pipedrive ID:").
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
        {"fields": ["id", "name", "stage_id", "active", "email_from", "phone", "partner_id"], "limit": 0},
    )

    # Leads do Meta guardam e-mail/telefone no CONTATO (res.partner), não no
    # próprio lead. Sem ler o contato, o casamento por e-mail/telefone falha e
    # o lead nunca é movido/arquivado. Buscamos os contatos em lote e usamos
    # como reforço quando o lead não tem e-mail/telefone preenchido.
    partner_ids = list({l["partner_id"][0] for l in leads if l.get("partner_id")})
    partner_by_id: dict[int, dict] = {}
    for i in range(0, len(partner_ids), 500):
        for p in models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY, "res.partner", "read",
            [partner_ids[i:i + 500]], {"fields": ["email", "phone", "mobile"]},
        ):
            partner_by_id[p["id"]] = p

    by_title: dict[str, list[dict]] = {}
    by_email: dict[str, list[dict]] = {}
    by_phone: dict[str, list[dict]] = {}
    for l in leads:
        partner = partner_by_id.get(l["partner_id"][0]) if l.get("partner_id") else {}
        t = (l.get("name") or "").strip().lower()
        if t:
            by_title.setdefault(t, []).append(l)
        e = ((l.get("email_from") or "").strip().lower()
             or (partner.get("email") or "").strip().lower())
        if e:
            by_email.setdefault(e, []).append(l)
        phones = {
            _norm_phone(x)
            for x in (l.get("phone"), partner.get("phone"), partner.get("mobile"))
            if _norm_phone(x)
        }
        for ph in phones:
            by_phone.setdefault(ph, []).append(l)
    return by_title, by_email, by_phone


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
    by_title, by_email, by_phone = get_odoo_lead_indexes(models, uid)
    log.info("Índices Odoo: "
             f"{len(by_title)} título(s), {len(by_email)} e-mail(s), {len(by_phone)} telefone(s).")

    matched = sem_lead = sem_stage = updated = errors = 0
    match_por = {"titulo": 0, "email": 0, "telefone": 0}

    for deal in pd_deals:
        title = (deal.get("title") or "").strip()
        pd_status = deal.get("status", "open")
        odoo_stage_id, ativo, stage_date = resolve_target(deal)

        # Deal aberto cuja etapa não está mapeada: não sabemos para onde mover.
        if pd_status == "open" and odoo_stage_id is None:
            log.warning(f"Stage Pipedrive {deal.get('stage_id')} sem mapeamento — '{title}'")
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
            estado = "PERDIDO(arquiva)" if not ativo else "ativo"
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
    log.info(f"Casamentos por: {match_por}")
    if DRY_RUN:
        log.info(f"Seriam atualizados: {updated} — rode com DRY_RUN=false para aplicar.")
    else:
        log.info(f"Atualizados: {updated} | Erros: {errors}")


if __name__ == "__main__":
    main()
