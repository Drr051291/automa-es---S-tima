#!/usr/bin/env python3
"""
Remove leads duplicados do Odoo CRM por IDENTIDADE (Meta ID, e-mail ou telefone).

Diferente da versão antiga (que só agrupava pelo marcador "Lead ID (Meta):"),
esta versão liga entre si quaisquer leads que compartilhem QUALQUER identidade —
Meta ID, e-mail OU telefone. Isso captura o caso da migração: o mesmo lead
entrou uma vez pelo Pipedrive (descrição "Pipedrive ID:", sem Meta ID) e outra
vez pelo importador do Meta (com "Lead ID (Meta):"). Como os dois compartilham
e-mail/telefone, agora são reconhecidos como o mesmo negócio.

Mantém o lead mais antigo de cada grupo (menor create_date) e apaga os demais.
Rode UMA VEZ com DRY_RUN=true, confira o relatório e depois DRY_RUN=false.
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

# Times (pipelines) a limpar. Padrão: Inbound Sétima (16) e Campanhas BrandSpot (17).
TEAM_IDS = [
    int(t) for t in os.environ.get("ODOO_TEAM_IDS_LIMPAR", "16,17").split(",") if t.strip()
]

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

META_ID_PATTERN = re.compile(r"Lead ID \(Meta\):\s*(\S+)")


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def normalizar_telefone(raw: str) -> str:
    """Número nacional completo (DDD+número), só dígitos, sem DDI 55.

    Usa o número inteiro (não só o sufixo) para NÃO agrupar pessoas distintas de
    DDDs diferentes que compartilhem os últimos 8 dígitos. Telefones com menos de
    10 dígitos são considerados inválidos e não geram chave (evita merge por
    número parcial/garbage)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("55") and len(digits) >= 12:
        digits = digits[2:]
    return digits if len(digits) >= 10 else ""


def identidades_do_lead(lead: dict, partner_email: str, partner_phone: str) -> set[str]:
    """Tokens de identidade do lead. Dois leads que compartilhem um token são o
    mesmo negócio (Meta ID, e-mail ou telefone)."""
    tokens: set[str] = set()
    match = META_ID_PATTERN.search(lead.get("description") or "")
    if match:
        tokens.add(f"meta:{match.group(1)}")
    email = (lead.get("email_from") or "").strip().lower() or (partner_email or "").strip().lower()
    if email:
        tokens.add(f"email:{email}")
    phone = normalizar_telefone(lead.get("phone") or "") or normalizar_telefone(partner_phone)
    if phone:
        tokens.add(f"phone:{phone}")
    return tokens


def main():
    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será apagado ===")
    else:
        log.info("=== MODO REAL — duplicatas serão apagadas do Odoo ===")
    log.info(f"Times analisados: {TEAM_IDS}")

    models, uid = odoo_connect()

    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search_read",
        [[["team_id", "in", TEAM_IDS], ["active", "in", [True, False]]]],
        {"fields": ["id", "name", "description", "create_date",
                    "email_from", "phone", "partner_id"], "limit": 0},
    )
    log.info(f"{len(leads)} lead(s) analisado(s).")

    # E-mail/telefone dos contatos (para leads migrados, ficam no res.partner).
    partner_ids = {
        (l["partner_id"][0] if isinstance(l["partner_id"], (list, tuple)) else l["partner_id"])
        for l in leads if l.get("partner_id")
    }
    partner_info: dict[int, dict] = {}
    if partner_ids:
        for p in models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "read",
            [list(partner_ids)], {"fields": ["email", "phone"]},
        ):
            partner_info[p["id"]] = p

    # Union-find: liga leads que compartilham qualquer token de identidade.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    token_owner: dict[str, int] = {}
    lead_by_id: dict[int, dict] = {}
    for lead in leads:
        lead_by_id[lead["id"]] = lead
        pid = lead.get("partner_id")
        pinfo = {}
        if pid:
            pinfo = partner_info.get(pid[0] if isinstance(pid, (list, tuple)) else pid, {})
        find(lead["id"])
        for token in identidades_do_lead(lead, pinfo.get("email", ""), pinfo.get("phone", "")):
            if token in token_owner:
                union(token_owner[token], lead["id"])
            else:
                token_owner[token] = lead["id"]

    grupos: dict[int, list[dict]] = {}
    for lid in lead_by_id:
        grupos.setdefault(find(lid), []).append(lead_by_id[lid])

    ids_para_apagar: list[int] = []
    for membros in grupos.values():
        if len(membros) < 2:
            continue
        # mais antigo = menor create_date (empate: menor id)
        membros.sort(key=lambda x: (x.get("create_date") or "", x["id"]))
        manter = membros[0]
        apagar = membros[1:]
        log.info(
            f"Grupo de {len(membros)}: mantendo Odoo#{manter['id']} ({manter['name']}), "
            f"apagando {[d['id'] for d in apagar]}"
        )
        ids_para_apagar.extend(d["id"] for d in apagar)

    log.info(f"Total de duplicatas a remover: {len(ids_para_apagar)}")

    if not ids_para_apagar:
        log.info("Nenhuma duplicata encontrada.")
        return

    if DRY_RUN:
        log.info("Simulação concluída. Para apagar, execute com DRY_RUN=false.")
        return

    # apaga em lotes para não estourar limites do XML-RPC
    for i in range(0, len(ids_para_apagar), 100):
        lote = ids_para_apagar[i:i + 100]
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "unlink", [lote])
        log.info(f"Removidos {len(lote)} lead(s).")
    log.info(f"{len(ids_para_apagar)} duplicata(s) removida(s) com sucesso.")


if __name__ == "__main__":
    main()
