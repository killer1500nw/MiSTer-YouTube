"""Where does the ARM's time go during playback? Reads /proc (Linux only) and reports per-process CPU use for the log."""
import os
import time

CLK_TCK = 100.0     # jiffies per second on Linux/ARM (os.sysconf would say the same)


def parse_proc_stat(text):
    """/proc/<pid>/stat -> utime + stime in jiffies. The command name is in parentheses and may contain spaces."""
    rest = text[text.rindex(')') + 2:].split()
    return int(rest[11]) + int(rest[12])            # fields 14 and 15 of the file (utime, stime)


def parse_cpu_line(text):
    """The first line of /proc/stat -> (busy jiffies, idle jiffies) of all CPUs together."""
    fields = [int(x) for x in text.split('\n', 1)[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)           # idle + iowait
    return sum(fields) - idle, idle


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


class CpuWatch:
    """sample() -> 'player 58%, video ffmpeg 110%, ...' since the last call (percent of ONE core, so two busy cores read 200%).

    `named` maps a name to a pid (or None); this process is always included as 'player'."""

    def __init__(self, named, read=_read, now=time.monotonic):
        self.named, self.read, self.now = dict(named), read, now
        self.last = self._take()

    def _take(self):
        procs = {'player': os.getpid(), **{k: v for k, v in self.named.items() if v}}
        jiffies = {}
        for name, pid in procs.items():
            text = self.read('/proc/%d/stat' % pid)
            if text:
                try:
                    jiffies[name] = parse_proc_stat(text)
                except (ValueError, IndexError):
                    pass
        total = self.read('/proc/stat')
        try:
            busy, idle = parse_cpu_line(total) if total else (0, 0)
        except (ValueError, IndexError):
            busy, idle = 0, 0
        return self.now(), jiffies, busy, idle

    def sample(self):
        t0, j0, b0, i0 = self.last
        self.last = t1, j1, b1, i1 = self._take()
        span = max(0.001, t1 - t0)
        parts = ['%s %d%%' % (name, round(100.0 * (j1[name] - j0[name]) / CLK_TCK / span)) for name in j1 if name in j0]
        total = (b1 - b0) + (i1 - i0)
        if total > 0:
            parts.append('idle %d%% of all cores' % round(100.0 * (i1 - i0) / total))
        return ', '.join(parts)
