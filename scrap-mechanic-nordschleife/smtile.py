"""Read and write Scrap Mechanic .tile files (just enough to place assets).

Layout follows QuestionableM's SM-Converter reader (Code/Converter/TileConverter):
a "TILE" header, one fixed-size header per 64 m cell, then LZ4 blocks that the
cell headers point at by absolute file offset. Writing keeps every original
block untouched and appends new asset lists at the end of the file, so terrain,
clutter and anything we don't understand stays exactly as the Tile Editor saved it.
"""

import struct
import uuid as uuidlib

import lz4.block

MAGIC = 0x454C4954  # "TILE"
HEADER = struct.Struct("<II16sQIIIIiii")  # magic + TileHeaderBaseInfo
CELL_SIZE = 64.0

# int32 slots inside a cell header (byte offset / 4)
ASSET_COUNT, ASSET_INDEX, ASSET_CSIZE, ASSET_SIZE = 0x54 // 4, 0x64 // 4, 0x74 // 4, 0x84 // 4


class Asset:
    def __init__(self, uuid_bytes, pos, rot_xyzw=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0),
                 colors=(), tail=b"\x00"):
        self.uuid_bytes = uuid_bytes
        self.pos = tuple(pos)
        self.rot = tuple(rot_xyzw)
        self.scale = tuple(scale)
        self.colors = list(colors)  # [(name, 0xAABBGGRR int)]
        self.tail = tail  # extra byte(s) added in tile version 12

    def __repr__(self):
        return (f"Asset(uuid={self.uuid_bytes.hex()}, pos={self.pos}, rot={self.rot}, "
                f"scale={self.scale}, colors={self.colors}, tail={self.tail.hex()})")


class Tile:
    def __init__(self, data):
        self.data = bytearray(data)
        (magic, self.version, self.uuid_raw, self.creator, self.width, self.height,
         self.cell_offset, self.cell_size, self.val1, self.val2, self.type) = HEADER.unpack_from(self.data, 0)
        if magic != MAGIC:
            raise ValueError("not a .tile file")
        if self.cell_offset != HEADER.size:
            raise ValueError(f"unexpected cell header offset {self.cell_offset}")

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self.data)

    # -- cell headers -------------------------------------------------------
    def _cell_pos(self, x, y):
        return self.cell_offset + (x + y * self.width) * self.cell_size

    def cell_ints(self, x, y):
        n = self.cell_size // 4
        return list(struct.unpack_from(f"<{n}i", self.data, self._cell_pos(x, y)))

    def _set_cell_int(self, x, y, slot, value):
        struct.pack_into("<i", self.data, self._cell_pos(x, y) + slot * 4, value)

    # -- assets --------------------------------------------------------------
    def assets(self, x, y, lst):
        h = self.cell_ints(x, y)
        count, index = h[ASSET_COUNT + lst], h[ASSET_INDEX + lst]
        csize, size = h[ASSET_CSIZE + lst], h[ASSET_SIZE + lst]
        if count == 0:
            return []
        raw = lz4.block.decompress(bytes(self.data[index:index + csize]), uncompressed_size=size)
        out, i = [], 0
        for _ in range(count):
            pos = struct.unpack_from("<3f", raw, i)
            rot = struct.unpack_from("<4f", raw, i + 0x0C)
            if self.version < 5:
                s = struct.unpack_from("<f", raw, i + 0x1C)[0]
                scale = (s, s, s)
                i += 0x20
            else:
                scale = struct.unpack_from("<3f", raw, i + 0x1C)
                i += 0x28
            uid = bytes(raw[i:i + 16])
            i += 16
            colors = []
            n = raw[i]
            i += 1
            for _ in range(n):
                ln = raw[i]
                name = raw[i + 1:i + 1 + ln].decode("utf-8", "replace")
                i += 1 + ln
                colors.append((name, struct.unpack_from("<I", raw, i)[0]))
                i += 4
            tail = b""
            if self.version >= 12:
                tail = bytes(raw[i:i + 1])
                i += 1
            out.append(Asset(uid, pos, rot, scale, colors, tail))
        if i != size:
            raise ValueError(f"asset list size mismatch in cell {x},{y} list {lst}: read {i}, header {size}")
        return out

    def _pack_assets(self, assets):
        if self.version < 12:
            raise ValueError(f"tile version {self.version} not supported for writing")
        buf = bytearray()
        for a in assets:
            buf += struct.pack("<3f4f3f", *a.pos, *a.rot, *a.scale)
            buf += a.uuid_bytes
            buf.append(len(a.colors))
            for name, col in a.colors:
                nb = name.encode("utf-8")
                buf.append(len(nb))
                buf += nb
                buf += struct.pack("<I", col)
            buf += a.tail if a.tail else b"\x00"
        return bytes(buf)

    def set_assets(self, x, y, lst, assets):
        """Replace one asset list of a cell. Positions are cell-local metres."""
        if not assets:
            for slot in (ASSET_COUNT, ASSET_INDEX, ASSET_CSIZE, ASSET_SIZE):
                self._set_cell_int(x, y, slot + lst, 0)
            return
        raw = self._pack_assets(assets)
        comp = lz4.block.compress(raw, store_size=False, mode="high_compression")
        while len(self.data) % 4:
            self.data.append(0)
        index = len(self.data)
        self.data += comp
        self._set_cell_int(x, y, ASSET_COUNT + lst, len(assets))
        self._set_cell_int(x, y, ASSET_INDEX + lst, index)
        self._set_cell_int(x, y, ASSET_CSIZE + lst, len(comp))
        self._set_cell_int(x, y, ASSET_SIZE + lst, len(raw))

    def set_uuid(self, new_uuid=None):
        u = new_uuid or uuidlib.uuid4()
        self.uuid_raw = u.bytes
        self.data[8:24] = u.bytes
        return u

    def describe(self):
        lines = [f"version {self.version}  size {self.width}x{self.height} cells  type {self.type}  "
                 f"creator {self.creator}  uuid {self.uuid_raw.hex()}  vals {self.val1},{self.val2}  "
                 f"cell header {self.cell_size} bytes  file {len(self.data)} bytes"]
        for y in range(self.height):
            for x in range(self.width):
                h = self.cell_ints(x, y)
                counts = h[ASSET_COUNT:ASSET_COUNT + 4]
                if any(counts):
                    lines.append(f"cell {x},{y} asset counts {counts}")
                    for lst in range(4):
                        for a in self.assets(x, y, lst):
                            lines.append(f"  list {lst}: {a}")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(Tile.load(sys.argv[1]).describe())
