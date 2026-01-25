# RobertaEmailer — JSON → Email Gateway

En lille HTTP-service som modtager JSON fra `/ingest` og sender den via mail (body + `data.json` attachment).
Inkluderer `/health` til at tjekke status

## Krav
- Python **3.10+** (3.11+ foretrukket)
- SMTP access (Standard port **587** med STARTTLS)

## Hurtig Start

### 1) Opret dit `.env`
Kopier `.env.template` og indsæt ønskede værdier
