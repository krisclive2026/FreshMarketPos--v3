"""
keygen.py  –  FreshMarket POS  License Key Generator  (GUI)
============================================================
VENDOR USE ONLY  –  do NOT distribute with the application.

Requirements:  Python 3.8+   (tkinter is part of the standard library)
Run:           python keygen.py
"""

import hmac
import hashlib
import struct
import time
import base64
import os
import sys
import datetime
import tkinter as tk
from tkinter import ttk, messagebox

# ─── MUST MATCH license_manager.py ───────────────────────────────────────────
VENDOR_SECRET = b"K9#mPx2!qL5@nR8&vT1*wZ4^jF6$bH3%cD7_yA0"

KEY_TYPE_ACTIVATE = 0x01
KEY_TYPE_RENEW    = 0x02

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# ─── Crypto helpers ───────────────────────────────────────────────────────────

def _encode_key(data: bytes) -> str:
    b32 = base64.b32encode(data).decode().rstrip("=")
    std = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    result = "".join(_ALPHABET[std.index(c)] for c in b32)
    chunks = [result[i:i+5] for i in range(0, len(result), 5)]
    return "-".join(chunks)

def parse_secret_code(code: str) -> str:
    clean = code.replace("-", "").replace(" ", "").lower()
    if len(clean) != 16:
        raise ValueError(f"Secret code must be 16 hex characters (got {len(clean)})")
    int(clean, 16)   # validate hex
    return clean

def generate_key(machine_id_hex: str, days: int, key_type: int):
    now        = int(time.time())
    expiry_ts  = now + days * 86400
    expiry_min = expiry_ts // 60

    machine_hash = bytes.fromhex(
        hashlib.sha256(machine_id_hex.encode()).hexdigest()
    )[:8]
    nonce  = os.urandom(4)
    header = (
        machine_hash
        + struct.pack(">I", expiry_min)
        + struct.pack("B", key_type)
        + nonce
    )
    sig     = hmac.new(VENDOR_SECRET, header, hashlib.sha256).digest()[:8]
    payload = header + sig
    key     = _encode_key(payload)
    expiry_date = datetime.datetime.utcfromtimestamp(expiry_ts).strftime("%d-%m-%Y")
    return key, expiry_date

# ─── Colours & fonts ──────────────────────────────────────────────────────────

BG        = "#0f172a"
CARD      = "#1e293b"
BORDER    = "#334155"
ACCENT    = "#38bdf8"
ACCENT2   = "#6366f1"
SUCCESS   = "#4ade80"
DANGER    = "#f87171"
TEXT      = "#f1f5f9"
MUTED     = "#94a3b8"
FONT_UI   = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 11)
FONT_H1   = ("Segoe UI", 14, "bold")
FONT_TINY = ("Segoe UI", 8)

# ─── GUI ──────────────────────────────────────────────────────────────────────

class KeygenApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FreshMarket POS — License Key Generator")
        self.resizable(False, True)
        self.configure(bg=BG)

        # Centre on screen — taller base height so result panel is always visible
        self.update_idletasks()
        w, h = 580, 760
        x = (self.winfo_screenwidth()  - w) // 2
        y = max(0, (self.winfo_screenheight() - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

        self._build()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        root = self

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(root, bg=CARD, pady=18)
        hdr.pack(fill="x")

        tk.Label(hdr, text="🔑", font=("Segoe UI Emoji", 28),
                 bg=CARD, fg=ACCENT).pack()
        tk.Label(hdr, text="License Key Generator",
                 font=FONT_H1, bg=CARD, fg=TEXT).pack()
        tk.Label(hdr, text="VENDOR USE ONLY  ·  Do not distribute",
                 font=FONT_TINY, bg=CARD, fg=DANGER).pack(pady=(2, 0))

        # ── Body card ─────────────────────────────────────────────────────────
        body = tk.Frame(root, bg=BG, padx=32, pady=24)
        body.pack(fill="both", expand=True)

        # Secret code input
        self._section(body, "1  ·  Customer Secret Code")
        secret_frame = tk.Frame(body, bg=CARD, bd=0, relief="flat",
                                highlightthickness=1,
                                highlightbackground=BORDER)
        secret_frame.pack(fill="x", pady=(4, 16))

        self.secret_var = tk.StringVar()
        self.secret_entry = tk.Entry(
            secret_frame,
            textvariable=self.secret_var,
            font=FONT_MONO,
            bg=CARD, fg=ACCENT,
            insertbackground=ACCENT,
            relief="flat",
            bd=10,
        )
        self.secret_entry.pack(fill="x")
        self.secret_entry.bind("<KeyRelease>", self._on_secret_type)

        tk.Label(body, text="Paste the 16-character code shown in the app's activation screen.",
                 font=FONT_TINY, bg=BG, fg=MUTED, anchor="w").pack(fill="x", pady=(0, 14))

        # Key type
        self._section(body, "2  ·  Key Type")
        type_frame = tk.Frame(body, bg=BG)
        type_frame.pack(fill="x", pady=(4, 16))

        self.key_type_var = tk.IntVar(value=KEY_TYPE_ACTIVATE)
        self._radio(type_frame, "Activation  (first-time setup)",
                    KEY_TYPE_ACTIVATE).pack(side="left", padx=(0, 24))
        self._radio(type_frame, "Renewal  (extend existing license)",
                    KEY_TYPE_RENEW).pack(side="left")

        # Validity
        self._section(body, "3  ·  Validity Period")
        days_frame = tk.Frame(body, bg=BG)
        days_frame.pack(fill="x", pady=(4, 16))

        self.days_var = tk.IntVar(value=365)
        presets = [("30 days", 30), ("90 days", 90),
                   ("180 days", 180), ("1 year", 365), ("2 years", 730)]
        for label, val in presets:
            btn = tk.Button(
                days_frame, text=label,
                font=("Segoe UI", 9),
                bg=CARD, fg=MUTED,
                activebackground=ACCENT2, activeforeground="#fff",
                relief="flat", bd=0,
                padx=10, pady=6, cursor="hand2",
                command=lambda v=val: self._set_days(v),
            )
            btn.pack(side="left", padx=(0, 6))

        custom_frame = tk.Frame(body, bg=BG)
        custom_frame.pack(fill="x", pady=(6, 0))
        tk.Label(custom_frame, text="Custom days:", font=FONT_UI,
                 bg=BG, fg=MUTED).pack(side="left")
        self.days_spin = tk.Spinbox(
            custom_frame,
            from_=1, to=3650,
            textvariable=self.days_var,
            width=6, font=FONT_MONO,
            bg=CARD, fg=TEXT,
            buttonbackground=CARD,
            relief="flat", bd=4,
            insertbackground=TEXT,
        )
        self.days_spin.pack(side="left", padx=(8, 0))

        # Generate button
        self.gen_btn = tk.Button(
            body,
            text="Generate Key  →",
            font=("Segoe UI", 11, "bold"),
            bg=ACCENT, fg=BG,
            activebackground="#7dd3fc", activeforeground=BG,
            relief="flat", bd=0,
            pady=12, cursor="hand2",
            command=self._generate,
        )
        self.gen_btn.pack(fill="x", pady=(20, 0))

        # ── Result card ───────────────────────────────────────────────────────
        self.result_frame = tk.Frame(root, bg=CARD, padx=32, pady=20)
        # not packed until generation succeeds

        tk.Label(self.result_frame, text="Generated Key",
                 font=FONT_BOLD, bg=CARD, fg=MUTED).pack(anchor="w")

        self.result_key_var = tk.StringVar()
        key_row = tk.Frame(self.result_frame, bg=CARD)
        key_row.pack(fill="x", pady=(6, 0))

        copy_btn = tk.Button(
            key_row, text="⎘ Copy",
            font=("Segoe UI", 9, "bold"),
            bg="#164e35", fg=SUCCESS,
            activebackground="#15803d", activeforeground="#fff",
            relief="flat", bd=0, padx=10, pady=6, cursor="hand2",
            command=self._copy_key,
        )
        copy_btn.pack(side="right")

        self.result_key_lbl = tk.Label(
            key_row,
            textvariable=self.result_key_var,
            font=("Consolas", 12, "bold"),
            bg=CARD, fg=SUCCESS,
            wraplength=420, justify="left",
        )
        self.result_key_lbl.pack(side="left", fill="x", expand=True)

        self.result_meta_var = tk.StringVar()
        tk.Label(self.result_frame,
                 textvariable=self.result_meta_var,
                 font=FONT_TINY, bg=CARD, fg=MUTED).pack(anchor="w", pady=(8, 0))

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(root, textvariable=self.status_var,
                 font=FONT_TINY, bg="#0a1020", fg=MUTED,
                 anchor="w", padx=16, pady=6).pack(fill="x", side="bottom")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _section(self, parent, text):
        tk.Label(parent, text=text, font=FONT_BOLD,
                 bg=BG, fg=MUTED, anchor="w").pack(fill="x")

    def _radio(self, parent, text, value):
        return tk.Radiobutton(
            parent, text=text,
            variable=self.key_type_var, value=value,
            font=FONT_UI, bg=BG, fg=TEXT,
            selectcolor=BG,
            activebackground=BG, activeforeground=ACCENT,
            cursor="hand2",
        )

    def _set_days(self, val):
        self.days_var.set(val)

    def _on_secret_type(self, _event=None):
        # Auto-insert dashes every 4 chars
        raw = self.secret_var.get().replace("-", "").replace(" ", "").upper()
        raw = raw[:16]
        formatted = "-".join(raw[i:i+4] for i in range(0, len(raw), 4))
        self.secret_var.set(formatted)
        self.secret_entry.icursor(tk.END)

    def _generate(self):
        # Validate secret code
        raw_code = self.secret_var.get().strip()
        try:
            machine_id = parse_secret_code(raw_code)
        except Exception as e:
            self._flash_status(f"✗  {e}", error=True)
            messagebox.showerror("Invalid Secret Code",
                                 str(e), parent=self)
            return

        # Validate days
        try:
            days = int(self.days_var.get())
            if days <= 0:
                raise ValueError("Must be > 0")
        except Exception:
            self._flash_status("✗  Enter a valid number of days.", error=True)
            messagebox.showerror("Invalid Days",
                                 "Please enter a positive number of days.", parent=self)
            return

        key_type = self.key_type_var.get()
        type_label = "ACTIVATION" if key_type == KEY_TYPE_ACTIVATE else "RENEWAL"

        try:
            key, expiry_date = generate_key(machine_id, days, key_type)
        except Exception as e:
            self._flash_status(f"✗  Generation failed: {e}", error=True)
            return

        self.result_key_var.set(key)
        self.result_meta_var.set(
            f"Type: {type_label}   ·   Valid until: {expiry_date}   ·   ({days} days)"
        )

        self.result_frame.pack(fill="x", before=self.nametowidget(
            self.winfo_children()[-1]  # status bar
        ))
        # Auto-expand window height to ensure result panel is fully visible
        self.update_idletasks()
        needed = self.winfo_reqheight()
        current = self.winfo_height()
        if needed > current:
            self.geometry(f"{self.winfo_width()}x{needed}")
        self._flash_status(f"✔  {type_label} key generated — valid until {expiry_date}")

    def _copy_key(self):
        key = self.result_key_var.get()
        if not key:
            return
        self.clipboard_clear()
        self.clipboard_append(key)
        self._flash_status("✔  Key copied to clipboard!")

    def _flash_status(self, msg, error=False):
        self.status_var.set(msg)
        color = DANGER if error else SUCCESS
        bar = self.winfo_children()[-1]
        bar.configure(fg=color)
        self.after(4000, lambda: (
            self.status_var.set("Ready"),
            bar.configure(fg=MUTED)
        ))


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = KeygenApp()
    app.mainloop()
