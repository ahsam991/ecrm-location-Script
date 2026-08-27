"""Location_Automation_full - one-file glass UI for the Retailer Location
Migration pipeline.

- Dependency gate: checks pandas, psycopg2, PySide6 on startup; if any are
  missing it ASKS (Tk prompt) then installs them before continuing.
- Glassmorphism UI (PySide6 translucent window + rounded glass panels).
- Wires in the pipeline backend (config.py + migration_common.py) in a worker
  thread with live logs and a stage bar chart.
- "Graphify"-style context memory persists config + run summaries + notes to
  context_memory.json and auto-reloads, so you burn fewer tokens re-entering
  context.

Usage:  python Location_Automation_full.py
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MEMORY_FILE = SCRIPT_DIR / "context_memory.json"

REQUIRED = [("pandas", "pandas"), ("psycopg2", "psycopg2-binary"), ("PySide6", "PySide6")]

STAGE_ORDER = ["point", "key", "route", "cluster", "outlet"]
STAGE_SHORT = {"point": "Point", "key": "Keys", "route": "Route",
               "cluster": "Cluster", "outlet": "Outlet"}


# --------------------------------------------------------------------------- #
# Dependency gate (stdlib only, so it works even before PySide6 is present)
# --------------------------------------------------------------------------- #
def _import_name(pip: str) -> str:
    if pip == "psycopg2-binary":
        return "psycopg2"
    return pip.replace("-", "_")


def _missing() -> list[str]:
    out = []
    for mod, pip in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            out.append(pip)
    return out


def _pip(package: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", "--progress-bar", "off", package]
    ).returncode


def ensure_dependencies() -> bool:
    missing = _missing()
    if not missing:
        return True
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception:
        print("Missing libraries: " + ", ".join(missing))
        return all(_pip(p) == 0 for p in missing)
    root = tk.Tk(); root.withdraw()
    if not messagebox.askyesno(
        "Missing libraries",
        "This app requires these libraries (not installed):\n\n- "
        + "\n- ".join(missing)
        + "\n\nInstall them now?\n(No = exit.)",
    ):
        root.destroy(); return False
    root.deiconify(); root.title("Installing libraries")
    root.geometry("540x220")
    tk.Label(root, text="Installing required libraries...", font=("Segoe UI", 11)).pack(pady=18)
    bar = ttk.Progressbar(root, length=480, maximum=len(missing)); bar.pack(pady=8)
    lab = tk.Label(root, text=""); lab.pack(); root.update()
    failed = False
    for i, pkg in enumerate(missing):
        lab["text"] = f"[{i+1}/{len(missing)}] Installing {pkg} ..."
        root.update()
        failed |= _pip(pkg) != 0
        bar["value"] = i + 1; root.update()
    root.destroy()
    return not failed


# --------------------------------------------------------------------------- #
# Context memory ("Graphify"-style) - pure stdlib, usable from anywhere
# --------------------------------------------------------------------------- #
class ContextMemory:
    def __init__(self, path: Path = MEMORY_FILE):
        self.path = Path(path)
        self.data: dict = {"nodes": [], "edges": [], "notes": "", "runs": [], "updated": ""}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update(raw)
        except Exception:
            pass

    def save(self) -> None:
        self.data["updated"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")

    def add_node(self, node_id: str, node_type: str, label: str, props: dict) -> None:
        nodes = self.data.setdefault("nodes", [])
        for n in nodes:
            if n["id"] == node_id:
                n.update({"type": node_type, "label": label, "props": props}); return
        nodes.append({"id": node_id, "type": node_type, "label": label, "props": props})

    def add_edge(self, source: str, target: str, rel: str) -> None:
        edges = self.data.setdefault("edges", [])
        for e in edges:
            if (e["source"], e["target"], e["rel"]) == (source, target, rel):
                return
        edges.append({"source": source, "target": target, "rel": rel})

    def remember_run(self, stats: dict, context: dict) -> int:
        self.data.setdefault("runs", []).append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "stats": stats, "context": context,
        })
        self.data["runs"] = self.data["runs"][-20:]
        self.save()
        return len(self.data["runs"])

    def summary(self) -> str:
        nodes = self.data.get("nodes", [])
        runs = self.data.get("runs", [])
        return (f"{len(nodes)} saved node(s), {len(runs)} saved run(s).\n"
                f"Last updated: {self.data.get('updated', 'never')}")


# --------------------------------------------------------------------------- #
# The glass GUI (imported only after the dependency gate resolves)
# --------------------------------------------------------------------------- #
def run_app() -> int:
    from PySide6.QtCore import Qt, QThread, QObject, Signal, QPoint
    from PySide6.QtGui import (QPainter, QColor, QLinearGradient, QPen,
                               QPainterPath, QFont)
    from PySide6.QtWidgets import (
        QApplication, QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout,
        QPushButton, QCheckBox, QLineEdit, QTextEdit, QStackedWidget, QProgressBar,
        QGraphicsDropShadowEffect, QFileDialog, QMessageBox, QSizeGrip, QScrollArea,
    )

    class Brace(QObject):
        sig = Signal(str)

    bridge = Brace()

    class EmitterHandler(logging.Handler):
        def emit(self, record):
            try:
                bridge.sig.emit(self.format(record))
            except Exception:
                pass

    handler = EmitterHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                           datefmt="%H:%M:%S"))

    class GlassPanel(QFrame):
        def __init__(self, radius=18, parent=None):
            super().__init__(parent)
            self.radius = radius
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(26); eff.setOffset(0, 6); eff.setColor(QColor(0, 0, 0, 90))
            self.setGraphicsEffect(eff)

        def paintEvent(self, ev):
            p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), self.radius, self.radius)
            p.fillPath(path, QColor(255, 255, 255, 22))
            p.setPen(QPen(QColor(255, 255, 255, 70), 1)); p.drawPath(path); p.end()

    class GradWindow(QWidget):
        def paintEvent(self, ev):
            p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
            g = QLinearGradient(0, 0, self.width(), self.height())
            g.setColorAt(0.0, QColor(18, 22, 38)); g.setColorAt(0.55, QColor(30, 34, 56))
            g.setColorAt(1.0, QColor(12, 14, 26)); p.fillRect(self.rect(), g)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(120, 160, 255, 26)); p.drawEllipse(self.width()*0.62, -60, 340, 340)
            p.setBrush(QColor(90, 220, 200, 18)); p.drawEllipse(-90, self.height()*0.55, 320, 320)
            p.end()

    class BarChart(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.stats = {}
            self.setMinimumHeight(300)

        def set_stats(self, stats):
            self.stats = stats or {}; self.update()

        def paintEvent(self, ev):
            p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            pl, pr, pt, pb = 90, 24, 54, 62
            pw, ph = max(w-pl-pr, 40), max(h-pt-pb, 40); base = pt + ph
            p.setFont(QFont("Segoe UI", 12, QFont.Bold))
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(0, 16, w, 30, Qt.AlignHCenter, "Matched vs Missing per Stage")
            p.setFont(QFont("Segoe UI", 10))
            if not self.stats:
                p.setPen(QColor(200, 200, 200, 180))
                inner = QPainterPath(); inner.addRoundedRect(pl, pt, pw, ph, 12, 12)
                p.setPen(QPen(QColor(255,255,255,40),1)); p.drawPath(inner)
                p.drawText(pl, pt, pw, ph, Qt.AlignCenter, "Run the pipeline to draw the chart.")
                return
            stages = [s for s in STAGE_ORDER if s in self.stats]
            maxv = max([int(self.stats[s][k]) for s in stages for k in ("filled", "missing")] or [1])
            maxv = max(maxv, 1)
            gw, bw, gap = pw/len(stages), pw/len(stages)*0.28, pw/len(stages)*0.10
            for i, s in enumerate(stages):
                cx = pl + gw*i + gw/2
                filled = int(self.stats[s].get("filled", 0)); missing = int(self.stats[s].get("missing", 0))
                fh, mh = ph*filled/maxv, ph*missing/maxv
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(88, 210, 150, 225)); p.drawRoundedRect(int(cx-bw-gap/2), int(base-fh), int(bw), int(fh), 4, 4)
                p.setBrush(QColor(230, 90, 120, 225)); p.drawRoundedRect(int(cx+gap/2), int(base-mh), int(bw), int(mh), 4, 4)
                p.setFont(QFont("Segoe UI", 8, QFont.Bold)); p.setPen(QColor(255, 255, 255, 235))
                if filled: p.drawText(int(cx-bw-gap/2), max(base-fh-16, 4), int(bw), 16, Qt.AlignHCenter, str(filled))
                if missing: p.drawText(int(cx+gap/2), max(base-mh-16, 4), int(bw), 16, Qt.AlignHCenter, str(missing))
                p.setFont(QFont("Segoe UI", 10)); p.setPen(QColor(255, 255, 255, 235))
                p.drawText(int(cx-gw/2), base+10, int(gw), 22, Qt.AlignHCenter, STAGE_SHORT[s])
            p.setPen(QColor(200, 200, 200, 170))
            p.drawLine(int(pl), int(pt), int(pl), int(base)); p.drawLine(int(pl), int(base), int(pl+pw), int(base))
            for t in range(6):
                val = maxv*t/5; y = int(base-ph*t/5)
                p.drawText(0, y-7, int(pl-8), 14, Qt.AlignRight, f"{val:,.0f}")
            p.end()

    class Worker(QObject):
        log_line = Signal(str)
        done = Signal(dict, dict)     # stats, context
        okay = Signal(dict)
        failed = Signal(str)

        def __init__(self, mk):
            super().__init__(); self.mk = mk

        def run(self):
            import pandas as pd
            import migration_common as mc
            from config import load_config
            cfg, opts, stages = self.mk["cfg"], self.mk["opts"], self.mk["stages"]
            try:
                mc.setup_logging()
                if handler not in logging.getLogger("migration").handlers:
                    logging.getLogger("migration").addHandler(handler)
                if opts.write_db:
                    opts.conn = mc.connect(cfg, dry_run=False)
                    self.log_line.emit(f"Connected to {cfg.table}")
                df = pd.read_csv(cfg.new_path, dtype=str)
                self.log_line.emit(f"Loaded {len(df)} rows from {cfg.new_path}")
                mc.validate_inputs(df, cfg, opts)
                self.log_line.emit("Inputs validated (columns + outlet-code uniqueness).")
                for stage in stages:
                    self.log_line.emit(f"===== Stage: {stage} =====")
                    fn = {"point": mc.stage_points, "key": mc.stage_keys,
                          "route": mc.stage_route, "cluster": mc.stage_cluster,
                          "outlet": mc.stage_outlet}[stage]
                    df = fn(df, cfg, opts)
                    s = opts.stats.get(stage, {})
                    self.log_line.emit(f"  {stage}: filled={s.get('filled','?')} missing={s.get('missing','?')}")
                if not opts.dry_run:
                    rec = mc.reconcile(df, opts)
                    if rec:
                        self.log_line.emit(f"Reconcile warnings: {rec} row(s) (review).")
                if opts.write_csv and not opts.dry_run:
                    df.to_csv(cfg.output_path, index=False)
                    self.log_line.emit(f"Saved output -> {cfg.output_path}")
                if not opts.dry_run:
                    mc.verify(df, opts)
                    self.log_line.emit("Verification PASSED: all four id columns populated.")
                if opts.conn is not None:
                    opts.conn.commit(); opts.conn.close()
                    self.log_line.emit("DB transaction committed and connection closed.")
                self.done.emit(opts.stats, self.mk["context"])
            except Exception as exc:
                try:
                    if opts.conn is not None and not opts.dry_run:
                        opts.conn.rollback(); opts.conn.close()
                except Exception:
                    pass
                self.failed.emit(f"{exc}")

    class MainWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.resize(1120, 760)
            self.memory = ContextMemory()
            self.stats = {}
            self.cfg = self._load_default_cfg()
            self._drag = None
            self._build()
            bridge.sig.connect(self.append_log)
            self.append_log(self.memory.summary())

        def _load_default_cfg(self):
            from config import load_config
            try:
                return load_config()
            except Exception:
                from config import Config
                return Config()

        def _btn(self, text, primary=False):
            b = QPushButton(text); b.setCursor(Qt.PointingHandCursor); b.setMinimumHeight(40)
            bg = "rgba(120,160,255,120)" if primary else "rgba(255,255,255,26)"
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:#e8ecff;border:1px solid rgba(255,255,255,70);"
                f"border-radius:12px;font:14px 'Segoe UI';padding:8px 16px;}}"
                f"QPushButton:hover{{background:rgba(160,190,255,90);}}"
                f"QPushButton:disabled{{color:#889;}}")
            return b

        def _lbl(self, text, size=12, color="#c8d3ff", bold=False):
            w = QLabel(text)
            w.setStyleSheet(f"color:{color};font:{size}px 'Segoe UI';" + ("font-weight:600;" if bold else ""))
            return w

        def _build(self):
            root = GradWindow(self)
            outer = QVBoxLayout(root); outer.setContentsMargins(24, 16, 24, 18)
            tr = QHBoxLayout()
            ti = QLabel("◆  Location Automation — RDS Migration")
            ti.setStyleSheet("color:#eef2ff;font:15px 'Segoe UI';font-weight:600;")
            tr.addWidget(ti); tr.addStretch(1)
            for txt, fn in (("—", self.showMinimized), ("✕", self.close)):
                b = QPushButton(txt); b.setCursor(Qt.PointingHandCursor); b.setFixedSize(32, 30)
                b.setStyleSheet("QPushButton{background:rgba(255,255,255,30);border:none;color:#e8ecff;"
                                "border-radius:8px;}QPushButton:hover{background:rgba(230,90,120,180);}")
                b.clicked.connect(fn); tr.addWidget(b)
            outer.addLayout(tr)

            body = QHBoxLayout(); body.setSpacing(16)
            side = GlassPanel(); side.setFixedWidth(214)
            sv = QVBoxLayout(side); sv.setContentsMargins(14, 18, 14, 18)
            self.nav = []
            for key, lbl in (("setup", "Setup"), ("run", "Run"), ("visual", "Visuals"),
                             ("memory", "Memory"), ("log", "Log")):
                b = self._btn(lbl); b.clicked.connect(lambda c=False, k=key: self.switch(k))
                sv.addWidget(b); self.nav.append((key, b))
            sv.addStretch(1)
            body.addWidget(side)

            self.stack = QStackedWidget()
            self._page_setup(); self._page_run(); self._page_visual()
            self._page_memory(); self._page_log()
            body.addWidget(self.stack, 1)
            outer.addLayout(body, 1)
            outer.addWidget(QSizeGrip(self))
            lo = QVBoxLayout(self); lo.setContentsMargins(0, 0, 0, 0); lo.addWidget(root)
            self.switch("run")

        def _page_setup(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.setSpacing(10)
            lay.addWidget(self._lbl("Setup — files & database", 16, "#eef2ff", True))
            self.i_old = QLineEdit(); self.i_new = QLineEdit(); self.i_out = QLineEdit()
            self.i_save = QLineEdit(); self.i_table = QLineEdit()
            for w, v in ((self.i_old, self.cfg.old_file), (self.i_new, self.cfg.new_file),
                         (self.i_out, self.cfg.output_file), (self.i_save, self.cfg.save_dir),
                         (self.i_table, self.cfg.table)):
                w.setText(v)
            grid = QGridLayout()
            for i, (lab, w) in enumerate((("Old mapped file", self.i_old),
                                          ("New fresh extract", self.i_new),
                                          ("Output file", self.i_out),
                                          ("Reports dir", self.i_save),
                                          ("Table", self.i_table))):
                grid.addWidget(self._lbl(lab), i, 0); grid.addWidget(w, i, 1)
            grid.setColumnStretch(1, 1); lay.addLayout(grid)
            b = self._btn("Browse..."); b.clicked.connect(self._browse); lay.addWidget(b, 0, Qt.AlignLeft)

            lay.addWidget(self._lbl("PostgreSQL (used only when 'Write to DB' is on)", 12, "#eef2ff", True))
            self.i_host, self.i_port, self.i_name, self.i_user, self.i_pwd = (
                QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit())
            self.i_host.setText(self.cfg.db_host or "localhost")
            self.i_port.setText(str(self.cfg.db_port or 5432))
            self.i_name.setText(self.cfg.db_name); self.i_user.setText(self.cfg.db_user)
            self.i_pwd.setText(self.cfg.db_password); self.i_pwd.setEchoMode(QLineEdit.Password)
            gd = QGridLayout()
            for i, (lab, w) in enumerate((("Host", self.i_host), ("Port", self.i_port),
                                          ("Database", self.i_name), ("User", self.i_user),
                                          ("Password", self.i_pwd))):
                gd.addWidget(self._lbl(lab), i, 0); gd.addWidget(w, i, 1)
            lay.addLayout(gd)
            hb = QHBoxLayout()
            hb.addWidget(self._btn("Save config.json", True)); hb.addWidget(self._btn("Load config.json"))
            lay.addLayout(hb); lay.addStretch(1)
            self.stack.addWidget(panel)

        def _page_run(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.addWidget(self._lbl("Run — pipeline stages", 16, "#eef2ff", True))
            row = QHBoxLayout()
            self.stage_vars = {}
            for s in STAGE_ORDER:
                c = QCheckBox(STAGE_SHORT[s]); c.setChecked(True); c.setStyleSheet(
                    "QCheckBox{color:#e8ecff;font:13px 'Segoe UI'; spacing:6px;}")
                self.stage_vars[s] = c; row.addWidget(c)
            lay.addLayout(row)
            opts = QHBoxLayout()
            self.c_dry = QCheckBox("Dry run (no writes)"); self.c_write = QCheckBox("Write to DB")
            for c in (self.c_dry, self.c_write):
                c.setChecked(c is self.c_dry); c.setStyleSheet("QCheckBox{color:#e8ecff;font:13px 'Segoe UI';}")
            opts.addWidget(self.c_dry); opts.addWidget(self.c_write); opts.addStretch(1)
            lay.addLayout(opts)
            self.btn_run = self._btn("Run pipeline", True); self.btn_stop = self._btn("Stop")
            self.btn_stop.setEnabled(False)
            self.btn_run.clicked.connect(self.start_run); self.btn_stop.clicked.connect(self.stop_run)
            hb = QHBoxLayout(); hb.addWidget(self.btn_run); hb.addWidget(self.btn_stop); hb.addStretch(1)
            lay.addLayout(hb)
            self.run_progress = QProgressBar(); self.run_progress.setRange(0, 100)
            self.run_progress.setFixedHeight(10); self.run_progress.setTextVisible(False)
            self.run_progress.setStyleSheet("QProgressBar{background:rgba(255,255,255,30);border:none;"
                                            "border-radius:5px;}QProgressBar::chunk{background:#5bd1a0;border-radius:5px;}")
            lay.addWidget(self.run_progress)
            self.run_status_label = self._lbl("Status: ready", 12, "#a9b2ff")
            lay.addWidget(self.run_status_label)
            lay.addStretch(1)
            self.stack.addWidget(panel)

        def _page_visual(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.addWidget(self._lbl("Visuals", 16, "#eef2ff", True))
            self.chart = BarChart(); lay.addWidget(self.chart, 1)
            self.stack.addWidget(panel)

        def _page_memory(self):
            panel = GlassPanel(); s = QScrollArea(); s.setWidgetResizable(True)
            body = QWidget(); lay = QVBoxLayout(body); lay.setContentsMargins(4, 4, 4, 4)
            lay.addWidget(self._lbl("Context Memory (Graphify-style)", 16, "#eef2ff", True))
            self.mem_summary = self._lbl(self.memory.summary())
            lay.addWidget(self.mem_summary)
            lay.addWidget(self._lbl("Notes (persisted):", 12, "#c8d3ff"))
            self.mem_notes = QTextEdit(); self.mem_notes.setPlainText(self.memory.data.get("notes", ""))
            self.mem_notes.setMinimumHeight(180)
            self.mem_notes.setStyleSheet("background:rgba(255,255,255,26);color:#eef2ff;border:none;"
                                         "border-radius:12px;font:12px 'Segoe UI';padding:10px;")
            lay.addWidget(self.mem_notes)
            lay.addWidget(self._lbl("Saved runs:", 12, "#c8d3ff"))
            runs = self.memory.data.get("runs", [])
            self.runs_list = QTextEdit(); self.runs_list.setReadOnly(True)
            self.runs_list.setPlainText("\n".join(json.dumps(r.get("stats", {})) for r in runs[-10:]))
            self.runs_list.setMaximumHeight(150)
            self.runs_list.setStyleSheet("background:rgba(255,255,255,26);color:#dfe6ff;border:none;"
                                         "border-radius:12px;font:11px 'Consolas';padding:10px;")
            lay.addWidget(self.runs_list)
            hb = QHBoxLayout(); hb.addWidget(self._btn("Save memory", True))
            hb.addWidget(self._btn("Clear runs"))
            lay.addLayout(hb); lay.addStretch(1)
            s.setWidget(body); panel; outer = QVBoxLayout(panel); outer.setContentsMargins(22, 22, 22, 22)
            outer.addWidget(s); self.stack.addWidget(panel)

        def _page_log(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.addWidget(self._lbl("Log", 16, "#eef2ff", True))
            self.log_text = QTextEdit(); self.log_text.setReadOnly(True)
            self.log_text.setStyleSheet("background:rgba(0,0,0,70);color:#e4ecff;border:none;"
                                        "border-radius:12px;font:11px 'Consolas';padding:10px;")
            lay.addWidget(self.log_text, 1)
            lay.addLayout(self._clear_log_row()); self.stack.addWidget(panel)

        def _clear_log_row(self):
            h = QHBoxLayout(); h.addStretch(1)
            b = self._btn("Clear log"); b.clicked.connect(lambda: self.log_text.clear()); h.addWidget(b)
            return h

        def _browse(self):
            import tkinter as tk
            from tkinter import filedialog, messagebox
            root = tk.Tk(); root.withdraw()
            p = filedialog.askopenfilename()
            root.destroy()
            if p:
                self.i_new.setText(p); self.i_out.setText(p.rsplit(".", 1)[0] + "_update.csv")

        def _collect_cfg(self):
            from config import Config
            c = Config()
            c.old_file, c.new_file = self.i_old.text(), self.i_new.text()
            c.output_file, c.save_dir = self.i_out.text(), self.i_save.text()
            c.table = self.i_table.text()
            c.db_host = self.i_host.text(); c.db_port = int(self.i_port.text() or 5432)
            c.db_name = self.i_name.text(); c.db_user = self.i_user.text(); c.db_password = self.i_pwd.text()
            return c

        def start_run(self):
            from config import CONFIG_FILE
            import migration_common as mc
            stages = [s for s in STAGE_ORDER if self.stage_vars[s].isChecked()]
            if not stages:
                QMessageBox.warning(self, "Run", "Select at least one stage.")
                return
            dry = self.c_dry.isChecked(); write = self.c_write.isChecked() and not dry
            self._save_cfg_json()
            cfg = self._collect_cfg()
            opts = mc.Opts(dry_run=dry, write_db=write, write_csv=not dry,
                           conn=None, save_dir=cfg.save_path)
            self.memory.add_node("cfg", "config", "Configuration", str(cfg.__dict__))
            context = {k: getattr(cfg, k) for k in ("old_file", "new_file", "output_file", "table")}
            mk = {"cfg": cfg, "opts": opts, "stages": stages, "context": context}
            self.thread = QThread(); self.worker = Worker(mk)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.done.connect(self._on_done)
            self.worker.failed.connect(self._on_fail)
            self.worker.okay.connect(lambda c: self._set_status(f"Verification passed."))
            self.worker.log_line.connect(self.append_log)
            self.worker.done.connect(self.thread.quit)
            self.worker.failed.connect(self.thread.quit)
            self.btn_run.setEnabled(False); self.btn_stop.setEnabled(True)
            self.run_progress.setRange(0, 0); self._set_status("Running...")
            self.thread.start()

        def _save_cfg_json(self):
            try:
                from config import CONFIG_FILE
                d = {"old_file": self.i_old.text(), "new_file": self.i_new.text(),
                     "output_file": self.i_out.text(), "save_dir": self.i_save.text(),
                     "table": self.i_table.text(), "db_host": self.i_host.text(),
                     "db_port": int(self.i_port.text() or 5432), "db_name": self.i_name.text(),
                     "db_user": self.i_user.text(), "db_password": self.i_pwd.text()}
                CONFIG_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
            except Exception:
                pass

        def _on_done(self, stats, context):
            self.stats = stats; self.chart.set_stats(stats)
            self.memory.remember_run(stats, context)
            self.memory.data["notes"] = self.mem_notes.toPlainText()
            self.memory.save()
            self.runs_list.setPlainText("\n".join(json.dumps(r.get("stats", {}))
                                                  for r in self.memory.data.get("runs", [])[-10:]))
            self.mem_summary.setText(self.memory.summary())
            self.run_progress.setRange(0, 100); self.run_progress.setValue(100)
            self._set_status("Done."); self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)

        def _on_fail(self, msg):
            self.append_log("ERROR: " + msg)
            QMessageBox.critical(self, "Error", msg)
            self.run_progress.setRange(0, 100); self.run_progress.setValue(0)
            self._set_status(f"Error: {msg}"); self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)

        def stop_run(self):
            if self.thread is not None and self.thread.isRunning():
                self.thread.requestInterruption()
                self._set_status("Stopped.")

        def _set_status(self, text):
            if getattr(self, "run_status_label", None) and self.run_status_label is not None:
                self.run_status_label.setText("Status: " + text)

        def switch(self, key):
            idx = ["setup", "run", "visual", "memory", "log"].index(key)
            self.stack.setCurrentIndex(idx)
            for k, b in self.nav:
                b.setStyleSheet("QPushButton{background:rgba(255,255,255,26);color:#e8ecff;"
                                "border:1px solid rgba(255,255,255,70);border-radius:12px;"
                                "font:14px 'Segoe UI';padding:8px 16px;}"
                                "QPushButton:hover{background:rgba(160,190,255,90);}"
                                "QPushButton:disabled{color:#889;}")

        def append_log(self, text):
            self.log_text.append(text)

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._drag = e.globalPosition().toPoint()
            super().mousePressEvent(e)

        def mouseMoveEvent(self, e):
            if self._drag is not None and e.buttons() & Qt.LeftButton:
                self.move(self.pos() + e.globalPosition().toPoint() - self._drag)
                self._drag = e.globalPosition().toPoint()
            super().mouseMoveEvent(e)

        def mouseReleaseEvent(self, e):
            self._drag = None; super().mouseReleaseEvent(e)

    app = QApplication(sys.argv)
    app.setApplicationName("Location_Automation_full")
    win = MainWindow()
    win.show()
    return app.exec()


def main() -> int:
    if not ensure_dependencies():
        print("Required libraries are missing and were not installed. Exit.")
        return 1
    print("Dependencies OK. Launching glass UI ...")
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
