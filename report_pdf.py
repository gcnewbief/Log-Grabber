"""report_pdf - GC-branded automatic failure summary PDF from diagnostics.json.

Deterministic: counts failure indicators from the collector's diagnostics.json
and maps them to likely causes using the shop's known-bad-part and event
pattern knowledge. No AI write-up needed; md/ + diagnostics.json remain the
source for a deeper AI pass later.

  generate_pdf(diag, out_path, version)
"""

from __future__ import annotations

import re
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

# Bugcheck code -> name. Keys are the raw numeric codes as stored in the
# dump header / Event 41 BugcheckCode (e.g. 0xEF = 239). Keep in sync with
# $bugcheckNames in collector.ps1.
BUGCHECK_NAMES = {
    0x00: "none (hard power / hang)",
    0x0A: "IRQL_NOT_LESS_OR_EQUAL",
    0x19: "BAD_POOL_HEADER",
    0x1A: "MEMORY_MANAGEMENT",
    0x1E: "KMODE_EXCEPTION_NOT_HANDLED",
    0x24: "NTFS_FILE_SYSTEM",
    0x3B: "SYSTEM_SERVICE_EXCEPTION",
    0x50: "PAGE_FAULT_IN_NONPAGED_AREA",
    0x74: "BAD_SYSTEM_CONFIG_INFO",
    0x77: "KERNEL_STACK_INPAGE_ERROR",
    0x7A: "KERNEL_DATA_INPAGE_ERROR",
    0x7B: "INACCESSIBLE_BOOT_DEVICE",
    0x7F: "UNEXPECTED_KERNEL_MODE_TRAP",
    0x80: "NMI_HARDWARE_FAILURE",
    0x85: "SETUP_FAILURE",
    0x9C: "MACHINE_CHECK_EXCEPTION",
    0x9F: "DRIVER_POWER_STATE_FAILURE",
    0xBE: "ATTEMPTED_WRITE_TO_READONLY_MEMORY",
    0xC2: "BAD_POOL_CALLER",
    0xC5: "DRIVER_CORRUPTED_EXPOOL",
    0xD1: "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
    0xDE: "POOL_CORRUPTION_IN_FILE_AREA",
    0xED: "UNMOUNTABLE_BOOT_VOLUME",
    0xEF: "CRITICAL_PROCESS_DIED",
    0xF4: "CRITICAL_OBJECT_TERMINATION",
    0xFC: "ATTEMPTED_EXECUTE_OF_NOEXECUTE_MEMORY",
    0xFE: "BUGCODE_USB_DRIVER",
    0x101: "CLOCK_WATCHDOG_TIMEOUT",
    0x10D: "WDF_VIOLATION",
    0x10E: "VIDEO_MEMORY_MANAGEMENT_INTERNAL",
    0x116: "VIDEO_TDR_FAILURE",
    0x117: "VIDEO_TDR_TIMEOUT_DETECTED",
    0x124: "WHEA_UNCORRECTABLE_ERROR",
    0x133: "DPC_WATCHDOG_TIMEOUT",
    0x139: "KERNEL_SECURITY_CHECK_FAILURE",
    0x13A: "KERNEL_MODE_HEAP_CORRUPTION",
    0x144: "BUGCODE_USB3_DRIVER",
    0x154: "UNEXPECTED_STORE_EXCEPTION",
    0x15F: "LOST_DELAYED_WRITE_TIMEOUT",
    0x1C7: "STORE_DATA_STRUCTURE_CORRUPTION",
    0x1000007E: "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
}

# Bugcheck name -> likely cause, for the indicator assessment column.
BUGCHECK_CAUSES = {
    "CRITICAL_PROCESS_DIED": "critical system process died - usually corrupt system files or failing boot storage",
    "UNEXPECTED_STORE_EXCEPTION": "store exception - classic wedged/failing SSD pattern",
    "KERNEL_DATA_INPAGE_ERROR": "kernel data page-in failed - failing drive or storage path",
    "KERNEL_STACK_INPAGE_ERROR": "kernel stack page-in failed - failing drive or storage path",
    "INACCESSIBLE_BOOT_DEVICE": "boot device lost during boot - storage/controller suspect",
    "UNMOUNTABLE_BOOT_VOLUME": "boot volume unmountable - storage/filesystem suspect",
    "WHEA_UNCORRECTABLE_ERROR": "uncorrectable hardware error - PCIe/CPU/RAM fault",
    "MACHINE_CHECK_EXCEPTION": "CPU machine check - board/CPU/power suspect",
    "IRQL_NOT_LESS_OR_EQUAL": "IRQL fault - driver or RAM suspect",
    "MEMORY_MANAGEMENT": "memory management fault - RAM suspect",
    "PAGE_FAULT_IN_NONPAGED_AREA": "bad pointer fault - RAM or driver suspect",
    "NTFS_FILE_SYSTEM": "NTFS fault - filesystem corruption or failing drive",
    "BAD_SYSTEM_CONFIG_INFO": "registry/config fault - often storage corruption",
    "NMI_HARDWARE_FAILURE": "hardware NMI - board/RAM/CPU suspect",
    "BUGCODE_USB_DRIVER": "USB driver/device fault",
    "BUGCODE_USB3_DRIVER": "USB3 driver/device fault",
    "DRIVER_POWER_STATE_FAILURE": "driver power-state fault - often storage or driver",
    "VIDEO_TDR_FAILURE": "GPU reset timeout - graphics driver/hardware",
    "VIDEO_TDR_TIMEOUT_DETECTED": "GPU timeout - graphics driver/hardware",
    "UNEXPECTED_KERNEL_MODE_TRAP": "kernel trap - RAM/CPU or overclock suspect",
    "CLOCK_WATCHDOG_TIMEOUT": "CPU watchdog - processor, power or cooling suspect",
    "DPC_WATCHDOG_TIMEOUT": "DPC watchdog - often storage/driver latency",
    "LOST_DELAYED_WRITE_TIMEOUT": "lost delayed write - storage suspect",
    "SETUP_FAILURE": "setup failure - install/media suspect",
    "none (hard power / hang)": "no bugcheck recorded - hard power loss, hang, or forced power-off",
    "unknown": "unrecognised bugcheck code - review event data",
}

# Bugcheck classes that implicate the boot drive specifically.
STORAGE_BUGCHECKS = {
    "CRITICAL_PROCESS_DIED", "UNEXPECTED_STORE_EXCEPTION",
    "KERNEL_DATA_INPAGE_ERROR", "KERNEL_STACK_INPAGE_ERROR",
    "INACCESSIBLE_BOOT_DEVICE", "UNMOUNTABLE_BOOT_VOLUME",
    "LOST_DELAYED_WRITE_TIMEOUT", "NTFS_FILE_SYSTEM",
}

# WHEA CPER section-type GUIDs in the raw little-endian byte form used by the
# binary error record. Names the hardware class the record is about without
# decoding the full section body.
WHEA_SECTION_TYPES = {
    "adcc7698b447db4bb65e16f193c4f3db": "processor generic",
    "b0a03edc41a19747b95b53a242b6e4d5": "x86/x64 processor",
    "1411bca5646fde4eb8633e83ed7c83b1": "platform memory",
    "54e995d9c1bb0f43ad91b44dcb3c6f35": "PCIe",
    "633975c6843b9540bf78eddad3f9c9dd": "PCI/PCI-X bus",
    "85465eeb66ca6947b6a226068b001326": "PCI/PCI-X device",
    "962a2181ed09964994718d729c8e69ed": "firmware error ref",
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
USB_BUS_TYPES = {"7", "18"}  # 7=USB, 18=SD (0=SATA/AHCI, 17=NVMe are internal)

# Storport Operational semantics (channel manifest):
# - 549 "first instance of the error seen during this time period": fires once
#   per unique (opcode, SrbStatus, ScsiStatus, sense) signature per device.
#   Most are ~1ms completions where firmware rejected a command it does not
#   implement (SenseKey 5 ILLEGAL REQUEST on READ CAPACITY(16)/mode/log pages,
#   SrbStatus 6/8/33 on LUN probes) - normal chatter on every machine.
# - 557 "IO took more than Threshold_ms to complete": the threshold applies to
#   RequestDuration + WaitDuration. WaitDuration keeps counting while an IO
#   sits parked in the storport queue - disk in D3, port paused, machine asleep
#   or hibernated - so healthy laptops log 557s with hours-long waits after
#   resume. Only a large RequestDuration means the device itself was slow.
# - 500/501/550/561: request timeouts and device/bus/hierarchical resets.
STORPORT_SLOW_MS = 5000                     # single-IO service time that counts as a real stall
STORPORT_ABSURD_MS = 1_800_000              # 30min - beyond this a duration is a sleep/sentinel artifact
STORPORT_ERR_STATUS = {5, 9, 10, 11, 13, 14}  # busy, timeout, sel/cmd timeout, bus reset, parity
STORPORT_ERR_SENSE = {3, 4, 11}             # medium error, hardware error, aborted command
STORPORT_PROBE_STATUS = {6, 8, 33}          # invalid request, no device, bad function
STORPORT_RESET_IDS = {500, 501, 550, 561}
STORPORT_CATS = ("stall", "error", "inconclusive", "queued", "benign")


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _storport_bucket(s):
    """Bucket one storport sample:
    'stall'        - 557 with a sane but slow device service time (real latency)
    'error'        - meaningful command failure (busy/timeout/reset, bad sense)
                     or a timeout/reset event
    'inconclusive' - error-type signature but unreliable timing (sleep/sentinel
                     duration) or an unrecognised status
    'queued'       - 557 where queue wait dominated (parked during sleep/power)
    'benign'       - unsupported-command/probe rejects (~1ms, normal chatter)
    """
    dd = s.get("data") or {}
    eid = _int(s.get("id"))
    req = _int(dd.get("RequestDuration_ms"))
    if eid == 557:
        if req >= STORPORT_ABSURD_MS:
            return "inconclusive"
        return "stall" if req >= STORPORT_SLOW_MS else "queued"
    if eid in STORPORT_RESET_IDS:
        return "error"
    if eid != 549:
        return "inconclusive"
    srb, sense = _int(dd.get("SrbStatus")), _int(dd.get("SenseKey"))
    if srb in STORPORT_ERR_STATUS or sense in STORPORT_ERR_SENSE or (srb == 2 and req >= 1000):
        return "error" if req < STORPORT_ABSURD_MS else "inconclusive"
    if srb in STORPORT_PROBE_STATUS or sense == 5:
        return "benign"
    return "inconclusive" if req >= STORPORT_ABSURD_MS else "benign"


def _storport_classify(diag):
    """Bucket sampled storport events per category and bus side:
    -> {"stall"|"error"|"inconclusive"|"queued"|"benign":
        {"internal": {dev: n}, "external": {dev: n}}, "truncated": bool}"""
    cats = {c: {"internal": {}, "external": {}} for c in STORPORT_CATS}
    stor = (diag.get("events") or {}).get("storport") or {}
    for s in stor.get("samples") or []:
        dd = s.get("data") or {}
        mp = (dd.get("MiniportName") or "").strip().lower()
        dev = (dd.get("ProductId") or dd.get("VendorId") or "").strip() or mp or "unknown"
        boot = (dd.get("BootDevice") or "").strip().lower() == "true"
        side = "external" if (not boot) and (
            mp in EXTERNAL_MINIPORTS or (dd.get("BusType") or "").strip() in USB_BUS_TYPES
        ) else "internal"
        bucket = cats[_storport_bucket(s)][side]
        bucket[dev] = bucket.get(dev, 0) + 1
    cats["truncated"] = bool(stor.get("truncated"))
    return cats


def _merge_counts(*counters):
    out = {}
    for c in counters:
        for k, v in c.items():
            out[k] = out.get(k, 0) + v
    return out


def _storport_total(cat):
    return sum(sum(d.values()) for d in cat.values())


def _disk_error_samples(diag):
    """Real 'disk'-provider error events. The collector's old XPath matched
    every provider with IDs 7/11/51/153, so Kernel-Boot 153 (VBS info) and
    Kernel-General 11 (TxR registry mounts) inflated the count."""
    out = []
    for s in ((diag.get("events") or {}).get("disk") or {}).get("samples") or []:
        if (s.get("provider") or "").lower() == "disk":
            out.append(s)
    return out


def _disk_targets(samples):
    nums = set()
    for s in samples:
        m = re.search(r"(?:Harddisk|Disk )(\d+)", s.get("message") or "")
        if m:
            nums.add(int(m.group(1)))
    return sorted(nums)


def _disk_error_count(diag, samples):
    """Provider-filtered disk error count; falls back to the raw count only
    when the bundle has no disk samples at all."""
    if ((diag.get("events") or {}).get("disk") or {}).get("samples"):
        return len(samples)
    return _count(diag, "disk_7_11_51_153")


# --- Storport/Health channel decoders (events.storage_health) ----------------
# The Health channel carries periodic per-device SMART snapshots: 510 = classic
# SMART attribute table, 511 = ATA Device Statistics pages, 512 = NVMe Health
# Information log - all binary blobs in the event payload.

LATENCY_WARN_MS = 10000  # MS: ReadLatencyMax >10s "may indicate a problem"

NVME_WARN_BITS = (
    (0, "spare below threshold"), (1, "temperature out of range"),
    (2, "reliability degraded"), (3, "read-only mode"),
    (4, "volatile memory backup failed"), (5, "persistent memory unreliable"),
)

# SMART attributes whose raw value >0 means real trouble or early warning.
SMART_BAD_ATTRS = {
    5: "Reallocated sectors", 187: "Reported uncorrectable errors",
    188: "Command timeouts", 196: "Reallocation events",
    197: "Pending sectors", 198: "Offline uncorrectable",
    199: "Interface CRC errors", 201: "Soft read errors",
}
SMART_SEVERE_ATTRS = {"Reported uncorrectable errors", "Pending sectors",
                      "Offline uncorrectable"}
SMART_INFO_ATTRS = {9: "hours", 12: "power cycles"}


def _hex_bytes(s):
    try:
        return bytes.fromhex((s or "").strip())
    except (TypeError, ValueError):
        return b""


def _le128(b, off):
    return int.from_bytes(b[off:off + 16], "little")


def _decode_nvme_health(dd):
    """Event 512 NvmeHealthLog / collector nvme_log = the standard NVMe
    SMART/Health page (512 bytes)."""
    raw = _hex_bytes(dd.get("NvmeHealthLog"))
    # composite temp is always a sane Kelvin value on a live drive - rejects
    # all-zero pages and wrong-offset decodes of unrelated data
    if len(raw) < 192 or not any(raw) or not (150 <= int.from_bytes(raw[1:3], "little") <= 500):
        return None
    return {
        "critical_warning": raw[0],
        "temperature_c": int.from_bytes(raw[1:3], "little") - 273,
        "spare_pct": raw[3],
        "spare_threshold_pct": raw[4],
        "used_pct": raw[5],
        "power_cycles": _le128(raw, 112),
        "power_on_hours": _le128(raw, 128),
        "unsafe_shutdowns": _le128(raw, 144),
        "media_errors": _le128(raw, 160),
        "error_log_entries": _le128(raw, 176),
    }


def _decode_smart_attrs(dd):
    """Event 510 SmartAttributes = 2-byte header + 30 x 12-byte records
    [id, flags(2), value, worst, raw(6), reserved] -> {id: (value, worst, raw)}."""
    raw = _hex_bytes(dd.get("SmartAttributes"))
    out = {}
    for i in range(2, len(raw) - 11, 12):
        aid = raw[i]
        if aid:
            out[aid] = (raw[i + 3], raw[i + 4], int.from_bytes(raw[i + 5:i + 11], "little"))
    return out


# ATA Device Statistics (event 511): each page blob starts with an 8-byte
# header, then 8-byte entries [stat#, flags, value(6 LE)]. Only well-known
# statistic numbers are surfaced.
ATA_STATS = {
    "general_errors": {1: ("reported uncorrectable errors", "")},
    "temperature": {1: ("current temp", "C")},
    "transport": {1: ("hardware resets", ""), 3: ("interface CRC errors", "")},
    "solid_state": {1: ("SSD endurance used", "%")},
    "free_fall": {1: ("overlimit shock events", "")},
}


def _decode_ata_devstats(dd):
    """Event 511 -> {page: {known_stat: value}} (unknown stats dropped)."""
    pages = {}
    for page, key in (("general_errors", "GeneralErrors"),
                      ("temperature", "Temperature"),
                      ("transport", "Transport"),
                      ("solid_state", "SolidStateDevice"),
                      ("free_fall", "Freefall")):
        raw = _hex_bytes(dd.get(key))
        stats = {}
        for i in range(8, len(raw) - 7, 8):
            hit = ATA_STATS.get(page, {}).get(raw[i])
            if hit:
                v = int.from_bytes(raw[i + 2:i + 8], "little")
                stats[hit[0]] = f"{v}{hit[1]}"
        if stats:
            pages[page] = stats
    return pages


def _storage_health(diag):
    """Latest decoded health data per device (serial, else product) from the
    storport Health channel. Samples arrive newest-first.
    -> {key: {"product": str, "nvme"|"smart"|"ata": {...}, "health_events": n}}"""
    devs = {}
    for s in ((diag.get("events") or {}).get("storage_health") or {}).get("samples") or []:
        dd = s.get("data") or {}
        key = (dd.get("SerialNumber") or "").strip() or (dd.get("ProductId") or "").strip()
        if not key:
            continue
        ent = devs.setdefault(key, {"product": (dd.get("ProductId") or "").strip() or key,
                                    "health_events": 0})
        ent["health_events"] += 1
        eid = _int(s.get("id"))
        if eid == 512 and "nvme" not in ent:
            dec = _decode_nvme_health(dd)
            if dec:
                ent["nvme"] = dec
        elif eid == 510 and "smart" not in ent:
            dec = _decode_smart_attrs(dd)
            if dec:
                ent["smart"] = dec
        elif eid == 511 and "ata" not in ent:
            dec = _decode_ata_devstats(dd)
            if dec:
                ent["ata"] = dec
    return devs


def _device_health(diag):
    """Merged per-device decoded health: Storport/Health channel events plus
    NVMe logs the collector read directly off the drive (smart[].nvme_log).
    The channel is empty on most client machines - nvme_log is the reliable
    source there."""
    devs = _storage_health(diag)
    for s in (diag.get("storage") or {}).get("smart") or []:
        dec = _decode_nvme_health({"NvmeHealthLog": s.get("nvme_log")})
        if not dec:
            continue
        key = (s.get("serial") or "").strip() or (s.get("friendly") or "?")
        ent = devs.setdefault(key, {"product": s.get("friendly") or key,
                                    "health_events": 0})
        if "nvme" not in ent:
            ent["nvme"] = dec
    return devs


def _match_health(serial, friendly, health_map):
    """Match a physical disk to its decoded health entry by serial, then by
    product string."""
    serial = (serial or "").strip()
    if serial and serial in health_map:
        return health_map[serial]
    friendly = (friendly or "").strip().lower()
    for ent in health_map.values():
        prod = (ent.get("product") or "").strip().lower()
        if prod and (prod in friendly or friendly in prod):
            return ent
    return None


def _smart_latency_ms(smart_entry):
    best = 0
    for k in ("read_latency_max_ms", "write_latency_max_ms", "flush_latency_max_ms"):
        v = _int(smart_entry.get(k))
        if 0 < v < 4294967295:
            best = max(best, v)
    return best


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


def _parse_dmp_header(b):
    """Parse a Windows kernel DUMP_HEADER (first page of a .dmp file).

    Small memory dumps and MEMORY.DMP share this container: 'PAGEDU64'
    (64-bit) or 'PAGEDUMP' (32-bit) signature, then BugCheckCode and four
    parameters at fixed offsets. Returns a dict or None if not a dump.
    """
    if len(b) < 0x60 or not b.startswith(b"PAGE"):
        return None
    if b[4:8] == b"DU64":
        code = int.from_bytes(b[0x38:0x3C], "little")
        params = [int.from_bytes(b[o:o + 8], "little") for o in (0x40, 0x48, 0x50, 0x58)]
        machine = int.from_bytes(b[0x30:0x34], "little")
        arch = {0x8664: "x64", 0xAA64: "ARM64", 0x14C: "x86"}.get(machine, f"0x{machine:X}")
    elif b[4:8] == b"DUMP":
        code = int.from_bytes(b[0x28:0x2C], "little")
        params = [int.from_bytes(b[o:o + 4], "little") for o in (0x2C, 0x30, 0x34, 0x38)]
        arch = "x86"
    else:
        return None
    if not (0 < code <= 0x1FFFFFFF):  # bugcheck code sanity range
        return None
    return {"code": code, "hex": f"0x{code:X}",
            "name": BUGCHECK_NAMES.get(code, "unknown"),
            "params": [f"0x{p:X}" for p in params], "arch": arch}


def _parsed_dumps(diag):
    """Parse dump headers captured by the collector -> [{file, mtime, ...}]."""
    dumps = diag.get("dumps") or {}
    out = []
    for m in dumps.get("minidumps") or []:
        info = _parse_dmp_header(_hex_bytes(m.get("header")))
        if info:
            out.append({"file": m.get("name") or "?", "mtime": m.get("mtime"), **info})
    info = _parse_dmp_header(_hex_bytes(dumps.get("memory_dmp_header")))
    if info:
        out.append({"file": "MEMORY.DMP", "mtime": None, **info})
    return out


def _whea_section_counts(diag):
    """Decode WHEA CPER error records -> {hardware section type: count}."""
    counts = {}
    for s in ((diag.get("events") or {}).get("whea_errors") or {}).get("samples") or []:
        raw = _hex_bytes((s.get("data") or {}).get("RawData"))
        if not raw:
            continue
        if len(raw) >= 136 and raw[:4] == b"CPER":
            n_sec = int.from_bytes(raw[10:12], "little")
            for i in range(min(n_sec, 32)):
                off = 128 + i * 72  # section descriptors follow the 128B header
                if off + 32 > len(raw):
                    break
                name = WHEA_SECTION_TYPES.get(raw[off + 16:off + 32].hex(), "other")
                counts[name] = counts.get(name, 0) + 1
        else:  # unexpected encoding - still classify by GUID substring scan
            hx = raw.hex()
            hits = [n for g, n in WHEA_SECTION_TYPES.items() if g in hx]
            for name in (hits or ["other"]):
                counts[name] = counts.get(name, 0) + 1
    return counts


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
    disk_samples = _disk_error_samples(diag)
    disk_err = _disk_error_count(diag, disk_samples)
    bug = _bugcheck_summary(diag)
    bad_checks = {n: v for n, v in bug.items() if n != "none (hard power / hang)"}
    # Parsed dump headers recover crash causes Event 41 missed (code 0) -
    # dedupe by name so the same crash isn't double-counted.
    parsed_dumps = _parsed_dumps(diag)
    for pd in parsed_dumps:
        if pd["name"] not in bad_checks:
            bad_checks[pd["name"]] = (1, pd["hex"])

    bad_disk_names = []
    for d in (diag.get("storage") or {}).get("disks") or []:
        friendly = (d.get("friendly") or "")
        for sku, note in KNOWN_BAD_DISKS.items():
            if sku.lower() in friendly.lower():
                bad_disk_names.append((friendly, note))

    # 1. Storage pattern - strongest shop signal. Storport samples are bucketed
    # first: real device latency/command errors vs sleep/queue-wait artifacts vs
    # unsupported-command rejects, then internal vs USB/external.
    sp = _storport_classify(diag)
    sig_int = _merge_counts(sp["stall"]["internal"], sp["error"]["internal"])
    n_sig_int = sum(sig_int.values())
    incon_int = sp["inconclusive"]["internal"]
    n_incon_int = sum(incon_int.values())
    incon_explained = False
    top_internal = sorted(sig_int.items(), key=lambda kv: -kv[1])
    store_culprit = bad_disk_names[0][0] if bad_disk_names else (top_internal[0][0] if top_internal else None)
    storage_checks = sorted(bad_checks.keys() & STORAGE_BUGCHECKS)
    n_storage_checks = sum(bad_checks[n][0] for n in storage_checks)
    if n_sig_int or (volmgr and kp41) or bad_disk_names or n_storage_checks:
        bits = []
        n_stall = sum(sp["stall"]["internal"].values())
        n_err = sum(sp["error"]["internal"].values())
        if n_stall:
            bits.append(f"{n_stall} IOs the device took >{STORPORT_SLOW_MS // 1000}s to service "
                        f"({_top_devices(sp['stall']['internal'])})")
        if n_err:
            bits.append(f"{n_err} storport command errors - busy/timeout/reset/bad-sense "
                        f"({_top_devices(sp['error']['internal'])})")
        if n_incon_int:
            bits.append(f"{n_incon_int} further command failures with unreliable timing "
                        "(sleep/power-state artifacts)")
            incon_explained = True
        if volmgr:
            bits.append(f"{volmgr} volmgr 161 dump-write failures")
        if storage_checks:
            bits.append(f"storage-class bugchecks ({', '.join(storage_checks)})")
        tone = "red" if (n_sig_int >= 3 or volmgr or bad_disk_names
                         or n_storage_checks >= 2) else "orange"
        detail = "; ".join(bits) or "storage suspect"
        if store_culprit:
            detail += f". Suspect device: {store_culprit}"
        title = "Boot storage failing" if tone == "red" else "Boot storage suspect"
        tail = ". Replace the drive, do not just reinstall Windows." if tone == "red" \
            else ". Confirm with a drive test before condemning."
        findings.append((tone, title, detail + tail))
    elif n_incon_int >= 3:
        findings.append(("orange", "Boot storage suspect",
                         (f"{n_incon_int} storport command errors on internal devices "
                          f"({_top_devices(incon_int)}) with unreliable timing - probably "
                          "sleep/power artifacts, but the status pattern is worth a drive test.")))
        incon_explained = True

    ext_ev = _merge_counts(sp["stall"]["external"], sp["error"]["external"],
                           sp["inconclusive"]["external"])
    n_ext = sum(ext_ev.values())
    if n_ext:
        findings.append(("green", f"Storport stalls/errors on USB/external devices only (x{n_ext})",
                         (f"Devices: {_top_devices(ext_ev)}. USB/UASP bridge or power-management "
                          "noise - not evidence of a failing boot drive.")))
    n_queued = _storport_total(sp["queued"])
    if n_queued:
        findings.append(("green", f"Storport slow-IO warnings are sleep/queue-wait artifacts (x{n_queued})",
                         ("WaitDuration counts time an IO sat parked while the disk or machine was "
                          "in a low-power state - device service times were fast. Seen on healthy "
                          "laptops, not evidence of a failing drive.")))
    n_benign = _storport_total(sp["benign"])
    n_incon_all = _storport_total(sp["inconclusive"])
    if n_benign or (n_incon_all and not incon_explained):
        extra = (f" Plus {n_incon_all} unclear-signature command errors."
                 if n_incon_all and not incon_explained else "")
        findings.append(("green", f"Storport command rejects - unsupported opcodes (x{n_benign})",
                         ("Devices answered probe commands (READ CAPACITY(16), mode/log pages, "
                          "REPORT LUNS) with ILLEGAL REQUEST or no-device status in ~1ms - "
                          "normal chatter on every machine." + extra)))

    # 1b. SMART / health evidence - decoded device counters (Health channel
    # blobs + per-drive NVMe health logs collected directly off the device).
    for key, h in _device_health(diag).items():
        name = h.get("product") or key
        nvme = h.get("nvme")
        if nvme:
            wflags = [lbl for bit, lbl in NVME_WARN_BITS if nvme["critical_warning"] & (1 << bit)]
            if wflags:
                findings.append(("red", f"NVMe critical warning on {name}",
                                 "; ".join(wflags) + " - the drive is reporting a fault state."))
            if nvme["media_errors"]:
                findings.append(("red" if nvme["media_errors"] >= 5 else "orange",
                                 f"NVMe media errors on {name}",
                                 (f"{nvme['media_errors']} media/data integrity errors in the NVMe "
                                  "health log - failing flash. Back up and replace.")))
            if nvme["spare_threshold_pct"] and nvme["spare_pct"] <= nvme["spare_threshold_pct"]:
                findings.append(("orange", f"NVMe spare capacity exhausted on {name}",
                                 (f"available spare {nvme['spare_pct']}% at/below threshold "
                                  f"{nvme['spare_threshold_pct']}% - worn out.")))
        sa = h.get("smart")
        if sa:
            bad = {SMART_BAD_ATTRS[a]: r for a, (_v, _w, r) in sa.items()
                   if a in SMART_BAD_ATTRS and r}
            if bad:
                severe = any(k in SMART_SEVERE_ATTRS for k in bad)
                findings.append(("red" if severe else "orange",
                                 f"SMART error attributes on {name}",
                                 (", ".join(f"{k}={v}" for k, v in bad.items()) +
                                  (" - unrecoverable errors; replace the drive." if severe
                                   else " - early warning signs, test the drive."))))
        ge = _int((h.get("ata") or {}).get("general_errors", {}).get("reported uncorrectable errors"))
        if ge:
            findings.append(("orange", f"ATA reported uncorrectable errors on {name}",
                             f"{ge} uncorrectable IO errors in the device statistics log."))

    for s in (diag.get("storage") or {}).get("smart") or []:
        if s.get("predict_failure"):
            findings.append(("red", f"SMART predicts failure on {s.get('friendly', '?')}",
                             "drive firmware reports an imminent failure (ATA SMART prediction) - back up and replace."))
        lat = _smart_latency_ms(s)
        if lat >= LATENCY_WARN_MS:
            findings.append(("orange", f"Very slow IO recorded on {s.get('friendly', '?')}",
                             (f"reliability counter shows max IO latency {lat}ms - "
                              "device-side evidence corroborating stalls.")))

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
        whea_secs = _whea_section_counts(diag)
        whea_kinds = ", ".join(f"{k} x{n}" for k, n in
                               sorted(whea_secs.items(), key=lambda kv: -kv[1]))
        n_times = len({s.get("time") for s in
                       ((diag.get("events") or {}).get("whea_errors") or {}).get("samples") or []})
        kinds_txt = whea_kinds or "unclassified records"
        findings.append(("red", f"WHEA hardware errors (x{whea})",
                         (f"WHEA Errors channel - {kinds_txt} across {n_times} logged time(s). "
                          "One big burst = device fell off the bus; PCIe errors often track a "
                          "dying NVMe; memory sections implicate RAM.")))
    if disk_err:
        targets = _disk_targets(disk_samples)
        tgt = f"; targets: {', '.join(f'Disk {n}' for n in targets)}" if targets else ""
        findings.append(("orange", f"Disk error events (x{disk_err})",
                         "System disk 7/11/51/153 errors - controller, cable or drive" + tgt + "."))

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
    disk_samples = _disk_error_samples(diag)
    disk_err = _disk_error_count(diag, disk_samples)
    disk_targets = _disk_targets(disk_samples)
    rows.append(("Disk errors (7/11/51/153)", disk_err,
                 "controller/cable/drive errors"
                 + (f"; on {', '.join(f'Disk {n}' for n in disk_targets)}" if disk_targets else ""),
                 "orange" if disk_err else "green"))
    stor = _count(diag, "storport_549_557")
    sp = _storport_classify(diag)
    sig_int = _merge_counts(sp["stall"]["internal"], sp["error"]["internal"])
    sig_ext = _merge_counts(sp["stall"]["external"], sp["error"]["external"])
    n_sig_int, n_sig_ext = sum(sig_int.values()), sum(sig_ext.values())
    n_sleep_int = sum(sp["queued"]["internal"].values())
    n_sleep_ext = sum(sp["queued"]["external"].values())
    n_incon_int = sum(sp["inconclusive"]["internal"].values())
    n_noise_int = sum(sp["benign"]["internal"].values()) + n_incon_int
    n_noise_ext = sum(sp["benign"]["external"].values()) + sum(sp["inconclusive"]["external"].values())
    n_int = n_sig_int + n_sleep_int + n_noise_int
    n_ext = n_sig_ext + n_sleep_ext + n_noise_ext
    int_note = (f"{n_sig_int} real stalls/errors ({_top_devices(sig_int, 2)}); "
                f"{n_sleep_int} sleep/queue-wait; {n_noise_int} benign/unclear"
                if n_int else "none recorded")
    if not stor:
        int_note = ">5s device I/O or command errors beat SMART Healthy - suspect SSD/NVMe"
    rows.append(("Storport - internal / boot drive", n_int, int_note,
                 "red" if n_sig_int >= 3 else "orange"
                 if n_sig_int or n_incon_int >= 3 else "green"))
    rows.append(("Storport - external / USB devices", n_ext,
                 (f"{n_sig_ext} real stalls/errors ({_top_devices(sig_ext, 2)}); "
                  f"{n_sleep_ext} sleep/queue-wait; {n_noise_ext} benign"
                  if n_ext else "none recorded"),
                 "orange" if n_sig_ext else "green"))
    whea_secs = _whea_section_counts(diag)
    whea_kinds = "/".join(sorted(whea_secs)) or "PCIe/RAM/CPU"
    rows.append(("WHEA hardware errors", _count(diag, "whea_errors"),
                 f"{whea_kinds} hardware fault" if whea_secs else "PCIe/RAM/CPU hardware fault",
                 "red" if _count(diag, "whea_errors") else "green"))
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
    def _smart_for(d):
        """Match a storage.smart entry to a storage.disks entry (same
        Get-PhysicalDisk source -> serial, else friendly name)."""
        for s in smart:
            if (s.get("serial") or "") and s.get("serial") == d.get("serial"):
                return s
            if (s.get("friendly") or "") and s.get("friendly") == d.get("friendly"):
                return s
        return None

    disk_lines = []
    health_map = _device_health(diag)
    for d in disks:
        friendly = d.get("friendly", "?")
        bad = any(sku.lower() in (friendly or "").lower() for sku in KNOWN_BAD_DISKS)
        line = f"{friendly} ({d.get('media', '?')}, {d.get('bus', '?')}, {_gb(d.get('size_bytes'))}, health: {d.get('health', '?')})"
        if bad:
            line += "  ** KNOWN-BAD SKU **"
        disk_lines.append(line)
        s = _smart_for(d)
        if s is None:
            disk_lines.append(f"  SMART {friendly}: not exposed by the storage API")
            continue
        if s.get("error"):
            disk_lines.append(f"  SMART {friendly}: unreadable ({s.get('error', '?')})")
            continue
        def _sv(v):
            return str(v) if v is not None else "?"
        parts = [
            f"wear {_sv(s.get('wear'))}%",
            f"{_sv(s.get('power_on_hours'))}h on",
            f"{_sv(s.get('temperature'))}C",
            f"uncorr. r/w {_sv(s.get('read_errors_uncorrected'))}/{_sv(s.get('write_errors_uncorrected'))}",
        ]
        lat = _smart_latency_ms(s)
        if lat:
            parts.append(f"max IO {lat}ms" + (" ** SLOW **" if lat >= LATENCY_WARN_MS else ""))
        if s.get("predict_failure"):
            parts.append("** PREDICTS FAILURE **")
        disk_lines.append(f"  SMART {friendly}: " + ", ".join(parts))
        h = _match_health(s.get("serial"), s.get("friendly"), health_map)
        if not h:
            continue
        nvme = h.get("nvme")
        if nvme:
            wflags = [lbl for bit, lbl in NVME_WARN_BITS if nvme["critical_warning"] & (1 << bit)]
            warn = f"  ** CRITICAL WARNING: {'; '.join(wflags)} **" if wflags else ""
            disk_lines.append(
                f"    NVMe health log: spare {nvme['spare_pct']}% (thr {nvme['spare_threshold_pct']}%), "
                f"used {nvme['used_pct']}%, media errors {nvme['media_errors']}, "
                f"unsafe shutdowns {nvme['unsafe_shutdowns']}, cycles {nvme['power_cycles']}, "
                f"{nvme['power_on_hours']}h, {nvme['temperature_c']}C" + warn)
        sa = h.get("smart")
        if sa:
            bad = [f"{SMART_BAD_ATTRS[a]}={sa[a][2]}" for a in SMART_BAD_ATTRS
                   if a in sa and sa[a][2]]
            info = [f"{SMART_INFO_ATTRS[a]} {sa[a][2]}" for a in SMART_INFO_ATTRS
                    if a in sa and sa[a][2]]
            t = sa.get(194)
            if t and 0 < (t[2] & 0xFFFF) < 120:
                info.append(f"temp {t[2] & 0xFFFF}C")
            if bad or info:
                disk_lines.append("    SMART attrs: " + ", ".join(bad + info))
        ata = h.get("ata")
        if ata:
            flat = [f"{k}={v}" for pg in ata.values() for k, v in pg.items()]
            if flat:
                disk_lines.append("    ATA dev stats: " + ", ".join(flat))

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
    parsed_dumps = {pd["file"]: pd for pd in _parsed_dumps(diag)}

    def _bug_cell(fname):
        pd = parsed_dumps.get(fname)
        if not pd:
            return ""
        return Paragraph(f"<b>{pd['name']} ({pd['hex']})</b> {pd['arch']}<br/>"
                         f"<font color='#67798e'>params: {' '.join(pd['params'])}</font>", st["cell"])

    md = dumps.get("minidumps") or []
    drows = [[Paragraph("<b>File</b>", st["cell"]), Paragraph("<b>Modified</b>", st["cell"]),
              Paragraph("<b>Size</b>", st["cell"]), Paragraph("<b>Copied</b>", st["cell"]),
              Paragraph("<b>Decoded bugcheck</b>", st["cell"])]]
    if md:
        for m in md:
            drows.append([Paragraph(m.get("name", "?"), st["cell"]),
                          Paragraph(m.get("mtime", "?"), st["cell"]),
                          Paragraph(f"{(m.get('bytes') or 0) / 1e6:.1f} MB", st["cell"]),
                          Paragraph("yes" if m.get("copied") else "no", st["cell"]),
                          _bug_cell(m.get("name", "?"))])
    else:
        drows.append([Paragraph("no minidumps found", st["cell"]), "", "", "", ""])
    mem_dmp = f"{(dumps.get('memory_dmp_bytes') or 0) / 1e9:.1f} GB (header only)" if dumps.get("memory_dmp_exists") else "not present"
    drows.append([Paragraph("MEMORY.DMP", st["cell"]), "", Paragraph(mem_dmp, st["cell"]),
                  "", _bug_cell("MEMORY.DMP")])
    dt = Table(drows, colWidths=[42 * mm, 33 * mm, 27 * mm, 14 * mm, 66 * mm], repeatRows=1)
    dt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY_MID),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ROW_ALT]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(dt)
    if parsed_dumps:
        story.append(Paragraph(
            "Bugcheck decoded from dump headers - parameters are the four bugcheck "
            "arguments (see Microsoft bugcheck reference for meaning).", st["small"]))

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
