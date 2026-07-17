# Integração das Landing Pages → Odoo CRM

> **Brief de execução.** Este documento é a especificação completa para conectar
> os formulários das duas landing pages diretamente ao CRM Odoo, criando o
> negócio (lead) no funil correto no exato momento do envio do formulário —
> **sem planilha, sem etapa intermediária**: campo do formulário → campo do Odoo.
>
> Está escrito para ser executado ponta a ponta. Todos os códigos internos
> (IDs de funil, IDs de etapa e hashes dos campos personalizados) já estão
> resolvidos e listados abaixo. O que falta são apenas **as credenciais** (fornecidas
> à parte, por segurança) e **os nomes dos campos no seu formulário** (só você tem).

---

## 1. Objetivo

Cada envio de formulário nas landing pages vira, na hora, um **negócio (`crm.lead`)**
no Odoo, no funil e etapa corretos, com o contato e todas as respostas de
qualificação já preenchidas nos campos personalizados.

| Landing page | Funil de destino no Odoo |
|---|---|
| **Sétima** (`setima.cc`) | Inbound Sétima |
| **BrandSpot** (`brandspot.com.br`) | Campanhas BrandSpot |

A partir do momento em que o negócio entra no Odoo, o restante já está pronto e
não é responsabilidade desta integração (o negócio é espelhado para o CRM de
atendimento por WhatsApp por uma automação separada).

---

## 2. Como funciona (arquitetura)

O ponto crítico: **a chave de API do Odoo nunca pode ir para o navegador.**
Portanto o formulário **não** fala direto com o Odoo pelo JavaScript da página.
Ele envia para um pequeno endpoint no back-end (função serverless ou rota de
API), e é esse endpoint — que guarda a chave em variável de ambiente — que cria
o negócio no Odoo via XML-RPC.

```
┌─────────────────────┐     POST (JSON)      ┌──────────────────────┐    XML-RPC     ┌──────────────┐
│  Formulário da LP    │ ───────────────────▶ │  Endpoint back-end    │ ─────────────▶ │   Odoo CRM   │
│  (Sétima/BrandSpot)  │   nome, email,       │  (serverless/API)     │  authenticate  │              │
│                      │   telefone, respostas│  guarda a ODOO_API_KEY│  + create      │  crm.lead    │
└─────────────────────┘                       └──────────────────────┘                └──────────────┘
        navegador                                   servidor (seguro)                      privado
```

Fluxo dentro do endpoint, para cada envio:

1. Recebe o JSON do formulário.
2. Autentica no Odoo (XML-RPC) e obtém o `uid`.
3. Cria/reaproveita o **contato** (`res.partner`) pelo e-mail.
4. Cria o **negócio** (`crm.lead`) no funil correto, com etapa inicial e os
   campos personalizados preenchidos.
5. Responde `200 OK` para o formulário.

---

## 3. ⚠️ Regra de ouro de segurança

- A **chave de API do Odoo fica só no servidor** (variável de ambiente/secret).
  Nunca em HTML, JS de página, repositório público ou no `<script>` da LP.
- O endpoint deve aceitar requisições **apenas** dos domínios das LPs (CORS
  restrito a `setima.cc` e `brandspot.com.br`).
- Recomendado: um honeypot ou reCAPTCHA no formulário para evitar spam criando
  negócios falsos.

---

## 4. Credenciais necessárias (fornecidas à parte)

Configure como variáveis de ambiente no back-end. **Não recebem valor neste
documento** — peça os valores ao responsável pelo Odoo e guarde como secrets.

| Variável | O que é |
|---|---|
| `ODOO_URL` | URL da instância, ex.: `https://suaempresa.odoo.com` |
| `ODOO_DB` | Nome do banco de dados |
| `ODOO_USER` | E-mail do usuário de integração |
| `ODOO_API_KEY` | Chave de API desse usuário (Odoo → Preferências → Conta → Chaves de API) |

---

## 5. Contrato da API do Odoo (XML-RPC)

O Odoo expõe dois endpoints XML-RPC:

- `POST {ODOO_URL}/xmlrpc/2/common` → método `authenticate` → devolve o `uid`.
- `POST {ODOO_URL}/xmlrpc/2/object` → método `execute_kw` → executa operações
  (`create`, `search_read`, etc.) nos modelos.

Operações usadas nesta integração:

| Objetivo | Modelo | Método |
|---|---|---|
| Autenticar | `common` | `authenticate(db, user, apikey, {})` |
| Achar contato por e-mail | `res.partner` | `search_read([[["email","=",email]]], {fields:["id"],limit:1})` |
| Criar contato | `res.partner` | `create({name, email, phone})` |
| Criar negócio | `crm.lead` | `create({...})` |

---

## 6. Mapa dos funis (IDs internos)

Estes IDs identificam o funil de destino e a etapa inicial. **Use exatamente estes valores.**

| Landing page | Funil (Odoo) | `team_id` | Etapa inicial | `stage_id` |
|---|---|:---:|---|:---:|
| **Sétima** | Inbound Sétima | **`16`** | Lead | **`68`** |
| **BrandSpot** | Campanhas BrandSpot | **`17`** | Lead | **`68`** |

> `type` do `crm.lead` deve ser sempre `"opportunity"` (para entrar como negócio
> no funil, não como lead solto).

---

## 7. Mapa dos campos personalizados (hashes)

No Odoo, os campos personalizados de `crm.lead` são gravados no campo
`lead_properties`, como uma **lista de objetos** `{"name": "<hash>", "value": "<valor>"}`.
O `name` **não** é o rótulo legível — é o **hash interno** da propriedade. Use os
hashes exatos abaixo, senão o valor não gruda no campo certo.

### 7.1 BrandSpot (`team_id = 17`)

| Campo no Odoo (rótulo) | `name` (hash) — use exatamente |
|---|---|
| Qual o seu cargo? | `21787d30494d3a40` |
| Faturamento anual médio | `41ce60baaee088c1` |
| Segmento da sua empresa | `188ab74703451402` |
| Modelo atual de produção de conteúdo | `b49c6c5c6fc45f8a` |
| Número de funcionários | `881921103e407f69` |
| Site da sua empresa | `834030f02ce53208` |
| Campanha (origem) | `324e2a46af924b86` |
| Conjunto de anúncios / mídia | `84f5d4100dc16aa6` |
| Anuncio / criativo | `8a501dfea857d1ef` |
| Plataforma / canal | `58b355d3cd153bd0` |

> Os 4 últimos vinham do Meta Ads. Vindo da LP, use-os para os **UTMs**
> (`utm_campaign` → Campanha, `utm_medium` → mídia, `utm_content` → criativo,
> `utm_source` → canal). Se preferir, deixe-os vazios — são opcionais.

### 7.2 Sétima (`team_id = 16`)

| Campo no Odoo (rótulo) | `name` (hash) — use exatamente |
|---|---|
| Qual é o principal objetivo do seu projeto com a Sétima? | `bc2f7080491cd41e` |
| Qual o setor da sua empresa | `da57e67b7aa5aa49` |
| Quantos colaboradores sua empresa tem aproximadamente? | `f13a6ff4b0b1b0c8` |
| Qual seu cargo? | `0c56bb6e8f08dcf2` |
| Investimento mensal total em marketing | `6a48564918fb945e` |
| Site | `362c9666b018e4fd` |

---

## 8. Mapeamento formulário → Odoo (PREENCHER)

Os campos fixos do negócio ficam nos campos nativos do `crm.lead`/`res.partner`.
Os campos de qualificação vão em `lead_properties` pelos hashes da seção 7.

**Sua parte:** preencher a coluna "Campo no seu formulário" com o `name`/`id`
real de cada input das suas LPs (só você tem essa informação).

### 8.1 Campos fixos (os dois formulários)

| Campo no seu formulário | Destino no Odoo | Onde |
|---|---|---|
| `__________` (nome) | `res.partner.name` + `crm.lead.contact_name` | contato |
| `__________` (e-mail) | `res.partner.email` + `crm.lead.email_from` | contato |
| `__________` (telefone/WhatsApp) | `res.partner.phone` + `crm.lead.phone` | contato |
| `__________` (empresa) | `crm.lead.partner_name` | negócio |
| — | `crm.lead.name` (título do negócio) | usar a empresa; se vazia, o nome |

> **Telefone:** normalize para o padrão internacional antes de gravar, ex.
> `+55 51 98236-1323` (ou só dígitos `5551982361323`). Isso garante o disparo
> correto do WhatsApp no passo seguinte da esteira.

### 8.2 Qualificação — BrandSpot

| Campo no seu formulário | Hash de destino (`lead_properties.name`) |
|---|---|
| `__________` (cargo) | `21787d30494d3a40` |
| `__________` (faturamento anual) | `41ce60baaee088c1` |
| `__________` (segmento) | `188ab74703451402` |
| `__________` (produção de conteúdo) | `b49c6c5c6fc45f8a` |
| `__________` (nº de funcionários) | `881921103e407f69` |
| `__________` (site) | `834030f02ce53208` |
| `utm_campaign` | `324e2a46af924b86` |
| `utm_medium` | `84f5d4100dc16aa6` |
| `utm_content` | `8a501dfea857d1ef` |
| `utm_source` | `58b355d3cd153bd0` |

### 8.3 Qualificação — Sétima

| Campo no seu formulário | Hash de destino (`lead_properties.name`) |
|---|---|
| `__________` (objetivo do projeto) | `bc2f7080491cd41e` |
| `__________` (setor) | `da57e67b7aa5aa49` |
| `__________` (nº de colaboradores) | `f13a6ff4b0b1b0c8` |
| `__________` (cargo) | `0c56bb6e8f08dcf2` |
| `__________` (investimento em marketing) | `6a48564918fb945e` |
| `__________` (site) | `362c9666b018e4fd` |

> Envie o **valor exatamente como o usuário selecionou** (ex.:
> `"menos de R$ 50 milhões/ano"`). São campos de texto no Odoo — não precisam
> casar com opção pré-cadastrada.

---

## 9. Estrutura do negócio a criar (`crm.lead`)

Objeto final montado pelo endpoint antes do `create`:

```jsonc
{
  "name": "Empresa do Lead",          // partner_name; se vazio, usar o nome da pessoa
  "type": "opportunity",              // sempre
  "team_id": 17,                       // 17 = BrandSpot | 16 = Sétima
  "stage_id": 68,                      // etapa inicial "Lead" (igual nos dois)
  "contact_name": "Nome da Pessoa",
  "email_from": "pessoa@empresa.com",
  "phone": "+55 51 98236-1323",
  "partner_name": "Empresa do Lead",
  "partner_id": 1234,                  // id do res.partner criado/encontrado
  "description": "Origem: LP Sétima\nEnviado em: 2026-07-17T10:00:00\nUTM: ...",
  "lead_properties": [
    { "name": "0c56bb6e8f08dcf2", "value": "Fundador/Sócio" },
    { "name": "da57e67b7aa5aa49", "value": "Tecnologia e Telecom" }
    // ... demais campos da seção 8, omitindo os vazios
  ]
}
```

Regras:
- Omita do `lead_properties` qualquer campo que veio vazio.
- Use `description` para guardar origem (`LP Sétima`/`LP BrandSpot`), data/hora do
  envio e os UTMs — serve de rastro e não custa nada.

---

## 10. Deduplicação

- **Contato:** antes de criar, busque `res.partner` por `email`. Se existir,
  reutilize o `id` (não duplica pessoa). Se não, crie.
- **Negócio:** cada envio de formulário é um negócio novo — pode criar sempre.
- **Anti-duplo-clique (opcional):** ignore envios idênticos (mesmo e-mail) num
  intervalo curto, ex. 2 minutos, para não gerar dois negócios num toque duplo.

---

## 11. Implementação de referência

Duas versões equivalentes — use a que combina com seu back-end. Ambas leem as
credenciais de variáveis de ambiente e expõem **uma função por landing page**
(mudam só `team_id` e o mapa de qualificação).

### 11.1 Node.js (serverless / Express)

```js
// npm i xmlrpc
const xmlrpc = require("xmlrpc");

const { ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY } = process.env;

const common = xmlrpc.createSecureClient({ url: `${ODOO_URL}/xmlrpc/2/common` });
const object = xmlrpc.createSecureClient({ url: `${ODOO_URL}/xmlrpc/2/object` });

const call = (client, method, params) =>
  new Promise((res, rej) =>
    client.methodCall(method, params, (e, v) => (e ? rej(e) : res(v)))
  );

async function authenticate() {
  const uid = await call(common, "authenticate", [ODOO_DB, ODOO_USER, ODOO_API_KEY, {}]);
  if (!uid) throw new Error("Falha de autenticação no Odoo");
  return uid;
}

const kw = (uid, model, method, args, kwargs = {}) =>
  call(object, "execute_kw", [ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs]);

// --- CONFIG POR LANDING PAGE ------------------------------------------------
const FUNIS = {
  setima:   { team_id: 16, stage_id: 68, origem: "LP Sétima" },
  brandspot:{ team_id: 17, stage_id: 68, origem: "LP BrandSpot" },
};

// mapa: chave do formulário -> hash do Odoo (preencher as chaves do SEU form)
const QUALIFICACAO = {
  setima: {
    objetivo:      "bc2f7080491cd41e",
    setor:         "da57e67b7aa5aa49",
    colaboradores: "f13a6ff4b0b1b0c8",
    cargo:         "0c56bb6e8f08dcf2",
    investimento:  "6a48564918fb945e",
    site:          "362c9666b018e4fd",
  },
  brandspot: {
    cargo:         "21787d30494d3a40",
    faturamento:   "41ce60baaee088c1",
    segmento:      "188ab74703451402",
    conteudo:      "b49c6c5c6fc45f8a",
    funcionarios:  "881921103e407f69",
    site:          "834030f02ce53208",
    utm_campaign:  "324e2a46af924b86",
    utm_medium:    "84f5d4100dc16aa6",
    utm_content:   "8a501dfea857d1ef",
    utm_source:    "58b355d3cd153bd0",
  },
};
// ---------------------------------------------------------------------------

async function criarNegocio(landing, form) {
  const cfg = FUNIS[landing];
  const uid = await authenticate();

  // 1. contato (reaproveita por e-mail)
  let partnerId;
  if (form.email) {
    const found = await kw(uid, "res.partner", "search_read",
      [[["email", "=", form.email]]], { fields: ["id"], limit: 1 });
    partnerId = found[0]?.id;
  }
  if (!partnerId) {
    partnerId = await kw(uid, "res.partner", "create",
      [{ name: form.nome || "Contato LP", email: form.email, phone: form.telefone }]);
  }

  // 2. campos de qualificação -> lead_properties (omite vazios)
  const props = Object.entries(QUALIFICACAO[landing])
    .filter(([campo]) => form[campo])
    .map(([campo, hash]) => ({ name: hash, value: String(form[campo]).trim() }));

  // 3. negócio
  const vals = {
    name: form.empresa || form.nome || `Lead ${cfg.origem}`,
    type: "opportunity",
    team_id: cfg.team_id,
    stage_id: cfg.stage_id,
    contact_name: form.nome,
    email_from: form.email,
    phone: form.telefone,
    partner_name: form.empresa,
    partner_id: partnerId,
    description: `Origem: ${cfg.origem}\nEnviado em: ${new Date().toISOString()}`,
    lead_properties: props,
  };
  // remove chaves vazias
  Object.keys(vals).forEach((k) => (vals[k] === "" || vals[k] == null) && delete vals[k]);

  const leadId = await kw(uid, "crm.lead", "create", [vals]);
  return leadId;
}

// exemplo de handler HTTP (Express)
// app.post("/lead/setima",    async (req,res)=>{ await criarNegocio("setima",    req.body); res.json({ok:true}); });
// app.post("/lead/brandspot", async (req,res)=>{ await criarNegocio("brandspot", req.body); res.json({ok:true}); });
```

### 11.2 Python (Flask / serverless)

```python
import os, datetime, xmlrpc.client

ODOO_URL, ODOO_DB = os.environ["ODOO_URL"], os.environ["ODOO_DB"]
ODOO_USER, ODOO_API_KEY = os.environ["ODOO_USER"], os.environ["ODOO_API_KEY"]

FUNIS = {
    "setima":    {"team_id": 16, "stage_id": 68, "origem": "LP Sétima"},
    "brandspot": {"team_id": 17, "stage_id": 68, "origem": "LP BrandSpot"},
}
QUALIFICACAO = {
    "setima": {
        "objetivo": "bc2f7080491cd41e", "setor": "da57e67b7aa5aa49",
        "colaboradores": "f13a6ff4b0b1b0c8", "cargo": "0c56bb6e8f08dcf2",
        "investimento": "6a48564918fb945e", "site": "362c9666b018e4fd",
    },
    "brandspot": {
        "cargo": "21787d30494d3a40", "faturamento": "41ce60baaee088c1",
        "segmento": "188ab74703451402", "conteudo": "b49c6c5c6fc45f8a",
        "funcionarios": "881921103e407f69", "site": "834030f02ce53208",
        "utm_campaign": "324e2a46af924b86", "utm_medium": "84f5d4100dc16aa6",
        "utm_content": "8a501dfea857d1ef", "utm_source": "58b355d3cd153bd0",
    },
}

def _connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Falha de autenticação no Odoo")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid

def criar_negocio(landing: str, form: dict) -> int:
    cfg = FUNIS[landing]
    models, uid = _connect()

    partner_id = None
    if form.get("email"):
        found = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "res.partner",
            "search_read", [[["email", "=", form["email"]]]], {"fields": ["id"], "limit": 1})
        partner_id = found[0]["id"] if found else None
    if not partner_id:
        partner_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "res.partner", "create",
            [{"name": form.get("nome") or "Contato LP",
              "email": form.get("email"), "phone": form.get("telefone")}])

    props = [{"name": h, "value": str(form[c]).strip()}
             for c, h in QUALIFICACAO[landing].items() if form.get(c)]

    vals = {
        "name": form.get("empresa") or form.get("nome") or f"Lead {cfg['origem']}",
        "type": "opportunity", "team_id": cfg["team_id"], "stage_id": cfg["stage_id"],
        "contact_name": form.get("nome"), "email_from": form.get("email"),
        "phone": form.get("telefone"), "partner_name": form.get("empresa"),
        "partner_id": partner_id,
        "description": f"Origem: {cfg['origem']}\nEnviado em: {datetime.datetime.now().isoformat()}",
        "lead_properties": props,
    }
    vals = {k: v for k, v in vals.items() if v not in (None, "", 0)}
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, "crm.lead", "create", [vals])
```

---

## 12. Checklist de validação

- [ ] Variáveis de ambiente configuradas no servidor (nada de chave no front).
- [ ] CORS restrito aos domínios das LPs.
- [ ] Nomes reais dos campos do formulário preenchidos na seção 8 e no mapa de código.
- [ ] Envio de teste na **Sétima** → negócio aparece no funil **Inbound Sétima**,
      etapa **Lead**, com contato + qualificação preenchidos.
- [ ] Envio de teste na **BrandSpot** → negócio aparece no funil **Campanhas
      BrandSpot**, etapa **Lead**, idem.
- [ ] Telefone gravado em formato internacional (`+55...`).
- [ ] Reenvio com o mesmo e-mail **não** duplica o contato.
- [ ] Campos de qualificação caíram no campo certo (conferir abrindo o negócio →
      aba de campos personalizados).

---

## 13. O que preciso receber de volta

Para eu validar do lado do Odoo/CRM:

1. A **URL do endpoint** de cada LP (ex.: `/lead/setima`, `/lead/brandspot`).
2. Confirmação de que rodou o teste da seção 12 e os negócios apareceram.
3. Se algum campo de qualificação não gravar, me mande **o valor enviado** e
   **o hash usado** — reviso o mapeamento.

> Observação: os hashes e IDs deste documento são específicos desta instância do
> Odoo. Se o funil ou algum campo personalizado for recriado/renomeado no Odoo, o
> hash muda e precisa ser atualizado aqui — me avise que eu regero a lista.
