from __future__ import annotations

import argparse
import json
import os
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, UTC
from decimal import Decimal
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

SMTP_HOST: str = "send.one.com"
SMTP_PORT: int = 587
SMTP_USER: Optional[str] = "thor@nesbit.dk"  # Kan laves med "None", hvis nødvendigt
SMTP_PASS: Optional[str] = "tHor1999"  # Kode til SMTP-afsender
SMTP_STARTTLS: bool = True

MAIL_FROM: str = "thor@nesbit.dk"
DEFAULT_TO: str = "thn003@edu.zealand.dk"
DEFAULT_SUBJECT: str = "RobertaSender JSON Payload"

# Hvis True, JSON 'to' feltet kan overskrive DEFAULT_TO
ALLOW_TO_OVERRIDE_DEFAULT: bool = False

def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return float(value)
    return str(value)

def send_email_json(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: Optional[str],
    smtp_pass: Optional[str],
    smtp_starttls: bool,
    mail_from: str,
    mail_to: str,
    subject: str,
    payload: Dict[str, Any],
) -> None:
    pretty = json.dumps(payload, indent=2, ensure_ascii=False, default=json_default)

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Subject"] = subject
    msg.set_content(pretty)
    msg.add_attachment(
        pretty.encode("utf-8"), 
        maintype="application", 
        subtype="json", 
        filename="data.json"
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        if smtp_starttls:
            server.starttls()
            server.ehlo()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)

@dataclass(frozen=True, slots=True)
class RobotBConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: Optional[str]
    smtp_pass: Optional[str]
    smtp_starttls: bool
    mail_from: str
    default_to: str
    allow_to_override: bool
    default_subject: str

class RobertaEmailerHandler(BaseHTTPRequestHandler):
    server_version = "RobertaEmailer/1.0"

    def _json(self, status: int, body: Dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "service": "RobertaEmailer ACTIVE",
                        "time_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    },
                )
                return

            self._json(404, {"ok": False, "error": "Not found", "path": parsed.path})

    def do_POST(self) -> None:
        cfg: RobotBConfig = self.server.cfg  # type: ignore[attr-defined]

        parsed = urlparse(self.path)
        if parsed.path != "/ingest":
            self._json(404, {"ok": False, "error": "Not found", "path": parsed.path})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._json(400, {"ok": False, "error": "Empty body"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON must be an object")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        mail_to = cfg.default_to
        if cfg.allow_to_override and isinstance(payload.get("to"), str) and payload["to"].strip():
            mail_to = payload["to"].strip()

        subject = cfg.default_subject
        if isinstance(payload.get("subject"), str) and payload["subject"].strip():
            subject = payload["subject"].strip()

        try:
            send_email_json(
                smtp_host=cfg.smtp_host,
                smtp_port=cfg.smtp_port,
                smtp_user=cfg.smtp_user,
                smtp_pass=cfg.smtp_pass,
                smtp_starttls=cfg.smtp_starttls,
                mail_from=cfg.mail_from,
                mail_to=mail_to,
                subject=subject,
                payload=payload,
            )
        except Exception as e:
            self._json(500, {"ok": False, "error": f"Email send failed: {e}"})
            return

        self._json(200, {"ok": True, "sent_to": mail_to, "subject": subject})

    def log_message(self, fmt: str, *args: Any) -> None:
        return

def _build_hardcoded_config(*, allow_to_override: bool) -> RobotBConfig:
    smtp_host = (os.getenv("SMTP_HOST") or SMTP_HOST).strip()
    smtp_port = int(os.getenv("SMTP_PORT") or str(SMTP_PORT))
    smtp_user = (os.getenv("SMTP_USER") or (SMTP_USER or "")).strip() or None
    smtp_pass = (os.getenv("SMTP_PASS") or (SMTP_PASS or "")).strip() or None
    smtp_starttls = (os.getenv("SMTP_STARTTLS") or str(SMTP_STARTTLS)).strip().lower() in {"1", "true", "yes", "y", "on"}

    mail_from = (os.getenv("SMTP_FROM") or MAIL_FROM).strip()
    default_to = (os.getenv("DEFAULT_TO") or DEFAULT_TO).strip()
    default_subject = (os.getenv("DEFAULT_SUBJECT") or DEFAULT_SUBJECT).strip() or "Robot B JSON Payload"

    if not smtp_host:
        raise SystemExit("SMTP_HOST must be set (env or constant).")
    if not mail_from:
        raise SystemExit("SMTP_FROM/MAIL_FROM must be set (env or constant).")
    if not default_to and not allow_to_override:
        raise SystemExit("DEFAULT_TO must be set unless allow-to-override is enabled.")

    return RobotBConfig(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        smtp_starttls=smtp_starttls,
        mail_from=mail_from,
        default_to=default_to,
        allow_to_override=allow_to_override,
        default_subject=default_subject,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Robot B: JSON receiver + email sender.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--default-to", default=os.getenv("DEFAULT_TO", ""), help="Default recipient")
    parser.add_argument("--subject", default="Robot B JSON Payload", help="Default subject")
    parser.add_argument(
        "--allow-to-override", 
        action="store_true", 
        help="Allow request JSON to set 'to'"
    )
    args = parser.parse_args()
    
    allow_override = bool(args.allow_to_override or ALLOW_TO_OVERRIDE_DEFAULT)
    cfg = _build_hardcoded_config(allow_to_override=allow_override)

    httpd = HTTPServer((args.host, args.port), RobertaEmailerHandler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    print(f"Robot B listening on http://{args.host}:{args.port}/ingest")
    httpd.serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())