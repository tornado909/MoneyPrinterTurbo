# Use a supported Debian 12 runtime. Bullseye package metadata can reference
# security package revisions that are no longer present on the mirror, causing
# reproducible 404 failures while installing ffmpeg/git.
FROM python:3.11-slim-bookworm

WORKDIR /MoneyPrinterTurbo
RUN chmod 777 /MoneyPrinterTurbo

ENV PYTHONPATH="/MoneyPrinterTurbo"

# Local users may prefer mainland mirrors. Release/GHCR builds pass
# DOCKER_BUILD_MIRROR=default and PIP_USE_OFFICIAL=1.
ARG DOCKER_BUILD_MIRROR=china
ARG PIP_USE_OFFICIAL=0

# Keep the image deterministic on Debian 12 and retry transient repository
# errors. For the optional China mode we replace the Debian deb822 source file
# with a small bookworm sources.list; default mode keeps the source definition
# shipped by the official python image.
RUN set -eu; \
    configure_china_mirror() { \
        rm -f /etc/apt/sources.list.d/debian.sources; \
        printf '%s\n' \
          'deb https://mirrors.aliyun.com/debian bookworm main' \
          'deb https://mirrors.aliyun.com/debian bookworm-updates main' \
          'deb https://mirrors.aliyun.com/debian-security bookworm-security main' \
          > /etc/apt/sources.list; \
    }; \
    install_system_dependencies() { \
        apt-get -o Acquire::Retries=5 update; \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            git ffmpeg ca-certificates; \
    }; \
    if [ "$DOCKER_BUILD_MIRROR" = "china" ]; then \
        configure_china_mirror; \
        if ! install_system_dependencies; then \
            echo "Aliyun mirror failed; restoring official Debian bookworm sources" >&2; \
            rm -f /etc/apt/sources.list; \
            printf '%s\n' \
              'deb https://deb.debian.org/debian bookworm main' \
              'deb https://deb.debian.org/debian bookworm-updates main' \
              'deb https://deb.debian.org/debian-security bookworm-security main' \
              > /etc/apt/sources.list; \
            install_system_dependencies; \
        fi; \
    else \
        install_system_dependencies; \
    fi; \
    git --version; \
    ffmpeg -version | head -n 1; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN if [ "$PIP_USE_OFFICIAL" = "1" ]; then \
        pip install --no-cache-dir --retries 3 --timeout 60 -r requirements.txt; \
    else \
        pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --retries 3 --timeout 60 -r requirements.txt || \
        pip install --no-cache-dir -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/ --trusted-host mirrors.tuna.tsinghua.edu.cn --retries 3 --timeout 60 -r requirements.txt || \
        pip install --no-cache-dir --retries 3 --timeout 60 -r requirements.txt; \
    fi

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "./webui/Main.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.serverAddress=127.0.0.1", "--server.enableCORS=True", "--browser.gatherUsageStats=False", "--client.toolbarMode=minimal", "--logger.hideWelcomeMessage=True", "--server.showEmailPrompt=False"]
