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
| `make_icon.py` | Generates `icon.ico`/`icon.png` (log cross-section). `py make_icon.py`. |

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
- Hive copies: live SYSTEM/SOFTWARE/SAM/SECURITY via `reg save`, DEFAULT via
  `HKU\.DEFAULT` (`HKLM\DEFAULT` is not a live mount point — the same
  `config\DEFAULT` hive is mounted under HKU). Offline → direct file copy.
- **Failure isolation**: every external call (`reg.exe`, `wevtutil`,
  `robocopy`) and WMI/CIM query is wrapped — a blocked/failed step logs
  `FAIL <step>` to `collect.log` and collection continues. Real-world case:
  Defender/policy blocked `reg.exe` for the SAM save on one machine — an
  unwrapped call would have aborted the whole run. Keep it that way.
  The GUI's Create ZIP also stays available on collector failure so partial
  bundles ship.
- Zip timestamps clamped 1980–2107 (`strict_timestamps=False` + clamp).
- `CREATE_NO_WINDOW` on the powershell spawn — without it a console window
  appears under the windowed exe.
- Icon: `make_icon.py` draws the log cross-section → `icon.ico` (exe icon via
  `--icon`, plus bundled as data so the tkinter window uses it too).

## Rebuild

    py -m pip install pyinstaller evtx reportlab
    build.cmd   # or the PyInstaller line inside it (--icon icon.ico
                # --add-data "collector.ps1;." --add-data "icon.ico;.")

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

## Storport interpretation (report_pdf.py)

Storport 549/557 covers **all** storage — internal NVMe/SATA **and** USB
sticks, UASP enclosures, card readers, virtual disks. A raw total is not a
boot-drive verdict:

- `_storport_breakdown(diag)` splits `events.storport.samples` into
  `internal` vs `external` counters by `MiniportName` / `BusType` /
  `BootDevice` (BootDevice=true always counts internal).
- External miniports: `uaspstor`, `usbstor`, `usbccid`, `sdstor`, `vhdmp`,
  `vsmpp`; USB bus types 7/18. USB/UASP stalls are bridge/power-management
  noise — the PDF reports them as a green note, never as drive evidence.
- Only internal stalls feed the "Boot storage failing" finding (red at ≥3)
  and the suspect-device name comes from the top internal ProductId.
- Samples are capped at 80 (`truncated`), so the split reflects the newest
  events — device names still surface correctly.
- Payload schemas vary by Windows build: 549 events carry
  MiniportName/VendorId/ProductId/BootDevice; 576-type records carry
  AdapterGuid/AdapterHardwareId instead and have no ProductId — code must
  tolerate missing fields.

Repo: github.com/gcnewbief/Log-Grabber · releases carry `LogGrabber.exe`.
Version lives in `log_grabber.py __version__`; bump on behavior changes and
re-tag (`v1.0.0` = initial release, `1.0.1` = storport internal/external
split, `1.0.2` = fix build_findings crash on first real bugcheck —
bad_checks dict had lost the (count, hex) tuples).

## Future work (next update)

- **Refine storport USB/external reporting**: break external stalls down
  further (per-device table, event ID split 549 vs 557, first/last seen).
  Rationale from the shop: many stalls on one USB device usually means a
  dodgy stick/enclosure, not a machine fault — the report should say which
  specific external device is implicated so it can be discounted or
  blamed. Internal drives remain the main focus.
- Reconcile the automated PDF with technician review (report is a baseline;
  AI/tech write-up decides the verdict).
