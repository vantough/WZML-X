"""Small bounded bencode reader for resolving .torrent total sizes."""


class TorrentMetadataError(ValueError):
    pass


def decode_bencode(data, *, max_depth=64, max_items=200_000):
    if not isinstance(data, (bytes, bytearray)):
        raise TorrentMetadataError("Torrent metadata must be bytes")
    data = bytes(data)
    count = 0

    def parse(offset, depth):
        nonlocal count
        count += 1
        if count > max_items or depth > max_depth or offset >= len(data):
            raise TorrentMetadataError("Invalid or excessive torrent metadata")
        marker = data[offset : offset + 1]
        if marker == b"i":
            end = data.find(b"e", offset + 1)
            if end < 0:
                raise TorrentMetadataError("Invalid bencoded integer")
            raw = data[offset + 1 : end]
            if not raw or raw == b"-0" or (raw.startswith(b"0") and raw != b"0"):
                raise TorrentMetadataError("Invalid bencoded integer")
            try:
                value = int(raw)
            except ValueError as exc:
                raise TorrentMetadataError("Invalid bencoded integer") from exc
            return value, end + 1
        if marker == b"l":
            values = []
            offset += 1
            while offset < len(data) and data[offset : offset + 1] != b"e":
                value, offset = parse(offset, depth + 1)
                values.append(value)
            if offset >= len(data):
                raise TorrentMetadataError("Unterminated bencoded list")
            return values, offset + 1
        if marker == b"d":
            values = {}
            offset += 1
            while offset < len(data) and data[offset : offset + 1] != b"e":
                key, offset = parse(offset, depth + 1)
                if not isinstance(key, bytes):
                    raise TorrentMetadataError("Bencoded dictionary keys must be bytes")
                value, offset = parse(offset, depth + 1)
                values[key] = value
            if offset >= len(data):
                raise TorrentMetadataError("Unterminated bencoded dictionary")
            return values, offset + 1
        if marker.isdigit():
            colon = data.find(b":", offset)
            if colon < 0:
                raise TorrentMetadataError("Invalid bencoded string")
            raw_len = data[offset:colon]
            if not raw_len or (raw_len.startswith(b"0") and raw_len != b"0"):
                raise TorrentMetadataError("Invalid bencoded string length")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise TorrentMetadataError("Invalid bencoded string length") from exc
            start = colon + 1
            end = start + length
            if length < 0 or end > len(data):
                raise TorrentMetadataError("Truncated bencoded string")
            return data[start:end], end
        raise TorrentMetadataError("Invalid bencoded value")

    value, end = parse(0, 0)
    if end != len(data) or not isinstance(value, dict):
        raise TorrentMetadataError("Invalid torrent metainfo")
    return value


def torrent_total_size(data):
    metainfo = decode_bencode(data)
    info = metainfo.get(b"info")
    if not isinstance(info, dict):
        raise TorrentMetadataError("Torrent metadata has no info dictionary")
    files = info.get(b"files")
    if files is None:
        length = info.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise TorrentMetadataError("Torrent metadata has no valid file size")
        return length
    if not isinstance(files, list) or not files:
        raise TorrentMetadataError("Torrent metadata has no files")
    total = 0
    for file_info in files:
        length = file_info.get(b"length") if isinstance(file_info, dict) else None
        if not isinstance(length, int) or length < 0:
            raise TorrentMetadataError("Torrent metadata has an invalid file size")
        total += length
    return total
