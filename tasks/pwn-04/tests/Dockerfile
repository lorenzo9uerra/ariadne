ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY answers.py /tests/benchmark/answers.py
COPY verify.py /tests/benchmark/verifier.py
COPY --chmod=0755 test.sh /tests/test.sh
