#!/usr/bin/env python3
"""
Importa leads do Google Sheets (Meta Ads - Sétima) para o CRM Odoo via XML-RPC.
Pipeline: Inbound Sétima (team_id=16) | Estágio inicial: Leads (stage_id=68)
Deduplicação baseada no Odoo: consulta leads existentes pelo Meta ID na descrição.
"""

import csv
import io
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

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------
ODOO_URL      = os.environ["ODOO_URL"]
ODOO_DB       = os.environ["ODOO_DB"]
ODOO_USER     = os.environ["ODOO_USER"]
ODOO_API_KEY  = os.environ["ODOO_API_KEY"]
ODOO_TEAM_ID  = int(os.environ.get("ODOO_TEAM_ID_SETIMA", "16"))
ODOO_STAGE_ID = int(os.environ.get("ODOO_STAGE_ID_SETIMA", "68"))

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID_SETIMA", "1tshAT18GUQMs54DIQS8yBF7rZUWzQW-h4wXYf2sxZRU")
SHEETS_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv"
)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_sheet_records() -> list[dict]:
    response = requests.get(SHEETS_CSV_URL, timeout=30)
    response.raise_for_status()
    response.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader)


# ---------------------------------------------------------------------------
# Odoo XML-RPC
# ---------------------------------------------------------------------------

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def get_existing_meta_ids(models, uid) -> set:
    """Retorna Meta IDs já importados no pipeline Inbound Sétima."""
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[
            ["description", "like", "Lead ID (Meta):"],
            ["team_id", "=", ODOO_TEAM_ID],
        ]],
        {"fields": ["description"], "limit": 0},
    )
    ids = set()
    pattern = re.compile(r"Lead ID \(Meta\):\s*(\S+)")
    for lead in leads:
        match = pattern.search(lead.get("description") or "")
        if match:
            ids.add(match.group(1))
    log.info(f"{len(ids)} Meta ID(s) já existentes no Odoo (Inbound Sétima).")
    return ids


# Mapeamento hash das Properties do Odoo → coluna no Sheets
LEAD_PROPERTIES_MAP = {
    "bc2f7080491cd41e": "qual_é_o_principal_objetivo_do_seu_projeto_com_a_sétima?",
    "da57e67b7aa5aa49": "qual_o_setor_da_sua_empresa",
    "f13a6ff4b0b1b0c8": "quantos_colaboradores_sua_empresa_tem_aproximadamente_?",
    "0c56bb6e8f08dcf2": "qual_seu_cargo_?",
    "6a48564918fb945e": "qual_é_o_investimento_mensal_total_da_sua_empresa_em_marketing_?_(considere_mídia_paga_+_produção_de_conteúdo_+_agências/fornecedores)",
    "362c9666b018e4fd": "site",
}


def create_or_find_contact(models, uid, name: str, email: str, phone: str) -> int:
    if email:
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "search_read",
            [[["email", "=", email]]],
            {"fields": ["id", "name"], "limit": 1},
        )
        if existing:
            log.info(f"Contato já existe: res.partner#{existing[0]['id']} | {existing[0]['name']}")
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
    log.info(f"Contato criado: res.partner#{partner_id} | {contact_vals['name']}")
    return partner_id


def build_lead_vals(row: dict, partner_id: int) -> dict:
    description = "\n".join([
        f"Lead ID (Meta): {row.get('id', '')}",
        f"Data de criacao (Meta): {row.get('created_time', '')}",
        f"Formulario: {row.get('form_name', '')}",
        f"Campanha: {row.get('campaign_name', '')}",
        f"Conjunto de anuncios: {row.get('adset_name', '')}",
        f"Anuncio: {row.get('ad_name', '')}",
        f"Plataforma: {row.get('platform', '')}",
    ])

    properties = [
        {"name": hash_key, "value": row.get(col, "").strip()}
        for hash_key, col in LEAD_PROPERTIES_MAP.items()
    ]

    company_name = row.get("company_name", "").strip()

    vals = {
        "name": company_name or row.get("first_name", "").strip() or "Lead Meta Ads",
        "partner_id": partner_id,
        "description": description,
        "type": "opportunity",
        "team_id": ODOO_TEAM_ID,
        "stage_id": ODOO_STAGE_ID,
        "lead_properties": properties,
    }

    return {k: v for k, v in vals.items() if v not in (None, "", 0)}


def create_lead(models, uid, vals: dict) -> int:
    return models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "create",
        [vals],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Iniciando importação de leads (Inbound Sétima) ===")

    log.info("Lendo planilha do Google Sheets...")
    try:
        records = get_sheet_records()
    except Exception as exc:
        log.error(f"Erro ao ler o Sheets: {exc}")
        raise
    log.info(f"{len(records)} lead(s) na planilha.")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    existing_ids = get_existing_meta_ids(models, uid)
    new_records = [r for r in records if r.get("id", "") not in existing_ids]
    log.info(f"{len(new_records)} lead(s) novo(s) para importar.")

    if not new_records:
        log.info("Nenhum lead novo. Encerrando.")
        return

    imported = 0
    errors = 0
    for row in new_records:
        meta_id = row.get("id", "")
        try:
            name  = row.get("first_name", "").strip()
            email = row.get("email", "").strip()
            phone = row.get("phone_number", "").strip().replace("p:", "").strip()

            partner_id = create_or_find_contact(models, uid, name, email, phone)
            vals = build_lead_vals(row, partner_id)
            lead_id = create_lead(models, uid, vals)
            log.info(f"Negócio criado: Odoo#{lead_id} | {vals.get('name')} | Contato: {name} | Meta ID: {meta_id}")
            imported += 1
        except Exception as exc:
            log.error(f"Erro ao criar lead Meta ID {meta_id}: {exc}")
            errors += 1

    log.info(f"=== Concluído: {imported} importado(s), {errors} erro(s) ===")


if __name__ == "__main__":
    main()
