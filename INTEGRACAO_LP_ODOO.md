# Integração das Landing Pages → Odoo CRM

> **Brief de execução.** Especificação completa para conectar os formulários das
> landing pages diretamente ao CRM Odoo, criando o negócio (lead) no funil correto
> no exato momento do envio do formulário — **sem planilha, sem etapa intermediária**:
> campo do formulário → campo do Odoo.
>
> Está escrito para ser executado ponta a ponta. Todos os códigos internos (IDs de
> funil, IDs de etapa e hashes dos campos personalizados) já estão resolvidos. O que
> falta são apenas **as credenciais** (fornecidas à parte, por segurança) e **os nomes
> dos campos no formulário de cada LP** (só você tem).

---

## ⛳ Leia primeiro: são DUAS landing pages = DOIS processos separados

São **dois projetos diferentes**, cada um com sua landing page, seu funil no Odoo e
seu conjunto de campos. **Trate como duas integrações independentes e faça uma de
cada vez:**

1. **Processo A — Sétima** (`setima.cc`) → funil *Inbound Sétima*
2. **Processo B — BrandSpot** (`brandspot.com.br`) → funil *Campanhas BrandSpot*

> ✅ **Recomendação:** implemente e valide o **Processo A** por completo (item a item do
> checklist), confirme que o negócio caiu certo no Odoo, e só então comece o **Processo B**.
> Não misture os dois — os funis e os campos de qualificação são diferentes.

A estrutura do documento:

- **Seções 1–5** → base comum aos dois (leia uma vez): objetivo, arquitetura,
  segurança, credenciais e o contrato da API do Odoo.
- **Seção 6 → Processo A (Sétima)** — autocontido.
- **Seção 7 → Processo B (BrandSpot)** — autocontido.
- **Seção 8** → código de referência (serve para os dois).
- **Seção 9** → o que devolver para validação.

---

## 1. Objetivo

Cada envio de formulário vira, na hora, um **negócio (`crm.lead`)** no Odoo, no funil
e etapa corretos, com o contato e todas as respostas de qualificação já preenchidas.
A partir do momento em que o negócio entra no Odoo, o resto da esteira (espelhar para
o CRM de atendimento por WhatsApp) já está pronto e **não** faz parte desta integração.

---

## 2. Como funciona (arquitetura) — vale para os dois processos

O ponto crítico: **a chave de API do Odoo nunca pode ir para o navegador.** O formulário
**não** fala direto com o Odoo pelo JavaScript da página. Ele envia para um pequeno
endpoint no back-end (função serverless ou rota de API), e é esse endpoint — que guarda
a chave em variável de ambiente — que cria o negócio no Odoo via XML-RPC.

```
┌─────────────────────┐     POST (JSON)      ┌──────────────────────┐    XML-RPC     ┌──────────────┐
│  Formulário da LP    │ ───────────────────▶ │  Endpoint back-end    │ ─────────────▶ │   Odoo CRM   │
│  (Sétima OU BrandSpot)│  nome, email,       │  (serverless/API)     │  authenticate  │              │
│                      │   telefone, respostas│  guarda a ODOO_API_KEY│  + create      │  crm.lead    │
└─────────────────────┘                       └──────────────────────┘                └──────────────┘
        navegador                                   servidor (seguro)                      privado
```

Fluxo dentro do endpoint, para cada envio:

1. Recebe o JSON do formulário.
2. Autentica no Odoo (XML-RPC) e obtém o `uid`.
3. Cria/reaproveita o **contato** (`res.partner`) pelo e-mail.
4. Cria o **negócio** (`crm.lead`) no funil correto, com etapa inicial e os campos
   personalizados preenchidos.
5. Responde `200 OK` para o formulário.

> Você pode ter **um endpoint por LP** (ex.: `/lead/setima` e `/lead/brandspot`) ou um
> só que recebe qual é a landing. O código da seção 8 já é preparado para os dois.

---

## 3. ⚠️ Regra de ouro de segurança

- A **chave de API do Odoo fica só no servidor** (variável de ambiente/secret). Nunca em
  HTML, JS de página, repositório público ou no `<script>` da LP.
- O endpoint deve aceitar requisições **apenas** dos domínios das LPs (CORS restrito a
  `setima.cc` e `brandspot.com.br`).
- Recomendado: honeypot ou reCAPTCHA no formulário para evitar spam criando negócios falsos.

---

## 4. Credenciais necessárias (fornecidas à parte)

Configure como variáveis de ambiente no back-end. **Não recebem valor neste documento** —
peça os valores ao responsável pelo Odoo e guarde como secrets. São as **mesmas** para os
dois processos.

| Variável | O que é |
|---|---|
| `ODOO_URL` | URL da instância, ex.: `https://suaempresa.odoo.com` |
| `ODOO_DB` | Nome do banco de dados |
| `ODOO_USER` | E-mail do usuário de integração |
| `ODOO_API_KEY` | Chave de API desse usuário (Odoo → Preferências → Conta → Chaves de API) |

---

## 5. Contrato da API do Odoo (XML-RPC) — vale para os dois processos

O Odoo expõe dois endpoints XML-RPC:

- `POST {ODOO_URL}/xmlrpc/2/common` → método `authenticate` → devolve o `uid`.
- `POST {ODOO_URL}/xmlrpc/2/object` → método `execute_kw` → executa operações nos modelos.

| Objetivo | Modelo | Método |
|---|---|---|
| Autenticar | `common` | `authenticate(db, user, apikey, {})` |
| Achar contato por e-mail | `res.partner` | `search_read([[["email","=",email]]], {fields:["id"],limit:1})` |
| Criar contato | `res.partner` | `create({name, email, phone})` |
| Criar negócio | `crm.lead` | `create({...})` |

Como os campos personalizados funcionam no `crm.lead`: eles ficam no campo
`lead_properties`, como uma **lista de objetos** `{"name": "<hash>", "value": "<valor>"}`.
O `name` **não** é o rótulo legível — é o **hash interno** da propriedade. Use os hashes
exatos das seções 6 e 7, senão o valor não gruda no campo certo.

Estrutura do negócio a criar (igual nos dois processos, muda só `team_id` e a
qualificação):

```jsonc
{
  "name": "Empresa do Lead",          // partner_name; se vazio, usar o nome da pessoa
  "type": "opportunity",              // sempre
  "team_id": 16,                       // 16 = Sétima | 17 = BrandSpot
  "stage_id": 68,                      // etapa inicial "Lead" (igual nos dois)
  "contact_name": "Nome da Pessoa",
  "email_from": "pessoa@empresa.com",
  "phone": "+55 51 98236-1323",       // formato internacional
  "partner_name": "Empresa do Lead",
  "partner_id": 1234,                  // id do res.partner criado/encontrado
  "description": "Origem: LP Sétima\nEnviado em: 2026-07-17T10:00:00",
  "lead_properties": [                 // só os campos preenchidos (omita vazios)
    { "name": "0c56bb6e8f08dcf2", "value": "Fundador/Sócio" }
  ]
}
```

Deduplicação (igual nos dois): busque `res.partner` por `email`; se existir, reutilize o
`id` (não duplica pessoa). O **negócio** pode ser criado sempre (cada envio é um negócio
novo). Opcional: ignore envios idênticos do mesmo e-mail num intervalo de ~2 min
(anti-duplo-clique).

---

# 🟦 Processo A — Landing Page da Sétima

> Faça este processo primeiro, do começo ao fim, e valide antes de ir para o Processo B.

### A.1 Funil de destino

| Item | Valor — use exatamente |
|---|---|
| Funil (Odoo) | Inbound Sétima |
| `team_id` | **`16`** |
| Etapa inicial | Lead |
| `stage_id` | **`68`** |
| `type` | `"opportunity"` |

### A.2 Campos fixos — formulário da Sétima → Odoo

Preencha a coluna da esquerda com o `name`/`id` real de cada input **da LP da Sétima**.

| Campo no formulário da Sétima | Destino no Odoo |
|---|---|
| `__________` (nome) | `res.partner.name` + `crm.lead.contact_name` |
| `__________` (e-mail) | `res.partner.email` + `crm.lead.email_from` |
| `__________` (telefone/WhatsApp) | `res.partner.phone` + `crm.lead.phone` |
| `__________` (empresa) | `crm.lead.partner_name` (e vira o título `crm.lead.name`) |

> **Telefone:** normalize para o padrão internacional (`+55 51 98236-1323` ou dígitos
> `5551982361323`) antes de gravar — garante o disparo correto do WhatsApp na esteira.

### A.3 Campos de qualificação — formulário da Sétima → hashes

| Campo no formulário da Sétima | Hash de destino (`lead_properties.name`) |
|---|---|
| `__________` (objetivo do projeto com a Sétima) | `bc2f7080491cd41e` |
| `__________` (setor da empresa) | `da57e67b7aa5aa49` |
| `__________` (nº de colaboradores) | `f13a6ff4b0b1b0c8` |
| `__________` (cargo) | `0c56bb6e8f08dcf2` |
| `__________` (investimento mensal em marketing) | `6a48564918fb945e` |
| `__________` (site) | `362c9666b018e4fd` |

> Envie o valor exatamente como o usuário selecionou (ex.: `"menos de R$ 50 milhões/ano"`).
> São campos de texto no Odoo — não precisam casar com opção pré-cadastrada.

### A.4 Checklist de validação — Sétima

- [ ] Endpoint da Sétima no ar, credenciais em variável de ambiente (nada no front).
- [ ] CORS restrito a `setima.cc`.
- [ ] Nomes reais dos campos preenchidos (A.2 e A.3) no código.
- [ ] Envio de teste na LP da Sétima → negócio aparece no funil **Inbound Sétima**,
      etapa **Lead**, com contato + qualificação preenchidos.
- [ ] Telefone gravado em formato internacional (`+55...`).
- [ ] Reenvio com o mesmo e-mail **não** duplica o contato.
- [ ] Abrir o negócio no Odoo → aba de campos personalizados → cada resposta no campo certo.

**➡️ Só depois de todos os itens acima marcados, siga para o Processo B.**

---

# 🟨 Processo B — Landing Page da BrandSpot

> Só comece depois de concluir e validar o Processo A.

### B.1 Funil de destino

| Item | Valor — use exatamente |
|---|---|
| Funil (Odoo) | Campanhas BrandSpot |
| `team_id` | **`17`** |
| Etapa inicial | Lead |
| `stage_id` | **`68`** |
| `type` | `"opportunity"` |

### B.2 Campos fixos — formulário da BrandSpot → Odoo

Preencha a coluna da esquerda com o `name`/`id` real de cada input **da LP da BrandSpot**.

| Campo no formulário da BrandSpot | Destino no Odoo |
|---|---|
| `__________` (nome) | `res.partner.name` + `crm.lead.contact_name` |
| `__________` (e-mail) | `res.partner.email` + `crm.lead.email_from` |
| `__________` (telefone/WhatsApp) | `res.partner.phone` + `crm.lead.phone` |
| `__________` (empresa) | `crm.lead.partner_name` (e vira o título `crm.lead.name`) |

> Mesma regra de telefone internacional do Processo A.

### B.3 Campos de qualificação — formulário da BrandSpot → hashes

| Campo no formulário da BrandSpot | Hash de destino (`lead_properties.name`) |
|---|---|
| `__________` (cargo) | `21787d30494d3a40` |
| `__________` (faturamento anual médio) | `41ce60baaee088c1` |
| `__________` (segmento da empresa) | `188ab74703451402` |
| `__________` (modelo atual de produção de conteúdo) | `b49c6c5c6fc45f8a` |
| `__________` (nº de funcionários) | `881921103e407f69` |
| `__________` (site da empresa) | `834030f02ce53208` |
| `utm_campaign` | `324e2a46af924b86` |
| `utm_medium` | `84f5d4100dc16aa6` |
| `utm_content` | `8a501dfea857d1ef` |
| `utm_source` | `58b355d3cd153bd0` |

> Os 4 últimos são de origem de tráfego (UTMs). Se a LP capturar os UTMs da URL, mapeie-os;
> se não, deixe vazios — são opcionais.

### B.4 Checklist de validação — BrandSpot

- [ ] Endpoint da BrandSpot no ar, credenciais em variável de ambiente (nada no front).
- [ ] CORS restrito a `brandspot.com.br`.
- [ ] Nomes reais dos campos preenchidos (B.2 e B.3) no código.
- [ ] Envio de teste na LP da BrandSpot → negócio aparece no funil **Campanhas BrandSpot**,
      etapa **Lead**, com contato + qualificação preenchidos.
- [ ] Telefone gravado em formato internacional (`+55...`).
- [ ] Reenvio com o mesmo e-mail **não** duplica o contato.
- [ ] Abrir o negócio no Odoo → aba de campos personalizados → cada resposta no campo certo.

---

## 8. Código de referência (serve para os dois processos)

Duas versões equivalentes — use a que combina com seu back-end. Ambas leem as credenciais
de variáveis de ambiente e expõem **uma função por landing page** (mudam só `team_id`,
`stage_id` e o mapa de qualificação). Basta preencher as **chaves do formulário** nos
dois objetos `QUALIFICACAO` com os nomes reais dos seus inputs (as das seções A.3 e B.3).

### 8.1 Node.js (serverless / Express)

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
  setima:    { team_id: 16, stage_id: 68, origem: "LP Sétima" },
  brandspot: { team_id: 17, stage_id: 68, origem: "LP BrandSpot" },
};

// chave = nome do campo NO SEU FORMULÁRIO | valor = hash do Odoo (não mexer nos hashes)
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

async function criarNegocio(landing, form) {   // landing = "setima" | "brandspot"
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

  // 2. qualificação -> lead_properties (omite vazios)
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
  Object.keys(vals).forEach((k) => (vals[k] === "" || vals[k] == null) && delete vals[k]);

  return kw(uid, "crm.lead", "create", [vals]);
}

// handlers HTTP (um por LP)
// app.post("/lead/setima",    async (req,res)=>{ await criarNegocio("setima",    req.body); res.json({ok:true}); });
// app.post("/lead/brandspot", async (req,res)=>{ await criarNegocio("brandspot", req.body); res.json({ok:true}); });
```

### 8.2 Python (Flask / serverless)

```python
import os, datetime, xmlrpc.client

ODOO_URL, ODOO_DB = os.environ["ODOO_URL"], os.environ["ODOO_DB"]
ODOO_USER, ODOO_API_KEY = os.environ["ODOO_USER"], os.environ["ODOO_API_KEY"]

FUNIS = {
    "setima":    {"team_id": 16, "stage_id": 68, "origem": "LP Sétima"},
    "brandspot": {"team_id": 17, "stage_id": 68, "origem": "LP BrandSpot"},
}
# chave = nome do campo NO SEU FORMULÁRIO | valor = hash do Odoo (não mexer nos hashes)
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

def criar_negocio(landing: str, form: dict) -> int:   # landing = "setima" | "brandspot"
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

## 9. O que preciso receber de volta

Para eu validar do lado do Odoo/CRM, **por processo** (primeiro a Sétima, depois a BrandSpot):

1. A **URL do endpoint** da LP (ex.: `/lead/setima`).
2. Confirmação de que rodou o teste do checklist (A.4 / B.4) e o negócio apareceu no funil certo.
3. Se algum campo de qualificação não gravar, me mande **o valor enviado** e **o hash usado** —
   reviso o mapeamento.

> Observação: os hashes e IDs deste documento são específicos desta instância do Odoo. Se um
> funil ou campo personalizado for recriado/renomeado no Odoo, o hash muda e precisa ser
> atualizado aqui — me avise que eu regero a lista.
