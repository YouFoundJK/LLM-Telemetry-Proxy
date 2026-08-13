#!/usr/bin/env python3
"""
Model Costs Auto-Updater — Fetches latest provider pricing from LiteLLM
and updates dashboard/model_costs.json using appendable historical tiers.

Rule:
- If pricing for a model has changed, appends a new tier with today's date.
- If pricing has NOT changed, does NOT append anything (prevents redundant duplicates).
- If a new model is found in telemetry DB or config, adds its initial tier.
"""

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
COSTS_PATH = DASHBOARD_DIR / "model_costs.json"
DB_PATH = REPO_ROOT / "data" / "llm_telemetry.db"
MAPPING_PATH = REPO_ROOT / "data" / "model_mapping.json"
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"


def fetch_litellm_prices(timeout: int = 15) -> dict:
    req = urllib.request.Request(
        LITELLM_URL,
        headers={"User-Agent": "LLMProxy-CostUpdater/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_all_tracked_models() -> set:
    models = set()

    # 1. Existing models in model_costs.json
    if COSTS_PATH.exists():
        try:
            with open(COSTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    models.update(data.keys())
        except Exception:
            pass

    # 2. Models from SQLite api_calls
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            rows = conn.execute("SELECT DISTINCT model FROM api_calls WHERE model IS NOT NULL AND model != ''").fetchall()
            conn.close()
            for r in rows:
                if r[0]:
                    models.add(r[0].strip())
        except Exception:
            pass

    # 3. Models from model_mapping.json
    if MAPPING_PATH.exists():
        try:
            with open(MAPPING_PATH, "r", encoding="utf-8") as f:
                mapping = json.load(f)
                if isinstance(mapping, dict):
                    models.update(mapping.keys())
        except Exception:
            pass

    return {m for m in models if m}


def find_best_litellm_match(model_name: str, litellm_data: dict):
    """
    Finds the most appropriate pricing entry in LiteLLM for the given model.
    Prioritizes official / direct provider entries over third-party proxies when available.
    """
    m_clean = model_name.lower().strip()
    
    # Priority providers in order of preference
    provider_priority = [
        "deepseek", "moonshot", "zai", "dashscope", "openrouter",
        "fireworks_ai", "cloudflare", "together_ai", "groq", "cerebras",
        "deepinfra", "azure_ai", "scaleway", "cohere_chat", "vercel_ai_gateway",
        "libertai", "tensormesh", "bedrock"
    ]

    exact_matches = []
    prefix_or_suffix_matches = []
    fuzzy_matches = []

    for k, v in litellm_data.items():
        if not isinstance(v, dict):
            continue
        k_lower = k.lower()
        inp = v.get("input_cost_per_token")
        out = v.get("output_cost_per_token")
        if inp is None and out is None:
            continue

        prov = (v.get("litellm_provider") or "").lower()

        if k_lower == m_clean:
            exact_matches.append((k, v, prov))
        elif k_lower.endswith("/" + m_clean) or k_lower.startswith(m_clean + "/"):
            prefix_or_suffix_matches.append((k, v, prov))
        elif m_clean in k_lower or m_clean.replace(".", "p") in k_lower or m_clean.replace("-", "") in k_lower.replace("-", ""):
            fuzzy_matches.append((k, v, prov))

    def rank_match(item):
        _, _, prov = item
        try:
            return provider_priority.index(prov)
        except ValueError:
            return 999

    for pool in [exact_matches, prefix_or_suffix_matches, fuzzy_matches]:
        if pool:
            pool.sort(key=rank_match)
            return pool[0][0], pool[0][1]

    return None, None


def sync_model_costs(today_str: str = None) -> dict:
    if not today_str:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    litellm_data = fetch_litellm_prices()
    tracked_models = get_all_tracked_models()

    existing_config = {}
    if COSTS_PATH.exists():
        try:
            with open(COSTS_PATH, "r", encoding="utf-8") as f:
                existing_config = json.load(f)
        except Exception:
            existing_config = {}

    updated = []
    new_models = []
    unchanged = []
    unmatched = []

    result_config = dict(existing_config)

    for model in sorted(tracked_models):
        match_key, match_data = find_best_litellm_match(model, litellm_data)

        if not match_data:
            if model in result_config:
                unchanged.append(model)
            else:
                unmatched.append(model)
            continue

        raw_in = match_data.get("input_cost_per_token") or 0.0
        raw_out = match_data.get("output_cost_per_token") or 0.0
        new_in_pm = round(float(raw_in) * 1e6, 6)
        new_out_pm = round(float(raw_out) * 1e6, 6)
        prov = match_data.get("litellm_provider") or "LiteLLM"
        provider_src = f"{prov.capitalize()} (LiteLLM)" if "litellm" not in prov.lower() else prov

        existing_tiers = result_config.get(model)

        if not existing_tiers:
            # Brand new model
            new_tier = {
                "effective_date": today_str,
                "input_cost_per_million": new_in_pm,
                "output_cost_per_million": new_out_pm,
                "provider_source": provider_src
            }
            result_config[model] = [new_tier]
            new_models.append({
                "model": model,
                "matched_key": match_key,
                "input_cost_per_million": new_in_pm,
                "output_cost_per_million": new_out_pm,
                "provider_source": provider_src
            })
            continue

        # Normalize existing tiers to a list
        if isinstance(existing_tiers, dict):
            existing_tiers = [existing_tiers]
            result_config[model] = existing_tiers

        latest_tier = existing_tiers[-1]
        prev_in = float(latest_tier.get("input_cost_per_million", 0.0))
        prev_out = float(latest_tier.get("output_cost_per_million", 0.0))

        # Check if price changed
        price_changed = (abs(new_in_pm - prev_in) > 1e-5 or abs(new_out_pm - prev_out) > 1e-5)

        if price_changed:
            new_tier = {
                "effective_date": today_str,
                "input_cost_per_million": new_in_pm,
                "output_cost_per_million": new_out_pm,
                "provider_source": provider_src
            }
            result_config[model].append(new_tier)
            updated.append({
                "model": model,
                "matched_key": match_key,
                "old_input": prev_in,
                "new_input": new_in_pm,
                "old_output": prev_out,
                "new_output": new_out_pm,
                "effective_date": today_str,
                "provider_source": provider_src
            })
        else:
            unchanged.append(model)

    # Save to model_costs.json
    COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(COSTS_PATH, "w", encoding="utf-8") as f:
        json.dump(result_config, f, indent=2)

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "today": today_str,
        "updated_count": len(updated),
        "new_count": len(new_models),
        "unchanged_count": len(unchanged),
        "unmatched_count": len(unmatched),
        "updated": updated,
        "new_models": new_models,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "message": f"Sync completed: {len(updated)} updated, {len(new_models)} new, {len(unchanged)} unchanged."
    }


if __name__ == "__main__":
    print(f"Fetching latest pricing from {LITELLM_URL}...")
    try:
        report = sync_model_costs()
        print(f"\nResult: {report['message']}")
        if report['updated']:
            print("\nUpdated Models:")
            for u in report['updated']:
                print(f"  - {u['model']}: in ${u['old_input']} -> ${u['new_input']}/M, out ${u['old_output']} -> ${u['new_output']}/M (effective {u['effective_date']})")
        if report['new_models']:
            print("\nNew Models Added:")
            for n in report['new_models']:
                print(f"  + {n['model']}: in ${n['input_cost_per_million']}/M, out ${n['output_cost_per_million']}/M")
        print(f"\nUnchanged Models ({len(report['unchanged'])}): {', '.join(report['unchanged'][:10])}{'...' if len(report['unchanged']) > 10 else ''}")
    except Exception as e:
        print(f"Error during cost synchronization: {e}", file=sys.stderr)
        sys.exit(1)
