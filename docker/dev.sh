#!/bin/bash
#
# A development container: the image (docker/Dockerfile) kept running, with
# the source on the Mac mounted at /app, so changes are seen at once, and the
# Linux environment in a volume of its own (vibefpmgui-pixi). Run the tests,
# or anything, in it with `docker exec`. See docker/README.md.
#
#   docker/dev.sh start | stop | test [pytest args] | run COMMAND... | shell
#
# --init: a process that cleans up the programs run once they end (else they
# stay as zombies); SYS_PTRACE and seccomp=unconfined: so gdb can debug.

set -e
cd "$(dirname "$0")/.."
NAME=vibefpmgui-dev

case "${1:-}" in
    start)
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        docker run -d --init --name "$NAME" --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
            -v "$PWD":/app -v vibefpmgui-pixi:/app/.pixi vibefpmgui sleep infinity >/dev/null
        echo "$NAME started"
        ;;
    stop)
        docker rm -f "$NAME" >/dev/null && echo "$NAME stopped"
        ;;
    test)
        shift
        docker exec "$NAME" bash -lc "cd /app && pixi run pytest -p no:cacheprovider $*"
        ;;
    run)
        shift
        docker exec "$NAME" bash -lc "cd /app && pixi run $*"
        ;;
    shell)
        docker exec -it "$NAME" bash -l
        ;;
    *)
        echo "usage: docker/dev.sh start | stop | test [pytest args] | run COMMAND... | shell" >&2
        exit 2
        ;;
esac
