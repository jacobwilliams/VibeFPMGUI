#!/bin/bash
#
# Build VibeFPMGUI's Linux Docker image and run the app in it, its window
# shown on the Mac by XQuartz. See docker/README.md for the details.
#
# Needs: Docker Desktop running, and XQuartz running with "Allow connections
# from network clients" ticked (XQuartz → Settings → Security).
# Run it from the project's folder (the paths below are relative to it).

# Build the image (named "vibefpmgui") from docker/Dockerfile, with the
# project's folder as what it's built from: Ubuntu, the system libraries Qt
# needs, git, and the project's pixi environment. The first build downloads a
# few GB; later ones reuse what's built unless pixi.toml or pixi.lock change.
docker build -t vibefpmgui -f docker/Dockerfile .

# Run the test suite in the container (no display needed: Qt's offscreen
# platform). Takes a few minutes; uncomment to run it before the app.
# docker run --rm vibefpmgui pixi run test

# Let programs connecting from this Mac (the container's, through Docker)
# show windows on XQuartz. Needed each time XQuartz starts.
xhost +localhost

# Run the app in a new container from the image: --rm removes the container
# when the app quits, -it keeps it attached to this terminal (Ctrl-C stops
# it), and DISPLAY tells Qt to show the window on XQuartz on the Mac
# (host.docker.internal is the Mac, seen from the container).
docker run --rm -it -e DISPLAY=host.docker.internal:0 vibefpmgui
