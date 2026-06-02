import sys
import os
import shutil

# Ensure terminal output handles UTF-8 characters (e.g. ₹ symbol on Windows)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def resource_path(*relative_parts: str) -> str:
    if relative_parts and relative_parts[0] == '__data__':
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, *relative_parts[1:])
    else:
        if getattr(sys, 'frozen', False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, *relative_parts)

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
import uuid
import json
from datetime import datetime
from database import init_db, get_db
from license_manager import check_license, activate, LicenseStatus

# ─── Pydantic models ──────────────────────────────────────────────────────────

class Item(BaseModel):
    name: str
    price: float
    quantity: int
    discount: Optional[float] = 0.0   # always 0 now; kept for DB/receipt compat

class Cart(BaseModel):
    items: List[Item]
    subtotal: float          # sum of (price * qty) before discount
    discount_total: float    # sum of all line discounts
    tax_amount: float        # GST amount (tax-inclusive, extracted from total)
    total: float             # final amount paid

class InventoryItem(BaseModel):
    name: str
    price: float
    image_url: str
    category: Optional[str] = "General"
    stock: Optional[int] = 999

class CategoryCreate(BaseModel):
    name: str

class ClaimImageRequest(BaseModel):
    filename: str

class ActivationRequest(BaseModel):
    key: str

class ShopSettings(BaseModel):
    shop_name: str
    address: Optional[str] = ""
    phone: Optional[str] = ""
    gstin: Optional[str] = ""
    gst_mode: str = "none"              # "none" | "single" | "split"
    gst_rate: Optional[float] = 0.0    # total GST % (e.g. 18)
    sales_tax_enabled: Optional[bool] = False
    sales_tax_rate: Optional[float] = 0.0
    sales_tax_name: Optional[str] = "Sales Tax"

# ─── Printer Setup ────────────────────────────────────────────────────────────

ESC_INIT          = b"\x1b@"
ESC_CUT           = b"\x1dV\x00"
ESC_BOLD_ON       = b"\x1bE\x01"
ESC_BOLD_OFF      = b"\x1bE\x00"
ESC_ALIGN_CENTER  = b"\x1ba\x01"
ESC_ALIGN_LEFT    = b"\x1ba\x00"
ESC_ALIGN_RIGHT   = b"\x1ba\x02"
ESC_DOUBLE_HEIGHT = b"\x1b!\x10"
ESC_DOUBLE_WIDTH  = b"\x1b!\x20"
ESC_NORMAL_SIZE   = b"\x1b!\x00"
ESC_UNDERLINE_ON  = b"\x1b-\x01"
ESC_UNDERLINE_OFF = b"\x1b-\x00"
ESC_FEED_LINES    = lambda n: bytes([0x1b, 0x64, n])

RECEIPT_WIDTH = 42

def _write(h, data: bytes):
    import win32print
    win32print.WritePrinter(h, data)

def _wline(h, text: str):
    _write(h, (text + "\n").encode("utf-8", errors="replace"))

def _find_thermal_printer_win32() -> str:
    try:
        import win32print
        printers = [p[2] for p in win32print.EnumPrinters(2)]
    except Exception as e:
        print(f"[PRINTER] Could not enumerate printers: {e}")
        return ""
    thermal_keywords = ("thermal", "pos", "receipt", "tsp", "rp", "xp", "80mm", "58mm", "escpos")
    virtual_keywords = ("pdf", "xps", "fax", "onenote", "print to", "microsoft", "adobe")
    print(f"[PRINTER] Installed printers: {printers}")
    for name in printers:
        lower = name.lower()
        if any(k in lower for k in thermal_keywords):
            print(f"[PRINTER] Matched thermal printer: {name}")
            return name
    for name in printers:
        lower = name.lower()
        if not any(k in lower for k in virtual_keywords):
            print(f"[PRINTER] No thermal keyword match; using: {name}")
            return name
    print("[PRINTER] All printers appear virtual.")
    return ""

def _get_shop_settings() -> dict:
    """Load shop settings from DB."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT key, value FROM shop_settings")
            rows = cursor.fetchall()
            return {row['key']: row['value'] for row in rows}
    except Exception:
        return {}

def print_thermal_receipt(sale_id: int, cart) -> bool:
    try:
        import win32print
    except ImportError:
        print("[PRINTER] win32print not available — falling back to serial.")
        return _print_serial_fallback(sale_id, cart)

    printer_name = _find_thermal_printer_win32()
    if not printer_name:
        print("[PRINTER] No thermal printer found — falling back to serial.")
        return _print_serial_fallback(sale_id, cart)

    settings = _get_shop_settings()
    shop_name         = settings.get("shop_name", "FreshMarket POS")
    address           = settings.get("address", "")
    phone             = settings.get("phone", "")
    gstin             = settings.get("gstin", "")
    gst_mode          = settings.get("gst_mode", "none")
    gst_rate          = float(settings.get("gst_rate", 0) or 0)
    sales_tax_enabled = settings.get("sales_tax_enabled", "false") == "true"
    sales_tax_rate    = float(settings.get("sales_tax_rate", 0) or 0)
    sales_tax_name    = settings.get("sales_tax_name", "Sales Tax") or "Sales Tax"
    W = RECEIPT_WIDTH

    sales_tax_amount = round(cart.total * sales_tax_rate / 100, 2) if sales_tax_enabled and sales_tax_rate > 0 else 0
    grand_total = cart.total + sales_tax_amount

    has_gst       = gst_mode != "none" and cart.tax_amount > 0
    has_discount  = cart.discount_total > 0
    has_sales_tax = sales_tax_amount > 0
    show_subtotal = has_gst or has_discount or has_sales_tax

    try:
        hPrinter = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(hPrinter, 1, (f"{shop_name} Receipt", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)

            _write(hPrinter, ESC_INIT)
            _write(hPrinter, b'\x1b\x4a\x00')

            _write(hPrinter, ESC_ALIGN_CENTER)
            _write(hPrinter, ESC_BOLD_ON)
            _write(hPrinter, ESC_DOUBLE_HEIGHT)
            _wline(hPrinter, shop_name)
            _write(hPrinter, ESC_NORMAL_SIZE)
            _write(hPrinter, ESC_BOLD_OFF)
            if address:
                _wline(hPrinter, address)
            if phone:
                _wline(hPrinter, f"Ph: {phone}")
            if gstin:
                _wline(hPrinter, f"GSTIN: {gstin}")

            _write(hPrinter, ESC_ALIGN_CENTER)
            _wline(hPrinter, "=" * W)
            _wline(hPrinter, datetime.now().strftime("%d-%m-%Y  %H:%M:%S"))
            _wline(hPrinter, f"Sale ID: #{sale_id}")
            _wline(hPrinter, "-" * W)

            _write(hPrinter, ESC_ALIGN_LEFT + ESC_BOLD_ON)
            _wline(hPrinter, f"{'ITEM':<20} {'QTY':>3} {'PRICE':>8} {'AMT':>8}")
            _write(hPrinter, ESC_BOLD_OFF)
            _wline(hPrinter, "-" * W)

            for item in cart.items:
                line_total = item.quantity * item.price
                name = item.name[:20]
                line = f"{name:<20} {item.quantity:>3} {item.price:>8.2f} {line_total:>8.2f}"
                _wline(hPrinter, line)

            _wline(hPrinter, "-" * W)
            _write(hPrinter, ESC_ALIGN_LEFT)
            if show_subtotal:
                _wline(hPrinter, f"{'Subtotal:':<30} {cart.subtotal:>10.2f}")
            if has_discount:
                _wline(hPrinter, f"{'Discount:':<30} -{cart.discount_total:>9.2f}")

            if has_gst:
                if gst_mode == "split":
                    half = cart.tax_amount / 2
                    half_rate = gst_rate / 2
                    _wline(hPrinter, f"{'CGST (' + str(half_rate) + '%)':<30} {half:>10.2f}")
                    _wline(hPrinter, f"{'SGST (' + str(half_rate) + '%)':<30} {half:>10.2f}")
                else:
                    _wline(hPrinter, f"{'GST (' + str(gst_rate) + '%)':<30} {cart.tax_amount:>10.2f}")

            if has_sales_tax:
                label = f"{sales_tax_name} ({sales_tax_rate}%)"
                _wline(hPrinter, f"{label:<30} {sales_tax_amount:>10.2f}")

            _wline(hPrinter, "=" * W)
            _write(hPrinter, ESC_BOLD_ON + ESC_DOUBLE_WIDTH)
            _wline(hPrinter, f"TOTAL: Rs:{grand_total:>8.2f}")
            _write(hPrinter, ESC_NORMAL_SIZE + ESC_BOLD_OFF)
            _wline(hPrinter, "=" * W)

            _write(hPrinter, ESC_ALIGN_CENTER)
            _wline(hPrinter, "")
            _wline(hPrinter, "Thank you for shopping!")
            _wline(hPrinter, "Please come again :)")
            _wline(hPrinter, "")

            _write(hPrinter, ESC_FEED_LINES(3) + ESC_CUT)

            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)

        print(f"[PRINTER] Receipt sent to '{printer_name}' via win32print.")
        return True
    except Exception as e:
        print(f"[PRINTER] win32print error: {e} — trying serial fallback.")
        return _print_serial_fallback(sale_id, cart)


def _print_serial_fallback(sale_id: int, cart) -> bool:
    port = _find_serial_printer()
    if not port:
        print("[PRINTER] No USB printer port found.")
        return False
    try:
        import serial
        receipt = _build_receipt_text(sale_id, cart)
        with serial.Serial(port, baudrate=9600, timeout=2) as ser:
            ser.write(ESC_INIT)
            ser.write(ESC_ALIGN_LEFT)
            ser.write(ESC_BOLD_ON)
            ser.write(receipt.encode("cp437", errors="replace"))
            ser.write(ESC_BOLD_OFF)
            ser.write(ESC_FEED_LINES(4))
            ser.write(ESC_CUT)
        print(f"[PRINTER] Receipt sent to serial port {port}.")
        return True
    except Exception as e:
        print(f"[PRINTER] Serial fallback failed on {port}: {e}")
        return False


def _find_serial_printer() -> str | None:
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            desc = (p.description or "").lower()
            if any(k in desc for k in ("printer", "thermal", "pos", "receipt")):
                return p.device
        for p in ports:
            if "USB" in (p.description or "") or "USB" in (p.hwid or ""):
                return p.device
    except Exception:
        pass
    return None


def _build_receipt_text(sale_id: int, cart) -> str:
    settings = _get_shop_settings()
    shop_name         = settings.get("shop_name", "FreshMarket POS")
    address           = settings.get("address", "")
    phone             = settings.get("phone", "")
    gstin             = settings.get("gstin", "")
    gst_mode          = settings.get("gst_mode", "none")
    gst_rate          = float(settings.get("gst_rate", 0) or 0)
    sales_tax_enabled = settings.get("sales_tax_enabled", "false") == "true"
    sales_tax_rate    = float(settings.get("sales_tax_rate", 0) or 0)
    sales_tax_name    = settings.get("sales_tax_name", "Sales Tax") or "Sales Tax"
    W = RECEIPT_WIDTH

    # Sales tax is added on top of the cart total
    sales_tax_amount = round(cart.total * sales_tax_rate / 100, 2) if sales_tax_enabled and sales_tax_rate > 0 else 0
    grand_total = cart.total + sales_tax_amount

    has_gst       = gst_mode != "none" and cart.tax_amount > 0
    has_discount  = cart.discount_total > 0
    has_sales_tax = sales_tax_amount > 0
    show_subtotal = has_gst or has_discount or has_sales_tax

    lines = [
        "=" * W,
        shop_name.center(W),
    ]
    if address:
        lines.append(address.center(W))
    if phone:
        lines.append(f"Ph: {phone}".center(W))
    if gstin:
        lines.append(f"GSTIN: {gstin}".center(W))
    lines += [
        "CUSTOMER RECEIPT".center(W),
        "=" * W,
        f"Date: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}",
        f"Sale ID: #{sale_id}",
        "-" * W,
        f"{'ITEM':<20} {'QTY':>3} {'PRICE':>8} {'AMT':>8}",
        "-" * W,
    ]
    for item in cart.items:
        line_total = item.quantity * item.price
        name = item.name[:20]
        lines.append(f"{name:<20} {item.quantity:>3} {item.price:>8.2f} {line_total:>8.2f}")
    lines.append("-" * W)
    if show_subtotal:
        lines.append(f"{'Subtotal:':<30} {cart.subtotal:>10.2f}")
    if has_discount:
        lines.append(f"{'Discount:':<30}-{cart.discount_total:>10.2f}")
    if has_gst:
        if gst_mode == "split":
            half      = cart.tax_amount / 2
            half_rate = gst_rate / 2
            lines.append(f"{'CGST (' + str(half_rate) + '):' :<30} {half:>10.2f}")
            lines.append(f"{'SGST (' + str(half_rate) + '):' :<30} {half:>10.2f}")
        else:
            lines.append(f"{'GST (' + str(gst_rate) + '):' :<30} {cart.tax_amount:>10.2f}")
    if has_sales_tax:
        label = f"{sales_tax_name} ({sales_tax_rate}%):"
        lines.append(f"{label:<30} {sales_tax_amount:>10.2f}")
    lines += [
        f"{'TOTAL:':<30} Rs:{grand_total:>8.2f}",
        "=" * W,
        "Thank you for shopping!".center(W),
        "Please come again :)".center(W),
        "",
    ]
    return "\n".join(lines)


app = FastAPI(title="POS PoC")

init_db()

static_dir = resource_path('static')
bluetooth_inbox_dir = resource_path('__data__', 'data', 'bluetooth_inbox')
images_dir = resource_path('__data__', 'data', 'images')

# sessions.json lives inside __data__/data alongside images & bluetooth_inbox
data_dir = resource_path('__data__', 'data')
session_file = os.path.join(data_dir, 'sessions.json')

os.makedirs(bluetooth_inbox_dir, exist_ok=True)
os.makedirs(images_dir, exist_ok=True)

def _set_hidden_win(path: str) -> bool:
    """
    Mark a path as hidden on Windows using FILE_ATTRIBUTE_HIDDEN (0x02).
    Preserves existing attributes (e.g. DIRECTORY=0x10) by OR-ing them together.
    Returns True on success.
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Read existing attributes first so we don't strip DIRECTORY/READONLY/etc.
        existing = kernel32.GetFileAttributesW(path)
        if existing == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            existing = 0
        new_attrs = existing | 0x02  # OR in FILE_ATTRIBUTE_HIDDEN
        ret = kernel32.SetFileAttributesW(path, new_attrs)
        if ret:
            print(f"[HIDDEN] OK: '{path}'")
            return True
        else:
            err = kernel32.GetLastError()
            print(f"[HIDDEN] FAILED on '{path}': Windows error {err}")
            return False
    except Exception as e:
        print(f"[HIDDEN] Exception on '{path}': {e}")
        return False

def _hide_folders():
    """
    Hide __data__ and __data__/data on Windows so only the .exe is visible.
    Both levels must be marked individually — Windows does not auto-hide subfolders.
    """
    if os.name != 'nt':
        return
    for target in [
        resource_path('__data__'),
        resource_path('__data__', 'data'),
    ]:
        if os.path.exists(target):
            _set_hidden_win(target)
        else:
            print(f"[HIDDEN] Skipped (does not exist): {target}")

# All folders are created above; now mark them hidden
_hide_folders()

def _read_sessions() -> int:
    try:
        with open(session_file, 'r') as f:
            return int(json.load(f).get('count', 0))
    except Exception:
        return 0

def _increment_sessions() -> int:
    count = _read_sessions() + 1
    try:
        with open(session_file, 'w') as f:
            json.dump({'count': count}, f)
    except Exception:
        pass
    return count

app.mount("/static/images", StaticFiles(directory=images_dir), name="images")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Increment session count when server starts
_SESSION_COUNT = _increment_sessions()

@app.middleware("http")
async def add_utf8_charset(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith('.js'):
        response.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    elif request.url.path.endswith('.css'):
        response.headers['Content-Type'] = 'text/css; charset=utf-8'
    return response

@app.get("/")
def read_root():
    return FileResponse(resource_path('static', 'index.html'))

@app.get("/api/session-count")
def get_session_count():
    return {"count": _SESSION_COUNT}

# ─── License ──────────────────────────────────────────────────────────────────

@app.get("/api/license/status")
def license_status():
    return check_license()

@app.post("/api/license/activate")
def license_activate(req: ActivationRequest):
    result = activate(req.key)
    return result

@app.middleware("http")
async def license_guard(request: Request, call_next):
    """Block all non-license API calls when the app is not properly licensed."""
    path = request.url.path

    # Always allow: static assets, root, license endpoints
    allowed_prefixes = (
        "/static",
        "/api/license",
        "/api/session-count",
        "/api/exit",
        "/api/check-hidden",
    )
    if path == "/" or any(path.startswith(p) for p in allowed_prefixes):
        return await call_next(request)

    # Check license for all other API calls
    status = check_license()
    if status["status"] == LicenseStatus.ACTIVE:
        return await call_next(request)

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=402,
        content={
            "detail": "license_required",
            "status": status["status"],
            "message": status["message"],
            "secret_code": status["secret_code"],
        }
    )

@app.get("/api/check-hidden")
def check_hidden():
    """Debug endpoint: reports hidden attribute status for all managed folders."""
    if os.name != 'nt':
        return {"platform": "non-Windows; hidden attribute not applicable"}
    import ctypes
    kernel32 = ctypes.windll.kernel32
    targets = {
        "__data__":      resource_path('__data__'),
        "__data__/data": resource_path('__data__', 'data'),
    }
    results = {}
    for label, path in targets.items():
        entry = {"path": path, "exists": os.path.exists(path)}
        if entry["exists"]:
            attrs = kernel32.GetFileAttributesW(path)
            if attrs == 0xFFFFFFFF:
                entry["error"] = "GetFileAttributesW failed"
            else:
                entry["attributes_hex"] = hex(attrs)
                entry["is_hidden"] = bool(attrs & 0x02)
        results[label] = entry
    return results

# ─── Shop Settings ────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("SELECT key, value FROM shop_settings")
        raw = {row['key']: row['value'] for row in cursor.fetchall()}

    # Cast numeric and boolean fields so JS receives proper types
    def _float(k):  return float(raw.get(k) or 0)
    def _bool(k):   return raw.get(k, "false").lower() == "true"

    return {
        "shop_name":         raw.get("shop_name", "FreshMarket POS"),
        "address":           raw.get("address",   ""),
        "phone":             raw.get("phone",      ""),
        "gstin":             raw.get("gstin",      ""),
        "gst_mode":          raw.get("gst_mode",   "none"),
        "gst_rate":          _float("gst_rate"),
        "sales_tax_enabled": _bool("sales_tax_enabled"),
        "sales_tax_rate":    _float("sales_tax_rate"),
        "sales_tax_name":    raw.get("sales_tax_name", "Sales Tax"),
    }

@app.post("/api/settings")
def save_settings(settings: ShopSettings):
    pairs = [
        ("shop_name",         settings.shop_name),
        ("address",           settings.address or ""),
        ("phone",             settings.phone or ""),
        ("gstin",             settings.gstin or ""),
        ("gst_mode",          settings.gst_mode),
        ("gst_rate",          str(settings.gst_rate or 0)),
        ("sales_tax_enabled", "true" if settings.sales_tax_enabled else "false"),
        ("sales_tax_rate",    str(settings.sales_tax_rate or 0)),
        ("sales_tax_name",    settings.sales_tax_name or "Sales Tax"),
    ]
    with get_db() as db:
        cursor = db.cursor()
        for key, value in pairs:
            cursor.execute(
                "INSERT INTO shop_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
        db.commit()
    return {"status": "success"}

# ─── Checkout ─────────────────────────────────────────────────────────────────

@app.post("/api/checkout")
def checkout(cart: Cart):
    if not cart.items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    with get_db() as db:
        cursor = db.cursor()
        for item in cart.items:
            row = cursor.execute(
                "SELECT stock FROM inventory WHERE name = ?", (item.name,)
            ).fetchone()
            if row and row['stock'] != 999:
                if row['stock'] < item.quantity:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Insufficient stock for {item.name}. Available: {row['stock']}"
                    )
                cursor.execute(
                    "UPDATE inventory SET stock = stock - ? WHERE name = ?",
                    (item.quantity, item.name)
                )

        cursor.execute(
            "INSERT INTO sales (total, subtotal, discount_total, tax_amount) VALUES (?, ?, ?, ?)",
            (cart.total, cart.subtotal, cart.discount_total, cart.tax_amount)
        )
        sale_id = cursor.lastrowid

        for item in cart.items:
            cursor.execute(
                "INSERT INTO sale_items (sale_id, name, quantity, price, discount) VALUES (?, ?, ?, ?, ?)",
                (sale_id, item.name, item.quantity, item.price, item.discount or 0)
            )
        db.commit()

    receipt_text = _build_receipt_text(sale_id, cart)
    print("[CHECKOUT] Sale completed. Receipt:\n" + receipt_text)
    printed = print_thermal_receipt(sale_id, cart)

    return {
        "status": "success",
        "sale_id": sale_id,
        "printer_ok": printed,
        "receipt": receipt_text,
        "items": [{"name": i.name, "price": i.price, "quantity": i.quantity, "discount": i.discount} for i in cart.items],
        "subtotal": cart.subtotal,
        "discount_total": cart.discount_total,
        "tax_amount": cart.tax_amount,
        "total": cart.total
    }

# ─── Sales ────────────────────────────────────────────────────────────────────

@app.get("/api/sales")
def get_sales():
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sales ORDER BY timestamp DESC")
        sales = [dict(row) for row in cursor.fetchall()]
    return {"sales": sales}

@app.get("/api/sales/detailed")
def get_sales_detailed():
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sales ORDER BY timestamp DESC")
        sales = [dict(row) for row in cursor.fetchall()]
        for sale in sales:
            cursor.execute(
                "SELECT * FROM sale_items WHERE sale_id = ?", (sale['id'],)
            )
            sale['items'] = [dict(row) for row in cursor.fetchall()]
    return {"sales": sales}

# ─── Inventory ────────────────────────────────────────────────────────────────

@app.get("/api/inventory")
def get_inventory():
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM inventory")
        inventory = [dict(row) for row in cursor.fetchall()]
    return {"inventory": inventory}

@app.post("/api/inventory")
def add_inventory(item: InventoryItem):
    if not item.image_url or not item.image_url.strip():
        item.image_url = "/static/logo.png"
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO inventory (name, price, image_url, category, stock) VALUES (?, ?, ?, ?, ?)",
            (item.name, item.price, item.image_url, item.category, item.stock)
        )
        db.commit()
    return {"status": "success", "message": "Item added to inventory"}

@app.put("/api/inventory/{item_id}")
def update_inventory(item_id: int, item: InventoryItem):
    if not item.image_url or not item.image_url.strip():
        item.image_url = "/static/logo.png"
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute(
            "UPDATE inventory SET name=?, price=?, image_url=?, category=?, stock=? WHERE id=?",
            (item.name, item.price, item.image_url, item.category, item.stock, item_id)
        )
        db.commit()
    return {"status": "success", "message": "Item updated"}

@app.delete("/api/inventory/{item_id}")
def delete_inventory(item_id: int):
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
        db.commit()
    return {"status": "success", "message": "Item deleted from inventory"}

# ─── Categories ───────────────────────────────────────────────────────────────

@app.get("/api/categories")
def get_categories():
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM categories ORDER BY name")
        categories = [dict(row) for row in cursor.fetchall()]
    return {"categories": categories}

@app.post("/api/categories")
def add_category(cat: CategoryCreate):
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("INSERT INTO categories (name) VALUES (?)", (cat.name,))
            db.commit()
        return {"status": "success", "message": "Category added"}
    except Exception:
        raise HTTPException(status_code=400, detail="Category already exists")

@app.put("/api/categories/{cat_id}")
def update_category(cat_id: int, cat: CategoryCreate):
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute("UPDATE categories SET name=? WHERE id=?", (cat.name, cat_id))
        db.commit()
    return {"status": "success", "message": "Category updated"}

@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int):
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute(
            "UPDATE inventory SET category='General' WHERE category=(SELECT name FROM categories WHERE id=?)",
            (cat_id,)
        )
        cursor.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        db.commit()
    return {"status": "success", "message": "Category deleted"}

# ─── Bluetooth ────────────────────────────────────────────────────────────────

@app.get("/api/bluetooth-images")
def get_bluetooth_images():
    images = []
    if os.path.exists(bluetooth_inbox_dir):
        for file in os.listdir(bluetooth_inbox_dir):
            if file.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                images.append(file)
    images.sort(key=lambda x: os.path.getmtime(resource_path('__data__', 'data', 'bluetooth_inbox', x)), reverse=True)
    return {"images": images}

@app.get("/api/bluetooth-images/preview/{filename}")
def preview_bluetooth_image(filename: str):
    safe_name = os.path.basename(filename)
    file_path = resource_path('__data__', 'data', 'bluetooth_inbox', safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Image not found in inbox")
    return FileResponse(file_path)

@app.post("/api/bluetooth-images/claim")
def claim_bluetooth_image(req: ClaimImageRequest):
    src_path = resource_path('__data__', 'data', 'bluetooth_inbox', req.filename)
    if not os.path.exists(src_path):
        raise HTTPException(status_code=404, detail="Image not found in inbox")
    dest_path = resource_path('__data__', 'data', 'images', req.filename)
    base, ext = os.path.splitext(req.filename)
    counter = 1
    while os.path.exists(dest_path):
        new_filename = f"{base}_{counter}{ext}"
        dest_path = resource_path('__data__', 'data', 'images', new_filename)
        req.filename = new_filename
        counter += 1
    shutil.move(src_path, dest_path)
    return {"status": "success", "image_url": f"/static/images/{req.filename}"}

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}

def _get_bt_receive_folder() -> str:
    home = os.path.expanduser("~")
    candidates = []
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        path, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        winreg.CloseKey(key)
        path = os.path.expandvars(path)
        if path and os.path.isdir(path):
            candidates.append(path)
    except Exception:
        pass
    candidates.append(os.path.join(home, "Downloads"))
    candidates.append(os.path.join(home, "Documents"))
    for folder in candidates:
        if os.path.isdir(folder):
            return folder
    return home

def _copy_new_bt_images():
    bt_folder = _get_bt_receive_folder()
    copied = []
    if not os.path.isdir(bt_folder):
        return copied
    existing = set(os.listdir(bluetooth_inbox_dir))
    for fname in os.listdir(bt_folder):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        if fname in existing:
            continue
        src = os.path.join(bt_folder, fname)
        dst = resource_path('__data__', 'data', 'bluetooth_inbox', fname)
        try:
            shutil.copy2(src, dst)
            copied.append(fname)
        except Exception as e:
            print(f"[BT-INBOX] Could not copy {fname}: {e}")
    return copied

@app.post("/api/bluetooth-receive")
def open_bluetooth_receive():
    import subprocess
    try:
        subprocess.Popen(["fsquirt.exe", "/receive"])
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="fsquirt.exe not found — is Bluetooth enabled on this PC?")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "success", "message": "Bluetooth receive wizard opened"}

@app.post("/api/bluetooth-sync")
def sync_bluetooth_inbox():
    copied = _copy_new_bt_images()
    return {
        "status": "success",
        "copied": copied,
        "count": len(copied),
        "bt_folder": _get_bt_receive_folder()
    }

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}

@app.post("/api/bluetooth-inbox/add")
async def add_to_inbox(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
    safe_name = os.path.basename(file.filename)
    dest = resource_path('__data__', 'data', 'bluetooth_inbox', safe_name)
    base, ex = os.path.splitext(safe_name)
    counter = 1
    while os.path.exists(dest):
        dest = resource_path('__data__', 'data', 'bluetooth_inbox', f"{base}_{counter}{ex}")
        counter += 1
    contents = await file.read()
    with open(dest, 'wb') as f:
        f.write(contents)
    return {"status": "success", "filename": os.path.basename(dest)}

@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = resource_path('__data__', 'data', 'images', unique_name)
    contents = await file.read()
    with open(dest_path, 'wb') as f:
        f.write(contents)
    return {"status": "success", "image_url": f"/static/images/{unique_name}"}

# ─── Exit ─────────────────────────────────────────────────────────────────────

@app.post("/api/exit")
def exit_app():
    import threading
    def _shutdown():
        import time
        time.sleep(0.3)
        import os, signal
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_shutdown, daemon=True).start()
    return {"status": "bye"}

# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import multiprocessing
    import threading
    multiprocessing.freeze_support()

    PORT = 8000
    URL  = f"http://localhost:{PORT}"

    try:
        import webview

        def _run_server():
            uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

        server_thread = threading.Thread(target=_run_server, daemon=True)
        server_thread.start()

        import time
        time.sleep(1.2)

        window = webview.create_window(
            title="FreshMarket POS",
            url=URL,
            fullscreen=True,
            min_size=(900, 600),
            resizable=True,
        )
        webview.start(debug=False)

    except ImportError:
        import webbrowser
        print(f"[INFO] pywebview not found. Opening in browser at {URL}")
        threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
        uvicorn.run(app, host="0.0.0.0", port=PORT)
