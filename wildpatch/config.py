"""Settings, read once from the environment (and .env in development)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ---- Otari (hosted otari.ai). The key should be a *service key* so each volunteer is an end user
# with a budget of their own (Keys > Create key > Advanced > Service key).
OTARI_API_KEY = _env("OTARI_API_KEY")
OTARI_BASE_URL = _env("OTARI_BASE_URL", "https://api.otari.ai/api/v1").rstrip("/")

# ---- Public URL of this app, so Otari's cloud can reach our MCP server and guardrail service.
# Render sets RENDER_EXTERNAL_URL; a Cloudflare tunnel URL works too.
PUBLIC_URL = (_env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")
MCP_TOKEN = _env("MCP_TOKEN")
GUARD_TOKEN = _env("GUARD_TOKEN")
# Id of the MCP server registered in the Otari workspace (Tools > MCP servers). Without it the app
# sends the server inline with each request instead.
OTARI_MCP_SERVER_ID = _env("OTARI_MCP_SERVER_ID")

MCP_URL = f"{PUBLIC_URL}/mcp" if PUBLIC_URL else ""
GUARD_URL = f"{PUBLIC_URL}/guard/{GUARD_TOKEN}" if PUBLIC_URL and GUARD_TOKEN else ""

# ---- Open-weight models, one per role. Each can be a catalog id or the name of an Otari routing policy.
MODELS = {
    "intake": _env("MODEL_INTAKE", "openai/gpt-oss-20b"),
    "vision": _env("MODEL_VISION", "google/gemma-3-27b-it"),
    "surveyor": _env("MODEL_SURVEYOR", "z-ai/glm-4.7-flash"),
    "analyst": _env("MODEL_ANALYST", "alibaba/qwen3-coder-30b-a3b-instruct"),
    "scout": _env("MODEL_SCOUT", "moonshotai/kimi-k2.5"),
    "planner": _env("MODEL_PLANNER", "wildpatch-planner"),
    "structurer": _env("MODEL_STRUCTURER", "openai/gpt-oss-20b"),
    "guard_policy": _env("MODEL_GUARD_POLICY", "openai/gpt-oss-safeguard-120b"),
    "guard_policy_backup": _env("MODEL_GUARD_POLICY_BACKUP", "openai/gpt-oss-120b"),
    "guard_safety": _env("MODEL_GUARD_SAFETY", "meta/llama-guard-4-12b"),
}
# The planner policy's cheaper tier and the outage-drill policy (see OTARI_SETUP.md).
PLANNER_ECONOMY_MODEL = _env("PLANNER_ECONOMY_MODEL", "alibaba/qwen3-30b-a3b-instruct-2507")
PLANNER_DRILL_POLICY = _env("PLANNER_DRILL_POLICY", "wildpatch-drill")

# ---- Per-volunteer budgets: end-user budget ids on the service key's list. Otari enforces them; the caps
# here only label the allowance meter and should match the budgets set in the dashboard.
GUEST_BUDGET_ID = _env("GUEST_BUDGET_ID", "wildpatch-guest")
VOLUNTEER_CAP_USD = float(_env("VOLUNTEER_CAP_USD", "0.50"))
GUEST_CAP_USD = float(_env("GUEST_CAP_USD", "0.02"))
VOLUNTEERS = {
    "ana": {"name": "Ana", "role": "School garden coordinator", "budget": None, "cap_usd": VOLUNTEER_CAP_USD},
    "jordi": {"name": "Jordi", "role": "Neighbourhood volunteer", "budget": None, "cap_usd": VOLUNTEER_CAP_USD},
    "guest": {"name": "Guest", "role": "Visitor, small daily allowance", "budget": GUEST_BUDGET_ID,
              "cap_usd": GUEST_CAP_USD},
}
END_USER_PREFIX = _env("END_USER_PREFIX", "wildpatch:")

# ---- Abuse limits for a public demo that spends real money.
RUNS_PER_HOUR_PER_IP = int(_env("RUNS_PER_HOUR_PER_IP", "12"))
MAX_CONCURRENT_RUNS = int(_env("MAX_CONCURRENT_RUNS", "3"))
