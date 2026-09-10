"""Watch the launcher independently of browser installation and server startup."""
import os
import subprocess
import threading


def watch_parent(parent_pid, stop_file):
    """Return a cleanup callback. On Windows retain a handle, not a reusable PID."""
    finished = threading.Event()
    if not parent_pid:
        return finished.set
    handle = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        def alive():
            return bool(handle) and kernel.WaitForSingleObject(handle, 0) == 258
    else:
        def alive():
            return os.getppid() == parent_pid

    def monitor():
        try:
            while not finished.wait(.25):
                if alive():
                    continue
                stop_file.touch()
                # The ordinary server stop path releases tasks and browsers first.
                # During a stalled download/startup, bound the wait and end only this tree.
                if not finished.wait(30):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    os._exit(1)
                return
        finally:
            if handle:
                kernel.CloseHandle(handle)
    worker = threading.Thread(target=monitor, daemon=True)
    worker.start()
    def close():
        finished.set()
        worker.join(timeout=2)
    return close
