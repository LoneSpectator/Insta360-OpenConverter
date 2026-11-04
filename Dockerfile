# syntax=docker/dockerfile:1
FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
# Optional combined trust bundle for builds behind the cloud session proxy.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      export SSL_CERT_FILE=/run/secrets/proxy_ca; \
      export APT_CONFIG=/tmp/apt-proxy-ca.conf; \
      printf 'Acquire::https::CaInfo "/run/secrets/proxy_ca";\n' > "$APT_CONFIG"; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates tzdata python3 python3-tomli ffmpeg libimage-exiftool-perl tini g++ \
      libegl1 libgl1 libvulkan1 libglib2.0-0 libgomp1 libxrender1 libxext6 \
      libxi6 libxrandr2 libxcursor1 libxinerama1 libsm6 libasound2 libgtk-3-0 freeglut3 \
    && rm -rf /var/lib/apt/lists/* /tmp/apt-proxy-ca.conf
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8 TZ=Asia/Shanghai \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics
WORKDIR /app
COPY --chmod=0555 converter /app/converter
COPY --chmod=0555 scripts /app/scripts
COPY --chmod=0555 native /app/native
COPY --chmod=0444 config.example.toml /app/config.example.toml
USER 1000:1000
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "converter"]
CMD ["watch"]
