import os
from typing import Optional

# Home Assistant configuration
HA_URL: str = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN: str = os.environ.get("HA_TOKEN", "")

# Directory where dashboard (Lovelace) configs are backed up before any save.
# Override with HASS_MCP_BACKUP_DIR. NOTE: when running in Docker, mount a
# volume at this path — otherwise backups live only in the container's
# filesystem and are lost when the container is recreated.
HASS_MCP_BACKUP_DIR: str = os.environ.get(
    "HASS_MCP_BACKUP_DIR",
    os.path.join(os.path.expanduser("~"), ".hass-mcp", "dashboard-backups"),
)

def get_ha_headers() -> dict:
    """Return the headers needed for Home Assistant API requests"""
    headers = {
        "Content-Type": "application/json",
    }
    
    # Only add Authorization header if token is provided
    if HA_TOKEN:
        headers["Authorization"] = f"Bearer {HA_TOKEN}"
    
    return headers
