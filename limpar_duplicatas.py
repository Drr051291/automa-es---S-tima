#!/usr/bin/env python3
"""
Remove leads duplicados do Odoo CRM (mesmo Meta ID importado mais de uma vez).

Agrupa por Meta ID (chave confiável gravada na descrição). Só considera
DUPLICATA quando o MESMO Meta ID aparece em mais de um lead — nunca junta
empresas diferentes que apenas têm nome parecido.

De cada grupo, MANTÉM o lead mais antigo (menor id do Odoo) e trata os demais.

Ação (reversível por padrão):
  - HARD_DELETE=false (padrão): ARQUIVA as cópias (active=False) — some do
    Kanban mas dá para desarquivar. Recomendado.
  - HARD_DELETE=true: APAGA de vez (unlink) — irreversível.

Escopo:
  - ODOO_TEAM_ID (opcional): se definido, só mexe nos leads dessa equipe
    (ex.: 17 = Campanhas BrandSpot). Sem definir, processa todas as equipes.

Só enxerga leads ATIVOS (não arquivados) — assim mira exatamente as cópias
que ainda estão poluindo o funil.

Rode primeiro com DRY_RUN=true para revisar.
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

DRY_RUN     = os.environ.get("DRY_RUN", "true").lower() != "false"
HARD_DELETE = os.environ.get("HARD_DELETE", "false").lower() == "true"
# 0 / vazio = todas as equipes; ex.: 17 = Campanhas BrandSpot
ODOO_TEAM_ID = int(os.environ.get("ODOO_TEAM_ID", "0") or "0")


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def main():
    acao = "APAGAR (unlink, irreversível)" if HARD_DELETE else "ARQUIVAR (active=False, reversível)"
    escopo = f"equipe {ODOO_TEAM_ID}" if ODOO_TEAM_ID else "todas as equipes"
    if DRY_RUN:
        log.info(f"=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será alterado === | Ação: {acao} | Escopo: {escopo}")
    else:
        log.info(f"=== MODO REAL === | Ação: {acao} | Escopo: {escopo}")

    models, uid = odoo_connect()

    domain = [["description", "like", "Lead ID (Meta):"]]
    if ODOO_TEAM_ID:
        domain.append(["team_id", "=", ODOO_TEAM_ID])

    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [domain],
        {"fields": ["id", "name", "description", "create_date", "stage_id"], "limit": 0},
    )
    log.info(f"{len(leads)} lead(s) ativo(s) com Meta ID no escopo.")

    pattern = re.compile(r"Lead ID \(Meta\):\s*(\S+)")
    groups: dict[str, list[dict]] = {}
    for lead in leads:
        match = pattern.search(lead.get("description") or "")
        if match:
            groups.setdefault(match.group(1), []).append(lead)

    ids_para_tratar: list[int] = []
    grupos_dup = 0
    for meta_id, group in groups.items():
        if len(group) > 1:
            grupos_dup += 1
            group.sort(key=lambda x: x["id"])   # menor id = mais antigo, mantém
            to_keep = group[0]
            to_delete = group[1:]
            log.info(
                f"Meta ID {meta_id}: mantendo Odoo#{to_keep['id']} ({to_keep['name']}), "
                f"tratando {[d['id'] for d in to_delete]}"
            )
            ids_para_tratar.extend(d["id"] for d in to_delete)

    log.info(f"{grupos_dup} grupo(s) com duplicata | {len(ids_para_tratar)} cópia(s) a tratar.")

    if not ids_para_tratar:
        log.info("Nenhuma duplicata encontrada.")
        return

    if DRY_RUN:
        log.info("Simulação concluída. Para aplicar, rode com DRY_RUN=false.")
        return

    if HARD_DELETE:
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "unlink", [ids_para_tratar])
        log.info(f"{len(ids_para_tratar)} duplicata(s) APAGADA(s) permanentemente.")
    else:
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "write",
                          [ids_para_tratar, {"active": False}])
        log.info(f"{len(ids_para_tratar)} duplicata(s) ARQUIVADA(s) (reversível).")


if __name__ == "__main__":
    main()
