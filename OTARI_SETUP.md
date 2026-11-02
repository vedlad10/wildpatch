# Setting up Otari for Wildpatch

Everything below happens in the [otari.ai](https://otari.ai) dashboard. It takes about 15 minutes.

## 1. Account and region

Sign up at otari.ai with the email you registered on DoraHacks. Note your region:

| Region | `OTARI_BASE_URL` |
|---|---|
| US (default) | `https://api.otari.ai/api/v1` |
| EU | `https://eu.api.otari.ai/api/v1` |

A key from one region gets `421` on the other.

## 2. Tools in the workspace

In your workspace, set both of these to **Allowed**:

- **Tools → Code execution**. Leave "Who runs provider code tools" on the default.
- **Tools → Web search**, under Web access.

## 3. Budgets

Go to **Organization → Spend & Budgets → Add budget** and create three budgets:

| Name | Amount | Period | Used for |
|---|---|---|---|
| `wildpatch-volunteer` | $0.50 | Daily | each volunteer (end user) |
| `wildpatch-guest` | $0.02 | Daily | the guest persona, so the demo can show a budget running out |
| `wildpatch-app` | $5.00 | Monthly | the whole app (ceiling on the key) |

Note the id of `wildpatch-guest` and put it in `.env` as `GUEST_BUDGET_ID`.

## 4. Service key

Go to **API Keys → Create Key → Advanced** and turn on **Service key**. Then:

- **End-user budgets**: choose `wildpatch-volunteer` as the default and also allow `wildpatch-guest`.
- **Key budget / ceiling**: choose `wildpatch-app`. If the form has no such field, add it under Spend & Budgets
  → **Add ceiling** for the workspace instead.

Copy the key (`otk_…`) into `.env` as `OTARI_API_KEY`.

## 5. Routing policies

Go to **Routing → Policies → Create policy**.

**`wildpatch-planner`**: start it as a **Fallback** policy.

- Main model: `meta/llama-3.3-70b-instruct` (about 140 tokens/s on hosted Otari, against 15 for Qwen3 235B)
- If unavailable, use: `alibaba/qwen3-235b-a22b-instruct-2507`, then `deepseek/deepseek-v3.2`
- Then open the policy and, in its **Tier down on budget** section, switch to
  `alibaba/qwen3-30b-a3b-instruct-2507` at **80%** of the budget used.

**`wildpatch-drill`**: also a **Fallback** policy, for the outage drill.

- Main model: `moonshotai/kimi-k3-spot`. It is listed in the catalog but not deployed on hosted otari.ai (it
  answers 404 "Model not found… not deployed", checked 2026-11-02), so the first attempt fails and Otari falls back.
  `z-ai/glm-5.3-spot` (currently a provider 503) works the same way.
- If unavailable, use: `meta/llama-3.3-70b-instruct`

## 6. Public URL, then the MCP server

Otari's cloud must reach the app over https:

- **Deploy**: push the repo to GitHub, then on [Render](https://render.com) choose New → Blueprint and pick the
  repo (`render.yaml`). Set `OTARI_API_KEY`. Render generates `MCP_TOKEN` and `GUARD_TOKEN` and provides the URL.
- **Or tunnel**: run `cloudflared tunnel --url http://localhost:8000` and put the `https://…trycloudflare.com` URL in
  `.env` as `PUBLIC_URL`.

Then go to **Tools → MCP servers → Add MCP server** and fill in:

- Name: `wildpatch-biodiversity`
- URL: `https://<your app>/mcp`
- Authorization token: the value of `MCP_TOKEN`
- Purpose hint: `Open biodiversity and climate data for the survey site`

Put the server's id in `.env` as `OTARI_MCP_SERVER_ID`. Without it, the app sends the server inline with each
request, which also works.

## 7. Check

```bash
uv run python scripts/check_otari.py
```

It confirms the key, each model role, the tools, the service key's end users and the public URL.
