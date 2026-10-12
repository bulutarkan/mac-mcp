"""Signed catalog of skill and recipe packs (#146).

The catalog is one JSON document (catalog.json) with a detached SSH signature
(catalog.json.sig) made in its own namespace, mac-mcp-catalog, so a catalog
signature can never pass as a release signature or the other way round. Every
pack lists its version, publisher, supported Mac MCP version, declared
capabilities and the size and SHA-256 of each file. Nothing is installed by an
agent: the owner reviews a pack (what it is, what it may do, what changed since
the installed version) and installs or updates it with an explicit confirmation.
Installed versions are pinned in installed.json and only change on a confirmed
install. A signature or digest that does not match blocks the install.

Recipes from a pack land as drafts, so they run only after recipe activation.
Scripts in a skill pack are held back: they are installed one at a time with
enable_script after their own review, never with the pack.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from . import policy, recipes, release_trust
from .version import __version__

SCHEMA_VERSION = 1
CATALOG_FILE = "catalog.json"
SIGNATURE_FILE = "catalog.json.sig"
SIGNER_IDENTITY = "mac-mcp-catalog"
SIGNATURE_NAMESPACE = "mac-mcp-catalog"
TRUSTED_SIGNERS_FILENAME = "catalog_trusted_signers.txt"
KINDS = frozenset({"skill", "recipe"})
MAX_PACKS = 200
MAX_FILES = 64
MAX_FILE_BYTES = 512 * 1024
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_SKILL_DIRS = ("references", "assets")
_CAPABILITIES = frozenset(item.value for item in policy.Capability)


class CatalogError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.code, "message": str(self), **self.extra}


def catalog_dir() -> Path:
    raw = os.getenv("MAC_MCP_CATALOG_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "catalog"


def trusted_signers_path() -> Path:
    raw = os.getenv("MAC_MCP_CATALOG_TRUSTED_SIGNERS", "").strip()
    return Path(raw).expanduser() if raw else Path(__file__).resolve().with_name(TRUSTED_SIGNERS_FILENAME)


def _state_root() -> Path:
    state = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    root = state / "catalog"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _skills_root() -> Path:
    raw = os.getenv("MAC_MCP_SKILLS_DIR", "").strip()
    root = (Path(raw).expanduser() if raw else Path.home() / ".mac-mcp" / "skills").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _version_tuple(value: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in str(value).split("."))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- the signed catalog -----------------------------------------------------

def _validate_pack(pack: Any) -> Dict[str, Any]:
    if not isinstance(pack, dict):
        raise CatalogError("CATALOG_INVALID", "A catalog pack must be an object.")
    pack_id = str(pack.get("id") or "")
    if not _ID_RE.fullmatch(pack_id) or len(pack_id) > 64:
        raise CatalogError("CATALOG_INVALID", f"Invalid pack id {pack_id!r}.")
    if pack.get("kind") not in KINDS:
        raise CatalogError("CATALOG_INVALID", f"{pack_id}: kind must be skill or recipe.")
    for key in ("version", "min_mac_mcp_version"):
        if not _VERSION_RE.fullmatch(str(pack.get(key) or "")):
            raise CatalogError("CATALOG_INVALID", f"{pack_id}: {key} must look like 1.2.3.")
    for key in ("name", "publisher", "description"):
        if not isinstance(pack.get(key), str) or not pack[key].strip() or len(pack[key]) > 1000:
            raise CatalogError("CATALOG_INVALID", f"{pack_id}: {key} is required.")
    caps = pack.get("capabilities")
    if not isinstance(caps, list) or not set(caps) <= _CAPABILITIES:
        raise CatalogError("CATALOG_INVALID", f"{pack_id}: capabilities must be names from {sorted(_CAPABILITIES)}.")
    files = pack.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise CatalogError("CATALOG_INVALID", f"{pack_id}: files must list 1-{MAX_FILES} entries.")
    seen = set()
    for item in files:
        path = str((item or {}).get("path") or "")
        if not _PATH_RE.fullmatch(path) or ".." in path.split("/") or path in seen:
            raise CatalogError("CATALOG_INVALID", f"{pack_id}: unsafe or repeated file path {path!r}.")
        seen.add(path)
        if not _SHA256_RE.fullmatch(str(item.get("sha256") or "")) or not isinstance(item.get("size"), int):
            raise CatalogError("CATALOG_INVALID", f"{pack_id}: {path} needs size and sha256.")
        if item["size"] > MAX_FILE_BYTES:
            raise CatalogError("CATALOG_INVALID", f"{pack_id}: {path} is larger than {MAX_FILE_BYTES} bytes.")
    main_file = "SKILL.md" if pack["kind"] == "skill" else "recipe.json"
    if main_file not in seen:
        raise CatalogError("CATALOG_INVALID", f"{pack_id}: a {pack['kind']} pack needs {main_file}.")
    return pack


def build_catalog(root: Path) -> Dict[str, Any]:
    """The unsigned catalog for root/packs/<id>/<version>/: pack.json holds the metadata and every
    other file in the version directory is listed with its size and SHA-256. Only the newest
    version of each pack is offered."""
    packs = []
    for pack_dir in sorted(path for path in (root / "packs").iterdir() if path.is_dir()):
        versions = [path for path in pack_dir.iterdir() if path.is_dir() and _VERSION_RE.fullmatch(path.name)]
        if not versions:
            continue
        version_dir = max(versions, key=lambda path: _version_tuple(path.name))
        meta = json.loads((version_dir / "pack.json").read_text(encoding="utf-8"))
        if meta.get("id") != pack_dir.name or meta.get("version") != version_dir.name:
            raise CatalogError("CATALOG_INVALID", f"{version_dir}/pack.json id/version must match its directory.")
        files = []
        for path in sorted(item for item in version_dir.rglob("*") if item.is_file() and item.name != "pack.json"):
            data = path.read_bytes()
            files.append({"path": path.relative_to(version_dir).as_posix(), "size": len(data), "sha256": _sha256(data)})
        packs.append(_validate_pack({**meta, "files": files}))
    return {"schema_version": SCHEMA_VERSION, "catalog": "mac-mcp-curated", "packs": packs}


def load_catalog(source: Optional[Path] = None) -> Dict[str, Any]:
    """Read catalog.json after its signature verifies against the pinned catalog signers."""
    root = Path(source) if source else catalog_dir()
    manifest_path, signature_path = root / CATALOG_FILE, root / SIGNATURE_FILE
    if not manifest_path.is_file() or not signature_path.is_file():
        raise CatalogError("CATALOG_MISSING", f"No signed catalog at {root} (catalog.json and catalog.json.sig).")
    manifest_bytes = manifest_path.read_bytes()
    try:
        signer = release_trust.verify_manifest_signature(
            manifest_bytes, signature_path.read_bytes(), signers_path=trusted_signers_path(),
            identity=SIGNER_IDENTITY, namespace=SIGNATURE_NAMESPACE,
        )
    except release_trust.ReleaseVerificationError as exc:
        raise CatalogError("CATALOG_SIGNATURE_INVALID", f"The catalog signature does not verify: {exc}") from None
    try:
        catalog = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise CatalogError("CATALOG_INVALID", f"catalog.json is not valid JSON: {exc}") from None
    if not isinstance(catalog, dict) or catalog.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError("CATALOG_INVALID", f"Unsupported catalog schema (expected {SCHEMA_VERSION}).")
    packs = catalog.get("packs")
    if not isinstance(packs, list) or len(packs) > MAX_PACKS:
        raise CatalogError("CATALOG_INVALID", "catalog.json must list packs.")
    ids = [_validate_pack(pack)["id"] for pack in packs]
    if len(ids) != len(set(ids)):
        raise CatalogError("CATALOG_INVALID", "A pack id appears twice in the catalog.")
    return {"root": root, "signer": signer, "catalog": catalog, "digest": _sha256(manifest_bytes),
            "packs": {pack["id"]: pack for pack in packs}}


def _pack_files(root: Path, pack: Mapping[str, Any]) -> Dict[str, bytes]:
    """The pack's files, each checked against the size and SHA-256 the signed catalog lists."""
    base = root / "packs" / str(pack["id"]) / str(pack["version"])
    out: Dict[str, bytes] = {}
    for item in pack["files"]:
        path = base / item["path"]
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(base.resolve())
            data = resolved.read_bytes()
        except (OSError, ValueError):
            raise CatalogError("PACK_FILE_MISSING", f"{pack['id']}: {item['path']} is missing.") from None
        if len(data) != item["size"] or _sha256(data) != item["sha256"]:
            raise CatalogError("PACK_DIGEST_MISMATCH",
                               f"{pack['id']}: {item['path']} does not match the signed catalog; nothing was installed.")
        out[item["path"]] = data
    return out


# --- what a pack may do -----------------------------------------------------

def _recipe_document(files: Mapping[str, bytes]) -> Dict[str, Any]:
    try:
        doc = json.loads(files["recipe.json"])
    except (KeyError, json.JSONDecodeError):
        raise CatalogError("PACK_INVALID", "recipe.json is not valid JSON.") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        raise CatalogError("PACK_INVALID", "recipe.json needs steps.")
    return doc


def _step_tools(value: Any) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    if isinstance(value, dict):
        if isinstance(value.get("tool"), str):
            yield value["tool"], value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
        for item in value.values():
            yield from _step_tools(item)
    elif isinstance(value, list):
        for item in value:
            yield from _step_tools(item)


def effective_capabilities(pack: Mapping[str, Any], files: Mapping[str, bytes]) -> Dict[str, Any]:
    """What the pack can actually do, worked out from its content rather than taken on trust."""
    caps = set()
    tools: List[str] = []
    destructive = False
    if pack["kind"] == "recipe":
        for tool, arguments in _step_tools(_recipe_document(files).get("steps")):
            try:
                _declared, effective = policy.resolve_risk(tool, arguments)
            except KeyError:
                raise CatalogError("PACK_INVALID", f"{pack['id']}: recipe uses unknown tool {tool!r}.") from None
            caps.update(cap.value for cap in effective.capabilities)
            destructive = destructive or effective.destructive
            tools.append(tool)
    else:
        caps.add(policy.Capability.READ.value)
    scripts = sorted(path for path in files if path.startswith("scripts/"))
    return {"capabilities": sorted(caps), "tools": sorted(set(tools)), "destructive": destructive,
            "scripts": scripts, "script_capabilities": [policy.Capability.RAW_EXECUTION.value] if scripts else []}


# --- installed state --------------------------------------------------------

def _installed_path() -> Path:
    return _state_root() / "installed.json"


def installed() -> Dict[str, Any]:
    path = _installed_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise CatalogError("INSTALLED_STATE_UNREADABLE", f"{path} is not readable; fix or remove it.") from None
    return data if isinstance(data, dict) else {}


def _held_dir(pack_id: str, version: str) -> Path:
    return _state_root() / "held" / pack_id / version


def _summary(pack: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: pack.get(key) for key in ("id", "kind", "name", "version", "publisher", "description",
                                           "min_mac_mcp_version", "capabilities")}


def list_packs(source: Optional[Path] = None) -> Dict[str, Any]:
    loaded = load_catalog(source)
    pinned = installed()
    packs = []
    for pack_id, pack in sorted(loaded["packs"].items()):
        current = pinned.get(pack_id) or {}
        packs.append({**_summary(pack), "installed_version": current.get("version"),
                      "update_available": bool(current) and current.get("version") != pack["version"]})
    return {"ok": True, "signer": loaded["signer"], "catalog_digest": loaded["digest"], "packs": packs}


def review(pack_id: str, source: Optional[Path] = None) -> Dict[str, Any]:
    """Everything the owner needs before installing or updating: provenance, what the pack can
    do (declared and worked out from its content), and what changes against the pinned version."""
    loaded = load_catalog(source)
    pack = loaded["packs"].get(pack_id)
    if pack is None:
        raise CatalogError("PACK_NOT_FOUND", f"No pack {pack_id!r} in the catalog.")
    files = _pack_files(loaded["root"], pack)
    effective = effective_capabilities(pack, files)
    declared = set(pack["capabilities"])
    undeclared = sorted(set(effective["capabilities"]) - declared)
    current = installed().get(pack_id)
    previous = set((current or {}).get("capabilities") or [])
    compatible = _version_tuple(__version__) >= _version_tuple(pack["min_mac_mcp_version"])
    action = "install" if not current else ("up_to_date" if current.get("version") == pack["version"] else "update")
    out: Dict[str, Any] = {
        "ok": True, "pack": _summary(pack), "signer": loaded["signer"], "catalog_digest": loaded["digest"],
        "action": action, "installed_version": (current or {}).get("version"),
        "capabilities": sorted(declared), "effective": effective,
        "files": [{"path": item["path"], "size": item["size"], "sha256": item["sha256"]} for item in pack["files"]],
        "compatible": compatible, "running_mac_mcp_version": __version__,
    }
    if current:
        out["permission_changes"] = {"added": sorted(declared - previous), "removed": sorted(previous - declared)}
    problems = []
    if undeclared:
        problems.append(f"the content needs capabilities it does not declare: {', '.join(undeclared)}")
    if not compatible:
        problems.append(f"it needs Mac MCP {pack['min_mac_mcp_version']} or newer")
    if problems:
        out["blocked"] = "; ".join(problems)
    if pack["kind"] == "recipe":
        out["recipe_tools"] = effective["tools"]
        out["note"] = "Installs as a draft recipe: it runs only after recipe(action='activate', confirm=true)."
    if effective["scripts"]:
        out["scripts_held"] = effective["scripts"]
        out["script_note"] = ("Scripts are not installed with the pack. Review each and enable it on its own "
                              "with catalog enable-script; an enabled script can run anything (raw_execution).")
    return out


def install(pack_id: str, *, confirm: bool = False, source: Optional[Path] = None) -> Dict[str, Any]:
    """Install or update one pack after review. Everything is checked again here, at install time."""
    report = review(pack_id, source)
    if report.get("blocked"):
        raise CatalogError("PACK_BLOCKED", f"{pack_id} cannot be installed: {report['blocked']}.", review=report)
    if report["action"] == "up_to_date":
        return {"ok": True, "pack_id": pack_id, "version": report["pack"]["version"], "unchanged": True}
    if not confirm:
        raise CatalogError("CONFIRM_REQUIRED", "Review the pack, then install with confirm=true.", review=report)
    loaded = load_catalog(source)
    pack = loaded["packs"][pack_id]
    files = _pack_files(loaded["root"], pack)
    pinned = installed()
    current = pinned.get(pack_id) or {}
    record: Dict[str, Any] = {
        "kind": pack["kind"], "version": pack["version"], "publisher": pack["publisher"],
        "capabilities": sorted(pack["capabilities"]), "catalog_digest": loaded["digest"], "signer": loaded["signer"],
        "files": {item["path"]: item["sha256"] for item in pack["files"]}, "installed_at": time.time(),
        "previous_version": current.get("version"),
    }
    if pack["kind"] == "skill":
        record.update(_install_skill(pack, files, current))
    else:
        doc = _recipe_document(files)
        stored = recipes.import_recipe(
            name=str(doc.get("name") or pack["name"]), description=str(doc.get("description") or pack["description"]),
            steps=doc["steps"], plan_version=int(doc.get("plan_version") or 2), parameters=doc.get("parameters") or {},
            budgets=doc.get("budgets") or {},
            source={"installed_from": "catalog", "pack_id": pack_id, "version": pack["version"],
                    "publisher": pack["publisher"]},
            replace_recipe_id=current.get("recipe_id"),
        )
        record["recipe_id"] = stored["recipe_id"]
    pinned[pack_id] = record
    _write_json(_installed_path(), pinned)
    return {"ok": True, "pack_id": pack_id, "version": pack["version"], "previous_version": record["previous_version"],
            **({"recipe_id": record["recipe_id"], "recipe_status": "draft"} if "recipe_id" in record else {}),
            **({"skill_dir": record["skill_dir"], "scripts_held": sorted(record.get("scripts") or {})}
               if pack["kind"] == "skill" else {})}


def _skill_name(files: Mapping[str, bytes], pack_id: str) -> str:
    text = files["SKILL.md"].decode("utf-8")
    if not text.startswith("---"):
        raise CatalogError("PACK_INVALID", f"{pack_id}: SKILL.md needs YAML frontmatter.")
    try:
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
    except yaml.YAMLError:
        raise CatalogError("PACK_INVALID", f"{pack_id}: SKILL.md frontmatter is not valid YAML.") from None
    name = str(meta.get("name") or "")
    if name != pack_id:
        raise CatalogError("PACK_INVALID", f"{pack_id}: SKILL.md name must equal the pack id.")
    return name


def _install_skill(pack: Mapping[str, Any], files: Mapping[str, bytes], current: Mapping[str, Any]) -> Dict[str, Any]:
    name = _skill_name(files, str(pack["id"]))
    target = _skills_root() / name
    if target.exists() and not current:
        # Never overwrite a skill the owner made or registered by hand.
        raise CatalogError("SKILL_EXISTS", f"A skill named {name!r} already exists at {target}; it was not changed.")
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=str(_skills_root())))
    held = _held_dir(str(pack["id"]), str(pack["version"]))
    scripts: Dict[str, Dict[str, Any]] = {}
    try:
        for path, data in files.items():
            if path.startswith("scripts/"):
                held_path = held / path
                held_path.parent.mkdir(parents=True, exist_ok=True)
                held_path.write_bytes(data)
                os.chmod(held_path, 0o600)
                scripts[path] = {"sha256": _sha256(data), "enabled": False}
                continue
            if path != "SKILL.md" and path.split("/", 1)[0] not in _SKILL_DIRS:
                continue
            out = staging / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            os.chmod(out, 0o644)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {"skill_dir": str(target), "scripts": scripts}


def enable_script(pack_id: str, path: str, *, confirm: bool = False) -> Dict[str, Any]:
    """Install one held script into the skill after its own review (its content digest)."""
    pinned = installed()
    record = pinned.get(pack_id)
    if not record or record.get("kind") != "skill":
        raise CatalogError("PACK_NOT_INSTALLED", f"{pack_id} is not an installed skill pack.")
    script = (record.get("scripts") or {}).get(path)
    if script is None:
        raise CatalogError("SCRIPT_NOT_FOUND", f"{pack_id} has no held script {path!r}.")
    held_path = _held_dir(pack_id, record["version"]) / path
    data = held_path.read_bytes() if held_path.is_file() else b""
    if _sha256(data) != script["sha256"]:
        raise CatalogError("PACK_DIGEST_MISMATCH", f"{path} changed after install; it was not enabled.")
    preview = {"pack_id": pack_id, "path": path, "sha256": script["sha256"], "size": len(data),
               "content": data.decode("utf-8", errors="replace")[:4000]}
    if not confirm:
        raise CatalogError("CONFIRM_REQUIRED", "Read the script, then enable it with confirm=true.", script=preview)
    out = Path(record["skill_dir"]) / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    os.chmod(out, 0o755)
    script["enabled"] = True
    script["enabled_at"] = time.time()
    _write_json(_installed_path(), pinned)
    return {"ok": True, "pack_id": pack_id, "path": path, "enabled": True, "installed_at": str(out)}
