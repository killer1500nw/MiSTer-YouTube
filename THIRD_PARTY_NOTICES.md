# Credits and notices

MiSTer framework: https://github.com/MiSTer-devel/Template_MiSTer
Pinned commit: 3ea1134cf05d62c2b1db30362277a823d739ced2.
See fpga/UPSTREAM.md, fpga/LICENSE and individual source headers. Original
framework and PLL files are preserved from the source package.

The CA bundle comes from curl's conversion of Mozilla's certificate store.
See scripts/YouTubeURL/CERTIFICATES-NOTICE.txt for source and MPL-2.0 information.
This certificate notice also accompanies the historical FPGA test scripts.

The native ARM helper is supplied with its C source and build instructions.
External yt-dlp, QuickJS, Node.js (MIT, downloaded from nodejs.org), FFmpeg and Quartus are not included; their respective
upstream licences apply. No ownership of those projects is claimed.

## MiSTer H264 core

* The optional H264 mode talks to the **MiSTer H264 core** by owenb321 (https://github.com/owenb321/MiSTer_h264_decoder, GPL-3.0-or-later;
  its client library is MIT). The core is not included in this repository or its installer. The Python code in
  `scripts/YouTubeCRT/ytcrt/h264ring.py` is an independent implementation of the core's published interface (version 1.1), written with
  that project's documentation and MIT-licensed client library as reference.

## YouTube CRT GUI

* `scripts/YouTubeCRT/ytcrt/font8x8.py`: 8x8 bitmap font, public domain (Daniel Hepper, font8x8_basic).
* Feature set and browsing model inspired by the YouTube module of anthonycaccese/240-MP
  (https://github.com/anthonycaccese/240-MP). No code from that project is included.
