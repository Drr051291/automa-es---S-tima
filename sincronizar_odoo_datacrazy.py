#!/usr/bin/env python3
"""
Sincroniza novos negócios do Odoo → DataCrazy (webhook de Entrada de Negócios).

Pipelines: Campanhas BrandSpot (team_id=17) e Inbound Sétima (team_id=16).
Cada pipeline envia para o SEU webhook no DataCrazy (uma integração por pipe),
permitindo etapa/tags/automação de WhatsApp diferentes para cada funil.

Deduplicação baseada no Odoo: após envio com sucesso, o lead recebe a tag
"DataCrazy" (crm.tag). Leads com a tag nunca são reenviados.

Só sincroniza leads criados a partir de DATACRAZY_SYNC_DESDE, para não
despejar o histórico migrado do Pipedrive dentro do DataCrazy.
"""

import json
import logging
import os
import re
import time
import unicodedata
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
ODOO_URL     = os.environ["ODOO_URL"]
ODOO_DB      = os.environ["ODOO_DB"]
ODOO_USER    = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

TEAM_ID_BRANDSPOT = int(os.environ.get("ODOO_TEAM_ID_BRANDSPOT", "17"))
TEAM_ID_SETIMA    = int(os.environ.get("ODOO_TEAM_ID_SETIMA", "16"))

# Webhook de "Entrada de Negócios" (Configurações → Integrações) de cada pipe
WEBHOOKS = {
    TEAM_ID_BRANDSPOT: os.environ.get("DATACRAZY_WEBHOOK_BRANDSPOT", "").strip(),
    TEAM_ID_SETIMA:    os.environ.get("DATACRAZY_WEBHOOK_SETIMA", "").strip(),
}
PIPELINE_LABEL = {
    TEAM_ID_BRANDSPOT: "BrandSpot",
    TEAM_ID_SETIMA:    "Sétima",
}

# Só envia leads criados a partir desta data (evita reprocessar histórico)
SYNC_DESDE = os.environ.get("DATACRAZY_SYNC_DESDE", "2026-07-15 00:00:00")

# Tag usada como marcador de "já sincronizado" no Odoo
TAG_NAME = os.environ.get("DATACRAZY_TAG", "DataCrazy")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
LIMIT   = int(os.environ.get("LIMIT", "0"))  # 0 = sem limite

# Webhook do DataCrazy aceita 120 req/min → ~0.6s entre envios
DELAY_ENTRE_ENVIOS = 0.6

# ---------------------------------------------------------------------------
# Odoo XML-RPC
# ---------------------------------------------------------------------------

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha na autenticação com o Odoo.")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def kw(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs or {})


def get_or_create_tag(models, uid) -> int:
    """Busca (ou cria) a tag de marcação 'DataCrazy' em crm.tag."""
    existing = kw(models, uid, "crm.tag", "search_read",
                  [[["name", "=", TAG_NAME]]], {"fields": ["id"], "limit": 1})
    if existing:
        return existing[0]["id"]
    tag_id = kw(models, uid, "crm.tag", "create", [{"name": TAG_NAME}])
    log.info(f"Tag '{TAG_NAME}' criada no Odoo: crm.tag#{tag_id}")
    return tag_id


def get_leads_pendentes(models, uid, tag_id: int) -> list[dict]:
    """Leads dos dois pipes, criados após o corte, ainda sem a tag DataCrazy."""
    team_ids = [t for t, url in WEBHOOKS.items() if url]
    if not team_ids:
        raise RuntimeError(
            "Nenhum webhook configurado. Defina DATACRAZY_WEBHOOK_BRANDSPOT "
            "e/ou DATACRAZY_WEBHOOK_SETIMA."
        )
    return kw(models, uid, "crm.lead", "search_read",
              [[
                  ["team_id", "in", team_ids],
                  ["create_date", ">=", SYNC_DESDE],
                  ["tag_ids", "not in", [tag_id]],
              ]],
              {
                  "fields": [
                      "id", "name", "contact_name", "partner_name", "email_from",
                      "phone", "expected_revenue", "description", "create_date",
                      "stage_id", "team_id", "partner_id", "lead_properties",
                  ],
                  "order": "create_date asc",
                  "limit": LIMIT or 0,
              })


def get_partner(models, uid, partner_id: int) -> dict:
    res = kw(models, uid, "res.partner", "read",
             [[partner_id]], {"fields": ["name", "email", "phone"]})
    return res[0] if res else {}


def marcar_sincronizado(models, uid, lead_id: int, tag_id: int):
    kw(models, uid, "crm.lead", "write", [[lead_id], {"tag_ids": [(4, tag_id)]}])


# ---------------------------------------------------------------------------
# Transformação Odoo → payload DataCrazy
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def slugify(label: str) -> str:
    """'Qual o setor da sua empresa?' → 'qual_o_setor_da_sua_empresa'."""
    norm = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    norm = re.sub(r"[^a-zA-Z0-9]+", "_", norm).strip("_").lower()
    return norm or "campo"


def normalizar_telefone(raw: str) -> tuple[str, str]:
    """Retorna (nacional_sem_ddi, completo_com_55). Ex.: '+55 11 9...' → ('119...', '55119...')."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("55") and len(digits) >= 12:
        nacional = digits[2:]
    else:
        nacional = digits
    completo = f"55{nacional}" if nacional else ""
    return nacional, completo


def flatten_properties(props) -> dict:
    """Achata lead_properties do Odoo em {rotulo_slug: valor} para mapear em Campos adicionais."""
    campos = {}
    for p in props or []:
        if not isinstance(p, dict):
            continue
        value = p.get("value")
        if value in (None, False, ""):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v not in (None, False, ""))
        campos[slugify(p.get("string") or p.get("name") or "campo")] = str(value)
    return campos


def build_payload(lead: dict, partner: dict, pipeline: str) -> dict:
    nome_contato = (lead.get("contact_name") or "").strip() or (partner.get("name") or "").strip()
    email = (lead.get("email_from") or "").strip() or (partner.get("email") or "").strip()
    telefone_raw = (lead.get("phone") or "").strip() or (partner.get("phone") or "").strip()
    telefone, telefone_completo = normalizar_telefone(telefone_raw)

    stage = lead.get("stage_id")
    team = lead.get("team_id")

    # Mantém todas as chaves sempre presentes (mesmo vazias) para o mapeamento
    # de campos no DataCrazy nunca quebrar entre um envio e outro.
    return {
        "origem": "Odoo",
        "pipeline": pipeline,
        "odoo_id": lead["id"],
        "negocio": lead.get("name") or "",
        "valor": lead.get("expected_revenue") or 0,
        "nome": nome_contato or lead.get("name") or "",
        "empresa": (lead.get("partner_name") or "").strip(),
        "email": email,
        "telefone": telefone,
        "telefone_completo": telefone_completo,
        "etapa_odoo": stage[1] if isinstance(stage, (list, tuple)) else "",
        "equipe_odoo": team[1] if isinstance(team, (list, tuple)) else "",
        "criado_em": lead.get("create_date") or "",
        "descricao": strip_html(lead.get("description") or "")[:2000],
        "campos": flatten_properties(lead.get("lead_properties")),
    }


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

def enviar_datacrazy(webhook_url: str, payload: dict) -> bool:
    r = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code >= 300:
        log.error(f"DataCrazy respondeu {r.status_code}: {r.text[:300]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Sincronização Odoo → DataCrazy ===")
    log.info(f"Corte de data: create_date >= {SYNC_DESDE} | DRY_RUN={DRY_RUN} | LIMIT={LIMIT or 'sem limite'}")

    for team_id, url in WEBHOOKS.items():
        label = PIPELINE_LABEL.get(team_id, str(team_id))
        log.info(f"Webhook {label} (team {team_id}): {'configurado' if url else 'NÃO configurado — pipe será ignorado'}")

    models, uid = odoo_connect()
    tag_id = get_or_create_tag(models, uid)

    leads = get_leads_pendentes(models, uid, tag_id)
    log.info(f"{len(leads)} lead(s) pendente(s) de sincronização.")
    if not leads:
        log.info("Nada a enviar. Encerrando.")
        return

    enviados = 0
    erros = 0
    for lead in leads:
        team = lead.get("team_id")
        team_id = team[0] if isinstance(team, (list, tuple)) else team
        webhook_url = WEBHOOKS.get(team_id, "")
        pipeline = PIPELINE_LABEL.get(team_id, str(team_id))
        if not webhook_url:
            continue

        try:
            partner = {}
            pid = lead.get("partner_id")
            if pid and (not lead.get("email_from") or not lead.get("phone") or not lead.get("contact_name")):
                partner = get_partner(models, uid, pid[0] if isinstance(pid, (list, tuple)) else pid)

            payload = build_payload(lead, partner, pipeline)

            if DRY_RUN:
                log.info(f"[DRY_RUN] Odoo#{lead['id']} ({pipeline}) → {json.dumps(payload, ensure_ascii=False)}")
                continue

            if enviar_datacrazy(webhook_url, payload):
                marcar_sincronizado(models, uid, lead["id"], tag_id)
                log.info(f"Enviado: Odoo#{lead['id']} | {payload['negocio']} | {pipeline}")
                enviados += 1
            else:
                erros += 1
            time.sleep(DELAY_ENTRE_ENVIOS)
        except Exception as exc:
            log.error(f"Erro no lead Odoo#{lead.get('id')}: {exc}")
            erros += 1

    log.info(f"=== Concluído: {enviados} enviado(s), {erros} erro(s) ===")
    if erros:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
