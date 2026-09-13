# CUDA worker RTSP failure (1.0.2)

The 1.0.1 installed worker reported `CUDAExecutionProvider`, proving its strict
SFace zero-input warmup had succeeded. With the real publisher offline, a bounded
synthetic test-pattern RTMP publisher reproduced `online: true` together with
`state: error`, `error: Stream unavailable or timed out`. No camera footage,
face catalog reads, or recordings were used.

The CUDA 12.4 Ubuntu 22.04 image installs FFmpeg 4.4. That version's RTSP
`-timeout` is the deprecated **listen timeout in seconds** and implies server
mode. Client TCP socket timeout is `-stimeout` in microseconds. FFmpeg 5+ uses
`-timeout` for the client socket timeout instead. The old integration test ran
FFmpeg 7 and therefore missed the image-specific regression.

References:
- https://github.com/FFmpeg/FFmpeg/blob/n4.4/libavformat/rtsp.c#L96-L102
- https://github.com/FFmpeg/FFmpeg/blob/n4.4/libavformat/rtspdec.c#L738-L739

The worker now probes `ffmpeg -hide_banner -h demuxer=rtsp` once, without any
credentials, and prefers `-stimeout` when available. Socket timeouts remain ten
seconds; frame watchdog, nonblocking draining, and shutdown bounds are unchanged.
The CUDA Docker build also exercises the packaged FFmpeg probe. Regression tests
cover both option sets and cache behavior. GPU provider checks, real SFace
warmup, NVIDIA base, and driver requirements are unchanged.

Live post-update verification must repeat the synthetic publisher and confirm
`online: true`, `analysis.state: analyzing`, `CUDAExecutionProvider`, and no error.
This proves decoding and GPU startup inference, not face-recognition accuracy.
