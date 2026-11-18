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

from . import brain, community, dashboard, notify, store

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"


def _line(r) -> str:
    price = f" ${r['retail']:.0f}" if r["retail"] else ""
    return f"{(r['name'] or '')[:55]}{price} — SKU {r['sku']} · {r['reports']} reportes"


def accuracy_line(acc: dict) -> str:
    """'🎯 Aciertos: extendiéndose 7/10 (70%) · red 6/10 · al azar 12%' or when it starts."""
    parts = []
    for key, label in (("spreading", "extendiéndose"), ("network", "red")):
        a = acc.get(key)
        if a:
            parts.append(f"{label} {a['hits']}/{a['judged']} ({a['rate'] * 100:.0f}%)")
    if not parts:
        when = acc.get("first_judged_on")
        return f"🎯 Aciertos: se miden desde el {when}" if when else "🎯 Aciertos: aún sin predicciones"
    rnd = acc.get("random")
    return "🎯 Aciertos: " + " · ".join(parts) + (f" · al azar {rnd['rate'] * 100:.0f}%" if rnd else "")


def build(con, counts: dict, rep: dict) -> str:
    """Plain-text summary for the job log."""
    hot = community.hottest(con, days=7, limit=40)
    fl = [r for r in hot if r["states"] and "FL" in r["states"].split(",")][:8]
    total = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports").fetchone()[0]
    lines = [f"Productos: {total}", f"En Florida (7 días): {len(fl)}", f"Modelo: {rep['status']}",
             accuracy_line(rep.get("accuracy") or {}), "Se están extendiendo:"]
    lines += [f"  +{r['states_gain_7d']} estados · {(r['name'] or '')[:50]} ({r['sku']})"
              for r in rep.get("spreading") or []]
    return "\n".join(lines)


def caption(con, counts: dict, rep: dict, limit: int = 1024) -> str:
    """Compact HTML summary that fits in a Telegram caption (1024 chars)."""
    e = html.escape
    hot = community.hottest(con, days=7, limit=40)
    fl = [r for r in hot if r["states"] and "FL" in r["states"].split(",")]
    spread = rep.get("spreading") or []
    total = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports").fetchone()[0]
    new = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports "
                      "WHERE date(first_collected) = date('now', 'localtime')").fetchone()[0]
    days = rep.get("snapshot_days", 0)
    target = brain.HORIZON_DAYS + 1

    def item(r, n=34):
        name = (r["name"] or "")[:n].rstrip()
        price = f" ${r['retail']:.0f}" if r["retail"] else ""
        return f"• {e(name)}{price}\n   <code>{r['sku']}</code> · {r['reports']} rep."

    def sitem(r, n=34):
        return (f"• {e((r['name'] or '')[:n].rstrip())}\n   <code>{r['sku']}</code> · "
                f"+{r['states_gain_7d']} estados ({r['n_states']} en total)")

    head = [f"✅ <b>Actualización lista</b> · día {days}"
            + ("" if rep.get("trained") else f" de {target}"),
            f"📦 {total} productos · {new} nuevos hoy", ""]
    if rep.get("trained"):
        brain_l = [f"🧠 <b>Red entrenada</b> · AUC {rep['cv_auc']}"]
        brain_l += [f"🔮 {p*100:.0f}% {e(n[:30])} <code>{s}</code>" for p, s, n in rep["predictions"][:3]]
    else:
        filled = round(10 * min(days, target) / target)
        brain_l = [f"🧠 Aprendiendo {'▰' * filled}{'▱' * (10 - filled)} {min(days, target)}/{target}"]
    brain_l.append(accuracy_line(rep.get("accuracy") or {}))
    failed = [k for k, v in counts.items() if v == -1]
    tail = brain_l + ([f"⚠️ Fuentes con error: {', '.join(failed)}"] if failed else [])

    for n_fl, n_sp in ((4, 3), (3, 3), (3, 2), (2, 2), (2, 1), (1, 1), (1, 0), (0, 0)):
        body = []
        if fl and n_fl:
            body += ["📍 <b>En Florida (7 días)</b>"] + [item(r) for r in fl[:n_fl]] + [""]
        if spread and n_sp:
            body += ["🔥 <b>Se están extendiendo</b> (aún no en FL)"] + [sitem(r) for r in spread[:n_sp]] + [""]
        text = "\n".join(head + body + tail)
        if len(text) <= limit:
            return text
    return "\n".join(head + tail)[:limit]


def weekly_message(con, rep: dict) -> str:
    from . import insights
    e = html.escape
    w = insights.weekly(con)
    lines = ["📅 <b>Resumen de la semana</b> · Penny Brain", "",
             f"📦 {w['new_products']} productos nuevos esta semana",
             f"📍 En Florida: {w['fl_now']} (hace 7 días: {w['fl_week_ago']})",
             accuracy_line(rep.get("accuracy") or {})]
    if w["arrived_fl"]:
        lines += ["", "🆕 <b>Llegaron a Florida esta semana</b>"]
        lines += [f"• {e((a['name'] or '')[:45])} <code>{a['sku']}</code>" for a in w["arrived_fl"][:8]]
    spread = rep.get("spreading") or []
    if spread:
        lines += ["", "🔥 <b>Los que más se extienden</b>"]
        lines += [f"• +{r['states_gain_7d']} estados · {e((r['name'] or '')[:40])} <code>{r['sku']}</code>"
                  for r in spread[:5]]
    if w["source_errors"]:
        lines += ["", "⚠️ Fuentes con errores: " + ", ".join(f"{k} ({v})" for k, v in w["source_errors"].items())]
    return "\n".join(lines)[:4000]


def export_products(con) -> None:
    """brain/products.csv: one row per product with its latest numbers, for Excel/Sheets."""
    last = con.execute("SELECT MAX(snap_date) FROM item_snapshots").fetchone()[0]
    rows = con.execute(
        """SELECT c.sku, MAX(c.name) name, MAX(c.internet_number) internet_number, MAX(c.retail) retail,
                  MIN(c.reported_at) first_reported, MAX(c.reported_at) last_reported,
                  GROUP_CONCAT(DISTINCT c.source) sources,
                  s.report_count, s.n_states, s.has_fl, s.states_gain_7d, s.reports_gain_7d
           FROM community_reports c
           LEFT JOIN item_snapshots s ON s.sku = c.sku AND s.snap_date = ?
           GROUP BY c.sku ORDER BY s.has_fl DESC, s.states_gain_7d DESC, s.report_count DESC""",
        (last,)).fetchall()
    with open(ROOT / "products.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["sku", "name", "internet_number", "retail", "first_reported", "last_reported",
                    "sources", "reports", "states", "in_florida", "states_gained_7d",
                    "reports_gained_7d", "homedepot_link"])
        for r in rows:
            w.writerow([*[("" if v is None else v) for v in tuple(r)],
                        f"https://www.homedepot.com/s/{r['sku']}"])


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
        prog.step("Buscando los que se extienden y entrenando la red")
        rep = brain.run_daily(con)
        prog.step("Generando el reporte")
        tg = build(con, counts, rep)
        dashboard.render(con, counts, rep)
        export_products(con)
        store.save(con)                 # CSV state that the workflow commits
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
        if date.today().weekday() == 6:          # Sunday
            notify.send(weekly_message(con, rep), html=True)
        if not sent:
            print("::error::Telegram message was not delivered (check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID secrets)")
    # every source failing means the run learned nothing new: fail the job so you see it
    ok_sources = [v for k, v in counts.items() if not k.startswith("_") and v != -1]
    if not ok_sources:
        print("::error::every community source failed today")
    failed = [k for k, v in counts.items() if v == -1]
    if failed and ok_sources:   # partial failure: job stays green, so warn in Slack
        notify.slack_alert(f":warning: *Penny Brain*: fuentes con error hoy: {', '.join(failed)}. "
                           "La actualización siguió con las demás.")
    return 0 if ok_sources and sent else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
