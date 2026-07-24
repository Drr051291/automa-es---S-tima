#!/usr/bin/env python3
"""
Migração ÚNICA: importa para o Odoo os negócios do Pipedrive BrandSpot
(pipeline 9) que ainda não existem no Odoo (qualquer origem: Landing Page,
outbound, manuais). Inclui histórico:
  - open  -> criado ativo na etapa mapeada
  - won   -> criado ativo na etapa Ganho
  - lost  -> criado ARQUIVADO (status perdido), preservando a etapa de origem

Dedup: por título (equipe BrandSpot, ativos+arquivados) para não duplicar os
leads que já vieram do Meta; e por "Pipedrive ID:" para poder re-rodar sem
duplicar o que esta migração já criou.

Não tem relação com o import recorrente do Meta — é um passo único.
Rode primeiro com DRY_RUN=true para revisar.
"""

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

ODOO_URL        = os.environ["ODOO_URL"]
ODOO_DB         = os.environ["ODOO_DB"]
ODOO_USER       = os.environ["ODOO_USER"]
ODOO_API_KEY    = os.environ["ODOO_API_KEY"]
PIPEDRIVE_TOKEN    = os.environ["PIPEDRIVE_TOKEN"]
PIPEDRIVE_BASE     = "https://api.pipedrive.com/v1"
PIPEDRIVE_PIPELINE = int(os.environ.get("PIPEDRIVE_PIPELINE_ID", "9"))
ODOO_TEAM_ID       = int(os.environ.get("ODOO_TEAM_ID", "17"))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# Mesmo de-para por ID usado na sincronização (Pipedrive stage_id -> Odoo stage id)
PD_STAGE_TO_ODOO = {
    43: 68,   # Lead                -> Lead
    48: 51,   # MQL                 -> MQL
    49: 52,   # SQL (Call Agendada) -> SQL
    39: 63,   # Show  room          -> Visita Showroom
    75: 17,   # Oportuindade        -> Oportunidade
    40: 71,   # Negociação          -> Negociação
}
ODOO_STAGE_GANHO = 4   # etapa Ganho (is_won)
ODOO_STAGE_FALLBACK = 68   # se um lost vier de etapa sem mapeamento, cai em Lead


# ---------------------------------------------------------------------------
# Pipedrive
# ---------------------------------------------------------------------------

def pd_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params["api_token"] = PIPEDRIVE_TOKEN
    r = requests.get(f"{PIPEDRIVE_BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_pipedrive_deals() -> list[dict]:
    """Só deals do pipeline BrandSpot (a API ignora o filtro pipeline_id)."""
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
    return [d for d in deals if d.get("pipeline_id") == PIPEDRIVE_PIPELINE]


def extract_person_info(deal: dict) -> tuple[str, str, str]:
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
    return (name or "").strip(), (email or "").strip(), (phone or "").strip()


def resolve_target(deal: dict) -> tuple[int, bool, str | None]:
    """(odoo_stage_id, ativo, data)."""
    status   = deal.get("status", "open")
    stage_id = deal.get("stage_id")
    mapped   = PD_STAGE_TO_ODOO.get(stage_id, ODOO_STAGE_FALLBACK)
    if status == "won":
        return ODOO_STAGE_GANHO, True, (deal.get("won_time") or deal.get("stage_change_time"))
    if status == "lost":
        return mapped, False, (deal.get("lost_time") or deal.get("stage_change_time"))
    return mapped, True, (deal.get("stage_change_time") or deal.get("add_time"))


# ---------------------------------------------------------------------------
# Odoo
# ---------------------------------------------------------------------------

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def get_existing_titles(models, uid) -> set[str]:
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "search_read",
        [[["team_id", "=", ODOO_TEAM_ID], ["active", "in", [True, False]]]],
        {"fields": ["name"], "limit": 0},
    )
    return {(l["name"] or "").strip().lower() for l in leads}


def get_existing_pd_ids(models, uid) -> set[str]:
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "search_read",
        [[["description", "like", "Pipedrive ID:"], ["active", "in", [True, False]]]],
        {"fields": ["description"], "limit": 0},
    )
    ids, pat = set(), re.compile(r"Pipedrive ID:\s*(\S+)")
    for l in leads:
        m = pat.search(l.get("description") or "")
        if m:
            ids.add(m.group(1))
    return ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if os.environ.get("CONFIRMAR_LEITURA_PIPEDRIVE", "").lower() != "true":
        log.warning(
            "Migração Pipedrive → Odoo (BrandSpot) já concluída. Este script LÊ o "
            "Pipedrive e serve apenas para a migração pontual — não faz parte do "
            "fluxo contínuo (Meta → Odoo → DataCrazy). Para rodar mesmo assim, "
            "defina CONFIRMAR_LEITURA_PIPEDRIVE=true."
        )
        return

    if DRY_RUN:
        log.info("=== MODO SIMULAÇÃO (DRY_RUN=true) — nada será criado ===")
    else:
        log.info("=== MODO REAL — leads serão criados no Odoo ===")

    log.info(f"Buscando deals do Pipedrive (pipeline {PIPEDRIVE_PIPELINE} - BrandSpot)...")
    deals = get_pipedrive_deals()
    log.info(f"{len(deals)} deal(s) do BrandSpot.")

    models, uid = odoo_connect()
    existing_titles = get_existing_titles(models, uid)
    existing_pd_ids = get_existing_pd_ids(models, uid)
    log.info(f"{len(existing_titles)} título(s) já no Odoo (equipe {ODOO_TEAM_ID}); "
             f"{len(existing_pd_ids)} já com Pipedrive ID.")

    criados = pulados = erros = 0
    por_status = {"open": 0, "won": 0, "lost": 0}

    for deal in deals:
        pd_id  = str(deal.get("id", ""))
        title  = (deal.get("title") or "").strip()
        status = deal.get("status", "open")

        if pd_id in existing_pd_ids:
            pulados += 1
            continue
        if title and title.lower() in existing_titles:
            pulados += 1
            continue

        stage_id, ativo, stage_date = resolve_target(deal)
        person_name, email, phone = extract_person_info(deal)
        nome_lead = title or person_name or f"Negócio Pipedrive {pd_id}"

        log.info(f"[{status}] '{nome_lead}' (PD#{pd_id}) -> stage={stage_id} "
                 f"{'ativo' if ativo else 'ARQUIVADO(perdido)'} ({stage_date})")

        por_status[status] = por_status.get(status, 0) + 1

        if DRY_RUN:
            criados += 1
            # evita recontar título repetido dentro da própria simulação
            if title:
                existing_titles.add(title.lower())
            existing_pd_ids.add(pd_id)
            continue

        try:
            description = "\n".join([
                f"Pipedrive ID: {pd_id}",
                "Origem: Pipedrive (migração BrandSpot)",
                f"Data de entrada na etapa: {stage_date or ''}",
            ])
            # Não cria res.partner: dados de contato ficam no próprio negócio.
            vals = {
                "name": nome_lead,
                "type": "opportunity",
                "team_id": ODOO_TEAM_ID,
                "stage_id": stage_id,
                "description": description,
                "active": ativo,
            }
            if person_name:
                vals["contact_name"] = person_name
            if email:
                vals["email_from"] = email
            if phone:
                vals["phone"] = phone
            if stage_date:
                vals["date_last_stage_update"] = stage_date
            if status == "won":
                vals["probability"] = 100
            elif status == "lost":
                vals["probability"] = 0

            lead_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "create", [vals])
            log.info(f"  Criado: Odoo#{lead_id}")
            criados += 1
            if title:
                existing_titles.add(title.lower())
            existing_pd_ids.add(pd_id)
        except Exception as exc:
            log.error(f"  Erro ao criar '{nome_lead}' (PD#{pd_id}): {exc}")
            erros += 1

    log.info("=" * 60)
    log.info(f"BrandSpot: {len(deals)} | Pulados (já no Odoo): {pulados} | "
             f"Por status: {por_status}")
    if DRY_RUN:
        log.info(f"Seriam criados: {criados} — rode com DRY_RUN=false para aplicar.")
    else:
        log.info(f"Criados: {criados} | Erros: {erros}")


if __name__ == "__main__":
    main()
