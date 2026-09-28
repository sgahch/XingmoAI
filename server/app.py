"""StarDevil local demo server and OpenAI-compatible facade.

This service intentionally owns the public API contract. NewAPI and the
physical model remain upstream infrastructure and are never exposed to a browser.

Per-deployment model channels are isolated by logged-in user: each user owns its
own upstream configuration, while API keys are bound to their owning user so that
external callers transparently use that user's channel.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "xingmo.db"
MODEL_ALIAS = "xingmo-chat"
SESSION_DAYS = 7

# Optional HTTP Basic gate for public tunnels. Set XINGMO_TUNNEL_AUTH="user:pass"
# to require Basic Auth on every request before the app's own routing/login runs.
# Empty (default) keeps local behaviour unchanged.
TUNNEL_AUTH = os.environ.get("XINGMO_TUNNEL_AUTH", "").strip()


def utc_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_prefix TEXT NOT NULL,
                daily_limit INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                used_today INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                token_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                upstream_mode TEXT NOT NULL DEFAULT 'demo',
                upstream_protocol TEXT NOT NULL DEFAULT 'openai',
                upstream_base_url TEXT NOT NULL DEFAULT '',
                upstream_api_key TEXT NOT NULL DEFAULT '',
                upstream_model TEXT NOT NULL DEFAULT '',
                expose_real_models INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(api_keys)")}
        if "token_hash" not in columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN token_hash TEXT")
        if "owner_user_id" not in columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN owner_user_id TEXT")
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        if "sources" not in message_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")

        # Repair rows left half-configured by the old registration behaviour, which
        # copied the global Base URL / model into a new user row but deliberately
        # left the API key empty. Such a row looks "configured" in the console while
        # every upstream call fails. Saving through the API always requires a key,
        # so a custom-mode row without a key can only be an inheritance artifact.
        connection.execute(
            """UPDATE user_settings
                  SET upstream_mode = 'demo', upstream_base_url = '', upstream_model = ''
                WHERE upstream_mode != 'demo'
                  AND upstream_api_key = ''
                  AND upstream_base_url = (SELECT value FROM settings WHERE name = 'upstream_base_url')
                  AND upstream_model = (SELECT value FROM settings WHERE name = 'upstream_model')"""
        )

        demo_token = "xm-live-demo-local"
        if not connection.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone():
            connection.execute(
                "INSERT INTO api_keys (id, name, token_prefix, daily_limit, enabled, used_today, created_at, token_hash, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("key-demo", "演示合作方 Key", "xm-live-demo...", 10000, 1, 2840, utc_now(), hashlib.sha256(demo_token.encode()).hexdigest(), None),
            )
        else:
            connection.execute(
                "UPDATE api_keys SET token_hash = ? WHERE id = ? AND (token_hash IS NULL OR token_hash = '')",
                (hashlib.sha256(demo_token.encode()).hexdigest(), "key-demo"),
            )
        connection.execute(
            "INSERT OR IGNORE INTO settings VALUES (?, ?)", ("upstream_mode", "demo")
        )

        if not connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            admin_id = "user-admin"
            connection.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (admin_id, "admin", hash_password("admin123"), utc_now()),
            )
            gs = read_settings()
            connection.execute(
                "INSERT INTO user_settings (user_id, upstream_mode, upstream_protocol, upstream_base_url, upstream_api_key, upstream_model, expose_real_models) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (admin_id, gs.get("upstream_mode", "demo"), gs.get("upstream_protocol", "openai"), gs.get("upstream_base_url", ""), "", gs.get("upstream_model", "")),
            )


def split_knowledge_text(content: str, size: int = 520, overlap: int = 80) -> list[str]:
    cleaned = re.sub(r"\r\n?", "\n", content).strip()
    if not cleaned:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + size)
        if end < len(cleaned):
            boundary = max(cleaned.rfind("\n", start + size // 2, end), cleaned.rfind("。", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


def knowledge_terms(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    chinese = [normalized[index:index + 2] for index in range(max(len(normalized) - 1, 0))]
    latin = re.findall(r"[a-z0-9_-]{2,}", text.lower())
    return list(dict.fromkeys([term for term in chinese + latin if len(term) >= 2]))[:28]


def retrieve_knowledge(query: str, limit: int = 3) -> list[dict[str, str]]:
    terms = knowledge_terms(query)
    if not terms:
        return []
    with db() as connection:
        rows = connection.execute(
            """
            SELECT knowledge_documents.id AS document_id, knowledge_documents.name, knowledge_chunks.content
            FROM knowledge_chunks
            JOIN knowledge_documents ON knowledge_documents.id = knowledge_chunks.document_id
            """
        ).fetchall()
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        lowered = row["content"].lower()
        score = sum(lowered.count(term) for term in terms)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"document_id": row["document_id"], "name": row["name"], "content": row["content"]}
        for _, row in scored[:limit]
    ]


def knowledge_prompt(sources: list[dict[str, str]]) -> str:
    if not sources:
        return ""
    reference = "\n\n".join(f"【{item['name']}】\n{item['content']}" for item in sources)
    return (
        "请优先依据以下企业知识库资料回答。资料不足时请明确说明，不要编造。"
        "不要透露内部系统、原始模型或网关信息。\n\n" + reference
    )


def simulated_answer(question: str, sources: list[dict[str, str]] | None = None) -> str:
    subject = question.strip()[:80] or "您的问题"
    if sources:
        primary = sources[0]
        excerpt = re.sub(r"\s+", " ", primary["content"]).strip()[:320]
        return (
            f"根据知识库资料《{primary['name']}》，关于“{subject}”可以这样理解：\n\n{excerpt}\n\n"
            "以上内容来自已导入的企业资料。星魔会优先检索相关资料，再由模型结合上下文生成回答。"
        )
    return (
        f"关于“{subject}”，星魔可以基于企业内部部署的智能模型提供稳定、可控的问答服务。\n\n"
        "在当前演示环境中，回答会以流式方式实时生成；正式部署后，星魔会通过受控网关连接客户本地模型服务，"
        "对外统一提供标准 OpenAI 兼容接口，并隐藏底层模型与基础设施信息。\n\n"
        "您还可以在控制台创建访问密钥、配置每日用量额度，并查看调用统计与会话记录。"
    )


def read_settings() -> dict[str, str]:
    with db() as connection:
        return {row["name"]: row["value"] for row in connection.execute("SELECT name, value FROM settings")}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def session_for(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = (now + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M")
    with db() as connection:
        connection.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.strftime("%Y-%m-%d %H:%M"), expires),
        )
    return token


def upstream_config(user_id: str | None = None) -> dict[str, object]:
    """Resolve a normalized upstream channel config.

    A logged-in user's own ``user_settings`` takes priority; otherwise the global
    ``settings`` table is used as the shared default.
    """
    if user_id:
        with db() as connection:
            row = connection.execute(
                "SELECT upstream_mode, upstream_protocol, upstream_base_url, upstream_api_key, upstream_model, expose_real_models FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row and (row["upstream_base_url"] or row["upstream_mode"] != "demo"):
                return {
                    "mode": row["upstream_mode"],
                    "protocol": row["upstream_protocol"],
                    "base_url": row["upstream_base_url"],
                    "api_key": row["upstream_api_key"],
                    "model": row["upstream_model"],
                    "expose_real_models": bool(row["expose_real_models"]),
                }
    config = read_settings()
    return {
        "mode": config.get("upstream_mode", "demo"),
        "protocol": config.get("upstream_protocol", "openai"),
        "base_url": config.get("upstream_base_url") or config.get("newapi_base_url", ""),
        "api_key": config.get("upstream_api_key") or config.get("newapi_token", ""),
        "model": config.get("upstream_model", ""),
        "expose_real_models": False,
    }


def normalize_base_url(value: object) -> str:
    """Accept the common pastes of an endpoint URL and reduce it to a Base URL.

    Callers frequently paste ``.../v1/chat/completions`` (or ``/models``) where a
    Base URL is expected; keeping the tail would produce a doubled path.
    """
    base = str(value or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/models", "/messages"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base


def upstream_endpoint(base_url: str, protocol: str) -> str:
    base_url = normalize_base_url(base_url)
    if protocol == "anthropic":
        if base_url.endswith("/messages"):
            return base_url
        return f"{base_url}/messages" if base_url.endswith("/v1") else f"{base_url}/v1/messages"
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"


def proxy_upstream_models(config: dict[str, object]) -> dict:
    base = normalize_base_url(config["base_url"])
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    if config["protocol"] == "anthropic":
        headers = {"x-api-key": str(config["api_key"]), "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {config['api_key']}"}
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content or "")


def upstream_request(messages: list[dict], context: str = "", stream: bool = True, config: dict | None = None):
    """Create a request for a configured OpenAI-compatible or Anthropic-compatible model channel."""
    if config is None:
        config = upstream_config(None)
    protocol = config["protocol"]
    if protocol not in {"openai", "anthropic"}:
        raise ValueError("Unsupported upstream protocol")
    if not all((config["base_url"], config["api_key"], config["model"])):
        raise ValueError("Base URL, API Key, and model name are required")

    if protocol == "anthropic":
        system_parts = [content_text(item.get("content")) for item in messages if item.get("role") == "system"]
        if context:
            system_parts.append(context)
        outbound_messages = [
            {"role": item.get("role"), "content": content_text(item.get("content"))}
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        outbound = {"model": config["model"], "max_tokens": 2048, "stream": stream, "messages": outbound_messages}
        if system_parts:
            outbound["system"] = "\n\n".join(part for part in system_parts if part)
        headers = {
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    else:
        outbound_messages = list(messages)
        if context:
            outbound_messages.insert(0, {"role": "system", "content": context})
        outbound = {"model": config["model"], "stream": stream, "messages": outbound_messages}
        headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

    request = Request(
        upstream_endpoint(config["base_url"], protocol),
        data=json.dumps(outbound, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    return urlopen(request, timeout=180), protocol


def upstream_delta(raw_line: bytes, protocol: str) -> str:
    decoded = raw_line.decode("utf-8", errors="replace").strip()
    if not decoded.startswith("data: "):
        return ""
    data = decoded[6:]
    if data == "[DONE]":
        return ""
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return ""
    if protocol == "anthropic":
        if event.get("type") == "content_block_delta":
            return str((event.get("delta") or {}).get("text") or "")
        return ""
    choices = event.get("choices") or []
    return str((choices[0].get("delta") or {}).get("content") or "") if choices else ""


def upstream_deltas(messages: list[dict], context: str = "", config: dict | None = None):
    response, protocol = upstream_request(messages, context=context, stream=True, config=config)
    with response:
        for raw_line in response:
            delta = upstream_delta(raw_line, protocol)
            if delta:
                yield delta


class StarDevilHandler(SimpleHTTPRequestHandler):
    server_version = "StarDevil/0.2"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{utc_now()}] {self.address_string()} - {fmt % args}")

    def json_response(self, body: object, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---- tunnel gate ----
    def tunnel_gate(self) -> bool:
        """Public-tunnel Basic Auth. Runs before any routing when TUNNEL_AUTH is set."""
        if not TUNNEL_AUTH:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                decoded = ""
            if secrets.compare_digest(decoded, TUNNEL_AUTH):
                return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="StarDevil"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        return False

    # ---- auth helpers ----
    def user_from_request(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"session=([^;]+)", cookie)
        if not match:
            return None
        token = unquote(match.group(1))
        with db() as connection:
            row = connection.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < utc_now():
                connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
            return row["user_id"]

    def set_session_cookie(self, token: str) -> None:
        self.send_header(
            "Set-Cookie",
            f"session={token}; HttpOnly; Path=/; Max-Age={SESSION_DAYS * 24 * 3600}; SameSite=Lax",
        )

    def require_auth(self) -> bool:
        if self.user_from_request():
            return True
        self.json_response({"error": {"message": "需要登录", "type": "auth_required"}}, HTTPStatus.UNAUTHORIZED)
        return False

    def key_owner_user_id(self) -> str | None:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        digest = hashlib.sha256(authorization[7:].strip().encode("utf-8")).hexdigest()
        with db() as connection:
            row = connection.execute(
                "SELECT owner_user_id FROM api_keys WHERE token_hash = ? AND enabled = 1", (digest,)
            ).fetchone()
        return row["owner_user_id"] if row else None

    # ---- auth endpoints ----
    def auth_me(self) -> None:
        uid = self.user_from_request()
        if not uid:
            self.json_response({"user": None}, HTTPStatus.UNAUTHORIZED)
            return
        with db() as connection:
            row = connection.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
        self.json_response({"user": dict(row) if row else None})

    def auth_login(self, payload: dict) -> None:
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        with db() as connection:
            row = connection.execute(
                "SELECT id, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            self.json_response({"error": "用户名或密码错误"}, HTTPStatus.UNAUTHORIZED)
            return
        token = session_for(row["id"])
        self.send_response(HTTPStatus.OK)
        self.set_session_cookie(token)
        body = json.dumps({"ok": True, "user": {"id": row["id"], "username": username}}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def auth_register(self, payload: dict) -> None:
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if len(username) < 2 or len(password) < 6:
            self.json_response({"error": "用户名至少 2 位、密码至少 6 位"}, HTTPStatus.BAD_REQUEST)
            return
        with db() as connection:
            if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                self.json_response({"error": "用户名已存在"}, HTTPStatus.CONFLICT)
                return
            uid = f"user-{secrets.token_hex(4)}"
            connection.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (uid, username, hash_password(password), utc_now()),
            )
            gs = read_settings()
            # Start from an empty own row so the account transparently inherits the
            # global default channel (including its key). Copying the global Base URL
            # and model while dropping the key would produce a half-configured row
            # that reports "custom channel" but can never reach the upstream.
            connection.execute(
                "INSERT OR IGNORE INTO user_settings (user_id, upstream_mode, upstream_protocol, upstream_base_url, upstream_api_key, upstream_model, expose_real_models) VALUES (?, 'demo', ?, '', '', '', 0)",
                (uid, gs.get("upstream_protocol", "openai")),
            )
        token = session_for(uid)
        self.send_response(HTTPStatus.CREATED)
        self.set_session_cookie(token)
        body = json.dumps({"ok": True, "user": {"id": uid, "username": username}}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def auth_logout(self) -> None:
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"session=([^;]+)", cookie)
        if match:
            token = unquote(match.group(1))
            with db() as connection:
                connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie", "session=; HttpOnly; Path=/; Max-Age=0")
        body = b"{}"
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- model listing ----
    def models_fetch(self, payload: dict | None = None) -> None:
        """Probe the channel described by the console form.

        The form values are authoritative (a blank API Key falls back to the saved
        one) so the button can be used before saving — testing the stored config
        instead would report "not configured" for a form the user just filled in.
        """
        uid = self.user_from_request()
        saved = upstream_config(uid)
        payload = payload if isinstance(payload, dict) else {}

        def pick(name: str, fallback: object) -> str:
            return str(payload.get(name) or "").strip() or str(fallback or "")

        mode = pick("upstream_mode", saved["mode"])
        config = {
            "mode": mode,
            "protocol": pick("upstream_protocol", saved["protocol"]),
            "base_url": normalize_base_url(pick("upstream_base_url", saved["base_url"])),
            "api_key": pick("upstream_api_key", saved["api_key"]),
            "model": pick("upstream_model", saved["model"]),
        }
        if mode == "demo":
            self.json_response(
                {
                    "ok": False,
                    "reason": "demo_mode",
                    "message": "当前运行模式是「演示模式（本地模拟回复）」，不会连接上游。请把运行模式切换为「自定义模型通道」后重试。",
                }
            )
            return
        missing = [
            label
            for label, value in (("Base URL", config["base_url"]), ("API Key", config["api_key"]), ("模型名称", config["model"]))
            if not value
        ]
        if missing:
            self.json_response(
                {
                    "ok": False,
                    "reason": "incomplete",
                    "message": f"通道配置不完整，缺少：{'、'.join(missing)}。请在下方表单补齐后再测试（可直接测试，不必先保存）。",
                }
            )
            return
        try:
            data = proxy_upstream_models(config)
        except HTTPError as error:
            detail = ""
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = str(body.get("error", {}).get("message") or body.get("message") or "")
            except Exception:
                detail = ""
            hint = "（API Key 无效、过期或无权访问）" if error.code in (401, 403) else ""
            self.json_response(
                {
                    "ok": False,
                    "reason": "upstream",
                    "message": f"上游返回 HTTP {error.code}{hint}：{detail or error.reason}",
                },
                HTTPStatus.BAD_GATEWAY,
            )
            return
        except (ValueError, URLError, TimeoutError) as error:
            self.json_response(
                {"ok": False, "reason": "upstream", "message": f"无法连接上游，请检查 Base URL 与网络：{error}"},
                HTTPStatus.BAD_GATEWAY,
            )
            return
        models = [item.get("id") for item in data.get("data", []) if isinstance(item, dict)] if isinstance(data, dict) else []
        self.json_response({"ok": True, "models": models})

    def openai_models(self) -> None:
        owner = self.key_owner_user_id()
        config = upstream_config(owner)
        if config.get("expose_real_models") and config["mode"] != "demo" and config["base_url"] and config["api_key"]:
            try:
                data = proxy_upstream_models(config)
                self.json_response(data)
                return
            except (ValueError, HTTPError, URLError, TimeoutError) as error:
                self.json_response({"error": {"message": f"上游模型列表获取失败：{error}"}}, HTTPStatus.BAD_GATEWAY)
                return
        self.json_response({"object": "list", "data": [{"id": MODEL_ALIAS, "object": "model", "owned_by": "xingmo"}]})

    # ---- internal API ----
    def dashboard(self) -> None:
        uid = self.user_from_request()
        with db() as connection:
            conversations = connection.execute("SELECT COUNT(*) AS total FROM conversations").fetchone()["total"]
            messages = connection.execute("SELECT COUNT(*) AS total FROM messages").fetchone()["total"]
            key_data = connection.execute("SELECT COUNT(*) AS total, COALESCE(SUM(used_today), 0) AS used FROM api_keys").fetchone()
        self.json_response(
            {
                "mode": "per-user" if uid else "demo",
                "model": MODEL_ALIAS,
                "metrics": [
                    {"label": "今日请求", "value": 1284 + messages, "delta": "+18.6%", "tone": "gold"},
                    {"label": "今日 Token", "value": "86.4K", "delta": "+12.1%", "tone": "blue"},
                    {"label": "可用密钥", "value": key_data["total"], "delta": "全部正常", "tone": "green"},
                    {"label": "演示会话", "value": conversations, "delta": "本地已保存", "tone": "ink"},
                ],
                "activity": [
                    {"time": "刚刚", "text": "星魔网关完成一次流式对话响应", "status": "success"},
                    {"time": "10:24", "text": "演示合作方 Key 调用 xingmo-chat", "status": "success"},
                    {"time": "09:58", "text": "本地模型通道健康检查通过", "status": "success"},
                    {"time": "昨天", "text": "控制台品牌配置已更新", "status": "info"},
                ],
            }
        )

    def conversations(self) -> None:
        with db() as connection:
            rows = connection.execute(
                "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        self.json_response([dict(row) for row in rows])

    def messages(self, conversation_id: str) -> None:
        with db() as connection:
            rows = connection.execute(
                "SELECT role, content, sources, created_at FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            ).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            try:
                item["sources"] = json.loads(item["sources"] or "[]")
            except json.JSONDecodeError:
                item["sources"] = []
            messages.append(item)
        self.json_response(messages)

    def keys(self) -> None:
        with db() as connection:
            rows = connection.execute(
                "SELECT id, name, token_prefix, daily_limit, enabled, used_today, created_at, owner_user_id FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        self.json_response([dict(row) for row in rows])

    def knowledge_documents(self) -> None:
        with db() as connection:
            rows = connection.execute(
                "SELECT id, name, source_type, chunk_count, created_at FROM knowledge_documents ORDER BY created_at DESC"
            ).fetchall()
        self.json_response([dict(row) for row in rows])

    def create_knowledge_document(self, payload: dict) -> None:
        name = str(payload.get("name", "未命名资料")).strip()[:100] or "未命名资料"
        content = str(payload.get("content", "")).strip()
        source_type = str(payload.get("source_type", "text")).strip()[:20] or "text"
        if len(content) < 8:
            return self.json_response({"error": "资料内容至少需要 8 个字符"}, HTTPStatus.BAD_REQUEST)
        chunks = split_knowledge_text(content)
        document_id = f"kb-{secrets.token_hex(6)}"
        item = {"id": document_id, "name": name, "source_type": source_type, "chunk_count": len(chunks), "created_at": utc_now()}
        with db() as connection:
            connection.execute(
                "INSERT INTO knowledge_documents VALUES (:id, :name, :source_type, :chunk_count, :created_at)", item
            )
            connection.executemany(
                "INSERT INTO knowledge_chunks (document_id, chunk_index, content) VALUES (?, ?, ?)",
                [(document_id, index, chunk) for index, chunk in enumerate(chunks)],
            )
        self.json_response(item, HTTPStatus.CREATED)

    def knowledge_search(self, payload: dict) -> None:
        query = str(payload.get("query", "")).strip()
        if not query:
            return self.json_response({"error": "请输入检索内容"}, HTTPStatus.BAD_REQUEST)
        sources = retrieve_knowledge(query)
        self.json_response([{"name": item["name"], "excerpt": item["content"][:260]} for item in sources])

    def delete_knowledge_document(self, document_id: str) -> None:
        with db() as connection:
            connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            deleted = connection.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,)).rowcount
        if not deleted:
            return self.json_response({"error": "资料不存在"}, HTTPStatus.NOT_FOUND)
        self.json_response({"ok": True})

    def authenticate_api_key(self) -> bool:
        """Authenticate a public API request without revealing stored raw tokens."""
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            self.json_response(
                {"error": {"message": "Missing Bearer API key", "type": "authentication_error"}}, HTTPStatus.UNAUTHORIZED
            )
            return False
        digest = hashlib.sha256(authorization[7:].strip().encode("utf-8")).hexdigest()
        with db() as connection:
            key = connection.execute(
                "SELECT id, daily_limit, used_today FROM api_keys WHERE token_hash = ? AND enabled = 1", (digest,)
            ).fetchone()
            if not key:
                self.json_response(
                    {"error": {"message": "Invalid or disabled API key", "type": "authentication_error"}}, HTTPStatus.UNAUTHORIZED
                )
                return False
            if key["used_today"] >= key["daily_limit"]:
                self.json_response(
                    {"error": {"message": "Daily API quota exceeded", "type": "insufficient_quota"}}, HTTPStatus.TOO_MANY_REQUESTS
                )
                return False
            connection.execute("UPDATE api_keys SET used_today = used_today + 1 WHERE id = ?", (key["id"],))
        return True

    def settings(self) -> None:
        config = upstream_config(self.user_from_request())
        self.json_response(
            {
                "public_model": MODEL_ALIAS,
                "gateway_url": "/v1",
                "upstream_mode": config["mode"],
                "upstream_protocol": config["protocol"],
                "upstream_base_url": config["base_url"],
                "upstream_model": config["model"],
                "has_api_key": bool(config["api_key"]),
                "expose_real_models": bool(config.get("expose_real_models")),
                "channel_source": config.get("source", "global"),
            }
        )

    def create_key(self, payload: dict) -> None:
        uid = self.user_from_request()
        key_id = f"key-{secrets.token_hex(4)}"
        raw_token = f"xm-live-{secrets.token_urlsafe(18)}"
        item = {
            "id": key_id,
            "name": payload.get("name", "新建演示 Key").strip()[:40] or "新建演示 Key",
            "token_prefix": f"{raw_token[:14]}...",
            "daily_limit": int(payload.get("daily_limit", 10000)),
            "enabled": 1,
            "used_today": 0,
            "created_at": utc_now(),
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "owner": uid,
        }
        with db() as connection:
            connection.execute(
                "INSERT INTO api_keys (id, name, token_prefix, daily_limit, enabled, used_today, created_at, token_hash, owner_user_id) VALUES (:id, :name, :token_prefix, :daily_limit, :enabled, :used_today, :created_at, :token_hash, :owner)", item
            )
        self.json_response({"item": item, "token": raw_token}, HTTPStatus.CREATED)

    def save_settings(self, payload: dict) -> None:
        uid = self.user_from_request()
        if not uid:
            return self.json_response({"error": "未登录"}, HTTPStatus.UNAUTHORIZED)
        existing = upstream_config(uid)
        mode = str(payload.get("upstream_mode", existing["mode"])).strip()
        protocol = str(payload.get("upstream_protocol", existing["protocol"])).strip()
        base_url = str(payload.get("upstream_base_url", existing["base_url"])).strip()
        api_key = str(payload.get("upstream_api_key", "")).strip() or existing["api_key"]
        model = str(payload.get("upstream_model", existing["model"])).strip()
        expose = 1 if payload.get("expose_real_models") else 0
        if mode not in {"demo", "custom", "newapi"}:
            return self.json_response({"error": "Unsupported upstream mode"}, HTTPStatus.BAD_REQUEST)
        if protocol not in {"openai", "anthropic"}:
            return self.json_response({"error": "Unsupported protocol"}, HTTPStatus.BAD_REQUEST)
        if mode in {"custom", "newapi"} and not all((base_url, model, api_key)):
            return self.json_response({"error": "自定义通道需要填写 Base URL、API Key 和模型名称"}, HTTPStatus.BAD_REQUEST)
        with db() as connection:
            connection.execute(
                """INSERT INTO user_settings (user_id, upstream_mode, upstream_protocol, upstream_base_url, upstream_api_key, upstream_model, expose_real_models)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     upstream_mode=excluded.upstream_mode,
                     upstream_protocol=excluded.upstream_protocol,
                     upstream_base_url=excluded.upstream_base_url,
                     upstream_api_key=excluded.upstream_api_key,
                     upstream_model=excluded.upstream_model,
                     expose_real_models=excluded.expose_real_models""",
                (uid, mode, protocol, base_url, api_key, model, expose),
            )
        self.json_response({"ok": True, "upstream_mode": mode, "expose_real_models": bool(expose)})

    def stream_text(self, answer: str, model: str = MODEL_ALIAS, openai: bool = False) -> None:
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for index in range(0, len(answer), 8):
            delta = answer[index:index + 8]
            if openai:
                body = {"id": f"chatcmpl_{secrets.token_hex(8)}", "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                event = f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
            else:
                event = f"event: message\ndata: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            self.wfile.write(event.encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.028)
        if openai:
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(b"event: done\ndata: {}\n\n")
        self.wfile.flush()

    def chat(self, payload: dict) -> None:
        uid = self.user_from_request()
        config = upstream_config(uid)
        question = str(payload.get("message", "")).strip()
        if not question:
            return self.json_response({"error": "Message is required"}, HTTPStatus.BAD_REQUEST)
        conversation_id = str(payload.get("conversation_id") or f"conv-{secrets.token_hex(6)}")
        use_upstream = config["mode"] in {"custom", "newapi"}
        sources = retrieve_knowledge(question)
        context = knowledge_prompt(sources)
        answer = ""
        now = utc_now()
        with db() as connection:
            exists = connection.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not exists:
                connection.execute(
                    "INSERT INTO conversations VALUES (?, ?, ?, ?)", (conversation_id, question[:26], now, now)
                )
            else:
                connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            connection.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)", (conversation_id, "user", question, now))
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Conversation-Id", conversation_id)
        self.end_headers()
        if use_upstream:
            try:
                for delta in upstream_deltas([{"role": "user", "content": question}], context, config=config):
                    answer += delta
                    payload_text = json.dumps({"delta": delta, "conversation_id": conversation_id}, ensure_ascii=False)
                    self.wfile.write(f"event: message\ndata: {payload_text}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (ValueError, HTTPError, URLError, TimeoutError) as error:
                answer = "星魔模型服务暂时不可用，请稍后再试。"
                payload_text = json.dumps({"delta": answer, "conversation_id": conversation_id}, ensure_ascii=False)
                self.wfile.write(f"event: message\ndata: {payload_text}\n\n".encode("utf-8"))
                self.wfile.flush()
                print(f"Configured model channel error: {error}")
        else:
            answer = simulated_answer(question, sources)
            for index in range(0, len(answer), 8):
                payload_text = json.dumps({"delta": answer[index:index + 8], "conversation_id": conversation_id}, ensure_ascii=False)
                self.wfile.write(f"event: message\ndata: {payload_text}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.028)
        with db() as connection:
            connection.execute(
                "INSERT INTO messages (conversation_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, "assistant", answer, json.dumps([{"name": item["name"]} for item in sources], ensure_ascii=False), utc_now()),
            )
        if sources:
            source_payload = json.dumps({"sources": [{"name": item["name"]} for item in sources]}, ensure_ascii=False)
            self.wfile.write(f"event: sources\ndata: {source_payload}\n\n".encode("utf-8"))
        self.wfile.write(b"event: done\ndata: {}\n\n")
        self.wfile.flush()

    def openai_completion(self, payload: dict) -> None:
        owner = self.key_owner_user_id()
        config = upstream_config(owner)
        messages = payload.get("messages", [])
        prompt = next((item.get("content", "") for item in reversed(messages) if item.get("role") == "user"), "")
        sources = retrieve_knowledge(str(prompt))
        context = knowledge_prompt(sources)
        use_upstream = config["mode"] in {"custom", "newapi"}
        if use_upstream:
            try:
                deltas = upstream_deltas(messages, context, config=config)
                if payload.get("stream"):
                    self.close_connection = True
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for delta in deltas:
                        body = {"id": f"chatcmpl_{secrets.token_hex(8)}", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL_ALIAS, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                answer = "".join(deltas)
                return self.json_response(
                    {
                        "id": f"chatcmpl_{secrets.token_hex(8)}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": MODEL_ALIAS,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                    }
                )
            except (ValueError, HTTPError, URLError, TimeoutError) as error:
                print(f"Configured model channel error: {error}")
                return self.json_response(
                    {"error": {"message": "星魔模型服务暂时不可用", "type": "upstream_error"}}, HTTPStatus.BAD_GATEWAY
                )
        answer = simulated_answer(str(prompt), sources)
        if payload.get("stream"):
            return self.stream_text(answer, openai=True)
        self.json_response(
            {
                "id": f"chatcmpl_{secrets.token_hex(8)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_ALIAS,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 82, "total_tokens": 102},
            }
        )

    # ---- routing ----
    def do_GET(self) -> None:
        if not self.tunnel_gate():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/auth/"):
            if path == "/api/auth/me":
                return self.auth_me()
            if path == "/api/auth/logout":
                return self.auth_logout()
            return self.json_response({"error": {"message": "Resource not found"}}, HTTPStatus.NOT_FOUND)
        if path == "/v1/models":
            if not self.authenticate_api_key():
                return
            return self.openai_models()
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            if path == "/api/dashboard":
                return self.dashboard()
            if path == "/api/conversations":
                return self.conversations()
            if path.startswith("/api/conversations/"):
                return self.messages(path.rsplit("/", 1)[-1])
            if path == "/api/keys":
                return self.keys()
            if path == "/api/settings":
                return self.settings()
            if path == "/api/knowledge/documents":
                return self.knowledge_documents()
            if path == "/api/models/fetch":
                return self.models_fetch()
            return self.json_response({"error": {"message": "Resource not found"}}, HTTPStatus.NOT_FOUND)
        requested = WEB_ROOT / path.lstrip("/")
        if path in {"/", "/chat", "/admin", "/developer", "/login", "/settings"} or not requested.exists():
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        if not self.tunnel_gate():
            return
        path = urlparse(self.path).path
        payload = self.read_body()
        if path == "/api/auth/register":
            return self.auth_register(payload)
        if path == "/api/auth/login":
            return self.auth_login(payload)
        if path == "/api/auth/logout":
            return self.auth_logout()
        if path == "/v1/chat/completions":
            if not self.authenticate_api_key():
                return
            return self.openai_completion(payload)
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            if path == "/api/chat":
                return self.chat(payload)
            if path == "/api/keys":
                return self.create_key(payload)
            if path == "/api/settings":
                return self.save_settings(payload)
            if path == "/api/knowledge/documents":
                return self.create_knowledge_document(payload)
            if path == "/api/knowledge/search":
                return self.knowledge_search(payload)
            if path == "/api/models/fetch":
                return self.models_fetch(payload)
            return self.json_response({"error": {"message": "Resource not found"}}, HTTPStatus.NOT_FOUND)
        return self.json_response({"error": {"message": "Resource not found"}}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        if not self.tunnel_gate():
            return
        path = urlparse(self.path).path
        if not self.require_auth():
            return
        if path.startswith("/api/knowledge/documents/"):
            return self.delete_knowledge_document(path.rsplit("/", 1)[-1])
        return self.json_response({"error": {"message": "Resource not found"}}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the StarDevil local demo server")
    parser.add_argument("--port", type=int, default=5178)
    args = parser.parse_args()
    initialize()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), StarDevilHandler)
    print(f"StarDevil is running at http://127.0.0.1:{args.port}")
    print("Mode: per-user model channels. Login at /login (default admin / admin123).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def urllib_parse_unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def urllib_parse_unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


if __name__ == "__main__":
    main()
