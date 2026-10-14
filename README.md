# Log Grabber

Single-exe Windows diagnostics collector for the repair bench. Combines the
Gadget Centre log grabber, the evtx→Markdown converter, and an automatic
failure-summary PDF into one portable file.

**Ship `dist/LogGrabber.exe` alone** — the collector script, parser and PDF
engine are bundled inside.

## Use

1. Copy `LogGrabber.exe` anywhere (USB stick, bench machine).
2. Double-click it → click **Yes** on UAC (logs/hives need admin).
3. Pick the Windows volume — the GUI lists every drive containing a Windows
   install; the running OS is marked `(this Windows)`.
4. Click **Start**. Progress mirrors the collector steps, then Markdown
   conversion and PDF generation run automatically.
5. Click **Create ZIP** → `LogGrabber-<stamp>.zip` lands next to the exe.

## What's collected (per run, into `cases\<stamp>\`)

| Output | Contents |
|---|---|
| `evtx\` | 16 diagnostic event channels (System, Application, Storport, WHEA, Kernel-Power thermal, Kernel-Dump, CodeIntegrity, Ntfs, PnP, Boot, WU, LiveDump, HardwareEvents, DiagPerf, ProcPower) |
| `md\` | every `.evtx` converted to Markdown tables — ready to feed an AI |
| `report.pdf` | GC-branded auto report: machine summary, ranked likely causes, bugcheck codes, failure-indicator table, dump list |
| `diagnostics.json` | raw facts (counts, samples, SMART, hives data) — input for a deeper AI write-up |
| `hives\` | SYSTEM, SOFTWARE, SAM, SECURITY (+ DEFAULT offline) |
| `minidumps\` | up to 10 most recent `.dmp` files (MEMORY.DMP size noted, not copied) |
| `wer\`, `wer_queue\` | WER report archives |
| `collect.log`, `progress.txt` | run log + step heartbeat |

Works on **offline customer drives** (direct file copy + `reg load`) and on
the **live running OS** (`wevtutil` export + live registry + `reg save`)
automatically — whichever volume you pick.

## PDF contents

Deterministic, no AI needed: counts per indicator (Event 41 bugcheck codes,
Storport 549/557 stalls, Kernel-Power 86 thermal trips, WHEA errors, volmgr
161 dump failures, 6008 dirty shutdowns, disk/Ntfs/PnP/CodeIntegrity errors…)
each mapped to a likely cause and a clear/watch/act status. Encodes shop
knowledge e.g. the Kingston SNV3 failure pattern. `md\` and
`diagnostics.json` stay in the zip so a fuller AI-written report can be
generated later.

## Notes

- Zip timestamps are clamped to 1980–2107 (bogus future mtimes otherwise
  kill the archive).
- The ZIP contains customer data and credential-bearing hives — treat it
  accordingly.
- Unsigned self-elevating exes get SmartScreen/AV flags; sign if you have a
  cert (command in `build.cmd`).

## Build / develop

```
py -m pip install pyinstaller evtx reportlab
build.cmd
```

Files: `log_grabber.py` (GUI/pipeline), `collector.ps1` (collector, embedded),
`evtx_to_md.py` (converter module), `report_pdf.py` (PDF module),
`build.cmd`. Dev: `set LOGGRABBER_NOELEVATE=1` to skip UAC when testing the
GUI unelevated. `LogGrabber.exe --version` prints the version.
