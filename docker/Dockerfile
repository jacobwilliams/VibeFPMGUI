# VibeFPMGUI on Linux, in a container: the project's pixi environment (the
# one CI uses), git, and what Qt needs to show its window on an X11 display
# (XQuartz on a Mac). See docker/README.md.
#
# Built for the machine's own architecture (arm64 on an Apple Silicon Mac:
# pixi.lock has linux-aarch64), or, with --platform linux/amd64, for x86-64 as
# CI runs (linux-64; emulated by Rosetta on a Mac).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# git: the Git panel, and fpm fetching git dependencies. libc6-dbg: the C
# library's symbols, which Valgrind (in the pixi environment) needs on arm64
# to start. The rest: what Qt's X11 (xcb) platform plugin loads from the
# system, and fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        libgl1 libegl1 libglib2.0-0 libdbus-1-3 libfontconfig1 libfreetype6 \
        libx11-xcb1 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 \
        libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxrender1 libxi6 libsm6 libice6 \
        fonts-dejavu-core x11-apps \
        libc6-dbg \
    && rm -rf /var/lib/apt/lists/*

# pixi, the version CI uses.
ARG PIXI_VERSION=v0.80.0
RUN curl -fsSL https://pixi.sh/install.sh | PIXI_VERSION=${PIXI_VERSION} PIXI_HOME=/usr/local PIXI_NO_PATH_UPDATE=1 bash

# The source may be mounted from the Mac (owned by another user): git works in it anyway.
RUN git config --global --add safe.directory '*'

WORKDIR /app
# The environment first, in a layer of its own: rebuilt only when pixi.toml or pixi.lock change.
COPY pixi.toml pixi.lock ./
RUN pixi install --locked
COPY . .

# XQuartz has no OpenGL for remote clients: Qt draws without it.
ENV QT_XCB_GL_INTEGRATION=none

CMD ["pixi", "run", "start"]
