from math import gcd

from Crypto.Util.number import bytes_to_long, getPrime, isPrime


def gen_primes(nbit, balance):
    p = 2
    while len(bin(p)[2:]) < nbit - 2 * balance:
        factor = getPrime(balance)
        p *= factor
    left_bit = (nbit - len(bin(p)[2:])) // 2

    while True:
        r, s = [getPrime(left_bit) for _ in "01"]
        _p = p * r * s
        if len(bin(_p)[2:]) < nbit:
            left_bit += 1
        if len(bin(_p)[2:]) > nbit:
            left_bit -= 1
        if isPrime(_p + 1):
            p = _p + 1
            break

    return p


NBITS = 2048
BALANCE = 22
EXPONENT = 65537


def encrypt(message: bytes) -> dict[str, int]:
    p = gen_primes(NBITS // 2, BALANCE)
    q = gen_primes(NBITS // 2, BALANCE)
    if p == q or gcd(EXPONENT, (p - 1) * (q - 1)) != 1:
        raise ValueError("Generated parameters do not permit decryption")
    n = p * q
    m = bytes_to_long(message)
    if m >= n:
        raise ValueError("Message is too large for the generated modulus")
    return {"n": n, "c": pow(m, EXPONENT, n)}
