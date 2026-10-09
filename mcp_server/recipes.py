"""Saved, reviewed and parameterized computer_plan recipes.

A recipe is captured from a successful computer_plan run as a draft, reviewed so
literal values become typed parameters, activated on explicit confirmation, and
then run again with new values. Running always goes through computer_plan, so
every step keeps normal policy, scopes, leases and verification.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .data_guard import contains_direct_secret

SCHEMA_VERSION = 1
MAX_RECIPES = 200
MAX_PARAMETERS = 20
_NAME_LIMIT = 80
_DESCRIPTION_LIMIT = 500
_STRING_VALUE_LIMIT = 2000
_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,31})\s*\}\}")
_ID_RE = re.compile(r"^rcp_[0-9a-f]{12}$")
# Template markers may only change argument values, never what runs.
_STRUCTURAL_KEYS = frozenset({"tool", "id", "kind", "type"})
_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "datetime"})
_STATUSES = frozenset({"draft", "active", "paused"})
_BUDGET_KEYS = ("max_seconds", "max_recoveries", "max_recovery_seconds", "max_action_units")


class RecipeError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def recipe_dir() -> Path:
    raw = os.getenv("MAC_MCP_RECIPE_DIR", "").strip()
    root = Path(raw).expanduser() if raw else Path.home() / ".mac-mcp" / "recipes"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _now() -> float:
    return time.time()


def _path(recipe_id: str) -> Path:
    if not _ID_RE.fullmatch(str(recipe_id or "")):
        raise RecipeError("RECIPE_ID_INVALID", "recipe_id must look like rcp_ followed by 12 hex characters.")
    return recipe_dir() / f"{recipe_id}.json"


def _load(recipe_id: str) -> Dict[str, Any]:
    path = _path(recipe_id)
    if not path.exists():
        raise RecipeError("RECIPE_NOT_FOUND", f"Recipe not found: {recipe_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeError("RECIPE_CORRUPT", f"Recipe file is unreadable: {recipe_id}") from exc


def _write(manifest: Mapping[str, Any]) -> None:
    path = _path(str(manifest["recipe_id"]))
    fd, tmp = tempfile.mkstemp(prefix=".recipe-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise RecipeError("RECIPE_ARGUMENT_INVALID", f"{field} is required.")
    if len(text) > limit:
        raise RecipeError("RECIPE_ARGUMENT_INVALID", f"{field} must be at most {limit} characters.")
    return text


def _walk_strings(value: Any, path: str = "") -> Iterable[Tuple[str, str, Optional[str]]]:
    """Yield (path, string, parent key) for every string inside steps."""
    if isinstance(value, str):
        yield path, value, path.rsplit(".", 1)[-1] if path else None
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_strings(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}.{index}" if path else str(index))


def _template_names(steps: Any) -> List[str]:
    names: List[str] = []
    for path, text, key in _walk_strings(steps):
        found = _TEMPLATE_RE.findall(text)
        if found and key in _STRUCTURAL_KEYS:
            raise RecipeError("RECIPE_TEMPLATE_NOT_ALLOWED",
                              f"Parameters may only appear in step arguments, not in '{key}' ({path}).")
        for name in found:
            if name not in names:
                names.append(name)
    return names


def _secret_paths(steps: Any) -> List[str]:
    return [path for path, text, _ in _walk_strings(steps) if contains_direct_secret(text)]


def _normalize_parameters(raw: Any) -> Dict[str, Dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise RecipeError("RECIPE_ARGUMENT_INVALID", "parameters must be an object of name -> spec.")
    if len(raw) > MAX_PARAMETERS:
        raise RecipeError("RECIPE_ARGUMENT_INVALID", f"A recipe can have at most {MAX_PARAMETERS} parameters.")
    params: Dict[str, Dict[str, Any]] = {}
    for name, spec in raw.items():
        if not _PARAM_RE.fullmatch(str(name)):
            raise RecipeError("RECIPE_ARGUMENT_INVALID",
                              f"Parameter name '{name}' must be lowercase letters, digits or _ and start with a letter.")
        spec = dict(spec or {}) if isinstance(spec, Mapping) else {"type": str(spec)}
        kind = str(spec.get("type") or "string").strip().lower()
        if kind not in _PARAM_TYPES:
            raise RecipeError("RECIPE_ARGUMENT_INVALID",
                              f"Parameter '{name}' type must be one of {', '.join(sorted(_PARAM_TYPES))}.")
        clean: Dict[str, Any] = {
            "type": kind,
            "description": _clean_text(spec.get("description"), f"{name}.description", 200),
            "required": bool(spec.get("required", "default" not in spec)),
        }
        if "enum" in spec and spec["enum"] is not None:
            if not isinstance(spec["enum"], list) or not spec["enum"] or len(spec["enum"]) > 50:
                raise RecipeError("RECIPE_ARGUMENT_INVALID", f"Parameter '{name}' enum must be a list of 1-50 values.")
            clean["enum"] = [_coerce(name, {"type": kind}, item) for item in spec["enum"]]
        if kind == "string":
            clean["max_length"] = max(1, min(int(spec.get("max_length") or 200), _STRING_VALUE_LIMIT))
        if "default" in spec and spec["default"] is not None:
            clean["default"] = _coerce(name, clean, spec["default"])
        params[str(name)] = clean
    return params


def _coerce(name: str, spec: Mapping[str, Any], value: Any) -> Any:
    kind = spec.get("type", "string")
    try:
        if kind == "string":
            text = str(value)
            if len(text) > int(spec.get("max_length") or _STRING_VALUE_LIMIT):
                raise ValueError(f"longer than {spec.get('max_length')} characters")
            result: Any = text
        elif kind == "integer":
            if isinstance(value, bool):
                raise ValueError("not an integer")
            result = int(str(value).strip())
        elif kind == "number":
            if isinstance(value, bool):
                raise ValueError("not a number")
            result = float(value)
        elif kind == "boolean":
            if isinstance(value, bool):
                result = value
            elif str(value).strip().lower() in {"true", "yes", "1"}:
                result = True
            elif str(value).strip().lower() in {"false", "no", "0"}:
                result = False
            else:
                raise ValueError("not true/false")
        elif kind == "date":
            result = date.fromisoformat(str(value).strip()).isoformat()
        else:  # datetime
            result = datetime.fromisoformat(str(value).strip()).isoformat(timespec="minutes")
    except (TypeError, ValueError) as exc:
        raise RecipeError("RECIPE_PARAMETER_INVALID", f"Parameter '{name}' must be a valid {kind}: {exc}") from exc
    if "enum" in spec and result not in spec["enum"]:
        raise RecipeError("RECIPE_PARAMETER_INVALID", f"Parameter '{name}' must be one of {spec['enum']}.")
    return result


def _substitute(value: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _TEMPLATE_RE.fullmatch(value.strip())
        if whole:
            # A value that is only a placeholder keeps its typed value (number, bool...).
            return values[whole.group(1)]
        return _TEMPLATE_RE.sub(lambda match: str(values[match.group(1)]), value)
    if isinstance(value, Mapping):
        return {key: (child if key in _STRUCTURAL_KEYS else _substitute(child, values)) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute(child, values) for child in value]
    return value


def _sample_values(params: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    samples = {"string": "x", "integer": 1, "number": 1.0, "boolean": True,
               "date": "2026-01-01", "datetime": "2026-01-01T09:00"}
    return {name: spec.get("default", (spec.get("enum") or [samples[spec["type"]]])[0]) for name, spec in params.items()}


def _validate_plan(steps: Any, plan_version: int, params: Mapping[str, Mapping[str, Any]]) -> None:
    from .computer_plan import ComputerPlanError, _validate_steps

    try:
        _validate_steps(_substitute(copy.deepcopy(steps), _sample_values(params)), version=plan_version)
    except ComputerPlanError as exc:
        raise RecipeError("RECIPE_PLAN_INVALID", f"The recipe steps are not a valid plan: {exc}") from exc


def _consequential(steps: Any) -> bool:
    from .computer_plan import _MUTATING_TOOLS

    return any(key == "tool" and text in _MUTATING_TOOLS for _, text, key in _walk_strings(steps))


def _review(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    steps = manifest.get("steps") or []
    params = manifest.get("parameters") or {}
    used = _template_names(steps)
    literals = [
        {"path": path, "value": text if len(text) <= 120 else text[:117] + "..."}
        for path, text, key in _walk_strings(steps)
        if key not in _STRUCTURAL_KEYS and not _TEMPLATE_RE.search(text) and text.strip()
    ]
    return {
        "parameters_used": used,
        "undeclared_parameters": [name for name in used if name not in params],
        "unused_parameters": [name for name in params if name not in used],
        "literal_values": literals[:60],
        "secret_like_values": _secret_paths(steps),
        "consequential": _consequential(steps),
    }


def _public(manifest: Mapping[str, Any], *, full: bool = False) -> Dict[str, Any]:
    keys = ("recipe_id", "name", "description", "status", "parameters", "consequential",
            "created_at", "updated_at", "run_count", "last_run")
    out = {key: manifest.get(key) for key in keys}
    out["step_count"] = len(manifest.get("steps") or [])
    out["tools"] = sorted({text for _, text, key in _walk_strings(manifest.get("steps") or []) if key == "tool"})
    if full:
        out["plan_version"] = manifest.get("plan_version")
        out["budgets"] = manifest.get("budgets")
        out["steps"] = manifest.get("steps")
        out["source"] = manifest.get("source")
        out["review"] = _review(manifest)
    return out


def capture_draft(
    name: str, *, steps: Any, plan_version: int, budgets: Mapping[str, Any], plan_result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Save a successful plan as a draft that must be reviewed before it can run."""
    if not plan_result.get("ok"):
        raise RecipeError("RECIPE_CAPTURE_FAILED_PLAN", "Only a plan that finished successfully can be saved as a recipe.")
    existing = list(recipe_dir().glob("rcp_*.json"))
    if len(existing) >= MAX_RECIPES:
        raise RecipeError("RECIPE_LIMIT", f"At most {MAX_RECIPES} recipes can be stored; delete one first.")
    now = _now()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "recipe_id": "rcp_" + uuid.uuid4().hex[:12],
        "name": _clean_text(name, "name", _NAME_LIMIT, required=True),
        "description": "",
        "status": "draft",
        "plan_version": int(plan_version),
        "steps": copy.deepcopy(list(steps)),
        "parameters": {},
        "budgets": {key: budgets[key] for key in _BUDGET_KEYS if key in budgets},
        "consequential": _consequential(steps),
        "source": {
            "captured_from": "computer_plan",
            "captured_at": now,
            "steps_executed": (plan_result.get("plan_stats") or {}).get("steps_executed"),
        },
        "created_at": now,
        "updated_at": now,
        "run_count": 0,
        "last_run": None,
    }
    _write(manifest)
    return {
        "recipe_id": manifest["recipe_id"],
        "name": manifest["name"],
        "status": "draft",
        "review_required": True,
        "next": "recipe(action='inspect') to review steps, replace literal values with parameters via "
                "recipe(action='update', parameterize=[...]), then recipe(action='activate', confirm=true).",
    }


def list_recipes(*, include_drafts: bool = True) -> Dict[str, Any]:
    items = []
    for path in sorted(recipe_dir().glob("rcp_*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not include_drafts and manifest.get("status") == "draft":
            continue
        items.append(_public(manifest))
    items.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return {"ok": True, "count": len(items), "recipes": items}


def inspect_recipe(recipe_id: str) -> Dict[str, Any]:
    return {"ok": True, "recipe": _public(_load(recipe_id), full=True)}


def update_recipe(
    recipe_id: str, *, name: Optional[str] = None, description: Optional[str] = None,
    parameters: Optional[Mapping[str, Any]] = None, parameterize: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Rename, describe or parameterize a recipe. Any edit returns it to draft for review."""
    manifest = _load(recipe_id)
    if name is not None:
        manifest["name"] = _clean_text(name, "name", _NAME_LIMIT, required=True)
    if description is not None:
        manifest["description"] = _clean_text(description, "description", _DESCRIPTION_LIMIT)
    params = dict(manifest.get("parameters") or {})
    if parameters is not None:
        params = _normalize_parameters(parameters)
    replaced: Dict[str, int] = {}
    if parameterize:
        if not isinstance(parameterize, list) or len(parameterize) > 40:
            raise RecipeError("RECIPE_ARGUMENT_INVALID", "parameterize must be a list of at most 40 {literal, param} items.")
        steps = manifest.get("steps") or []
        for item in parameterize:
            literal = str((item or {}).get("literal") or "")
            param = str((item or {}).get("param") or "")
            if not literal or not _PARAM_RE.fullmatch(param):
                raise RecipeError("RECIPE_ARGUMENT_INVALID", "Each parameterize item needs a literal and a valid param name.")
            if param not in params:
                params[param] = {"type": "string", "description": "", "required": True, "max_length": 200}
            steps, count = _replace_literal(steps, literal, "{{" + param + "}}")
            if not count:
                raise RecipeError("RECIPE_LITERAL_NOT_FOUND", f"'{literal}' does not appear in the recipe's arguments.")
            replaced[param] = replaced.get(param, 0) + count
        manifest["steps"] = steps
    if parameters is not None or parameterize:
        manifest["parameters"] = params
    undeclared = [n for n in _template_names(manifest.get("steps") or []) if n not in params]
    if undeclared:
        raise RecipeError("RECIPE_PARAMETER_UNDECLARED", f"Steps use undeclared parameters: {', '.join(undeclared)}.")
    _validate_plan(manifest.get("steps") or [], int(manifest.get("plan_version") or 2), params)
    edits_steps = parameters is not None or bool(parameterize)
    if edits_steps and manifest.get("status") != "draft":
        manifest["status"] = "draft"  # changed steps must be reviewed again
    manifest["consequential"] = _consequential(manifest.get("steps") or [])
    manifest["updated_at"] = _now()
    _write(manifest)
    return {"ok": True, "replaced": replaced, "recipe": _public(manifest, full=True)}


def _replace_literal(value: Any, literal: str, template: str, key: Optional[str] = None) -> Tuple[Any, int]:
    if isinstance(value, str):
        if key in _STRUCTURAL_KEYS or literal not in value:
            return value, 0
        return value.replace(literal, template), value.count(literal)
    if isinstance(value, Mapping):
        total = 0
        out = {}
        for child_key, child in value.items():
            out[child_key], count = _replace_literal(child, literal, template, child_key)
            total += count
        return out, total
    if isinstance(value, list):
        total = 0
        out_list = []
        for child in value:
            new, count = _replace_literal(child, literal, template, key)
            out_list.append(new)
            total += count
        return out_list, total
    return value, 0


def set_status(recipe_id: str, status_value: str, *, confirm: bool = False) -> Dict[str, Any]:
    manifest = _load(recipe_id)
    target = str(status_value)
    if target not in _STATUSES - {"draft"}:
        raise RecipeError("RECIPE_ARGUMENT_INVALID", "status must be active or paused.")
    if target == "active" and manifest.get("status") == "draft":
        review = _review(manifest)
        if not confirm:
            return {"ok": False, "confirmation_required": True, "recipe": _public(manifest, full=True),
                    "message": "Review every step and parameter, then call activate again with confirm=true."}
        if review["secret_like_values"]:
            raise RecipeError("RECIPE_CONTAINS_SECRET",
                              "Steps contain secret-like values; replace them with parameters before activating.",
                              paths=review["secret_like_values"])
        if review["undeclared_parameters"]:
            raise RecipeError("RECIPE_PARAMETER_UNDECLARED", "Declare every parameter before activating.")
    # A paused recipe was already reviewed, so resuming it needs no new confirmation.
    manifest["status"] = target
    manifest["updated_at"] = _now()
    _write(manifest)
    return {"ok": True, "recipe": _public(manifest)}


def delete_recipe(recipe_id: str, *, confirm: bool = False) -> Dict[str, Any]:
    manifest = _load(recipe_id)
    if not confirm:
        return {"ok": False, "confirmation_required": True, "recipe": _public(manifest),
                "message": "Call delete again with confirm=true to remove this recipe permanently."}
    _path(recipe_id).unlink()
    return {"ok": True, "deleted": True, "recipe_id": recipe_id}


def prepare_run(recipe_id: str, values: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate values and return the concrete plan to hand to computer_plan."""
    manifest = _load(recipe_id)
    status_value = manifest.get("status")
    if status_value == "draft":
        raise RecipeError("RECIPE_NOT_ACTIVE", "This recipe is a draft; review and activate it before running.")
    if status_value == "paused":
        raise RecipeError("RECIPE_PAUSED", "This recipe is paused; resume it before running.")
    params = manifest.get("parameters") or {}
    supplied = dict(values or {})
    unknown = sorted(set(supplied) - set(params))
    if unknown:
        raise RecipeError("RECIPE_PARAMETER_INVALID", f"Unknown parameters: {', '.join(unknown)}.")
    resolved: Dict[str, Any] = {}
    for name, spec in params.items():
        if name in supplied and supplied[name] is not None:
            resolved[name] = _coerce(name, spec, supplied[name])
        elif "default" in spec:
            resolved[name] = spec["default"]
        elif spec.get("required", True):
            raise RecipeError("RECIPE_PARAMETER_MISSING", f"Parameter '{name}' is required.")
    missing_in_steps = [n for n in _template_names(manifest.get("steps") or []) if n not in resolved]
    if missing_in_steps:
        raise RecipeError("RECIPE_PARAMETER_MISSING", f"No value for: {', '.join(missing_in_steps)}.")
    steps = _substitute(copy.deepcopy(manifest.get("steps") or []), resolved)
    return {
        "recipe": manifest,
        "steps": steps,
        "plan_version": int(manifest.get("plan_version") or 2),
        "budgets": dict(manifest.get("budgets") or {}),
        "values": resolved,
    }


def record_run(recipe_id: str, result: Mapping[str, Any]) -> Dict[str, Any]:
    manifest = _load(recipe_id)
    manifest["run_count"] = int(manifest.get("run_count") or 0) + 1
    manifest["last_run"] = {
        "at": _now(),
        "ok": bool(result.get("ok")),
        "reason_code": result.get("reason_code"),
        "failed_step": result.get("step_id"),
        "steps_executed": (result.get("plan_stats") or {}).get("steps_executed"),
    }
    _write(manifest)
    return manifest["last_run"]
