# command line front end for the carver

import argparse
import json
import sys

from .carve import carve, rows_as_sql
from .format import SqliteFormatError

ABOUT = "recover rows and values that SQLite left behind in a database file"


def build_parser():
    parser = argparse.ArgumentParser(prog="sqlite-carve", description=ABOUT)
    parser.add_argument("database", help="path to the SQLite file to read")
    parser.add_argument("--table", action="append", default=None,
                        help="only look at this table, can be repeated")
    parser.add_argument("--min-length", type=int, default=6,
                        help="shortest text run to report, default 6")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many rows and strings, 0 means all")
    parser.add_argument("--rows-only", action="store_true", help="skip the text output")
    parser.add_argument("--strings-only", action="store_true", help="skip the row output")
    parser.add_argument("--keep-live", action="store_true",
                        help="also report text that is still present in a live table")
    parser.add_argument("--no-wal", action="store_true", help="do not read a write ahead log")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    parser.add_argument("--sql", action="store_true", help="print recovered rows as INSERT statements")
    parser.add_argument("--quiet", action="store_true", help="print nothing but the findings")
    return parser


def shorten(value, width=70):
    text = repr(value)
    if len(text) > width:
        return text[:width - 3] + "..."
    return text


def report(findings, args, stream):
    summary = findings["summary"]
    if not args.quiet:
        print("sqlite-carve %s" % findings["database"], file=stream)
        print("page size %d, %d pages, encoding %s" % (findings["page_size"], findings["page_count"], findings["encoding"]), file=stream)
        print("tables: %s" % (", ".join(findings["tables"]) or "none"), file=stream)
        print("freelist pages: %d" % summary["freelist_pages"], file=stream)

    if not args.strings_only:
        rows = findings["rows"][:args.limit or None]
        print("\nrecovered rows (%d):" % summary["recovered_rows"], file=stream)
        for row in rows:
            values = ", ".join(shorten(v, 40) for v in row["values"])
            rowid = "-" if row["rowid"] is None else row["rowid"]
            print("  %-10s %s rowid=%s page %d offset %d copies %d  [%s]" % (
                row["kind"], row["table"] or "unknown", rowid, row["page"],
                row["offset"], row["copies"], values), file=stream)

    if not args.rows_only:
        strings = findings["strings"][:args.limit or None]
        print("\nrecovered text (%d):" % summary["recovered_strings"], file=stream)
        for item in strings:
            spot = item["locations"][0]
            print("  %-6s %s %s" % ("x%d" % item["count"], shorten(item["text"]), "page %d offset %d %s" % (
                spot["page"], spot["offset"], spot["source"])), file=stream)

    if args.sql:
        print("\n-- recovered rows as SQL", file=stream)
        for line in rows_as_sql(findings):
            print(line, file=stream)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        findings = carve(
            args.database,
            min_string=args.min_length,
            hide_live=not args.keep_live,
            include_wal=not args.no_wal,
            tables=args.table,
        )
    except OSError as error:
        print("cannot read %s: %s" % (args.database, error), file=sys.stderr)
        return 2
    except SqliteFormatError as error:
        print("not a SQLite database: %s" % error, file=sys.stderr)
        return 2
    if args.json:
        json.dump(findings, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        report(findings, args, sys.stdout)
    return 0
