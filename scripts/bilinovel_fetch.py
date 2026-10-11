#!/usr/bin/env python3
"""
Bilinovel / Linovelib fetcher — pure HTTP (no browser/CDP).

Sites: www.bilinovel.com (intl) and www.linovelib.com (CN mirror, same catalogue).
The page JS redirects by browser locale, which we skip by using the host given.

Page layout:
  book:    /novel/<aid>.html
  catalog: /novel/<aid>/catalog
  volume:  /novel/<aid>/vol_<volid>.html   -> chapter list
  chapter: /novel/<aid>/<cid>.html, paginated as <cid>_2.html, <cid>_3.html ...
           until `var nextpage` points at a different chapter id.

Host matters for anti-scraping, so the mirror is tried first (see parse_url):
www.bilinovel.com withholds sentences from non-JS clients -- the body comes back
cut short with an inline decoy marker ("內容加載失敗！請刷新或更換瀏覽器") and only a
real browser sees the full text. www.linovelib.com serves the complete body to
curl_cffi with JS disabled, so no browser is needed. Note the "www." subdomain is
required: the bare linovelib.com does not complete a TLS handshake.

Usage:
  python bilinovel_fetch.py -u https://www.bilinovel.com/novel/4750.html
  python bilinovel_fetch.py -u ... --html "D:/out/dir"     # per-chapter HTML
  python bilinovel_fetch.py -u ... --start 1 --end 5
"""

import re, sys, json, time, argparse
from pathlib import Path

try:
    from curl_cffi import requests as curl_requests
    _has_curl = True
except ImportError:
    _has_curl = False
try:
    import requests as _requests
except ImportError:
    _requests = None

import urllib3
urllib3.disable_warnings()

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
# Decoy marker the site injects when it withholds text from non-JS clients.
_TRUNC_MARK = '內容加載失敗'
_TRUNC_MARK_S = '内容加载失败'
_HEADERS = {'User-Agent': _UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}

_SESSION = None
_HOST = None
_HOSTS = []
_HOST_IDX = 0


def _session(hosts=None):
    """Browser-impersonating session; the visit cookie is picked up on first GET."""
    global _SESSION, _HOST, _HOSTS
    if _SESSION is None:
        if not _has_curl:
            raise RuntimeError("curl_cffi required (pip install curl_cffi)")
        _SESSION = curl_requests.Session(impersonate='chrome120')
        _HOSTS = list(hosts) if hosts else []
        _HOST = _HOSTS[0] if _HOSTS else None
    return _SESSION


def _switch_host():
    """Fall back to the next host (mirror). Returns False when none is left."""
    global _HOST, _HOST_IDX
    if _HOST_IDX + 1 >= len(_HOSTS):
        return False
    _HOST_IDX += 1
    _HOST = _HOSTS[_HOST_IDX]
    print(f'  [host] switching to {_HOST}', file=sys.stderr, flush=True)
    return True


def _is_truncated(html):
    """True when the body came back withheld (decoy marker present)."""
    return _TRUNC_MARK in html or _TRUNC_MARK_S in html


_MIN_INTERVAL = 2.0      # seconds between any two requests to the site
_last_request = [0.0]


def _throttle():
    """Space out requests; the site rate-limits aggressively (HTTP 429)."""
    wait = _MIN_INTERVAL - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.time()


def _get(path, referer=None, retries=5):
    """GET a site path; returns decoded HTML.

    Handles the site's rate limit: on HTTP 429 honor Retry-After and back off
    exponentially. Also retries the intermittent TLS/library error.
    """
    s = _session(_HOST)
    url = path if path.startswith('http') else _HOST + path
    hdrs = dict(_HEADERS)
    if referer:
        hdrs['Referer'] = referer
    last = None
    for attempt in range(retries):
        _throttle()
        try:
            r = s.get(url, headers=hdrs, verify=False, timeout=30)
            if r.status_code == 429:
                ra = r.headers.get('Retry-After')
                delay = int(ra) if (ra or '').isdigit() else min(90, 15 * (attempt + 1))
                print(f'  [429] rate limited, waiting {delay}s', file=sys.stderr, flush=True)
                time.sleep(delay)
                last = RuntimeError(f'HTTP 429 for {url}')
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} for {url}")
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.0 + attempt)
    raise last


# ============================================================
#  Parsers
# ============================================================

def _meta(html, prop):
    m = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % re.escape(prop), html)
    return m.group(1).strip() if m else ''


def parse_book(aid, html):
    """Return (title, author, cover_url, volumes) where volumes = [(volid, name), ...]."""
    title = _meta(html, 'og:novel:book_name') or _meta(html, 'og:title')
    if not title:
        m = re.search(r'<title>(.*?)</title>', html, re.S)
        title = re.sub(r'_.*$', '', m.group(1)).strip() if m else aid
    author = _meta(html, 'og:novel:author')
    # Cover: the book page has no og:image; the site serves it at
    # /files/article/image/<first-digit>/<aid>/<aid>s.jpg (index-page thumbnails).
    cover = _meta(html, 'og:image')
    if not cover:
        m = re.search(r"url_image:'([^']+)'", html)
        cover = m.group(1) if m else ''
    if not cover:
        m = re.search(r'(https?://[^"\'\s]+)?(/files/article/image/\d+/%s/%ss\.jpg)' % (aid, aid), html)
        if m:
            cover = (m.group(1) or '') + m.group(2)
    if cover.startswith('/'):
        cover = (_HOST or '') + cover
    volumes, seen = [], set()
    for m in re.finditer(r'href="(/novel/%s/vol_(\d+)\.html)"[^>]*>(.*?)</a>' % aid, html, re.S):
        vid = m.group(2)
        if vid in seen:
            continue
        seen.add(vid)
        name = re.sub(r'<[^>]+>|\s+', ' ', m.group(3)).strip()
        volumes.append((vid, name))
    # The book page lists volumes newest-first; sort by volume id for reading order.
    volumes.sort(key=lambda v: int(v[0]))
    return title, author, cover, volumes


def parse_volume(aid, html):
    """Return [(cid, title), ...] in page order, deduped."""
    out, seen = [], set()
    for m in re.finditer(r'href="/novel/%s/(\d+)\.html"[^>]*>(.*?)</a>' % aid, html, re.S):
        cid = m.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        title = re.sub(r'<[^>]+>|\s+', ' ', m.group(2)).strip()
        if title:
            out.append((cid, title))
    return out


def _next_page(html):
    m = re.search(r'var\s+nextpage\s*=\s*"([^"]+)"', html) or \
        re.search(r"url_next:'([^']+)'", html)
    return m.group(1) if m else ''


def _chapter_text(html):
    """Extract plain text from the content container (linovelib: #TextContent)."""
    m = re.search(r'<div[^>]*id="(?:TextContent|acontent)"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    if not m:
        m = re.search(r'<div[^>]*id="(?:TextContent|acontent)"[^>]*>(.*?)(?:<div[^>]*id="footlink"|<div[^>]*class="footlink"|$)',
                      html, re.S)
    seg = m.group(1) if m else ''
    # Ad/script artifacts that leak into the text container
    seg = re.sub(r'<script.*?</script>', '', seg, flags=re.S)
    seg = re.sub(r'\(adsbygoogle[^)]*\)\.push\(\{\}\);', '', seg)
    seg = re.sub(r'<br\s*/?>', '\n', seg)
    seg = re.sub(r'</p>', '\n', seg)
    txt = re.sub(r'<[^>]+>', '', seg)
    txt = txt.replace('&nbsp;', ' ').replace('&#160;', ' ')
    txt = re.sub(r'[ \t\u3000]+', ' ', txt)
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    return txt.strip()


def fetch_chapter(aid, cid, referer=None):
    """Fetch one chapter, following _2/_3 pagination. Returns (text, pages).

    If the body comes back truncated (the decoy marker is present) the host is at
    fault, not the chapter -> switch to the mirror host and refetch.
    """
    url = f'/novel/{aid}/{cid}.html'
    parts, page = [], 0
    switched = False
    while url and page < 30:
        html = _get(url, referer=referer)
        if _is_truncated(html):
            if not switched and _switch_host():
                switched = True
                url = f'/novel/{aid}/{cid}.html'   # restart this chapter on the new host
                page = 0
                parts = []
                continue
        parts.append(_chapter_text(html))
        page += 1
        nxt = _next_page(html)
        # Another page of THIS chapter only if it is <cid>_N.html
        if not nxt or not re.search(r'/%s_\d+\.html$' % cid, nxt):
            break
        url = nxt
    text = '\n'.join(p for p in parts if p)
    return re.sub(r'\n{3,}', '\n\n', text).strip(), page


# ============================================================
#  Memory
# ============================================================

def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_chapter(novel_id, ch_id, novel_title):
    data = _load_memory()
    key = f'bilinovel:{novel_id}'
    data.setdefault(key, {'title': novel_title, 'chapters': {},
                          'first_fetch': time.strftime('%Y-%m-%d %H:%M')})
    data[key]['chapters'][str(ch_id)] = time.strftime('%Y-%m-%d %H:%M')
    data[key]['last_fetch'] = time.strftime('%Y-%m-%d %H:%M')
    _save_memory(data)


def _get_fetched(novel_id):
    return set(_load_memory().get(f'bilinovel:{novel_id}', {}).get('chapters', {}).keys())


# ============================================================
#  HTML output
# ============================================================

_HTML_PAGE = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: "Microsoft YaHei", "SimSun", "Noto Serif CJK SC", "Yu Mincho", serif;
           max-width: 800px; margin: 0 auto; padding: 2em 1.5em;
           line-height: 1.9; font-size: 18px; }}
    p {{ text-indent: 1em; margin: 0.3em 0; }}
    h1 {{ text-align: center; font-size: 1.4em; text-indent: 0; }}
    img {{ max-width: 100%; height: auto; display: block; margin: 1em auto; }}
  </style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>'''


def save_html_chapter(html_dir, ch_num, ch_title, body_html):
    html_dir = Path(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', ch_title or f'chapter{ch_num}')
    fname = f"{ch_num:04d}_{safe_title}.html"
    full = _HTML_PAGE.format(title=ch_title or f'Chapter {ch_num}', body=body_html)
    (html_dir / fname).write_text(full, encoding='utf-8')
    return fname


def _text_to_html(text):
    paras = []
    for line in text.split('\n'):
        s = line.strip()
        if s:
            paras.append(f'<p>{s}</p>')
    return '\n'.join(paras)


def download_image(url, img_dir, fname):
    img_dir = Path(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / fname
    if path.exists():
        return fname
    try:
        r = _session(_HOST).get(url, headers=_HEADERS, verify=False, timeout=20)
        if r.status_code == 200 and len(r.content) > 100:
            path.write_bytes(r.content)
            return fname
    except Exception as e:
        print(f"  image FAILED: {fname} - {e}", file=sys.stderr)
    return None


# ============================================================
#  Main
# ============================================================

def parse_url(url):
    """Return (hosts, aid) from a bilinovel/linovelib book URL.

    The two hosts serve the same catalogue but differ on anti-scraping: bilinovel.com
    returns TRUNCATED chapter bodies to plain HTTP clients (every chapter body gets
    the decoy marker inline), while linovelib.com serves the full text to curl_cffi
    with JS disabled. So prefer the mirror; keep the other as a fallback.
    """
    m = re.match(r'(https?://[^/]+)/novel/(\d+)(?:\.html)?', url)
    if not m:
        raise SystemExit(f'ERROR: not a bilinovel book URL: {url}')
    host, aid = m.group(1), m.group(2)

    # Swap only the domain, keeping the subdomain: the mirror needs its "www." host
    # (the bare linovelib.com fails TLS), so a naive domain replacement breaks it.
    order = [host]
    if 'bilinovel.com' in host:
        order.insert(0, host.replace('bilinovel.com', 'linovelib.com'))
    elif 'linovelib.com' in host:
        order.append(host.replace('linovelib.com', 'bilinovel.com'))
    seen, uniq = set(), []
    for h in order:
        if h not in seen:
            seen.add(h)
            uniq.append(h.rstrip('/'))
    return uniq, aid


def main():
    global _HOST
    parser = argparse.ArgumentParser(description='Fetch bilinovel/linovelib novel via pure HTTP')
    parser.add_argument('-u', '--url', required=True, help='Book URL (…/novel/<aid>.html)')
    parser.add_argument('-o', '--output', help='Output TXT file')
    parser.add_argument('--html', help='Output per-chapter HTML files to this directory')
    parser.add_argument('-d', '--delay', type=float, default=1.5, help='Delay between chapters')
    parser.add_argument('--start', type=int, default=1, help='Start chapter index')
    parser.add_argument('--end', type=int, default=0, help='End chapter index (0=all)')
    parser.add_argument('--force', action='store_true', help='Re-fetch even if in memory')
    args = parser.parse_args()

    hosts, aid = parse_url(args.url)
    _session(hosts)
    print(f"Book ID: {aid} @ {_HOST}" + (f"  (fallback: {hosts[1]})" if len(hosts) > 1 else ""))

    # Warm-up GET establishes the jieqiVisitId cookie the server checks; without it
    # chapters come back truncated with the decoy "content load failed" marker.
    book_html = _get(f'/novel/{aid}.html')
    title, author, cover_url, volumes = parse_book(aid, book_html)
    if not volumes:
        # Some layouts link chapters straight from the book page (no volume pages).
        chapters = parse_volume(aid, book_html)
        if not chapters:
            print('ERROR: no volumes or chapters found', file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Volumes: {len(volumes)}")
        chapters = []
        seen = set()
        for vid, vname in volumes:
            vhtml = _get(f'/novel/{aid}/vol_{vid}.html', referer=book_html and f'{_HOST}/novel/{aid}.html')
            got = parse_volume(aid, vhtml)
            for cid, ctitle in got:
                if cid not in seen:
                    seen.add(cid)
                    chapters.append((cid, ctitle))
            print(f"  vol {vid} ({vname[:24]}): {len(got)} chapters", flush=True)
            time.sleep(args.delay)

    author = author or '未知'
    title = title or aid
    print(f"Title: {title}")
    print(f"Author: {author}")
    print(f"Found {len(chapters)} chapters")
    if not chapters:
        sys.exit(1)

    total = len(chapters)
    start_idx = max(0, args.start - 1)
    end_idx = min(total, args.end) if args.end > 0 else total
    chapters = chapters[start_idx:end_idx]

    html_mode = bool(args.html)
    html_output_dir = img_dir = None
    cover_file = ''
    chapters_info = []
    if html_mode:
        html_output_dir = Path(args.html)
        html_output_dir.mkdir(parents=True, exist_ok=True)
        img_dir = html_output_dir / 'images'
        img_dir.mkdir(parents=True, exist_ok=True)
        if cover_url:
            ext = '.jpg'
            low = cover_url.lower()
            for e in ('.png', '.webp', '.gif'):
                if low.endswith(e):
                    ext = e
            ck = download_image(cover_url, img_dir, f'cover{ext}')
            if ck:
                cover_file = ck
                print(f"Cover: {cover_file}")

    parts = [f"# {title}", f"作者: {author}", '', '']
    fetched_set = _get_fetched(aid) if not args.force else set()

    for i, (cid, ch_title) in enumerate(chapters):
        ch_num = start_idx + i + 1
        prefix = f"  [{ch_num}/{total}]"
        if cid in fetched_set:
            print(f"{prefix} (skip)", flush=True)
            continue
        # Resume: an existing chapter file means a previous run already got it.
        if html_mode and not args.force:
            safe_title = re.sub(r'[<>:"/\\|?*]', '_', ch_title or f'chapter{ch_num}')
            if (html_output_dir / f"{ch_num:04d}_{safe_title}.html").exists():
                print(f"{prefix} (exists)", flush=True)
                continue
        print(f"{prefix} {ch_title[:40]}...", end=' ', flush=True)

        try:
            text, pages = fetch_chapter(aid, cid, referer=f'{_HOST}/novel/{aid}.html')
            if not text:
                print('(empty)')
                continue
            if html_mode:
                body = f'<div class="chapter">\n{_text_to_html(text)}\n</div>'
                fname = save_html_chapter(html_output_dir, ch_num, ch_title, body)
                chapters_info.append({'num': ch_num, 'title': ch_title, 'file': fname, 'type': 'chapter'})
                print(f'OK ({pages}p)')
            else:
                parts.append(f'\n\n第{ch_num}章 {ch_title}\n')
                parts.append(text)
                print(f'OK ({pages}p)')
        except Exception as e:
            print(f'FAILED: {e}')
            if not html_mode:
                parts.append(f'\n\n第{ch_num}章 {ch_title}\n[获取失败: {e}]')

        time.sleep(args.delay)

    if html_mode:
        if cover_file:
            cover_body = (f'<div style="text-align:center;padding:2em 0;">'
                          f'<img src="images/{cover_file}" alt="封面" style="max-width:100%;height:auto;"/></div>')
            fname = save_html_chapter(html_output_dir, 0, '封面', cover_body)
            chapters_info.insert(0, {'num': 0, 'title': '封面', 'file': fname, 'type': 'cover'})
        info = {'book_name': title, 'author': author, 'cover': cover_file, 'chapters': chapters_info}
        (html_output_dir / 'book_info.json').write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\nHTML chapters saved to: {html_output_dir}")
        print(f"Total: {len(chapters_info)} chapters")
    else:
        if not args.output:
            safe = re.sub(r'[^\w一-鿿]', '_', title).strip('_') or f'bilinovel_{aid}'
            args.output = f'{safe}.txt'
        combined = '\n'.join(parts)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(combined)
        print(f"\nSaved: {args.output} ({len(combined):,} chars)")

    for cid, _ in chapters:
        _mark_chapter(aid, cid, title)

    # Report any residual truncation so a silent partial fetch cannot go unnoticed.
    if html_mode:
        try:
            bad = [f.name for f in Path(html_output_dir).glob('*.html')
                   if _is_truncated(f.read_text(encoding='utf-8', errors='replace'))]
            if bad:
                print(f"WARNING: {len(bad)} file(s) still truncated (e.g. {bad[:3]})", file=sys.stderr)
                print("         re-run to retry them, or check the host fallback.", file=sys.stderr)
        except Exception:
            pass

    try:
        from fetch_history import record as _rec
        _rec('bilinovel', title, aid, len(chapters), 'chapters',
             args.html or args.output, 'web')
    except Exception:
        pass
    print("Done!")


if __name__ == '__main__':
    main()
