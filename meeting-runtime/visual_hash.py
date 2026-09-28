"""PNG decode and digests without required image libraries.

Pillow is used for the pixel stage only when it is importable (the meeting image has it);
the pure-Python path returns identical pixels and is what the host daemon runs.
"""
import hashlib
import io
import struct
import zlib


PNG_SIG = b'\x89PNG\r\n\x1a\n'
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
USE_PILLOW = True
_PILLOW = []


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def decode_png_rgb(data):
    width, height, color_type, compressed = _read_png(data)
    fast = _pillow_rgb(data, width, height) if USE_PILLOW else None
    if fast is not None:
        return width, height, fast
    raw = zlib.decompress(compressed)
    channels = CHANNELS[color_type]
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
        rows.append(scan)
    return width, height, _to_rgb(b''.join(rows), color_type, width * height)


def _read_png(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) < 24 or data[:8] != PNG_SIG:
        raise ValueError('frame is not a PNG image')
    offset = 8
    width = height = None
    color_type = None
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
            if bit_depth != 8 or color_type not in CHANNELS:
                raise ValueError('PNG color type is unsupported')
        elif kind == b'IDAT':
            idat.append(chunk)
        elif kind == b'IEND':
            break
    if width is None or not idat:
        raise ValueError('PNG is missing image data')
    return width, height, color_type, b''.join(idat)


def _pillow_rgb(data, width, height):
    if not _PILLOW:
        try:
            from PIL import Image
        except Exception:
            Image = None
        _PILLOW.append(Image)
    image_module = _PILLOW[0]
    if image_module is None:
        return None
    try:
        with image_module.open(io.BytesIO(bytes(data))) as image:
            if image.size != (width, height):
                return None
            rgb = image.convert('RGB').tobytes()
    except Exception:
        return None
    return rgb if len(rgb) == width * height * 3 else None


def _to_rgb(pixels, color_type, count):
    if color_type == 2:
        return bytes(pixels)
    rgb = bytearray(count * 3)
    if color_type == 6:
        for channel in range(3):
            rgb[channel::3] = pixels[channel::4]
    else:
        gray = pixels if color_type == 0 else pixels[0::2]
        for channel in range(3):
            rgb[channel::3] = gray
    return bytes(rgb)


def _paeth_unfilter(filter_id, scan, prior, bpp):
    length = len(scan)
    if filter_id == 0:
        return
    if filter_id in (2, 4) and not any(scan):
        # A zero Up or Paeth residual always reproduces the row above.
        scan[:] = prior
        return
    if filter_id == 1:
        for i in range(bpp, length):
            scan[i] = (scan[i] + scan[i - bpp]) & 255
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
        for i in range(min(bpp, length)):
            scan[i] = (scan[i] + prior[i]) & 255
        for i in range(bpp, length):
            a = scan[i - bpp]
            b = prior[i]
            c = prior[i - bpp]
            pa = b - c if b >= c else c - b
            pb = a - c if a >= c else c - a
            pc = a + b - c - c
            if pc < 0:
                pc = -pc
            if pa <= pb and pa <= pc:
                scan[i] = (scan[i] + a) & 255
            elif pb <= pc:
                scan[i] = (scan[i] + b) & 255
            else:
                scan[i] = (scan[i] + c) & 255
        return
    raise ValueError('PNG filter is unsupported')


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
