#!/usr/bin/env python3
"""
Migração única: importa para o Odoo os deals do Pipedrive (pipeline 13 - Sétima)
que ainda NÃO existem no Odoo. Inclui histórico:
  - open  -> criado ativo na etapa mapeada
  - won   -> criado ativo na etapa Ganho
  - lost  -> criado ARQUIVADO (status perdido), PRESERVANDO a etapa de origem.
             NÃO usa a etapa "Perdido": o perdido é um STATUS, não uma etapa.

Dedup: por título (equipe Sétima, ativos+arquivados) para não duplicar os leads
que já vieram do Meta; e por "Pipedrive ID:" para poder re-rodar sem duplicar o
que esta migração já criou.

Preserva a data de entrada na etapa (date_last_stage_update = stage_change_time)
para não perder as métricas de conversão.

Rode primeiro com DRY_RUN=true para revisar.
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

STAGE_MAP = {
    "Lead":         "Lead",
    "MQL":          "MQL",
    "Discovery":    "Discovery",
    "SQL":          "SQL",
    "Oportunidade": "Oportunidade",
    "Negociação":   "Negociação",
}
ODOO_STAGE_GANHO_NAME    = "Ganho"   # etapa de ganho (is_won)
ODOO_STAGE_FALLBACK_NAME = "Lead"    # se um lost vier de etapa sem mapeamento


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
    """SÓ deals do pipeline Sétima (a API ignora o filtro pipeline_id)."""
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


def extract_person_info(deal: dict) -> tuple[str, str, str]:
    """Extrai nome, email e telefone do campo person_id do deal."""
    person = deal.get("person_id") or {}
    name = person.get("name", "") if isinstance(person, dict) else ""

    emails = person.get("email", []) if isinstance(person, dict) else []
    email = next((e["value"] for e in emails if e.get("primary")), "")
    if not email and emails:
        email = emails[0].get("value", "")

    phones = person.get("phone", []) if isinstance(person, dict) else []
    phone = next((p["value"] for p in phones if p.get("primary")), "")
    if not phone and phones:
        phone = phones[0].get("value", "")

    return name.strip(), email.strip(), phone.strip()


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


def get_existing_titles(models, uid) -> set[str]:
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "search_read",
        [[["team_id", "=", ODOO_TEAM_ID], ["active", "in", [True, False]]]],
        {"fields": ["name"], "limit": 0},
    )
    return {(l["name"] or "").strip().lower() for l in leads}


def get_existing_pd_ids(models, uid) -> set[str]:
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "search_read",
        [[["description", "like", "Pipedrive ID:"], ["active", "in", [True, False]]]],
        {"fields": ["description"], "limit": 0},
    )
    ids, pat = set(), re.compile(r"Pipedrive ID:\s*(\S+)")
    for l in leads:
        m = pat.search(l.get("description") or "")
        if m:
            ids.add(m.group(1))
    return ids


def create_or_find_contact(models, uid, name: str, email: str, phone: str) -> int:
    if email:
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "search_read",
            [[["email", "=", email]]],
            {"fields": ["id", "name"], "limit": 1},
        )
        if existing:
            return existing[0]["id"]

    contact_vals = {"name": name or "Contato Pipedrive"}
    if email:
        contact_vals["email"] = email
    if phone:
        contact_vals["phone"] = phone

    return models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "res.partner", "create",
        [contact_vals],
    )


# ---------------------------------------------------------------------------
# Resolução do destino
# ---------------------------------------------------------------------------

def resolve_target(deal, pd_stages, odoo_stages):
    """(odoo_stage_id, ativo, data). Retorna (None, ...) se open sem mapeamento."""
    status        = deal.get("status", "open")
    pd_stage_name = pd_stages.get(deal.get("stage_id"), "")
    mapped_name   = STAGE_MAP.get(pd_stage_name)
    mapped_id     = odoo_stages.get(mapped_name) if mapped_name else None
    fallback_id   = odoo_stages.get(ODOO_STAGE_FALLBACK_NAME)

    if status == "won":
        return odoo_stages.get(ODOO_STAGE_GANHO_NAME), True, (deal.get("won_time") or deal.get("stage_change_time"))
    if status == "lost":
        # Perdido como STATUS, preservando a etapa de origem (fallback = Lead).
        return (mapped_id or fallback_id), False, (deal.get("stage_change_time") or deal.get("lost_time"))
    # open
    return mapped_id, True, (deal.get("stage_change_time") or deal.get("add_time"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será criado ===")
    else:
        log.info("=== MODO REAL — leads serão criados no Odoo ===")

    log.info(f"Buscando stages do Pipedrive (pipeline {PIPEDRIVE_PIPELINE} - Sétima)...")
    pd_stages = get_pipedrive_stages()
    log.info(f"Stages: {list(pd_stages.values())}")

    log.info("Buscando deals do Pipedrive...")
    pd_deals = get_pipedrive_deals()
    log.info(f"{len(pd_deals)} deal(s) encontrado(s).")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    odoo_stages     = get_odoo_stages(models, uid)
    existing_titles = get_existing_titles(models, uid)
    existing_pd_ids = get_existing_pd_ids(models, uid)
    log.info(f"{len(existing_titles)} título(s) já no Odoo (equipe {ODOO_TEAM_ID}); "
             f"{len(existing_pd_ids)} já com Pipedrive ID.")

    criados = pulados = erros = 0
    por_status = {"open": 0, "won": 0, "lost": 0}

    for deal in pd_deals:
        pd_id  = str(deal.get("id", ""))
        title  = (deal.get("title") or "").strip()
        status = deal.get("status", "open")

        if pd_id in existing_pd_ids:
            pulados += 1
            continue
        if title and title.lower() in existing_titles:
            pulados += 1
            continue

        stage_id, ativo, stage_date = resolve_target(deal, pd_stages, odoo_stages)

        # Deal aberto cuja etapa não tem mapeamento: não sabemos onde colocar.
        if status == "open" and stage_id is None:
            pd_stage_name = pd_stages.get(deal.get("stage_id"), "")
            if pd_stage_name:
                log.warning(f"Stage '{pd_stage_name}' sem mapeamento — '{title}'")
            continue

        person_name, email, phone = extract_person_info(deal)
        nome_lead = title or person_name or f"Negócio Pipedrive {pd_id}"

        por_status[status] = por_status.get(status, 0) + 1
        log.info(f"[{status}] '{nome_lead}' (PD#{pd_id}) -> stage={stage_id} "
                 f"{'ativo' if ativo else 'ARQUIVADO(perdido)'} ({stage_date})")

        if DRY_RUN:
            criados += 1
            if title:
                existing_titles.add(title.lower())
            existing_pd_ids.add(pd_id)
            continue

        try:
            partner_id = create_or_find_contact(models, uid, person_name, email, phone)

            description = "\n".join([
                f"Pipedrive ID: {pd_id}",
                "Origem: Pipedrive (migração Sétima)",
                f"Data de entrada na etapa: {stage_date or ''}",
            ])

            vals = {
                "name": nome_lead,
                "partner_id": partner_id,
                "description": description,
                "type": "opportunity",
                "team_id": ODOO_TEAM_ID,
                "stage_id": stage_id,
                "active": ativo,
            }
            if stage_date:
                vals["date_last_stage_update"] = stage_date
            if status == "won":
                vals["probability"] = 100
            elif status == "lost":
                vals["probability"] = 0

            vals = {k: v for k, v in vals.items() if v not in (None, "")}

            lead_id = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY,
                "crm.lead", "create",
                [vals],
            )
            log.info(f"  Criado: Odoo#{lead_id}")
            criados += 1
            if title:
                existing_titles.add(title.lower())
            existing_pd_ids.add(pd_id)
        except Exception as exc:
            log.error(f"  Erro ao criar '{nome_lead}' (PD#{pd_id}): {exc}")
            erros += 1

    log.info("=" * 60)
    log.info(f"Sétima: {len(pd_deals)} | Pulados (já no Odoo): {pulados} | Por status: {por_status}")
    if DRY_RUN:
        log.info(f"Seriam criados: {criados} — rode com DRY_RUN=false para aplicar.")
    else:
        log.info(f"Criados: {criados} | Erros: {erros}")


if __name__ == "__main__":
    main()
