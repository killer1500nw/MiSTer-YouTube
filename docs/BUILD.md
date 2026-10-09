# Build and test

Open `fpga/YouTubeCRT.qpf` with Quartus 17.0.x, or run `fpga/build-windows.bat`.
On Linux run `bash fpga/build-linux.sh` with Quartus 17.0.x available. Read the
build and timing output before using a newly compiled RBF. The supplied release
RBF is the previously hardware-tested build, not a new compile of this packaging.

`fpga/DEVELOPER.md` documents the native helper build (Zig 0.13.0), FPGA tests and
historical host tests. To rebuild the current helper run its documented command
from `scripts/YouTubeURL/`; the C source and binary are unchanged from core v0.13.

Current player host checks:

```sh
python3 tests/test_pipeline.py
```

Original FPGA/host suite (requires Icarus Verilog 12, GCC, FFmpeg and OpenSSL):

```sh
bash fpga/tests/run.sh
```

The original host suite uses the historical scripts inside fpga/MiSTer-Scripts.
FPGA protocol details are in fpga/PROTOCOL.md. Tests cannot establish network
availability or on-device performance. Packaging does not change FPGA source.
