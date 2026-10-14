"""evtx_to_md - convert every .evtx file in a folder to Markdown for AI ingestion.

Run it in (or point it at) a folder containing .evtx files. It writes:

    <folder>/evtx_md/_summary.md      - overview table across all files
    <folder>/evtx_md/<name>.md        - full event detail per source file
    <folder>/evtx_md/<name>.partNNN.md - chunked parts when a file would
                                        exceed --max-size-mb

Usage:
    evtx_to_md.exe [folder] [-o output_dir] [--max-events N] [--max-size-mb MB]

Defaults: folder = current directory, output = <folder>/evtx_md,
          parts capped at 20 MB each.
"""

import argparse
import os
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone

from evtx import PyEvtxParser

__version__ = "1.0.0"

LEVELS = {
    "0": "LogAlways",
    "1": "Critical",
    "2": "Error",
    "3": "Warning",
    "4": "Information",
    "5": "Verbose",
}

# Bytes reserved per part for the header block when computing split points.
HEADER_RESERVE = 4096


def md_escape(value):
    """Make a string safe to embed in a Markdown table cell."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("|", "\\|").replace("\n", "<br>")
    return text


def flatten_no_key(elem, key, fields):
    """Flatten an XML element into (name, value) field pairs under `key`.

    Attributes become '<key>.<attr>'; text becomes '<key>'; children recurse.
    Repeated child tags get a [n] suffix so nothing is lost.
    """
    text = (elem.text or "").strip()
    if text:
        fields.append((key, text))
    for attr, val in elem.attrib.items():
        fields.append((f"{key}.{attr.split('}', 1)[-1]}", val))

    counts = Counter(c.tag.split("}", 1)[-1] for c in elem)
    seen = Counter()
    for child in elem:
        ctag = child.tag.split("}", 1)[-1]
        seen[ctag] += 1
        child_key = f"{key}.{ctag}"
        if counts[ctag] > 1:
            child_key += f"[{seen[ctag]}]"
        flatten_no_key(child, child_key, fields)


def parse_event(xml_text):
    """Parse one <Event> record. Returns (fields, meta) where meta holds the
    headline values used in the heading and summary stats."""
    fields = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        fields.append(("RawXml", xml_text[:4000]))
        return fields, {}

    meta = {}
    for section in root:
        tag = section.tag.split("}", 1)[-1]
        if tag == "EventData":
            seen = Counter()
            for data in section:
                name = data.attrib.get("Name")
                if name is None:
                    seen["Data"] += 1
                    name = f"Data[{seen['Data']}]"
                else:
                    seen[name] += 1
                    if seen[name] > 1:
                        name = f"{name}[{seen[name]}]"
                fields.append((f"EventData.{name}", (data.text or "").strip()))
        else:
            # System, UserData, DebugData, RenderingInfo, Binary, etc.
            start = len(fields)
            counts = Counter(c.tag.split("}", 1)[-1] for c in section)
            seen = Counter()
            for child in section:
                ctag = child.tag.split("}", 1)[-1]
                seen[ctag] += 1
                key = f"{tag}.{ctag}"
                if counts[ctag] > 1:
                    key += f"[{seen[ctag]}]"
                flatten_no_key(child, key, fields)
            # Section itself may carry attributes (rare) - keep them.
            for attr, val in section.attrib.items():
                fields.insert(start, (f"{tag}.{attr.split('}', 1)[-1]}", val))

    # Pull headline values straight out of the flattened list.
    flat = dict(fields)
    meta["event_id"] = flat.get("System.EventID", "?")
    meta["record_id"] = flat.get("System.EventRecordID", "?")
    meta["time"] = flat.get("System.TimeCreated.SystemTime", "")
    meta["level"] = LEVELS.get(flat.get("System.Level", ""), flat.get("System.Level", ""))
    return fields, meta


def render_event(n, meta, fields):
    """Render one event as a Markdown block (heading + field table)."""
    rid = meta.get("record_id", n)
    ts = meta.get("time", "")
    lines = [
        f"### Event {n} — RecordID {rid} — {ts or 'unknown time'}",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for name, val in fields:
        lines.append(f"| `{md_escape(name)}` | {md_escape(val)} |")
    return "\n".join(lines) + "\n\n"


def update_stats(stats, meta):
    stats["event_ids"][meta.get("event_id", "?")] += 1
    if meta.get("level"):
        stats["levels"][meta["level"]] += 1
    ts = meta.get("time", "")
    if ts:
        if stats["first_time"] is None or ts < stats["first_time"]:
            stats["first_time"] = ts
        if stats["last_time"] is None or ts > stats["last_time"]:
            stats["last_time"] = ts


def scan_events(evtx_path, max_events=None):
    """Pass 1: gather stats and the rendered size of each event block."""
    stats = {
        "file": os.path.basename(evtx_path),
        "events": 0,
        "first_time": None,
        "last_time": None,
        "event_ids": Counter(),
        "levels": Counter(),
        "status": "ok",
        "error": None,
    }
    sizes = []
    try:
        parser = PyEvtxParser(evtx_path)
        for record in parser.records():
            if max_events and len(sizes) >= max_events:
                stats["status"] = f"truncated at {max_events} events"
                break
            fields, meta = parse_event(record["data"])
            sizes.append(len(render_event(len(sizes) + 1, meta, fields).encode("utf-8")))
            update_stats(stats, meta)
    except Exception as exc:  # corrupt chunk/record - keep what we have
        stats["status"] = f"stopped early: {exc}"
    stats["events"] = len(sizes)
    return stats, sizes


def split_into_parts(sizes, limit):
    """Group event indices into (start, end) 1-based inclusive ranges, each
    fitting `limit` bytes. Never splits inside an event."""
    if not sizes:
        return [(1, 0)]
    if limit is None:
        return [(1, len(sizes))]
    parts, start, acc = [], 0, 0
    for i, s in enumerate(sizes):
        if acc + s > limit and i > start:
            parts.append((start + 1, i))
            start, acc = i, 0
        acc += s
    parts.append((start + 1, len(sizes)))
    return parts


def file_header(evtx_path, stats, part, total_parts, part_range):
    """Header block written at the top of each output part."""
    name = os.path.basename(evtx_path)
    chunked = total_parts > 1
    title = f"# {name} — part {part} of {total_parts}" if chunked else f"# {name}"
    top_ids = ", ".join(
        f"`{eid}` ({c})" for eid, c in stats["event_ids"].most_common(10)
    ) or "n/a"
    lv = ", ".join(
        f"{lvl}: {c}" for lvl, c in sorted(stats["levels"].items())
    ) or "n/a"
    lines = [
        title,
        "",
        f"- **Source:** `{evtx_path}`",
    ]
    if chunked:
        lines.append(
            f"- **Part:** {part} of {total_parts} — events {part_range[0]}-{part_range[1]}"
        )
    lines += [
        f"- **Events written:** {stats['events']}",
        f"- **Time range:** {stats['first_time'] or '?'} -> {stats['last_time'] or '?'}",
        f"- **Levels:** {lv}",
        f"- **Top event IDs:** {top_ids}",
        "",
        "---",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_parts(evtx_path, out_dir, md_name, stats, sizes, parts):
    """Pass 2: re-parse and stream rendered events into part files.
    Returns the list of output file names."""
    total = len(parts)
    if total == 1:
        names = [md_name]
    else:
        stem = md_name[:-3]  # strip .md
        names = [f"{stem}.part{i:03d}.md" for i in range(1, total + 1)]

    part_i = 0
    out = open(os.path.join(out_dir, names[0]), "w", encoding="utf-8", newline="")
    try:
        out.write(file_header(evtx_path, stats, 1, total, parts[0]))
        parser = PyEvtxParser(evtx_path)
        n = 0
        for record in parser.records():
            if n >= len(sizes):
                break
            fields, meta = parse_event(record["data"])
            n += 1
            if n > parts[part_i][1]:
                out.close()
                part_i += 1
                out = open(
                    os.path.join(out_dir, names[part_i]),
                    "w",
                    encoding="utf-8",
                    newline="",
                )
                out.write(
                    file_header(evtx_path, stats, part_i + 1, total, parts[part_i])
                )
            out.write(render_event(n, meta, fields))
    finally:
        out.close()
    return names


def convert_file(evtx_path, out_dir, md_name, max_events=None, max_bytes=None):
    """Convert one .evtx file to one or more Markdown parts."""
    stats, sizes = scan_events(evtx_path, max_events)
    limit = max_bytes - HEADER_RESERVE if max_bytes else None
    parts = split_into_parts(sizes, limit)
    stats["outputs"] = write_parts(evtx_path, out_dir, md_name, stats, sizes, parts)
    if max_bytes and any(s > limit for s in sizes):
        note = "single event exceeds size limit"
        stats["status"] = note if stats["status"] == "ok" else f"{stats['status']}; {note}"
    return stats


def write_summary(out_dir, scan_dir, all_stats):
    path = os.path.join(out_dir, "_summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# EVTX conversion summary\n\n")
        f.write(f"- **Source folder:** `{scan_dir}`\n")
        f.write(f"- **Generated:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        f.write(f"- **evtx_to_md version:** {__version__}\n\n")
        f.write("| File | Events | Time range | Top event IDs | Output | Status |\n")
        f.write("|---|---|---|---|---|---|\n")
        for s in all_stats:
            rng = f"{s['first_time'] or '?'} -> {s['last_time'] or '?'}"
            top = ", ".join(f"`{k}` ({v})" for k, v in s["event_ids"].most_common(5))
            status = s["status"] if s["status"] == "ok" else f"**{s['status']}**"
            outs = s.get("outputs") or []
            if len(outs) == 1:
                out_cell = f"[{outs[0]}](<{outs[0]}>)"
            elif outs:
                shown = outs[:8]
                out_cell = " ".join(
                    f"[{i + 1}](<{o}>)" for i, o in enumerate(shown)
                )
                if len(outs) > 8:
                    out_cell += f" … +{len(outs) - 8} more"
            else:
                out_cell = "—"
            f.write(
                f"| {md_escape(s['file'])} | {s['events']} | {rng} | {top} "
                f"| {out_cell} | {status} |\n"
            )
        f.write("\n")
        for s in all_stats:
            if s["status"] != "ok" and s["error"]:
                f.write(f"## {s['file']} — error\n\n```\n{s['error']}\n```\n\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert all .evtx files in a folder to Markdown."
    )
    ap.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    ap.add_argument(
        "folder",
        nargs="?",
        default=os.getcwd(),
        help="Folder to scan for .evtx files (default: current directory)",
    )
    ap.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: <folder>/evtx_md)",
    )
    ap.add_argument(
        "--max-events",
        type=int,
        default=None,
        metavar="N",
        help="Cap events written per file (default: no cap)",
    )
    ap.add_argument(
        "--max-size-mb",
        type=float,
        default=20.0,
        metavar="MB",
        help="Split output .md files into parts of at most MB megabytes "
        "(1 MB = 1,000,000 bytes; 0 = no limit, default: 20)",
    )
    args = ap.parse_args(argv)

    scan_dir = os.path.abspath(args.folder)
    out_dir = os.path.abspath(args.output or os.path.join(scan_dir, "evtx_md"))

    if not os.path.isdir(scan_dir):
        print(f"error: not a folder: {scan_dir}", file=sys.stderr)
        return 2

    evtx_files = sorted(
        os.path.join(scan_dir, f)
        for f in os.listdir(scan_dir)
        if f.lower().endswith(".evtx")
    )
    if not evtx_files:
        print(f"No .evtx files found in {scan_dir}")
        return 1

    os.makedirs(out_dir, exist_ok=True)
    print(f"Found {len(evtx_files)} .evtx file(s) in {scan_dir}")
    print(f"Writing markdown to {out_dir}\n")

    md_name_for = lambda evtx: re.sub(r"\.evtx$", ".md", evtx, flags=re.I)
    max_bytes = int(args.max_size_mb * 1_000_000) if args.max_size_mb > 0 else None

    all_stats = []
    for i, evtx_path in enumerate(evtx_files, 1):
        base = os.path.basename(evtx_path)
        md_name = md_name_for(base)
        print(f"[{i}/{len(evtx_files)}] {base} ... ", end="", flush=True)
        try:
            stats = convert_file(
                evtx_path, out_dir, md_name, args.max_events, max_bytes
            )
        except Exception as exc:
            stats = {
                "file": base,
                "events": 0,
                "first_time": None,
                "last_time": None,
                "event_ids": Counter(),
                "levels": Counter(),
                "status": "failed",
                "error": f"{exc}\n{traceback.format_exc(limit=3)}",
            }
        all_stats.append(stats)
        n_out = len(stats.get("outputs") or [])
        dest = md_name if n_out <= 1 else f"{n_out} parts"
        print(f"{stats['events']} events -> {dest}  [{stats['status']}]")

    summary_path = write_summary(out_dir, scan_dir, all_stats)
    print(f"\nDone. Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    code = main()
    # If launched as a frozen exe with no args (e.g. double-clicked), keep the
    # console open so the user can read the result.
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    sys.exit(code)
