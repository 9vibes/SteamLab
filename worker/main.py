import os

from .runtime import run

if __name__ == "__main__":
    # Native CUDA calls cannot be cancelled safely. run() reaps FFmpeg first, then
    # process exit also bounds shutdown if a daemon inference thread is stuck in C++.
    os._exit(run())
