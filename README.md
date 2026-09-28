# sqlite-carve

sqlite-carve reads a SQLite database file byte by byte and reports the rows and text values SQLite left behind in the space it marked free. It answers questions such as which row was deleted from an app's message table, or what a dropped table used to hold, without using the sqlite3 library, without replaying a journal, and without any write access to the file.

## What it reads

- Freeblocks, the space inside a page where a deleted cell used to sit.
- Unallocated space, the gap between the cell pointer array and the start of the cell content area, which is where a cell dropped off the front of that area leaves its bytes.
- Pages that were returned to the freelist, such as the pages of a dropped table or of an emptied page, which are often untouched.
- Write ahead log frames, when a `<database>-wal` file sits next to the database, since those frames hold page images that were never checkpointed.

## What it deliberately does not do

- It never imports sqlite3, so the target file is only ever opened for reading. There is no journal replay, no lock, no page rewrite and no hot journal recovery.
- It never guesses a value. A row is only reported when its cell structure adds up exactly: the payload length, the rowid, the record header length, the serial types and the sum of the column body sizes all have to agree.

## Usage

```
python3 -m sqlite_carve path/to/app.db
python3 -m sqlite_carve app.db --json
python3 -m sqlite_carve app.db --table messages --sql
python3 -m sqlite_carve app.db --min-length 4 --keep-live
```

Options:

- `--table NAME` limits the report to one table, and can be repeated.
- `--min-length N` sets the shortest text run to report, 6 by default.
- `--rows-only` and `--strings-only` cut the report in half.
- `--keep-live` turns off the filter that hides a text run starting with a value a live row still holds.
- `--json` prints a machine readable report, `--sql` prints the recovered rows as INSERT statements.
- `--no-wal` skips the write ahead log, `--limit N` caps how many findings are printed.
- `--quiet` drops the header lines and leaves only the findings.

## Reading the output

- `deleted` means the rowid is gone from the live table, so this row no longer exists there.
- `superseded` means the rowid still exists but carries different values, so this is an older copy of a row that is still present.
- `copies` counts how many places the same record turned up, which is useful when a page was reused a few times.
- A text run is raw bytes out of the free space, so it often runs on into the next column of the same deleted record, for example `bob@example.compro` where `pro` was the plan column.
- Table names come from the pages each b-tree owns, so a row found on a page that no tree owns any more is reported with no table name.

## Limits worth knowing

- `PRAGMA secure_delete=ON` zeroes bytes as they are freed and a `VACUUM` rewrites the whole file, so both remove what this tool reads.
- A cell that is freed as a freeblock loses the first four bytes to the freeblock header, and those four bytes are the payload length, the rowid and the beginning of the record header. Such a cell comes back as text rather than as a full row, because the rowid cannot be recovered from bytes that are no longer there.
- An `INTEGER PRIMARY KEY` column is stored as the rowid and is not part of the record, so recovered values for that column show as NULL and the rowid field carries the number.
- A REAL column holding a whole number is stored as an integer to save space, so `99.0` comes back as `99`.
- A record that spills onto overflow pages is only recovered when the page holding it is free and the payload still fits in one page.
- Text encoding follows the file header, so UTF-16 databases are read as UTF-16 and everything else is read as 8 bit text.

## Tests

```
python3 -m unittest discover -s tests -t .
```

The suite builds real databases with sqlite3, deletes rows, drops a table and then checks that the carver finds exactly the values that were removed, that live rows are never reported as recovered, and that the command line report and JSON output stay in step.

## Layout

- `sqlite_carve/format.py` holds the varint and record readers plus the serial type decoding.
- `sqlite_carve/pages.py` parses the database header, the b-tree pages, the freeblock list, the freelist and the write ahead log.
- `sqlite_carve/carve.py` decides which byte ranges are free, recovers rows and text from them, and filters the result against the live tables.
- `sqlite_carve/cli.py` is the command line front end.

## License

MIT, see LICENSE.
