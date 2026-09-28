# reading helpers for the SQLite record format, kept free of the sqlite3 module
# so the carver never opens the target database with a library that could write to it

import struct


class SqliteFormatError(Exception):
    pass


def read_varint(buf, offset):
    # a SQLite varint is one to nine bytes, big endian, seven bits per byte
    # the ninth byte contributes all eight of its bits
    value = 0
    for i in range(9):
        if offset + i >= len(buf):
            raise SqliteFormatError("varint runs past the end of the buffer")
        byte = buf[offset + i]
        if i == 8:
            return ((value << 8) | byte), 9
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, i + 1
    raise SqliteFormatError("unterminated varint")


# how many body bytes each serial type occupies
INT_SIZES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}


def serial_type_size(serial):
    if serial == 0:
        return 0
    if serial in INT_SIZES:
        return INT_SIZES[serial]
    if serial == 7:
        return 8
    if serial in (8, 9):
        return 0
    if serial == 10 or serial == 11:
        raise SqliteFormatError("serial type %d is reserved" % serial)
    if serial >= 12:
        if serial % 2 == 0:
            return (serial - 12) // 2
        return (serial - 13) // 2
    raise SqliteFormatError("unknown serial type %d" % serial)


def decode_value(serial, raw, encoding):
    if serial == 0:
        return None
    if serial in INT_SIZES:
        return int.from_bytes(raw, "big", signed=True)
    if serial == 7:
        return struct.unpack(">d", raw)[0]
    if serial == 8:
        return 0
    if serial == 9:
        return 1
    if serial % 2 == 0:
        return bytes(raw)
    return raw.decode(encoding, errors="replace")


def decode_record(payload, encoding):
    # a record is a header length varint, one serial type per column, then the
    # column bodies packed back to back
    header_length, header_varint = read_varint(payload, 0)
    if header_length > len(payload):
        raise SqliteFormatError("record header runs past the payload")
    serials = []
    pos = header_varint
    while pos < header_length:
        serial, used = read_varint(payload, pos)
        serials.append(serial)
        pos += used
    if pos != header_length:
        raise SqliteFormatError("serial types do not fill the record header")
    body = header_length
    values = []
    for serial in serials:
        size = serial_type_size(serial)
        chunk = payload[body:body + size]
        if len(chunk) != size:
            raise SqliteFormatError("record body runs past the payload")
        values.append(decode_value(serial, chunk, encoding))
        body += size
    return values, body


# turns recovered values back into SQL so the output can be replayed
def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return "X'%s'" % value.hex()
    escaped = value.replace("'", "''")
    return "'%s'" % escaped
