import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlite_carve import carve
from sqlite_carve.cli import main
from sqlite_carve.format import decode_record, read_varint
from sqlite_carve.pages import Database


def sample_database(path, row_count=6):
    # secure_delete is turned off so a deleted row leaves its bytes on the page
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA secure_delete=OFF")
    connection.execute("PRAGMA auto_vacuum=NONE")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, score REAL)")
    for rowid in range(1, row_count + 1):
        connection.execute(
            "INSERT INTO t VALUES (?, ?, ?)",
            (rowid, "carved-marker-%d" % rowid, rowid * 1.5),
        )
    connection.commit()
    return connection


def encode_varint(value):
    if value == 0:
        return b"\x00"
    groups = []
    remaining = value
    while remaining:
        groups.append(remaining & 0x7F)
        remaining >>= 7
    groups.reverse()
    out = bytearray()
    for index, group in enumerate(groups):
        out.append(group if index == len(groups) - 1 else group | 0x80)
    return bytes(out)


class FormatTests(unittest.TestCase):
    def test_varint_round_trip(self):
        for value in (0, 1, 127, 128, 300, 16383, 16384, 1 << 30, 1 << 40):
            encoded = encode_varint(value)
            decoded, used = read_varint(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(used, len(encoded))

    def test_decode_record_mixed_types(self):
        values = [42, "hello", 2.5, None, b"\x01\x02", 1]
        serials = []
        body = bytearray()
        for value in values:
            if value is None:
                serials.append(0)
            elif isinstance(value, int):
                size = 1 if abs(value) < 128 else 2
                serials.append(size)
                body += value.to_bytes(size, "big", signed=True)
            elif isinstance(value, float):
                serials.append(7)
                body += struct.pack(">d", value)
            elif isinstance(value, str):
                raw = value.encode()
                serials.append(13 + 2 * len(raw))
                body += raw
            else:
                serials.append(12 + 2 * len(value))
                body += value
        header = bytes([1 + len(serials)]) + bytes(serials)
        decoded, consumed = decode_record(header + bytes(body), "utf-8")
        self.assertEqual(decoded, values)
        self.assertEqual(consumed, len(header) + len(body))


class CarveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sqlite-carve-")
        self.path = os.path.join(self.tmp, "sample.db")

    def test_schema_and_live_rows_are_read(self):
        connection = sample_database(self.path)
        connection.close()
        database = Database(self.path)
        names = [entry["name"] for entry in database.load_schema()]
        self.assertIn("t", names)
        rows = database.live_rows("t")
        self.assertEqual(sorted(rows), [1, 2, 3, 4, 5, 6])
        self.assertEqual(rows[4][1], "carved-marker-4")
        self.assertAlmostEqual(rows[4][2], 6.0)

    def test_deleted_row_text_is_recovered(self):
        connection = sample_database(self.path)
        connection.execute("DELETE FROM t WHERE id = 3")
        connection.commit()
        connection.close()
        findings = carve(self.path)
        texts = [item["text"] for item in findings["strings"]]
        # a slack run can run on into the next column of the deleted record
        self.assertTrue(any("carved-marker-3" in text for text in texts))
        # that value is gone from the live table, so a live value must not show up
        self.assertFalse(any("carved-marker-1" in text for text in texts))
        marker = [item for item in findings["strings"] if "carved-marker-3" in item["text"]][0]
        self.assertGreater(marker["locations"][0]["offset"], 0)

    def test_deleted_row_text_from_a_freed_cell(self):
        # a rowid and a payload that both need two byte varints still leave the
        # text of a freed record sitting in the freeblock on the page
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, payload TEXT, score REAL)")
        long_name = "fragment-value-" + "x" * 160
        for rowid in range(1, 301):
            connection.execute(
                "INSERT INTO big VALUES (?, ?, ?)",
                (rowid, long_name + str(rowid), rowid * 0.5),
            )
        connection.commit()
        connection.execute("DELETE FROM big WHERE id = 150")
        connection.commit()
        connection.close()
        findings = carve(self.path, hide_live=False)
        texts = [item["text"] for item in findings["strings"]]
        self.assertTrue(any(long_name + "150" in text for text in texts), "text of the freed cell was not carved")
        marker = [item for item in findings["strings"] if long_name + "150" in item["text"]][0]
        self.assertEqual(marker["table"], "big")

    def test_row_that_moved_into_unallocated_space_is_recovered_with_its_rowid(self):
        # dropping a cell off the front of the cell content area leaves the whole
        # cell in place, so the row comes back with its rowid attached
        connection = sample_database(self.path)
        connection.execute("INSERT INTO t VALUES (9, 'unallocated-space-row', 99.0)")
        connection.commit()
        connection.execute("DELETE FROM t WHERE id = 9")
        connection.commit()
        connection.close()
        findings = carve(self.path)
        found = [row for row in findings["rows"] if row["rowid"] == 9 and row["table"] == "t"]
        self.assertTrue(found, "the deleted row was not recovered with a rowid")
        # the id column is an alias of the rowid so SQLite never stored it in the
        # record, and a REAL column holding a whole number is stored as an int
        self.assertEqual(found[0]["values"], [None, "unallocated-space-row", 99])
        self.assertEqual(found[0]["kind"], "deleted")

    def test_keep_live_reports_values_that_are_still_there(self):
        # a value is only filtered out when a live row still carries it, so the
        # same text is written twice, one copy deleted and one kept
        connection = sample_database(self.path)
        connection.execute("INSERT INTO t VALUES (9, 'carved-marker-3', 99.0)")
        connection.commit()
        connection.execute("DELETE FROM t WHERE id = 9")
        connection.commit()
        connection.close()
        hidden = carve(self.path)["strings"]
        kept = carve(self.path, hide_live=False)["strings"]
        self.assertFalse(any("carved-marker-3" in item["text"] for item in hidden))
        self.assertTrue(any("carved-marker-3" in item["text"] for item in kept))

    def test_live_rows_are_never_reported_as_recovered(self):
        connection = sample_database(self.path)
        connection.execute("DELETE FROM t WHERE id = 3")
        connection.commit()
        connection.close()
        findings = carve(self.path)
        for row in findings["rows"]:
            if row["table"] == "t":
                self.assertNotIn(row["rowid"], [1, 2, 4, 5, 6])

    def test_rows_from_a_dropped_table_are_recovered(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute("CREATE TABLE gone (id INTEGER PRIMARY KEY, payload TEXT)")
        for rowid in range(1, 200):
            connection.execute(
                "INSERT INTO gone VALUES (?, ?)",
                (rowid, "dropped-payload-%03d" % rowid),
            )
        connection.commit()
        connection.execute("DROP TABLE gone")
        connection.commit()
        connection.close()
        findings = carve(self.path)
        recovered = [row for row in findings["rows"] if row["kind"] == "deleted"]
        self.assertTrue(recovered, "no rows were recovered from the dropped table")
        texts = [row["values"][1] for row in recovered if len(row["values"]) > 1]
        self.assertTrue(any(str(text).startswith("dropped-payload-") for text in texts))

    def test_json_output_from_the_command_line(self):
        connection = sample_database(self.path)
        connection.execute("DELETE FROM t WHERE id = 3")
        connection.commit()
        connection.close()
        stdout = io.StringIO()
        saved = sys.stdout
        sys.stdout = stdout
        try:
            code = main([self.path, "--json"])
        finally:
            sys.stdout = saved
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertGreaterEqual(payload["summary"]["recovered_strings"], 1)
        self.assertTrue(any("carved-marker-3" in item["text"] for item in payload["strings"]))

    def test_text_report_and_sql_output(self):
        connection = sample_database(self.path)
        connection.execute("DELETE FROM t WHERE id = 3")
        connection.commit()
        connection.close()
        stdout = io.StringIO()
        saved = sys.stdout
        try:
            sys.stdout = stdout
            code = main([self.path, "--sql", "--no-wal"])
        finally:
            sys.stdout = saved
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("recovered rows", output)
        self.assertIn("carved-marker-3", output)

    def test_missing_file_is_reported(self):
        stderr = io.StringIO()
        saved = sys.stderr
        try:
            sys.stderr = stderr
            code = main([os.path.join(self.tmp, "nothing.db")])
        finally:
            sys.stderr = saved
        self.assertEqual(code, 2)
        self.assertIn("cannot read", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
