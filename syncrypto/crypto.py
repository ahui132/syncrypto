#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2015 Qing Liang (https://github.com/liangqing)
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals
from __future__ import division
from io import open
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import os
import zlib
import hashlib
from struct import pack, unpack
from time import time
try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO
from .filetree import FileEntry


class InvalidKey(Exception):
    pass


class DigestMissMatch(Exception):
    pass


class UnrecognizedContent(Exception):
    pass


class VersionNotCompatible(Exception):
    pass


class Crypto:

    VERSION = 0x1

    COMPRESS = 0x1

    BUFFER_SIZE = 1024

    def __init__(self, password, key_size=32):

        self.password = password.encode("latin-1")
        self.key_size = key_size
        self.block_size = 16

    def encrypt_file(self, plain_path, encrypted_path, plain_file_entry):
        plain_fd = open(plain_path, 'rb')
        encrypted_fd = open(encrypted_path, 'wb')
        self.encrypt_fd(plain_fd, encrypted_fd, plain_file_entry)
        plain_fd.close()
        encrypted_fd.close()

    def decrypt_file(self, encrypted_path, plain_path):
        plain_fd = open(plain_path, 'wb')
        encrypted_fd = open(encrypted_path, 'rb')
        file_entry = self.decrypt_fd(encrypted_fd, plain_fd)
        plain_fd.close()
        encrypted_fd.close()
        return file_entry

    @staticmethod
    def compress_fd(in_fd, out_fd):
        data = zlib.compress(in_fd.read())
        out_fd.write(data)

    @staticmethod
    def decompress_fd(in_fd, out_fd):
        out_fd.write(zlib.decompress(in_fd.read()))

    def gen_key_and_iv(self, salt):
        d = d_i = b''
        while len(d) < self.key_size + self.block_size:
            d_i = hashlib.md5(d_i + self.password + salt).digest()
            d += d_i
        return d[:self.key_size], d[self.key_size:self.key_size+self.block_size]

    @staticmethod
    def _build_footer(file_entry):
        return file_entry.digest + \
               pack(b'!Q', file_entry.size) + \
               pack(b'!I', int(file_entry.ctime)) + \
               pack(b'!I', int(file_entry.mtime)) + \
               pack(b'!i', file_entry.mode) + (12 * b'\0')

    @staticmethod
    def _unpack_footer(pathname, footer):
        (size, ctime, mtime, mode) = unpack(b'!QIIi', footer[16:36])
        return FileEntry(pathname, size, ctime, mtime, mode,
                         footer[:16], False)

    def encrypt_fd(self, in_fd, out_fd, file_entry, flags=0):
        """
            +-----------------------------------------------------+
            | Version(1) | Flags(1) | Pathname size(2) | Salt(12) |
            +-----------------------------------------------------+
            |                  Encrypted Pathname                 |
            +-----------------------------------------------------+
            |                  Encrypted Content                  |
            |                        ...                          |
            +-----------------------------------------------------+
            |                  Encrypted Digest(16)               |
            +-----------------------------------------------------+
            |         size(8)*        |   ctime(4)   |   mtime(4) |
            +-----------------------------------------------------+
            |     mode(4)   |            padding(12)              |
            +-----------------------------------------------------+

            * size, ctime, mtime, mode are also encrypted
        """
        bs = self.block_size
        if file_entry is None:
            file_entry = FileEntry('.tmp', 0, time(), time(), 0)
        if file_entry.salt is None:
            file_entry.salt = os.urandom(bs - 4)
        key, iv = self.gen_key_and_iv(file_entry.salt)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv),
                        backend=default_backend())
        encryptor = cipher.encryptor()
        pathname = file_entry.pathname.encode("utf-8")[:2**16]
        pathname_size = len(pathname)
        pathname_padding = b''
        if pathname_size % bs != 0:
            padding_length = (bs - pathname_size % bs)
            pathname_padding = padding_length * b'\0'

        flags &= 0xFF
        out_fd.write(pack(b'BB', self.VERSION, flags))
        out_fd.write(pack(b'!H', pathname_size))
        out_fd.write(file_entry.salt)
        out_fd.write(encryptor.update(pathname+pathname_padding))

        if flags & Crypto.COMPRESS:
            buf = BytesIO()
            self.compress_fd(in_fd, buf)
            in_fd = buf
            in_fd.seek(0)

        finished = False
        md5 = hashlib.md5()
        while not finished:
            chunk = in_fd.read(Crypto.BUFFER_SIZE * bs)
            md5.update(chunk)
            if len(chunk) == 0 or len(chunk) % bs != 0:
                padding_length = (bs - len(chunk) % bs) or bs
                chunk += padding_length * pack(b'B', padding_length)
                finished = True
            out_fd.write(encryptor.update(chunk))

        file_entry.digest = md5.digest()
        out_fd.write(encryptor.update(self._build_footer(file_entry)))
        out_fd.write(encryptor.finalize())
        return file_entry

    def decrypt_fd(self, in_fd, out_fd):
        bs = self.block_size
        line = in_fd.read(bs)
        if len(line) < bs:
            raise UnrecognizedContent(
                "header line size is not correct, expect %d, got %d" %
                (bs, len(line)))
        ints = bytearray(line[:2])
        version = ints[0]
        if version > self.VERSION:
            raise VersionNotCompatible("Unrecognized version: (%d)" % version)
        flags = ints[1]
        (pathname_size,) = unpack(b'!H', line[2:4])
        salt = line[4:]
        key, iv = self.gen_key_and_iv(salt)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv),
                        backend=default_backend())
        decryptor = cipher.decryptor()
        pathname_block_size = pathname_size
        if pathname_size % bs != 0:
            pathname_block_size = int((pathname_size/bs+1) * bs)
        pathname_data = in_fd.read(pathname_block_size)
        if len(pathname_data) < pathname_block_size:
            raise UnrecognizedContent(
                "pathname length is not correct, expect %d, got %d" %
                (pathname_block_size, len(pathname_data)))
        pathname = decryptor.update(pathname_data)[:pathname_size]
        str_io = BytesIO()
        md5 = hashlib.md5()
        next_chunk = ''
        finished = False
        file_entry = None
        while not finished:
            chunk, next_chunk = next_chunk, in_fd.read(self.BUFFER_SIZE * bs)
            if chunk:
                plaintext = decryptor.update(chunk)
                if len(next_chunk) == 0:
                    plaintext += decryptor.finalize()
                    file_entry = self._unpack_footer(pathname, plaintext[-48:])
                    padding_length = bytearray(plaintext[-49:-48])[0]
                    plaintext = plaintext[:-padding_length-48]
                    finished = True
                str_io.write(plaintext)
                md5.update(plaintext)
        if flags & self.COMPRESS:
            buf = BytesIO()
            str_io.seek(0)
            self.decompress_fd(str_io, buf)
            str_io.close()
            str_io = buf
            str_io.seek(0)

        file_entry.salt = salt
        if file_entry.digest != md5.digest()[:bs]:
            raise DigestMissMatch()

        out_fd.write(str_io.getvalue())
        str_io.close()
        return file_entry
