FROM ubuntu:22.04 AS base

FROM pwn.red/jail

COPY --from=base / /srv
COPY ./src/scanner /srv/app/run
COPY ./flag.txt /srv/app/
ENV JAIL_TIME=60 JAIL_MEM=100M JAIL_PORT=4010 JAIL_CPU=1000
