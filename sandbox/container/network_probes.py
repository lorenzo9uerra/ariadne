"""Network probes: outbound connections and DNS lookups must fail.

Arguments: the addresses of the network's gateway and of the Docker host,
whose SSH and Docker API ports must be unreachable.
"""

import socket
import sys
from pathlib import Path

routes = Path("/proc/net/route").read_text().splitlines()[1:]
assert not any(row.split()[1] == "00000000" for row in routes), (
    "Unexpected default route"
)
assert Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").read_text().strip() == "1"
for name in ("example.com", "host.docker.internal", "gateway.docker.internal"):
    try:
        socket.getaddrinfo(name, 443)
    except socket.gaierror:
        continue
    raise AssertionError("Unexpected external or host name resolution")
for address in sys.argv[1:]:
    for port in (22, 2375, 2376):
        try:
            socket.create_connection((address, port), timeout=1).close()
        except OSError:
            continue
        raise AssertionError(f"Unexpected connection to {address}:{port}")
print("PASS: no default route, IPv6 disabled, external and host DNS denied")
print("PASS: gateway and Docker host unreachable")
