"""
Bulk host add: turn pasted text into a checked list of hostnames.

Names are separated by new lines or commas. A numbered run is written in
brackets and expands in place, keeping zero padding:

    EDGE-SW-[01-24]          EDGE-SW-01 ... EDGE-SW-24
    B1-FL[1-5]-SW[001-200]   every floor x every stack number (1000 names)

Several ranges in one name multiply, leftmost varying slowest, so a site's
floors and stack numbers come out grouped by floor.

The preview and the add both call plan(), so what is added is exactly what
was previewed. The browser only ever sends the original text.
"""

import itertools
import re

MAX_HOSTS = 1000

HOSTNAME = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
_RANGE = re.compile(r'\[(\d+)-(\d+)\]')
_ANY_BRACKET = re.compile(r'[\[\]]')


class BulkError(ValueError):
    """The text cannot be expanded at all (too many names, bad range)."""


def _expand_one(entry):
    """All names one entry stands for."""
    parts, ranges, pos = [], [], 0
    for m in _RANGE.finditer(entry):
        parts.append(entry[pos:m.start()])
        start_s, end_s = m.group(1), m.group(2)
        start, end = int(start_s), int(end_s)
        if end < start:
            raise BulkError('%s: range [%s-%s] runs backwards' % (entry, start_s, end_s))
        # Checked before the list is built, so [1-999999999] costs nothing.
        if end - start + 1 > MAX_HOSTS:
            raise BulkError('%s expands to more than %d names' % (entry, MAX_HOSTS))
        # Zero padding is taken from the start: [01-24] pads to 2, [1-200] not at all.
        width = len(start_s) if start_s.startswith('0') and len(start_s) > 1 else 0
        ranges.append(['%0*d' % (width, n) for n in range(start, end + 1)])
        pos = m.end()
    parts.append(entry[pos:])

    if _ANY_BRACKET.search(''.join(parts)):
        raise BulkError('%s: brackets must hold a range like [01-24]' % entry)

    count = 1
    for r in ranges:
        count *= len(r)
        if count > MAX_HOSTS:
            raise BulkError('%s expands to more than %d names' % (entry, MAX_HOSTS))

    names = []
    for combo in itertools.product(*ranges):
        name = parts[0]
        for value, tail in zip(combo, parts[1:]):
            name += value + tail
        names.append(name)
    return names


def expand(text):
    """Every name the text stands for, in order. Raises BulkError."""
    entries = [e.strip() for e in re.split(r'[\n,]+', text or '') if e.strip()]
    names = []
    for entry in entries:
        names.extend(_expand_one(entry))
        if len(names) > MAX_HOSTS:
            raise BulkError('More than %d names in one batch; split it up.' % MAX_HOSTS)
    return names


def plan(text, existing):
    """Each name with whether it would be added and, if not, why.

    existing: hostnames already in the project (compared case-insensitively,
    as single add does).
    """
    names = expand(text)
    taken = {h.lower() for h in existing}
    seen = set()
    out = []
    for name in names:
        key = name.lower()
        if not HOSTNAME.match(name):
            reason = 'invalid: letters, numbers, - and _ only, up to 64'
        elif key in taken:
            reason = 'already in the project'
        elif key in seen:
            reason = 'repeated in this list'
        else:
            reason = None
        seen.add(key)
        out.append({'name': name, 'add': reason is None, 'reason': reason})
    return out
