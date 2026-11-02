# Wildpatch

**Rewild any patch of land with open biodiversity data and open-weight AI, routed through [Otari](https://otari.ai).**

A school wants to turn a corner of its playground into a wildflower meadow. A residents' group wants to stop mowing a
verge. A student club wants to restore a lakeside strip. They all ask the same questions: *what should we plant here,
what lives here already, how much water will it need, and who can help?*

Drop a pin, describe the patch (optionally add a photo) and Wildpatch produces an evidence-backed plan:

- a **planting palette** chosen only from species *observed growing natively within the survey radius* (iNaturalist),
  each with its photo and observation count;
- **numbers computed in a sandbox, not guessed**: plant and pollinator diversity, native share, plug and shrub counts,
  and month-by-month watering in litres from ten years of ERA5 climate, re-checked locally;
- **local help found on the web**: nurseries, grants and rules, with every link verified against the search results;
- a **12-month plan** timed to the local climate, with a monitoring routine against today's baseline.

It was built for MozFest 2026's *Developers Wilding* hackathon, which is about "wilding" literally.

> Demo video: _link_ · Live app: _link_ · Hackathon branch: `devs-wilding`

## How a run works

Seven open-weight model roles, every call through one Otari service key. vision, survey and scout run in parallel.

```
browser ── POST /api/run (SSE) ──> pipeline.py ──> otari.py ──> Otari (hosted, api.otari.ai)
                                                                  │  guardrails ──> /guard/<token>/validate (this app)
                                                                  │  MCP loop  ──> /mcp (this app: iNaturalist, GBIF, Open-Meteo)
                                                                  │  web search / fetch, code sandbox (Otari built-ins)
                                                                  └> open-weight models

 1 intake     gpt-oss-20b            reads the brief             guardrails: wildlife-protection + content-safety
 2 vision     Gemma 3 27B            reads the site photo
 3 survey     GLM-4.7-flash          Otari-managed MCP loop      6 tools on the Wildpatch Biodiversity MCP server
 4 scout      Kimi K2.5              Otari web search + fetch    links checked against the search results
 5 analysis   Qwen3 Coder 30B        Otari code sandbox          WILDPATCH_RESULT re-computed locally (analysis.py)
 6 plan       wildpatch-planner      Otari routing policy        Llama 3.3 70B, fallback chain, budget tier-down
 7 structure  gpt-oss-20b            palette + quick wins        species checked against the native list
    guards    gpt-oss-safeguard-120b, Llama Guard 4 (called by Otari through our /validate service)
```

## Otari features used (and where)

| Rubric item | How Wildpatch uses it | Code |
|---|---|---|
| Open-weight models, two or more for different purposes | Eight open-weight families in nine roles: gpt-oss, Gemma 3, GLM-4.7, Kimi K2.5, Qwen3 Coder, Llama 3.3, gpt-oss-safeguard and Llama Guard 4, each picked by measuring speed and tool use on hosted Otari. No closed model anywhere. | [`config.py`](wildpatch/config.py) `MODELS` |
| Multiple models serving distinct roles | Vision, tool-using surveyor, coder, web scout, planner, structurer and two safety classifiers, each chosen for its job. | [`pipeline.py`](wildpatch/pipeline.py) |
| Model routing and fallbacks | The plan is requested as the routing policy `wildpatch-planner` (fallback chain). The **Outage drill** toggle sends it through `wildpatch-drill`, whose first model is down, and the UI shows `Otari-Fallback` / `Otari-Attempt-Count`. | `Run._plan`, `Receipt.read_headers` |
| Budgets and spending limits | A ceiling on the service key caps the whole app. The planner policy **tiers down** to a cheaper model once a volunteer has used 80% of their budget. Refusals (`403 budget_exceeded`, `Retry-After`) end the run cleanly. | `Run._stage`, `OtariError` |
| Per-user budgets | The key is an Otari **service key**: each volunteer is an end user (`metadata.user_id` / `user`) with a daily budget of their own. The guest starts on a tiny one via `Otari-End-User-Budget`. The header meters read `GET /api/v1/end-users`. | [`otari.py`](wildpatch/otari.py) |
| MCP integrations | Our own **Wildpatch Biodiversity MCP server** (7 read-only tools over iNaturalist, GBIF and Open-Meteo), registered in the Otari workspace and driven by Otari's managed MCP loop. Calls stream back as `mcp_tool_use` / `mcp_tool_result` blocks. | [`mcp_server.py`](wildpatch/mcp_server.py) |
| Web search or web fetch | The scout uses Otari's search and fetch (`Otari-Web-Search: otari`, `Otari-Web-Fetch: otari`) to find nurseries, grants and rules. | `Run._scout` |
| Code execution | The analyst writes and runs Python in Otari's sandbox. Its stdout is read from `code_execution_tool_result` and re-computed locally, so the UI can say *verified*. | `Run._analysis`, [`analysis.py`](wildpatch/analysis.py) |
| Guardrails | Hosted Otari has no guardrails service of its own, so every guarded request names **ours** (`"url"` on the guardrail entry). Otari calls it before any provider: gpt-oss-safeguard enforces a custom **wildlife-protection policy** (no locating, collecting or harming sensitive species; no prompt injection), and Llama Guard 4 checks content safety. `block` mode fails closed. | [`guard.py`](wildpatch/guard.py) |

Each call also carries `Otari-Tags` (app, run, stage, volunteer) and an `Otari-Conversation-Id`, so a whole run reads as
one session in Otari's dashboard.

### Seeing the gateway's tools run

Chat Completions returns only the final message for gateway-run code execution and MCP. Wildpatch calls those stages on
**`/api/v1/messages`**, where Otari streams `server_tool_use`, `code_execution_tool_result`, `web_search_tool_result`
and (with `anthropic-beta: mcp-client-2025-11-20`) `mcp_tool_use` / `mcp_tool_result` blocks, even for open models. The
UI shows every tool call live, with the code the model wrote and what the sandbox printed.

## Data ethics

- The MCP server never returns coordinates of individual observations. Threatened species are reported by name and
  status only (`sensitive_species`), because they can be harmed by being found.
- The guardrail refuses requests that try to locate, collect or harm wildlife before any model sees them.
- Open data: [iNaturalist](https://www.inaturalist.org) (research-grade observations, native/introduced status per
  country), [GBIF](https://www.gbif.org), [Open-Meteo](https://open-meteo.com) (ERA5) and
  [OpenStreetMap Nominatim](https://nominatim.org).

## Run it

Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # add your Otari service key; see OTARI_SETUP.md
uv run uvicorn wildpatch.app:app --port 8000
```

Otari's cloud has to reach the MCP server and the guardrail service, so set `PUBLIC_URL` to a public https address of the
app: deploy it (`render.yaml` and the `Dockerfile` are included) or run a tunnel such as
`cloudflared tunnel --url http://localhost:8000`. [OTARI_SETUP.md](OTARI_SETUP.md) walks through the dashboard:
service key, budgets, routing policies and the MCP server.

Tests run the whole pipeline against a local stand-in for Otari's wire formats ([`tests/fake_otari.py`](tests/fake_otari.py)),
covering a full run, a guardrail refusal, an exhausted guest budget and the outage drill:

```bash
uv run pytest
```

## License

MIT
