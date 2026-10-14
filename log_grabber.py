"""Log Grabber - single-exe Windows diagnostics collector for the bench.

Flow: pick the target Windows volume -> collect logs/hives/dumps via the
embedded collector.ps1 (elevated) -> convert .evtx to .md for AI ingestion ->
write a GC-branded failure-summary PDF -> zip the bundle on demand.

Output goes to <exe dir>/cases/<stamp>/ ; the zip lands next to the exe.

Build: build.cmd (PyInstaller --onefile, embeds collector.ps1)
Dev:   set LOGGRABBER_NOELEVATE=1 to skip the UAC self-elevation.
"""

import ctypes
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import evtx_to_md
import report_pdf

__version__ = "1.0.0"
TITLE = f"Log Grabber v{__version__}"
NAVY = "#102a43"
NAVY_MID = "#243b53"
ACCENT = "#0f7b6c"

# GUI bar: 14 collector steps + 3 post steps (md, pdf, done)
GUI_STEPS = 17
MD_MAX_BYTES = 20_000_000


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def elevate() -> bool:
    """Relaunch this exe elevated. Returns True if the UAC launch was sent."""
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv[1:]) or None, None, 1
    )
    return rc > 32


def bundle_dir() -> Path:
    # PyInstaller --onefile extracts --add-data files here.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_windows_volumes():
    """Drive letters containing a Windows install, live volume flagged."""
    vols = []
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.isdir(root):
            continue
        if os.path.isdir(os.path.join(root, "Windows", "System32", "winevt", "Logs")) or \
           os.path.isdir(os.path.join(root, "Windows", "System32", "config")):
            vols.append((f"{letter}:", letter + ":" == sysdrive))
    return vols


def read_progress(path: Path):
    """Parse collector progress.txt -> dict or None."""
    try:
        out = {}
        for line in path.read_text(encoding="ascii", errors="replace").splitlines():
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
        return out or None
    except OSError:
        return None


def zip_case(case_dir: Path, zip_path: Path) -> int:
    """Zip the case dir; returns count of files skipped.

    ZIP stores mtimes in DOS format (1980..2107) - clamp bogus dates so a
    single weird file can't abort the archive.
    """
    skipped = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for p in sorted(case_dir.rglob("*")):
            if not p.is_file():
                continue
            arc = str(p.relative_to(case_dir.parent))
            try:
                zi = zipfile.ZipInfo.from_file(p, arcname=arc, strict_timestamps=False)
                zi.compress_type = zipfile.ZIP_DEFLATED
                dt = zi.date_time
                zi.date_time = (min(max(dt[0], 1980), 2107), *dt[1:])
                with open(p, "rb") as src, z.open(zi, "w") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            except Exception as e:
                skipped += 1
                print(f"[LogGrabber] skipped {arc}: {e}")
    return skipped


class LogGrabberApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.case_dir = None
        self.zip_path = None
        self.proc = None          # set only after the collector subprocess exits
        self.worker = None
        self.collector_done = False
        self._build_ui()
        self._refresh_volumes()

    # ---------- UI ----------

    def _build_ui(self):
        r = self.root
        r.title(TITLE)
        r.geometry("640x520")
        r.minsize(560, 460)
        r.configure(bg=NAVY)

        tk.Label(r, text="Gadget Centre - Log Grabber", bg=NAVY, fg="white",
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(r, text="Collects logs, hives and crash dumps, then writes "
                         "Markdown (for AI) and a failure-summary PDF.",
                 bg=NAVY, fg="#9fb3c8", font=("Segoe UI", 9)).pack(anchor="w", padx=16)

        row = tk.Frame(r, bg=NAVY)
        row.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(row, text="Windows volume:", bg=NAVY, fg="white",
                 font=("Segoe UI", 10)).pack(side="left")
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(row, textvariable=self.drive_var,
                                        state="readonly", width=26)
        self.drive_combo.pack(side="left", padx=8)
        ttk.Button(row, text="Refresh", width=8,
                   command=self._refresh_volumes).pack(side="left", padx=4)
        self.start_btn = ttk.Button(row, text="Start", width=10,
                                    command=self.start)
        self.start_btn.pack(side="left", padx=10)

        self.status_var = tk.StringVar(value="Pick the customer Windows volume.")
        tk.Label(r, textvariable=self.status_var, bg=NAVY, fg="white",
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(8, 2))

        self.bar = ttk.Progressbar(r, maximum=GUI_STEPS)
        self.bar.pack(fill="x", padx=16, pady=4)

        self.log = tk.Listbox(r, bg=NAVY_MID, fg="white", font=("Consolas", 9),
                              activestyle="none", highlightthickness=0, bd=0)
        self.log.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        btns = tk.Frame(r, bg=NAVY)
        btns.pack(fill="x", padx=16, pady=(0, 14))
        self.open_btn = ttk.Button(btns, text="Open output folder",
                                   command=self._open_output, state="disabled")
        self.open_btn.pack(side="left")
        self.zip_btn = ttk.Button(btns, text="Create ZIP",
                                  command=self._make_zip, state="disabled")
        self.zip_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Close", command=r.destroy).pack(side="right")

    def _logline(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{stamp}] {text}")
        self.log.see("end")

    def _set_status(self, text, step=None):
        self.status_var.set(text)
        if step is not None:
            self.bar["value"] = min(step, GUI_STEPS)

    def _refresh_volumes(self):
        vols = find_windows_volumes()
        items = [f"{d}  (this Windows)" if live else d for d, live in vols]
        self.drive_combo["values"] = items
        # Prefer D: if present, else the live volume, else first.
        pick = next((i for i in items if i.startswith("D:")), None) or \
               next((i for i in items if "this Windows" in i), None) or \
               (items[0] if items else "")
        self.drive_var.set(pick)
        if not items:
            self.status_var.set("No Windows volumes found - check the drive is attached.")

    # ---------- run ----------

    def start(self):
        sel = self.drive_var.get().strip()[:2]
        if not re.match(r"^[A-Za-z]:$", sel):
            messagebox.showerror(TITLE, "Pick a drive letter first.")
            return
        self.start_btn["state"] = "disabled"
        self.drive_combo["state"] = "disabled"
        self.zip_btn["state"] = "disabled"
        self.collector_done = False

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.case_dir = exe_dir() / "cases" / stamp
        self.case_dir.mkdir(parents=True, exist_ok=True)

        ps1 = bundle_dir() / "collector.ps1"
        ps_exe = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        cmd = [str(ps_exe), "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(ps1), "-WindowsRoot", sel, "-OutDir", str(self.case_dir), "-NoUi"]
        self._logline(f"collector -> {sel} into {self.case_dir.name}")
        self._set_status("Collecting...", 1)
        self.worker = threading.Thread(target=self._run_pipeline, args=(cmd,), daemon=True)
        self.worker.start()
        self.root.after(300, self._poll_progress)

    def _run_pipeline(self, cmd):
        """Worker thread: collector subprocess, then md/pdf post-steps."""
        try:
            self.proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            self.root.after(0, self._fail, f"collector could not start: {e}")
            return
        self.collector_done = True
        if (self.case_dir / "FAILED.txt").exists() and not (self.case_dir / "DONE.txt").exists():
            self.root.after(0, self._fail, "collector failed - see collect.log in the output folder")
            return
        if not (self.case_dir / "DONE.txt").exists():
            self.root.after(0, self._fail,
                            f"collector exited (code {self.proc.returncode}) without finishing")
            return

        self.root.after(0, self._set_status, "Converting event logs to Markdown...", 15)
        md_dir = self.case_dir / "md"
        md_dir.mkdir(exist_ok=True)
        evtx_dir = self.case_dir / "evtx"
        n_files = n_events = 0
        if evtx_dir.is_dir():
            for p in sorted(evtx_dir.glob("*.evtx")):
                try:
                    stats = evtx_to_md.convert_file(
                        str(p), str(md_dir), p.stem + ".md", None, MD_MAX_BYTES
                    )
                    n_files += 1
                    n_events += stats.get("events") or 0
                except Exception as e:
                    self.root.after(0, self._logline, f"md convert failed {p.name}: {e}")
        self.root.after(0, self._logline, f"markdown: {n_events} events across {n_files} logs")

        self.root.after(0, self._set_status, "Writing PDF report...", 16)
        try:
            diag_path = self.case_dir / "diagnostics.json"
            diag = json.loads(diag_path.read_text(encoding="utf-8-sig"))
            report_pdf.generate_pdf(diag, self.case_dir / "report.pdf", __version__)
            self.root.after(0, self._logline, "report.pdf written")
        except Exception as e:
            self.root.after(0, self._logline, f"PDF failed: {e}")

        self.root.after(0, self._finish_ok)

    def _poll_progress(self):
        if self.collector_done:
            return  # worker thread drives the post-steps now
        prog = self.case_dir and read_progress(self.case_dir / "progress.txt")
        if prog:
            try:
                step = int(prog.get("step", 0))
            except ValueError:
                step = 0
            self._set_status(f"{step}/{prog.get('total', '?')}  {prog.get('text', '')}", step)
            if prog.get("status") == "failed":
                self._fail(prog.get("text", "collector failed"))
                return
        self.root.after(400, self._poll_progress)

    def _finish_ok(self):
        self._set_status("Done - open the folder or create the zip.", GUI_STEPS)
        self._logline("collection complete")
        self.open_btn["state"] = "normal"
        self.zip_btn["state"] = "normal"
        self.start_btn["state"] = "disabled"

    def _fail(self, msg):
        self._set_status(f"FAILED: {msg}")
        self._logline(f"FAILED: {msg}")
        self.open_btn["state"] = "normal"
        self.zip_btn["state"] = "normal"  # still let them zip partial output
        self.start_btn["state"] = "normal"
        self.drive_combo["state"] = "readonly"

    # ---------- actions ----------

    def _open_output(self):
        target = self.zip_path if self.zip_path else self.case_dir
        if target and target.exists():
            os.startfile(target if target.is_dir() else target.parent)

    def _make_zip(self):
        if not self.case_dir or not self.case_dir.exists():
            return
        self.zip_btn["state"] = "disabled"
        self._set_status("Zipping bundle...")
        stamp = self.case_dir.name
        zip_path = exe_dir() / f"LogGrabber-{stamp}.zip"

        def work():
            try:
                skipped = zip_case(self.case_dir, zip_path)
                self.zip_path = zip_path
                msg = f"zip written: {zip_path.name}"
                if skipped:
                    msg += f" ({skipped} files skipped)"
                self.root.after(0, self._logline, msg)
                self.root.after(0, self._set_status,
                                f"Done - {zip_path} ready to send.")
            except Exception as e:
                self.root.after(0, self._set_status, f"ZIP FAILED: {e}")
                self.root.after(0, lambda: self.zip_btn.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"Log Grabber {__version__}")
        return 0

    if not is_admin() and not os.environ.get("LOGGRABBER_NOELEVATE"):
        if elevate():
            return 0
        # Elevation refused - still offer the GUI? No: collection needs admin.
        ctypes.windll.user32.MessageBoxW(
            0, "Log Grabber needs administrator rights to read Windows logs "
               "and hives.\nNothing was collected.", TITLE, 0x10)
        return 1

    root = tk.Tk()
    ico = bundle_dir() / "icon.ico"
    if ico.exists():
        root.iconbitmap(str(ico))
    LogGrabberApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
