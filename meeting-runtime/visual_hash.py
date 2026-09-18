"""PNG decode, average-hash, and change detection without image libraries."""
import hashlib
import struct
import zlib


PNG_SIG = b'\x89PNG\r\n\x1a\n'
HASH_SIZE = 8


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def decode_png_rgb(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) < 24 or data[:8] != PNG_SIG:
        raise ValueError('frame is not a PNG image')
    offset = 8
    width = height = None
    color_type = bit_depth = interlace = None
    idat = []
    while offset + 12 <= len(data):
        length = struct.unpack('>I', data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            raise ValueError('PNG chunk is truncated')
        chunk = bytes(data[start:end])
        offset = end + 4
        if kind == b'IHDR':
            if length != 13:
                raise ValueError('PNG header is invalid')
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack('>IIBBBBB', chunk))
            if compression != 0 or filter_method != 0 or interlace != 0:
                raise ValueError('PNG compression is unsupported')
            if bit_depth != 8 or color_type not in (0, 2, 4, 6):
                raise ValueError('PNG color type is unsupported')
        elif kind == b'IDAT':
            idat.append(chunk)
        elif kind == b'IEND':
            break
    if width is None or not idat:
        raise ValueError('PNG is missing image data')
    raw = zlib.decompress(b''.join(idat))
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    stride = width * channels
    rows = []
    cursor = 0
    prior = bytearray(stride)
    for _ in range(height):
        if cursor + 1 + stride > len(raw):
            raise ValueError('PNG scanline is truncated')
        filter_id = raw[cursor]
        scan = bytearray(raw[cursor + 1:cursor + 1 + stride])
        cursor += 1 + stride
        _paeth_unfilter(filter_id, scan, prior, channels)
        prior = scan
        rows.append(bytes(scan))
    rgb = bytearray(width * height * 3)
    out = 0
    for row in rows:
        i = 0
        for _ in range(width):
            if color_type == 0:
                gray = row[i]
                rgb[out:out + 3] = bytes((gray, gray, gray))
                i += 1
            elif color_type == 2:
                rgb[out:out + 3] = row[i:i + 3]
                i += 3
            elif color_type == 4:
                gray = row[i]
                rgb[out:out + 3] = bytes((gray, gray, gray))
                i += 2
            else:
                rgb[out:out + 3] = row[i:i + 3]
                i += 4
            out += 3
    return width, height, bytes(rgb)


def _paeth_unfilter(filter_id, scan, prior, bpp):
    length = len(scan)
    if filter_id == 0:
        return
    if filter_id == 1:
        for i in range(length):
            left = scan[i - bpp] if i >= bpp else 0
            scan[i] = (scan[i] + left) & 255
        return
    if filter_id == 2:
        for i in range(length):
            scan[i] = (scan[i] + prior[i]) & 255
        return
    if filter_id == 3:
        for i in range(length):
            left = scan[i - bpp] if i >= bpp else 0
            scan[i] = (scan[i] + ((left + prior[i]) // 2)) & 255
        return
    if filter_id == 4:
        for i in range(length):
            a = scan[i - bpp] if i >= bpp else 0
            b = prior[i]
            c = prior[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            scan[i] = (scan[i] + pr) & 255
        return
    raise ValueError('PNG filter is unsupported')


def average_hash(data):
    width, height, rgb = decode_png_rgb(data)
    samples = []
    totals = [0, 0, 0]
    count = max(width * height, 1)
    for i in range(0, len(rgb), 3):
        totals[0] += rgb[i]
        totals[1] += rgb[i + 1]
        totals[2] += rgb[i + 2]
    for y in range(HASH_SIZE):
        src_y = min(height - 1, (y * height) // HASH_SIZE)
        for x in range(HASH_SIZE):
            src_x = min(width - 1, (x * width) // HASH_SIZE)
            i = (src_y * width + src_x) * 3
            samples.append((rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000)
    mean = sum(samples) / len(samples)
    bits = 0
    for index, value in enumerate(samples):
        if value >= mean:
            bits |= 1 << index
    color = tuple(value // count for value in totals)
    return (bits, color)


def hash_distance(left, right):
    left_bits = left[0] if isinstance(left, tuple) else left
    right_bits = right[0] if isinstance(right, tuple) else right
    return bin((left_bits or 0) ^ (right_bits or 0)).count('1')


def change_score(left, right):
    bit_score = hash_distance(left, right) / float(HASH_SIZE * HASH_SIZE)
    left_color = left[1] if isinstance(left, tuple) and len(left) > 1 else None
    right_color = right[1] if isinstance(right, tuple) and len(right) > 1 else None
    if left_color is not None and right_color is not None:
        color_score = sum(abs(a - b) for a, b in zip(left_color, right_color)) / 765.0
        return max(bit_score, color_score)
    return bit_score


def mean_rgb(data):
    width, height, rgb = decode_png_rgb(data)
    count = width * height
    if count <= 0:
        return (0, 0, 0)
    totals = [0, 0, 0]
    for i in range(0, len(rgb), 3):
        totals[0] += rgb[i]
        totals[1] += rgb[i + 1]
        totals[2] += rgb[i + 2]
    return tuple(value // count for value in totals)


def encode_rgb_png(width, height, rgb):
    if width < 1 or height < 1 or len(rgb) != width * height * 3:
        raise ValueError('RGB payload does not match dimensions')
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw.extend(rgb[y * stride:(y + 1) * stride])
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    chunks = [_chunk(b'IHDR', ihdr), _chunk(b'IDAT', zlib.compress(bytes(raw), 9)), _chunk(b'IEND', b'')]
    return PNG_SIG + b''.join(chunks)


def solid_png(width, height, red, green, blue):
    pixel = bytes((red & 255, green & 255, blue & 255))
    return encode_rgb_png(width, height, pixel * (width * height))


def _chunk(kind, payload):
    body = kind + payload
    return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)
