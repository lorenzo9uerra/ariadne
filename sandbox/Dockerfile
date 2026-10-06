# Use a pinned Debian snapshot for all system packages.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

COPY tool-versions.env /etc/ariadne-tools.env
RUN . /etc/ariadne-tools.env \
    && case "$(dpkg --print-architecture)" in \
        amd64) platform_packages="$SYSTEM_PACKAGES_AMD64" ;; \
        arm64) platform_packages="$SYSTEM_PACKAGES_ARM64" ;; \
        *) exit 1 ;; \
    esac \
    # Plain HTTP needs no CA certificates in the minimal base; apt still verifies
    # every package against Debian's archive signing keys.
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm-updates main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s/ bookworm-security main\n' \
        "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends $SYSTEM_PACKAGES $platform_packages \
    && dpkg-query -W > /opt/system-packages.txt \
    && rm -rf /var/lib/apt/lists/*

RUN . /etc/ariadne-tools.env \
    && case "$(dpkg --print-architecture)" in \
        amd64) uv_arch=x86_64; uv_sha="$UV_SHA256_AMD64"; jdk_url="$JDK_URL_AMD64"; jdk_sha="$JDK_SHA256_AMD64" ;; \
        arm64) uv_arch=aarch64; uv_sha="$UV_SHA256_ARM64"; jdk_url="$JDK_URL_ARM64"; jdk_sha="$JDK_SHA256_ARM64" ;; \
        *) exit 1 ;; \
    esac \
    && curl -fLsS "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_arch-unknown-linux-gnu.tar.gz" -o /tmp/uv.tar.gz \
    && printf '%s  /tmp/uv.tar.gz\n' "$uv_sha" | sha256sum -c - \
    && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 --wildcards '*/uv' \
    && curl -fLsS "$jdk_url" -o /tmp/jdk.tar.gz \
    && printf '%s  /tmp/jdk.tar.gz\n' "$jdk_sha" | sha256sum -c - \
    && mkdir /opt/java \
    && tar -xzf /tmp/jdk.tar.gz -C /opt/java --strip-components=1 \
    && curl -fLsS "$GHIDRA_URL" -o /tmp/ghidra.zip \
    && printf '%s  /tmp/ghidra.zip\n' "$GHIDRA_SHA256" | sha256sum -c - \
    && unzip -q /tmp/ghidra.zip -d /opt \
    && mv "/opt/$GHIDRA_DIRECTORY" /opt/ghidra \
    && if [ "$uv_arch" = aarch64 ]; then \
        make -C /opt/ghidra/Ghidra/Features/Decompiler/src/decompile/cpp -j2 ghidra_opt ARCH_TYPE= OSDIR=linux_arm_64 \
        && mkdir -p /opt/ghidra/Ghidra/Features/Decompiler/os/linux_arm_64 \
        && cp /opt/ghidra/Ghidra/Features/Decompiler/src/decompile/cpp/ghidra_opt /opt/ghidra/Ghidra/Features/Decompiler/os/linux_arm_64/decompile; \
    fi \
    && rm -rf /tmp/uv.tar.gz /tmp/jdk.tar.gz /tmp/ghidra.zip /opt/ghidra/Ghidra/Features/Decompiler/src/decompile/cpp/ghi_opt

ENV JAVA_HOME=/opt/java \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_CACHE_DIR=/tmp/uv-cache
COPY python/ /opt/agent-python/
RUN . /etc/ariadne-tools.env \
    && uv sync --project /opt/agent-python --locked --no-dev --python "$PYTHON_VERSION" \
    && rm -rf /tmp/uv-cache
COPY Decompile.java /opt/ariadne-scripts/Decompile.java
# Precompile the exporter and discard the temporary analysis data and settings.
RUN . /etc/ariadne-tools.env \
    && mkdir -p /tmp/ghidra-build/home /tmp/ghidra-build/project \
    && XDG_CONFIG_HOME=/tmp/ghidra-build/home/config \
       GHIDRA_HEADLESS_MAXMEM="$GHIDRA_HEAP" \
       GHIDRA_HEADLESS_JAVA_OPTIONS=-Duser.home=/tmp/ghidra-build/home \
       /usr/bin/timeout -k 5s 120s /opt/ghidra/support/analyzeHeadless \
         /tmp/ghidra-build/project bootstrap -import /bin/true -noanalysis \
         -scriptPath /opt/ariadne-scripts -postScript Decompile.java \
         /tmp/ghidra-build/result /tmp/ghidra-build/status '' \
         "$DECOMPILE_FUNCTION_SECONDS" "$DECOMPILE_OUTPUT_BYTES" -deleteProject \
    && test -f /tmp/ghidra-build/status \
    && cp -a "/tmp/ghidra-build/home/config/ghidra/$GHIDRA_DIRECTORY/osgi/compiled-bundles" /opt/ariadne-script-cache \
    && chmod -R a+rX /opt/ariadne-script-cache \
    && rm -rf /tmp/ghidra-build
COPY --chmod=0755 decompile /usr/local/bin/decompile

RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /workspace agent \
    && mkdir /workspace \
    && chown agent:agent /workspace

ENV HOME=/workspace \
    PATH=/opt/agent-python/.venv/bin:/opt/java/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    TERM=dumb
WORKDIR /workspace
USER 1000:1000
