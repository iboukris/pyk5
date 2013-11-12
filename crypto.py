# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in
#   the documentation and/or other materials provided with the
#   distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

# XXX current status:
# * AES encryption, checksum, string2key done and tested
#   - should be enough to start doing 4120 stuff
# * PRF not done; will be needed for FAST
# * Still to do:
#   - PRF will be needed for FAST
#   - DES3, RC4, DES enctypes and cksumtypes
#   - Unkeyed checksum types
#   - Special RC4, raw DES/DES3 operations for GSSAPI
# * Difficult or low priority:
#   - Camellia not supported by PyCrypto
#   - Cipher state only needed for kcmd suite
#   - Nonstandard enctypes and cksumtypes like des-hmac-sha1

from fractions import gcd
from struct import pack, unpack
from Crypto.Cipher import AES
from Crypto.Hash import HMAC
from Crypto.Hash import SHA
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes


class Enctypenum(object):
    DES_CBC_CRC = 1
    DES_CBC_MD4 = 2
    DES_CBC_MD5 = 3
    DES3_CBC = 16
    AES128_CTS = 17
    AES256_CTS = 18
    RC4_HMAC = 23


class Checksumtypenum(object):
    CRC32 = 1
    MD4 = 2
    MD4_DES = 3
    MD5 = 7
    MD5_DES = 8
    SHA1 = 9
    SHA1_DES3 = 12
    SHA1_AES128 = 15
    SHA1_AES256 = 16
    HMAC_MD5 = -138


def _zeropad(s, padsize):
    # Return s padded with 0 bytes to a multiple of padsize.
    padlen = (padsize - (len(s) % padsize)) % padsize
    return s + '\0'*padlen


def _xorbytes(b1, b2):
    # xor two strings together and return the resulting string.
    assert len(b1) == len(b2)
    return ''.join(chr(ord(x) ^ ord(y)) for x, y in zip(b1, b2))


def _mac_equal(mac1, mac2):
    # Constant-time comparison function.  (We can't use HMAC.verify
    # since we use truncated macs.)
    assert len(mac1) == len(mac2)
    res = 0
    for x, y in zip(mac1, mac2):
        res |= ord(x) ^ ord(y)
    return res == 0


def _nfold(str, nbytes):
    # Convert str to a string of length nbytes using the RFC 3961 nfold
    # operation.

    # Rotate the bytes in str to the right by nbits bits.
    def rotate_right(str, nbits):
        nbytes, remain = (nbits//8) % len(str), nbits % 8
        return ''.join(chr((ord(str[i-nbytes]) >> remain) |
                           ((ord(str[i-nbytes-1]) << (8-remain)) & 0xff))
                       for i in xrange(len(str)))

    # Add equal-length strings together with end-around carry.
    def add_ones_complement(str1, str2):
        n = len(str1)
        v = [ord(a) + ord(b) for a, b in zip(str1, str2)]
        # Propagate carry bits to the left until there aren't any left.
        while any(x & ~0xff for x in v):
            v = [(v[i-n+1]>>8) + (v[i]&0xff) for i in xrange(n)]
        return ''.join(chr(x) for x in v)

    # Concatenate copies of str to produce the least common multiple
    # of len(str) and nbytes, rotating each copy of str to the right
    # by 13 bits times its list position.  Decompose the concatenation
    # into slices of length nbytes, and add them together as
    # big-endian ones' complement integers.
    slen = len(str)
    lcm = nbytes * slen / gcd(nbytes, slen)
    bigstr = ''.join((rotate_right(str, 13 * i) for i in xrange(lcm / slen)))
    slices = (bigstr[p:p+nbytes] for p in xrange(0, lcm, nbytes))
    return reduce(add_ones_complement, slices)


class _Enctype(object):
    # Base class for enctype profiles.  Usable enctype classes must define:
    #   * enctype: enctype number
    #   * XXX do we need protocol key size?
    #   * seedsize: random_to_key input size (in bytes) (XXX simplified only?)
    #   * random_to_key (if the keyspace is not dense)
    #   * string_to_key
    #   * encrypt
    #   * decrypt

    @classmethod
    def random_to_key(cls, seed):
        return Key(cls.enctype, seed)


class _SimplifiedEnctype(_Enctype):
    # Base class for enctypes using the RFC 3961 simplified profile.
    # Defines the encrypt and decrypt methods.  Subclasses must define:
    #   * blocksize: Underlying cipher block size in bytes
    #   * padsize: Underlying cipher padding multiple (1 or blocksize)
    #   * macsize: Size of integrity MAC in bytes
    #   * hashmod: PyCrypto hash module for underlying hash function
    #   * basic_encrypt, basic_decrypt: Underlying CBC/CTS cipher

    @classmethod
    def derive(cls, key, constant):
        # RFC 3961 only says to n-fold the constant only if it is
        # shorter than the cipher block size.  But all Unix
        # implementations n-fold constants if their length is larger
        # than the block size as well, and n-folding when the length
        # is equal to the block size is a no-op.
        plaintext = _nfold(constant, cls.blocksize)
        rndseed = ""
        while len(rndseed) < cls.seedsize:
            ciphertext = cls.basic_encrypt(key, plaintext)
            rndseed += ciphertext
            plaintext = ciphertext
        return cls.random_to_key(rndseed[0:cls.seedsize])

    @classmethod
    def encrypt(cls, key, keyusage, plaintext, confounder):
        ki = cls.derive(key, pack('>IB', keyusage, 0x55))
        ke = cls.derive(key, pack('>IB', keyusage, 0xAA))
        if confounder is None:
            confounder = get_random_bytes(cls.blocksize)
        basic_plaintext = confounder + _zeropad(plaintext, cls.padsize)
        hmac = HMAC.new(ki.contents, basic_plaintext, cls.hashmod).digest()
        return cls.basic_encrypt(ke, basic_plaintext) + hmac[:cls.macsize]

    @classmethod
    def decrypt(cls, key, keyusage, ciphertext):
        ki = cls.derive(key, pack('>IB', keyusage, 0x55))
        ke = cls.derive(key, pack('>IB', keyusage, 0xAA))
        if len(ciphertext) < cls.blocksize + cls.macsize:
            raise ValueError('ciphertext too short')
        basic_ctext, mac = ciphertext[:-cls.macsize], ciphertext[-cls.macsize:]
        if len(basic_ctext) % cls.padsize != 0:
            raise ValueError('ciphertext does not meet padding requirement')
        basic_plaintext = cls.basic_decrypt(ke, basic_ctext)
        hmac = HMAC.new(ki.contents, basic_plaintext, cls.hashmod).digest()
        expmac = hmac[:cls.macsize]
        if not _mac_equal(mac, expmac):
            raise ValueError('ciphertext integrity failure')
        # Discard the confounder.
        return basic_plaintext[cls.blocksize:]


class _AESEnctype(_SimplifiedEnctype):
    # Base class for aes128-cts and aes256-cts.
    enctype = Enctypenum.AES256_CTS
    blocksize = 16
    padsize = 1
    macsize = 12
    hashmod = SHA

    @classmethod
    def string_to_key(cls, string, salt, params):
        (iterations,) = unpack('>L', params or '\x00\x00\x10\x00')
        print repr(iterations)
        prf = lambda p, s: HMAC.new(p, s, SHA).digest()
        seed = PBKDF2(string, salt, cls.seedsize, iterations, prf)
        tkey = cls.random_to_key(seed)
        return cls.derive(tkey, "kerberos")

    @classmethod
    def basic_encrypt(cls, key, plaintext):
        assert len(plaintext) >= 16
        aes = AES.new(key.contents, AES.MODE_CBC, '\0' * 16)
        ctext = aes.encrypt(_zeropad(plaintext, 16))
        if len(plaintext) > 16:
            # Swap the last two ciphertext blocks and truncate the
            # final block to match the plaintext length.
            lastlen = len(plaintext) % 16 or 16
            ctext = ctext[:-32] + ctext[-16:] + ctext[-32:-16][:lastlen]
        return ctext

    @classmethod
    def basic_decrypt(cls, key, ciphertext):
        assert len(ciphertext) >= 16
        aes = AES.new(key.contents, AES.MODE_ECB)
        if len(ciphertext) == 16:
            return aes.decrypt(ciphertext)
        # Split the ciphertext into blocks.  The last block may be partial.
        cblocks = [ciphertext[p:p+16] for p in xrange(0, len(ciphertext), 16)]
        lastlen = len(cblocks[-1])
        # CBC-decrypt all but the last two blocks.
        prev_cblock = '\0' * 16
        plaintext = ''
        for b in cblocks[:-2]:
            plaintext += _xorbytes(aes.decrypt(b), prev_cblock)
            prev_cblock = b
        # Decrypt the second-to-last cipher block.  The left side of
        # the decrypted block will be the final block of plaintext
        # xor'd with the final partial cipher block; the right side
        # will be the omitted bytes of ciphertext from the final
        # block.
        b = aes.decrypt(cblocks[-2])
        lastplaintext =_xorbytes(b[:lastlen], cblocks[-1])
        omitted = b[lastlen:]
        # Decrypt the final cipher block plus the omitted bytes to get
        # the second-to-last plaintext block.
        plaintext += _xorbytes(aes.decrypt(cblocks[-1] + omitted), prev_cblock)
        return plaintext + lastplaintext


class _AES128CTS(_AESEnctype):
    seedsize = 16


class _AES256CTS(_AESEnctype):
    seedsize = 32


class _Checksum(object):
    # Base class for checksum profiles.  Usable checksum classes must
    # define:
    #   * checksum
    #   * verify
    pass


class _SimplifiedChecksum(_Checksum):
    # Base class for checksums using the RFC 3961 simplified profile.
    # Defines the checksum and verify methods.  Subclasses must
    # define:
    #   * macsize: Size of checksum in bytes
    #   * enc: Profile of associated enctype

    @classmethod
    def checksum(cls, key, keyusage, text):
        # NOTE: not checking key against cls.enc.enctype.
        kc = cls.enc.derive(key, pack('>IB', keyusage, 0x99))
        hmac = HMAC.new(kc.contents, text, cls.enc.hashmod).digest()
        return hmac[:cls.macsize]

    @classmethod
    def verify(cls, key, keyusage, text, cksum):
        expected = cls.checksum(key, keyusage, text)
        if not _mac_equal(cksum, expected):
            raise ValueError('checksum verification failure')


class _SHA1AES128(_SimplifiedChecksum):
    macsize = 12
    enc = _AES128CTS


class _SHA1AES256(_SimplifiedChecksum):
    macsize = 12
    enc = _AES256CTS


_enctype_table = {
    Enctypenum.AES128_CTS: _AES128CTS,
    Enctypenum.AES256_CTS: _AES256CTS
}


_checksum_table = {
    Checksumtypenum.SHA1_AES128: _SHA1AES128,
    Checksumtypenum.SHA1_AES256: _SHA1AES256
}


def _get_enctype_profile(enctype):
    if enctype not in _enctype_table:
        raise ValueError('Invalid enctype %d' % enctype)
    return _enctype_table[enctype]


def _get_checksum_profile(cksumtype):
    if cksumtype not in _checksum_table:
        raise ValueError('Invalid cksumtype %d' % cksumtype)
    return _checksum_table[cksumtype]


class Key(object):
    def __init__(self, enctype, contents):
        # NOTE: not checking length of contents against enctype.
        self.enctype = enctype
        self.contents = contents


def random_to_key(enctype, seed):
    e = _get_enctype_profile(enctype)
    if len(seed) != e.seedsize:
        raise ValueError('Wrong crypto seed length')
    return e.random_to_key(seed)


def string_to_key(enctype, string, salt, params=None):
    e = _get_enctype_profile(enctype)
    return e.string_to_key(string, salt, params)


def encrypt(key, keyusage, plaintext, confounder=None):
    e = _get_enctype_profile(key.enctype)
    return e.encrypt(key, keyusage, plaintext, confounder)


def decrypt(key, keyusage, ciphertext):
    e = _get_enctype_profile(key.enctype)
    return e.decrypt(key, keyusage, ciphertext)


def make_checksum(cksumtype, key, keyusage, text):
    c = _get_checksum_profile(cksumtype)
    return c.checksum(key, keyusage, text)


def verify_checksum(cksumtype, key, keyusage, text, cksum):
    c = _get_checksum_profile(cksumtype)
    return c.verify(key, keyusage, text, cksum)


#####
# XXX remove these tests later.

def printhex(s):
    print ''.join('\\x{0:02X}'.format(ord(c)) for c in s)

#print 'encrypt AES128'
#k=Key(17, '\x90\x62\x43\x0C\x8C\xDA\x33\x88\x92\x2E\x6D\x6A\x50\x9F\x5B\x7A')
#c=encrypt(k, 2, '9 bytesss',
#          '\x94\xB4\x91\xF4\x81\x48\x5B\x9A\x06\x78\xCD\x3C\x4E\xA3\x86\xAD')
#printhex(c)

#print 'decrypt AES128'
#k=Key(17, '\x90\x62\x43\x0C\x8C\xDA\x33\x88\x92\x2E\x6D\x6A\x50\x9F\x5B\x7A')
#p=decrypt(k, 2,
#          '\x68\xFB\x96\x79\x60\x1F\x45\xC7\x88\x57\xB2\xBF\x82\x0F\xD6\xE5'
#          '\x3E\xCA\x8D\x42\xFD\x4B\x1D\x70\x24\xA0\x92\x05\xAB\xB7\xCD\x2E'
#          '\xC2\x6C\x35\x5D\x2F')
#print p

#print 'encrypt AES256'
#k=Key(18,
#      '\xF1\xC7\x95\xE9\x24\x8A\x09\x33\x8D\x82\xC3\xF8\xD5\xB5\x67\x04'
#      '\x0B\x01\x10\x73\x68\x45\x04\x13\x47\x23\x5B\x14\x04\x23\x13\x98')
#c=encrypt(k, 4, '30 bytes bytes bytes bytes byt',
#          '\xE4\x5C\xA5\x18\xB4\x2E\x26\x6A\xD9\x8E\x16\x5E\x70\x6F\xFB\x60')
#printhex(c)

#print 'decrypt AES256'
#k=Key(18,
#      '\xF1\xC7\x95\xE9\x24\x8A\x09\x33\x8D\x82\xC3\xF8\xD5\xB5\x67\x04'
#      '\x0B\x01\x10\x73\x68\x45\x04\x13\x47\x23\x5B\x14\x04\x23\x13\x98')
#p=decrypt(k, 4,
#          '\xD1\x13\x7A\x4D\x63\x4C\xFE\xCE\x92\x4D\xBC\x3B\xF6\x79\x06\x48'
#          '\xBD\x5C\xFF\x7D\xE0\xE7\xB9\x94\x60\x21\x1D\x0D\xAE\xF3\xD7\x9A'
#          '\x29\x5C\x68\x88\x58\xF3\xB3\x4B\x9C\xBD\x6E\xEB\xAE\x81\xDA\xF6'
#          '\xB7\x34\xD4\xD4\x98\xB6\x71\x4F\x1C\x1D')
#print p

#print 'checksum SHA1AES128'
#k=Key(17, '\x90\x62\x43\x0C\x8C\xDA\x33\x88\x92\x2E\x6D\x6A\x50\x9F\x5B\x7A')
#verify_checksum(15, k, 3, 'eight nine ten eleven twelve thirteen',
#                '\x01\xA4\xB0\x88\xD4\x56\x28\xF6\x94\x66\x14\xE3')

#print 'checksum SHA1AES256'
#k=Key(18,
#      '\xB1\xAE\x4C\xD8\x46\x2A\xFF\x16\x77\x05\x3C\xC9\x27\x9A\xAC\x30'
#      '\xB7\x96\xFB\x81\xCE\x21\x47\x4D\xD3\xDD\xBC\xFE\xA4\xEC\x76\xD7')
#verify_checksum(16, k, 4, 'fourteen',
#                '\xE0\x87\x39\xE3\x27\x9E\x29\x03\xEC\x8E\x38\x36')

#print 's2k AES128'
#k=string_to_key(17, 'password', 'ATHENA.MIT.EDUraeburn', '\0\0\0\2')
#printhex(k.contents)
## \xC6\x51\xBF\x29\xE2\x30\x0A\xC2\x7F\xA4\x69\xD6\x93\xBD\xDA\x13

#print 's2k AES256'
#k=string_to_key(18, 'X'*64, 'pass phrase equals block size', '\0\0\x04\xB0')
#printhex(k.contents)
## \x89\xAD\xEE\x36\x08\xDB\x8B\xC7\x1F\x1B\xFB\xFE\x45\x94\x86\xB0
## \x56\x18\xB7\x0C\xBA\xE2\x20\x92\x53\x4E\x56\xC5\x53\xBA\x4B\x34