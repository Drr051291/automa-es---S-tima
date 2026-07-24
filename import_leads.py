#!/usr/bin/env python3
"""
Importa leads do Google Sheets (Meta Ads) para o CRM Odoo via XML-RPC.
Deduplicação baseada no Odoo: consulta leads existentes pelo Meta ID gravado
na descrição, eliminando dependência de cache externo.
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
# Configurações (via .env / secrets)
# ---------------------------------------------------------------------------
ODOO_URL      = os.environ["ODOO_URL"]
ODOO_DB       = os.environ["ODOO_DB"]
ODOO_USER     = os.environ["ODOO_USER"]
ODOO_API_KEY  = os.environ["ODOO_API_KEY"]
ODOO_TEAM_ID  = int(os.environ.get("ODOO_TEAM_ID", "17"))
ODOO_STAGE_ID = int(os.environ.get("ODOO_STAGE_ID", "68"))  # etapa inicial "Lead"

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "12UT2WhaIWsjZW-ojJ1Asy6bH3zaTsDLcRdwiKN3D3_I")
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
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models, uid


META_ID_PATTERN = re.compile(r"Lead ID \(Meta\):\s*(\S+)")


def normalizar_telefone(raw: str) -> str:
    """Reduz um telefone a uma chave comparável: só dígitos, sem DDI 55, últimos 8."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("55") and len(digits) >= 12:
        digits = digits[2:]
    return digits[-8:] if len(digits) >= 8 else digits


def get_existing_identities(models, uid) -> tuple[set, set, set]:
    """Indexa TODOS os leads do pipe (ativos + arquivados) por Meta ID, e-mail e
    telefone, para não recriar quem já existe — inclusive negócios migrados do
    Pipedrive (descrição "Pipedrive ID:" e contato no res.partner)."""
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[
            ["team_id", "=", ODOO_TEAM_ID],
            ["active", "in", [True, False]],
        ]],
        {"fields": ["description", "email_from", "phone", "partner_id"], "limit": 0},
    )

    meta_ids: set = set()
    emails: set = set()
    phones: set = set()
    partner_ids: set = set()

    for lead in leads:
        match = META_ID_PATTERN.search(lead.get("description") or "")
        if match:
            meta_ids.add(match.group(1))
        email = (lead.get("email_from") or "").strip().lower()
        if email:
            emails.add(email)
        phone = normalizar_telefone(lead.get("phone") or "")
        if phone:
            phones.add(phone)
        pid = lead.get("partner_id")
        if pid:
            partner_ids.add(pid[0] if isinstance(pid, (list, tuple)) else pid)

    if partner_ids:
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "read",
            [list(partner_ids)],
            {"fields": ["email", "phone"]},
        )
        for p in partners:
            email = (p.get("email") or "").strip().lower()
            if email:
                emails.add(email)
            phone = normalizar_telefone(p.get("phone") or "")
            if phone:
                phones.add(phone)

    log.info(
        f"Índice de identidade: {len(meta_ids)} Meta ID(s), "
        f"{len(emails)} e-mail(s), {len(phones)} telefone(s) já no Odoo."
    )
    return meta_ids, emails, phones


def row_ja_existe(row: dict, meta_ids: set, emails: set, phones: set) -> bool:
    """True se o lead da planilha já existe no Odoo por Meta ID, e-mail OU telefone."""
    meta_id = (row.get("id") or "").strip()
    if meta_id and meta_id in meta_ids:
        return True
    email = (row.get("email") or "").strip().lower()
    if email and email in emails:
        return True
    phone = normalizar_telefone((row.get("phone_number") or "").replace("p:", ""))
    if phone and phone in phones:
        return True
    return False


# Mapeamento hash das Properties do Odoo -> coluna no Sheets
LEAD_PROPERTIES_MAP = {
    "21787d30494d3a40": "qual_o_seu_cargo_?",
    "41ce60baaee088c1": "faturamento_anual_médio",
    "188ab74703451402": "segmento_da_sua_empresa",
    "b49c6c5c6fc45f8a": "modelo_atual_de_produção_de_conteúdo",
    "881921103e407f69": "número_de_funcionários",
    "834030f02ce53208": "site_da_sua_empresa",
    "324e2a46af924b86": "campaign_name",
    "84f5d4100dc16aa6": "adset_name",
    "8a501dfea857d1ef": "ad_name",
    "58b355d3cd153bd0": "platform",
}


def create_or_find_contact(models, uid, name: str, email: str, phone: str) -> int:
    """Cria um contato no Odoo ou retorna o ID de um já existente (busca por e-mail)."""
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
    ])

    properties = [
        {"name": hash_key, "value": row.get(col, "").strip()}
        for hash_key, col in LEAD_PROPERTIES_MAP.items()
    ]

    company_name = row.get("company_name", "").strip()

    vals = {
        "name": company_name or row.get("full_name", "").strip() or "Lead Meta Ads",
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
    log.info("=== Iniciando importação de leads ===")

    log.info("Lendo planilha do Google Sheets...")
    try:
        records = get_sheet_records()
    except Exception as exc:
        log.error(f"Erro ao ler o Sheets: {exc}")
        raise
    log.info(f"{len(records)} lead(s) na planilha.")

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    meta_ids, emails, phones = get_existing_identities(models, uid)
    new_records = [r for r in records if not row_ja_existe(r, meta_ids, emails, phones)]
    log.info(f"{len(new_records)} lead(s) novo(s) para importar.")

    if not new_records:
        log.info("Nenhum lead novo. Encerrando.")
        return

    imported = 0
    errors = 0
    for row in new_records:
        meta_id = row.get("id", "")
        try:
            name = row.get("full_name", "").strip()
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
