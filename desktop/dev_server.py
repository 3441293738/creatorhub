"""Browser preview backed by the real controller and a separate local data home."""
import argparse
import os
from pathlib import Path
from desktop.controller import Controller
from desktop.launcher import InstanceLock, prepare_home
from desktop.web_shell import ShellServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--home", default="build/desktop-preview-data")
    parser.add_argument("--skip-browser-install", action="store_true")
    args = parser.parse_args()
    home = Path(args.home).resolve()
    os.environ["CREATORHUB_DESKTOP_HOME"] = str(home)
    prepare_home(home)
    lock = InstanceLock(home)
    controller = Controller(home, install_browser=not args.skip_browser_install)
    controller.preferences["open_on_ready"] = False
    server = ShellServer(controller, args.port)
    print(f"Desktop preview: {server.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        if controller.worker:
            controller.worker.join(timeout=50)
        server.server_close()
        lock.close()


if __name__ == "__main__":
    main()
