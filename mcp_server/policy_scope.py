from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional


class AccessMode(str, Enum):
    """Ordered access modes shared by policy and delegated-agent scopes."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL = "full"


_ACCESS_MODE_ORDER = {
    AccessMode.READ_ONLY: 0,
    AccessMode.WORKSPACE_WRITE: 1,
    AccessMode.FULL: 2,
}


def normalize_access_mode(value: AccessMode | str) -> AccessMode:
    if isinstance(value, AccessMode):
        return value
    normalized = str(value).strip().lower().replace("-", "_")
    aliases = {
        "read_only": AccessMode.READ_ONLY,
        "readonly": AccessMode.READ_ONLY,
        "workspace_write": AccessMode.WORKSPACE_WRITE,
        "full": AccessMode.FULL,
        "danger_full_access": AccessMode.FULL,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown access mode: {value}") from exc


def access_mode_allows(ceiling: AccessMode | str, requested: AccessMode | str) -> bool:
    return _ACCESS_MODE_ORDER[normalize_access_mode(requested)] <= _ACCESS_MODE_ORDER[normalize_access_mode(ceiling)]


def _canonical_path(value: str | os.PathLike[str]) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def path_is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Use resolved paths so symlinks cannot escape an allowed root."""

    candidate = _canonical_path(path)
    allowed_root = _canonical_path(root)
    try:
        return os.path.commonpath((candidate, allowed_root)) == allowed_root
    except ValueError:
        return False


def _normalize_values(values: Optional[Iterable[str]]) -> Optional[tuple[str, ...]]:
    if values is None:
        return None
    return tuple(sorted({str(value) for value in values}))


def _normalize_roots(values: Optional[Iterable[str | os.PathLike[str]]]) -> Optional[tuple[str, ...]]:
    if values is None:
        return None
    return tuple(sorted({_canonical_path(value) for value in values}))


@dataclass(frozen=True)
class ResourceScope:
    """Composable resource limits; ``None`` means unrestricted for that dimension.

    Agent integration hook: intersect the parent scope with the requested child
    scope before spawn, persist ``to_dict()`` in agent metadata, and use the
    resulting ``access_mode`` as the child process' maximum access mode.
    """

    path_roots: Optional[tuple[str, ...]] = None
    browser_tabs: Optional[tuple[str, ...]] = None
    terminal_ids: Optional[tuple[str, ...]] = None
    job_ids: Optional[tuple[str, ...]] = None
    tool_families: Optional[tuple[str, ...]] = None
    access_mode: AccessMode = AccessMode.FULL

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_roots", _normalize_roots(self.path_roots))
        object.__setattr__(self, "browser_tabs", _normalize_values(self.browser_tabs))
        object.__setattr__(self, "terminal_ids", _normalize_values(self.terminal_ids))
        object.__setattr__(self, "job_ids", _normalize_values(self.job_ids))
        object.__setattr__(self, "tool_families", _normalize_values(self.tool_families))
        object.__setattr__(self, "access_mode", normalize_access_mode(self.access_mode))

    @classmethod
    def unrestricted(cls) -> "ResourceScope":
        return cls()

    @classmethod
    def from_dict(cls, value: Optional[dict[str, Any]]) -> "ResourceScope":
        if not value:
            return cls.unrestricted()
        allowed = {"path_roots", "browser_tabs", "terminal_ids", "job_ids", "tool_families", "access_mode"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"Unknown scope field(s): {', '.join(unknown)}")
        return cls(
            path_roots=value.get("path_roots"),
            browser_tabs=value.get("browser_tabs"),
            terminal_ids=value.get("terminal_ids"),
            job_ids=value.get("job_ids"),
            tool_families=value.get("tool_families"),
            access_mode=value.get("access_mode", AccessMode.FULL),
        )

    def intersect(self, other: "ResourceScope") -> "ResourceScope":
        return intersect_scopes(self, other)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_roots": None if self.path_roots is None else list(self.path_roots),
            "browser_tabs": None if self.browser_tabs is None else list(self.browser_tabs),
            "terminal_ids": None if self.terminal_ids is None else list(self.terminal_ids),
            "job_ids": None if self.job_ids is None else list(self.job_ids),
            "tool_families": None if self.tool_families is None else list(self.tool_families),
            "access_mode": self.access_mode.value,
        }


@dataclass(frozen=True)
class ScopeRequest:
    path: Optional[str] = None
    browser_tab: Optional[str] = None
    terminal_id: Optional[str] = None
    job_id: Optional[str] = None
    tool_family: Optional[str] = None
    access_mode: Optional[AccessMode | str] = None


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def code(self) -> str:
        return "scope_allowed" if self.allowed else "scope_denied"


def _intersect_values(left: Optional[tuple[str, ...]], right: Optional[tuple[str, ...]]) -> Optional[tuple[str, ...]]:
    if left is None:
        return right
    if right is None:
        return left
    return tuple(sorted(set(left).intersection(right)))


def _intersect_roots(left: Optional[tuple[str, ...]], right: Optional[tuple[str, ...]]) -> Optional[tuple[str, ...]]:
    if left is None:
        return right
    if right is None:
        return left
    roots: set[str] = set()
    for left_root in left:
        for right_root in right:
            if path_is_within(left_root, right_root):
                roots.add(left_root)
            elif path_is_within(right_root, left_root):
                roots.add(right_root)
    return tuple(sorted(roots))


def intersect_scopes(left: ResourceScope, right: ResourceScope) -> ResourceScope:
    ceiling = left.access_mode if access_mode_allows(right.access_mode, left.access_mode) else right.access_mode
    return ResourceScope(
        path_roots=_intersect_roots(left.path_roots, right.path_roots),
        browser_tabs=_intersect_values(left.browser_tabs, right.browser_tabs),
        terminal_ids=_intersect_values(left.terminal_ids, right.terminal_ids),
        job_ids=_intersect_values(left.job_ids, right.job_ids),
        tool_families=_intersect_values(left.tool_families, right.tool_families),
        access_mode=ceiling,
    )


def evaluate_scope(scope: ResourceScope, request: ScopeRequest) -> ScopeDecision:
    reasons: list[str] = []
    if request.path is not None and scope.path_roots is not None:
        if not any(path_is_within(request.path, root) for root in scope.path_roots):
            reasons.append("path_not_allowed")
    for value, allowed, reason in (
        (request.browser_tab, scope.browser_tabs, "browser_tab_not_allowed"),
        (request.terminal_id, scope.terminal_ids, "terminal_id_not_allowed"),
        (request.job_id, scope.job_ids, "job_id_not_allowed"),
        (request.tool_family, scope.tool_families, "tool_family_not_allowed"),
    ):
        if value is not None and allowed is not None and "*" not in allowed and value not in allowed:
            reasons.append(reason)
    if request.access_mode is not None and not access_mode_allows(scope.access_mode, request.access_mode):
        reasons.append("access_mode_exceeds_ceiling")
    return ScopeDecision(allowed=not reasons, reasons=tuple(reasons))


def child_scope(parent: ResourceScope, requested: ResourceScope) -> ResourceScope:
    """Narrow-only integration hook for delegated-agent spawn implementations."""

    return intersect_scopes(parent, requested)


def scope_contains(parent: ResourceScope, requested: ResourceScope) -> bool:
    """Return True only when requested is no broader than parent in every dimension."""

    return intersect_scopes(parent, requested) == requested
