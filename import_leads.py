#!/usr/bin/env python3
"""
Importa leads do Google Sheets (Meta Ads) para o CRM Odoo via JSON-RPC.
Execute periodicamente via cron: */15 * * * * /usr/bin/python3 /path/to/import_leads.py
"""

import json
import os
import logging
import xmlrpc.client
from datetime import datetime
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("import_leads.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurações (via .env)
# ---------------------------------------------------------------------------
ODOO_URL = os.environ["ODOO_URL"]               # ex: https://meuodoo.com
ODOO_DB = os.environ["ODOO_DB"]                 # nome do banco
ODOO_USER = os.environ["ODOO_USER"]             # e-mail do usuário
ODOO_API_KEY = os.environ["ODOO_API_KEY"]       # chave de API do Odoo

GOOGLE_CREDENTIALS_FILE = os.environ["GOOGLE_CREDENTIALS_FILE"]  # caminho do JSON da conta de serviço
SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "12UT2WhaIWsjZW-ojJ1Asy6bH3zaTsDLcRdwiKN3D3_I"
)
SHEET_NAME = os.environ.get("SHEET_NAME", "")  # deixe vazio para usar a primeira aba

# ID do pipeline (sales team) e estágio inicial no Odoo.
# Descubra com: models.execute_kw(db, uid, key, 'crm.team', 'search_read', [[]], {'fields': ['id','name']})
ODOO_TEAM_ID = int(os.environ.get("ODOO_TEAM_ID", "0")) or None
ODOO_STAGE_ID = int(os.environ.get("ODOO_STAGE_ID", "0")) or None

PROCESSED_IDS_FILE = Path(os.environ.get("PROCESSED_IDS_FILE", "processed_ids.json"))


# ---------------------------------------------------------------------------
# Controle de IDs já importados
# ---------------------------------------------------------------------------

def load_processed_ids() -> set:
    if PROCESSED_IDS_FILE.exists():
        return set(json.loads(PROCESSED_IDS_FILE.read_text()))
    return set()


def save_processed_ids(ids: set) -> None:
    PROCESSED_IDS_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_sheet_records() -> list[dict]:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(SHEET_NAME) if SHEET_NAME else spreadsheet.sheet1
    return worksheet.get_all_records()


# ---------------------------------------------------------------------------
# Odoo JSON-RPC
# ---------------------------------------------------------------------------

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo. Verifique ODOO_USER e ODOO_API_KEY.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models, uid


def build_lead_vals(row: dict) -> dict:
    """Mapeia as colunas do Sheets para os campos do crm.lead no Odoo."""

    name_parts = str(row.get("full_name", "")).strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    # Monta a descrição interna com todos os campos do formulário
    description_lines = [
        f"**Origem:** Meta Ads — {row.get('platform', '')}",
        f"**Campanha:** {row.get('campaign_name', '')} (ID: {row.get('campaign_id', '')})",
        f"**Conjunto de anúncios:** {row.get('adset_name', '')}",
        f"**Anúncio:** {row.get('ad_name', '')}",
        f"**Formulário:** {row.get('form_name', '')}",
        "",
        f"**Cargo:** {row.get('qual_o_seu_cargo_?', '')}",
        f"**Faturamento anual médio:** {row.get('faturamento_anual_médio', '')}",
        f"**Segmento:** {row.get('segmento_da_sua_empresa', '')}",
        f"**Modelo de produção de conteúdo:** {row.get('modelo_atual_de_produção_de_conteúdo', '')}",
        f"**Número de funcionários:** {row.get('número_de_funcionários', '')}",
        f"**Site:** {row.get('site_da_sua_empresa', '')}",
        "",
        f"**Lead ID (Meta):** {row.get('id', '')}",
        f"**Data de criação (Meta):** {row.get('created_time', '')}",
    ]

    vals: dict = {
        "name": row.get("full_name", "Lead Meta Ads").strip() or "Lead Meta Ads",
        "contact_name": row.get("full_name", "").strip(),
        "email_from": row.get("email", "").strip(),
        "phone": row.get("phone_number", "").strip(),
        "partner_name": row.get("company_name", "").strip(),
        "website": row.get("site_da_sua_empresa", "").strip(),
        "description": "\n".join(description_lines),
        "type": "lead",
        # Origem: Meta Ads
        "ref": f"META-{row.get('id', '')}",
    }

    # Pipeline e estágio (se configurados)
    if ODOO_TEAM_ID:
        vals["team_id"] = ODOO_TEAM_ID
    if ODOO_STAGE_ID:
        vals["stage_id"] = ODOO_STAGE_ID

    # Remove campos vazios para não sobrescrever defaults do Odoo
    return {k: v for k, v in vals.items() if v not in (None, "", 0)}


def create_lead(models, uid, vals: dict) -> int:
    lead_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "create",
        [vals],
    )
    return lead_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Iniciando importação de leads ===")

    processed_ids = load_processed_ids()
    log.info(f"{len(processed_ids)} lead(s) já importados anteriormente.")

    log.info("Lendo planilha do Google Sheets...")
    try:
        records = get_sheet_records()
    except Exception as exc:
        log.error(f"Erro ao ler o Sheets: {exc}")
        raise

    new_records = [r for r in records if str(r.get("id", "")) not in processed_ids]
    log.info(f"{len(new_records)} lead(s) novo(s) encontrado(s).")

    if not new_records:
        log.info("Nenhum lead novo. Encerrando.")
        return

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    imported = 0
    errors = 0
    for row in new_records:
        meta_id = str(row.get("id", ""))
        try:
            vals = build_lead_vals(row)
            lead_id = create_lead(models, uid, vals)
            processed_ids.add(meta_id)
            log.info(f"Lead criado: Odoo#{lead_id} | {vals.get('name')} | Meta ID: {meta_id}")
            imported += 1
        except Exception as exc:
            log.error(f"Erro ao criar lead Meta ID {meta_id}: {exc}")
            errors += 1

    save_processed_ids(processed_ids)
    log.info(f"=== Concluído: {imported} importado(s), {errors} erro(s) ===")


if __name__ == "__main__":
    main()
