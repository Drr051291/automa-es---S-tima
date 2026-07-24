# Histórico de contexto — Migração Pipedrive → Odoo (Sétima e BrandSpot)

Documento de contexto para quem for dar manutenção nestas automações.
Última atualização: 2026-07-24.

## O que estamos fazendo

Migração do CRM do **Pipedrive** para o **Odoo**. Duas frentes de automação:

1. **Fluxo contínuo (permanente):** leads novos do **Meta (Lead Ads)** caem numa
   planilha do Google Sheets e são importados para o Odoo a cada 15 min. Depois,
   os negócios novos do Odoo são sincronizados para o **DataCrazy** (webhook por
   pipe), que dispara as automações de WhatsApp.
   - `import_leads_setima.py` → Inbound Sétima (team 16), etapa inicial Lead (68)
   - `import_leads.py` → Campanhas BrandSpot (team 17), etapa inicial Lead (68)
   - `sincronizar_odoo_datacrazy.py` → Odoo → DataCrazy (BrandSpot e Sétima)
   - Leads vindos da **landing page** são responsabilidade de um dev externo e
     **não** passam por este repositório.

2. **Migração pontual do histórico (única, JÁ CONCLUÍDA):** trouxe os deals
   antigos do Pipedrive para o Odoo, com etapa/data corretas.
   - `migrar_pipedrive_setima.py` / `sincronizar_stages_pipedrive_setima.py`
   - `migrar_pipedrive_brandspot.py` / `sincronizar_stages_pipedrive.py`

> **Regra de ouro:** depois da migração concluída, o fluxo contínuo é
> **Meta → Odoo → DataCrazy**. Não se lê mais o Pipedrive.

## O problema que apareceu (e por que voltava sozinho)

Sintoma: após limpar a etapa **Lead** do pipe Sétima, algumas horas depois ela
voltava a acumular **200+ leads** (ver print do Kanban).

Causa-raiz: o importador do Meta (`import_leads_setima.py`) deduplicava **só**
pelos leads que ele mesmo tinha criado — procurava o marcador
`"Lead ID (Meta):"` na descrição e **só entre leads ativos**. Problemas:

- Os negócios **migrados do Pipedrive** têm descrição `"Pipedrive ID:"` (sem o
  marcador do Meta) e guardam e-mail/telefone no `res.partner`. Para o
  importador, cada um desses parecia um lead **novo** → era **recriado** na
  etapa Lead (68) a cada rodada do cron.
- Leads **arquivados** (ganho/perdido) ficavam invisíveis (faltava
  `active in [True, False]`) → também eram recriados.
- Efeito colateral: cada recriação tinha `create_date` novo e sem a tag
  `DataCrazy` → era **reenviada ao DataCrazy**, reonerando o WhatsApp.

Laço que explica o "voltou ao mesmo estágio": ao limpar as cópias do Meta,
sobravam as cópias do Pipedrive (sem o marcador) → no cron seguinte tudo era
recriado de novo. Toda limpeza durava até o próximo ciclo de 15 min.

## O que foi ajustado (este commit)

1. **Deduplicação por identidade** em `import_leads_setima.py` e
   `import_leads.py`: indexa **todos** os leads do pipe (ativos **e**
   arquivados) por **Meta ID, e-mail e telefone** (e-mail/telefone lidos também
   do `res.partner`). Uma linha da planilha só é importada se **nenhuma** dessas
   identidades já existir no Odoo. Isso reconhece os leads migrados do Pipedrive
   e encerra a recriação.

2. **Limpeza definitiva das duplicatas atuais** — `limpar_duplicatas.py`
   reescrito para agrupar por **qualquer** identidade compartilhada (union-find
   por Meta ID / e-mail / telefone) nos times 16 e 17. Assim ele funde a cópia
   do Pipedrive com a cópia do Meta (mesmo e-mail/telefone) e mantém a **mais
   antiga**. Rodar uma vez com `DRY_RUN=true`, conferir, depois `DRY_RUN=false`.

3. **Trava contra ler o Pipedrive de novo** — os 4 scripts que leem o Pipedrive
   agora só executam com `CONFIRMAR_LEITURA_PIPEDRIVE=true` (input `confirmar`
   nos workflows, padrão `false`). Sem isso, saem com aviso e não fazem nada. A
   migração é pontual e já está concluída.

## Passo a passo para estabilizar (rodar uma vez)

1. `Limpar Leads Duplicados no Odoo` com `dry_run=true` → conferir o relatório.
2. Mesmo workflow com `dry_run=false` → remove as duplicatas atuais.
3. Deixar o cron `Importar Leads Sétima` seguir rodando: com a dedup por
   identidade, a etapa Lead não volta a inflar.

## Fluxo permanente (depois de estável)

- **Meta → Odoo:** `import_leads_setima.py` / `import_leads.py` (cron 15 min).
- **Odoo → DataCrazy:** `sincronizar_odoo_datacrazy.py` (cron 15 min).
- **Pipedrive:** não é mais lido. Scripts de migração ficam arquivados atrás da
  trava `CONFIRMAR_LEITURA_PIPEDRIVE`.
