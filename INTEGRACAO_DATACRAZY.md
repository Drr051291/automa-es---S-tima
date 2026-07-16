# Integração Odoo → DataCrazy (novos negócios)

Cada novo cadastro que entra no Odoo nos pipelines **Campanhas BrandSpot** (team 17)
e **Inbound Sétima** (team 16) vira automaticamente um card no DataCrazy, o CRM
que o SDR usa para conversar com os clientes via WhatsApp.

## Arquitetura

A opção mais simples e no padrão do repositório: um script Python que roda no
GitHub Actions a cada 15 minutos, consulta o Odoo via XML-RPC (mesma credencial
das outras automações) e envia cada lead novo, em JSON, para o **webhook de
Entrada de Negócios** do DataCrazy (Configurações → Integrações).

```
Meta Ads → Sheets → Odoo (automações existentes)
                      │
                      │  GitHub Actions (cron 15 min)
                      │  sincronizar_odoo_datacrazy.py
                      ▼
   leads novos (create_date >= corte, sem tag "DataCrazy")
                      │
        ┌─────────────┴──────────────┐
        ▼                            ▼
 Webhook DataCrazy               Webhook DataCrazy
 "Entrada BrandSpot"             "Entrada Sétima"
        │                            │
        ▼                            ▼
 Card no funil do SDR  →  automação de WhatsApp do DataCrazy
```

Por que assim, e não de outro jeito:

- **Sem servidor novo**: reaproveita o GitHub Actions que já roda as demais
  automações (mesmos secrets do Odoo). Nada de Make/n8n/Zapier pago no meio.
- **Sem tocar no Odoo**: não precisa de Studio nem de Ações Automatizadas com
  webhook de saída — o payload do webhook nativo do Odoo é engessado e difícil
  de mapear no DataCrazy; aqui a gente controla o JSON.
- **Duas integrações no DataCrazy (uma por pipe)**: cada webhook pode ter sua
  própria etapa de entrada, tags e automação de WhatsApp, sem lógica condicional.
- **Deduplicação no próprio Odoo**: depois do envio, o lead ganha a tag
  `DataCrazy`. Sem arquivo de estado, sem risco de reenvio, e o SDR enxerga no
  Odoo o que já caiu no CRM.
- **Só leads novos**: o corte `DATACRAZY_SYNC_DESDE` (2026-07-15) impede que o
  histórico migrado do Pipedrive seja despejado no DataCrazy.
- **Rate limit respeitado**: o webhook aceita 120 req/min; o script envia com
  0,6 s de intervalo.

## Passo a passo de configuração

### 1. Criar as duas integrações no DataCrazy

Em **Configurações → Integrações → criar integração** do tipo
**Entrada de Negócios**, crie:

1. `Odoo — BrandSpot`
2. `Odoo — Sétima`

Copie a URL do webhook de cada uma (campo *Webhook* do modal).

### 2. Cadastrar os secrets no GitHub

No repositório, em *Settings → Environments → automações*, adicione:

| Secret | Valor |
|---|---|
| `DATACRAZY_WEBHOOK_BRANDSPOT` | URL do webhook da integração "Odoo — BrandSpot" |
| `DATACRAZY_WEBHOOK_SETIMA` | URL do webhook da integração "Odoo — Sétima" |

Os secrets do Odoo (`ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`) já
existem e são reaproveitados.

### 3. Gerar a amostra para mapear os campos

O aviso **"Wrong json string input"** no painel *Dados recebidos* só significa
que nenhum JSON válido chegou ainda. Para gerar a amostra:

1. Garanta que exista (ou crie) um lead de teste no pipe, criado depois do corte.
2. Rode o workflow **Sincronizar Odoo → DataCrazy** manualmente
   (*Actions → Run workflow*) com `limit = 1`.
3. No DataCrazy, abra a integração e clique em **Receber dados** — o JSON do
   lead aparece no painel e os campos ficam disponíveis nos seletores.

### 4. Mapear os campos no modal da integração

O script envia sempre este JSON (as chaves nunca mudam, mesmo vazias):

```json
{
  "origem": "Odoo",
  "pipeline": "Sétima",
  "odoo_id": 1234,
  "negocio": "Nome do negócio",
  "valor": 5000,
  "nome": "Nome do contato",
  "empresa": "Empresa do lead",
  "email": "contato@empresa.com",
  "telefone": "11999999999",
  "telefone_completo": "5511999999999",
  "etapa_odoo": "Lead",
  "equipe_odoo": "Inbound Sétima",
  "criado_em": "2026-07-15 10:00:00",
  "descricao": "Campanha, formulário, anúncio…",
  "campos": {
    "qual_o_setor_da_sua_empresa": "…",
    "qual_seu_cargo": "…"
  }
}
```

Mapeamento sugerido:

| Aba | Campo DataCrazy | Campo do JSON |
|---|---|---|
| Perfil | Nome | `nome` |
| Perfil | Empresa | `empresa` |
| Perfil | Email | `email` |
| Perfil | Telefone | `telefone` (DDI já fica no seletor 🇧🇷 +55) |
| Negócios | Nome | `negocio` |
| Negócios | Preço | `valor` |
| Automação | Etapa | etapa de entrada do funil do SDR (ex.: *Lead*) |
| Automação | Tags | ex.: `Odoo` + `BrandSpot` ou `Sétima` |
| Campos adicionais | (criar conforme necessidade) | `etapa_odoo`, `criado_em`, `descricao`, `odoo_id`, `campos.qual_o_setor_da_sua_empresa`, … |

> As perguntas de qualificação do formulário (Sétima) chegam dentro de
> `campos.*` com o rótulo da pergunta em snake_case — dá para mapear cada uma
> num campo adicional do DataCrazy.

Deixe **INTEGRAÇÃO ATIVA** ligada e clique em **Confirmar** nas duas integrações.

### 5. Ligar a automação de WhatsApp no DataCrazy

Como o card entra sempre na etapa definida na aba *Automação*, basta criar no
DataCrazy (manualmente, como você já faz) uma automação com gatilho
**"negócio criado"** ou **"entrou na etapa X"** desse funil para disparar a
primeira mensagem de WhatsApp ao lead. O SDR assume a conversa a partir daí.

### 6. Deixar rodando

O workflow roda sozinho a cada 15 minutos. Não precisa fazer mais nada:
todo lead novo dos dois pipes cai no DataCrazy em até 15 min.

## Operação e ajustes

- **Simular sem enviar**: rode o workflow manual com `dry_run = true` — loga os
  payloads sem postar nem marcar a tag.
- **Reenviar um lead**: remova a tag `DataCrazy` do lead no Odoo; ele entra no
  próximo ciclo.
- **Mudar a data de corte**: edite `DATACRAZY_SYNC_DESDE` no workflow
  (`.github/workflows/sincronizar_odoo_datacrazy.yml`).
- **Falha de envio**: o lead não recebe a tag e é retentado automaticamente na
  próxima execução; o run do Actions termina em erro para ficar visível.
