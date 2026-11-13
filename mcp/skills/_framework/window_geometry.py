"""Measure each game window independently of desktop rows and columns."""


def _neutral(color):
    return max(color)-min(color) < 20 and (min(color) >= 200 or max(color) <= 90)


def _title_bands(image):
    """Group long neutral horizontal runs into title-bar bands.

    Runs are per row, so a title text or notification icon only splits the
    rows it covers; rows below the glyphs keep the full window span. Bands
    that continue each other across a few rows (gradient title bars) merge.

    A startup overlay (the RustDesk connection-manager panel) can CLIP a
    title bar horizontally, leaving each glyph row as a short piece. Pieces
    of one bar share their x span and sit within 18 px of each other, so they
    merge vertically into one band whose y1 is the bar's real bottom (rows
    outside the glyphs already spanned the full width, but when EVERY row is
    clipped only the merge recovers the bar).
    """
    pixels = image.load()
    width, height = image.size
    bands = []
    for y in range(height):
        x = 0
        while x < width:
            color = pixels[x, y]
            if not _neutral(color):
                x += 1
                continue
            start = x
            while (x < width and _neutral(pixels[x, y])
                   and max(abs(pixels[x, y][c]-color[c]) for c in range(3)) < 24):
                x += 1
            if x-start < 250:
                continue
            for band in bands:
                same_span = (abs(start-band['x0']) <= 6 and abs(x-band['x1']) <= 6)
                if ((y-band['y1'] <= 3 or (same_span and y-band['y1'] <= 18))
                        and min(x, band['x1'])-max(start, band['x0'])
                        > .5*min(x-start, band['x1']-band['x0'])
                        and max(abs(color[c]-band['color'][c]) for c in range(3)) <= 60):
                    band['x0'] = min(band['x0'], start)
                    band['x1'] = max(band['x1'], x)
                    band['y1'] = y
                    break
            else:
                bands.append(dict(x0=start, x1=x, y0=y, y1=y, color=color))
    return [band for band in bands
            if band['x1']-band['x0'] >= 300 and 8 <= band['y1']-band['y0']+1 <= 80]


def _taskbar_top(image):
    """Native y of the strongest horizontal edge in the bottom tenth — the
    taskbar border, or the screen bottom when there is no taskbar."""
    pixels = image.load()
    width, height = image.size
    columns = range(0, width, 8)
    best, top = 0.0, height-1
    for y in range(max(1, round(height*.9)), height):
        strength = sum(abs(sum(pixels[x, y])-sum(pixels[x, y-1]))/3
                       for x in columns)/len(columns)
        if strength > best:
            best, top = strength, y
    return top if best > 8 else height-1


def _bottom_edges(image, left, right, low, high):
    """Full-width horizontal edges in [low, high] (row y differs from y-1
    across the span): the window border, and possibly HUD edges above it."""
    pixels = image.load()
    edges = []
    for y in range(low, high+1):
        differences = [sum(abs(pixels[x, y][c]-pixels[x, y-1][c])
                           for c in range(3))/3
                       for x in range(left+4, right-4, 4)]
        if not differences:
            continue
        coverage = sum(d > 20 for d in differences)/len(differences)
        strength = sum(differences)/len(differences)
        if coverage >= .75 and strength >= 25:
            edges.append(y)
    return edges


def _bottom_border(image, left, right, low, high):
    """Last full-width horizontal edge in [low, high]: the window's outer
    bottom border (a stray HUD edge may be stronger but sits above it)."""
    edges = _bottom_edges(image, left, right, low, high)
    return max(edges) if edges else None


def _right_edge(image, x0, prior_w, start, end):
    """Measured vertical content border near x0+prior_w, scanned over the
    lower half of the content (below where a startup overlay panel reaches).
    The LAST full-height edge wins: interior HUD edges rarely span the whole
    range, and the window border is the rightmost one."""
    pixels = image.load()
    lo = max(1, x0+int(prior_w*.85))
    hi = min(image.width-1, x0+int(prior_w*1.15))
    rows = range(start+int((end-start)*.55), max(start+int((end-start)*.55)+1, end-15), 2)
    best = None
    for x in range(lo, hi):
        differences = [sum(abs(pixels[x, y][c]-pixels[x-1, y][c]) for c in range(3))/3
                       for y in rows]
        if not differences:
            continue
        coverage = sum(d > 20 for d in differences)/len(differences)
        strength = sum(differences)/len(differences)
        if coverage >= .9 and strength >= 20:
            best = x
    return best


def measure_windows(image):
    """Locate visible game windows from title-bar pixel bands, without OCR.

    Returns one dict per window: {'rect': [x, y, w, h], 'estimated': [...]}.
    'estimated' names edges that could not be measured (only 'right' today:
    a title bar clipped by an overlay leaves the width a peer-median prior).
    'narrow' marks windows recovered from a clipped band; the caller must
    confirm those with one title OCR before trusting them. None when no band
    is plausible.
    """
    bands = _title_bands(image)
    if not bands:
        return None
    taskbar = _taskbar_top(image)
    widths = sorted(band['x1']-band['x0'] for band in bands)
    median = widths[len(widths)//2]
    full_span = (median*.8, median*1.2)

    def window_rect(band, caps):
        visible = band['x1']-band['x0']
        narrow = visible < full_span[0]
        width = median if narrow else visible
        start = band['y1']+1
        below = [top for left, right, top in caps
                 if top > start+100
                 and min(right, band['x1'])-max(left, band['x0']) > visible*.5]
        low = start+max(300, round(width/1.55))
        if below:
            # A following window's title bar is chrome: never scan into it.
            high = min(min(below)-1, start+round(width/1.15))
        else:
            # Bottom row: the window border and the taskbar edge may be the
            # same row, so the cap itself stays in range.
            high = min(taskbar, start+round(width/1.15))
        if low > high:
            return None
        edges = _bottom_edges(image, band['x0'], band['x1'], low, high)
        if not edges:
            return None
        # Clients are ~4:3; the chrome row just below a stacked neighbor's
        # title bar (or the taskbar edge) sits ~9-11 px past the border and
        # used to win the "last edge" rule. Prefer the last edge within 8 px
        # of the 4:3 prior; a non-4:3 window keeps the last-edge rule. A
        # measured sibling median cannot be used as the prior: all siblings
        # share the same bias, so their median inherits it.
        end = max(edges)
        prior = start+round(width*3/4)
        within = [y for y in edges if abs(y-prior) <= 8]
        if within:
            end = max(within)
        if end-start < 300:
            return None
        estimated = []
        if narrow:
            edge = _right_edge(image, band['x0'], width, start, end)
            if edge is not None:
                width = edge-band['x0']
            else:
                estimated.append('right')
                width = median
        if not 1.15 <= width/(end-start) <= 1.55:
            return None
        return dict(rect=[band['x0'], start, width, end-start],
                    estimated=estimated, narrow=narrow)

    # Only full-width bands may cap another window's bottom search: a neutral
    # in-game dialog title inside a client must neither cap the client under
    # it nor leak in as a window of its own (its clipped band cannot produce
    # a plausible rect from the median-derived scan range).
    def caps_from(cands):
        return [(band['x0'], band['x1'], band['y0']) for band in cands
                if full_span[0] <= band['x1']-band['x0'] <= full_span[1]]

    caps = caps_from(bands)
    first = [window_rect(band, caps) for band in bands]
    # A content artifact that formed a band must not cap a real window: the
    # second pass only trusts bands that produced a plausible rectangle.
    trusted = [band for band, rect in zip(bands, first) if rect]
    if not trusted:
        return None
    caps = caps_from(trusted)
    rects = [rect for rect in (window_rect(band, caps) for band in trusted) if rect]
    return rects or None


def measure_band_windows(image):
    """Backward-compatible entry: just the measured rects (no flags).

    Callers that only need geometry (zhuagui's legacy autocalignment) keep
    working; discovery uses measure_windows for the full flags.
    """
    rects = measure_windows(image)
    if not rects:
        return None
    return [dict(entry)['rect'] for entry in rects]


def measure_title_windows(image, items):
    pixels = image.load()

    bars = []
    for anchor in items:
        if '梦幻西游' not in anchor['text'] or anchor['score'] < .3:
            continue
        x = min(image.width-1, round((anchor['x']+anchor['w'])*image.width)+10)
        bottom = round((anchor['y']+anchor['h'])*image.height)
        runs = []
        for y in range(max(0, bottom), min(image.height, bottom+9)):
            color = pixels[x, y]
            if max(color)-min(color) >= 20 or not (min(color) >= 220 or max(color) <= 80):
                continue

            def same_bar(column, row):
                return max(abs(pixels[column, row][c]-color[c]) for c in range(3)) < 20

            left, right = x, x+1
            while left > 0 and same_bar(left-1, y):
                left -= 1
            while right < image.width and same_bar(right, y):
                right += 1
            runs.append((right-left, left, right, y, color))
        if not runs:
            return None
        width, left, right, y, color = max(runs)
        if width < 300:
            return None
        probes = range(left+round(width*.25), left+round(width*.75), 4)

        def title_row(row):
            return sum(same_bar(p, row) for p in probes)/len(probes) >= .9

        top, start = y, y
        while top > 0 and y-top < 40 and title_row(top-1):
            top -= 1
        while start < image.height and start-y < 20 and title_row(start):
            start += 1
        if start == image.height or start-y == 20:
            return None
        bar = (left, right, top, start)
        if bar not in bars:
            bars.append(bar)
    if not bars:
        return None

    rects = []
    for left, right, top, start in bars:
        width = right-left
        stop = min([b[2] for b in bars if b[2] > start
                    and min(right, b[1])-max(left, b[0]) > width*.5]
                   + [image.height-1])
        edges = []
        for y in range(start+max(300, round(width/1.55)),
                       min(stop, start+round(width/1.15)+1)):
            differences = [sum(abs(pixels[x, y][c]-pixels[x, y-1][c])
                               for c in range(3))/3
                           for x in range(left+4, right-4, 4)]
            coverage = sum(d > 20 for d in differences)/len(differences)
            strength = sum(differences)/len(differences)
            if coverage >= .75 and strength >= 25:
                edges.append((strength, y))
        if not edges:
            return None
        # The HUD can contain a stronger edge just above the outer border.
        _, end = max(edges, key=lambda edge: edge[1])
        rect = [left, start, width, end-start]
        if rect not in rects:
            rects.append(rect)
    return rects
