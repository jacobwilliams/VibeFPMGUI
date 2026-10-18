# VibeFPMGUI in a Linux container (on a Mac)

The image (`docker/Dockerfile`) is Ubuntu 24.04 with the project's pixi
environment (the same one CI uses: gfortran, fpm, Python, PySide6…, and on
Linux gdb and Valgrind), git, the C library's debug symbols (Valgrind needs
them on arm64), and what Qt needs to show its window on an X11 display. On a Mac, XQuartz
is that display: the app's window opens beside your Mac windows.

It's built for your Mac's own architecture: arm64 Linux on Apple Silicon
(`pixi.lock` has `linux-aarch64`), at full speed. To run exactly what CI
runs (x86-64 Linux, `linux-64`), to reproduce a failure seen only there,
build and run it for `linux/amd64` instead: see the end.

## The files

| File | What it is | Use it to |
|---|---|---|
| [`Dockerfile`](Dockerfile) | The image's recipe: Ubuntu, the system libraries Qt and Valgrind need, pixi and the project's environment, the source, and settings that list the demo project in *Recent Projects*. Built as the image `vibefpmgui`. | (read by `docker build`; see [Build the image](#build-the-image)) |
| [`build-docker.sh`](build-docker.sh) | Builds the image, then runs the app in a new container, its window shown by XQuartz. | Try the app on Linux in one step: `docker/build-docker.sh` |
| [`dev.sh`](dev.sh) | Keeps a container (`vibefpmgui-dev`) running with the source you're editing mounted in it. | Run the tests, or anything, on Linux as you work: see [Developing in the container](#developing-in-the-container-dockerdevsh) |
| [`../.dockerignore`](../.dockerignore) | (In the project's folder, where Docker looks for it.) What `docker build` leaves out of the image: the Mac's `.pixi` environment, build output, caches. | (read by `docker build`) |
| `README.md` | This file. | |

All of them are run from (or work out) the project's folder, as the
commands below are.

## Quick start

With [Docker Desktop and XQuartz set up](#once), XQuartz running:

```sh
docker/build-docker.sh
```

It builds the image (the first time takes a while: see below), lets the
container show windows on XQuartz, and starts the app. The demo project
(`examples/demo`) is in *File → Projects → Recent Projects*. Quit the app
(or Ctrl-C in the terminal) to end the container. The sections below do the
same steps one at a time, with more choices.

## Once

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Install XQuartz, the X11 display, for the GUI (not needed for the tests).
   Download the installer from [xquartz.org](https://www.xquartz.org) and
   run it. Or, if you use [Homebrew](https://brew.sh) (not required):

   ```sh
   brew install --cask xquartz
   ```

   Log out and back in (it sets up `DISPLAY` then). Start XQuartz, and in
   *XQuartz → Settings → Security* tick *Allow connections from network
   clients*; quit XQuartz and start it again.

## Build the image

From the project's folder (`docker/build-docker.sh` does this first):

```sh
docker build -t vibefpmgui -f docker/Dockerfile .
```

The first build downloads the environment (a few GB, some minutes); later
ones reuse it unless `pixi.toml` or `pixi.lock` changed.

## Run the tests (no display needed)

```sh
docker run --rm --init vibefpmgui pixi run test
docker run --rm --init vibefpmgui pixi run lint
```

The tests use Qt's offscreen platform: no window appears.

## Run the app

With XQuartz running, let containers connect to it (each time XQuartz
starts), then run the app with its display (what `docker/build-docker.sh`
does after building):

```sh
xhost +localhost
docker run --rm --init -it --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -e DISPLAY=host.docker.internal:0 vibefpmgui
```

`--init` gives the container a process that cleans up the programs the app
runs (fpm, gdb, the tests) once they end; the two security options let gdb
debug programs (*Debug → Start Debugging*).

The demo project (`examples/demo`) is already in *File → Projects → Recent
Projects*: the image's settings list it, so it opens in two clicks.

(If the window doesn't appear, try `xhost + 127.0.0.1` instead of
`xhost +localhost`.) XQuartz doesn't use the Retina resolution: if the
window is small, add `-e QT_SCALE_FACTOR=2`.

### Your projects, your settings, and the source as you edit it

Folders on the Mac are mounted with `-v`. For example, the source you're
editing (instead of the copy in the image), a project to open, and a volume
for the app's settings so they're kept between runs:

```sh
docker run --rm --init -it --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -e DISPLAY=host.docker.internal:0 \
    -v "$PWD":/app -v vibefpmgui-pixi:/app/.pixi \
    -v ~/git/halo:/projects/halo \
    -v vibefpmgui-config:/root/.config \
    vibefpmgui
```

`-v vibefpmgui-pixi:/app/.pixi` matters with `-v "$PWD":/app`: it keeps the
container's Linux environment in a volume of its own (filled from the image
the first time), so it doesn't take the place of the Mac's `.pixi` in your
folder (or the other way round). After changing `pixi.toml` or `pixi.lock`,
rebuild the image and remove that volume (`docker volume rm vibefpmgui-pixi`)
so it's filled again.

Then open `/projects/halo` with *File → Open Project…*. (The settings
volume is filled from the image the first time, so the demo is in Recent
Projects then too; after that it keeps your own list.)

A shell in the container, to run anything else (`pixi run …`, `fpm`, `git`):

```sh
docker run --rm --init -it -e DISPLAY=host.docker.internal:0 vibefpmgui bash
```

## As CI runs it: x86-64 Linux

In Docker Desktop's *Settings → General*, turn on *Use Rosetta for
x86_64/amd64 emulation on Apple Silicon* (much faster than QEMU's
emulation). Then build and run a second image with `--platform`:

```sh
docker build --platform linux/amd64 -t vibefpmgui-amd64 -f docker/Dockerfile .
docker run --rm --init --platform linux/amd64 vibefpmgui-amd64 pixi run test
```

(Mounting the source with `-v "$PWD":/app`, give it its own environment
volume too, e.g. `-v vibefpmgui-pixi-amd64:/app/.pixi`: the two
architectures' environments differ.)

## Developing in the container: `docker/dev.sh`

A container that stays up, with the source you're editing on the Mac
mounted at `/app` (changes are seen at once, no rebuilding), to run the
tests (or anything else) on Linux as you go. Its Linux environment is kept
in a volume of its own, `vibefpmgui-pixi`, so it doesn't take the place of
the Mac's `.pixi` in your folder:

```sh
docker/dev.sh start                              # once (needs the image: see above)
docker/dev.sh test tests/test_debug_gdb.py       # pytest, with any arguments
docker/dev.sh run gdb --version                  # anything, in the project's environment
docker/dev.sh shell                              # a shell in it
docker/dev.sh stop
```

`start` makes a new container from the image each time (a running one is
replaced), so after rebuilding the image, `docker/dev.sh start` again to
use it.

## After changing `pixi.toml`, `pixi.lock` or the `Dockerfile`

Rebuild the image, then start again from it. The environment volumes were
filled from the old image, so remove them too (they're filled again from
the new one):

```sh
docker build -t vibefpmgui -f docker/Dockerfile .   # (or docker/build-docker.sh)
docker/dev.sh stop
docker volume rm vibefpmgui-pixi                    # and vibefpmgui-pixi-amd64, if you made it
docker/dev.sh start
```

Only source changes (Python files, tests, the demo)? Nothing to do: the
development container sees them at once, and `docker/build-docker.sh`
copies them into the image when it builds (a quick step).
