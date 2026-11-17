"""Minimal DTLS 1.2 PSK client for the Hue Entertainment API, in pure Python.

The Hue Bridge only accepts DTLS 1.2 with TLS_PSK_WITH_AES_128_GCM_SHA256 on
UDP port 2100. TouchDesigner's Python has no DTLS, so this module implements
exactly that one cipher suite with the standard library only:

  * AES-128 (encryption direction only, which is all GCM needs)
  * AES-GCM (RFC 5288 record protection)
  * TLS 1.2 PRF with HMAC-SHA256
  * DTLS 1.2 client handshake (RFC 6347) with cookie exchange and retransmission

It is a client for one known server, not a general TLS library: no certificates,
no renegotiation, no session resumption, no extensions.
"""
import hashlib
import hmac
import os
import socket
import struct
import time

CIPHER_TLS_PSK_WITH_AES_128_GCM_SHA256 = 0x00A8
DTLS12 = b'\xfe\xfd'

CT_CHANGE_CIPHER_SPEC = 20
CT_ALERT = 21
CT_HANDSHAKE = 22
CT_APPLICATION_DATA = 23

HT_CLIENT_HELLO = 1
HT_SERVER_HELLO = 2
HT_HELLO_VERIFY_REQUEST = 3
HT_SERVER_KEY_EXCHANGE = 12
HT_SERVER_HELLO_DONE = 14
HT_CLIENT_KEY_EXCHANGE = 16
HT_FINISHED = 20

ALERTS = {0: 'close_notify', 10: 'unexpected_message', 20: 'bad_record_mac', 40: 'handshake_failure',
          47: 'illegal_parameter', 50: 'decode_error', 51: 'decrypt_error', 70: 'protocol_version',
          80: 'internal_error', 115: 'unknown_psk_identity'}


class DtlsError(Exception):
    pass


# --------------------------------------------------------------------------- AES-128
def _xtime(a):
    a <<= 1
    return (a ^ 0x11B) if a & 0x100 else a


def _build_tables():
    sbox = [0] * 256
    p = q = 1
    # Classic generator-based S-box construction.
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    t0 = []
    for s in sbox:
        s2 = _xtime(s)
        s3 = s2 ^ s
        t0.append((s2 << 24) | (s << 16) | (s << 8) | s3)
    t1 = [((t >> 8) | (t << 24)) & 0xFFFFFFFF for t in t0]
    t2 = [((t >> 16) | (t << 16)) & 0xFFFFFFFF for t in t0]
    t3 = [((t >> 24) | (t << 8)) & 0xFFFFFFFF for t in t0]
    return sbox, t0, t1, t2, t3


_SBOX, _T0, _T1, _T2, _T3 = _build_tables()


class AES128:
    """AES-128 block encryption (FIPS-197) using 32-bit T-tables."""

    def __init__(self, key):
        if len(key) != 16:
            raise ValueError('AES-128 needs a 16-byte key')
        w = list(struct.unpack('>4I', key))
        rcon = 1
        for i in range(4, 44):
            t = w[i - 1]
            if i % 4 == 0:
                t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
                t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 255] << 16) | (_SBOX[(t >> 8) & 255] << 8) | _SBOX[t & 255]
                t ^= rcon << 24
                rcon = _xtime(rcon)
            w.append(w[i - 4] ^ t)
        self.rk = w

    def encrypt_block(self, block):
        rk = self.rk
        s0, s1, s2, s3 = struct.unpack('>4I', block)
        s0 ^= rk[0]; s1 ^= rk[1]; s2 ^= rk[2]; s3 ^= rk[3]
        T0, T1, T2, T3 = _T0, _T1, _T2, _T3
        k = 4
        for _ in range(9):
            t0 = T0[s0 >> 24] ^ T1[(s1 >> 16) & 255] ^ T2[(s2 >> 8) & 255] ^ T3[s3 & 255] ^ rk[k]
            t1 = T0[s1 >> 24] ^ T1[(s2 >> 16) & 255] ^ T2[(s3 >> 8) & 255] ^ T3[s0 & 255] ^ rk[k + 1]
            t2 = T0[s2 >> 24] ^ T1[(s3 >> 16) & 255] ^ T2[(s0 >> 8) & 255] ^ T3[s1 & 255] ^ rk[k + 2]
            t3 = T0[s3 >> 24] ^ T1[(s0 >> 16) & 255] ^ T2[(s1 >> 8) & 255] ^ T3[s2 & 255] ^ rk[k + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
            k += 4
        S = _SBOX
        o0 = ((S[s0 >> 24] << 24) | (S[(s1 >> 16) & 255] << 16) | (S[(s2 >> 8) & 255] << 8) | S[s3 & 255]) ^ rk[40]
        o1 = ((S[s1 >> 24] << 24) | (S[(s2 >> 16) & 255] << 16) | (S[(s3 >> 8) & 255] << 8) | S[s0 & 255]) ^ rk[41]
        o2 = ((S[s2 >> 24] << 24) | (S[(s3 >> 16) & 255] << 16) | (S[(s0 >> 8) & 255] << 8) | S[s1 & 255]) ^ rk[42]
        o3 = ((S[s3 >> 24] << 24) | (S[(s0 >> 16) & 255] << 16) | (S[(s1 >> 8) & 255] << 8) | S[s2 & 255]) ^ rk[43]
        return struct.pack('>4I', o0, o1, o2, o3)


# --------------------------------------------------------------------------- GCM
_R = 0xE1 << 120


def _gf_mult(x, y):
    z = 0
    v = y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        v = (v >> 1) ^ _R if v & 1 else v >> 1
    return z


class AESGCM:
    """AES-128-GCM (NIST SP 800-38D) with 96-bit nonces and 128-bit tags."""

    def __init__(self, key):
        self.aes = AES128(key)
        h = int.from_bytes(self.aes.encrypt_block(b'\0' * 16), 'big')
        # Per-byte-position tables: X*H = XOR of table[pos][byte] (multiplication is linear).
        self.table = []
        for pos in range(16):
            shift = 8 * (15 - pos)
            basis = [_gf_mult(1 << (shift + bit), h) for bit in range(8)]
            row = [0] * 256
            for b in range(1, 256):
                low = b & -b
                row[b] = row[b ^ low] ^ basis[low.bit_length() - 1]
            self.table.append(row)

    def _mul_h(self, x):
        t = self.table
        z = 0
        for pos in range(16):
            z ^= t[pos][(x >> (8 * (15 - pos))) & 255]
        return z

    def _ghash(self, aad, ct):
        y = 0
        for data in (aad, ct):
            for i in range(0, len(data), 16):
                block = data[i:i + 16]
                if len(block) < 16:
                    block = block + b'\0' * (16 - len(block))
                y = self._mul_h(y ^ int.from_bytes(block, 'big'))
        y = self._mul_h(y ^ (((len(aad) * 8) << 64) | (len(ct) * 8)))
        return y

    def _ctr(self, nonce, data, start):
        out = bytearray()
        counter = start
        enc = self.aes.encrypt_block
        for i in range(0, len(data), 16):
            ks = enc(nonce + struct.pack('>I', counter))
            chunk = data[i:i + 16]
            out += bytes(a ^ b for a, b in zip(chunk, ks))
            counter = (counter + 1) & 0xFFFFFFFF
        return bytes(out)

    def encrypt(self, nonce, plaintext, aad):
        if len(nonce) != 12:
            raise ValueError('GCM nonce must be 12 bytes')
        ct = self._ctr(nonce, plaintext, 2)
        s = self._ghash(aad, ct)
        tag = int.from_bytes(self.aes.encrypt_block(nonce + b'\0\0\0\1'), 'big') ^ s
        return ct + tag.to_bytes(16, 'big')

    def decrypt(self, nonce, data, aad):
        if len(data) < 16:
            raise DtlsError('Ciphertext too short')
        ct, tag = data[:-16], data[-16:]
        s = self._ghash(aad, ct)
        expect = int.from_bytes(self.aes.encrypt_block(nonce + b'\0\0\0\1'), 'big') ^ s
        if not hmac.compare_digest(expect.to_bytes(16, 'big'), tag):
            raise DtlsError('bad_record_mac (GCM tag mismatch)')
        return self._ctr(nonce, ct, 2)


# --------------------------------------------------------------------------- TLS 1.2 PRF
def prf(secret, label, seed, size):
    """TLS 1.2 PRF: P_SHA256(secret, label + seed)."""
    seed = label + seed
    out = b''
    a = seed
    while len(out) < size:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:size]


def psk_premaster(psk):
    """RFC 4279 plain PSK premaster secret."""
    n = len(psk)
    return struct.pack('>H', n) + b'\0' * n + struct.pack('>H', n) + psk


# --------------------------------------------------------------------------- client
class DtlsPskClient:
    """Blocking DTLS 1.2 client. Use from one thread (the sender thread)."""

    def __init__(self, host, identity, psk, port=2100, timeout=1.0, retries=6):
        self.addr = (host, port)
        self.identity = identity.encode() if isinstance(identity, str) else identity
        self.psk = psk
        self.timeout = timeout
        self.retries = retries
        self.sock = None
        self.epoch = 0
        self.write_seq = 0
        self.message_seq = 0
        self.transcript = b''
        self.write_aead = None
        self.read_aead = None
        self.write_iv = b''
        self.read_iv = b''
        self.connected = False

    # ---- record layer
    def _record(self, ctype, payload):
        seq = struct.pack('>H', self.epoch) + self.write_seq.to_bytes(6, 'big')
        self.write_seq += 1
        if self.epoch and self.write_aead is not None:
            nonce = self.write_iv + seq
            aad = seq + bytes([ctype]) + DTLS12 + struct.pack('>H', len(payload))
            payload = seq + self.write_aead.encrypt(nonce, payload, aad)
        return bytes([ctype]) + DTLS12 + seq + struct.pack('>H', len(payload)) + payload

    def _parse_records(self, datagram):
        records = []
        i = 0
        while i + 13 <= len(datagram):
            ctype = datagram[i]
            epoch, = struct.unpack('>H', datagram[i + 3:i + 5])
            seq = datagram[i + 3:i + 11]
            length, = struct.unpack('>H', datagram[i + 11:i + 13])
            body = datagram[i + 13:i + 13 + length]
            i += 13 + length
            if len(body) != length:
                break
            if epoch == 1:
                if self.read_aead is None or len(body) < 24:
                    continue
                explicit, data = body[:8], body[8:]
                aad = seq + bytes([ctype]) + DTLS12 + struct.pack('>H', len(data) - 16)
                body = self.read_aead.decrypt(self.read_iv + explicit, data, aad)
            elif epoch != 0:
                continue
            if ctype == CT_ALERT and len(body) >= 2:
                raise DtlsError('Bridge sent DTLS alert: ' + ALERTS.get(body[1], str(body[1])))
            records.append((ctype, epoch, body))
        return records

    # ---- handshake messages
    def _handshake(self, htype, body, seq=None):
        seq = self.message_seq if seq is None else seq
        self.message_seq = max(self.message_seq, seq + 1)
        n = len(body)
        msg = bytes([htype]) + n.to_bytes(3, 'big') + struct.pack('>H', seq) + b'\0\0\0' + n.to_bytes(3, 'big') + body
        return msg

    def _client_hello(self, cookie):
        # The retry after HelloVerifyRequest must reuse the same random (RFC 6347 4.2.1).
        return (DTLS12 + self.client_random + b'\x00' + bytes([len(cookie)]) + cookie +
                struct.pack('>HH', 2, CIPHER_TLS_PSK_WITH_AES_128_GCM_SHA256) + b'\x01\x00')

    def _exchange(self, flight, want):
        """Send `flight` (list of (ctype, payload, encrypt_epoch)) and collect handshake
        messages until `want(messages)` is satisfied. Retransmits on timeout."""
        messages = {}
        fragments = {}
        for attempt in range(self.retries):
            for ctype, payload in flight:
                self.sock.send(self._record(ctype, payload))
            deadline = time.monotonic() + self.timeout * (2 ** min(attempt, 3))
            while time.monotonic() < deadline:
                self.sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    datagram = self.sock.recv(4096)
                except socket.timeout:
                    break
                except OSError as e:
                    raise DtlsError('Socket error: ' + str(e))
                for ctype, epoch, body in self._parse_records(datagram):
                    if ctype != CT_HANDSHAKE:
                        continue
                    j = 0
                    while j + 12 <= len(body):
                        htype = body[j]
                        length = int.from_bytes(body[j + 1:j + 4], 'big')
                        mseq, = struct.unpack('>H', body[j + 4:j + 6])
                        off = int.from_bytes(body[j + 6:j + 9], 'big')
                        flen = int.from_bytes(body[j + 9:j + 12], 'big')
                        frag = body[j + 12:j + 12 + flen]
                        j += 12 + flen
                        if mseq in messages:
                            continue
                        # Reassemble possibly fragmented messages by offset.
                        parts = fragments.setdefault(mseq, {})
                        parts[off] = frag
                        got = b''
                        while len(got) in parts and parts[len(got)]:
                            got += parts[len(got)]
                        if len(got) >= length:
                            messages[mseq] = (htype, got[:length])
                if want(messages):
                    return messages
        raise DtlsError('Handshake timed out (no answer from %s:%d)' % self.addr)

    def _full(self, htype, seq, body):
        n = len(body)
        return bytes([htype]) + n.to_bytes(3, 'big') + struct.pack('>H', seq) + b'\0\0\0' + n.to_bytes(3, 'big') + body

    def connect(self):
        """Run the handshake. On failure the socket is closed and DtlsError raised."""
        try:
            self._connect()
        except Exception:
            self.close()
            raise

    def _connect(self):
        self.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect(self.addr)
        self.epoch = 0
        self.write_seq = 0
        self.message_seq = 0
        self.write_aead = self.read_aead = None
        self.connected = False

        # Flight 1: ClientHello without cookie.
        self.client_random = struct.pack('>I', int(time.time())) + os.urandom(28)
        ch1 = self._handshake(HT_CLIENT_HELLO, self._client_hello(b''), 0)

        def first_reply(m):
            return any(v[0] in (HT_HELLO_VERIFY_REQUEST, HT_SERVER_HELLO_DONE) for v in m.values())
        replies = self._exchange([(CT_HANDSHAKE, ch1)], first_reply)

        server_msgs = sorted(replies.items())
        hvr = [v for _, v in server_msgs if v[0] == HT_HELLO_VERIFY_REQUEST]
        if hvr:
            body = hvr[0][1]
            cookie = body[3:3 + body[2]]
            # Flight 3: ClientHello with cookie. The first ClientHello and the
            # HelloVerifyRequest are not part of the transcript (RFC 6347 4.2.6).
            ch2 = self._handshake(HT_CLIENT_HELLO, self._client_hello(cookie), 1)
            self.transcript = ch2

            def done(m):
                return any(v[0] == HT_SERVER_HELLO_DONE for v in m.values())
            replies = self._exchange([(CT_HANDSHAKE, ch2)], done)
            server_msgs = sorted((seq, v) for seq, v in replies.items() if v[0] != HT_HELLO_VERIFY_REQUEST)
        else:
            self.transcript = ch1

        server_random = None
        for seq, (htype, body) in server_msgs:
            if htype == HT_HELLO_VERIFY_REQUEST:
                continue
            self.transcript += self._full(htype, seq, body)
            if htype == HT_SERVER_HELLO:
                if body[:2] != DTLS12:
                    raise DtlsError('Bridge did not accept DTLS 1.2')
                server_random = body[2:34]
                sid_len = body[34]
                suite, = struct.unpack('>H', body[35 + sid_len:37 + sid_len])
                if suite != CIPHER_TLS_PSK_WITH_AES_128_GCM_SHA256:
                    raise DtlsError('Unexpected cipher suite 0x%04x' % suite)
            if htype == HT_SERVER_HELLO_DONE:
                break
        if server_random is None:
            raise DtlsError('No ServerHello received')

        # Keys.
        master = prf(psk_premaster(self.psk), b'master secret', self.client_random + server_random, 48)
        block = prf(master, b'key expansion', server_random + self.client_random, 40)
        client_key, server_key = block[0:16], block[16:32]
        self.write_iv, self.read_iv = block[32:36], block[36:40]

        # Flight 5: ClientKeyExchange, ChangeCipherSpec, Finished.
        cke = self._handshake(HT_CLIENT_KEY_EXCHANGE, struct.pack('>H', len(self.identity)) + self.identity)
        self.transcript += cke
        verify = prf(master, b'client finished', hashlib.sha256(self.transcript).digest(), 12)
        fin = self._handshake(HT_FINISHED, verify)
        self.transcript += fin
        expected_server = prf(master, b'server finished', hashlib.sha256(self.transcript).digest(), 12)

        self.write_aead = AESGCM(client_key)
        self.read_aead = AESGCM(server_key)

        # Epoch-0 records continue their sequence; epoch 1 starts at 0. Each record
        # (including retransmissions) gets a fresh sequence number in its epoch.
        sequences = {0: self.write_seq, 1: 0}
        flight5 = ((CT_HANDSHAKE, cke, 0), (CT_CHANGE_CIPHER_SPEC, b'\x01', 0), (CT_HANDSHAKE, fin, 1))

        def send_flight():
            for ctype, payload, epoch in flight5:
                self.epoch = epoch
                self.write_seq = sequences[epoch]
                self.sock.send(self._record(ctype, payload))
                sequences[epoch] = self.write_seq
            self.epoch = 1
            self.write_seq = sequences[1]

        messages = {}
        for attempt in range(self.retries):
            send_flight()
            deadline = time.monotonic() + self.timeout * (2 ** min(attempt, 3))
            while time.monotonic() < deadline:
                self.sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    datagram = self.sock.recv(4096)
                except socket.timeout:
                    break
                for ctype, epoch, body in self._parse_records(datagram):
                    if ctype == CT_HANDSHAKE and epoch == 1 and len(body) >= 12 and body[0] == HT_FINISHED:
                        flen = int.from_bytes(body[9:12], 'big')
                        messages['fin'] = body[12:12 + flen]
                if 'fin' in messages:
                    break
            if 'fin' in messages:
                break
        else:
            raise DtlsError('No Finished from Bridge (wrong client key / identity?)')
        if not hmac.compare_digest(messages['fin'], expected_server):
            raise DtlsError('Server Finished verification failed')
        self.epoch = 1
        self.write_seq = sequences[1]
        self.connected = True
        self.sock.setblocking(False)

    # ---- application data
    def send(self, data):
        if not self.connected:
            raise DtlsError('Not connected')
        self.sock.send(self._record(CT_APPLICATION_DATA, data))

    def poll_alert(self):
        """Non-blocking check for an alert from the Bridge (e.g. close_notify)."""
        if not self.connected:
            return None
        try:
            datagram = self.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError as e:
            return str(e)
        try:
            self._parse_records(datagram)
        except DtlsError as e:
            return str(e)
        return None

    def close(self):
        if self.sock is not None:
            try:
                if self.connected:
                    self.sock.send(self._record(CT_ALERT, b'\x01\x00'))
            except OSError:
                pass
            self.sock.close()
        self.sock = None
        self.connected = False
