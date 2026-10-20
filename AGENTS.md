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
- Minidumps: 10 most recent copied; MEMORY.DMP size only — but every dump's
  first 8KB (`header`/`memory_dmp_header` hex in diagnostics) is captured so
  the report can decode the bugcheck without the file.
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

Same as the old toolkit (`schema_version 1.0`) plus `flags.live_os_collect`,
`dumps.minidumps[].copied` / `.header`, `dumps.memory_dmp_header`,
`counts.storport_health` and `events.storage_health`. Keys: `live`,
`customer_os`, `memory`, `storage`, `battery`, `crash_control`, `dumps`,
`counts`, `flags`, `events`, `wer`, `cbs_error_lines`.

## Storport interpretation (report_pdf.py)

The collector samples storport Operational IDs 500/501/549/550/557/561 —
they are **not all "stalls"**, and a raw total is not a boot-drive verdict:

- **Event 549** = "first instance of the error seen during this time period" —
  fires once per unique (opcode, SrbStatus, ScsiStatus, sense) signature per
  device. Most are ~1ms completions where firmware rejected a command it
  doesn't implement (SenseKey 5 ILLEGAL REQUEST on READ CAPACITY(16),
  mode/log pages; SrbStatus 6/8/33 on LUN probes) — normal chatter on EVERY
  machine, not errors that matter.
- **Event 557** = "IO took more than Threshold_ms (30s) to complete" — but the
  threshold applies to `RequestDuration_ms + WaitDuration_ms`. WaitDuration
  keeps counting while an IO is parked in the storport queue (disk in D3,
  port paused, machine asleep/hibernated) — healthy laptops log 557s with
  waits of hours after every resume. Only a large **RequestDuration** means
  the device itself was slow.
- **Events 500/501/550/561** = request timeouts and device/bus/hierarchical
  resets — the real distress signals.
- Duration sentinel: `4294967295` (0xFFFFFFFF) and any value ≥30min
  (`STORPORT_ABSURD_MS`) is a sleep/sentinel artifact, never a measured time.

`_storport_classify(diag)` buckets each sample into `stall` (557 with
RequestDuration ≥ `STORPORT_SLOW_MS` = 5s), `error` (549/reset events with
busy/timeout/reset/parity SrbStatus or medium/hardware/aborted-command
sense), `inconclusive` (error signature but absurd timing, or unrecognised
status), `queued` (557 wait-dominated = sleep artifact) and `benign`
(probe/unsupported-command rejects) — each split `internal` vs `external` by
`MiniportName`/`BusType`/`BootDevice` (BootDevice=true ⇒ internal; external
miniports `uaspstor usbstor usbccid sdstor vhdmp vsmpp`, USB bus types 7/18;
0=SATA, 17=NVMe are internal).

Verdict rules: ≥3 real internal stall/error events → red "Boot storage
failing"; 1-2 or ≥3 inconclusive → orange "Boot storage suspect"; queue-wait
artifacts, benign rejects and external/USB events get explanatory green
notes. The same internal/external naming caveat applies: `ProductId` may be
absent on some payload versions — tolerate missing fields.

## Disk / provider filtering

`disk_7_11_51_153` previously matched the same event IDs from ANY provider —
Kernel-Boot 153 (VBS info) and Kernel-General 11 (TxR registry mounts)
inflated it. The collector XPath now filters `Provider[@Name="disk"]`, and
`report_pdf._disk_error_samples` recounts provider=="disk" samples for old
bundles. Real disk-provider events name their target ("Disk N" /
"HarddiskN") — surfaced in the finding; Disk 1+ is usually the USB device.

## Storage health / SMART (events.storage_health)

The **Storport/Health** channel (`...Storport%4Health.evtx`, copied by the
collector) carries periodic per-device SMART snapshots — the best no-extra-
tools source of real drive data (Get-StorageReliabilityCounter returns null
for most NVMe fields):

- **512** = NVMe Health Information Log — `NvmeHealthLog` is the raw
  512-byte NVMe SMART page (hex string). `_decode_nvme_health` pulls
  critical warning bits, spare/threshold %, % used, power cycles/hours,
  unsafe shutdowns, **media errors**, error-log entries.
- **510** = SMART statistics — `SmartAttributes` blob = standard attribute
  table (2-byte header + 30×12-byte records). `_decode_smart_attrs` ->
  {id: (value, worst, raw)}; `SMART_BAD_ATTRS` lists the ones that matter
  (5/187/188/196/197/198/199/201; 187/197/198 = unrecoverable → red).
- **511** = ATA Device Statistics — per-page blobs of 8-byte entries
  [stat#, flags, value(6 LE)]. Only whitelisted stats in `ATA_STATS`
  surface (reported uncorrectable, temp, hw resets, CRC, endurance used).
- 537–548 = "miniport logged a health event" — collected, counted, not
  decoded.
- Reliability-counter `read/write/flush_latency_max_ms` (ms — MS says >10s
  indicates a problem, `LATENCY_WARN_MS`) is now rendered in the SMART line
  and flagged "** SLOW **" + an orange finding — device-side corroboration
  for storport stalls.
- `_storage_health(diag)` keeps the newest decoded blob per device serial;
  `_match_health` joins it to `storage.smart` by serial then product.

## Crash dump / WHEA decoding (report_pdf.py)

- Kernel `.dmp` files (minidumps AND MEMORY.DMP) share the `PAGEDU64`/
  `PAGEDUMP` DUMP_HEADER: BugCheckCode at 0x38 (64-bit) / 0x28 (32-bit),
  four parameters at 0x40-0x58 / 0x2C-0x38, MachineImageType at 0x30.
  `_parse_dmp_header` decodes this from the captured `header` hex —
  `BUGCHECK_NAMES` maps codes → names (keep in sync with collector
  `$bugcheckNames`, which is keyed by the DECIMAL code value).
- Parsed dump bugchecks merge into `bad_checks` (deduped by name) so they
  surface as BSOD findings even when Event 41 recorded code 0. Storage-
  class names live in `STORAGE_BUGCHECKS` — they also trigger/upgrade the
  storage finding (≥2 → red on their own).
- WHEA `RawData` is a CPER record: `CPER` sig, section count at 0x0A,
  72-byte section descriptors at offset 128, SectionType GUID at
  descriptor+16 (little-endian byte form). `_whea_section_counts` maps
  these to hardware classes (PCIe / platform memory / processor…) so the
  finding names the fault type — PCIe sections point at NVMe/chipset.
- Real failing-drive case (`Potential False Positives/20261020-105724`):
  0xEF bugcheck + volmgr 161 dump-write failure + 40 WHEA PCIe records in
  a ~1s burst = drive fell off the bus mid-crash. Storport samples stayed
  noise-class — confirming the noise filtering isn't hiding real fails.

Repo: github.com/gcnewbief/Log-Grabber · releases carry `LogGrabber.exe`.
Version lives in `log_grabber.py __version__`; bump on behavior changes and
re-tag (`v1.0.0` = initial release, `1.0.1` = storport internal/external
split, `1.0.2` = fix build_findings crash on first real bugcheck —
bad_checks dict had lost the (count, hex) tuples, `1.0.3` = storport
stall-vs-noise split (557 WaitDuration sleep artifacts + 549
illegal-request rejects no longer count as drive failures), disk XPath
restricted to the disk provider, storport reset/timeout IDs
500/501/550/561 collected, Storport/Health channel collected + SMART/ATA/
NVMe health decoding in the PDF, `1.0.4` = dump-header decoding +
collection (bugcheck decoded from minidump/MEMORY.DMP headers, shown in the
Crash dumps table and merged into BSOD findings), WHEA CPER section-type
decoding, expanded + corrected bugcheck name maps, storage-class bugchecks
now count toward the storage verdict).

## Future work (next update)

- **Refine storport USB/external reporting**: break external stalls down
  further (per-device table, first/last seen timestamps). Rationale from the
  shop: many stalls on one USB device usually means a dodgy stick/enclosure,
  not a machine fault — the report should say which specific external device
  is implicated so it can be discounted or blamed. Internal drives remain
  the main focus.
- Possibly weight 549 `SRB_STATUS_BUSY`/reset statuses on the boot device
  harder once real failing-drive bundles confirm the pattern (the SNV3
  family stays suspect).
- Reconcile the automated PDF with technician review (report is a baseline;
  AI/tech write-up decides the verdict).
