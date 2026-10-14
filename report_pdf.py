"""report_pdf - GC-branded automatic failure summary PDF from diagnostics.json.

Deterministic: counts failure indicators from the collector's diagnostics.json
and maps them to likely causes using the shop's known-bad-part and event
pattern knowledge. No AI write-up needed; md/ + diagnostics.json remain the
source for a deeper AI pass later.

  generate_pdf(diag, out_path, version)
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import Color, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import ParagraphStyle

FONT_DIR = Path(r"C:\Windows\Fonts")
try:
    pdfmetrics.registerFont(TTFont("LS", str(FONT_DIR / "arial.ttf")))
    pdfmetrics.registerFont(TTFont("LS-Bold", str(FONT_DIR / "arialbd.ttf")))
    pdfmetrics.registerFont(TTFont("LS-Italic", str(FONT_DIR / "ariali.ttf")))
    registerFontFamily("LS", normal="LS", bold="LS-Bold", italic="LS-Italic", boldItalic="LS-Bold")
    BASE_FONT, BOLD_FONT = "LS", "LS-Bold"
except Exception:
    BASE_FONT, BOLD_FONT = "Helvetica", "Helvetica-Bold"

NAVY = Color(16 / 255, 42 / 255, 67 / 255)
NAVY_MID = Color(36 / 255, 59 / 255, 83 / 255)
TEAL = Color(15 / 255, 123 / 255, 108 / 255)
GREEN = Color(27 / 255, 127 / 255, 78 / 255)
ORANGE = Color(196 / 255, 92 / 255, 12 / 255)
RED = Color(176 / 255, 32 / 255, 32 / 255)
ROW_ALT = Color(247 / 255, 250 / 255, 252 / 255)
MUTED = Color(102 / 255, 122 / 255, 142 / 255)
BODY = Color(16 / 255, 42 / 255, 67 / 255)

TONE_COLOR = {"green": GREEN, "orange": ORANGE, "red": RED}
TONE_LABEL = {"green": "CLEAR", "orange": "WATCH", "red": "ACT"}

# Shop-known bad storage SKUs (see toolkit AGENTS.md).
KNOWN_BAD_DISKS = {
    "SNV3S2000G": "Kingston SNV3 2TB - shop-known failure pattern (runs hot, Storport stalls, mixed BSODs). Do not reuse as a boot disk.",
    "SA510": "WD Blue SA510 - known failure class with Storport BUSY/BUS_RESET. Condemn the drive.",
}

# Bugcheck name -> likely cause, for the indicator assessment column.
BUGCHECK_CAUSES = {
    "CRITICAL_PROCESS_DIED": "critical system process died - usually corrupt system files or failing boot storage",
    "UNEXPECTED_STORE_EXCEPTION": "store exception - classic wedged/failing SSD pattern",
    "IRQL_NOT_LESS_OR_EQUAL": "IRQL fault - driver or RAM suspect",
    "MEMORY_MANAGEMENT": "memory management fault - RAM suspect",
    "PAGE_FAULT_IN_NONPAGED_AREA": "bad pointer fault - RAM or driver suspect",
    "NMI_HARDWARE_FAILURE": "hardware NMI - board/RAM/CPU suspect",
    "BUGCODE_USB_DRIVER": "USB driver/device fault",
    "UNEXPECTED_KERNEL_MODE_TRAP": "kernel trap - RAM/CPU or overclock suspect",
    "CLOCK_WATCHDOG_TIMEOUT": "CPU watchdog - processor, power or cooling suspect",
    "DPC_WATCHDOG_TIMEOUT": "DPC watchdog - often storage/driver latency",
    "LOST_DELAYED_WRITE_TIMEOUT": "lost delayed write - storage suspect",
    "SETUP_FAILURE": "setup failure - install/media suspect",
    "none (hard power / hang)": "no bugcheck recorded - hard power loss, hang, or forced power-off",
    "unknown": "unrecognised bugcheck code - review event data",
}


def _gb(n):
    try:
        return f"{int(n) / (1024 ** 3):.0f} GB"
    except (TypeError, ValueError):
        return "?"


def _count(diag, key):
    return int((diag.get("counts") or {}).get(key) or 0)


# Storport miniports that are external/virtual - stalls on these are mostly
# USB bridge or power-management noise, not drive-failure evidence.
EXTERNAL_MINIPORTS = ("uaspstor", "usbstor", "usbccid", "sdstor", "vhdmp", "vsmpp")
USB_BUS_TYPES = {"7", "18"}  # 7=USB, 18=SD


def _storport_breakdown(diag):
    """Split sampled storport 549/557 events into (internal, external) device
    counters. BootDevice=true always counts as internal regardless of bus."""
    internal, external = {}, {}
    samples = ((diag.get("events") or {}).get("storport") or {}).get("samples") or []
    for s in samples:
        dd = s.get("data") or {}
        mp = (dd.get("MiniportName") or "").strip().lower()
        prod = (dd.get("ProductId") or dd.get("VendorId") or "").strip() or mp or "unknown"
        boot = (dd.get("BootDevice") or "").strip().lower() == "true"
        is_external = (not boot) and (
            mp in EXTERNAL_MINIPORTS or (dd.get("BusType") or "").strip() in USB_BUS_TYPES
        )
        (external if is_external else internal)[prod] = \
            (external if is_external else internal).get(prod, 0) + 1
    truncated = bool(((diag.get("events") or {}).get("storport") or {}).get("truncated"))
    return internal, external, truncated


def _top_devices(counter, limit=3):
    return ", ".join(f"{n} ({c})" for n, c in
                     sorted(counter.items(), key=lambda kv: -kv[1])[:limit]) or "none"


def _bugcheck_summary(diag):
    """Group Event 41 samples by bugcheck name -> (count, hex)."""
    out = {}
    for s in ((diag.get("events") or {}).get("kernel_power_41") or {}).get("samples") or []:
        name = s.get("bugcheck_name") or "unknown"
        hx = s.get("bugcheck_hex") or ""
        c, _hx = out.get(name, (0, hx))
        out[name] = (c + 1, hx)
    return out


def build_findings(diag):
    """Ranked (tone, title, detail) findings from the failure patterns."""
    findings = []
    counts = diag.get("counts") or {}
    flags = diag.get("flags") or {}
    stor = _count(diag, "storport_549_557")
    volmgr = _count(diag, "volmgr_161")
    therm = _count(diag, "thermal_86")
    whea = _count(diag, "whea_errors")
    kp41 = _count(diag, "kernel_power_41")
    code0 = _count(diag, "kernel_power_41_code0")
    e6008 = _count(diag, "event_6008")
    disk_err = _count(diag, "disk_7_11_51_153")
    bug = _bugcheck_summary(diag)
    bad_checks = {n: c for n, (c, _h) in bug.items() if n != "none (hard power / hang)"}

    bad_disk_names = []
    for d in (diag.get("storage") or {}).get("disks") or []:
        friendly = (d.get("friendly") or "")
        for sku, note in KNOWN_BAD_DISKS.items():
            if sku.lower() in friendly.lower():
                bad_disk_names.append((friendly, note))

    # 1. Storage pattern - strongest shop signal. Storport stalls are split by
    # device: USB/UASP/external stalls are bridge/power noise, only internal
    # (or BootDevice-flagged) stalls count toward a boot-drive verdict.
    internal_sp, external_sp, _sp_trunc = _storport_breakdown(diag)
    stor_internal = sum(internal_sp.values())
    stor_external = sum(external_sp.values())
    top_internal = sorted(internal_sp.items(), key=lambda kv: -kv[1])
    store_culprit = bad_disk_names[0][0] if bad_disk_names else (top_internal[0][0] if top_internal else None)
    if stor_internal or (volmgr and kp41) or bad_disk_names:
        bits = []
        if stor_internal:
            bits.append(f"{stor_internal} Storport 549/557 stalls on internal devices "
                        f"({_top_devices(internal_sp)})")
        if volmgr:
            bits.append(f"{volmgr} volmgr 161 dump-write failures")
        if "UNEXPECTED_STORE_EXCEPTION" in bad_checks or "CRITICAL_PROCESS_DIED" in bad_checks:
            bits.append("storage-class bugchecks")
        tone = "red" if (stor_internal >= 3 or volmgr or bad_disk_names) else "orange"
        detail = "; ".join(bits) or "storage suspect"
        if store_culprit:
            detail += f". Suspect device: {store_culprit}"
        findings.append((tone, "Boot storage failing", detail + ". Replace the drive, do not just reinstall Windows."))
    if stor_external and not stor_internal:
        findings.append(("green", f"Storport stalls on USB/external devices only (x{stor_external})",
                         f"Devices: {_top_devices(external_sp)}. USB/UASP bridge or power-management "
                         "noise - not evidence of a failing boot drive."))

    if bad_disk_names:
        for name, note in bad_disk_names:
            findings.append(("red", "Known-bad drive fitted", f"{name}: {note}"))

    # 2. Bugcheck classes.
    for name, (n, hx) in sorted(bad_checks.items(), key=lambda kv: -kv[1][0]):
        cause = BUGCHECK_CAUSES.get(name, "unrecognised bugcheck - review event data")
        findings.append(("red" if n >= 2 else "orange", f"BSOD {name} (x{n})",
                         f"{hx} {cause}"))
    if code0:
        findings.append(("orange", f"Hard power loss / hang (x{code0})",
                         "Event 41 with bugcheck code 0 - PSU, battery, overheating or forced power-off; not a recorded BSOD."))
    if e6008:
        findings.append(("orange", f"Unexpected shutdowns (x{e6008})",
                         "Event 6008 dirty shutdowns - power loss or hang."))

    # 3. Thermal / hardware.
    if therm:
        findings.append(("red" if therm >= 5 else "orange", f"CPU thermal trips (x{therm})",
                         "Kernel-Power 86 - CPU/package hit thermal limit. Check fan, vents, paste, blocked heatsink."))
    if whea:
        findings.append(("red", f"WHEA hardware errors (x{whea})",
                         "WHEA Errors channel - PCIe/RAM/CPU hardware fault. PCIe errors often track a dying NVMe."))
    if disk_err:
        findings.append(("orange", f"Disk error events (x{disk_err})",
                         "System disk 7/11/51/153 errors - controller, cable or drive."))

    # 4. Notes worth recording but not faults by themselves.
    if flags.get("mixed_ram"):
        vendors = ", ".join((diag.get("memory") or {}).get("vendors") or [])
        findings.append(("orange", "Mixed RAM fitted",
                         f"Modules from different vendors ({vendors}) - flag for memtest if RAM-class bugchecks persist."))
    if (diag.get("dumps") or {}).get("memory_dmp_exists") is False and flags.get("no_minidumps"):
        if kp41:
            findings.append(("orange", "No crash dumps to analyse",
                             "Bugchecks occurred but no dump files were found - dump writing may be failing."))
    if flags.get("auto_reboot_off"):
        findings.append(("green", "Auto-reboot disabled",
                         "CrashControl AutoReboot=0 - machine halts on the BSOD screen instead of restarting."))
    if _count(diag, "processor_power_37"):
        findings.append(("green", "Processor-Power 37 events present",
                         "Firmware limiting CPU speed - normal on modern laptops, not a cooling fault by itself."))
    if not findings:
        findings.append(("green", "No failure pattern found",
                         "No bugchecks, stalls, thermal trips or hardware errors in the collected logs."))
    order = {"red": 0, "orange": 1, "green": 2}
    findings.sort(key=lambda f: order[f[0]])
    return findings


def build_indicators(diag):
    """(name, count, assessment, tone) rows for the indicator table."""
    bug = _bugcheck_summary(diag)
    rows = []
    kp41 = _count(diag, "kernel_power_41")
    code0 = _count(diag, "kernel_power_41_code0")
    real_checks = kp41 - code0
    rows.append(("Kernel-Power 41 (unexpected restarts)", kp41,
                 f"{code0} hard power/hang, {real_checks} recorded bugcheck(s)",
                 "red" if real_checks else "orange" if kp41 else "green"))
    rows.append(("Event 6008 dirty shutdowns", _count(diag, "event_6008"),
                 "power loss or hang while running",
                 "orange" if _count(diag, "event_6008") else "green"))
    rows.append(("Event 1074 clean shutdowns/restarts", _count(diag, "event_1074"),
                 "user/software initiated restarts (informational)", "green"))
    rows.append(("volmgr 161 dump-write failures", _count(diag, "volmgr_161"),
                 "crash dump could not be written - disk already gone is typical",
                 "red" if _count(diag, "volmgr_161") else "green"))
    rows.append(("Kernel-Power 86 thermal trips", _count(diag, "thermal_86"),
                 "CPU/package hit thermal limit - cooling fault",
                 "red" if _count(diag, "thermal_86") >= 5 else "orange" if _count(diag, "thermal_86") else "green"))
    rows.append(("Kernel-Power 109", _count(diag, "kernel_power_109"),
                 "power transition faults", "orange" if _count(diag, "kernel_power_109") else "green"))
    rows.append(("Processor-Power 37", _count(diag, "processor_power_37"),
                 "firmware speed limiting - normal on recent laptops", "green"))
    rows.append(("Disk errors (7/11/51/153)", _count(diag, "disk_7_11_51_153"),
                 "controller/cable/drive errors", "orange" if _count(diag, "disk_7_11_51_153") else "green"))
    stor = _count(diag, "storport_549_557")
    internal_sp, external_sp, _t = _storport_breakdown(diag)
    stor_internal = sum(internal_sp.values())
    stor_external = sum(external_sp.values())
    sp_note = (f"internal: {_top_devices(internal_sp, 2)}"
               + (f"; external/USB: {_top_devices(external_sp, 2)}" if stor_external else "")
               if stor else ">30s I/O on boot disk beats SMART Healthy - suspect SSD/NVMe")
    rows.append(("Storport 549/557 I/O stalls", stor, sp_note,
                 "red" if stor_internal >= 3 else "orange" if stor else "green"))
    rows.append(("WHEA hardware errors", _count(diag, "whea_errors"),
                 "PCIe/RAM/CPU hardware fault", "red" if _count(diag, "whea_errors") else "green"))
    rows.append(("Code Integrity errors", _count(diag, "code_integrity"),
                 "unsigned/blocked code - driver or tampering",
                 "orange" if _count(diag, "code_integrity") else "green"))
    rows.append(("NTFS warnings/errors", _count(diag, "ntfs_warn_error"),
                 "filesystem/volume problems", "orange" if _count(diag, "ntfs_warn_error") else "green"))
    rows.append(("PnP errors", _count(diag, "pnp_errors"),
                 "device enumeration/driver problems", "orange" if _count(diag, "pnp_errors") else "green"))
    rows.append(("Application errors", _count(diag, "application_errors"),
                 "app-level faults (informational unless kernel pattern)", "green"))
    rows.append(("Windows Update errors", _count(diag, "windows_update_errors"),
                 "servicing problems", "green"))
    rows.append(("WER reports collected", _count(diag, "wer_reports"),
                 "Windows Error Reporting payloads in bundle", "green"))
    return rows


def _style():
    return {
        "body": ParagraphStyle("body", fontName=BASE_FONT, fontSize=8.5, leading=11, textColor=BODY),
        "cell": ParagraphStyle("cell", fontName=BASE_FONT, fontSize=8, leading=10, textColor=BODY),
        "cellb": ParagraphStyle("cellb", fontName=BOLD_FONT, fontSize=8, leading=10, textColor=BODY),
        "h2": ParagraphStyle("h2", fontName=BOLD_FONT, fontSize=11, leading=14, textColor=NAVY, spaceBefore=10, spaceAfter=4),
        "small": ParagraphStyle("small", fontName=BASE_FONT, fontSize=7.5, leading=9.5, textColor=MUTED),
        "white": ParagraphStyle("white", fontName=BOLD_FONT, fontSize=16, leading=20, textColor=white),
        "whites": ParagraphStyle("whites", fontName=BASE_FONT, fontSize=8.5, leading=11, textColor=white),
        "finding": ParagraphStyle("finding", fontName=BASE_FONT, fontSize=9, leading=12, textColor=BODY),
    }


def generate_pdf(diag: dict, out_path, version: str = "1.0.0"):
    """Render the automatic failure summary PDF. Returns out_path."""
    out_path = Path(out_path)
    st = _style()
    live = diag.get("live") or {}
    cust = diag.get("customer_os") or {}
    mem = diag.get("memory") or {}
    disks = (diag.get("storage") or {}).get("disks") or []
    smart = (diag.get("storage") or {}).get("smart") or []
    dumps = diag.get("dumps") or {}
    battery = diag.get("battery") or []

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title="REPAIR DIAGNOSTICS REPORT",
        author=f"Log Grabber {version}",
    )
    story = []

    # --- header band ---
    collected = diag.get("collected_at") or "?"
    hdr = Table(
        [[Paragraph("REPAIR DIAGNOSTICS REPORT", st["white"])],
         [Paragraph(
             f"Gadget Centre &mdash; automated failure summary &nbsp;|&nbsp; "
             f"collected {collected} &nbsp;|&nbsp; Log Grabber v{version}",
             st["whites"])]],
        colWidths=[182 * mm],
    )
    hdr.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ("LINEBELOW", (0, 1), (-1, 1), 3, TEAL),
    ]))
    story.append(hdr)

    # --- machine summary ---
    story.append(Paragraph("Machine", st["h2"]))
    disk_lines = []
    for d in disks:
        bad = any(sku.lower() in (d.get("friendly") or "").lower() for sku in KNOWN_BAD_DISKS)
        line = f"{d.get('friendly', '?')} ({d.get('media', '?')}, {d.get('bus', '?')}, {_gb(d.get('size_bytes'))}, health: {d.get('health', '?')})"
        if bad:
            line += "  ** KNOWN-BAD SKU **"
        disk_lines.append(line)
    for s in smart:
        disk_lines.append(
            f"  SMART {s.get('friendly', '?')}: wear {s.get('wear', '?')}, {s.get('power_on_hours', '?')}h on, "
            f"{s.get('temperature', '?')}C, uncorr. r/w {s.get('read_errors_uncorrected', '?')}/{s.get('write_errors_uncorrected', '?')}"
            if not s.get("error") else f"  SMART {s.get('friendly', '?')}: unreadable ({s.get('error', '?')})")

    ram_desc = f"{_gb(mem.get('total_bytes'))} in {mem.get('module_count', '?')} module(s)"
    if mem.get("mixed_vendors"):
        ram_desc += " - MIXED VENDORS: " + ", ".join(mem.get("vendors") or [])
    bat_desc = "; ".join(f"{b.get('name', '?')} ({b.get('status', '?')}, {b.get('estimated_pct', '?')}%)" for b in battery) or "none detected"
    info_rows = [
        ["Computer", f"{cust.get('computer_name') or live.get('computer_name') or '?'} "
                     f"({live.get('manufacturer', '?')} {live.get('model', '?')})"],
        ["Serial / BIOS", f"{live.get('serial', '?')} / {live.get('bios', '?')} {live.get('bios_date') or ''}"],
        ["Customer OS", f"{cust.get('product_name') or '?'} build {cust.get('current_build', '?')}.{cust.get('ubr', '?')} "
                        f"{cust.get('display_version') or ''} (installed {cust.get('install_date') or '?'})"],
        ["Bench OS", f"{live.get('os', '?')} on {live.get('system_drive', '?')} (target volume: {diag.get('windows_root', '?')})"],
        ["CPU", f"{live.get('cpu', '?')} ({live.get('cores', '?')}c/{live.get('logical', '?')}t)"],
        ["RAM", ram_desc],
        ["Disks", "<br/>".join(disk_lines) if disk_lines else "none detected"],
        ["Battery", bat_desc],
    ]
    t = Table([[Paragraph(f"<b>{k}</b>", st["cell"]), Paragraph(v, st["cell"])] for k, v in info_rows],
              colWidths=[32 * mm, 150 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [white, ROW_ALT]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, Color(0.85, 0.88, 0.9)),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    # --- most likely causes ---
    story.append(Paragraph("Most likely causes (ranked)", st["h2"]))
    findings = build_findings(diag)
    frows = []
    for tone, title, detail in findings:
        chip = Table([[""]], colWidths=[4 * mm], rowHeights=[4 * mm])
        chip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TONE_COLOR[tone])]))
        frows.append([chip, Paragraph(f"<b>{title}</b><br/><font color='#67798e'>{detail}</font>", st["finding"])])
    ft = Table(frows, colWidths=[8 * mm, 174 * mm])
    ft.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, Color(0.85, 0.88, 0.9)),
    ]))
    story.append(ft)

    # --- bugcheck breakdown ---
    bug = _bugcheck_summary(diag)
    if bug:
        story.append(Paragraph("Bugcheck codes seen (Event 41)", st["h2"]))
        brows = [[Paragraph("<b>Code</b>", st["cell"]), Paragraph("<b>Name</b>", st["cell"]),
                  Paragraph("<b>Count</b>", st["cell"]), Paragraph("<b>Likely cause</b>", st["cell"])]]
        for name, (n, hx) in sorted(bug.items(), key=lambda kv: -kv[1][0]):
            brows.append([Paragraph(hx, st["cell"]), Paragraph(name, st["cell"]),
                          Paragraph(str(n), st["cellb"]),
                          Paragraph(BUGCHECK_CAUSES.get(name, "unrecognised - review event data"), st["cell"])])
        bt = Table(brows, colWidths=[18 * mm, 62 * mm, 14 * mm, 88 * mm], repeatRows=1)
        bt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY_MID),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ROW_ALT]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        for i, (name, _v) in enumerate(sorted(bug.items(), key=lambda kv: -kv[1][0]), start=1):
            if name != "none (hard power / hang)":
                bt.setStyle(TableStyle([("TEXTCOLOR", (2, i), (2, i), RED)]))
        story.append(bt)

    # --- indicator table ---
    story.append(Paragraph("Failure indicators", st["h2"]))
    irows = [[Paragraph("<b>Indicator</b>", st["cell"]), Paragraph("<b>Count</b>", st["cell"]),
              Paragraph("<b>Status</b>", st["cell"]), Paragraph("<b>Assessment</b>", st["cell"])]]
    tones = []
    for name, n, assess, tone in build_indicators(diag):
        tones.append(tone)
        chip = Table([[""]], colWidths=[4 * mm], rowHeights=[4 * mm])
        chip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TONE_COLOR[tone])]))
        irows.append([Paragraph(name, st["cell"]), Paragraph(str(n), st["cellb"]), chip,
                      Paragraph(assess, st["cell"])])
    it = Table(irows, colWidths=[62 * mm, 14 * mm, 14 * mm, 92 * mm], repeatRows=1)
    it.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY_MID),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ROW_ALT]),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(it)
    story.append(Paragraph("Status: green = clear, orange = watch, red = act.", st["small"]))

    # --- dumps ---
    story.append(Paragraph("Crash dumps", st["h2"]))
    md = dumps.get("minidumps") or []
    drows = [[Paragraph("<b>File</b>", st["cell"]), Paragraph("<b>Modified</b>", st["cell"]),
              Paragraph("<b>Size</b>", st["cell"]), Paragraph("<b>Copied</b>", st["cell"])]]
    if md:
        for m in md:
            drows.append([Paragraph(m.get("name", "?"), st["cell"]),
                          Paragraph(m.get("mtime", "?"), st["cell"]),
                          Paragraph(f"{(m.get('bytes') or 0) / 1e6:.1f} MB", st["cell"]),
                          Paragraph("yes" if m.get("copied") else "no", st["cell"])])
    else:
        drows.append([Paragraph("no minidumps found", st["cell"]), "", "", ""])
    mem_dmp = f"{(dumps.get('memory_dmp_bytes') or 0) / 1e9:.1f} GB (not copied)" if dumps.get("memory_dmp_exists") else "not present"
    drows.append([Paragraph("MEMORY.DMP", st["cell"]), "", Paragraph(mem_dmp, st["cell"]), ""])
    dt = Table(drows, colWidths=[70 * mm, 45 * mm, 45 * mm, 22 * mm], repeatRows=1)
    dt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY_MID),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ROW_ALT]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(dt)

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Auto-generated summary - verify with technician review. Full data: "
        "diagnostics.json and converted event logs under md/ in this bundle.",
        st["small"]))

    doc.build(story)
    return out_path


if __name__ == "__main__":
    import json
    import sys

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("diagnostics.json")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".pdf")
    generate_pdf(json.loads(src.read_text(encoding="utf-8-sig")), dst)
    print(dst)
