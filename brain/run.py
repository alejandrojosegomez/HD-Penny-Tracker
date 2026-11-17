"""Daily brain run (GitHub Actions or local):

    python -m brain.run            # collect -> snapshot -> train -> predict -> report -> Telegram
    python -m brain.run --no-send  # same, but print the Telegram message instead of sending it

Writes (and the workflow commits back to the repo):
  brain/state/brain.db            community reports, daily snapshots, your scan outcomes
  brain/state/model.joblib        the trained neural network (once there is enough data)
  brain/state/model_report.json   last training report
  brain/README.md                 dashboard (see dashboard.py)
  brain/reports/YYYY/MM/DATE.md   the day's analysis report
  brain/reports/LATEST.md         copy of the newest report
  brain/predictions.csv           today's ranked predictions (for other components to read)
"""
from __future__ import annotations

import argparse
import html
import csv
import sys
from datetime import date
from pathlib import Path

from . import brain, community, dashboard, notify

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"


def _line(r) -> str:
    price = f" ${r['retail']:.0f}" if r["retail"] else ""
    return f"{(r['name'] or '')[:55]}{price} — SKU {r['sku']} · {r['reports']} reportes"


def build(con, counts: dict, rep: dict) -> tuple[str, str]:
    """Returns (telegram_text, markdown_report)."""
    hot = community.hottest(con, days=7, limit=40)
    fl = [r for r in hot if r["states"] and "FL" in r["states"].split(",")][:8]
    top = hot[:5]
    total = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports").fetchone()[0]
    new_today = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports "
                            "WHERE date(first_collected) = date('now')").fetchone()[0]
    failed = [k for k, v in counts.items() if v == -1]

    if rep.get("trained"):
        brain_lines = [f"Entrenada con {rep['labeled']} ejemplos · AUC {rep['cv_auc']}"]
        if rep.get("predictions"):
            brain_lines.append("Probables en FLORIDA pronto:")
            brain_lines += [f"{p*100:.0f}% · {n[:50]} (SKU {s})" for p, s, n in rep["predictions"][:5]]
    else:
        brain_lines = [f"Juntando datos: día {rep['snapshot_days']} de ~{brain.HORIZON_DAYS + 1} "
                       f"({rep['labeled']} ejemplos listos)"]
    brain_lines.append(f"Tus escaneos guardados: {rep['total_outcomes']}"
                       + (f" (+{len(rep['new_outcomes'])} nuevos)" if rep["new_outcomes"] else ""))

    tg = ["🟠 HD Penny Tracker — resumen diario",
          f"Base: {total} productos · {new_today} nuevos hoy"]
    if fl:
        tg += ["", "📍 Reportados en FLORIDA (7 días):"] + [f"• {_line(r)}" for r in fl]
    tg += ["", "🔥 Más fuertes en todo el país:"] + [f"• {_line(r)}" for r in top]
    tg += ["", "🧠 Red neuronal"] + brain_lines
    if failed:
        tg += ["", f"⚠️ Fuentes con error: {', '.join(failed)}"]
    tg += ["", "Reporta así: /penny SKU tienda  o  /no SKU tienda"]

    md = [f"# Reporte del cerebro — {date.today().isoformat()}", "",
          f"- Productos en la base: **{total}** ({new_today} nuevos hoy)",
          f"- Fuentes: " + ", ".join(f"{k} {'❌' if v == -1 else v}" for k, v in counts.items()
                                     if not k.startswith("_")),
          f"- Estado del modelo: {rep['status']}", ""]
    md += ["## 🧠 Red neuronal", ""] + [f"- {x}" for x in brain_lines] + [""]
    if fl:
        md += ["## 📍 Reportados en Florida (7 días)", ""] + [f"- {_line(r)}" for r in fl] + [""]
    md += ["## 🔥 Más fuertes en todo el país", ""] + [f"- {_line(r)}" for r in top] + [""]
    return "\n".join(tg), "\n".join(md)


def caption(con, counts: dict, rep: dict, limit: int = 1024) -> str:
    """Compact HTML summary that fits in a Telegram caption (1024 chars)."""
    e = html.escape
    hot = community.hottest(con, days=7, limit=40)
    fl = [r for r in hot if r["states"] and "FL" in r["states"].split(",")]
    total = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports").fetchone()[0]
    new = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports "
                      "WHERE date(first_collected) = date('now')").fetchone()[0]
    days = rep.get("snapshot_days", 0)
    target = brain.HORIZON_DAYS + 1

    def item(r, n=34):
        name = (r["name"] or "")[:n].rstrip()
        price = f" ${r['retail']:.0f}" if r["retail"] else ""
        return f"• {e(name)}{price}\n   <code>{r['sku']}</code> · {r['reports']} rep."

    head = [f"✅ <b>Actualización lista</b> · día {days}"
            + ("" if rep.get("trained") else f" de {target}"),
            f"📦 {total} productos · {new} nuevos hoy", ""]
    if rep.get("trained"):
        brain_l = [f"🧠 <b>Red entrenada</b> · AUC {rep['cv_auc']}"]
        brain_l += [f"🔮 {p*100:.0f}% {e(n[:30])} <code>{s}</code>" for p, s, n in rep["predictions"][:3]]
    else:
        filled = round(10 * min(days, target) / target)
        brain_l = [f"🧠 Aprendiendo {'▰' * filled}{'▱' * (10 - filled)} {min(days, target)}/{target}"]
    brain_l.append(f"📝 Escaneos: {rep['total_outcomes']}"
                   + (f" (+{len(rep['new_outcomes'])} hoy)" if rep["new_outcomes"] else ""))
    failed = [k for k, v in counts.items() if v == -1]
    tail = brain_l + ([f"⚠️ Fuentes con error: {', '.join(failed)}"] if failed else []) \
        + ["", "<i>/penny SKU tienda · /no SKU tienda</i>"]

    for n_fl, n_top in ((5, 3), (4, 2), (3, 2), (3, 1), (2, 0), (1, 0)):
        body = []
        if fl:
            body += ["📍 <b>En Florida (7 días)</b>"] + [item(r) for r in fl[:n_fl]] + [""]
        if n_top:
            body += ["🔥 <b>Más fuertes del país</b>"] + [item(r) for r in hot[:n_top]] + [""]
        text = "\n".join(head + body + tail)
        if len(text) <= limit:
            return text
    return "\n".join(head + tail)[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    prog = notify.Progress(enabled=not args.no_send)
    prog.start("Procesando datos del día")
    try:
        con = brain.connect()
        prog.step("Juntando listas de la comunidad")
        counts = community.collect(con)
        prog.step("Leyendo tus escaneos y entrenando la red")
        rep = brain.run_daily(con)
        prog.step("Generando el reporte")
        tg, md = build(con, counts, rep)
        dashboard.render(con, counts, rep)
    except Exception as e:
        prog.finish(f"❌ La actualización falló: {type(e).__name__}: {str(e)[:200]}", ok=False)
        raise
    with open(ROOT / "predictions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "probability_fl_14d", "sku", "name"])
        for p, s, n in rep.get("predictions") or []:
            w.writerow([date.today().isoformat(), round(p, 4), s, n])

    print(tg)
    sent = True
    if not args.no_send:
        sent = prog.finish(caption(con, counts, rep))
        if not sent:
            print("::error::Telegram message was not delivered (check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID secrets)")
    # every source failing means the run learned nothing new: fail the job so you see it
    ok_sources = [v for k, v in counts.items() if not k.startswith("_") and v != -1]
    if not ok_sources:
        print("::error::every community source failed today")
    return 0 if ok_sources and sent else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
