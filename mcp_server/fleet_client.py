"""Mac A's side of the fleet (#147): ask another Mac in, wait for its owner, then read its status
and run its recipes with the credential it issued. Every call goes to that Mac's own address."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import httpx

from . import fleet

TIMEOUT_S = 8.0
STATUS_STALE_S = 30.0


async def _post(url: str, payload: Dict[str, Any], *, credential: str = "", timeout: float = TIMEOUT_S) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.post(url, content=json.dumps(payload), headers=headers)


async def _get(url: str, *, credential: str, timeout: float = TIMEOUT_S) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(url, headers={"Authorization": f"Bearer {credential}"})


def _error(response: httpx.Response, fallback: str) -> fleet.FleetError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = str(body.get("error") or fallback).upper()
    return fleet.FleetError(code, str(body.get("message") or f"The other Mac answered {response.status_code}."),
                            response.status_code if 400 <= response.status_code < 500 else 502)


async def add(invitation: str) -> Dict[str, Any]:
    """Present the other Mac's invitation; the new Mac waits for its owner to allow it."""
    base, code = fleet.invitation_parts(invitation)
    if base == fleet.own_url():
        raise fleet.FleetError("FLEET_SELF", "That invitation is from this Mac.")
    me = fleet.identity()
    try:
        response = await _post(f"{base}/fleet/pair", {"code": code, "device_id": me["device_id"], "name": me["name"]})
    except httpx.HTTPError as exc:
        raise fleet.FleetError("FLEET_UNREACHABLE", f"The other Mac did not answer at {base} ({type(exc).__name__}).",
                               502) from exc
    if response.status_code != 200:
        raise _error(response, "FLEET_PAIR_FAILED")
    body = response.json()
    server = body.get("server") or {}
    for row in fleet.store().peers():  # the same Mac paired again replaces its old entry
        if server.get("device_id") and row.get("device_id") == server.get("device_id"):
            fleet.store().remove_peer(row["peer_id"])
    peer = fleet.store().add_pending_peer(
        url=base, name=str(server.get("name") or base), device_id=str(server.get("device_id") or ""),
        request_id=str(body["request_id"]), poll_secret=str(body["poll_secret"]),
        check_code_value=str(body["check_code"]), permission=str(body.get("permission") or ""))
    return fleet.public_peer(peer)


async def _check_waiting(peer: Dict[str, Any]) -> None:
    try:
        response = await _post(f"{peer['url']}/fleet/pair/status",
                               {"request_id": peer["request_id"], "poll_secret": peer["poll_secret"]}, timeout=5.0)
    except httpx.HTTPError:
        fleet.store().update_peer(peer["peer_id"], last_checked_at=time.time(), last_error="unreachable")
        return
    if response.status_code != 200:
        fleet.store().update_peer(peer["peer_id"], state="expired", poll_secret=None, last_checked_at=time.time())
        return
    body = response.json()
    status = body.get("status")
    if status == "allowed" and body.get("credential"):
        fleet.store().save_credential(peer["peer_id"], str(body["credential"]))
        fleet.store().update_peer(peer["peer_id"], state="paired", permission=body.get("permission"),
                                  poll_secret=None, request_id=None, check_code=None)
        await refresh(fleet.store().peer(peer["peer_id"]), force=True)
    elif status in {"denied", "expired"}:
        fleet.store().update_peer(peer["peer_id"], state="refused" if status == "denied" else "expired",
                                  poll_secret=None, check_code=None, last_checked_at=time.time())


async def refresh(peer: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """Ask one paired Mac how it is (at most every 30 seconds unless forced)."""
    if peer["state"] == "waiting":
        await _check_waiting(peer)
        return fleet.public_peer(fleet.store().peer(peer["peer_id"]))
    if peer["state"] != "paired":
        return fleet.public_peer(peer)
    if not force and peer.get("last_checked_at") and time.time() - float(peer["last_checked_at"]) < STATUS_STALE_S:
        return fleet.public_peer(peer)
    credential = fleet.store().credential(peer["peer_id"])
    now = time.time()
    try:
        response = await _get(f"{peer['url']}/fleet/status", credential=credential, timeout=5.0)
    except httpx.HTTPError as exc:
        fleet.store().update_peer(peer["peer_id"], last_checked_at=now, last_error=f"unreachable ({type(exc).__name__})")
        return fleet.public_peer(fleet.store().peer(peer["peer_id"]))
    if response.status_code == 401:
        fleet.store().update_peer(peer["peer_id"], state="removed_there", last_checked_at=now,
                                  last_error="That Mac no longer lets this one in.")
    elif response.status_code != 200:
        fleet.store().update_peer(peer["peer_id"], last_checked_at=now, last_error=f"answered {response.status_code}")
    else:
        status = response.json()
        fleet.store().update_peer(peer["peer_id"], last_checked_at=now, last_ok_at=now, last_error=None,
                                  last_status=json.dumps(status), name=str(status.get("name") or peer["name"]),
                                  permission=status.get("your_permission") or peer.get("permission"))
    return fleet.public_peer(fleet.store().peer(peer["peer_id"]))


async def refresh_all(*, force: bool = False) -> List[Dict[str, Any]]:
    peers = fleet.store().peers()
    return list(await asyncio.gather(*(refresh(peer, force=force) for peer in peers)))


def resolve(peer: str) -> Dict[str, Any]:
    """A paired Mac by its peer_id or (unique, case-insensitive) name."""
    rows = fleet.store().peers()
    for row in rows:
        if row["peer_id"] == peer:
            return row
    named = [row for row in rows if row["name"].casefold() == str(peer or "").strip().casefold()]
    if len(named) == 1:
        return named[0]
    raise fleet.FleetError("FLEET_PEER_UNKNOWN", "No paired Mac has that id or name; fleet action=list shows them.", 404)


def _paired(peer: str) -> Dict[str, Any]:
    row = resolve(peer)
    if row["state"] != "paired":
        raise fleet.FleetError("FLEET_NOT_PAIRED", f"{row['name']} is not paired (state: {row['state']}).", 409)
    return row


async def recipes(peer: str) -> Dict[str, Any]:
    row = _paired(peer)
    response = await _get(f"{row['url']}/fleet/recipes", credential=fleet.store().credential(row["peer_id"]))
    if response.status_code != 200:
        raise _error(response, "FLEET_RECIPES_FAILED")
    return {"ok": True, "mac": row["name"], **response.json()}


async def run_recipe(peer: str, recipe_id: str, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = _paired(peer)
    try:
        response = await _post(f"{row['url']}/fleet/recipes/run", {"recipe_id": recipe_id, "values": values or {}},
                               credential=fleet.store().credential(row["peer_id"]), timeout=600.0)
    except httpx.HTTPError as exc:
        raise fleet.FleetError("FLEET_UNREACHABLE", f"{row['name']} did not answer ({type(exc).__name__}); the "
                               "recipe may or may not have run there.", 502) from exc
    if response.status_code != 200:
        raise _error(response, "FLEET_RUN_FAILED")
    return {"ok": True, "mac": row["name"], **response.json()}


async def remove(peer_id: str) -> None:
    """Stop seeing a Mac: it is asked to forget this one too, then it is removed here either way."""
    row = fleet.store().peer(peer_id)
    credential = fleet.store().credential(peer_id)
    if row["state"] == "paired" and credential:
        try:
            await _post(f"{row['url']}/fleet/forget", {}, credential=credential, timeout=5.0)
        except httpx.HTTPError:
            pass  # unreachable: its owner can still remove this Mac there
    fleet.store().remove_peer(peer_id)
