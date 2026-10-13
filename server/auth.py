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
    """The app's HMAC signature: private build endpoints only."""
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


def client_hash(client_id: str, secret: str) -> str:
    """What the download journal keeps instead of the client id."""
    return hashlib.sha256(f"{secret}|{client_id}".encode()).hexdigest()[:32]


async def require_ci_token(request: Request, cfg: Config) -> None:
    """Bearer token of the catalog CI: bundle and build submission, deletion."""
    if not cfg.ci_token:
        raise _forbidden("CI submissions are disabled")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), cfg.ci_token):
        raise _unauthorized("Invalid token")


def client_ip(request: Request, cfg: Config) -> str:
    if cfg.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def is_banned(db, ip: str) -> bool:
    row = db.one("SELECT until FROM bans WHERE ip = ?", (ip,))
    return row is not None and row["until"] > time.time()


def ban(db, ip: str, days: int, reason: str) -> None:
    if not ip:
        return
    db.execute(
        "INSERT INTO bans (ip, until, reason) VALUES (?, ?, ?) "
        "ON CONFLICT(ip) DO UPDATE SET until = excluded.until, reason = excluded.reason",
        (ip, time.time() + days * 86400, reason),
    )


async def require_token_or_ban(request: Request, cfg: Config, db, token: str, label: str) -> None:
    """Bearer check that bans the address for cfg.ban_days on a wrong or
    missing token, and refuses a banned address whatever it sends."""
    ip = client_ip(request, cfg)
    if is_banned(db, ip):
        raise _forbidden("Banned")
    header = request.headers.get("authorization", "")
    scheme, _, given = header.partition(" ")
    if not token or scheme.lower() != "bearer" or not hmac.compare_digest(given.strip(), token):
        ban(db, ip, cfg.ban_days, f"unauthorized {label}")
        raise _forbidden("Banned")
