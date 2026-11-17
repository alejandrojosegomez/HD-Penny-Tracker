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
        new = con.execute("SELECT COUNT(DISTINCT sku) FROM community_reports "
                          "WHERE date(first_collected) = ?", (d,)).fetchone()[0]
        active = con.execute("SELECT items FROM snapshot_runs WHERE snap_date = ?", (d,)).fetchone()[0]
        fl = con.execute("SELECT COUNT(*) FROM item_snapshots WHERE snap_date = ? AND has_fl = 1",
                         (d,)).fetchone()[0]
        scans = con.execute("SELECT COUNT(*) FROM scan_outcomes WHERE date(noted_at) = ?",
                            (d,)).fetchone()[0]
        src = {r[0]: r[1] for r in con.execute(
            "SELECT source, MAX(CASE WHEN status='ok' THEN items ELSE -1 END) FROM collect_runs "
            "WHERE date(ran_at) = ? GROUP BY source", (d,))}
        out.append(dict(date=d, total=total, new=new, active=active, fl=fl, scans=scans, sources=src))
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
    scans_total = rep.get("total_outcomes", 0)
    fl = _fl(con)
    top = community.hottest(con, days=7, limit=10)
    preds = rep.get("predictions") or []
    src_ok = {k: v for k, v in counts.items() if not k.startswith("_")}

    if rep.get("trained"):
        state = f"🟢 Entrenada · AUC **{rep['cv_auc']}** con {rep['labeled']} ejemplos"
        badge = "entrenada-brightgreen"
    else:
        state = f"🟡 Juntando datos · día **{min(days, TARGET_DAYS)} de {TARGET_DAYS}**"
        badge = f"aprendiendo_{min(days, TARGET_DAYS)}%2F{TARGET_DAYS}-yellow"

    # ---- daily report
    src_lines = [f"| {SOURCE_NAMES.get(k, k)} | {'❌ falló' if v == -1 else '✅'} | {'' if v == -1 else v} |"
                 for k, v in src_ok.items()]
    daily = [f"# 🧠 Reporte del cerebro — {today}", "",
             f"[← Panel](../../../README.md) · [Todos los reportes](../../README.md)", "",
             "| Productos en la base | Nuevos hoy | Activos (14 días) | En Florida (14 días) | Tus escaneos |",
             "|:-:|:-:|:-:|:-:|:-:|",
             f"| **{total}** | **{new}** | **{hist[-1]['active'] if hist else 0}** "
             f"| **{hist[-1]['fl'] if hist else 0}** | **{scans_total}** |", "",
             f"**Red neuronal:** {state}", ""]
    if preds:
        daily += ["## 🔮 Predicciones: llegarían a Florida pronto", ""] + _pred_table(preds) + [""]
    daily += ["## 📍 Reportados en Florida (últimos 7 días)", ""]
    daily += (_item_table(fl, with_states=False) if fl else ["_Ninguno esta semana._"]) + [""]
    daily += ["## 🔥 Más fuertes en todo el país (7 días)", ""] + _item_table(top) + [""]
    if rep.get("new_outcomes"):
        daily += ["## 📝 Escaneos tuyos leídos hoy", ""] + [f"- `{x}`" for x in rep["new_outcomes"]] + [""]
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
           "| Día | Productos | Nuevos | Activos | En FL | Escaneos | Fuentes |",
           "|---|--:|--:|--:|--:|--:|:-:|"]
    for h in reversed(hist):
        p = _report_path(h["date"])
        link = f"[{h['date']}]({p.relative_to(REPORTS).as_posix()})" if p.exists() else h["date"]
        idx.append(f"| {link} | {h['total']} | {h['new']} | {h['active']} | {h['fl']} | {h['scans']} "
                   f"| {_src_cell(h['sources'])} |")
    (REPORTS / "README.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    # ---- dashboard
    dash = [
        "<div align=\"center\">", "",
        "# 🧠 Penny Brain", "",
        "**Red neuronal que aprende sola, cada día, qué productos de Home Depot llegarán a penny en Florida.**", "",
        f"![estado](https://img.shields.io/badge/red_neuronal-{badge})",
        f"![días](https://img.shields.io/badge/días_de_datos-{days}-blue)",
        f"![productos](https://img.shields.io/badge/productos-{total}-orange)",
        f"![escaneos](https://img.shields.io/badge/tus_escaneos-{scans_total}-purple)",
        "", f"_Última actualización: **{today}** · corre solo todos los días a las 7:00 AM (Florida)_", "",
        "</div>", "",
        "---", "",
        "## 📈 Progreso del aprendizaje", "",
        f"```text\n{_bar(min(days, TARGET_DAYS), TARGET_DAYS, 30)}  {min(days, TARGET_DAYS)}/{TARGET_DAYS} días\n```", "",
        state, "",
        ("La red necesita ~15 días de historia para saber, para cada producto, si 14 días después "
         "terminó reportado en Florida. Hasta entonces junta datos; después se reentrena cada día."
         if not rep.get("trained") else rep["status"]), "",
        "## 📊 Crecimiento", "", *_chart(hist), "",
        "## 🗓️ Hoy", "",
        "| Productos en la base | Nuevos hoy | En Florida (14 días) | Tus escaneos |",
        "|:-:|:-:|:-:|:-:|",
        f"| **{total}** | **{new}** | **{hist[-1]['fl'] if hist else 0}** | **{scans_total}** |", "",
        f"📄 [Reporte completo de hoy]({_report_path(today).relative_to(ROOT).as_posix()})", "",
    ]
    if preds:
        dash += ["### 🔮 Predicciones", ""] + _pred_table(preds[:5]) + [""]
    dash += ["### 📍 En Florida esta semana", ""]
    dash += (_item_table(fl[:5], with_states=False) if fl else ["_Ninguno esta semana._"]) + [""]
    dash += ["## 🗂️ Historial", "",
             "| Día | Productos | Nuevos | Activos | En FL | Escaneos | Fuentes |",
             "|---|--:|--:|--:|--:|--:|:-:|"]
    for h in list(reversed(hist))[:10]:
        p = _report_path(h["date"])
        link = f"[{h['date']}]({p.relative_to(ROOT).as_posix()})" if p.exists() else h["date"]
        dash.append(f"| {link} | {h['total']} | {h['new']} | {h['active']} | {h['fl']} | {h['scans']} "
                    f"| {_src_cell(h['sources'])} |")
    dash += ["", "📚 [Ver todos los reportes](reports/README.md)", "",
             "---", "",
             "## ⚙️ Cómo funciona", "",
             "```mermaid",
             "flowchart LR",
             "  A[🌐 Listas de la comunidad<br/>PennyRecon · Paydirt · PPM] --> B[(🗄️ brain.db)]",
             "  T[📱 Tus /penny y /no<br/>en Telegram] --> B",
             "  B --> C[📸 Foto diaria<br/>de cada producto]",
             "  C --> D[🧠 Red neuronal<br/>se reentrena]",
             "  D --> E[🔮 Predicciones]",
             "  E --> F[📄 Reportes en GitHub]",
             "  E --> G[📱 Resumen en Telegram]",
             "```", "",
             "Reporta tus escaneos al bot: `/penny SKU tienda` o `/no SKU tienda` "
             "(ej. `/penny 1015277793 0260`). Cada escaneo le enseña a la red.", "",
             "| Carpeta / archivo | Qué es |", "|---|---|",
             "| [`brain.py`](brain.py) | la red neuronal: fotos, etiquetas, entrenamiento, predicción |",
             "| [`community.py`](community.py) | lectores de las listas de la comunidad |",
             "| [`run.py`](run.py) · [`dashboard.py`](dashboard.py) | el proceso diario y estas páginas |",
             "| [`reports/`](reports/README.md) | un reporte por día, por año/mes |",
             "| [`predictions.csv`](predictions.csv) | ranking de hoy, para otras partes del proyecto |",
             "| `state/` | base de datos y modelo (los actualiza el bot) |", "",
             "Correr a mano: pestaña **Actions → Brain daily → Run workflow**.", ""]
    (ROOT / "README.md").write_text("\n".join(dash), encoding="utf-8")
