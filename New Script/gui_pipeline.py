"""Single-file GUI for the Retailer Location Migration pipeline.

Dependencies: python (tkinter), pandas, psycopg2 (only when writing to the DB).
Run `python Install_Library.py` first if a dependency is missing.

Usage:
    python gui_pipeline.py
"""
import logging
import os
import queue
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pandas as pd

import migration_common as mc
from config import Config, load_config

STAGE_ORDER = ["point", "key", "route", "cluster", "outlet"]
STAGE_TITLES = {
    "point": "1. Point id",
    "key": "2. Identifier keys",
    "route": "3. Route id",
    "cluster": "4. Cluster id",
    "outlet": "5. Outlet id",
}
STAGE_LABELS = {
    "point": "Point",
    "key": "Keys",
    "route": "Route",
    "cluster": "Cluster",
    "outlet": "Outlet",
}


class QueueLogHandler(logging.Handler):
    """Forward log records to a thread-safe queue the GUI drains."""

    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


class PipelineGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Retailer Location Migration — Pipeline")
        self.geometry("980x720")

        self.queue = queue.Queue()
        self.running = False
        self.stats = {}
        self._build_ui()

        self._handler = QueueLogHandler(self.queue)
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                                     datefmt="%H:%M:%S"))
        logging.getLogger("migration").addHandler(self._handler)

        self.after(100, self._drain_logs)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # --- Files & DB tab -------------------------------------------------
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="Files & Database")

        frm = ttk.LabelFrame(tab1, text="Input / output files")
        frm.pack(fill="x", padx=8, pady=6)
        self.var_old = tk.StringVar(value="data/retailers_02_05_2026_update.csv")
        self.var_new = tk.StringVar(value="data/retailers_05_07_2026.csv")
        self.var_out = tk.StringVar(value="data/retailers_05_07_2026_update.csv")
        self.var_save = tk.StringVar(value="finded_data")
        self.var_table = tk.StringVar(value="ecrm.locations")
        rows = [
            ("Old mapped file", self.var_old),
            ("New fresh extract", self.var_new),
            ("Output file", self.var_out),
            ("Reports / save dir", self.var_save),
            ("Destination table", self.var_table),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(frm, textvariable=var, width=60).grid(row=i, column=1, sticky="we", **pad)
            if "file" in label:
                ttk.Button(frm, text="Browse",
                           command=lambda v=var: self._browse(v)).grid(row=i, column=2, **pad)
        frm.columnconfigure(1, weight=1)

        db = ttk.LabelFrame(tab1, text="PostgreSQL (used only when 'Write to DB' is on)")
        db.pack(fill="x", padx=8, pady=6)
        self.var_host = tk.StringVar(value="localhost")
        self.var_port = tk.StringVar(value="5432")
        self.var_name = tk.StringVar(value="")
        self.var_user = tk.StringVar(value="")
        self.var_pwd = tk.StringVar(value="")
        dbrows = [("Host", self.var_host), ("Port", self.var_port), ("Database", self.var_name),
                  ("User", self.var_user), ("Password", self.var_pwd)]
        for i, (label, var) in enumerate(dbrows):
            ttk.Label(db, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", **pad)
            ttk.Entry(db, textvariable=var, width=34,
                      show="*" if label == "Password" else "").grid(
                row=i // 2, column=(i % 2) * 2 + 1, sticky="we", **pad)

        btns = ttk.Frame(tab1)
        btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(btns, text="Save config.json", command=self.save_config).pack(side="left", **pad)
        ttk.Button(btns, text="Load config.json", command=self.load_config).pack(side="left", **pad)

        # --- Run tab --------------------------------------------------------
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="Run")
        run = ttk.LabelFrame(tab2, text="Pipeline stages")
        run.pack(fill="x", padx=8, pady=6)
        self.stage_vars = {}
        for col, stage in enumerate(STAGE_ORDER):
            v = tk.BooleanVar(value=True)
            self.stage_vars[stage] = v
            ttk.Checkbutton(run, text=STAGE_TITLES[stage], variable=v).grid(
                row=0, column=col, sticky="w", **pad)

        opt = ttk.LabelFrame(tab2, text="Options")
        opt.pack(fill="x", padx=8, pady=6)
        self.var_dry = tk.BooleanVar(value=True)
        self.var_write = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Dry run (no DB/CSV writes)", variable=self.var_dry).pack(
            side="left", **pad)
        ttk.Checkbutton(opt, text="Write to DB (insert new locations)",
                        variable=self.var_write).pack(side="left", **pad)

        ctrl = ttk.Frame(tab2)
        ctrl.pack(fill="x", padx=8, pady=6)
        self.btn_run = ttk.Button(ctrl, text="Run pipeline", command=self.run_pipeline)
        self.btn_run.pack(side="left", **pad)
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self.stop_pipeline, state="disabled")
        self.btn_stop.pack(side="left", **pad)
        self.btn_verify = ttk.Button(ctrl, text="Verify only", command=self.verify_only)
        self.btn_verify.pack(side="left", **pad)

        # --- Log tab --------------------------------------------------------
        tab3 = ttk.Frame(nb)
        nb.add(tab3, text="Log")
        self.log_text = scrolledtext.ScrolledText(tab3, wrap="word", state="disabled",
                                                  font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

        # --- Visuals tab ----------------------------------------------------
        tab4 = ttk.Frame(nb)
        nb.add(tab4, text="Visuals")
        self.canvas = tk.Canvas(tab4, bg="white")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda e: self._redraw_chart())
        self.after(250, self._redraw_chart)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom")

    def _browse(self, var):
        path = filedialog.askopenfilename() if "Old" in var.get() or "New" in var.get() \
            else filedialog.askdirectory()
        if path:
            var.set(path)

    # ------------------------------------------------------------- visuals --
    @staticmethod
    def _fmt_int(v):
        return f"{int(v):,}"

    def _redraw_chart(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 900
        h = c.winfo_height() or 400
        if w < 40 or h < 40:
            return
        pad_l, pad_r, pad_t, pad_b = 100, 20, 60, 70
        plot_w = max(w - pad_l - pad_r, 50)
        plot_h = max(h - pad_t - pad_b, 50)
        base_y = pad_t + plot_h

        c.create_text(w / 2, 18, text="Matched vs Missing per Stage",
                      font=("Arial", 12, "bold"))
        title = ["Run the pipeline to see the chart.",
                 "Switch off 'Dry run' + tick 'Write to DB' to insert new rows."]
        for i, line in enumerate(title):
            c.create_text(w / 2, 40 + i * 16, text=line, fill="#888", font=("Arial", 10))

        if not self.stats:
            c.create_rectangle(pad_l, pad_t, pad_l + plot_w, base_y, outline="#ddd")
            return

        stages = [s for s in STAGE_ORDER if s in self.stats]
        max_val = 1
        for s in stages:
            max_val = max(max_val, int(self.stats[s]["filled"]), int(self.stats[s]["missing"]))

        group_w = plot_w / len(stages)
        bar_w = group_w * 0.28
        gap = group_w * 0.10

        for i, s in enumerate(stages):
            cx = pad_l + group_w * i + group_w / 2
            filled = int(self.stats[s]["filled"])
            missing = int(self.stats[s]["missing"])
            fh = plot_h * filled / max_val
            mh = plot_h * missing / max_val
            c.create_rectangle(cx - bar_w - gap / 2, base_y - fh, cx - gap / 2, base_y,
                               fill="#2e7d32", outline="")
            c.create_rectangle(cx + gap / 2, base_y - mh, cx + bar_w + gap / 2, base_y,
                               fill="#d32f2f", outline="")
            c.create_text(cx, base_y + 14, text=STAGE_LABELS[s], font=("Arial", 9))
            c.create_text(cx - bar_w / 2 - gap / 2, max(base_y - fh - 8, 16),
                          text=self._fmt_int(filled), fill="#2e7d32", font=("Arial", 8, "bold"))
            c.create_text(cx + bar_w / 2 + gap / 2, max(base_y - mh - 8, 16),
                          text=self._fmt_int(missing), fill="#d32f2f", font=("Arial", 8, "bold"))

        # axes
        c.create_line(pad_l, pad_t, pad_l, base_y, fill="#999")
        c.create_line(pad_l, base_y, pad_l + plot_w, base_y, fill="#999")
        for t in range(0, 6):
            val = max_val * t / 5
            y = base_y - plot_h * t / 5
            c.create_text(pad_l - 8, y, text=self._fmt_int(val), anchor="e", font=("Arial", 8))

        # legend
        lx = w - 180
        c.create_rectangle(lx, 40, lx + 16, 56, fill="#2e7d32", outline="")
        c.create_text(lx + 22, 48, text="Matched", anchor="w", font=("Arial", 9))
        c.create_rectangle(lx, 62, lx + 16, 78, fill="#d32f2f", outline="")
        c.create_text(lx + 22, 70, text="Missing", anchor="w", font=("Arial", 9))

    # ------------------------------------------------------------- config --
    def load_config(self):
        try:
            cfg = load_config()
        except Exception as exc:  # pragma: no cover
            messagebox.showerror("Config", str(exc))
            return
        self.var_old.set(cfg.old_file)
        self.var_new.set(cfg.new_file)
        self.var_out.set(cfg.output_file)
        self.var_save.set(cfg.save_dir)
        self.var_table.set(cfg.table)
        self.var_host.set(cfg.db_host)
        self.var_port.set(str(cfg.db_port))
        self.var_name.set(cfg.db_name)
        self.var_user.set(cfg.db_user)
        messagebox.showinfo("Config", "Loaded config.json (defaults if none saved).")

    def save_config(self):
        data = {
            "old_file": self.var_old.get(), "new_file": self.var_new.get(),
            "output_file": self.var_out.get(), "save_dir": self.var_save.get(),
            "table": self.var_table.get(), "db_host": self.var_host.get(),
            "db_port": int(self.var_port.get() or 5432), "db_name": self.var_name.get(),
            "db_user": self.var_user.get(), "db_password": self.var_pwd.get(),
        }
        try:
            import json
            from config import CONFIG_FILE
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            messagebox.showinfo("Config", f"Saved -> {CONFIG_FILE}")
        except Exception as exc:
            messagebox.showerror("Config", str(exc))

    def _cfg_from_ui(self):
        cfg = Config()
        cfg.old_file = self.var_old.get()
        cfg.new_file = self.var_new.get()
        cfg.output_file = self.var_out.get()
        cfg.save_dir = self.var_save.get()
        cfg.table = self.var_table.get()
        cfg.db_host = self.var_host.get()
        cfg.db_port = int(self.var_port.get() or 5432)
        cfg.db_name = self.var_name.get()
        cfg.db_user = self.var_user.get()
        cfg.db_password = self.var_pwd.get()
        return cfg

    def _opts_from_ui(self, cfg):
        dry = self.var_dry.get()
        write = not dry and self.var_write.get()
        return mc.Opts(dry_run=dry, write_db=write, write_csv=not dry,
                       conn=None, save_dir=cfg.save_path)

    # -------------------------------------------------------------- action --
    def _log_line(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_logs(self):
        try:
            while True:
                self._log_line(self.queue.get_nowait())
        except queue.Empty:
            pass
        if self.running:
            self.after(100, self._drain_logs)

    def verify_only(self):
        try:
            cfg = self._cfg_from_ui()
            opts = self._opts_from_ui(cfg)
            df = pd.read_csv(cfg.output_path, dtype=str)
            mc.setup_logging()
            counts = mc.verify(df, opts)
            self._log_line(f"VERIFY: {counts}")
            messagebox.showinfo("Verify", "All four id columns fully populated." if sum(counts.values()) == 0
                                else f"Missing ids: {counts}")
        except Exception as exc:
            messagebox.showerror("Verify", str(exc))

    def run_pipeline(self):
        if self.running:
            return
        stages = [s for s in STAGE_ORDER if self.stage_vars[s].get()]
        if not stages:
            messagebox.showwarning("Run", "Select at least one stage.")
            return
        self.running = True
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set("Running...")
        threading.Thread(target=self._worker, args=(stages,), daemon=True).start()

    def stop_pipeline(self):
        self.running = False
        self.status.set("Stopped.")
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _worker(self, stages):
        cfg = self._cfg_from_ui()
        opts = self._opts_from_ui(cfg)
        mc.setup_logging()
        try:
            if opts.write_db:
                opts.conn = mc.connect(cfg, dry_run=False)
                self._log_line(f"Connected to {cfg.table}")
            df = pd.read_csv(cfg.new_path, dtype=str)
            self._log_line(f"Loaded {len(df)} rows from {cfg.new_path}")
            for stage in stages:
                if not self.running:
                    break
                self._log_line(f"\n===== Stage: {stage} =====")
                df = {
                    "point": mc.stage_points, "key": mc.stage_keys,
                    "route": mc.stage_route, "cluster": mc.stage_cluster,
                    "outlet": mc.stage_outlet,
                }[stage](df, cfg, opts)
            mc.print_summary(opts)
            if not opts.dry_run and opts.conn is not None:
                opts.conn.commit()
                opts.conn.close()
                self._log_line("DB transaction committed and closed.")
            if not opts.dry_run and opts.write_csv:
                df.to_csv(cfg.output_path, index=False)
                self._log_line(f"Saved output -> {cfg.output_path}")
                mc.verify(df, opts)
                self._log_line("Verification passed.")
            self.status.set("Done.")
        except Exception as exc:
            if opts.conn is not None and not opts.dry_run:
                try:
                    opts.conn.rollback()
                except Exception:
                    pass
            self._log_line("\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            self.status.set(f"Error: {exc}")
        finally:
            self.running = False
            self.stats = dict(opts.stats)
            self.after(0, self._ui_done)

    def _ui_done(self):
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._redraw_chart()


if __name__ == "__main__":
    app = PipelineGUI()
    app.mainloop()
