"""Measure each game window independently of desktop rows and columns."""


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
