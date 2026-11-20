#!/usr/bin/env python3
"""Weekly dead-stream check for the BlindRadio-directory playlists.

Probes every stream URL in the repo's .m3u files twice (a stream is dead
only after both probes fail). For each dead stream, searches the station
directories BlindRadio itself supports (Radio Browser, Icecast,
SHOUTcast, IPRD, Radio.net, TuneIn, Live365, Online Radio Box, Moondust,
Recommended Radio Streams) plus the big GitHub collections (iptv-org
index; Moondust and Recommended live on GitHub too) for a replacement
for the SAME station. Candidates are probe-verified, then ranked by
bitrate, then codec quality (FLAC > Opus > AAC > OGG > MP3 at equal
bitrate); the best verified one replaces the URL in place. Dead streams
with no verified replacement stay listed and are named in the report.
Nothing is ever deleted.

Stdlib only. Usage: dead-stream-check.py [--root DIR] [--report FILE]
"""

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA_PROBE = "VLC/3.0.20 LibVLC/3.0.20"
UA_PROBE2 = "WinampMPEG/5.90"
UA_WEB = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
TIMEOUT = 12

CODEC_SCORE = {"FLAC": 6, "OPUS": 5, "AAC": 4, "AACP": 4, "AAC+": 4,
               "OGG": 3, "VORBIS": 3, "MP3": 2, "WMA": 1, "": 0}
CTYPE_CODEC = {"audio/mpeg": "MP3", "audio/mp3": "MP3", "audio/aac": "AAC",
               "audio/aacp": "AACP", "audio/ogg": "OGG", "audio/opus": "OPUS",
               "audio/flac": "FLAC", "audio/x-flac": "FLAC",
               "audio/x-ms-wma": "WMA", "audio/mp4": "AAC"}


def fetch(url, ua=UA_WEB, timeout=25, limit=2_000_000, data=None, headers=None,
          _depth=0):
    h = {"User-Agent": ua, "Accept-Encoding": "identity"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # Follow plain http(s) redirects urllib refuses (proxy hops).
        # icy:// / icyx:// 301s are left for probe() to judge.
        loc = e.headers.get("Location") if e.headers else None
        if loc and _depth < 2 and loc.startswith(("http://", "https://")):
            return fetch(loc, ua=ua, timeout=timeout, limit=limit,
                         _depth=_depth + 1)
        raise
    with r:
        try:
            body = r.read(limit)
        except Exception as e:  # IncompleteRead: server closed early
            body = getattr(e, "partial", b"") or b""
        return r.status, r.headers, body


# ---------------------------------------------------------------- probe

def _playlist_entries(body, base_url):
    text = body.decode("utf-8", "replace")
    urls = []
    bandwidth = None
    if text.lstrip().startswith("#EXTM3U"):
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"#EXT-X-STREAM-INF:.*BANDWIDTH=(\d+)", line)
            if m:
                bandwidth = int(m.group(1)) // 1000
            elif line and not line.startswith("#"):
                urls.append(urllib.parse.urljoin(base_url, line))
    elif "[playlist]" in text[:64].lower() or "File1=" in text:
        for line in text.splitlines():
            if line.lower().startswith("file") and "=" in line:
                urls.append(line.split("=", 1)[1].strip())
    elif text.strip().startswith(("http://", "https://")):
        urls = [l.strip() for l in text.splitlines() if l.strip().startswith("http")]
    return urls, bandwidth


def probe(url, ua=UA_PROBE, depth=0):
    """Return dict(alive, bitrate, codec, final_url, why)."""
    out = {"alive": False, "bitrate": None, "codec": "", "final_url": url, "why": ""}
    try:
        status, headers, body = fetch(url, ua=ua, timeout=TIMEOUT, limit=32768)
    except Exception as e:  # DNS, connect, TLS, HTTP error, timeout
        # A 301 to an icy:// / icyx:// Shoutcast URL is a working
        # stream for players, even though plain HTTP cannot play it.
        loc = ""
        if isinstance(e, urllib.error.HTTPError) and e.headers:
            loc = e.headers.get("Location") or ""
        if isinstance(e, urllib.error.HTTPError) \
                and e.code in (301, 302, 303, 307, 308) \
                and loc.startswith(("icy://", "icyx://")):
            out["alive"] = True
            out["why"] = "icy redirect"
            return out
        out["why"] = f"{type(e).__name__}: {e}"[:120]
        return out
    ctype = headers.get_content_type()
    icy_br = headers.get("icy-br") or headers.get("ice-audio-info") or ""
    m = re.search(r"bitrate=(\d+)", icy_br or "")
    if m:
        out["bitrate"] = int(m.group(1))
    elif icy_br.strip().isdigit():
        out["bitrate"] = int(icy_br.strip())
    if ctype in ("application/vnd.apple.mpegurl", "application/x-mpegurl",
                 "audio/x-scpls", "audio/scpls", "application/pls+xml") \
            or body.lstrip()[:7] == b"#EXTM3U" or b"[playlist]" in body[:64].lower():
        if depth >= 1:
            out["why"] = "nested playlist"
            return out
        entries, bw = _playlist_entries(body, url)
        if bw:
            out["bitrate"] = bw
        if not entries:
            out["why"] = "empty playlist"
            return out
        sub = probe(entries[0], ua=ua, depth=depth + 1)
        sub["bitrate"] = sub["bitrate"] or out["bitrate"]
        return sub
    if ctype in ("text/html", "text/plain") and not headers.get("icy-name"):
        out["why"] = f"not audio ({ctype})"
        return out
    if not body:
        out["why"] = "empty body"
        return out
    out["alive"] = True
    out["codec"] = CTYPE_CODEC.get(ctype, "")
    out["final_url"] = url
    return out


def probe_dead(url):
    """Two probes, different clients; dead only if both fail."""
    first = probe(url, ua=UA_PROBE)
    if first["alive"]:
        return None
    time.sleep(1)
    second = probe(url, ua=UA_PROBE2)
    if second["alive"]:
        return None
    return second["why"] or first["why"]


# ---------------------------------------------------------------- names

def norm(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(radio|fm|am|the|online|live|stream|hd|uk|de)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_norm(a, b):
    if not a or not b:
        return False
    if a == b or (len(a) >= 5 and a in b) or (len(b) >= 5 and b in a):
        return True
    if not set(a.split()) & set(b.split()):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


def name_match(want, got):
    return match_norm(norm(want), norm(got))


# ---------------------------------------------------------------- sources
# Each returns [{name, url, bitrate, codec, source}]. Failures are logged
# and swallowed: one dead directory must not sink the run.

def src_radio_browser(name):
    q = urllib.parse.quote(name)
    _, _, body = fetch(f"https://de1.api.radio-browser.info/json/stations/search"
                       f"?name={q}&hidebroken=true&limit=40", timeout=20)
    out = []
    for st in json.loads(body):
        url = st.get("url_resolved") or st.get("url") or ""
        if url.startswith(("http://", "https://")):
            out.append({"name": st.get("name", ""), "url": url,
                        "bitrate": st.get("bitrate") or None,
                        "codec": (st.get("codec") or "").upper(),
                        "source": "Radio Browser"})
    return out


_icecast_cache = None


def src_icecast(name):
    global _icecast_cache
    if _icecast_cache is None:
        _, _, body = fetch("https://dir.xiph.org/yp.xml", timeout=60,
                           limit=60_000_000)
        text = body.decode("utf-8", "replace")
        _icecast_cache = []
        for entry in re.findall(r"<entry>.*?</entry>", text, re.S):
            def tag(t):
                m = re.search(rf"<{t}>(.*?)</{t}>", entry, re.S)
                return m.group(1).strip() if m else ""
            sname = tag("server_name")
            br = tag("bitrate")
            if sname:
                _icecast_cache.append(
                    (sname, norm(sname), tag("listen_url"),
                     int(br) if br.isdigit() else None,
                     tag("server_type").replace("audio/", "").upper()))
    want = norm(name)
    return [{"name": sname, "url": url, "bitrate": br, "codec": codec,
             "source": "Icecast"}
            for sname, snorm, url, br, codec in _icecast_cache
            if url and match_norm(want, snorm)]


def src_shoutcast(name):
    data = urllib.parse.urlencode({"query": name}).encode()
    _, _, body = fetch("https://directory.shoutcast.com/Search/UpdateSearch",
                       data=data, timeout=20,
                       headers={"X-Requested-With": "XMLHttpRequest",
                                "Referer": "https://directory.shoutcast.com/"})
    out = []
    for row in json.loads(body):
        sid = row.get("ID") or row.get("id")
        if not sid:
            continue
        pls = f"https://directory.shoutcast.com/sbin/tunein-station.pls?id={sid}"
        entries, _ = _playlist_entries(fetch(pls, timeout=15)[2], pls)
        fmt = (row.get("Format") or "").lower()
        codec = "MP3" if "mpeg" in fmt else ("AAC" if "aac" in fmt else "")
        for u in entries[:1]:
            out.append({"name": row.get("Name", ""), "url": u,
                        "bitrate": row.get("Bitrate") or None,
                        "codec": codec, "source": "SHOUTcast"})
    return out


_iprd_cache = None


def src_iprd(name):
    global _iprd_cache
    if _iprd_cache is None:
        _, _, body = fetch("https://iprd-org.github.io/iprd/site_data/metadata/catalog.json",
                           timeout=60, limit=60_000_000)
        _iprd_cache = json.loads(body)
    stations = _iprd_cache.get("stations", _iprd_cache) \
        if isinstance(_iprd_cache, dict) else _iprd_cache
    want = norm(name)
    out = []
    for st in stations:
        sname = st.get("name") or st.get("title") or ""
        streams = st.get("streams") or ([st["url"]] if st.get("url") else [])
        if sname and match_norm(want, norm(sname)):
            for u in streams[:2]:
                if isinstance(u, dict):
                    u = u.get("url", "")
                if str(u).startswith(("http://", "https://")):
                    out.append({"name": sname, "url": str(u), "bitrate": None,
                                "codec": "", "source": "IPRD"})
    return out


def src_radionet(name):
    q = urllib.parse.quote(name)
    _, _, body = fetch(f"https://prod.radio-api.net/stations/search?query={q}&count=24&offset=0",
                       timeout=20)
    out = []
    for p in json.loads(body).get("playables", []):
        for s in p.get("streams", []):
            u = (s.get("url") or "").strip()
            if u.startswith(("http://", "https://")):
                out.append({"name": p.get("name", ""), "url": u,
                            "bitrate": s.get("bitrate") or None,
                            "codec": "", "source": "Radio.net"})
    return out


def src_tunein(name):
    q = urllib.parse.quote(name)
    _, _, body = fetch(f"https://opml.radiotime.com/Search.ashx?query={q}"
                       f"&types=station&formats=mp3,aac,ogg,html&render=json",
                       timeout=20)
    out = []
    for el in json.loads(body).get("body", []):
        tune = el.get("URL", "")
        if "Tune.ashx" not in tune:
            continue
        try:
            entries, _ = _playlist_entries(fetch(tune, timeout=15)[2], tune)
        except Exception:
            entries = []
        for u in entries[:2]:
            out.append({"name": el.get("text", ""), "url": u,
                        "bitrate": int(el["bitrate"]) if str(el.get("bitrate", "")).isdigit() else None,
                        "codec": "", "source": "TuneIn"})
    return out


def src_live365(name):
    q = urllib.parse.quote(name)
    _, _, body = fetch(f"https://live365.com/listen/search?query={q}", timeout=20,
                       limit=1_000_000)
    text = body.decode("utf-8", "replace")
    out, seen = [], set()
    for m in re.finditer(r"streaming\.live365\.com/(a\d+)", text):
        sid = m.group(1)
        if sid in seen:
            continue
        seen.add(sid)
        window = text[max(0, m.start() - 600):m.start() + 200]
        titles = re.findall(r'"(?:title|name)"\s*:\s*"([^"]{2,80})"', window)
        sname = titles[-1] if titles else ""
        if sname and name_match(name, sname):
            out.append({"name": sname, "url": f"https://streaming.live365.com/{sid}",
                        "bitrate": None, "codec": "AAC", "source": "Live365"})
    return out


def src_orb(name):
    q = urllib.parse.quote(name)
    _, _, body = fetch(f"https://onlineradiobox.com/search?q={q}", timeout=20,
                       limit=1_000_000)
    text = body.decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'stream="([^"]+)"[^>]*radioName="([^"]+)"', text):
        url, sname = m.group(1), m.group(2)
        if url.startswith(("http://", "https://")):
            out.append({"name": sname, "url": url, "bitrate": None,
                        "codec": "", "source": "Online Radio Box"})
    return out


_moondust_countries = ["US", "GB", "DE", "FR", "CA", "AU", "ES", "IT", "NL",
                       "BE", "CH", "PT", "IE", "AT", "PL", "BR", "MX", "IN"]
_moondust_cache = None


def src_moondust(name):
    global _moondust_cache
    if _moondust_cache is None:
        _moondust_cache = []
        for cc in _moondust_countries:
            try:
                _, _, body = fetch(
                    f"https://raw.githubusercontent.com/12moon-open/moondust/main/v1/stations/{cc}.ndjson",
                    timeout=30, limit=30_000_000)
                for line in body.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        _moondust_cache.append(json.loads(line))
            except Exception:
                pass
    out = []
    for st in _moondust_cache:
        if st.get("status") == "retired":
            continue
        sname = st.get("name", "")
        if sname and match_norm(norm(name), norm(sname)):
            for s in st.get("streams", []):
                u = s.get("url", "")
                if u.startswith(("http://", "https://")):
                    out.append({"name": sname, "url": u,
                                "bitrate": s.get("bitrate_kbps"),
                                "codec": (s.get("codec") or "").upper(),
                                "source": "Moondust (GitHub)"})
    return out


_github_cache = None


def src_github_collections(name):
    """iptv-org's index (built from the GitHub repo) + Recommended Radio
    Streams (the app's GitHub-backed source), name-matched locally."""
    global _github_cache
    if _github_cache is None:
        _github_cache = []
        try:
            _, _, body = fetch("https://iptv-org.github.io/iptv/index.m3u",
                               timeout=60, limit=80_000_000)
            lines = body.decode("utf-8", "replace").splitlines()
            for i, line in enumerate(lines[:-1]):
                if line.startswith("#EXTINF"):
                    sname = line.rsplit(",", 1)[-1].strip()
                    _github_cache.append((sname, norm(sname), lines[i + 1].strip()))
        except Exception:
            pass
        try:
            _, _, body = fetch(
                "https://api.github.com/repos/deroverda/recommended-radio-streams/contents/",
                timeout=20)
            for item in json.loads(body):
                if item["name"].endswith((".m3u", ".txt", ".pls")):
                    _, _, raw = fetch(item["download_url"], timeout=20)
                    lines = raw.decode("utf-8", "replace").splitlines()
                    for i, line in enumerate(lines[:-1]):
                        if line.startswith("#EXTINF"):
                            sname = line.rsplit(",", 1)[-1].strip()
                            _github_cache.append(
                                (sname, norm(sname), lines[i + 1].strip()))
        except Exception:
            pass
    want = norm(name)
    return [{"name": sname, "url": u, "bitrate": None, "codec": "",
             "source": "GitHub collections"}
            for sname, snorm, u in _github_cache
            if u.startswith(("http://", "https://")) and match_norm(want, snorm)]


SOURCES = [src_radio_browser, src_icecast, src_shoutcast, src_iprd,
           src_radionet, src_tunein, src_live365, src_orb, src_moondust,
           src_github_collections]


def find_replacement(name, dead_url):
    candidates, seen = [], set()
    for fn in SOURCES:
        try:
            for c in fn(name):
                if c["url"] != dead_url and c["url"] not in seen \
                        and name_match(name, c["name"]):
                    seen.add(c["url"])
                    candidates.append(c)
        except Exception as e:
            print(f"  source {fn.__name__} failed: {e}", file=sys.stderr)
    verified = []
    # Probe every candidate so the highest-quality stream wins, not
    # merely the first one a source happens to list.
    for c in candidates:
        p = probe(c["url"])
        if p["alive"]:
            c = dict(c)
            c["url"] = p["final_url"]
            c["bitrate"] = p["bitrate"] or c["bitrate"]
            c["codec"] = p["codec"] or c["codec"]
            verified.append(c)
    verified.sort(key=lambda c: (c["bitrate"] or 0,
                                 CODEC_SCORE.get((c["codec"] or "").upper(), 0)),
                  reverse=True)
    return verified[0] if verified else None


# ---------------------------------------------------------------- main

def parse_playlists(root):
    import os
    files = {}
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".m3u"):
            continue
        path = os.path.join(root, fname)
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        entries = []
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF") and i + 1 < len(lines) \
                    and lines[i + 1].startswith(("http://", "https://")):
                entries.append({"file": fname, "line": i + 1,
                                "name": line.rsplit(",", 1)[-1].strip(),
                                "url": lines[i + 1].strip()})
        files[fname] = (path, lines)
        yield fname, entries, files


def validate(root):
    """Same structural rules as a directory submission."""
    problems = []
    total = 0
    import os
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".m3u"):
            continue
        with open(os.path.join(root, fname), encoding="utf-8",
                  errors="replace") as f:
            lines = f.read().splitlines()
        if not lines or lines[0].strip() != "#EXTM3U":
            problems.append(f"{fname}: missing #EXTM3U header")
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF"):
                total += 1
                if i + 1 >= len(lines) or not lines[i + 1].startswith(
                        ("http://", "https://")):
                    problems.append(f"{fname}:{i + 1}: EXTINF not followed by a stream URL")
    return total, problems


def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    all_entries, files = [], {}
    for _fname, entries, fmap in parse_playlists(args.root):
        all_entries.extend(entries)
        files.update(fmap)
    print(f"checking {len(all_entries)} streams in {len(files)} playlists")

    with ThreadPoolExecutor(max_workers=24) as ex:
        first_pass = list(ex.map(lambda e: probe(e["url"]), all_entries))
    suspects = [e for e, p in zip(all_entries, first_pass) if not p["alive"]]
    print(f"first pass: {len(suspects)} suspects, confirming each once more")
    with ThreadPoolExecutor(max_workers=16) as ex:
        whys = list(ex.map(lambda e: probe_dead(e["url"]), suspects))
    dead = [(e, why) for e, why in zip(suspects, whys) if why is not None]
    print(f"confirmed dead: {len(dead)}")

    def search_one(pair):
        e, why = pair
        return e, why, find_replacement(e["name"], e["url"])

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(search_one, dead))
    replaced, unreplaced = [], []
    changed_files = set()
    for e, why, best in results:
        if best:
            path, lines = files[e["file"]]
            lines[e["line"]] = best["url"]
            changed_files.add(e["file"])
            replaced.append({"station": e["name"], "file": e["file"],
                             "old": e["url"], "new": best["url"],
                             "source": best["source"],
                             "bitrate": best["bitrate"], "codec": best["codec"]})
            print(f"replaced: {e['name']} ({e['file']}) via {best['source']} "
                  f"{best['bitrate']}k {best['codec']}")
        else:
            unreplaced.append({"station": e["name"], "file": e["file"],
                               "url": e["url"], "why": why})
            print(f"no replacement: {e['name']} ({e['file']}) — {why}")

    for fname in changed_files:
        path, lines = files[fname]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    total, problems = validate(args.root)
    report = {"checked": len(all_entries), "dead": len(dead),
              "replaced": replaced, "unreplaced": unreplaced,
              "validation_problems": problems}
    text = json.dumps(report, indent=1)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"summary: checked={len(all_entries)} dead={len(dead)} "
          f"replaced={len(replaced)} unreplaced={len(unreplaced)} "
          f"entries_after={total} problems={len(problems)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
