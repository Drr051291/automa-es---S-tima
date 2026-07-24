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


META_ID_PATTERN = re.compile(r"Lead ID \(Meta\):\s*(\S+)")


def normalizar_telefone(raw: str) -> str:
    """Reduz um telefone a uma chave comparável: só dígitos, sem DDI 55, últimos 8.

    Isso torna a deduplicação robusta a variações de formatação/DDI entre o
    Meta, o Pipedrive migrado e o Odoo (ex.: '+55 (11) 99999-8888' e
    '11999998888' viram a mesma chave)."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("55") and len(digits) >= 12:
        digits = digits[2:]
    return digits[-8:] if len(digits) >= 8 else digits


def get_existing_identities(models, uid) -> tuple[set, set, set]:
    """Indexa TODOS os leads da Sétima (ativos + arquivados) por identidade.

    Retorna (meta_ids, emails, telefones). A deduplicação passa a reconhecer
    um lead que já existe no Odoo mesmo que ele NÃO tenha o marcador
    "Lead ID (Meta):" — caso dos negócios migrados do Pipedrive, cuja descrição
    traz "Pipedrive ID:" e cujos contatos guardam e-mail/telefone no
    res.partner. Sem isso, cada lead migrado do Pipedrive é visto como "novo" e
    recriado na etapa Lead a cada execução do cron (a causa dos 200+ leads que
    voltavam sozinhos).
    """
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

    # E-mail/telefone dos leads migrados costumam viver no res.partner, não no
    # crm.lead — busca em lote para completar o índice de identidade.
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
        f"Índice de identidade Sétima: {len(meta_ids)} Meta ID(s), "
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
