#!/usr/bin/env python3
"""
Migra leads Meta Ads já existentes no Odoo para a nova estrutura:
- Cria um contato (res.partner) com o nome da pessoa
- Atualiza o título do negócio para o nome da empresa
- Linka o negócio ao contato via partner_id

Execute em modo simulação primeiro (DRY_RUN=true) para revisar o que será alterado.
"""

import logging
import os
import xmlrpc.client

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

ODOO_URL     = os.environ["ODOO_URL"]
ODOO_DB      = os.environ["ODOO_DB"]
ODOO_USER    = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def create_or_find_contact(models, uid, name: str, email: str, phone: str) -> int:
    if email:
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "search_read",
            [[["email", "=", email]]],
            {"fields": ["id", "name"], "limit": 1},
        )
        if existing:
            log.info(f"  Contato já existe: res.partner#{existing[0]['id']} | {existing[0]['name']}")
            return existing[0]["id"]

    contact_vals = {"name": name or "Contato Meta Ads"}
    if email:
        contact_vals["email"] = email
    if phone:
        contact_vals["phone"] = phone

    partner_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "res.partner", "create",
        [contact_vals],
    )
    log.info(f"  Contato criado: res.partner#{partner_id} | {contact_vals['name']}")
    return partner_id


def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será alterado ===")
    else:
        log.info("=== MODO REAL — leads serão atualizados no Odoo ===")

    models, uid = odoo_connect()

    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[["description", "like", "Lead ID (Meta):"]]],
        {
            "fields": ["id", "name", "contact_name", "email_from", "phone", "partner_name", "partner_id"],
            "limit": 0,
        },
    )
    log.info(f"{len(leads)} lead(s) Meta Ads encontrado(s) no Odoo.")

    # Filtra apenas os que ainda não têm contato linkado
    to_migrate = [l for l in leads if not l.get("partner_id")]
    already_ok = len(leads) - len(to_migrate)

    log.info(f"{already_ok} já possuem contato linkado — serão ignorados.")
    log.info(f"{len(to_migrate)} serão migrados.")

    if not to_migrate:
        log.info("Nada a fazer. Encerrando.")
        return

    updated = 0
    errors = 0

    for lead in to_migrate:
        lead_id    = lead["id"]
        person     = (lead.get("contact_name") or lead.get("name") or "").strip()
        email      = (lead.get("email_from") or "").strip()
        phone      = (lead.get("phone") or "").strip()
        company    = (lead.get("partner_name") or "").strip()
        new_title  = company or person or "Lead Meta Ads"

        log.info(f"Lead #{lead_id} | Título atual: '{lead['name']}' → novo: '{new_title}' | Contato: '{person}'")

        if DRY_RUN:
            updated += 1
            continue

        try:
            partner_id = create_or_find_contact(models, uid, person, email, phone)

            models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY,
                "crm.lead", "write",
                [[lead_id], {"name": new_title, "partner_id": partner_id}],
            )
            log.info(f"  Lead #{lead_id} atualizado.")
            updated += 1
        except Exception as exc:
            log.error(f"  Erro ao migrar lead #{lead_id}: {exc}")
            errors += 1

    if DRY_RUN:
        log.info(f"=== Simulação concluída: {updated} lead(s) seriam migrados ===")
        log.info("Para aplicar de verdade, execute com DRY_RUN=false")
    else:
        log.info(f"=== Concluído: {updated} migrado(s), {errors} erro(s) ===")


if __name__ == "__main__":
    main()
