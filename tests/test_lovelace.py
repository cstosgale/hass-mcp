"""Unit tests for app.lovelace — live Lovelace dashboard editing.

All Home Assistant interaction goes through `app.ws.call_ws`. These tests
replace it with `FakeWS`, an in-memory dispatcher keyed by WS message type
that models a small HA instance (a config store + a dashboards list). This
lets us assert real read-modify-write behaviour and exactly what got saved,
without any sockets.

Backups are redirected to a pytest tmp_path so the real ~/.hass-mcp dir is
never touched.
"""
import copy
import json
import os

import pytest

from app.ws import HassWebSocketError
from app import config as app_config
import app.lovelace as lovelace
from app.lovelace import LovelaceError


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeWS:
    """In-memory stand-in for app.ws.call_ws, routing by message type."""

    def __init__(self):
        # url_path (None == default) -> config dict
        self.config_store = {None: {"views": [{"title": "Home", "cards": []}]}}
        self.dashboards = [
            {"id": "abc", "url_path": "test-dash", "title": "Test", "mode": "storage"},
            {"id": "def", "url_path": "yaml-dash", "title": "YAML", "mode": "yaml"},
        ]
        self.info = {"mode": "storage"}
        self.saved = []  # list of (url_path, config) in call order

    async def __call__(self, message_type, **payload):
        if message_type == "lovelace/info":
            return self.info
        if message_type == "lovelace/dashboards/list":
            return self.dashboards
        if message_type == "lovelace/config":
            url_path = payload.get("url_path")
            if url_path in self.config_store:
                # HA returns freshly-deserialized JSON each call — never an
                # alias of caller-held state. Deep-copy to model that.
                return copy.deepcopy(self.config_store[url_path])
            raise HassWebSocketError(
                "WS request 'lovelace/config' failed: "
                "{'code': 'config_not_found', 'message': 'No config found.'}"
            )
        if message_type == "lovelace/config/save":
            url_path = payload.get("url_path")
            self.saved.append((url_path, copy.deepcopy(payload["config"])))
            self.config_store[url_path] = copy.deepcopy(payload["config"])
            return None
        raise AssertionError(f"unexpected WS message type: {message_type}")


@pytest.fixture
def fake_ws(monkeypatch, tmp_path):
    fake = FakeWS()
    monkeypatch.setattr("app.lovelace.call_ws", fake)
    monkeypatch.setattr("app.config.HASS_MCP_BACKUP_DIR", str(tmp_path))
    return fake


# --------------------------------------------------------------------------
# Raw layer
# --------------------------------------------------------------------------

async def test_list_dashboards_includes_default_and_named(fake_ws):
    dashboards = await lovelace.list_dashboards()
    by_path = {d["url_path"]: d for d in dashboards}
    assert None in by_path  # default dashboard present
    assert by_path[None]["mode"] == "storage"
    assert by_path["test-dash"]["mode"] == "storage"
    assert by_path["yaml-dash"]["mode"] == "yaml"


async def test_get_dashboard_config_returns_stored(fake_ws):
    cfg = await lovelace.get_dashboard_config("test-dash")
    fake_ws.config_store["test-dash"] = {"views": [{"title": "X"}]}
    cfg = await lovelace.get_dashboard_config("test-dash")
    assert cfg["views"][0]["title"] == "X"


async def test_get_dashboard_config_scaffolds_when_missing(fake_ws):
    # "new-dash" isn't in the store -> HA raises config_not_found.
    cfg = await lovelace.get_dashboard_config("new-dash")
    assert cfg["views"] == []
    assert "note" in cfg


async def test_set_dashboard_config_backs_up_then_saves(fake_ws):
    new_cfg = {"views": [{"title": "Home", "cards": [{"type": "markdown", "content": "hi"}]}]}
    result = await lovelace.set_dashboard_config(None, new_cfg)

    assert result["success"] is True
    assert fake_ws.saved == [(None, new_cfg)]
    # A backup file for the prior config was written.
    backup_dir = app_config.HASS_MCP_BACKUP_DIR
    files = os.listdir(backup_dir)
    assert len(files) == 1 and files[0].startswith("lovelace_default_")
    assert result["backup_id"] == files[0]
    # Backup holds the ORIGINAL config (one empty-card view), not the new one.
    with open(os.path.join(backup_dir, files[0])) as f:
        backed_up = json.load(f)
    assert backed_up == {"views": [{"title": "Home", "cards": []}]}


async def test_dry_run_does_not_save(fake_ws):
    new_cfg = {"views": [{"title": "Home", "cards": []}]}
    result = await lovelace.set_dashboard_config(None, new_cfg, dry_run=True)
    assert result["dry_run"] is True
    assert "summary" in result
    assert fake_ws.saved == []


async def test_yaml_mode_dashboard_rejected(fake_ws):
    with pytest.raises(LovelaceError, match="YAML"):
        await lovelace.set_dashboard_config("yaml-dash", {"views": []})
    assert fake_ws.saved == []


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

async def test_validation_requires_views_list(fake_ws):
    with pytest.raises(LovelaceError, match="views"):
        await lovelace.set_dashboard_config(None, {"not_views": 1})


async def test_validation_requires_card_type(fake_ws):
    bad = {"views": [{"cards": [{"content": "no type"}]}]}
    with pytest.raises(LovelaceError, match="type"):
        await lovelace.set_dashboard_config(None, bad)


# --------------------------------------------------------------------------
# Card ops
# --------------------------------------------------------------------------

async def test_add_card_appends(fake_ws):
    card = {"type": "markdown", "content": "hello"}
    await lovelace.add_card(None, view=0, card=card)
    _, saved = fake_ws.saved[-1]
    assert saved["views"][0]["cards"][-1] == card


async def test_add_card_at_position(fake_ws):
    fake_ws.config_store[None] = {
        "views": [{"title": "Home", "cards": [{"type": "a"}, {"type": "b"}]}]
    }
    await lovelace.add_card(None, view=0, card={"type": "x"}, position=1)
    _, saved = fake_ws.saved[-1]
    assert [c["type"] for c in saved["views"][0]["cards"]] == ["a", "x", "b"]


async def test_add_card_requires_type(fake_ws):
    with pytest.raises(LovelaceError, match="type"):
        await lovelace.add_card(None, view=0, card={"content": "x"})


async def test_update_card_replaces(fake_ws):
    fake_ws.config_store[None] = {"views": [{"cards": [{"type": "a"}, {"type": "b"}]}]}
    await lovelace.update_card(None, view=0, card_index=1, card={"type": "c"})
    _, saved = fake_ws.saved[-1]
    assert [c["type"] for c in saved["views"][0]["cards"]] == ["a", "c"]


async def test_remove_card(fake_ws):
    fake_ws.config_store[None] = {"views": [{"cards": [{"type": "a"}, {"type": "b"}]}]}
    await lovelace.remove_card(None, view=0, card_index=0)
    _, saved = fake_ws.saved[-1]
    assert [c["type"] for c in saved["views"][0]["cards"]] == ["b"]


async def test_move_card_reorders(fake_ws):
    fake_ws.config_store[None] = {
        "views": [{"cards": [{"type": "a"}, {"type": "b"}, {"type": "c"}]}]
    }
    await lovelace.move_card(None, view=0, card_index=0, new_index=2)
    _, saved = fake_ws.saved[-1]
    assert [c["type"] for c in saved["views"][0]["cards"]] == ["b", "c", "a"]


async def test_card_index_out_of_range(fake_ws):
    fake_ws.config_store[None] = {"views": [{"cards": [{"type": "a"}]}]}
    with pytest.raises(LovelaceError, match="out of range"):
        await lovelace.remove_card(None, view=0, card_index=5)


# --------------------------------------------------------------------------
# View ops + resolution
# --------------------------------------------------------------------------

async def test_resolve_view_by_title_and_path(fake_ws):
    fake_ws.config_store[None] = {
        "views": [
            {"title": "Living Room", "path": "living"},
            {"title": "Kitchen", "path": "kitchen"},
        ]
    }
    await lovelace.add_card(None, view="Kitchen", card={"type": "x"})
    _, saved = fake_ws.saved[-1]
    assert saved["views"][1]["cards"][-1]["type"] == "x"

    await lovelace.add_card(None, view="living", card={"type": "y"})
    _, saved = fake_ws.saved[-1]
    assert saved["views"][0]["cards"][-1]["type"] == "y"


async def test_resolve_view_not_found(fake_ws):
    with pytest.raises(LovelaceError, match="not found"):
        await lovelace.add_card(None, view="Nonexistent", card={"type": "x"})


async def test_add_view(fake_ws):
    await lovelace.add_view(None, view_config={"title": "New View"})
    _, saved = fake_ws.saved[-1]
    assert saved["views"][-1]["title"] == "New View"


async def test_remove_view(fake_ws):
    fake_ws.config_store[None] = {"views": [{"title": "A"}, {"title": "B"}]}
    await lovelace.remove_view(None, view="A")
    _, saved = fake_ws.saved[-1]
    assert [v["title"] for v in saved["views"]] == ["B"]


async def test_update_view_retitle_preserves_cards(fake_ws):
    fake_ws.config_store[None] = {
        "views": [{"title": "Old", "cards": [{"type": "a"}]}]
    }
    await lovelace.update_view(None, view=0, changes={"title": "New"})
    _, saved = fake_ws.saved[-1]
    assert saved["views"][0]["title"] == "New"
    assert saved["views"][0]["cards"] == [{"type": "a"}]  # cards untouched


# --------------------------------------------------------------------------
# Sections-type views
# --------------------------------------------------------------------------

def _sections_view_config():
    """A 'sections'-type view with one section containing a heading."""
    return {
        "views": [
            {
                "type": "sections",
                "title": "Air Quality",
                "path": "air-quality",
                "sections": [
                    {"type": "grid", "cards": [
                        {"type": "heading", "heading": "Temperature"},
                        {"type": "history-graph", "entities": ["sensor.temp"]},
                    ]},
                    {"type": "grid", "cards": [
                        {"type": "heading", "heading": "Controls"},
                    ]},
                ],
            }
        ]
    }


async def test_add_card_to_section_by_index(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    card = {"type": "history-graph", "entities": ["sensor.humidity"]}
    await lovelace.add_card(None, view=0, card=card, section=0)
    _, saved = fake_ws.saved[-1]
    # Went into sections[0].cards, NOT a top-level cards[] array.
    assert saved["views"][0]["sections"][0]["cards"][-1] == card
    assert "cards" not in saved["views"][0]


async def test_add_card_to_section_by_heading(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    await lovelace.add_card(None, view=0, card={"type": "button"}, section="Controls")
    _, saved = fake_ws.saved[-1]
    assert saved["views"][0]["sections"][1]["cards"][-1]["type"] == "button"


async def test_add_card_section_at_position(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    await lovelace.add_card(None, view=0, card={"type": "x"}, section=0, position=1)
    _, saved = fake_ws.saved[-1]
    types = [c.get("type") for c in saved["views"][0]["sections"][0]["cards"]]
    assert types == ["heading", "x", "history-graph"]


async def test_sections_view_without_section_is_rejected(fake_ws):
    """The silent-failure guard: editing a sections view needs a section."""
    fake_ws.config_store[None] = _sections_view_config()
    with pytest.raises(LovelaceError, match="sections' view"):
        await lovelace.add_card(None, view=0, card={"type": "x"})
    assert fake_ws.saved == []


async def test_section_index_out_of_range(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    with pytest.raises(LovelaceError, match="section index .* out of range"):
        await lovelace.add_card(None, view=0, card={"type": "x"}, section=9)


async def test_section_as_numeric_string_is_index(fake_ws):
    """MCP clients may stringify a numeric section arg — "0" must mean index 0."""
    fake_ws.config_store[None] = _sections_view_config()
    await lovelace.add_card(None, view=0, card={"type": "x"}, section="0")
    _, saved = fake_ws.saved[-1]
    assert saved["views"][0]["sections"][0]["cards"][-1]["type"] == "x"


async def test_section_numeric_string_out_of_range(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    with pytest.raises(LovelaceError, match="section index .* out of range"):
        await lovelace.add_card(None, view=0, card={"type": "x"}, section="9")


async def test_view_as_numeric_string_is_index(fake_ws):
    """Same coercion for the `view` selector: "1" means index 1."""
    fake_ws.config_store[None] = {"views": [{"title": "A"}, {"title": "B", "cards": []}]}
    await lovelace.add_card(None, view="1", card={"type": "x"})
    _, saved = fake_ws.saved[-1]
    assert saved["views"][1]["cards"][-1]["type"] == "x"


async def test_section_on_classic_view_is_rejected(fake_ws):
    """Passing section to a non-sections view is an error, not silently ignored."""
    fake_ws.config_store[None] = {"views": [{"title": "Home", "cards": []}]}
    with pytest.raises(LovelaceError, match="not a 'sections' view"):
        await lovelace.add_card(None, view=0, card={"type": "x"}, section=0)


async def test_update_and_remove_card_in_section(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    await lovelace.update_card(None, view=0, section=0, card_index=1,
                               card={"type": "gauge"})
    _, saved = fake_ws.saved[-1]
    assert saved["views"][0]["sections"][0]["cards"][1]["type"] == "gauge"

    await lovelace.remove_card(None, view=0, section=0, card_index=0)
    _, saved = fake_ws.saved[-1]
    assert [c["type"] for c in saved["views"][0]["sections"][0]["cards"]] == ["gauge"]


async def test_move_card_in_section(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    await lovelace.move_card(None, view=0, section=0, card_index=0, new_index=1)
    _, saved = fake_ws.saved[-1]
    assert [c["type"] for c in saved["views"][0]["sections"][0]["cards"]] == [
        "history-graph", "heading",
    ]


async def test_list_view_sections(fake_ws):
    fake_ws.config_store[None] = _sections_view_config()
    sections = await lovelace.list_view_sections(None, view=0)
    assert sections == [
        {"index": 0, "title": None, "heading": "Temperature", "card_count": 2},
        {"index": 1, "title": None, "heading": "Controls", "card_count": 1},
    ]


async def test_list_view_sections_on_classic_view_errors(fake_ws):
    fake_ws.config_store[None] = {"views": [{"title": "Home", "cards": []}]}
    with pytest.raises(LovelaceError, match="not a 'sections' view"):
        await lovelace.list_view_sections(None, view=0)


async def test_validation_rejects_bad_card_in_section(fake_ws):
    bad = {"views": [{"type": "sections", "sections": [
        {"cards": [{"content": "no type"}]}
    ]}]}
    with pytest.raises(LovelaceError, match="type"):
        await lovelace.set_dashboard_config(None, bad)


# --------------------------------------------------------------------------
# Backups: list + restore
# --------------------------------------------------------------------------

async def test_restore_dashboard_round_trip(fake_ws):
    original = {"views": [{"title": "Home", "cards": []}]}
    assert fake_ws.config_store[None] == original

    # Mutate (creates a backup of the original), then change again.
    await lovelace.add_card(None, view=0, card={"type": "markdown", "content": "1"})
    backups = lovelace.list_dashboard_backups(None)
    assert len(backups) == 1

    # Restore newest backup -> dashboard goes back to original.
    result = await lovelace.restore_dashboard(None)
    assert result["restored_from"] == backups[0]["backup_id"]
    assert fake_ws.config_store[None] == original


async def test_restore_no_backups_errors(fake_ws):
    with pytest.raises(LovelaceError, match="[Nn]o backups"):
        await lovelace.restore_dashboard("test-dash")
