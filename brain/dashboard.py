"""Renders the brain's GitHub pages from brain/state/brain.db:

  brain/README.md                         dashboard (status, progress, charts, today, history)
  brain/reports/YYYY/MM/YYYY-MM-DD.md     one report per day
  brain/reports/README.md                 index of every daily report
  brain/reports/LATEST.md                 copy of the newest daily report
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from . import brain, community

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
TARGET_DAYS = brain.HORIZON_DAYS + 1
SOURCE_NAMES = {"pennyrecon": "PennyRecon", "paydirt": "Paydirt", "ppm": "Penny Pinchin' Mom"}


# ------------------------------------------------------------------ data ---
def history(con) -> list[dict]:
    """One row per day the brain ran, oldest first."""
    days = [r[0] for r in con.execute("SELECT snap_date FROM snapshot_runs ORDER BY snap_date")]
    out = []
    for d in days:
        total = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports "
                            "WHERE date(first_collected) <= ?", (d,)).fetchone()[0]
        new = con.execute("SELECT COUNT(*) FROM (SELECT sku, MIN(date(first_collected)) d FROM community_reports GROUP BY sku) "
                          "WHERE d = ?", (d,)).fetchone()[0]
        active = con.execute("SELECT items FROM snapshot_runs WHERE snap_date = ?", (d,)).fetchone()[0]
        fl = con.execute("SELECT COUNT(*) FROM item_snapshots WHERE snap_date = ? AND has_fl = 1",
                         (d,)).fetchone()[0]
        spread = con.execute("SELECT COUNT(*) FROM item_snapshots WHERE snap_date = ? AND has_fl = 0 "
                             "AND states_gain_7d > 0", (d,)).fetchone()[0]
        src = {r[0]: r[1] for r in con.execute(
            "SELECT source, MAX(CASE WHEN status='ok' THEN items ELSE -1 END) FROM collect_runs "
            "WHERE date(ran_at) = ? GROUP BY source", (d,))}
        out.append(dict(date=d, total=total, new=new, active=active, fl=fl, spread=spread, sources=src))
    return out


def _fl(con, limit=10):
    hot = community.hottest(con, days=7, limit=60)
    return [r for r in hot if r["states"] and "FL" in r["states"].split(",")][:limit]


# ------------------------------------------------------------- helpers ---
def _bar(n: int, total: int, width: int = 15) -> str:
    filled = min(width, round(width * n / total)) if total else 0
    return "█" * filled + "░" * (width - filled)


def _money(v) -> str:
    return f"${v:,.0f}" if v else "—"


def _hd(sku: str, name: str | None, n: int = 60) -> str:
    name = (name or "(sin nombre)").replace("|", "/")
    name = name[:n] + ("…" if len(name) > n else "")
    return f"[{name}](https://www.homedepot.com/s/{sku})"


def _states(s: str | None) -> str:
    st = sorted({x for x in (s or "").split(",") if x})
    if not st:
        return "—"
    txt = ", ".join(("**FL**" if x == "FL" else x) for x in st[:6])
    return txt + (f" +{len(st) - 6}" if len(st) > 6 else "")


def _item_table(rows, with_states=True) -> list[str]:
    head = "| # | Producto | SKU | Precio | Reportes | Fuentes |" + (" Estados |" if with_states else "")
    sep = "|--:|---|---|--:|--:|:-:|" + ("---|" if with_states else "")
    out = [head, sep]
    for i, r in enumerate(rows, 1):
        line = (f"| {i} | {_hd(r['sku'], r['name'])} | `{r['sku']}` | {_money(r['retail'])} "
                f"| {r['reports']} | {r['sources']} |")
        if with_states:
            line += f" {_states(r['states'])} |"
        out.append(line)
    return out


def _pred_table(preds) -> list[str]:
    out = ["| # | Probabilidad de llegar a FL (14 días) | Producto | SKU |", "|--:|---|---|---|"]
    for i, (p, s, n) in enumerate(preds, 1):
        out.append(f"| {i} | `{_bar(round(p * 100), 100, 10)}` **{p * 100:.0f}%** | {_hd(s, n)} | `{s}` |")
    return out


def _spread_table(rows) -> list[str]:
    out = ["| # | Producto | SKU | Estados ganados (7 días) | Estados | Reportes |", "|--:|---|---|:-:|--:|--:|"]
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {_hd(r['sku'], r['name'])} | `{r['sku']}` | **+{r['states_gain_7d']}** "
                   f"| {r['n_states']} | {r['report_count']} |")
    return out


def _chart(hist: list[dict]) -> list[str]:
    if len(hist) < 2:
        return ["_El gráfico aparece a partir del segundo día._"]
    h = hist[-30:]
    labels = ", ".join(f'"{x["date"][5:]}"' for x in h)
    top = max(x["total"] for x in h)
    return ["```mermaid",
            "xychart-beta",
            '  title "Productos en la base (barras) y activos en la foto del día (línea)"',
            f"  x-axis [{labels}]",
            f'  y-axis "productos" 0 --> {int(top * 1.15) + 1}',
            f"  bar [{', '.join(str(x['total']) for x in h)}]",
            f"  line [{', '.join(str(x['active']) for x in h)}]",
            "```"]


def _report_path(d: str) -> Path:
    return REPORTS / d[:4] / d[5:7] / f"{d}.md"


def _src_cell(src: dict) -> str:
    if not src:
        return "—"
    return " ".join("✅" if v != -1 else "❌" for v in src.values())


# --------------------------------------------------------------- render ---
def render(con, counts: dict, rep: dict) -> None:
    today = date.today().isoformat()
    hist = history(con)
    days = len(hist)
    total = hist[-1]["total"] if hist else 0
    new = hist[-1]["new"] if hist else 0
    spread = rep.get("spreading") or []
    from .run import accuracy_line
    acc_line = accuracy_line(rep.get("accuracy") or {})
    fl = _fl(con)
    top = community.hottest(con, days=7, limit=10)
    # an unreliable network still trains and gets scored, but its picks aren't shown
    preds = (rep.get("predictions") or []) if rep.get("reliable", True) else []
    src_ok = {k: v for k, v in counts.items() if not k.startswith("_")}

    if rep.get("trained") and rep.get("reliable", True):
        state = f"🟢 Entrenada · AUC **{rep['cv_auc']}** con {rep['labeled']} ejemplos"
    elif rep.get("trained"):
        state = (f"🟠 Entrenada con pocos ejemplos · AUC **{rep['cv_auc']}** (aún no supera al azar; "
                 f"sus predicciones se muestran cuando pase de {brain.MIN_AUC})")
    elif rep.get("waiting_for") == "examples":
        state = (f"🟡 Esperando ejemplos · **{rep.get('positive_items', 0)} de {rep.get('items_needed', brain.MIN_PER_CLASS)}** "
                 f"productos llegaron a Florida (hacen falta {rep.get('items_needed', brain.MIN_PER_CLASS)} para entrenar)")
    else:
        state = f"🟡 Juntando datos · día **{min(days, TARGET_DAYS)} de {TARGET_DAYS}**"

    # ---- daily report
    src_lines = [f"| {SOURCE_NAMES.get(k, k)} | {'❌ falló' if v == -1 else '✅'} | {'' if v == -1 else v} |"
                 for k, v in src_ok.items()]
    daily = [f"# 🧠 Reporte del cerebro — {today}", "",
             "[← Panel](../../../README.md) · [Todos los reportes](../../README.md)", "",
             "| Productos en la base | Nuevos hoy | Activos (14 días) | En Florida (14 días) | Extendiéndose |",
             "|:-:|:-:|:-:|:-:|:-:|",
             f"| **{total}** | **{new}** | **{hist[-1]['active'] if hist else 0}** "
             f"| **{hist[-1]['fl'] if hist else 0}** | **{hist[-1]['spread'] if hist else 0}** |", "",
             f"**Red neuronal:** {state}", "", f"**{acc_line}**", ""]
    if preds:
        daily += ["## 🔮 Predicciones: llegarían a Florida pronto", ""] + _pred_table(preds) + [""]
    if spread:
        daily += ["## 🔥 Se están extendiendo (aún no en Florida)", ""] + _spread_table(spread) + [""]
    daily += ["## 📍 Reportados en Florida (últimos 7 días)", ""]
    daily += (_item_table(fl, with_states=False) if fl else ["_Ninguno esta semana._"]) + [""]
    daily += ["## 🔥 Más fuertes en todo el país (7 días)", ""] + _item_table(top) + [""]
    daily += ["## 🌐 Fuentes", "", "| Fuente | Estado | Productos |", "|---|:-:|--:|"] + src_lines + [""]
    daily += ["<sub>Generado automáticamente por GitHub Actions. "
              "Los enlaces abren la búsqueda del SKU en homedepot.com.</sub>", ""]
    path = _report_path(today)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(daily), encoding="utf-8")
    (REPORTS / "LATEST.md").write_text(
        "\n".join(daily).replace("../../../README.md", "../README.md").replace("../../README.md", "README.md"),
        encoding="utf-8")

    # ---- reports index
    idx = ["# 📚 Todos los reportes diarios", "", "[← Panel](../README.md)", "",
           "| Día | Productos | Nuevos | Activos | En FL | Extendiéndose | Fuentes |",
           "|---|--:|--:|--:|--:|--:|:-:|"]
    for h in reversed(hist):
        p = _report_path(h["date"])
        link = f"[{h['date']}]({p.relative_to(REPORTS).as_posix()})" if p.exists() else h["date"]
        idx.append(f"| {link} | {h['total']} | {h['new']} | {h['active']} | {h['fl']} | {h['spread']} "
                   f"| {_src_cell(h['sources'])} |")
    (REPORTS / "README.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    # ---- dashboard: one table, one row per day
    from datetime import timedelta

    trained = rep.get("trained")
    done = min(days, TARGET_DAYS)
    rows = ["| Día | Fecha | Estado | Productos | Nuevos | En FL | Extendiéndose | Reporte |",
            "|:-:|---|---|--:|--:|--:|--:|:-:|"]
    for i, h in enumerate(hist, 1):
        if i == len(hist) and trained:
            st = f"🧠 entrenada · AUC {rep['cv_auc']}"
        elif i >= TARGET_DAYS:
            st = "🧠 aprendiendo"
        else:
            st = "✅ listo" if all(v != -1 for v in h["sources"].values()) else "⚠️ fuente con error"
        p = _report_path(h["date"])
        link = f"[ver]({p.relative_to(ROOT).as_posix()})" if p.exists() else "—"
        rows.append(f"| {i} | {h['date']} | {st} | {h['total']} | {h['new']} | {h['fl']} "
                    f"| {h['spread']} | {link} |")
    last = date.fromisoformat(hist[-1]["date"]) if hist else date.today()
    upcoming = max(TARGET_DAYS - days, 1)          # until first training, then just tomorrow
    for k in range(1, upcoming + 1):
        n = days + k
        d = (last + timedelta(days=k)).isoformat()
        if k == 1:
            st = f"`{_bar(done, TARGET_DAYS, 10)}` 🔄 en progreso"
        else:
            st = f"`{_bar(0, 1, 10)}` ⏳ pendiente"
        if n == TARGET_DAYS:
            st += " · 🧠 primer entrenamiento"
        rows.append(f"| {n} | {d} | {st} | | | | | |")

    if trained and rep.get("reliable", True):
        head = f"🟢 **Red entrenada** · AUC {rep['cv_auc']}"
    elif trained:
        head = (f"🟠 **Red entrenada con pocos ejemplos** · AUC {rep['cv_auc']}. Todavía no supera al azar: "
                f"sigue entrenando cada día y sus predicciones aparecen cuando pase de {brain.MIN_AUC}.")
    elif rep.get("waiting_for") == "examples":
        need = rep.get("items_needed", brain.MIN_PER_CLASS)
        got = min(rep.get("positive_items", 0), need)
        head = (f"🟡 **Esperando ejemplos** · `{_bar(got, need, 20)}` {got}/{need} productos llegaron "
                f"a Florida. La red entrena en cuanto haya {need}.")
    else:
        head = (f"🟡 **Aprendiendo** · `{_bar(done, TARGET_DAYS, 20)}` {done}/{TARGET_DAYS} días "
                f"para el primer entrenamiento")
    dash = ["# 🧠 Penny Brain", "",
            "Se actualiza solo una vez al día por la mañana (Florida) y te manda el resumen por Telegram.", "",
            head, "", acc_line, "",
            "## 📅 Registro diario", "", *rows, ""]
    if preds:
        dash += ["## 🔮 Predicciones de la red", ""] + _pred_table(preds[:5]) + [""]
    if spread:
        dash += ["## 🔥 Se están extendiendo (aún no en Florida)", ""] + _spread_table(spread[:5]) + [""]
    dash += ["<sub>Productos = total en la base · Nuevos = vistos por primera vez ese día · "
             "En FL = activos reportados en Florida · Extendiéndose = sin FL todavía pero ganando estados "
             "en los últimos 7 días · Aciertos = cuántas de esas listas llegaron a Florida en 14 días. "
             "Datos para Excel: [products.csv](products.csv) · "
             "Todos los reportes: [reports/](reports/README.md)</sub>", ""]
    (ROOT / "README.md").write_text("\n".join(dash), encoding="utf-8")
