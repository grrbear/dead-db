"""
Minimal auto-approving OAuth 2.1 provider for the homelab MCP server.

Flow:
1. claude.ai discovers /.well-known/oauth-protected-resource  → metadata
2. claude.ai discovers /.well-known/oauth-authorization-server → metadata
3. claude.ai registers itself dynamically via POST /register
4. claude.ai redirects browser to /authorize → we immediately redirect back with a code
5. claude.ai exchanges code for access token via POST /token
6. All subsequent MCP requests use the bearer token

Tokens are persisted to TOKEN_STORE_PATH (/data/tokens.json by default) so they
survive container restarts — no need to re-authorize in claude.ai after a redeploy.
"""
import json
import os
import secrets
import time
import logging
from pathlib import Path
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    AuthorizationParams,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger("dead-mcp.oauth")

TOKEN_STORE_PATH = Path(os.getenv("TOKEN_STORE_PATH", "/mcp-data/tokens.json"))


def _load_store() -> dict:
    try:
        if TOKEN_STORE_PATH.exists():
            return json.loads(TOKEN_STORE_PATH.read_text())
    except Exception as e:
        log.warning("Could not load token store: %s", e)
    return {"clients": {}, "access_tokens": {}, "refresh_tokens": {}}


def _save_store(store: dict) -> None:
    try:
        TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STORE_PATH.write_text(json.dumps(store, indent=2))
    except Exception as e:
        log.warning("Could not save token store: %s", e)


class DeadOAuthProvider(OAuthAuthorizationServerProvider):
    """In-memory OAuth provider that auto-approves every authorization request.
    Persists tokens to disk so container restarts don't force re-authorization."""

    def __init__(self):
        store = _load_store()
        # Reconstruct typed objects from persisted dicts
        self._clients: dict[str, OAuthClientInformationFull] = {
            k: OAuthClientInformationFull(**v) for k, v in store.get("clients", {}).items()
        }
        self._auth_codes: dict[str, AuthorizationCode] = {}  # never persisted (short-lived)
        self._access_tokens: dict[str, AccessToken] = {
            k: AccessToken(**v) for k, v in store.get("access_tokens", {}).items()
        }
        self._refresh_tokens: dict[str, RefreshToken] = {
            k: RefreshToken(**v) for k, v in store.get("refresh_tokens", {}).items()
        }
        log.info("OAuth store loaded: %d clients, %d access tokens, %d refresh tokens",
                 len(self._clients), len(self._access_tokens), len(self._refresh_tokens))

    def _persist(self) -> None:
        store = {
            "clients":        {k: v.model_dump(mode="json") for k, v in self._clients.items()},
            "access_tokens":  {k: v.model_dump(mode="json") for k, v in self._access_tokens.items()},
            "refresh_tokens": {k: v.model_dump(mode="json") for k, v in self._refresh_tokens.items()},
        }
        _save_store(store)

    # ── Client registry ────────────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        self._persist()

    # ── Authorization code flow ────────────────────────────────────────────────

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Auto-approve: generate code and redirect immediately — no user login needed."""
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code and code.client_id == client.client_id and time.time() < code.expires_at:
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        access_token  = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)

        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=None,  # never expires
            resource=authorization_code.resource,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )
        self._persist()

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    # ── Refresh token flow ─────────────────────────────────────────────────────

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        tok = self._refresh_tokens.get(refresh_token)
        if tok and tok.client_id == client.client_id:
            return tok
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)
        for tok, info in list(self._access_tokens.items()):
            if info.client_id == client.client_id:
                del self._access_tokens[tok]

        effective_scopes = scopes or refresh_token.scopes
        new_access  = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)

        self._access_tokens[new_access] = AccessToken(
            token=new_access,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=None,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=effective_scopes,
        )
        self._persist()

        return OAuthToken(
            access_token=new_access,
            token_type="bearer",
            refresh_token=new_refresh,
            scope=" ".join(effective_scopes),
        )

    # ── Token verification ─────────────────────────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._access_tokens.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)
        self._persist()
