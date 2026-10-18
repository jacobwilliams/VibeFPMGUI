#!/bin/bash
#
# Build VibeFPMGUI's Linux Docker image and run the app in it, its window
# shown on the Mac by XQuartz. See docker/README.md for the details.
#
# Needs: Docker Desktop running, and XQuartz running with "Allow connections
# from network clients" ticked (XQuartz → Settings → Security).
#
#   docker/build-docker.sh
#
# Works from any folder: it goes to the project's folder first (the paths
# below are relative to it).

set -e  # (a step that fails stops it: a failed build doesn't run the old image)
cd "$(dirname "$0")/.."

# Build the image (named "vibefpmgui") from docker/Dockerfile, with the
# project's folder as what it's built from: Ubuntu, the system libraries Qt
# needs, git, and the project's pixi environment. The first build downloads a
# few GB; later ones reuse what's built unless pixi.toml or pixi.lock change.
docker build -t vibefpmgui -f docker/Dockerfile .

# Run the test suite in the container (no display needed: Qt's offscreen
# platform). Takes a few minutes; uncomment to run it before the app.
# docker run --rm --init vibefpmgui pixi run test

# Let programs connecting from this Mac (the container's, through Docker)
# show windows on XQuartz. Needed each time XQuartz starts.
xhost +localhost

# Run the app in a new container from the image: --rm removes the container
# when the app quits, --init gives it a process that cleans up the programs
# it runs once they end (fpm, gdb, tests), -it keeps it attached to this
# terminal (Ctrl-C stops it), the two security options let gdb debug the
# programs (Debug → Start Debugging), and DISPLAY tells Qt to show the window
# on XQuartz on the Mac (host.docker.internal is the Mac, seen from the container).
docker run --rm --init -it --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -e DISPLAY=host.docker.internal:0 vibefpmgui
