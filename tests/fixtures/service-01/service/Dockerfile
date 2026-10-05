# Same pinned multi-architecture base as the agent sandbox, so fixture builds
# reuse local layers and need no extra downloads.
FROM gcc:14.3.0-bookworm@sha256:5e927c284bf55a7dc796262e311a0703344f62f41f5621eb56843111b1d37e15

# The sandbox verifier's runtime probes require `python` on the target's PATH.
RUN ln -s "$(command -v python3)" /usr/local/bin/python
COPY service/server.py /opt/server.py
USER 1000:1000
WORKDIR /workspace
