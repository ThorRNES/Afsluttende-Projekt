from __future__ import annotations

import argparse
import json
import os
import smtplib
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _parse_bool(value: str, *, default: bool) -> bool:
    v = value.strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


def _opt_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = value.strip()
    return s or None


def _strip_optional_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()

        if "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = _strip_optional_quotes(val)
        if key:
            out[key] = val
    return out


def load_env_file(env_file: Optional[str]) -> None:
    
    requested = _opt_str(env_file)
    default_path = Path(".env")
    path = Path(requested) if requested else (default_path if default_path.exists() else None)
    if path is None:
        return
    if not path.exists():
        raise SystemExit(f".env file not found: {path}")

    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]

        load_dotenv(dotenv_path=str(path), override=False)
        return
    except ImportError:
        parsed = _parse_dotenv_file(path)
        for k, v in parsed.items():
            os.environ.setdefault(k, v)


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    user: Optional[str]
    password: Optional[str]
    starttls: bool
    mail_from: str
    timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    smtp: SmtpConfig
    default_to: str
    default_subject: str
    allow_to_override: bool
    max_body_bytes: int = 256_000


class EmailSender:
    def __init__(self, cfg: SmtpConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()

    def send_json(self, *, mail_to: str, subject: str, payload: Mapping[str, Any]) -> None:
        pretty = json.dumps(payload, indent=2, ensure_ascii=False, default=json_default)
        pretty_bytes = pretty.encode("utf-8")

        msg = EmailMessage()
        msg["From"] = self._cfg.mail_from
        msg["To"] = mail_to
        msg["Subject"] = subject
        msg.set_content(pretty)
        msg.add_attachment(
            pretty_bytes,
            maintype="application",
            subtype="json",
            filename="data.json",
        )

        with self._lock, smtplib.SMTP(self._cfg.host, self._cfg.port, timeout=self._cfg.timeout_s) as server:
            server.ehlo()
            if self._cfg.starttls:
                server.starttls()
                server.ehlo()
            if self._cfg.user and self._cfg.password:
                server.login(self._cfg.user, self._cfg.password)
            server.send_message(msg)


class RobertaEmailerHandler(BaseHTTPRequestHandler):
    server_version = "RobertaEmailer/1.1"

    def _json(self, status: int, body: Mapping[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self, *, max_bytes: int) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise ValueError("Missing Content-Length")

        try:
            length = int(length_header)
        except ValueError as e:
            raise ValueError("Invalid Content-Length") from e

        if length <= 0:
            raise ValueError("Empty body")
        if length > max_bytes:
            raise ValueError(f"Body too large (max {max_bytes} bytes)")

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid JSON: {e}") from e

        if not isinstance(payload, dict):
            raise ValueError("JSON must be an object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "service": "RobertaEmailer ACTIVE",
                    "time_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                },
            )
            return

        self._json(404, {"ok": False, "error": "Not found", "path": parsed.path})

    def do_POST(self) -> None:
        cfg: AppConfig = self.server.cfg  # type: ignore[attr-defined]
        sender: EmailSender = self.server.sender  # type: ignore[attr-defined]

        parsed = urlparse(self.path)
        if parsed.path != "/ingest":
            self._json(404, {"ok": False, "error": "Not found", "path": parsed.path})
            return

        try:
            payload = self._read_json_body(max_bytes=cfg.max_body_bytes)
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e)})
            return

        mail_to = cfg.default_to
        if cfg.allow_to_override:
            to_candidate = payload.get("to")
            if isinstance(to_candidate, str) and to_candidate.strip():
                mail_to = to_candidate.strip()

        subject = cfg.default_subject
        subject_candidate = payload.get("subject")
        if isinstance(subject_candidate, str) and subject_candidate.strip():
            subject = subject_candidate.strip()

        try:
            sender.send_json(mail_to=mail_to, subject=subject, payload=payload)
        except Exception as e:
            self._json(500, {"ok": False, "error": f"Email send failed: {e}"})
            return

        self._json(200, {"ok": True, "sent_to": mail_to, "subject": subject})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def build_config(args: argparse.Namespace) -> AppConfig:
    smtp_host = (args.smtp_host or os.getenv("SMTP_HOST") or "").strip()
    smtp_port = int(args.smtp_port or os.getenv("SMTP_PORT") or "587")

    smtp_user = _opt_str(args.smtp_user or os.getenv("SMTP_USER"))
    smtp_pass = _opt_str(args.smtp_pass or os.getenv("SMTP_PASS"))

    starttls_raw = (args.smtp_starttls or os.getenv("SMTP_STARTTLS") or "true")
    smtp_starttls = _parse_bool(starttls_raw, default=True)

    mail_from = (args.mail_from or os.getenv("SMTP_FROM") or os.getenv("MAIL_FROM") or "").strip()

    default_to = (args.default_to or os.getenv("DEFAULT_TO") or "").strip()
    default_subject = (args.subject or os.getenv("DEFAULT_SUBJECT") or "Robot B JSON Payload").strip() or "Robot B JSON Payload"

    allow_override = bool(args.allow_to_override)

    max_body = int(args.max_body_bytes or os.getenv("MAX_BODY_BYTES") or "256000")

    if not smtp_host:
        raise SystemExit("SMTP_HOST must be set (env, .env, or CLI).")
    if not mail_from:
        raise SystemExit("SMTP_FROM/MAIL_FROM must be set (env, .env, or CLI).")
    if not default_to and not allow_override:
        raise SystemExit("DEFAULT_TO must be set unless --allow-to-override is enabled.")
    if max_body <= 0:
        raise SystemExit("MAX_BODY_BYTES must be > 0.")

    smtp_cfg = SmtpConfig(
        host=smtp_host,
        port=smtp_port,
        user=smtp_user,
        password=smtp_pass,
        starttls=smtp_starttls,
        mail_from=mail_from,
        timeout_s=30.0,
    )
    return AppConfig(
        smtp=smtp_cfg,
        default_to=default_to,
        default_subject=default_subject,
        allow_to_override=allow_override,
        max_body_bytes=max_body,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Robot B: JSON receiver + email sender.")
    p.add_argument("--env-file", default="", help="Path to .env (defaults to ./.env if present)")

    p.add_argument("--host", default="127.0.0.1", help="Bind host")
    p.add_argument("--port", type=int, default=8080, help="Bind port")

    p.add_argument("--default-to", default="", help="Default recipient (or env/.env DEFAULT_TO)")
    p.add_argument("--subject", default="", help="Default subject (or env/.env DEFAULT_SUBJECT)")
    p.add_argument("--allow-to-override", action="store_true", help="Allow request JSON to set 'to'")

    p.add_argument("--max-body-bytes", default="", help="Max request size in bytes (or env/.env MAX_BODY_BYTES)")

    p.add_argument("--smtp-host", default="", help="SMTP host (or env/.env SMTP_HOST)")
    p.add_argument("--smtp-port", default="", help="SMTP port (or env/.env SMTP_PORT)")
    p.add_argument("--smtp-user", default="", help="SMTP user (or env/.env SMTP_USER)")
    p.add_argument("--smtp-pass", default="", help="SMTP password (or env/.env SMTP_PASS)")
    p.add_argument("--smtp-starttls", default="", help="STARTTLS bool (or env/.env SMTP_STARTTLS)")
    p.add_argument("--mail-from", default="", help="From address (or env/.env SMTP_FROM/MAIL_FROM)")

    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    cfg = build_config(args)

    sender = EmailSender(cfg.smtp)

    httpd = ThreadingHTTPServer((args.host, args.port), RobertaEmailerHandler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    httpd.sender = sender  # type: ignore[attr-defined]

    print(f"Robot B listening on http://{args.host}:{args.port}/ingest")
    httpd.serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
