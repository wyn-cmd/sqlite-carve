# page level readers: parse a database header, describe each b-tree page and
# walk the trees so slack space can be told apart from live cells

from .format import (
    SqliteFormatError,
    decode_record,
    read_varint,
)

INTERIOR_INDEX = 2
INTERIOR_TABLE = 5
LEAF_INDEX = 10
LEAF_TABLE = 13
BTREE_TYPES = (INTERIOR_INDEX, INTERIOR_TABLE, LEAF_INDEX, LEAF_TABLE)


def page_usable_size(blob):
    raw = int.from_bytes(blob[16:18], "big")
    page_size = 65536 if raw == 1 else raw
    reserved = blob[20]
    return page_size, page_size - reserved


def page_header_offset(page_no):
    # page 1 carries the 100 byte database header before its b-tree header
    return 100 if page_no == 1 else 0


class PageView:
    # one analysed page: its b-tree layout when it has one, plus whichever byte
    # ranges are no longer live cell data
    def __init__(self, page_no, blob, usable, header_offset=None):
        self.page_no = page_no
        self.blob = blob
        self.usable = usable
        self.header_offset = page_header_offset(page_no) if header_offset is None else header_offset
        self.type = blob[self.header_offset] if self.header_offset < len(blob) else 0
        self.is_btree = self.type in BTREE_TYPES
        self.cells = []
        self.freeblocks = []
        self.content_start = 0
        self.header_size = 0
        self.ncell = 0
        self.unallocated = (0, 0)
        if not self.is_btree:
            return
        base = self.header_offset
        self.ncell = int.from_bytes(blob[base + 3:base + 5], "big")
        self.content_start = int.from_bytes(blob[base + 5:base + 7], "big") or 65536
        self.header_size = 12 if self.type in (INTERIOR_INDEX, INTERIOR_TABLE) else 8
        pointer = base + self.header_size
        for i in range(self.ncell):
            slot = pointer + 2 * i
            if slot + 2 > len(blob):
                break
            self.cells.append(int.from_bytes(blob[slot:slot + 2], "big"))
        self._read_freeblocks()
        # everything between the cell pointer array and the start of the cell
        # content area is unallocated space, which is where a cell that was
        # dropped off the front of the content area leaves its bytes behind
        gap_start = pointer + 2 * self.ncell
        if 0 < self.content_start <= len(self.blob) and gap_start < self.content_start:
            self.unallocated = (gap_start, self.content_start)

    def _read_freeblocks(self):
        # freeblocks form a linked list inside the cell content area, each one
        # holding the offset of the next freeblock and its own size
        offset = int.from_bytes(self.blob[self.header_offset + 1:self.header_offset + 3], "big")
        seen = set()
        while offset and offset not in seen and offset + 4 <= len(self.blob):
            seen.add(offset)
            size = int.from_bytes(self.blob[offset + 2:offset + 4], "big")
            if size < 4 or offset + size > len(self.blob):
                break
            self.freeblocks.append((offset, size))
            offset = int.from_bytes(self.blob[offset:offset + 2], "big")

    def local_payload_size(self, payload_size):
        # SQLite spills part of a large record onto overflow pages, this is how
        # many payload bytes stay in the cell on this page
        usable = self.usable
        if self.type == LEAF_TABLE:
            max_local = usable - 35
        else:
            max_local = ((usable - 12) * 64 // 255) - 23
        if payload_size <= max_local:
            return payload_size, False
        min_local = ((usable - 12) * 32 // 255) - 23
        candidate = min_local + (payload_size - min_local) % (usable - 4)
        if candidate <= max_local:
            return candidate, True
        return min_local, True

    def cell_size(self, offset):
        # length in bytes of the cell starting at this page relative offset
        try:
            payload_size, used_payload = read_varint(self.blob, offset)
            if self.type == LEAF_TABLE:
                _rowid, used_rowid = read_varint(self.blob, offset + used_payload)
            else:
                used_rowid = 0
            local, spilled = self.local_payload_size(payload_size)
            return used_payload + used_rowid + local + (4 if spilled else 0)
        except (SqliteFormatError, IndexError):
            return None

    def live_cell_ranges(self):
        ranges = []
        for offset in self.cells:
            size = self.cell_size(offset)
            if size:
                ranges.append((offset, offset + size))
        return ranges


class Database:
    def __init__(self, path):
        with open(path, "rb") as handle:
            self.data = handle.read()
        self.path = path
        if len(self.data) < 100 or self.data[:16] != b"SQLite format 3\x00":
            raise SqliteFormatError("%s is not an SQLite database file" % path)
        self.page_size, self.usable = page_usable_size(self.data)
        encoding_id = int.from_bytes(self.data[56:60], "big")
        self.encoding = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(encoding_id, "utf-8")
        self.page_count = len(self.data) // self.page_size
        self.freelist_trunk = int.from_bytes(self.data[32:36], "big")
        self.freelist_count = int.from_bytes(self.data[36:40], "big")
        self.freelist_pages = self._read_freelist()
        self.pages = {}
        self.page_owner = {}
        self.schema_entries = []

    def _read_freelist(self):
        pages = set()
        trunk = self.freelist_trunk
        guard = 0
        while trunk and guard < 100000:
            guard += 1
            base = (trunk - 1) * self.page_size
            if base + 8 > len(self.data):
                break
            pages.add(trunk)
            leaf_count = int.from_bytes(self.data[base + 4:base + 8], "big")
            for i in range(leaf_count):
                slot = base + 8 + 4 * i
                if slot + 4 > len(self.data):
                    break
                pages.add(int.from_bytes(self.data[slot:slot + 4], "big"))
            trunk = int.from_bytes(self.data[base:base + 4], "big")
        return pages

    def page_bytes(self, page_no):
        base = (page_no - 1) * self.page_size
        return self.data[base:base + self.page_size]

    def page(self, page_no):
        if page_no not in self.pages:
            self.pages[page_no] = PageView(page_no, self.page_bytes(page_no), self.usable)
        return self.pages[page_no]

    def walk(self, root_page, owner):
        # collects every page of one tree and every live cell byte range in it,
        # which is what tells the carver which bytes are still in use
        stack = [root_page]
        seen = set()
        cell_ranges = []
        leaf_cells = []
        while stack:
            page_no = stack.pop()
            if page_no in seen or page_no < 1 or page_no > self.page_count:
                continue
            seen.add(page_no)
            if owner:
                self.page_owner.setdefault(page_no, owner)
            page = self.page(page_no)
            if not page.is_btree:
                continue
            cell_ranges.extend(page.live_cell_ranges())
            if page.type in (INTERIOR_TABLE, INTERIOR_INDEX):
                for offset in page.cells:
                    if offset + 4 <= len(page.blob):
                        stack.append(int.from_bytes(page.blob[offset:offset + 4], "big"))
                rightmost = int.from_bytes(page.blob[page.header_offset + 8:page.header_offset + 12], "big")
                if rightmost:
                    stack.append(rightmost)
            elif page.type == LEAF_TABLE:
                for offset in page.cells:
                    leaf_cells.append((page_no, offset))
        return {"pages": seen, "cell_ranges": cell_ranges, "leaf_cells": leaf_cells}

    def read_leaf_table_cell(self, page_no, offset):
        page = self.page(page_no)
        try:
            payload_size, used_payload = read_varint(page.blob, offset)
            rowid, used_rowid = read_varint(page.blob, offset + used_payload)
            local, spilled = page.local_payload_size(payload_size)
            start = offset + used_payload + used_rowid
            payload = page.blob[start:start + local]
            if len(payload) != local:
                return None
            values, _consumed = decode_record(payload, self.encoding)
        except (SqliteFormatError, IndexError):
            return None
        return {
            "page": page_no,
            "offset": offset,
            "rowid": rowid,
            "values": values,
            "payload_size": payload_size,
            "spilled": spilled,
        }

    def read_schema(self):
        # sqlite_master always lives in the tree rooted at page 1
        entries = []
        for page_no, offset in self.walk(1, "sqlite_master")["leaf_cells"]:
            record = self.read_leaf_table_cell(page_no, offset)
            if not record or len(record["values"]) < 5:
                continue
            kind, name, table_name, root_page, sql = record["values"][:5]
            entries.append({
                "type": kind,
                "name": name,
                "tbl_name": table_name,
                "rootpage": root_page,
                "sql": sql,
            })
        return entries

    def load_schema(self):
        self.schema_entries = self.read_schema()
        self.page_owner = {1: "sqlite_master"}
        for entry in self.schema_entries:
            root = entry["rootpage"]
            if not root:
                continue
            if entry["type"] == "table":
                self.walk(root, entry["name"])
            elif entry["type"] == "index":
                self.walk(root, entry["tbl_name"])
        return self.schema_entries

    def live_rows(self, table):
        # every row still reachable through the table b-tree, keyed by rowid
        rows = {}
        for entry in self.schema_entries:
            if entry["type"] != "table" or entry["name"] != table:
                continue
            root = entry["rootpage"]
            if not root:
                continue
            for page_no, offset in self.walk(root, table)["leaf_cells"]:
                record = self.read_leaf_table_cell(page_no, offset)
                if record:
                    rows[record["rowid"]] = record["values"]
        return rows

    def owner_of(self, page_no):
        return self.page_owner.get(page_no)


class WalLog:
    # a write ahead log keeps whole page images from before the last checkpoint,
    # so every frame can be analysed exactly like a page of the main file
    def __init__(self, path, page_size):
        with open(path, "rb") as handle:
            self.data = handle.read()
        self.path = path
        self.page_size = page_size
        self.frames = []
        self._read_frames()

    def _read_frames(self):
        if not self.data or self.data[:4] not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
            return
        offset = 32
        while offset + 24 + self.page_size <= len(self.data):
            page_no = int.from_bytes(self.data[offset:offset + 4], "big")
            blob = self.data[offset + 24:offset + 24 + self.page_size]
            self.frames.append((page_no, blob))
            offset += 24 + self.page_size

    def views(self, usable):
        for frame_no, (page_no, blob) in enumerate(self.frames, start=1):
            yield frame_no, PageView(page_no, blob, usable, page_header_offset(page_no))
