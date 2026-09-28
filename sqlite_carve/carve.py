# the carver itself: it walks the file page by page, reads only the bytes
# SQLite has marked free, and reports what is still sitting there

from .format import SqliteFormatError, decode_record, read_varint
from .pages import Database, WalLog

# every byte a text column of an ASCII database can hold
PRINTABLE = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}

# a carved record longer than this is treated as a misread of unrelated bytes
MAX_PAYLOAD = 1 << 22


def find_printable_runs(blob, offset, min_length):
    # contiguous printable ASCII, which is how a deleted text value survives
    runs = []
    start = None
    end = len(blob)
    for index in range(offset, end):
        byte = blob[index]
        if byte in PRINTABLE:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= min_length:
                runs.append((start, blob[start:index].decode("ascii", errors="replace")))
            start = None
    if start is not None and end - start >= min_length:
        runs.append((start, blob[start:end].decode("ascii", errors="replace")))
    return runs


def find_utf16_runs(blob, offset, min_length):
    # text stored as UTF-16 shows up as a printable byte followed by a zero byte
    runs = []
    start = None
    index = offset
    end = len(blob) - 1
    while index < end:
        low = blob[index]
        high = blob[index + 1]
        if high == 0 and low in PRINTABLE:
            if start is None:
                start = index
            index += 2
            continue
        if start is not None:
            if (index - start) // 2 >= min_length:
                runs.append((start, blob[start:index].decode("utf-16-le", errors="replace")))
            start = None
        index += 1
    if start is not None:
        raw = blob[start:end + 1]
        if len(raw) // 2 >= min_length:
            runs.append((start, raw.decode("utf-16-le", errors="replace")))
    return runs


def local_payload_size(usable, payload_size):
    # the same cap SQLite applies to the part of a record that stays on the page
    max_local = usable - 35
    if payload_size <= max_local:
        return payload_size
    min_local = ((usable - 12) * 32 // 255) - 23
    candidate = min_local + (payload_size - min_local) % (usable - 4)
    if candidate <= max_local:
        return candidate
    return min_local


def try_read_cell(blob, offset, usable, limit, encoding="utf-8"):
    # recognises a complete table leaf cell: payload length, rowid, record header
    # and column bodies whose sizes must add up exactly, which is what keeps
    # random free bytes from being reported as a recovered row
    try:
        payload_size, used_payload = read_varint(blob, offset)
        if payload_size < 2 or payload_size > MAX_PAYLOAD:
            return None
        rowid, used_rowid = read_varint(blob, offset + used_payload)
        if rowid < 0 or rowid > (1 << 63) - 1:
            return None
        payload_start = offset + used_payload + used_rowid
        local = local_payload_size(usable, payload_size)
        if payload_start + local > len(blob) or payload_start + local > limit:
            return None
        payload = blob[payload_start:payload_start + local]
        values, consumed = decode_record(payload, encoding)
        if consumed != payload_size:
            return None
    except (SqliteFormatError, IndexError, TypeError, UnicodeDecodeError):
        return None
    return {
        "rowid": rowid,
        "values": values,
        "payload_size": payload_size,
        "cell_length": used_payload + used_rowid + local,
        "offset": offset,
    }


def json_safe(value):
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


def slack_regions(view, owned, is_freelist):
    # the byte ranges of a page that no longer hold live data, each with the
    # kind of boundary the range has: a freeblock and the unallocated space both
    # end where the live cells begin, a whole page does not
    if is_freelist or not owned:
        return [(0, len(view.blob), "page")]
    if not view.is_btree:
        return []
    regions = [(start, size, "freeblock") for start, size in view.freeblocks]
    gap_start, gap_end = view.unallocated
    if gap_end > gap_start:
        regions.append((gap_start, gap_end - gap_start, "gap"))
    return regions


def chain_cells(region, usable, encoding):
    # cells recovered from a freeblock or from unallocated space have to join up
    # into a run that ends exactly where the live cells start, which is how the
    # bytes SQLite reclaimed are told apart from a chance reading of the slack
    found = {}
    position = 0
    size = len(region)
    while position < size:
        cell = try_read_cell(region, position, usable, size, encoding)
        if cell is None:
            position += 1
            continue
        found[position] = cell
        position += max(cell["cell_length"], 1)
    by_end = {}
    for offset, cell in found.items():
        by_end.setdefault(offset + cell["cell_length"], []).append(offset)
    chain = []
    cursor = size
    while cursor in by_end:
        offset = by_end[cursor][0]
        chain.append(found[offset])
        cursor = offset
    return chain


def carve_page(view, table, min_string, owned, is_freelist, encoding="utf-8"):
    rows = []
    strings = []
    for start, size, kind in slack_regions(view, owned, is_freelist):
        region = view.blob[start:start + size]
        if not region:
            continue
        for offset, text in find_printable_runs(region, 0, min_string):
            strings.append({"text": text, "page": view.page_no, "offset": start + offset})
        for offset, text in find_utf16_runs(region, 0, min_string):
            strings.append({"text": text, "page": view.page_no, "offset": start + offset})
        if kind == "page":
            cells = []
            position = 0
            while position < size:
                cell = try_read_cell(region, position, view.usable, size, encoding)
                if cell is None:
                    position += 1
                    continue
                cells.append(cell)
                position += max(cell["cell_length"], 1)
        else:
            cells = chain_cells(region, view.usable, encoding)
        for cell in cells:
            cell["page"] = view.page_no
            cell["offset"] = start + cell["offset"]
            cell["table"] = table
            rows.append(cell)
    return rows, strings


def live_texts(values):
    out = set()
    for value in values:
        if isinstance(value, str):
            out.add(value)
        elif isinstance(value, bytes):
            out.add(value.decode("utf-8", errors="replace"))
    return out


def carve(path, min_string=6, hide_live=True, include_wal=True, tables=None):
    db = Database(path)
    db.load_schema()
    table_entries = [e for e in db.schema_entries if e["type"] == "table" and e["rootpage"]]
    if tables:
        wanted = set(tables)
        table_entries = [e for e in table_entries if e["name"] in wanted]

    live = {}
    live_text = {}
    for entry in table_entries:
        rows = db.live_rows(entry["name"])
        live[entry["name"]] = rows
        texts = set()
        for values in rows.values():
            texts |= live_texts(values)
        live_text[entry["name"]] = texts

    findings = {
        "database": path,
        "page_size": db.page_size,
        "page_count": db.page_count,
        "encoding": db.encoding,
        "tables": [e["name"] for e in table_entries],
        "rows": [],
        "strings": [],
    }

    for page_no in range(1, db.page_count + 1):
        view = db.page(page_no)
        owned = db.owner_of(page_no)
        is_freelist = page_no in db.freelist_pages
        if not view.is_btree and not is_freelist:
            # an unowned page that is not on the freelist is an overflow page for
            # a live record or a pointer map page, so there is nothing to read
            continue
        rows, strings = carve_page(view, owned, min_string, bool(owned), is_freelist, db.encoding)
        findings["rows"].extend(rows)
        for item in strings:
            item["table"] = owned
            item["source"] = "main"
            findings["strings"].append(item)

    if include_wal:
        wal_path = path + "-wal"
        try:
            wal = WalLog(wal_path, db.page_size)
        except OSError:
            wal = None
        if wal and wal.frames:
            for frame_no, view in wal.views(db.usable):
                owner = db.owner_of(view.page_no)
                rows, strings = carve_page(view, owner, min_string, False, True, db.encoding)
                for row in rows:
                    row["source"] = "wal"
                    row["frame"] = frame_no
                    findings["rows"].append(row)
                for item in strings:
                    item["table"] = owner
                    item["source"] = "wal"
                    item["frame"] = frame_no
                    findings["strings"].append(item)

    return finish_findings(db, findings, live, live_text, hide_live, min_string)


def finish_findings(db, findings, live, live_text, hide_live, min_string=6):
    # a row only counts as deleted when its rowid is gone from the live table
    kept_rows = {}
    for row in findings["rows"]:
        table = row.get("table")
        rowid = row["rowid"]
        if table in live and rowid in live[table]:
            if live[table][rowid] == row["values"]:
                continue
            # the rowid still exists but with different values, so the copy found
            # here is a superseded version of that row
            kind = "superseded"
        else:
            kind = "deleted"
        signature = (table, rowid, repr([json_safe(v) for v in row["values"]]))
        if signature in kept_rows:
            kept_rows[signature]["copies"] += 1
            continue
        kept_rows[signature] = {
            "table": table,
            "rowid": rowid,
            "values": [json_safe(v) for v in row["values"]],
            "kind": kind,
            "source": row.get("source", "main"),
            "page": row["page"],
            "offset": row["offset"],
            "copies": 1,
        }
    findings["rows"] = sorted(
        kept_rows.values(),
        key=lambda r: (str(r["table"]), r["rowid"] is None, r["rowid"] or 0),
    )

    grouped = {}
    for item in findings["strings"]:
        text = item["text"]
        key = (item.get("table"), text)
        if key in grouped:
            grouped[key]["count"] += 1
            if len(grouped[key]["locations"]) < 10:
                grouped[key]["locations"].append({"page": item["page"], "offset": item["offset"], "source": item["source"]})
            continue
        grouped[key] = {
            "table": item.get("table"),
            "text": text,
            "count": 1,
            "locations": [{"page": item["page"], "offset": item["offset"], "source": item["source"]}],
        }
    strings = list(grouped.values())
    if hide_live:
        survivors = []
        for item in strings:
            known = live_text.get(item["table"], set()) if item["table"] in live_text else set()
            # a free text run often runs on into the next column body, so a run
            # counts as live when it starts with a value the live table still
            # holds. This filter trades recall for quiet output, so --keep-live
            # turns it off when every byte matters
            if any(len(value) >= min_string and (item["text"] == value or item["text"].startswith(value)) for value in known):
                continue
            survivors.append(item)
        strings = survivors
    findings["strings"] = sorted(strings, key=lambda s: (-s["count"], s["text"]))

    findings["summary"] = {
        "recovered_rows": len(findings["rows"]),
        "recovered_strings": len(findings["strings"]),
        "freelist_pages": len(db.freelist_pages),
    }
    return findings


def sql_literal_from_json(value):
    # turns a recovered value back into SQL so the output can be replayed
    if value is None:
        return "NULL"
    if isinstance(value, str) and value.startswith("0x"):
        return "X'%s'" % value[2:]
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("'", "''")
    return "'%s'" % escaped


def rows_as_sql(findings, table=None):
    lines = []
    for row in findings["rows"]:
        if table and row["table"] != table:
            continue
        name = row["table"] or "unknown_table"
        values = ", ".join(sql_literal_from_json(v) for v in row["values"])
        lines.append("INSERT INTO \"%s\" VALUES (%s);" % (name, values))
    return lines
