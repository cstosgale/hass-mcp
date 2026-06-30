"""Live Lovelace dashboard editing for Home Assistant.

Home Assistant manages dashboards (Lovelace) entirely over its WebSocket API,
and a `lovelace/config/save` pushes the change to every connected browser
instantly — no restart. HA has no partial-edit API: every change is a
read-modify-write of the dashboard's *entire* config. This module provides:

- a **raw layer** (`list_dashboards`, `get_dashboard_config`,
  `set_dashboard_config`) that maps directly onto the HA WS commands, and
- **high-level helpers** (`add_card`/`update_card`/`remove_card`/`move_card`
  and `add_view`/`remove_view`/`update_view`) that do the read-modify-write
  for you, routing every write through `set_dashboard_config`.

`set_dashboard_config` is the single write choke point and enforces all the
safety guardrails: it refuses YAML-mode dashboards (which HA can't save via
the API), validates the config's structure, backs the current config up to
disk before overwriting, and supports `dry_run` previews.

Everything talks to HA through `app.ws.call_ws`, the same authenticated
request/response primitive used by the statistics tools.
"""
from typing import Any, Dict, List, Optional, Union
import json
import logging
import os
from datetime import datetime, timezone

from app.ws import call_ws, HassWebSocketError
from app import config

logger = logging.getLogger(__name__)

# A `view` argument that selects an existing view: an integer index, or a
# string matched against each view's `path` or `title`.
ViewSelector = Union[int, str]


class LovelaceError(Exception):
    """Raised for dashboard edits that fail validation or HA constraints.

    Carries a human-readable, actionable message (bad index, YAML-mode
    dashboard, missing card `type`, etc.).
    """


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _label(url_path: Optional[str]) -> str:
    return f"'{url_path}'" if url_path else "(default)"


def _is_config_not_found(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("config_not_found", "no config found", "unknown config"))


def _is_yaml_or_not_storage(err: Exception) -> bool:
    s = str(err).lower()
    return any(
        k in s
        for k in ("yaml", "not storage", "config_not_found", "unknown config", "supported")
    )


def _summarize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    views = cfg.get("views", []) or []
    return {
        "views": len(views),
        "total_cards": sum(len(v.get("cards", []) or []) for v in views),
        "cards_per_view": [len(v.get("cards", []) or []) for v in views],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_card(card: Any, where: str) -> None:
    if not isinstance(card, dict):
        raise LovelaceError(f"{where} must be a dict.")
    if not isinstance(card.get("type"), str) or not card.get("type"):
        raise LovelaceError(f"{where} must have a non-empty string 'type'.")


def _validate_config(cfg: Any) -> None:
    """Structural-only check. Card *internals* aren't validated — HA core and
    custom cards define open-ended schemas, so we only guard the shape that
    would otherwise break the dashboard render or our own indexing."""
    if not isinstance(cfg, dict):
        raise LovelaceError("Dashboard config must be a dict containing a 'views' list.")
    views = cfg.get("views")
    if not isinstance(views, list):
        raise LovelaceError("Dashboard config must contain a 'views' list.")
    for vi, view in enumerate(views):
        if not isinstance(view, dict):
            raise LovelaceError(f"views[{vi}] must be a dict.")
        cards = view.get("cards")
        if cards is not None:
            if not isinstance(cards, list):
                raise LovelaceError(f"views[{vi}].cards must be a list.")
            for ci, card in enumerate(cards):
                _validate_card(card, f"views[{vi}].cards[{ci}]")


# ---------------------------------------------------------------------------
# View / card resolution
# ---------------------------------------------------------------------------

def _resolve_view(cfg: Dict[str, Any], view: ViewSelector) -> int:
    """Return the index of the view selected by `view` (index, path, or title)."""
    views = cfg.get("views", []) or []
    if isinstance(view, bool):  # bool is an int subclass — reject explicitly
        raise LovelaceError("view must be an int index or a string path/title.")
    if isinstance(view, int):
        if view < 0 or view >= len(views):
            raise LovelaceError(
                f"View index {view} out of range (have {len(views)} view(s))."
            )
        return view
    if isinstance(view, str):
        for i, v in enumerate(views):
            if v.get("path") == view or v.get("title") == view:
                return i
        available = [
            f"{i}:{v.get('title') or v.get('path') or '?'}" for i, v in enumerate(views)
        ]
        raise LovelaceError(f"View {view!r} not found. Available views: {available}")
    raise LovelaceError("view must be an int index or a string path/title.")


def _check_card_index(cards: List[Any], idx: int, view_idx: int) -> None:
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or idx >= len(cards):
        raise LovelaceError(
            f"card_index {idx} out of range for view {view_idx} "
            f"(have {len(cards)} card(s))."
        )


# ---------------------------------------------------------------------------
# Mode detection (YAML guard) + backups
# ---------------------------------------------------------------------------

async def _dashboard_mode(url_path: Optional[str]) -> Optional[str]:
    """Best-effort lookup of a dashboard's mode ('storage' / 'yaml').

    Returns None when the mode can't be determined; callers then rely on the
    reactive guard (translating HA's save error)."""
    try:
        if url_path is None:
            info = await call_ws("lovelace/info")
            if isinstance(info, dict):
                return info.get("mode") or info.get("resource_mode")
            return None
        dashboards = await call_ws("lovelace/dashboards/list")
        for d in dashboards or []:
            if d.get("url_path") == url_path:
                return d.get("mode")
    except HassWebSocketError:
        return None
    return None


async def _backup_current(url_path: Optional[str]) -> Optional[str]:
    """Write the dashboard's current config to the backup dir. Returns the
    backup id (filename), or None if there's nothing stored to back up."""
    payload: Dict[str, Any] = {"force": True}
    if url_path is not None:
        payload["url_path"] = url_path
    try:
        current = await call_ws("lovelace/config", **payload)
    except HassWebSocketError as err:
        if _is_config_not_found(err):
            return None  # nothing stored yet — first save creates it
        raise

    backups_dir = config.HASS_MCP_BACKUP_DIR
    os.makedirs(backups_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    slug = url_path if url_path else "default"
    backup_id = f"lovelace_{slug}_{stamp}.json"
    with open(os.path.join(backups_dir, backup_id), "w") as f:
        json.dump(current, f, indent=2)
    return backup_id


# ---------------------------------------------------------------------------
# Raw layer
# ---------------------------------------------------------------------------

async def list_dashboards() -> List[Dict[str, Any]]:
    """List dashboards, including the default 'Overview' (url_path=None).

    Each entry has at least `url_path`, `title`, and `mode` ('storage' or
    'yaml'). Only 'storage'-mode dashboards can be edited via this API."""
    default_mode = await _dashboard_mode(None)
    dashboards: List[Dict[str, Any]] = [
        {"url_path": None, "title": "Overview (default)", "mode": default_mode}
    ]
    try:
        listed = await call_ws("lovelace/dashboards/list")
    except HassWebSocketError:
        listed = []
    for d in listed or []:
        dashboards.append(
            {
                "url_path": d.get("url_path"),
                "title": d.get("title"),
                "mode": d.get("mode"),
                "id": d.get("id"),
                "icon": d.get("icon"),
            }
        )
    return dashboards


async def get_dashboard_config(url_path: Optional[str] = None) -> Dict[str, Any]:
    """Return a dashboard's full config dict.

    `url_path=None` is the default dashboard. If the dashboard has no stored
    config yet (HA auto-generates one), returns an empty scaffold
    `{"views": [], "note": ...}` rather than erroring."""
    payload: Dict[str, Any] = {"force": True}
    if url_path is not None:
        payload["url_path"] = url_path
    try:
        cfg = await call_ws("lovelace/config", **payload)
    except HassWebSocketError as err:
        if _is_config_not_found(err):
            return {
                "views": [],
                "note": (
                    f"Dashboard {_label(url_path)} has no stored config yet "
                    "(auto-generated). The first save will create one."
                ),
            }
        raise
    return cfg


async def set_dashboard_config(
    url_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Overwrite a dashboard's full config — the single write choke point.

    Enforces, in order: structural validation, YAML-mode rejection, dry-run
    preview, then backup-before-save. ⚠️ This replaces the ENTIRE dashboard
    config and is reflected live in every open browser. The prior config is
    backed up first (see `restore_dashboard`)."""
    if config is None:
        raise LovelaceError("config is required.")
    _validate_config(config)

    if await _dashboard_mode(url_path) == "yaml":
        raise LovelaceError(
            f"Dashboard {_label(url_path)} is in YAML mode and cannot be edited "
            "via the API. Edit its YAML file directly."
        )

    if dry_run:
        return {
            "dry_run": True,
            "url_path": url_path,
            "summary": _summarize(config),
            "config": config,
        }

    backup_id = await _backup_current(url_path)

    payload: Dict[str, Any] = {"config": config}
    if url_path is not None:
        payload["url_path"] = url_path
    try:
        await call_ws("lovelace/config/save", **payload)
    except HassWebSocketError as err:
        if _is_yaml_or_not_storage(err):
            raise LovelaceError(
                f"Dashboard {_label(url_path)} cannot be saved via the API "
                "(it is YAML-mode or not storage-backed)."
            )
        raise

    return {
        "success": True,
        "url_path": url_path,
        "backup_id": backup_id,
        "summary": _summarize(config),
    }


# ---------------------------------------------------------------------------
# High-level helpers — read-modify-write through set_dashboard_config
# ---------------------------------------------------------------------------

async def _load_for_edit(url_path: Optional[str]) -> Dict[str, Any]:
    cfg = await get_dashboard_config(url_path)
    cfg.pop("note", None)  # internal scaffold marker — never persist it
    cfg.setdefault("views", [])
    return cfg


async def add_card(
    url_path: Optional[str] = None,
    view: ViewSelector = 0,
    card: Optional[Dict[str, Any]] = None,
    position: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Add a card to a view. `position` inserts at that index (default: end)."""
    if card is None:
        raise LovelaceError("card is required.")
    _validate_card(card, "card")
    cfg = await _load_for_edit(url_path)
    vi = _resolve_view(cfg, view)
    cards = cfg["views"][vi].setdefault("cards", [])
    idx = len(cards) if position is None else max(0, min(position, len(cards)))
    cards.insert(idx, card)
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


async def update_card(
    url_path: Optional[str] = None,
    view: ViewSelector = 0,
    card_index: int = 0,
    card: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Replace the card at `card_index` in `view` with `card`."""
    if card is None:
        raise LovelaceError("card is required.")
    _validate_card(card, "card")
    cfg = await _load_for_edit(url_path)
    vi = _resolve_view(cfg, view)
    cards = cfg["views"][vi].get("cards", []) or []
    _check_card_index(cards, card_index, vi)
    cards[card_index] = card
    cfg["views"][vi]["cards"] = cards
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


async def remove_card(
    url_path: Optional[str] = None,
    view: ViewSelector = 0,
    card_index: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove the card at `card_index` from `view`."""
    cfg = await _load_for_edit(url_path)
    vi = _resolve_view(cfg, view)
    cards = cfg["views"][vi].get("cards", []) or []
    _check_card_index(cards, card_index, vi)
    cards.pop(card_index)
    cfg["views"][vi]["cards"] = cards
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


async def move_card(
    url_path: Optional[str] = None,
    view: ViewSelector = 0,
    card_index: int = 0,
    new_index: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reorder a card within a view, moving it from `card_index` to `new_index`."""
    cfg = await _load_for_edit(url_path)
    vi = _resolve_view(cfg, view)
    cards = cfg["views"][vi].get("cards", []) or []
    _check_card_index(cards, card_index, vi)
    if not isinstance(new_index, int) or isinstance(new_index, bool) or new_index < 0 or new_index >= len(cards):
        raise LovelaceError(
            f"new_index {new_index} out of range (have {len(cards)} card(s))."
        )
    card = cards.pop(card_index)
    cards.insert(new_index, card)
    cfg["views"][vi]["cards"] = cards
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


async def add_view(
    url_path: Optional[str] = None,
    view_config: Optional[Dict[str, Any]] = None,
    position: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Add a new view. `view_config` is the view dict (e.g. {'title': 'Garage'})."""
    if not isinstance(view_config, dict):
        raise LovelaceError(
            "view_config must be a dict describing the view (e.g. {'title': 'Garage'})."
        )
    cfg = await _load_for_edit(url_path)
    views = cfg["views"]
    idx = len(views) if position is None else max(0, min(position, len(views)))
    views.insert(idx, view_config)
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


async def remove_view(
    url_path: Optional[str] = None,
    view: ViewSelector = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove the view selected by `view` (index, path, or title)."""
    cfg = await _load_for_edit(url_path)
    vi = _resolve_view(cfg, view)
    cfg["views"].pop(vi)
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


async def update_view(
    url_path: Optional[str] = None,
    view: ViewSelector = 0,
    changes: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Shallow-merge `changes` into a view's properties (title, path, icon, …).

    Cards are preserved unless `changes` explicitly includes a `cards` key."""
    if not isinstance(changes, dict):
        raise LovelaceError(
            "changes must be a dict of view properties to update "
            "(e.g. {'title': 'New Title'})."
        )
    cfg = await _load_for_edit(url_path)
    vi = _resolve_view(cfg, view)
    cfg["views"][vi].update(changes)
    return await set_dashboard_config(url_path, cfg, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Backups: list + restore
# ---------------------------------------------------------------------------

def list_dashboard_backups(url_path: Optional[str] = None) -> List[Dict[str, str]]:
    """List on-disk backups for a dashboard, oldest first (newest last)."""
    backups_dir = config.HASS_MCP_BACKUP_DIR
    if not os.path.isdir(backups_dir):
        return []
    slug = url_path if url_path else "default"
    prefix = f"lovelace_{slug}_"
    out = []
    for name in sorted(os.listdir(backups_dir)):
        if name.startswith(prefix) and name.endswith(".json"):
            out.append({"backup_id": name, "path": os.path.join(backups_dir, name)})
    return out


async def restore_dashboard(
    url_path: Optional[str] = None,
    backup_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Restore a dashboard from a backup (newest if `backup_id` omitted).

    Goes through `set_dashboard_config`, so the current (pre-restore) config
    is itself backed up first — you can undo a restore."""
    backups = list_dashboard_backups(url_path)
    if not backups:
        raise LovelaceError(f"No backups found for dashboard {_label(url_path)}.")
    if backup_id is None:
        chosen = backups[-1]
    else:
        chosen = next((b for b in backups if b["backup_id"] == backup_id), None)
        if chosen is None:
            raise LovelaceError(
                f"Backup {backup_id!r} not found for dashboard {_label(url_path)}."
            )
    with open(chosen["path"]) as f:
        cfg = json.load(f)
    result = await set_dashboard_config(url_path, cfg, dry_run=dry_run)
    result["restored_from"] = chosen["backup_id"]
    return result
