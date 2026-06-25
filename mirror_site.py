import hashlib
import os
import posixpath
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, quote
from urllib.request import Request, urlopen

ROOT_URL = 'https://atinph.org/'
OUTPUT_DIR = Path('site-mirror')
USER_AGENT = 'Mozilla/5.0 (compatible; CodexMirror/1.0)'
ALLOWED_PAGE_HOSTS = {'atinph.org', 'www.atinph.org'}
ALLOWED_ASSET_HOSTS = {
    'atinph.org', 'www.atinph.org',
    'images.squarespace-cdn.com', 'static1.squarespace.com', 'assets.squarespace.com',
    'definitions.sqspcdn.com', 'use.typekit.net', 'p.typekit.net', 'fonts.gstatic.com', 'fonts.googleapis.com'
}
PAGE_EXTENSIONS = {'', '.html', '.htm'}
ASSET_EXTENSIONS = {
    '.css', '.js', '.mjs', '.json', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico',
    '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp4', '.webm', '.pdf', '.txt', '.xml', '.map', '.bin'
}
URL_RE = re.compile(r'url\(([^)]+)\)')
IMPORT_RE = re.compile(r'@import\s+(?:url\()?\s*[\"\']?([^\"\')\s]+)')

FONT_URLS = {
    'https://static1.squarespace.com/static/6243d15262ab610548237019/t/6243e3ce79c660786d2c3f9b/1648616398626/ApfelGrotezk-Regular.woff',
    'https://static1.squarespace.com/static/6243d15262ab610548237019/t/6243e492d39a550052f17340/1648616595008/ApfelGrotezk-Fett.woff'
}

PAGES = set()
ASSETS = set()
FETCHED = set()
FAILURES = []

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ('href', 'src', 'data-src', 'data-image', 'poster'):
            val = attrs.get(key)
            if val:
                self.links.append((key, val))
        srcset = attrs.get('srcset')
        if srcset:
            self.links.append(('srcset', srcset))
        style = attrs.get('style')
        if style:
            for u in extract_css_urls(style):
                self.links.append(('style', u))


def normalize_url(url: str, base: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if url.startswith(('mailto:', 'tel:', 'javascript:', '#', 'data:')):
        return None
    if url.startswith('//'):
        url = 'https:' + url
    full = urljoin(base, url)
    parsed = urlparse(full)
    if parsed.scheme not in ('http', 'https'):
        return None
    path = parsed.path or '/'
    normalized = urlunparse((parsed.scheme, parsed.netloc.lower(), path, '', parsed.query, ''))
    return normalized


def has_asset_extension(path: str) -> bool:
    return posixpath.splitext(path)[1].lower() in ASSET_EXTENSIONS


def is_page_url(url: str) -> bool:
    p = urlparse(url)
    if p.netloc.lower() not in ALLOWED_PAGE_HOSTS:
        return False
    if p.query and any(x in p.query for x in ('format=', 'author=', 'tag=', 'month=', 'view=')):
        return False
    ext = posixpath.splitext(p.path)[1].lower()
    return ext in PAGE_EXTENSIONS


def is_asset_url(url: str) -> bool:
    p = urlparse(url)
    host = p.netloc.lower()
    if host not in ALLOWED_ASSET_HOSTS:
        return False
    if host == 'images.squarespace-cdn.com':
        return True
    if has_asset_extension(p.path):
        return True
    if any(x in p.query for x in ('format=', 'nocustom=')):
        return True
    return False


def page_file_path(url: str) -> Path:
    p = urlparse(url)
    rel = p.path.lstrip('/')
    if rel == '' or rel.endswith('/'):
        rel = rel + 'index.html' if rel else 'index.html'
    elif posixpath.splitext(rel)[1].lower() not in {'.html', '.htm'}:
        rel = rel + '/index.html'
    return OUTPUT_DIR / rel


def guess_extension(host: str, path: str, query: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext:
        return ext
    if 'typekit.net' in host:
        return '.js'
    if 'format=' in query:
        fmt = re.search(r'format=([A-Za-z0-9]+)', query)
        if fmt:
            m = fmt.group(1).lower()
            if m.endswith('w') and m[:-1].isdigit():
                return '.png'
            if m in {'png','jpg','jpeg','webp','gif','ico','svg'}:
                return '.' + m
    if host == 'images.squarespace-cdn.com':
        return '.bin'
    return '.bin'


def shorten_filename(base: str, ext: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9._+-]+', '_', base)[:80].rstrip('._-') or 'asset'
    digest = hashlib.sha1(base.encode()).hexdigest()[:10]
    return f'{safe}__{digest}{ext}'


def safe_asset_rel_path(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower()
    path = p.path.lstrip('/')
    if not path or path.endswith('/'):
        path += 'index'
    dirname, filename = posixpath.split(path)
    if not filename:
        filename = 'index'
    base, ext = posixpath.splitext(filename)
    ext = ext or guess_extension(host, p.path, p.query)
    if len(filename) > 100 or host == 'use.typekit.net' or host == 'p.typekit.net':
        filename = shorten_filename(base + ('?' + p.query if p.query else ''), ext)
    else:
        filename = f'{base}{ext}'
    if p.query:
        qh = hashlib.sha1(p.query.encode()).hexdigest()[:10]
        base2, ext2 = posixpath.splitext(filename)
        filename = f'{base2}__q{qh}{ext2}'
    parts = ['assets', host]
    if dirname:
        parts.extend(dirname.split('/'))
    parts.append(filename)
    return posixpath.join(*parts)


def asset_file_path(url: str) -> Path:
    return OUTPUT_DIR / safe_asset_rel_path(url)


def relative_url(from_path: Path, to_path: Path) -> str:
    return posixpath.relpath(to_path.as_posix(), start=from_path.parent.as_posix())


def fetch(url: str) -> Tuple[bytes, str]:
    req = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get('Content-Type', '')
        return resp.read(), ctype


def fetch_with_fallback(url: str) -> Tuple[bytes, str]:
    try:
        return fetch(url)
    except Exception:
        p = urlparse(url)
        if p.query and p.netloc.lower() == 'images.squarespace-cdn.com':
            fallback = urlunparse((p.scheme, p.netloc, p.path, '', '', ''))
            return fetch(fallback)
        raise


def extract_css_urls(text: str):
    out = []
    for m in URL_RE.finditer(text):
        val = m.group(1).strip().strip('"\'')
        if val and not val.startswith('data:'):
            out.append(val)
    for m in IMPORT_RE.finditer(text):
        val = m.group(1).strip().strip('"\'')
        if val and not val.startswith('data:'):
            out.append(val)
    return out


def discover_from_html(html: str, base_url: str):
    parser = LinkParser()
    parser.feed(html)
    for kind, raw in parser.links:
        if kind == 'srcset':
            candidates = [p.strip().split()[0] for p in raw.split(',') if p.strip()]
        else:
            candidates = [raw]
        for item in candidates:
            url = normalize_url(item, base_url)
            if not url:
                continue
            if is_page_url(url):
                PAGES.add(url)
            elif is_asset_url(url):
                ASSETS.add(url)


def map_url(raw: str, base_url: str, current_file: Path) -> Optional[str]:
    if raw.lower().startswith('data:') or raw.startswith('#'):
        return None
    abs_url = normalize_url(raw, base_url)
    if not abs_url:
        return None
    if is_page_url(abs_url):
        target = page_file_path(abs_url)
        rel = relative_url(current_file, target)
        return './' if rel == 'index.html' else rel
    if is_asset_url(abs_url):
        target = asset_file_path(abs_url)
        return relative_url(current_file, target)
    return None


def rewrite_attr_urls(text: str, current_page_url: str, current_file: Path) -> str:
    def sub_attr(pattern, replacer, value_transform=None):
        def _repl(m):
            before, val, after = m.group(1), m.group(2), m.group(3)
            new_val = value_transform(val) if value_transform else replacer(val)
            return before + new_val + after
        return re.sub(pattern, _repl, text, flags=re.I | re.S)

    def single(val):
        return map_url(val, current_page_url, current_file) or val

    def srcset_val(val):
        parts = []
        for part in val.split(','):
            item = part.strip()
            if not item:
                continue
            segs = item.split()
            segs[0] = map_url(segs[0], current_page_url, current_file) or segs[0]
            parts.append(' '.join(segs))
        return ', '.join(parts)

    def style_val(val):
        def _css(m):
            raw = m.group(1)
            stripped = raw.strip().strip('"\'')
            new = map_url(stripped, current_page_url, current_file)
            return m.group(0) if not new else f'url("{new}")'
        return URL_RE.sub(_css, val)

    text = re.sub(r'((?:href|src|poster|data-src|data-image)\s*=\s*["\'])([^"\']+)(["\'])',
                  lambda m: m.group(1) + (single(m.group(2))) + m.group(3), text, flags=re.I | re.S)
    text = re.sub(r'((?:srcset)\s*=\s*["\'])([^"\']+)(["\'])',
                  lambda m: m.group(1) + srcset_val(m.group(2)) + m.group(3), text, flags=re.I | re.S)
    text = re.sub(r'(style\s*=\s*["\'])(.*?)(["\'])',
                  lambda m: m.group(1) + style_val(m.group(2)) + m.group(3), text, flags=re.I | re.S)
    return text


def rewrite_html(html: str, current_page_url: str) -> str:
    return rewrite_attr_urls(html, current_page_url, page_file_path(current_page_url))


def fetch_page(url: str):
    if url in FETCHED:
        return
    FETCHED.add(url)
    print('PAGE', url)
    data, _ = fetch(url)
    text = data.decode('utf-8', 'ignore')
    discover_from_html(text, url)
    out = page_file_path(url)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rewrite_html(text, url), encoding='utf-8')


def rewrite_css(css_text: str, css_url: str) -> str:
    current_file = asset_file_path(css_url)

    def replace_url_match(m):
        raw = m.group(1)
        stripped = raw.strip().strip('"\'')
        new = map_url(stripped, css_url, current_file)
        if new is None:
            return m.group(0)
        return f'url("{new}")'

    css_text = URL_RE.sub(replace_url_match, css_text)

    def replace_import(m):
        raw = m.group(1)
        new = map_url(raw, css_url, current_file)
        return m.group(0) if new is None else m.group(0).replace(raw, new, 1)

    css_text = IMPORT_RE.sub(replace_import, css_text)

    if 'Apfel Grotezk' in css_text:
        reg_woff2_rel = relative_url(current_file, OUTPUT_DIR / 'assets' / 'fonts' / 'ApfelGrotezk-Regular.woff2')
        reg_woff_rel = relative_url(current_file, OUTPUT_DIR / 'assets' / 'fonts' / 'ApfelGrotezk-Regular.woff')
        reg_otf_rel = relative_url(current_file, OUTPUT_DIR / 'assets' / 'fonts' / 'ApfelGrotezk-Regular.otf')

        bold_woff2_rel = relative_url(current_file, OUTPUT_DIR / 'assets' / 'fonts' / 'ApfelGrotezk-Fett.woff2')
        bold_woff_rel = relative_url(current_file, OUTPUT_DIR / 'assets' / 'fonts' / 'ApfelGrotezk-Fett.woff')
        bold_otf_rel = relative_url(current_file, OUTPUT_DIR / 'assets' / 'fonts' / 'ApfelGrotezk-Fett.otf')

        new_font_face = f"""@font-face {{
  font-family: 'Apfel Grotezk';
  src: url("{reg_woff2_rel}") format("woff2"),
       url("{reg_woff_rel}") format("woff"),
       url("{reg_otf_rel}") format("opentype");
  font-weight: normal;
  font-style: normal;
}}
@font-face {{
  font-family: 'Apfel Grotezk';
  src: url("{bold_woff2_rel}") format("woff2"),
       url("{bold_woff_rel}") format("woff"),
       url("{bold_otf_rel}") format("opentype");
  font-weight: bold;
  font-style: normal;
}}"""
        css_text = re.sub(
            r'@font-face\s*\{\s*font-family\s*:\s*[\'"]Apfel Grotezk[\'"]\s*;[^}]*\}',
            new_font_face,
            css_text,
            flags=re.I
        )

    return css_text


def fetch_asset(url: str):
    if url in FETCHED:
        return
    FETCHED.add(url)
    print('ASSET', url)
    if url in FONT_URLS:
        print('ASSET (LOCAL FONT skipped download)', url)
        return
    data, ctype = fetch_with_fallback(url)
    out = asset_file_path(url)
    out.parent.mkdir(parents=True, exist_ok=True)
    if 'site-bundle' in out.name and out.suffix == '.js':
        text = data.decode('utf-8', 'ignore')
        old_pattern = '"localhost"===window.location.hostname?a.p=window.location.origin+"/":e&&e.endsWith(t)&&(a.p=e.slice(0,-8))'
        new_pattern = 'a.p=(()=>{const cs=document.currentScript||document.querySelector(\'script[src*=\"site-bundle\"]\');const src=cs?cs.src:\"\";const idx=src.lastIndexOf(\"scripts/\");return idx>=0?src.substring(0,idx):(e&&e.endsWith(t)?e.slice(0,-8):\"/\")})()'
        if old_pattern in text:
            text = text.replace(old_pattern, new_pattern)
            print("Successfully patched site-bundle.js publicPath for local environment")
        else:
            print("WARNING: publicPath pattern not found in site-bundle.js!", file=sys.stderr)
        out.write_text(text, encoding='utf-8')
    elif out.suffix == '.css' or 'text/css' in ctype:
        text = data.decode('utf-8', 'ignore')
        for ref in extract_css_urls(text):
            abs_url = normalize_url(ref, url)
            if abs_url and is_asset_url(abs_url):
                ASSETS.add(abs_url)
        out.write_text(rewrite_css(text, url), encoding='utf-8')
    else:
        out.write_bytes(data)


def bootstrap_pages():
    initial = [ROOT_URL, urljoin(ROOT_URL, 'about'), urljoin(ROOT_URL, 'events'), urljoin(ROOT_URL, 'media'), urljoin(ROOT_URL, 'publications'), urljoin(ROOT_URL, 'contact-us'), urljoin(ROOT_URL, 'home')]
    for u in initial:
        PAGES.add(normalize_url(u, ROOT_URL))
    cart_script = 'https://static1.squarespace.com/static/vta/5c5a519771c10ba3470d8101/scripts/floating-cart.333bd5aee1885e7af603.js'
    ASSETS.add(normalize_url(cart_script, ROOT_URL))


def clean_output():
    if OUTPUT_DIR.exists():
        for root, dirs, files in os.walk(OUTPUT_DIR, topdown=False):
            for f in files:
                Path(root, f).unlink()
            for d in dirs:
                Path(root, d).rmdir()
    OUTPUT_DIR.mkdir(exist_ok=True)


def copy_local_fonts():
    src_dir = Path(__file__).parent / 'fonts'
    dest_dir = OUTPUT_DIR / 'assets' / 'fonts'
    dest_dir.mkdir(parents=True, exist_ok=True)
    if src_dir.exists():
        import shutil
        for item in src_dir.iterdir():
            if item.is_file() and item.suffix in {'.otf', '.woff', '.woff2'}:
                shutil.copy2(item, dest_dir / item.name)
        print(f"Copied local fonts from {src_dir} to {dest_dir}")
    else:
        print("WARNING: fonts directory not found!", file=sys.stderr)


def main():
    clean_output()
    copy_local_fonts()
    bootstrap_pages()
    while True:
        pending_pages = [u for u in sorted(PAGES) if u not in FETCHED]
        if not pending_pages:
            break
        for u in pending_pages:
            try:
                fetch_page(u)
            except Exception as e:
                FAILURES.append((u, str(e)))
                print('FAILED PAGE', u, e, file=sys.stderr)
    while True:
        pending_assets = [u for u in sorted(ASSETS) if u not in FETCHED]
        if not pending_assets:
            break
        for u in pending_assets:
            try:
                fetch_asset(u)
            except Exception as e:
                FAILURES.append((u, str(e)))
                print('FAILED ASSET', u, e, file=sys.stderr)
    (OUTPUT_DIR / 'README.md').write_text(
        '# ATINPH local mirror\n\n'
        '- Serve this folder with a static HTTP server.\n'
        '- Main entry: `index.html`\n'
        '- Pages mirrored: home, about, events, media, publications, contact-us.\n',
        encoding='utf-8'
    )
    print(f'Complete. Pages: {len(PAGES)} Assets: {len(ASSETS)} Failures: {len(FAILURES)}')
    if FAILURES:
        (OUTPUT_DIR / 'mirror-failures.txt').write_text('\n'.join(f'{u} :: {e}' for u,e in FAILURES), encoding='utf-8')

if __name__ == '__main__':
    main()
