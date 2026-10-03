# src/protocols/oauth.py
"""
OAuth 2.1 authorization server for the MCP endpoint.

The Gemini app (and any spec-compliant MCP client) only connects to remote MCP
servers over OAuth 2.1, so bearer tokens alone are not enough for third-party
app clients. This module implements the authorization side of RFC 8414 / RFC
9728 / RFC 7591 for a single-user server:

    /.well-known/oauth-authorization-server   RFC 8414 metadata
    /.well-known/oauth-protected-resource     RFC 9728 metadata
    POST /oauth/register                     RFC 7591 dynamic client registration
    GET+POST /oauth/authorize                authorization endpoint (owner approval)
    POST /oauth/token                        authorization_code (PKCE) + refresh_token

Single-user approval model: the authorize page asks for the server owner's
MCP_AUTH_TOKEN before issuing a code. No sessions, no user database.
"""
import base64
import hashlib
import hmac
import logging
from urllib.parse import urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from src.core.config import settings
from src.protocols.token_store import get_store

logger = logging.getLogger("de-job-intelligence.oauth")


def _issuer() -> str:
    return settings.MCP_PUBLIC_URL.rstrip("/")


# ---------------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------------


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    if method == "S256":
        return hmac.compare_digest(_s256_challenge(verifier), challenge)
    if method == "plain":
        return hmac.compare_digest(verifier, challenge)
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_redirect_uri(uri: str) -> bool:
    try:
        parts = urlparse(uri)
    except Exception:
        return False
    if not parts.hostname or parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    # http only for loopback (native apps, RFC 8252)
    return parts.scheme == "http" and parts.hostname in (
        "localhost",
        "127.0.0.1",
        "[::1]",
    )


def _redirect_with_params(redirect_uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=303)


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status
    )


# ---------------------------------------------------------------------------
# Discovery metadata
# ---------------------------------------------------------------------------


async def authorization_server_metadata(request: Request) -> JSONResponse:
    iss = _issuer()
    return JSONResponse(
        {
            "issuer": iss,
            "authorization_endpoint": f"{iss}/oauth/authorize",
            "token_endpoint": f"{iss}/oauth/token",
            "registration_endpoint": f"{iss}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "scopes_supported": ["mcp"],
        }
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    iss = _issuer()
    return JSONResponse(
        {
            "resource": iss,
            "authorization_servers": [iss],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
    )


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------


async def register_client(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _oauth_error("invalid_request", "Request body must be JSON.")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty list.")
    for uri in redirect_uris:
        if not isinstance(uri, str) or not _valid_redirect_uri(uri):
            return _oauth_error("invalid_redirect_uri", f"Rejected redirect URI: {uri!r}.")

    creds = get_store().register_client(
        redirect_uris=redirect_uris,
        client_name=str(body.get("client_name", ""))[:120],
        scope=str(body.get("scope", ""))[:200],
    )
    logger.info("Registered OAuth client %s", creds["client_id"])
    return JSONResponse(
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Authorization endpoint: owner approval
# ---------------------------------------------------------------------------


_APPROVAL_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize MCP access</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;padding:3rem 1rem;margin:0}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:2rem;max-width:26rem;width:100%}}
h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{color:#94a3b8;font-size:.9rem;line-height:1.5}}
code{{background:#0f172a;padding:.15rem .4rem;border-radius:4px;font-size:.8rem}}
label{{display:block;font-size:.8rem;color:#94a3b8;margin:1rem 0 .35rem}}
input{{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:.6rem .8rem;font-size:.9rem}}
.row{{display:flex;gap:.75rem;margin-top:1.25rem}}
button{{flex:1;border:0;border-radius:8px;padding:.7rem;font-size:.95rem;cursor:pointer}}
.approve{{background:#22c55e;color:#052e16;font-weight:600}}
.deny{{background:#334155;color:#e2e8f0}}
</style></head><body><div class="card">
<h1>Authorize <code>{client_name}</code>?</h1>
<p>This app wants to access your MCP server tools (read saved jobs, queue
applications, run workers). Only the server owner can approve &mdash; enter
your <code>MCP_AUTH_TOKEN</code> to confirm it&rsquo;s you.</p>
<form method="post">
<input type="hidden" name="client_id" value="{client_id}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="state" value="{state}">
<input type="hidden" name="scope" value="{scope}">
<input type="hidden" name="code_challenge" value="{code_challenge}">
<input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
<label>Owner token (MCP_AUTH_TOKEN)</label>
<input type="password" name="owner_token" autocomplete="off" required>
<div class="row">
<button class="approve" type="submit" name="decision" value="approve">Authorize</button>
<button class="deny" type="submit" name="decision" value="deny">Deny</button>
</div></form></div></body></html>"""

_ERROR_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Authorization error</title></head>
<body style="font-family:system-ui,sans-serif;padding:3rem"><h1>Authorization error</h1>
<p>{message}</p></body></html>"""


def _validate_authorize_params(params) -> tuple:
    """Returns (client_record, redirect_uri, error_response)."""
    store = get_store()
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    record = store.get_client(client_id) if client_id else None
    if record is None:
        return None, "", HTMLResponse(
            _ERROR_PAGE.format(message="Unknown client_id."), status_code=400
        )
    if redirect_uri not in record["redirect_uris"]:
        return None, "", HTMLResponse(
            _ERROR_PAGE.format(message="redirect_uri is not registered for this client."),
            status_code=400,
        )
    return record, redirect_uri, None


async def authorize_get(request: Request) -> HTMLResponse | RedirectResponse:
    params = request.query_params
    record, redirect_uri, error = _validate_authorize_params(params)
    if error is not None:
        return error
    if params.get("response_type") != "code":
        return _redirect_with_params(
            redirect_uri,
            {"error": "unsupported_response_type", "state": params.get("state", "")},
        )
    if not params.get("code_challenge"):
        return _redirect_with_params(
            redirect_uri,
            {"error": "invalid_request", "error_description": "PKCE code_challenge is required.", "state": params.get("state", "")},
        )
    client_name = record.get("client_name") or params.get("client_id")
    return HTMLResponse(
        _APPROVAL_PAGE.format(
            client_name=client_name,
            client_id=params.get("client_id", ""),
            redirect_uri=redirect_uri,
            state=params.get("state", ""),
            scope=params.get("scope", "mcp"),
            code_challenge=params.get("code_challenge", ""),
            code_challenge_method=params.get("code_challenge_method", "S256"),
        )
    )


async def authorize_post(request: Request):
    form = await request.form()
    record, redirect_uri, error = _validate_authorize_params(form)
    if error is not None:
        return error
    state = form.get("state", "")

    if form.get("decision") != "approve":
        return _redirect_with_params(redirect_uri, {"error": "access_denied", "state": state})

    owner_token = form.get("owner_token", "")
    if not hmac.compare_digest(owner_token, settings.MCP_AUTH_TOKEN):
        logger.warning("Rejected OAuth approval with wrong owner token")
        return HTMLResponse(
            _ERROR_PAGE.format(message="Owner token incorrect. Approval denied."),
            status_code=403,
        )

    code = get_store().create_code(
        client_id=form.get("client_id", ""),
        redirect_uri=redirect_uri,
        scope=form.get("scope", "mcp"),
        code_challenge=form.get("code_challenge", ""),
        code_challenge_method=form.get("code_challenge_method", "S256") or "S256",
    )
    logger.info("Issued OAuth authorization code")
    return _redirect_with_params(redirect_uri, {"code": code, "state": state})


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


def _check_client_auth(request: Request, form, client_id: str, record: dict) -> bool:
    """Verifies client_secret when presented; public clients use PKCE only."""
    presented = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
            basic_id, _, basic_secret = decoded.partition(":")
            if basic_id != client_id:
                return False
            presented = basic_secret
        except Exception:
            return False
    elif form.get("client_secret"):
        presented = form.get("client_secret")
    if presented is None:
        return True  # public client; PKCE binds the flow
    return hmac.compare_digest(presented, record.get("client_secret", ""))


async def token_endpoint(request: Request) -> JSONResponse:
    form = await request.form()
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    store = get_store()
    record = store.get_client(client_id) if client_id else None
    if record is None:
        return _oauth_error("invalid_client", "Unknown client_id.", 401)
    if not _check_client_auth(request, form, client_id, record):
        return _oauth_error("invalid_client", "Client authentication failed.", 401)

    if grant_type == "authorization_code":
        return _grant_authorization_code(form, client_id)
    if grant_type == "refresh_token":
        return _grant_refresh_token(form, client_id)
    return _oauth_error("unsupported_grant_type", f"Grant type {grant_type!r} is not supported.")


def _grant_authorization_code(form, client_id: str) -> JSONResponse:
    store = get_store()
    code = form.get("code", "")
    redirect_uri = form.get("redirect_uri", "")
    verifier = form.get("code_verifier", "")
    if not code or not redirect_uri or not verifier:
        return _oauth_error("invalid_request", "code, redirect_uri and code_verifier are required.")

    code_record = store.consume_code(code)  # single-use
    if code_record is None:
        return _oauth_error("invalid_grant", "Authorization code is invalid or expired.")
    if code_record["client_id"] != client_id:
        return _oauth_error("invalid_grant", "Code was issued to a different client.")
    if code_record["redirect_uri"] != redirect_uri:
        return _oauth_error("invalid_grant", "redirect_uri does not match the authorize request.")
    if not _verify_pkce(
        verifier, code_record["code_challenge"], code_record["code_challenge_method"]
    ):
        return _oauth_error("invalid_grant", "PKCE verification failed.")

    pair = store.create_token_pair(client_id, code_record.get("scope", "mcp"))
    logger.info("Issued OAuth token pair")
    return JSONResponse(
        {
            "access_token": pair["access_token"],
            "token_type": "Bearer",
            "expires_in": pair["expires_in"],
            "refresh_token": pair["refresh_token"],
            "scope": code_record.get("scope", "mcp"),
        }
    )


def _grant_refresh_token(form, client_id: str) -> JSONResponse:
    refresh_token = form.get("refresh_token", "")
    if not refresh_token:
        return _oauth_error("invalid_request", "refresh_token is required.")
    pair = get_store().rotate_refresh_token(refresh_token, client_id)
    if pair is None:
        return _oauth_error("invalid_grant", "Refresh token is invalid or expired.")
    return JSONResponse(
        {
            "access_token": pair["access_token"],
            "token_type": "Bearer",
            "expires_in": pair["expires_in"],
            "refresh_token": pair["refresh_token"],
        }
    )


oauth_routes = [
    Route("/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
    Route("/oauth/register", register_client, methods=["POST"]),
    Route("/oauth/authorize", authorize_get, methods=["GET"]),
    Route("/oauth/authorize", authorize_post, methods=["POST"]),
    Route("/oauth/token", token_endpoint, methods=["POST"]),
]
