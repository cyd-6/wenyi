"""Merge only referenced model definitions while preserving destination defaults."""

from __future__ import annotations

import os
from copy import deepcopy

from wenyi_core.config import Config
from wenyi_core.llm.registry import provider_spec

from ..config_documents import config_document, project_document
from ..model_registry import rename_model_references


def credential_name(provider: dict) -> str | None:
    return provider.get("api_key_env") or getattr(
        provider_spec(provider["kind"]).adapter_type(), "default_api_key_env", None
    )


def merge_registry(
    current: dict, incoming: dict, suffix: str
) -> tuple[dict, dict, list[dict], list[str]]:
    destination = deepcopy(current)
    source = config_document(Config.from_dict(incoming))
    llm, target = source["llm"], destination["llm"]
    used = set(llm["tiers"].values())
    for route in llm["routes"].values():
        if route.get("model"):
            used.add(route["model"])
        used.update(route.get("fallbacks") or [])
    providers = {llm["models"][key]["provider"] for key in used}
    quotas = {
        llm["providers"][key]["quota_group"]
        for key in providers
        if llm["providers"][key].get("quota_group")
    }
    changes = []

    def add(section: str, key: str, definition: dict) -> str:
        existing = target.setdefault(section, {})
        if key in existing and existing[key] == definition:
            return key
        identical = next((name for name, value in existing.items() if value == definition), None)
        if identical:
            return identical
        chosen = key
        if chosen in existing:
            chosen = f"{key}_import_{suffix[:8]}"
            counter = 2
            while chosen in existing and existing[chosen] != definition:
                chosen = f"{key}_import_{suffix[:8]}_{counter}"
                counter += 1
        existing[chosen] = definition
        if chosen != key:
            changes.append({"kind": section, "source": key, "target": chosen})
        return chosen

    quota_map = {key: add("quotas", key, llm["quotas"][key]) for key in sorted(quotas)}
    provider_map = {}
    missing = set()
    for key in sorted(providers):
        definition = deepcopy(llm["providers"][key])
        if definition.get("quota_group"):
            definition["quota_group"] = quota_map[definition["quota_group"]]
        env_name = credential_name(definition)
        # A different endpoint must not inherit an existing provider's credential.
        if env_name and any(
            credential_name(value) == env_name and value != definition
            for value in target["providers"].values()
        ):
            definition["api_key_env"] = f"{env_name}_IMPORT_{suffix[:8].upper()}"
            changes.append(
                {"kind": "credentials", "source": env_name, "target": definition["api_key_env"]}
            )
            env_name = definition["api_key_env"]
        provider_map[key] = add("providers", key, definition)
        if env_name and not os.environ.get(env_name):
            missing.add(env_name)
    model_map = {}
    for key in sorted(used):
        definition = {
            **llm["models"][key],
            "provider": provider_map[llm["models"][key]["provider"]],
        }
        model_map[key] = add("models", key, definition)
    source["llm"] = {
        **target,
        **{
            key: value
            for key, value in rename_model_references(llm, model_map).items()
            if key in {"tiers", "routes", "budget"}
        },
    }
    # Defaults in destination remain unchanged; imported projects store resolved choices.
    Config.from_dict(destination)
    imported = project_document(Config.from_dict(source))
    return destination, imported, changes, sorted(missing)
