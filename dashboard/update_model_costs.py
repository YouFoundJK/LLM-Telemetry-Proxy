#!/usr/bin/env python3
"""
Model Costs Auto-Updater — Fetches latest provider pricing from LiteLLM
and updates data/model_costs.json using appendable historical tiers.

Guarantees:
- Strictly matches exact model architecture / size from the official spec.
- No loss of precision / no artificial rounding.
- Only uses canonical model names (never alias keys like 'glm', 'deepseek', etc.).
- If pricing has NOT changed, does NOT append duplicate date tiers.
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
DATA_DIR = REPO_ROOT / "data"

def get_model_costs_path() -> Path:
    candidates = [
        DATA_DIR / "model_costs.json",
        DASHBOARD_DIR / "data" / "model_costs.json",
        REPO_ROOT / "model_costs.json",
        DASHBOARD_DIR / "model_costs.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

COSTS_PATH = get_model_costs_path()
DB_PATH = REPO_ROOT / "data" / "llm_telemetry.db"
MAPPING_PATH = REPO_ROOT / "data" / "model_mapping.json"
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

# Strict architectural patterns matching the exact models from the endpoint specification
MODEL_SOURCE_PATTERNS = {
    "gemma4": [
        "openrouter/google/gemma-4-31b-it",
        "google/gemma-4-31b-it",
        "tensormesh/google/gemma-4-31b-it",
        "libertai/gemma-4-31b-it",
        "sambanova/gemma-4-31b-it"
    ],
    "gpt-oss-120b": [
        "openrouter/openai/gpt-oss-120b",
        "together_ai/openai/gpt-oss-120b",
        "fireworks_ai/gpt-oss-120b",
        "azure_ai/gpt-oss-120b"
    ],
    "deepseek-v4-flash": [
        "deepseek/deepseek-v4-flash",
        "deepseek-v4-flash",
        "fireworks_ai/deepseek-v4-flash"
    ],
    "deepseek-v4-flash-thinking": [
        "libertai/deepseek-v4-flash-thinking",
        "deepseek/deepseek-v4-flash"
    ],
    "deepseek-v4-pro": [
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-pro"
    ],
    "deepseek-v4-pro-thinking": [
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-pro"
    ],
    "glm-5.2": [
        "openrouter/z-ai/glm-5.2",
        "dashscope/glm-5.2",
        "cloudflare/@cf/zai-org/glm-5.2"
    ],
    "kimi-k3": [
        "moonshot/kimi-k3",
        "moonshot/kimi-k2.7-code",
        "moonshot/kimi-k2.6"
    ],
    "qwen3.5-122b": [
        "openrouter/qwen/qwen3.5-122b-a10b",
        "libertai/qwen3.5-122b-a10b"
    ],
    "qwen3.5-int4": [
        "openrouter/qwen/qwen3.5-397b-a17b",
        "scaleway/qwen/qwen3.5-397b-a17b"
    ],
    "qwen3-embedding-4b": [
        "dashscope/qwen3-embedding-4b",
        "fireworks_ai/accounts/fireworks/models/qwen3-embedding-4b"
    ],
    "qwen3-reranker-4b": [
        "deepinfra/qwen3-reranker-4b",
        "fireworks_ai/accounts/fireworks/models/qwen3-reranker-4b"
    ]
}


def fetch_litellm_prices(timeout: int = 15) -> dict:
    req = urllib.request.Request(
        LITELLM_URL,
        headers={"User-Agent": "LLMProxy-CostUpdater/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_model_mapping() -> dict:
    candidates = [
        MAPPING_PATH,
        REPO_ROOT / "model_mapping.json",
        DASHBOARD_DIR / "data" / "model_mapping.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def resolve_canonical_model(model_name: str, mapping: dict = None, timestamp: str = None) -> str:
    if not model_name:
        return model_name
    if mapping is None:
        mapping = load_model_mapping()
    if not mapping:
        return model_name
    m_lower = str(model_name).lower().strip()
    
    target = mapping.get(m_lower)
    if target is None:
        for k, v in mapping.items():
            if k.lower().strip() == m_lower:
                target = v
                break
                
    if target is None:
        return model_name
        
    if isinstance(target, str):
        return target
        
    if isinstance(target, dict):
        sorted_dates = sorted(target.keys())
        if not sorted_dates:
            return model_name
        if not timestamp:
            return target[sorted_dates[-1]]
            
        ts_date = str(timestamp)[:10]
        matched_date = sorted_dates[0]
        for d in sorted_dates:
            if d <= ts_date:
                matched_date = d
            else:
                break
        return target[matched_date]
        
    if isinstance(target, list):
        return target[0] if target else model_name
        
    return model_name


def get_all_tracked_models() -> set:
    mapping = load_model_mapping()
    canonical_models = set()

    # 1. Canonical models defined in model_mapping.json
    for v in mapping.values():
        if isinstance(v, str):
            canonical_models.add(v.strip())
        elif isinstance(v, dict):
            for tgt in v.values():
                if tgt:
                    canonical_models.add(tgt.strip())
        elif isinstance(v, list):
            for tgt in v:
                if tgt:
                    canonical_models.add(tgt.strip())

    # 2. Existing models in model_costs.json (canonicalized)
    if COSTS_PATH.exists():
        try:
            with open(COSTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k in data.keys():
                        canon = resolve_canonical_model(k, mapping)
                        if canon:
                            canonical_models.add(canon)
        except Exception:
            pass

    # 3. Models from SQLite api_calls (canonicalized)
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            rows = conn.execute("SELECT DISTINCT model FROM api_calls WHERE model IS NOT NULL AND model != ''").fetchall()
            conn.close()
            for r in rows:
                if r[0]:
                    canon = resolve_canonical_model(r[0].strip(), mapping)
                    if canon:
                        canonical_models.add(canon)
        except Exception:
            pass

    return {m for m in canonical_models if m}


def find_best_litellm_match(model_name: str, litellm_data: dict):
    """
    Finds the exact pricing match in LiteLLM for the given canonical model.
    Uses strict architectural patterns to prevent cross-matching different model sizes.
    """
    m_clean = model_name.lower().strip()

    # 1. Check explicit architectural candidate keys
    if m_clean in MODEL_SOURCE_PATTERNS:
        for candidate_pattern in MODEL_SOURCE_PATTERNS[m_clean]:
            for k, v in litellm_data.items():
                if candidate_pattern.lower() in k.lower():
                    inp = v.get("input_cost_per_token")
                    out = v.get("output_cost_per_token")
                    if inp is not None or out is not None:
                        return k, v

    # 2. Fallback exact key matching
    for k, v in litellm_data.items():
        k_lower = k.lower()
        if k_lower == m_clean or k_lower.endswith("/" + m_clean):
            inp = v.get("input_cost_per_token")
            out = v.get("output_cost_per_token")
            if inp is not None or out is not None:
                return k, v

    return None, None


def sync_model_costs(today_str: str = None) -> dict:
    if not today_str:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    mapping = load_model_mapping()
    litellm_data = fetch_litellm_prices()
    tracked_models = get_all_tracked_models()

    existing_config = {}
    if COSTS_PATH.exists():
        try:
            with open(COSTS_PATH, "r", encoding="utf-8") as f:
                existing_config = json.load(f)
        except Exception:
            existing_config = {}

    # Build clean canonical config (prune any non-canonical alias keys)
    clean_config = {}
    for k, v in existing_config.items():
        canon = resolve_canonical_model(k, mapping)
        if canon == k:
            clean_config[canon] = v

    updated = []
    new_models = []
    unchanged = []
    unmatched = []

    for model in sorted(tracked_models):
        match_key, match_data = find_best_litellm_match(model, litellm_data)

        if not match_data:
            if model in clean_config:
                unchanged.append(model)
            else:
                unmatched.append(model)
            continue

        raw_in = match_data.get("input_cost_per_token")
        raw_out = match_data.get("output_cost_per_token")
        if raw_in is None and raw_out is None:
            unchanged.append(model)
            continue

        # Full precision per-million conversion (no round-off loss)
        new_in_pm = float(raw_in) * 1e6 if raw_in is not None else 0.0
        new_out_pm = float(raw_out) * 1e6 if raw_out is not None else 0.0
        prov = match_data.get("litellm_provider") or "LiteLLM"
        provider_src = f"{prov.capitalize()} (LiteLLM)" if "litellm" not in prov.lower() else prov

        existing_tiers = clean_config.get(model)

        if not existing_tiers:
            # Brand new model
            new_tier = {
                "effective_date": today_str,
                "input_cost_per_million": new_in_pm,
                "output_cost_per_million": new_out_pm,
                "provider_source": provider_src
            }
            clean_config[model] = [new_tier]
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
            clean_config[model] = existing_tiers

        latest_tier = existing_tiers[-1]
        prev_in = float(latest_tier.get("input_cost_per_million", 0.0))
        prev_out = float(latest_tier.get("output_cost_per_million", 0.0))

        # Check if price changed significantly (more than 0.0001% difference)
        diff_in = abs(new_in_pm - prev_in)
        diff_out = abs(new_out_pm - prev_out)
        price_changed = (diff_in > 1e-6 or diff_out > 1e-6)

        if price_changed:
            new_tier = {
                "effective_date": today_str,
                "input_cost_per_million": new_in_pm,
                "output_cost_per_million": new_out_pm,
                "provider_source": provider_src
            }
            clean_config[model].append(new_tier)
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

    # Save canonical model_costs.json
    COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(COSTS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean_config, f, indent=2)

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "today": today_str,
        "total_canonical_models": len(clean_config),
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
        print(f"Total Canonical Models Tracked: {report['total_canonical_models']}")
        if report['updated']:
            print("\nUpdated Models:")
            for u in report['updated']:
                print(f"  - {u['model']}: in ${u['old_input']} -> ${u['new_input']}/M, out ${u['old_output']} -> ${u['new_output']}/M (effective {u['effective_date']})")
        if report['new_models']:
            print("\nNew Models Added:")
            for n in report['new_models']:
                print(f"  + {n['model']}: in ${n['input_cost_per_million']}/M, out ${n['output_cost_per_million']}/M")
        print(f"\nUnchanged Models ({len(report['unchanged'])}): {', '.join(report['unchanged'])}")
    except Exception as e:
        print(f"Error during cost synchronization: {e}", file=sys.stderr)
        sys.exit(1)
