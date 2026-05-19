#!/usr/bin/env python3
"""
Importa leads do Google Sheets (Meta Ads) para o CRM Odoo via XML-RPC.
Execute periodicamente via cron: */15 * * * * /usr/bin/python3 /path/to/import_leads.py
"""

import csv
import io
import json
import logging
import os
import xmlrpc.client
from pathlib import Path

import requests
from dotenv import load_dotenv

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
ODOO_URL      = os.environ["ODOO_URL"]
ODOO_DB       = os.environ["ODOO_DB"]
ODOO_USER     = os.environ["ODOO_USER"]
ODOO_API_KEY  = os.environ["ODOO_API_KEY"]
ODOO_TEAM_ID  = int(os.environ.get("ODOO_TEAM_ID", "17"))
ODOO_STAGE_ID = int(os.environ.get("ODOO_STAGE_ID", "17"))

SPREADSHEET_ID     = os.environ.get("SPREADSHEET_ID", "12UT2WhaIWsjZW-ojJ1Asy6bH3zaTsDLcRdwiKN3D3_I")
PROCESSED_IDS_FILE = Path(os.environ.get("PROCESSED_IDS_FILE", "processed_ids.json"))

SHEETS_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv"
)

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
# Google Sheets (leitura pública via CSV)
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
        raise RuntimeError("Falha na autenticação com o Odoo. Verifique ODOO_USER e ODOO_API_KEY.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models, uid


# Mapeamento dos hashes das Properties do Odoo -> coluna no Sheets
LEAD_PROPERTIES_MAP = {
    # Dados do formulário
    "21787d30494d3a40": "qual_o_seu_cargo_?",
    "41ce60baaee088c1": "faturamento_anual_médio",
    "188ab74703451402": "segmento_da_sua_empresa",
    "b49c6c5c6fc45f8a": "modelo_atual_de_produção_de_conteúdo",
    "881921103e407f69": "número_de_funcionários",
    "834030f02ce53208": "site_da_sua_empresa",
    # UTM / Meta Ads
    "324e2a46af924b86": "campaign_name",
    "84f5d4100dc16aa6": "adset_name",
    "8a501dfea857d1ef": "ad_name",
    "58b355d3cd153bd0": "platform",
}


def build_lead_properties(row: dict) -> list:
    return [
        {"name": hash_key, "value": row.get(sheet_col, "").strip()}
        for hash_key, sheet_col in LEAD_PROPERTIES_MAP.items()
    ]


def build_lead_vals(row: dict) -> dict:
    phone_raw = row.get("phone_number", "").strip()
    phone = phone_raw.replace("p:", "").strip()

    description_lines = [
        f"Lead ID (Meta): {row.get('id', '')}",
        f"Data de criacao (Meta): {row.get('created_time', '')}",
        f"Formulario: {row.get('form_name', '')}",
    ]

    vals = {
        "name": row.get("full_name", "Lead Meta Ads").strip() or "Lead Meta Ads",
        "contact_name": row.get("full_name", "").strip(),
        "email_from": row.get("email", "").strip(),
        "phone": phone,
        "partner_name": row.get("company_name", "").strip(),
        "description": "\n".join(description_lines),
        "type": "opportunity",
        "team_id": ODOO_TEAM_ID,
        "stage_id": ODOO_STAGE_ID,
        "lead_properties": build_lead_properties(row),
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

    processed_ids = load_processed_ids()
    log.info(f"{len(processed_ids)} lead(s) já importados anteriormente.")

    log.info("Lendo planilha do Google Sheets...")
    try:
        records = get_sheet_records()
    except Exception as exc:
        log.error(f"Erro ao ler o Sheets: {exc}")
        raise

    new_records = [r for r in records if r.get("id", "") not in processed_ids]
    log.info(f"{len(new_records)} lead(s) novo(s) encontrado(s) de {len(records)} total.")

    if not new_records:
        log.info("Nenhum lead novo. Encerrando.")
        return

    log.info("Conectando ao Odoo...")
    models, uid = odoo_connect()

    imported = 0
    errors = 0
    for row in new_records:
        meta_id = row.get("id", "")
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
