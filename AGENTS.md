# Log Grabber — agent notes

Single-exe Windows diagnostics collector (bench tool). Successor merge of the
"AI report template" toolkit's collector + PythonEvtxtoMD converter + a new
deterministic failure-summary PDF.

## Layout

| File | Role |
|------|------|
| `log_grabber.py` | tkinter GUI + pipeline driver. `__version__` here. |
| `collector.ps1` | Embedded collector (offline + live modes). Written to `%_MEIPASS%` at runtime. |
| `evtx_to_md.py` | Converter module (imported, also runnable standalone). |
| `report_pdf.py` | Deterministic GC-branded PDF from `diagnostics.json`. |
| `build.cmd` | PyInstaller build → `dist\LogGrabber.exe`. |

## Architecture

- GUI self-elevates via `ShellExecuteW runas` (UAC). `LOGGRABBER_NOELEVATE=1`
  skips it for dev. `--version` prints version.
- Drive picker scans `A-Z:` for `Windows\System32\winevt\Logs` or `config\`;
  `$env:SystemDrive` flagged "(this Windows)".
- Pipeline: spawn `powershell -File collector.ps1 -WindowsRoot X: -OutDir
  cases\<stamp> -NoUi` (hidden window) → poll `progress.txt`
  (`status=/step=/total=/text=`) → `evtx_to_md.convert_file` per `.evtx` into
  `md\` → `report_pdf.generate_pdf` → done; ZIP on demand.
- Collector mode: `$isLive = ($win -eq $env:SystemDrive)`. Live → `wevtutil
  epl <channel>` (files locked, NO `/lf` — fails on some builds), direct HKLM
  reads, `reg save` hives. Offline → `Copy-Item` evtx + `reg load HKLM\OffSys/
  OffSoft`, copy raw hives.
- Minidumps: 10 most recent copied; MEMORY.DMP size only.
- Hive copies: live SYSTEM/SOFTWARE/SAM/SECURITY (HKLM\DEFAULT isn't a mount
  point — offline list adds it).
- Zip timestamps clamped 1980–2107 (`strict_timestamps=False` + clamp).
- `CREATE_NO_WINDOW` on the powershell spawn — without it a console window
  appears under the windowed exe.

## Rebuild

    py -m pip install pyinstaller evtx reportlab
    py -m PyInstaller --onefile --windowed --name LogGrabber \
        --add-data "collector.ps1;." log_grabber.py

- Parser is the Rust-based `evtx` PyPI package (`evtx.PyEvtxParser`) — not
  the pure-Python `python-evtx`.
- reportlab font registration falls back to Helvetica if Arial TTFs missing.
- Testing collector unelevated: it self-elevates unless you run the ps1
  directly and click the UAC prompt; `--NoUi` hides its own progress form.
- Never commit `cases\`, `test_run\`, `*.zip` — they contain customer data,
  SAM/SECURITY hives and dumps (all gitignored).

## diagnostics.json schema

Same as the old toolkit (`schema_version 1.0`) plus `flags.live_os_collect`
and `dumps.minidumps[].copied`. Keys: `live`, `customer_os`, `memory`,
`storage`, `battery`, `crash_control`, `dumps`, `counts`, `flags`, `events`,
`wer`, `cbs_error_lines`.
