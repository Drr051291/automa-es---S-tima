#!/usr/bin/env python3
"""
Migração única: importa deals do Pipedrive (pipeline 13 - Sétima) que ainda não
existem no Odoo. Usa o título do deal para verificar duplicatas.
Deals já importados via Meta Ads (Sheets) são ignorados automaticamente.
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


def get_existing_odoo_titles(models, uid) -> set[str]:
    """Retorna títulos (lowercase) de todos os leads já existentes no Inbound Sétima."""
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[["team_id", "=", ODOO_TEAM_ID], ["active", "in", [True, False]]]],
        {"fields": ["name"], "limit": 0},
    )
    return {l["name"].strip().lower() for l in leads}


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
# Main
# ---------------------------------------------------------------------------

def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será criado ===")
    else:
        log.info("=== MODO REAL — leads serão criados no Odoo ===")

    log.info("Buscando stages do Pipedrive (pipeline 13)...")
    pd_stages = get_pipedrive_stages()
    log.info(f"Stages: {list(pd_stages.values())}")

    log.info("Buscando deals do Pipedrive...")
    pd_deals = get_pipedrive_deals()
    log.info(f"{len(pd_deals)} deal(s) encontrado(s).")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    odoo_stages        = get_odoo_stages(models, uid)
    existing_titles    = get_existing_odoo_titles(models, uid)
    log.info(f"{len(existing_titles)} lead(s) já existentes no Odoo (Inbound Sétima).")

    to_import = [d for d in pd_deals if (d.get("title") or "").strip().lower() not in existing_titles]
    log.info(f"{len(to_import)} deal(s) do Pipedrive para importar.")

    imported = 0
    errors = 0

    for deal in to_import:
        title     = (deal.get("title") or "").strip()
        pd_status = deal.get("status", "open")
        pd_stage_id = deal.get("stage_id")
        pd_stage_name = pd_stages.get(pd_stage_id, "")

        if pd_status == "won":
            odoo_stage_name = "Ganho"
            stage_date = deal.get("won_time") or deal.get("stage_change_time")
        elif pd_status == "lost":
            odoo_stage_name = "Perdido"
            stage_date = deal.get("lost_time") or deal.get("stage_change_time")
        else:
            odoo_stage_name = STAGE_MAP.get(pd_stage_name)
            stage_date = deal.get("stage_change_time") or deal.get("add_time")
            if not odoo_stage_name:
                if pd_stage_name:
                    log.warning(f"Stage '{pd_stage_name}' sem mapeamento — '{title}'")
                continue

        odoo_stage_id = odoo_stages.get(odoo_stage_name)
        if not odoo_stage_id:
            log.warning(f"Stage '{odoo_stage_name}' não encontrada no Odoo — '{title}'")
            continue

        person_name, email, phone = extract_person_info(deal)

        log.info(f"'{title}' | Contato: '{person_name}' | Stage: {odoo_stage_name} ({stage_date})")

        if DRY_RUN:
            imported += 1
            continue

        try:
            partner_id = create_or_find_contact(models, uid, person_name, email, phone)

            description = "\n".join([
                f"Pipedrive ID: {deal.get('id', '')}",
                f"Data de entrada na stage: {stage_date or ''}",
            ])

            vals = {
                "name": title or "Lead Pipedrive",
                "partner_id": partner_id,
                "description": description,
                "type": "opportunity",
                "team_id": ODOO_TEAM_ID,
                "stage_id": odoo_stage_id,
            }
            if stage_date:
                vals["date_last_stage_update"] = stage_date

            vals = {k: v for k, v in vals.items() if v not in (None, "", 0)}

            lead_id = models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY,
                "crm.lead", "create",
                [vals],
            )
            log.info(f"  Criado: Odoo#{lead_id}")
            imported += 1
        except Exception as exc:
            log.error(f"  Erro ao criar '{title}': {exc}")
            errors += 1

    log.info("=" * 60)
    if DRY_RUN:
        log.info(f"Seriam importados: {imported} — rode com DRY_RUN=false para aplicar.")
    else:
        log.info(f"Importados: {imported} | Erros: {errors}")


if __name__ == "__main__":
    main()
