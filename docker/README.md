# VibeFPMGUI in a Linux container (on a Mac)

The image (`docker/Dockerfile`) is Ubuntu 24.04 with the project's pixi
environment (the same one CI uses: gfortran, fpm, Python, PySide6…), git,
and what Qt needs to show its window on an X11 display. On a Mac, XQuartz
is that display: the app's window opens beside your Mac windows.

It's built for your Mac's own architecture: arm64 Linux on Apple Silicon
(`pixi.lock` has `linux-aarch64`), at full speed. To run exactly what CI
runs (x86-64 Linux, `linux-64`), to reproduce a failure seen only there,
build and run it for `linux/amd64` instead: see the end.

## Once

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Install XQuartz, the X11 display, for the GUI (not needed for the tests):

   ```sh
   brew install --cask xquartz
   ```

   Log out and back in (it sets up `DISPLAY` then). Start XQuartz, and in
   *XQuartz → Settings → Security* tick *Allow connections from network
   clients*; quit XQuartz and start it again.

## Build the image

From the project's folder:

```sh
docker build -t vibefpmgui -f docker/Dockerfile .
```

The first build downloads the environment (a few GB, some minutes); later
ones reuse it unless `pixi.toml` or `pixi.lock` changed.

## Run the tests (no display needed)

```sh
docker run --rm vibefpmgui pixi run test
docker run --rm vibefpmgui pixi run lint
```

The tests use Qt's offscreen platform: no window appears.

## Run the app

With XQuartz running, let containers connect to it (each time XQuartz
starts), then run the app with its display:

```sh
xhost +localhost
docker run --rm -it -e DISPLAY=host.docker.internal:0 vibefpmgui
```

(If the window doesn't appear, try `xhost + 127.0.0.1` instead of
`xhost +localhost`.) XQuartz doesn't use the Retina resolution: if the
window is small, add `-e QT_SCALE_FACTOR=2`.

### Your projects, your settings, and the source as you edit it

Folders on the Mac are mounted with `-v`. For example, the source you're
editing (instead of the copy in the image), a project to open, and a volume
for the app's settings so they're kept between runs:

```sh
docker run --rm -it -e DISPLAY=host.docker.internal:0 \
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

Then open `/projects/halo` with *File → Open Project…*.

A shell in the container, to run anything else (`pixi run …`, `fpm`, `git`):

```sh
docker run --rm -it -e DISPLAY=host.docker.internal:0 vibefpmgui bash
```

## As CI runs it: x86-64 Linux

In Docker Desktop's *Settings → General*, turn on *Use Rosetta for
x86_64/amd64 emulation on Apple Silicon* (much faster than QEMU's
emulation). Then build and run a second image with `--platform`:

```sh
docker build --platform linux/amd64 -t vibefpmgui-amd64 -f docker/Dockerfile .
docker run --rm --platform linux/amd64 vibefpmgui-amd64 pixi run test
```

(Mounting the source with `-v "$PWD":/app`, give it its own environment
volume too, e.g. `-v vibefpmgui-pixi-amd64:/app/.pixi`: the two
architectures' environments differ.)
