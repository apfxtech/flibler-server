import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from .config import Config

HEADER_CLIENT = "x-qu-client"
HEADER_TIME = "x-qu-time"
HEADER_SIGN = "x-qu-sign"

def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail)

def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)

def canonical_path(path: str) -> str:
    marker = "/api/v1/"
    index = path.find(marker)
    return path[index:] if index > 0 else path

def sign_payload(
    secret: str, timestamp: int, method: str, path: str, body: bytes, client_id: str
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = (
        f"{timestamp}\n{method.upper()}\n{canonical_path(path)}\n"
        f"{body_hash}\n{client_id}"
    )
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

async def authenticate(request: Request, cfg: Config) -> str:
    user_agent = request.headers.get("user-agent", "")
    if cfg.app_name.lower() not in user_agent.lower():
        raise _forbidden("Unrecognized client")

    client_id = request.headers.get(HEADER_CLIENT, "")
    time_raw = request.headers.get(HEADER_TIME, "")
    signature = request.headers.get(HEADER_SIGN, "")
    if not client_id or not time_raw or not signature:
        raise _unauthorized("Missing authentication headers")
    if len(client_id) > 128:
        raise _unauthorized("Invalid client id")

    try:
        timestamp = int(time_raw)
    except ValueError:
        raise _unauthorized("Invalid timestamp")
    if abs(time.time() - timestamp) > cfg.auth_window_sec:
        raise _forbidden("Timestamp outside allowed window")

    body = await request.body()
    expected = sign_payload(
        cfg.shared_secret,
        timestamp,
        request.method,
        request.url.path,
        body,
        client_id,
    )
    if not hmac.compare_digest(expected, signature.lower()):
        raise _forbidden("Invalid signature")
    return client_id
