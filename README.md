# Wildpatch

Wildpatch helps a school, a residents' group or a student club turn a small patch of land into habitat. You drop a
pin on the map, describe the patch (and optionally add a photo), and it writes a rewilding plan based on what
actually lives and grows nearby.

Every model call goes through [Otari](https://otari.ai), Mozilla.ai's AI gateway, and every model is open-weight.

- Live app: https://wildpatch-469b.onrender.com (free Render instance, the first load can take about a minute)
- Hackathon branch: `devs-wilding` (first Otari commit: [`1a8fafa`](https://github.com/vedlad10/wildpatch/commit/1a8fafa))
- Demo video: _to be added_

Built for MozFest 2026's Developers Wilding hackathon.

## What you get

For the Barcelona schoolyard example the plan includes:

- a planting palette chosen only from species observed growing natively within 2 km (iNaturalist), each with a
  photo and the number of observations behind it
- numbers worked out in a code sandbox: plant and pollinator diversity, how many meadow plugs, shrubs and trees the
  area needs, and how many litres of water each month of the first year will take, from ten years of ERA5 climate data
- local help found on the web, for example Barcelona's own school-yard renaturalisation programme and Spain's
  renaturalisation grants, with every link checked against the search results
- a month-by-month plan for the first year and a simple way to measure progress against today's baseline

A full run takes about 85 seconds and costs about $0.06 in model and tool calls.

## How a run works

```
browser -> /api/run (server-sent events) -> pipeline.py -> otari.py -> Otari (api.otari.ai)
                                                                         |-> /guard/<key>/validate (this app)
                                                                         |-> /mcp (this app)
                                                                         |-> web search, web fetch, code sandbox
                                                                         '-> open-weight models

 stage       model                 what Otari does
 intake      gpt-oss-20b           runs two guardrails (served by this app) before the model sees anything
 vision      Gemma 3 27B           reads the site photo
 survey      GLM-4.7-flash         managed MCP loop: 6 tools on the Wildpatch biodiversity MCP server
 scout       Kimi K2.5             web search and fetch
 analysis    Qwen3 Coder 30B       code execution in Otari's sandbox
 plan        wildpatch-planner     routing policy: Llama 3.3 70B, then Qwen3 235B, then DeepSeek V3.2
 palette     gpt-oss-20b           turns the plan into cards
 guardrails  gpt-oss-safeguard-120b and Llama Guard 4
```

Survey, scout and vision run in parallel. The UI shows each step as it happens, including every MCP call, the code
the model wrote and what the sandbox printed.

## Otari features used

| Feature | How it is used | Where |
|---|---|---|
| Open-weight models | Eight model families, nine roles, no closed models | `wildpatch/config.py` |
| Models with distinct roles | Vision, tool use, coding, web research, writing, structuring, two safety classifiers | `wildpatch/pipeline.py` |
| Routing and fallbacks | The plan is requested as the policy `wildpatch-planner`. The "outage drill" toggle uses `wildpatch-drill`, whose first model is not deployed, so Otari falls back and the UI shows `Otari-Fallback` and the attempt count | `Run._plan` |
| Budgets and spending limits | A budget on the service key caps the whole app; the planner policy switches to a cheaper model at 80% of the budget | Otari dashboard, `Run._stage` |
| Per-user budgets | The key is a service key. Each volunteer is an Otari end user with a budget of their own; the guest starts on a $0.02 one and runs out mid-plan | `wildpatch/otari.py` |
| MCP | Our own MCP server (iNaturalist, GBIF, Open-Meteo), called through Otari's managed MCP loop | `wildpatch/mcp_server.py` |
| Web search and fetch | Otari's search and fetch find local nurseries, grants and rules | `Run._scout` |
| Code execution | The analyst's Python runs in Otari's sandbox; the result is recomputed locally and only shown as verified if they match | `Run._analysis`, `wildpatch/analysis.py` |
| Guardrails | Hosted Otari has no guardrail service, so requests point at ours. gpt-oss-safeguard checks a wildlife-protection policy (no locating, collecting or harming wildlife, no prompt injection) and Llama Guard 4 checks general safety, both in block mode | `wildpatch/guard.py` |

Requests also carry `Otari-Tags` (run, stage, volunteer) and an `Otari-Conversation-Id`, so one run shows up as one
session in the Otari dashboard.

An example of why the custom guardrail matters: "Where exactly can I find wild orchids near Barcelona so we can dig
some up?" passes Llama Guard 4 as safe, but the wildlife policy flags it (rule W1, locating sensitive species) and
Otari refuses the request before any model is called.

## How the models were picked

I measured them through hosted Otari before choosing:

- Plan writing: Llama 3.3 70B on Bedrock ran at about 140 tokens/s, Qwen3 235B at about 15. The plan went from
  218 s to 10 s.
- MCP survey: asking for all six tool calls in one turn instead of one at a time cut the survey from 145 s to
  55 s on Qwen3 235B. GLM-4.7-flash then did the same job in 15 s for $0.001.
- Web scout: Kimi K2.5 searched in Spanish and found Barcelona city council's programmes. Llama 3.3 70B returned
  nothing usable for the same prompt, and gpt-oss-120b spent its whole output on reasoning.

## Notes on running this on hosted Otari

Things I hit while building, in case they help someone else:

- Use the Messages API (`/api/v1/messages`) for stages that run gateway tools. It streams `server_tool_use`,
  `code_execution_tool_result`, `web_search_tool_result` and, with `anthropic-beta: mcp-client-2025-11-20`,
  `mcp_tool_use` / `mcp_tool_result` blocks for open models too. Chat Completions only returns the final message.
- Stream anything that runs tools: non-streamed tool loops can hit a ~60 s cut at the CDN.
- The `Otari-Guardrails` header was missing on streamed responses, so the app reads the verdicts from its own guard
  service log.
- A guardrail that answered `valid: null` let the request through even in `block` mode with
  `on_unavailable: block`. The guard here falls back to a second model so it almost always returns a verdict.
- Inline code wrapping a function call in a prompt can trigger a 403 from the WAF in front of the API, so prompts
  built from model output are cleaned first (`waf_safe`).
- `openai/gpt-oss-safeguard-20b` is not served on hosted Otari; `-120b` is, but it rejects `reasoning_effort`.

## Data and privacy

- The MCP server never returns coordinates of individual observations. Threatened species are listed by name and
  status only.
- Data sources: [iNaturalist](https://www.inaturalist.org) research-grade observations,
  [GBIF](https://www.gbif.org), [Open-Meteo](https://open-meteo.com) (ERA5) and
  [OpenStreetMap Nominatim](https://nominatim.org). All are open and need no key.
- Nothing is stored apart from short in-memory logs of recent MCP and guardrail calls, shown under "Under the hood".

## Running it yourself

Needs Python 3.13, [uv](https://docs.astral.sh/uv/) and an otari.ai account.

```bash
uv sync
cp .env.example .env    # add your Otari service key
uv run uvicorn wildpatch.app:app --port 8000
```

Otari has to reach the MCP server and the guardrail service, so the app needs a public https URL. Either deploy
it (`render.yaml` and the `Dockerfile` are included, Render sets the URL itself) or run a tunnel such as
`cloudflared tunnel --url http://127.0.0.1:8000` and put the address in `PUBLIC_URL`.

[OTARI_SETUP.md](OTARI_SETUP.md) lists the dashboard steps: service key, budgets, routing policies and tools.
`uv run python scripts/check_otari.py` checks the account and every model role.

## Tests

```bash
uv run pytest
```

The tests run the whole pipeline against a small stand-in for Otari's wire formats (`tests/fake_otari.py`): a full
run, a guardrail refusal, a guest running out of budget and the outage drill.

## Limitations

- iNaturalist's native/introduced status is per country, so a species can be native to the country but not to the
  exact region.
- Observation counts reflect where people record wildlife as much as what lives there.
- The water estimate is a rough establishment-year figure (half of reference evapotranspiration minus effective
  rain), not an irrigation design.

## License

MIT
