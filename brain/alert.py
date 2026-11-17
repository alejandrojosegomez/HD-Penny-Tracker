"""Slack failure summary for the daily workflow: which step failed, the error,
and the file/line where it happened, with the last log lines.

    python -m brain.alert            # reads env + *.log files written by the workflow
    python -m brain.alert --dry-run  # print instead of posting
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

STEPS = [  # (env var with the step outcome, friendly name, log file)
    ("OUT_SIMULATED", "Fallo simulado (prueba)", None),
    ("OUT_INSTALL", "Instalar dependencias", None),
    ("OUT_BRAIN", "Juntar datos / entrenar / reporte", "brain_run.log"),
    ("OUT_SAVE", "Guardar en GitHub", "save.log"),
]

TB_LINE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
ERR_LINE = re.compile(r"^(?:[\w.]+(?:Error|Exception|Exit|Interrupt)|::error::)[^\n]*", re.M)
GIT_ERR = re.compile(r"^(?:error|fatal|hint: .*conflict|CONFLICT)[^\n]*", re.M | re.I)


def _rel(path: str) -> str:
    p = path.replace("\\", "/")
    return p.split("/brain/", 1)[-1] if "/brain/" in p and "site-packages" not in p else p


def analyze(log: str) -> dict:
    """Pulls the error message and the most relevant code location out of a log."""
    out = {"error": None, "where": None, "tail": "\n".join(log.strip().splitlines()[-12:])}
    frames = list(TB_LINE.finditer(log))
    if frames:
        own = [f for f in frames if "site-packages" not in f["file"] and "/lib/python" not in f["file"]]
        f = (own or frames)[-1]
        out["where"] = f"brain/{_rel(f['file'])}:{f['line']} (en {f['func']})" \
            if "/brain/" in f["file"].replace("\\", "/") else f"{_rel(f['file'])}:{f['line']} (en {f['func']})"
    git = GIT_ERR.findall(log)
    errs = ([e for e in ERR_LINE.findall(log) if not e.startswith("::error::")]
            or git or ERR_LINE.findall(log))
    if errs:
        pick = errs[0] if errs is git else errs[-1]
        out["error"] = pick.replace("::error::", "").strip()[:300]
    return out


HINTS = [  # (pattern in log, plain-language explanation)
    (r"unstaged changes|stash", "Quedaron archivos sin guardar y GitHub no dejó subir la actualización."),
    (r"rejected|non-fast-forward|could not push", "Alguien subió cambios al mismo tiempo y no se pudo guardar."),
    (r"Telegram message was not delivered|telegram error", "Telegram no recibió el mensaje: revisa los secrets TELEGRAM_TOKEN / TELEGRAM_CHAT_ID."),
    (r"every community source failed", "Ninguna página de la comunidad respondió: pueden estar caídas o haber cambiado su diseño."),
    (r"ConnectionError|Timeout|Max retries|HTTPError|5\d\d", "Una página o servicio externo no respondió a tiempo."),
    (r"ModuleNotFoundError|No matching distribution|pip", "Falló la instalación de dependencias de Python."),
    (r"sqlite3|database is locked|malformed", "Problema con la base de datos brain.db."),
]


def explain(log: str) -> str | None:
    for pat, msg in HINTS:
        if re.search(pat, log, re.I):
            return msg
    return None


def build(env=os.environ, logdir: Path = Path(".")) -> str:
    failed = [(name, log) for var, name, log in STEPS if env.get(var) == "failure"]
    run_url = env.get("RUN_URL", "")
    lines = [f":rotating_light: *Penny Brain falló* — {date.today().isoformat()}"]
    if not failed:
        lines.append("No se pudo identificar el paso que falló.")
    for name, log in failed:
        lines.append(f"\n*Paso que falló:* {name}")
        text = (logdir / log).read_text(encoding="utf-8", errors="replace") if log and (logdir / log).exists() else ""
        info = analyze(text) if text else {"error": None, "where": None, "tail": ""}
        if name.startswith("Fallo simulado"):
            info["error"] = info["error"] or "Fallo a propósito para probar esta alerta. Todo está bien."
        why = explain(text) if text else None
        if why:
            lines.append(f"*Qué pasó:* {why}")
        lines.append(f"*Error:* `{info['error'] or 'sin mensaje (revisa el log)'}`")
        if info["where"]:
            lines.append(f"*Dónde:* `{info['where']}`")
        if info["tail"]:
            lines.append("*Últimas líneas del log:*\n```" + info["tail"][-1500:] + "```")
    lines.append(f"\n<{run_url}|Ver la ejecución completa en GitHub>" if run_url else "")
    return "\n".join(l for l in lines if l is not None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    text = build()
    url = os.getenv("SLACK_WEBHOOK_URL")
    if a.dry_run or not url:
        print(text)
        return 0
    r = requests.post(url, data=json.dumps({"text": text}),
                      headers={"Content-Type": "application/json"}, timeout=15)
    print("slack:", r.status_code, r.text[:100])
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
