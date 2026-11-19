"""Prices you type in yourself (from the shelf tag or the Home Depot app).

Home Depot blocks automated price reads, so this is how store prices get into
price_history now. Each entry is scored right away and can fire a Telegram alert.
"""
from __future__ import annotations

from typing import Optional

from .detect import score_history
from .storage import get_storage


class ManualPriceError(ValueError):
    """Bad input; the message is shown to the user as is (Spanish)."""


def _valid_number(n: str) -> str:
    n = (n or "").strip()
    if not n.isdigit() or len(n) not in (9, 10):
        raise ManualPriceError("El internet # tiene 9 o 10 dígitos (el número al final del link del producto).")
    return n


def last_original_price(product_id: int) -> Optional[float]:
    rows = get_storage()._q(
        "SELECT original_price FROM price_history WHERE product_id=? AND original_price IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (product_id,))
    return rows[0]["original_price"] if rows else None


def record_price(internet_number: str, store: str, price: float, original_price: Optional[float] = None,
                 qty: Optional[int] = None, name: Optional[str] = None, send_alerts: bool = True) -> dict:
    """Save one observation for one store, run detect, and return the new score for that pair.

    original_price: the regular price ("was"). When left out, the last known one for this
    product is reused; without one the discount (and most of the score) can't be computed.
    qty: units on the shelf. >0 = in stock, 0 = out of stock, None = not checked.
    """
    db = get_storage()
    n = _valid_number(internet_number)
    st = next((s for s in db.stores() if s["hd_store_id"] == str(store).strip().zfill(4)), None)
    if not st:
        raise ManualPriceError(f"Tienda {store} no está en config/stores.yaml.")
    if price is None or price <= 0:
        raise ManualPriceError("El precio tiene que ser mayor que $0.")
    if qty is not None and qty < 0:
        raise ManualPriceError("La cantidad no puede ser negativa.")

    prod = db.product_by_internet_number(n)
    if original_price is None:
        original_price = last_original_price(prod["id"]) if prod else None
        if original_price is None:
            raise ManualPriceError("Falta el precio original (el precio normal antes del descuento). "
                                   "Sin él no se puede calcular el % de descuento.")
    if original_price < price:
        raise ManualPriceError("El precio original no puede ser menor que el precio actual.")

    # input is valid: only now create / reactivate the product
    if prod:
        if not prod.get("active"):
            db.upsert_product(n, active=1)
        if name and not prod.get("name"):
            db.upsert_product(n, name=name.strip())
        pid = prod["id"]
    else:
        pid = db.upsert_product(n, name=(name or "").strip() or None, source="manual", active=1)

    availability = None if qty is None else ("in_stock" if qty > 0 else "out_of_stock")
    db.add_price(pid, st["id"], round(float(price), 2), round(float(original_price), 2), qty,
                 availability or "unknown", raw={"source": "manual"})

    from .detect import run
    run(send_alerts=send_alerts)

    prod = db.product_by_internet_number(n)
    hist = db.history(pid, st["id"])
    score, reasons = score_history(hist, prod.get("category"), db.recent_reports(n))
    latest = hist[-1]
    return {"internet_number": n, "name": prod.get("name"), "hd_store_id": st["hd_store_id"],
            "store_name": st["name"], "price": latest["price"], "original_price": latest["original_price"],
            "discount_pct": latest["discount_pct"], "stock": latest["stock"],
            "availability": latest["availability"], "score": score, "reasons": reasons}
