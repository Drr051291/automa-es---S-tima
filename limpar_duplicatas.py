#!/usr/bin/env python3
"""
Remove leads duplicados do Odoo CRM.
Mantém o lead mais antigo de cada Meta ID e apaga os demais.
Execute UMA VEZ manualmente após identificar duplicatas.
"""

import logging
import os
import re
import xmlrpc.client

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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


def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será apagado ===")
    else:
        log.info("=== MODO REAL — duplicatas serão apagadas do Odoo ===")

    models, uid = odoo_connect()

    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[["description", "like", "Lead ID (Meta):"]]],
        {"fields": ["id", "name", "description", "create_date"], "limit": 0},
    )
    log.info(f"{len(leads)} lead(s) com Meta ID encontrado(s).")

    pattern = re.compile(r"Lead ID \(Meta\):\s*(\S+)")
    groups: dict[str, list[dict]] = {}
    for lead in leads:
        match = pattern.search(lead.get("description") or "")
        if match:
            meta_id = match.group(1)
            groups.setdefault(meta_id, []).append(lead)

    duplicates_to_delete = []
    for meta_id, group in groups.items():
        if len(group) > 1:
            # Ordena pelo ID do Odoo (menor = mais antigo), mantém o primeiro
            group.sort(key=lambda x: x["id"])
            to_keep = group[0]
            to_delete = group[1:]
            log.info(
                f"Meta ID {meta_id}: mantendo Odoo#{to_keep['id']} ({to_keep['name']}), "
                f"apagando {[d['id'] for d in to_delete]}"
            )
            duplicates_to_delete.extend(d["id"] for d in to_delete)

    log.info(f"Total de duplicatas encontradas: {len(duplicates_to_delete)}")

    if not duplicates_to_delete:
        log.info("Nenhuma duplicata encontrada.")
        return

    if DRY_RUN:
        log.info("Simulação concluída. Para apagar, execute com DRY_RUN=false")
        return

    models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "unlink",
        [duplicates_to_delete],
    )
    log.info(f"{len(duplicates_to_delete)} duplicata(s) removida(s) com sucesso.")


if __name__ == "__main__":
    main()
