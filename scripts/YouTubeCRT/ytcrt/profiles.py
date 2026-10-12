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
# Calibration, from one real measurement on a Cyclone V MiSTer (Speed test): a 640x360, 30 fps, 591 kbps stream ran
# at 0.87x real time with the cheapest decoder setting. `cost` is ARM seconds per video second (1/0.87).
# The Speed test replaces this with a measurement of your own MiSTer.
CAL_DEFAULT = {'px': 640 * 360, 'tbr': 591.0, 'fps': 30.0, 'cost': 1 / 0.87}
PIXEL_SHARE = 0.5          # assume half of the decoding cost follows the pixel count and half follows the bitrate
LIMIT = 0.80              # a stream must be predicted to need at most this much ARM time per second of video (headroom for busy scenes)
BIG_PX = 110000            # above this many pixels (a little more than 426x240) the cheaper decoder settings are used


def label_for(width, height):
    """'240p' / '360p' / '480p' by the short side, so a vertical 240x426 clip is also 240p."""
    return '%dp' % min(width, height)


def predicted_cost(stream, cal=None, mult=1.0):
    """ARM seconds needed per second of video, from the stream's pixels, bitrate and frame rate.

    A 24 fps stream has 20% fewer frames to decode than a 30 fps one, so it costs 20% less."""
    cal = cal or CAL_DEFAULT
    px = stream['width'] * stream['height']
    tbr = stream.get('tbr') or cal['tbr']
    fps = stream.get('fps') or cal['fps']
    shape = PIXEL_SHARE * px / cal['px'] + (1.0 - PIXEL_SHARE) * tbr / cal['tbr']
    return cal['cost'] * mult * shape * fps / cal['fps']


def speed_for(px, speed, learned):
    """Decoder setting for a stream: big pictures use what the Speed test found (never the slow default)."""
    if px > BIG_PX:
        return learned if learned in ('Fast', 'Fastest') else ('Fast' if speed == 'Normal' else speed)
    return speed


CROPS = ('Off', '14:9', 'Fill')


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
