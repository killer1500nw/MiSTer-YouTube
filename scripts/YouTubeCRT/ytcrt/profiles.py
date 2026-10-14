"""The playback profile: 320x240 RGB565 records at 30 fps, exactly what the YouTubeCRT core expects.

A record is 160000 bytes: 153600 of picture and 1600 stereo samples (one 30 fps frame of 48 kHz audio). Other
frame rates would need a different core (see docs/PROTOCOL-v0.14-draft.md), and the CRT output mode (240p or
480i) is chosen in the core's own menu, so there is nothing for the player to choose here.
"""


class Profile:
    def __init__(self, key, label, fps):
        self.key = key
        self.label = label
        self.fps = fps                    # record rate
        self.w, self.h = 320, 240         # record geometry (fixed by the core's slot size)
        self.video_bytes = self.w * self.h * 2
        self.audio_bytes = 48000 // fps * 4
        self.record_bytes = self.video_bytes + self.audio_bytes


PROFILE = Profile('240p30', '240p 30fps', 30)
PROFILES = [PROFILE]
BY_KEY = {p.key: p for p in PROFILES}
DEFAULT = PROFILE.key


# Stream quality. The screen is 320x240, but a bigger source downscaled to it looks much cleaner (YouTube's own
# 240p is only ~210-240 kbps). What limits us is how much H.264 the 800 MHz ARM can decode in real time, and that
# depends on the stream: how many pixels, how many kbps and how many frames per second. The player estimates that
# cost for every stream a video offers and picks the best picture that fits.
QUALITY_BOX = {'240p': 240, '360p': 360, '480p': 480, 'Auto': 480}      # shorter side allowed, in pixels (4:3 and 16:9 alike)
# Calibration, from one real measurement on a Cyclone V MiSTer (Speed test, start-up time cancelled out): a 640x360, 30 fps,
# 307 kbps stream ran at 1.49x real time with the cheapest decoder setting. `cost` is ARM seconds per video second (1/1.49).
# The Speed test replaces this with a measurement of your own MiSTer.
CAL_DEFAULT = {'px': 640 * 360, 'tbr': 307.0, 'fps': 30.0, 'cost': 0.67}
PIXEL_SHARE = 0.5          # assume half of the decoding cost follows the pixel count and half follows the bitrate
LIMIT = 0.72              # the decoder may use at most this much of the ARM: the rest is the player's own pipe reading and record copying (about a quarter)
LIMIT_TOTAL = 1.10         # used instead of LIMIT once the Speed test has measured the picture copy: decoding plus copying may use this much
PICTURE_BOX = (640, 480)   # the v0.15 core takes pictures up to this size; a bigger stream is scaled down by ffmpeg first
PICTURE_BOX_BIG = (864, 480)   # the v0.16 core reading from the framebuffer takes pictures this big
BIG_480_PX = 300000        # above this (more than 640x360 = 230,400) Auto prefers the Fast decoder setting
BIG_PX = 110000            # above this many pixels (a little more than 426x240) the cheaper decoder settings are used


def label_for(width, height):
    """'240p' / '360p' / '480p' by the short side, so a vertical 240x426 clip is also 240p."""
    return '%dp' % min(width, height)


def limit_for(cal=None):
    """How much of the ARM the estimate (see predicted_cost) may use: more once it includes the picture copy."""
    return LIMIT_TOTAL if cal and cal.get('copy_ms') else LIMIT


def picture_bytes(stream, big=False):
    """Bytes of YUV 4:2:0 the player copies to the core for one picture of this stream."""
    w, h = stream['width'], stream['height']
    box = PICTURE_BOX_BIG if big else PICTURE_BOX
    f = min(1.0, box[0] / w, box[1] / h)
    return int(w * f) * int(h * f) * 3 // 2


def copy_scale(stream, cal):
    """How the copy time of this stream's pictures compares with the reference picture's. Measured copies are not proportional
    to the size (a 240p picture took 10 to 24 ms, a 360p one about 23 ms during playback), so a fixed part is assumed."""
    base = cal.get('copy_bytes')
    if cal.get('copy_fixed') or not base:
        return 1.0
    return 0.5 + 0.5 * picture_bytes(stream, bool(cal.get('big'))) / float(base)


def copy_cost(stream, cal, fps):
    """Share of the ARM spent copying this stream's pictures to the core (0 until the Speed test measured it)."""
    ms, base = cal.get('copy_ms'), cal.get('copy_bytes')
    if not ms or not base:
        return 0.0
    return ms / 1000.0 * copy_scale(stream, cal) * fps


def predicted_cost(stream, cal=None, mult=1.0):
    """ARM seconds needed per second of video: decoding (from the stream's pixels, bitrate and frame rate) plus copying the
    pictures to the core once the Speed test has measured that.

    A 24 fps stream has 20% fewer frames to decode than a 30 fps one, so it costs 20% less."""
    cal = cal or CAL_DEFAULT
    px = stream['width'] * stream['height']
    tbr = stream.get('tbr') or cal['tbr']
    fps = stream.get('fps') or cal['fps']
    shape = PIXEL_SHARE * px / cal['px'] + (1.0 - PIXEL_SHARE) * tbr / cal['tbr']
    return (cal['cost'] * shape * fps / cal['fps'] + copy_cost(stream, cal, fps)) * mult


SPEEDS = ('Normal', 'Fast', 'Fastest')      # gentlest first: Fast skips the deblocking filter on non-reference frames, Fastest on all


def setting_for(stream, cal, mult, speed_x):
    """The gentlest decoder setting (full deblocking filter = best picture) predicted to keep this stream within the ARM's
    limit, from the Speed test's measurement of every setting (`speed_x`: {setting: times real time}). None if unknown.

    The calibration describes the fastest setting; a gentler one takes best_x / x of the decoding time, and the picture
    copy costs the same whatever the setting."""
    if not (isinstance(speed_x, dict) and speed_x and cal and cal.get('cost')):
        return None
    xs = {k: float(v) for k, v in speed_x.items() if k in ('Normal', 'Fast') and v and float(v) > 0}
    if not xs.get('Fast'):
        return None
    best_x = xs['Fast']                              # the calibration describes Fast; Fastest (no deblocking at all) is never chosen automatically
    fps = stream.get('fps') or cal.get('fps') or 30.0
    copy = copy_cost(stream, cal, fps)
    decode = predicted_cost(stream, cal, 1.0) - copy
    # At 480p the whole ARM is busy (ffmpeg ~95% of a core, nothing idle), so Normal needs a wide margin; Fast looked right on the CRT.
    margin = 0.55 if (stream.get('width') or 0) * (stream.get('height') or 0) > BIG_480_PX else 0.9
    for level in SPEEDS:
        if level in xs and (decode * best_x / xs[level] + copy) * mult <= margin * limit_for(cal):
            return level
    return 'Fast'


def speed_for(px, speed, learned):
    """Decoder setting for a stream: big pictures use what the Speed test found (never the slow default)."""
    if px > BIG_PX and speed != 'Fastest':             # a manual Fastest is always honoured
        return learned if learned in ('Fast', 'Fastest') else ('Fast' if speed == 'Normal' else speed)
    return speed


CROPS = ('Fill', '14:9', 'Off')          # the order the buttons cycle through: most old TV is uploaded as 4:3 inside a 16:9 frame
QUALITIES = ('Auto', '240p', '360p', '480p')


def crop_filter(mode):
    """Centre crop applied before scaling. Never crops a picture that is already narrower.

    Off:  whole picture, black bars where the aspect does not match the 4:3 screen.
    14:9: trims the sides of widescreen video down to 14:9, leaving thin top/bottom bars.
    Fill: trims widescreen down to 4:3 so it fills the screen. This also removes the baked-in
          side bars of 4:3 shows stored inside widescreen files.
    """
    if mode == '14:9':
        return "crop=w='min(iw,ih*14/9)':h=ih,"
    if mode == 'Fill':
        return "crop=w='min(iw,ih*4/3)':h=ih,"
    return ''


def video_filter(profile, fast_scale=True, crop='Off'):
    # fast_bilinear is the cheapest scaler and looked as good as bicubic on real 360p frames
    flags = 'fast_bilinear' if fast_scale else 'bilinear'
    return (crop_filter(crop) +
            'fps=%d:start_time=0,scale=%d:%d:force_original_aspect_ratio=decrease:flags=%s,'
            'pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1'
            % (profile.fps, profile.w, profile.h, flags, profile.w, profile.h))


def decode_speed_args(level):
    """ffmpeg input options that trade picture quality for decode time."""
    if level == 'Fast':
        return ['-skip_loop_filter', 'noref', '-flags2', '+fast']
    if level == 'Fastest':
        return ['-skip_loop_filter', 'all', '-flags2', '+fast']
    return []
