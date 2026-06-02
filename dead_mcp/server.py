"""Dead MCP Server — Grateful Dead setlist + lore tools over Streamable HTTP + OAuth 2.1.
Connect from claude.ai: https://dead-mcp.quickswoodcapital.com/mcp
Extracted from homelab-mcp; reuses its FastMCP + auto-approving OAuth pattern.
"""
import os
import sys
import logging
from pathlib import Path

# repo root (/app) on path so `import lore` and `import dead_mcp.tools` resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

from dead_mcp.oauth_provider import DeadOAuthProvider
from dead_mcp import tools as deaddb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dead-mcp")

SERVER_URL = os.getenv("SERVER_URL", "https://dead-mcp.quickswoodcapital.com")

provider = DeadOAuthProvider()

mcp = FastMCP(
    "Dead",
    instructions=(
        "Grateful Dead knowledge base: setlists, song/show statistics, "
        "archive.org & Plex recordings, HeadyVersion community votes, and a "
        "RAG lore corpus (Wikipedia, Light Into Ashes, books, the Deadcast)."
    ),
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8768")),
    stateless_http=True,
    auth=AuthSettings(
        issuer_url=SERVER_URL,
        resource_server_url=SERVER_URL,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    ),
    auth_server_provider=provider,
)

deaddb.register(mcp)

if __name__ == "__main__":
    log.info("Starting dead-mcp on port %s", os.getenv("PORT", "8768"))
    log.info("MCP endpoint: %s/mcp", SERVER_URL)
    mcp.run(transport="streamable-http")
