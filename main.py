#!/usr/bin/env python3
"""アニメランキング生成プログラム（トークンなし版）

Filmarks（filmarks.com のアニメ機能）の公開ページから、季節ごとの作品の
利用者評価（5点満点の平均点と評価レビュー件数）と日本語あらすじを取得し、
点数の高い順に並べた静的ページ index.html を生成する。AIは使わない。
標準ライブラリのみで動く。

使い方: python3 main.py
- 現在の季節と前の季節は毎回取り直す。
- 2015年冬以降で、まだ持っていない季節は初回にまとめて取得する（途中保存するので中断しても再開できる）。
- 取得に失敗した季節は、前回のデータをそのまま残す。
"""
import html
import json
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept-Language": "ja,en;q=0.9"}
TIMEOUT = 30
SLEEP_SEC = 0.5          # 取得の間隔（通常の利用の範囲に収める）
TOP_N = 30
MAX_USER_REVIEWS = 3     # カードに埋め込む利用者レビューの最大件数（新しい順）
REVIEW_TEXT_MAX = 300    # レビュー本文の最大文字数（超えたら切って続きを案内する）
REVIEW_SCAN = 12         # 作品ページのレビュー欄で走査する最大件数
FLOOR_REVIEWS = 10       # 評価レビュー件数の下限（根拠は sources_report.md と実行ログ）
START_YEAR = 2015
SITE = "https://filmarks.com"

SEASON_ORDER = ["winter", "spring", "summer", "autumn"]
SEASON_MONTH = {"winter": 1, "spring": 4, "summer": 7, "autumn": 10}
SEASON_JP = {"winter": "冬", "spring": "春", "summer": "夏", "autumn": "秋"}


def log(lines, msg):
    lines.append(msg)
    print(msg, flush=True)


def current_season(now):
    m = now.month
    if m >= 10:
        return now.year, "autumn"
    if m >= 7:
        return now.year, "summer"
    if m >= 4:
        return now.year, "spring"
    return now.year, "winter"


def season_key(year, season):
    return f"{year}-{season}"


def season_label(year, season):
    return f"{year}年{SEASON_JP[season]}"


def all_seasons(upto_year, upto_season):
    out = []
    for y in range(START_YEAR, upto_year + 1):
        for s in SEASON_ORDER:
            if y == upto_year and SEASON_ORDER.index(s) > SEASON_ORDER.index(upto_season):
                continue
            out.append((y, s))
    return out


def prev_season(year, season):
    i = SEASON_ORDER.index(season)
    if i == 0:
        return year - 1, "autumn"
    return year, SEASON_ORDER[i - 1]


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def fetch_detail(url):
    """作品ページを読む。JSON-LD（冒頭）とレビュー欄（後半）の両方が要るため
    ページ全体を読む。リクエスト回数は従来と同じ1回で、読む量だけ増える。"""
    req = urllib.request.Request(url, headers=UA)
    buf = b""
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        while len(buf) < 1024 * 1024:
            chunk = r.read(16384)
            if not chunk:
                break
            buf += chunk
    return buf.decode("utf-8", "replace")


def _parse_list_images(text):
    """一覧ページの各作品ブロックから、作品パス→ポスター画像URLの対応を取る。

    各作品のブロック先頭にある onClickDetailLink の引数（作品パス）と、
    そのブロック内の最初のポスター画像（cloudfront の variants）を対応付ける。
    ポスターが無い作品（プレースホルダ表示）は variants のURLが無いため、
    対応表に入らない（＝画像なしとして扱う）。
    """
    imgmap = {}
    pat = re.compile(
        r"onClickDetailLink\(\$event, (?:&#39;|')(/animes/\d+/\d+)(?:&#39;|')\)")
    clicks = [(m.start(), m.group(1)) for m in pat.finditer(text)]
    for i, (pos, path) in enumerate(clicks):
        end = clicks[i + 1][0] if i + 1 < len(clicks) else len(text)
        m = re.search(
            r'<img[^>]+src="(https://[^"]+\.cloudfront\.net/variants/[^"]+)"',
            text[pos:end])
        if m and path not in imgmap:
            imgmap[path] = m.group(1)
    return imgmap


def fetch_season_work_urls(year, season):
    """季節の一覧ページ（ページ送り含む）から作品URL一覧と画像対応表を集める。

    戻り値: (作品ページURLのリスト, {作品URL: ポスター画像URL})
    画像は一覧ページ側から取る（詳細ページの JSON-LD にも image があるが、
    一覧で取れば追加の取得なしに済む）。
    """
    month = SEASON_MONTH[season]
    base_url = f"{SITE}/list-anime/release_year/{year}/{month}"
    urls = []
    seen = set()
    imgmap = {}
    for page in range(1, 13):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            text = fetch(url)
        except urllib.error.HTTPError as e:
            if e.code == 404 and page > 1:
                break  # 最終ページを過ぎたら404になるため、そこで終わりとする
            raise
        for path, img in _parse_list_images(text).items():
            imgmap.setdefault(SITE + path, img)
        found = re.findall(r'href="(/animes/\d+/\d+)"', text)
        new = 0
        for f in found:
            if f not in seen:
                seen.add(f)
                urls.append(SITE + f)
                new += 1
        if new == 0:
            break
        time.sleep(SLEEP_SEC)
    return urls, imgmap


def _find_tvseason(obj):
    if isinstance(obj, dict):
        if obj.get("@type") == "TVSeason":
            return obj
        for v in obj.values():
            r = _find_tvseason(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_tvseason(v)
            if r:
                return r
    return None


def _clean_review_text(raw):
    """レビュー本文のHTML断片をプレーンテキストにする。"""
    raw = re.sub(r'<a[^>]*>.*?続きを読む</a>', '', raw, flags=re.S)
    raw = re.sub(r'<br\s*/?>', '\n', raw)
    raw = re.sub(r'</p>\s*<p[^>]*>', '\n', raw)
    raw = re.sub(r'<[^>]+>', '', raw)
    raw = html.unescape(raw)
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in raw.split('\n')]
    return '\n'.join(ln for ln in lines if ln).strip()


def parse_reviews(text):
    """作品ページの「投稿された感想・評価」欄から利用者レビューを取る。

    戻り値: (レビューのリスト, 走査できたレビュー件数)
    - 新しい順に最大 MAX_USER_REVIEWS 件。本文は REVIEW_TEXT_MAX 文字で切る。
    - ネタバレ明記のレビューは本文を保存しない（件数の数えにも含めない形にせず、
      「走査できた件数」には含める。本文が漏れない事を優先する）。
    - 点数だけのレビュー（本文なし）は対象外。
    """
    idx = text.find('に投稿された感想・評価')
    if idx < 0:
        return [], 0
    chunks = text[idx:].split('<div class="p-mark">')
    seen = 0
    out = []
    for ch in chunks[1:REVIEW_SCAN + 1]:
        mbody = re.search(r'<div class="p-mark-review">(.*?)</div>', ch, re.S)
        if not mbody:
            continue
        seen += 1
        if ('このレビューはネタバレを含みます' in ch
                or 'p-mark__netabare' in ch):
            continue  # ネタバレは本文を保存しない
        body = _clean_review_text(mbody.group(1))
        if not body:
            continue
        mscore = re.search(r'c2-rating-s__text">([0-9]+(?:\.[0-9]+)?)<', ch)
        mdate = re.search(r'<time class="c-media__date" datetime="([^"]+)"', ch)
        murl = re.search(r'href="(/animes/\d+/\d+/reviews/\d+)"', ch)
        truncated = '続きを読む' in mbody.group(1)
        if len(body) > REVIEW_TEXT_MAX:
            body = body[:REVIEW_TEXT_MAX].rstrip() + '…'
            truncated = True
        out.append({
            "score": float(mscore.group(1)) if mscore else None,
            "date": mdate.group(1)[:10] if mdate else "",
            "text": body,
            "url": (SITE + murl.group(1)) if murl else "",
            "truncated": truncated,
        })
        if len(out) >= MAX_USER_REVIEWS:
            break
    return out, seen


def parse_detail(text, url):
    """作品ページの JSON-LD から、題名・点数・評価レビュー件数・あらすじ・開始日を取る。
    同じページのレビュー欄から利用者レビューも取る（追加リクエストなし）。"""
    work = {"url": url, "title": "", "score": None, "reviews": 0,
            "synopsis": "", "start": "", "image": "",
            "user_reviews": [], "reviews_seen": 0}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>',
                         text, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        tv = _find_tvseason(data)
        if not tv:
            continue
        work["title"] = (tv.get("title") or "").strip()
        img = tv.get("image")
        if isinstance(img, dict):
            work["image"] = (img.get("url") or "").strip()
        elif isinstance(img, str):
            work["image"] = img.strip()
        work["synopsis"] = re.sub(r"\s+", " ", (tv.get("outline") or "")).strip()
        sd = tv.get("startDate") or tv.get("releaseDate") or ""
        work["start"] = sd[:10]
        agg = tv.get("aggregateRating") or {}
        try:
            work["score"] = float(agg.get("ratingValue"))
        except (TypeError, ValueError):
            work["score"] = None
        try:
            work["reviews"] = int(agg.get("reviewCount"))
        except (TypeError, ValueError):
            work["reviews"] = 0
        break
    if not work["title"]:
        m = re.search(r"<title>(.*?) - ", text, re.S)
        if m:
            work["title"] = html.unescape(m.group(1)).strip()
    revs, seen = parse_reviews(text)
    work["user_reviews"] = revs
    work["reviews_seen"] = seen
    return work


def _safe_one(fn, u, lines):
    try:
        return fn(u)
    except Exception as e:
        log(lines, f"  作品取得失敗: {u}: {type(e).__name__}: {e}")
        return None


def fetch_season(year, season, lines):
    """1季節分を取得する。失敗したら例外を投げる（呼び出し側で旧データを残す）。"""
    urls, imgmap = fetch_season_work_urls(year, season)
    log(lines, f"{season_label(year, season)}: 一覧から{len(urls)}作品")
    works = []
    fails = 0

    def _one(u):
        time.sleep(0.15)
        return parse_detail(fetch_detail(u), u)

    # ブラウザが1ページ開く時の同時接続数と同程度（4本）に抑えて並行取得する
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda u: (u, _safe_one(_one, u, lines)), urls))
    for u, w in results:
        if w is not None and w["title"]:
            if not w.get("image"):
                w["image"] = imgmap.get(u, "")
            works.append(w)
        else:
            fails += 1
    if not works:
        raise RuntimeError("作品が1件も取れなかった")
    if fails:
        log(lines, f"  {season_label(year, season)}: 失敗{fails}件（残りで続行）")
    return {"year": year, "season": season, "label": season_label(year, season),
            "total": len(works), "works": works,
            "fetched": datetime.now(JST).date().isoformat()}


def rank_works(works):
    """下限以上の作品を、点数の高い順（同点は評価レビュー件数の多い順）に並べる。"""
    cand = [w for w in works
            if w["score"] is not None and w["reviews"] >= FLOOR_REVIEWS]
    cand.sort(key=lambda w: (-w["score"], -w["reviews"], w["title"]))
    return cand[:TOP_N]


def save_data(data):
    tmp = (BASE / "data.json").with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(BASE / "data.json")


def reviews_html(w):
    """「レビューを見る」ボタンとレビュー欄のHTML。レビュー欄は最初は hidden で、
    読み込み直後に高さが変わらないようにする（開閉はボタンの操作だけ）。"""
    if not w.get("reviews_seen"):
        return ""  # レビューが取れていない作品にはボタンを出さない
    items = ""
    for r in w.get("user_reviews") or []:
        sc = f'{r["score"]:.1f}' if r.get("score") is not None else "―"
        dt = f'<span class="rev-date">{html.escape(r.get("date", ""))}</span>' if r.get("date") else ""
        more = ""
        if r.get("truncated") and r.get("url"):
            more = (f'<div><a class="rev-more" href="{html.escape(r["url"], quote=True)}">'
                    f'続きはFilmarksで読む</a></div>')
        items += (f'<div class="review-item"><div class="rev-head">'
                  f'<span class="rev-score">★ {sc}</span>{dt}</div>'
                  f'<p class="rev-text">{html.escape(r.get("text", ""))}</p>{more}</div>')
    if not items:
        items = (f'<p class="rev-none">ネタバレなしのレビューはまだありません。'
                 f'<a class="rev-more" href="{html.escape(w["url"], quote=True)}">'
                 f'Filmarksでレビューを見る</a></p>')
    return (f'<div><button type="button" class="rev-btn">レビューを見る</button>'
            f'<div class="reviews" hidden>{items}</div></div>')


def card_html(rank, w):
    score = f"{w['score']:.1f}"
    start = f'<span class="start">放送開始：{html.escape(w["start"])}</span>' if w["start"] else ""
    syn = ""
    if w["synopsis"]:
        # ボタンは場所を常に確保し、表示・非表示は visibility で切り替える
        # （高さが変わるとスクロール中にページが飛ぶため）。最初から出すか
        # どうかは文字数の目安で決め、最終判定はページ側の実寸測定で行う。
        btn_cls = "more-btn show" if len(w["synopsis"]) > 100 else "more-btn"
        syn = (f'<p class="desc clamp">{html.escape(w["synopsis"])}</p>'
               f'<button type="button" class="{btn_cls}">もっと見る</button>')
    syn += reviews_html(w)
    thumb = ""
    if w.get("image"):
        # 失敗時は画像だけを透明にし、箱（.thumb）は残す。箱ごと消すと
        # 本文の幅が変わって高さがずれ、スクロール位置が飛ぶため。
        thumb = (f'<div class="thumb"><img src="{html.escape(w["image"], quote=True)}" '
                 f'alt="{html.escape(w["title"], quote=True)}" width="300" height="420" '
                 f'loading="lazy" decoding="async" '
                 f'onerror="this.style.visibility=\'hidden\'"></div>')
    return (
        f'<article class="card">{thumb}<div class="card-body">'
        f'<div class="meta"><span class="rank">{rank}位</span>'
        f'<span class="badge badge-score">スコア {score}</span>'
        f'<span class="badge badge-rev">評価レビュー {w["reviews"]}件</span>'
        f'{start}</div>'
        f'<h2><a href="{html.escape(w["url"], quote=True)}">{html.escape(w["title"])}</a></h2>'
        f'{syn}</div></article>')


def write_html(data, cur_key):
    seasons = data["seasons"]
    keys = [season_key(y, s) for (y, s) in
            all_seasons(data["cur_year"], data["cur_season"])]
    keys = [k for k in keys if k in seasons]
    # セレクタ用データ（順位つき作品だけを埋め込む）
    embed = {}
    for k in keys:
        s = seasons[k]
        ranked = rank_works(s["works"])
        embed[k] = {"label": s["label"], "total": s["total"],
                    "fetched": s["fetched"],
                    "works": [{"rank": i + 1, "title": w["title"],
                               "score": w["score"], "reviews": w["reviews"],
                               "synopsis": w["synopsis"], "url": w["url"],
                               "start": w["start"],
                               "image": w.get("image", ""),
                               "user_reviews": w.get("user_reviews", []),
                               "reviews_seen": w.get("reviews_seen")}
                              for i, w in enumerate(ranked)]}
    years = sorted({seasons[k]["year"] for k in keys}, reverse=True)
    year_opts = "".join(f'<option value="{y}">{y}年</option>' for y in years)
    season_opts = "".join(
        f'<option value="{s}">{SEASON_JP[s]}</option>' for s in SEASON_ORDER)
    cur = embed.get(cur_key)
    initial = "\n".join(card_html(w["rank"], w) for w in cur["works"]) if cur else ""
    embed_json = json.dumps(embed, ensure_ascii=False).replace("</", "<\\/")
    updated = data["updated"]
    page = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>今季アニメ面白さランキング</title>
<style>
:root {
  --bg: #0a0a0d;
  --surface: #16161d;
  --border: #2b2b36;
  --text: #f4f3ef;
  --sub: #c9c8cf;
  --muted: #8f8e99;
  --accent: #7aa2ff;
  --score: #ffb02e;
  --rev: #2dd4bf;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--text);
  background:
    radial-gradient(900px 420px at 50% -120px, rgba(122, 162, 255, 0.15), rgba(122, 162, 255, 0) 70%),
    linear-gradient(180deg, #101016 0, var(--bg) 340px);
  background-color: var(--bg);
  font-family: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Noto Sans CJK JP", "Yu Gothic UI", "Yu Gothic", sans-serif;
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 840px; margin: 0 auto; padding: 0 18px 56px; }
header { padding: 34px 0 26px; }
.overline {
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.42em;
  margin: 0 0 10px;
}
h1 {
  margin: 0;
  font-size: clamp(2.1rem, 8vw, 3.4rem);
  font-weight: 900;
  line-height: 1.15;
  letter-spacing: 0.03em;
}
h1 .accent { color: var(--accent); }
.tagline { margin: 14px 0 0; color: var(--sub); font-size: 0.95rem; }
.rule {
  height: 4px;
  margin-top: 26px;
  background: linear-gradient(90deg, var(--accent), rgba(122, 162, 255, 0.25) 55%, rgba(122, 162, 255, 0));
}
.headmeta {
  display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center;
  margin-top: 16px; color: var(--muted); font-size: 0.85rem;
}
.picker {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  margin-top: 18px;
}
.picker label { color: var(--sub); font-size: 0.9rem; }
.picker select {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 1rem;
}
.season-title { margin: 26px 0 4px; font-size: 1.35rem; font-weight: 800; }
.season-meta { color: var(--muted); font-size: 0.85rem; margin: 0 0 6px; }
.badge {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  border: 1px solid;
  white-space: nowrap;
}
.badge-score { color: var(--score); border-color: var(--score); background: rgba(255, 176, 46, 0.10); }
.badge-rev { color: var(--rev); border-color: var(--rev); background: rgba(45, 212, 191, 0.10); }
.card {
  display: flex;
  gap: 16px;
  background: linear-gradient(180deg, #181820, #121218);
  border: 1px solid var(--border);
  border-left: 5px solid var(--accent);
  border-radius: 12px;
  padding: 18px 22px 16px;
  margin: 16px 0;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
}
.card-body { flex: 1 1 auto; min-width: 0; }
.thumb {
  flex: 0 0 auto;
  width: 88px;
  height: 123px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #22222c;
  overflow: hidden;
}
.thumb img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  aspect-ratio: 300 / 420;
}
.meta { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
.meta .rank { font-weight: 900; font-size: 1.05rem; color: var(--accent); margin-right: 4px; }
.meta .start { margin-left: auto; color: var(--muted); font-size: 0.8rem; }
.card h2 { margin: 10px 0 0; font-size: 1.13rem; font-weight: 700; line-height: 1.55; }
.card h2 a { color: var(--text); text-decoration: none; }
.card h2 a:hover { color: var(--accent); text-decoration: underline; }
.desc { margin: 8px 0 0; color: var(--sub); font-size: 0.92rem; }
.desc.clamp {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  overflow: hidden;
}
.more-btn {
  margin-top: 6px;
  padding: 2px 14px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--accent);
  font-size: 0.8rem;
  font-weight: 700;
  cursor: pointer;
  visibility: hidden;
}
.more-btn.show { visibility: visible; }
.more-btn:hover { border-color: var(--accent); }
.rev-btn {
  margin-top: 8px;
  padding: 2px 14px;
  background: none;
  border: 1px solid var(--rev);
  border-radius: 999px;
  color: var(--rev);
  font-size: 0.8rem;
  font-weight: 700;
  cursor: pointer;
}
.rev-btn:hover { background: rgba(45, 212, 191, 0.10); }
.reviews { margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 10px; }
.reviews[hidden] { display: none; }
.review-item { margin: 0 0 12px; }
.review-item:last-child { margin-bottom: 2px; }
.rev-head { display: flex; gap: 10px; align-items: center; font-size: 0.8rem; }
.rev-score { color: var(--score); font-weight: 700; }
.rev-date { color: var(--muted); }
.rev-text { margin: 2px 0 0; color: var(--sub); font-size: 0.9rem; white-space: pre-line; }
.rev-more { color: var(--accent); font-size: 0.82rem; }
.rev-none { margin: 0; color: var(--sub); font-size: 0.9rem; }
.rev-none .rev-more { margin-left: 8px; }
footer { margin-top: 40px; color: var(--muted); font-size: 0.78rem; text-align: center; }
.thumb img { cursor: zoom-in; }
#lightbox {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(0, 0, 0, 0.88);
  display: flex;
  align-items: center;
  justify-content: center;
}
#lightbox[hidden] { display: none; }
#lightbox img {
  max-width: 92vw;
  max-height: 88vh;
  object-fit: contain;
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6);
  cursor: zoom-out;
}
#lightboxClose {
  position: absolute;
  top: 14px;
  right: 14px;
  width: 44px;
  height: 44px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: rgba(22, 22, 29, 0.92);
  color: var(--text);
  font-size: 1.25rem;
  line-height: 1;
  cursor: pointer;
}
@media (max-width: 560px) {
  .wrap { padding: 0 12px 40px; }
  header { padding: 22px 0 18px; }
  .card { padding: 14px 16px 12px; border-radius: 10px; gap: 12px; }
  .thumb { width: 68px; height: 95px; }
  .card h2 { font-size: 1.03rem; }
  .meta .start { margin-left: 0; }
}
</style>
</head>
<body>
<div class="wrap">
<header>
<p class="overline">ANIME RANKING</p>
<h1>今季アニメ<span class="accent">面白さ</span>ランキング</h1>
<p class="tagline">Filmarksの利用者評価（5点満点）が高い順に並べています。評価レビューがFLOOR件に満たない作品は、順位に入れていません。過去の季節も選べます。</p>
<div class="rule"></div>
<div class="headmeta"><span>最終更新：UPDATED</span><span>データ出典：Filmarks</span></div>
<div class="picker">
<label>年 <select id="yearSel">YEAROPTS</select></label>
<label>季節 <select id="seasonSel">SEASONOPTS</select></label>
</div>
</header>
<main>
<h2 class="season-title" id="seasonTitle">CURTITLE</h2>
<p class="season-meta" id="seasonMeta">CURMETA</p>
<div id="list">
INITIAL
</div>
</main>
<footer>このページはプログラムが自動で作成しています。点数とあらすじはFilmarksの公開データに基づきます。</footer>
</div>
<div id="lightbox" hidden>
<img id="lightboxImg" alt="">
<button type="button" id="lightboxClose" aria-label="閉じる">×</button>
</div>
<script>
var DATA = EMBED;
var CUR = "CURKEY";
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
  });
}
function reviewsHtml(w) {
  if (!w.reviews_seen) return '';
  var items = '';
  var rs = w.user_reviews || [];
  for (var i = 0; i < rs.length; i++) {
    var r = rs[i];
    var sc = r.score == null ? '―' : Number(r.score).toFixed(1);
    var dt = r.date ? '<span class="rev-date">' + esc(r.date) + '</span>' : '';
    var more = (r.truncated && r.url) ? '<div><a class="rev-more" href="' + esc(r.url) + '">続きはFilmarksで読む</a></div>' : '';
    items += '<div class="review-item"><div class="rev-head"><span class="rev-score">★ ' + sc + '</span>' + dt
      + '</div><p class="rev-text">' + esc(r.text) + '</p>' + more + '</div>';
  }
  if (!items) {
    items = '<p class="rev-none">ネタバレなしのレビューはまだありません。'
      + '<a class="rev-more" href="' + esc(w.url) + '">Filmarksでレビューを見る</a></p>';
  }
  return '<div><button type="button" class="rev-btn">レビューを見る</button>'
    + '<div class="reviews" hidden>' + items + '</div></div>';
}
function card(w) {
  var start = w.start ? '<span class="start">放送開始：' + esc(w.start) + '</span>' : '';
  var btnCls = w.synopsis && w.synopsis.length > 100 ? 'more-btn show' : 'more-btn';
  var syn = w.synopsis ? '<p class="desc clamp">' + esc(w.synopsis) + '</p><button type="button" class="' + btnCls + '">もっと見る</button>' : '';
  syn += reviewsHtml(w);
  var thumb = w.image ? '<div class="thumb"><img src="' + esc(w.image) + '" alt="' + esc(w.title)
    + '" width="300" height="420" loading="lazy" decoding="async" onerror="this.style.visibility=\\'hidden\\'"></div>' : '';
  return '<article class="card">' + thumb + '<div class="card-body"><div class="meta"><span class="rank">' + w.rank + '位</span>'
    + '<span class="badge badge-score">スコア ' + Number(w.score).toFixed(1) + '</span>'
    + '<span class="badge badge-rev">評価レビュー ' + w.reviews + '件</span>' + start + '</div>'
    + '<h2><a href="' + esc(w.url) + '">' + esc(w.title) + '</a></h2>' + syn + '</div></article>';
}
function show(key) {
  var s = DATA[key];
  var title = document.getElementById('seasonTitle');
  var meta = document.getElementById('seasonMeta');
  var list = document.getElementById('list');
  if (!s) {
    title.textContent = 'この季節のデータはまだありません';
    meta.textContent = '';
    list.innerHTML = '';
    return;
  }
  title.textContent = s.label + 'アニメ 面白さランキング';
  meta.textContent = '全' + s.total + '作品から、評価レビューFLOOR件以上の上位' + s.works.length + '作品（データ取得：' + s.fetched + '）';
  list.innerHTML = s.works.map(card).join('');
  initClamps();
}
function initClamps() {
  // 高さは測らない。測るために畳みを外すと、画面の並びが組み替わって
  // スクロール位置がずれる原因になるため、文字数だけで判定する。
  // 判定の基準は card() 側の表示判定（100文字超）とそろえてある。
  var nodes = document.querySelectorAll('#list .desc');
  for (var i = 0; i < nodes.length; i++) {
    var p = nodes[i];
    var btn = p.parentNode.querySelector('.more-btn');
    if (!btn) continue;
    if (p.textContent.length > 100) btn.classList.add('show');
    else btn.classList.remove('show');
  }
}
document.getElementById('list').addEventListener('click', function(e) {
  var btn = (e.target && e.target.closest) ? e.target.closest('.more-btn') : null;
  if (!btn) return;
  var p = btn.parentNode.querySelector('.desc');
  if (!p) return;
  if (p.classList.contains('clamp')) {
    p.classList.remove('clamp');
    btn.textContent = '閉じる';
  } else {
    p.classList.add('clamp');
    btn.textContent = 'もっと見る';
  }
});
document.getElementById('list').addEventListener('click', function(e) {
  var btn = (e.target && e.target.closest) ? e.target.closest('.rev-btn') : null;
  if (!btn) return;
  var box = btn.parentNode.querySelector('.reviews');
  if (!box) return;
  if (box.hidden) {
    box.hidden = false;
    btn.textContent = '閉じる';
  } else {
    box.hidden = true;
    btn.textContent = 'レビューを見る';
  }
});
// ポスター拡大表示（ページ内の仕組みのみ）。季節切り替えで作り直された
// カードでも効くよう、サムネイルのクリックは #list への委任で受ける。
// 閉じる操作は、拡大画面のどこを押しても（画像・背景・閉じるボタン）
// 拡大画面側のクリックで受ける。
var lightbox = document.getElementById('lightbox');
var lightboxImg = document.getElementById('lightboxImg');
var lbScrollY = 0;
function openLightbox(img) {
  if (img.style.visibility === 'hidden') return; // 読み込み失敗の画像は開かない
  lbScrollY = window.scrollY;
  lightboxImg.src = img.src;
  lightboxImg.alt = img.alt || '';
  lightbox.hidden = false;
  // 背面のスクロール止め。overflow だけでは止まらない環境があるため、
  // body を現在の表示位置で固定する（閉じる時に位置を戻す）
  document.documentElement.style.overflow = 'hidden';
  var b = document.body;
  b.style.overflow = 'hidden';
  b.style.position = 'fixed';
  b.style.top = (-lbScrollY) + 'px';
  b.style.left = '0';
  b.style.right = '0';
}
function closeLightbox() {
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  lightboxImg.removeAttribute('src');
  var b = document.body;
  document.documentElement.style.overflow = '';
  b.style.overflow = '';
  b.style.position = '';
  b.style.top = '';
  b.style.left = '';
  b.style.right = '';
  window.scrollTo(0, lbScrollY);
}
document.getElementById('list').addEventListener('click', function(e) {
  var img = (e.target && e.target.closest) ? e.target.closest('.thumb img') : null;
  if (img) openLightbox(img);
});
lightbox.addEventListener('click', function() { closeLightbox(); });
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeLightbox();
});
function onPick() {
  var y = document.getElementById('yearSel').value;
  var s = document.getElementById('seasonSel').value;
  show(y + '-' + s);
}
document.getElementById('yearSel').addEventListener('change', onPick);
document.getElementById('seasonSel').addEventListener('change', onPick);
(function() {
  var parts = CUR.split('-');
  document.getElementById('yearSel').value = parts[0];
  document.getElementById('seasonSel').value = parts[1];
  // 初期表示分は生成済みHTMLがそのまま入っているので、作り直さず
  // ボタンの表示判定だけ行う（作り直すと表示が一瞬組み替わり、
  // スクロール位置がずれる原因になるため）
  if (document.getElementById('list').children.length) initClamps();
  else show(CUR);
})();
</script>
</body>
</html>
"""
    cur_label = cur["label"] + "アニメ 面白さランキング" if cur else ""
    cur_meta = (f'全{cur["total"]}作品から、評価レビュー{FLOOR_REVIEWS}件以上の'
                f'上位{len(cur["works"])}作品（データ取得：{cur["fetched"]}）') if cur else ""
    page = (page.replace("FLOOR", str(FLOOR_REVIEWS))
            .replace("UPDATED", updated)
            .replace("YEAROPTS", year_opts)
            .replace("SEASONOPTS", season_opts)
            .replace("CURTITLE", html.escape(cur_label))
            .replace("CURMETA", html.escape(cur_meta))
            .replace("CURKEY", cur_key)
            .replace("INITIAL", initial)
            .replace("EMBED", embed_json))
    (BASE / "index.html").write_text(page, encoding="utf-8")


def backfill_images():
    """既存データの各作品に、一覧ページからポスター画像URLだけを補完する。

    点数・件数・あらすじ等の既存データには一切触らない。季節ごとに保存する
    ので中断しても再開できる。取得に失敗した季節はそのまま残す。
    使い方: python3 main.py --backfill-images
    """
    started = datetime.now(JST)
    lines = [f"画像補完 開始: {started.isoformat()}"]
    data_path = BASE / "data.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    total_filled = 0
    total_works = 0
    for k in sorted(data["seasons"].keys()):
        entry = data["seasons"][k]
        y, s = entry["year"], entry["season"]
        try:
            _urls, imgmap = fetch_season_work_urls(y, s)
        except Exception as e:
            log(lines, f"{entry['label']}: 一覧取得失敗のため補完を省略: "
                       f"{type(e).__name__}: {e}")
            continue
        filled = 0
        for w in entry["works"]:
            total_works += 1
            if not w.get("image") and w["url"] in imgmap:
                w["image"] = imgmap[w["url"]]
                filled += 1
        total_filled += filled
        have = sum(1 for w in entry["works"] if w.get("image"))
        log(lines, f"{entry['label']}: 画像あり {have}/{len(entry['works'])}作品"
                   f"（今回 {filled}件 補完）")
        save_data(data)
    cur_key = season_key(data["cur_year"], data["cur_season"])
    write_html(data, cur_key)
    log(lines, f"画像補完 合計: {total_filled}件 / 全{total_works}作品")
    log(lines, f"画像補完 終了: {datetime.now(JST).isoformat()}")
    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"backfill-{started.strftime('%Y%m%d-%H%M%S')}.log"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ログ: {log_file}")
    return 0


def backfill_reviews():
    """表示対象（各季節の上位作品）に、作品ページから利用者レビューだけを補完する。

    点数・件数・あらすじ・画像等の既存データには一切触らない（レビュー欄の
    取得結果 user_reviews / reviews_seen だけを書き足す）。季節ごとに保存する
    ので中断しても再開できる（reviews_seen が入っている作品は取り直さない）。
    使い方: python3 main.py --backfill-reviews
    """
    started = datetime.now(JST)
    lines = [f"レビュー補完 開始: {started.isoformat()}"]
    data_path = BASE / "data.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    total_target = 0
    total_with = 0
    total_fetched = 0
    for k in sorted(data["seasons"].keys()):
        entry = data["seasons"][k]
        ranked = rank_works(entry["works"])
        targets = [w for w in ranked if "reviews_seen" not in w]
        total_target += len(ranked)
        if targets:
            def _one(u):
                time.sleep(0.15)
                text = fetch_detail(u)
                return parse_reviews(text)

            with ThreadPoolExecutor(max_workers=4) as ex:
                results = list(ex.map(
                    lambda u: (u, _safe_one(_one, u, lines)),
                    [w["url"] for w in targets]))
            by_url = dict(results)
            for w in targets:
                r = by_url.get(w["url"])
                if r is None:
                    continue  # 取得失敗分は次回再実行で取り直す
                revs, seen = r
                w["user_reviews"] = revs
                w["reviews_seen"] = seen
                total_fetched += 1
        have = sum(1 for w in ranked if w.get("user_reviews"))
        total_with += have
        log(lines, f"{entry['label']}: レビューあり {have}/{len(ranked)}作品"
                   f"（今回 {len(targets)}作品を取得）")
        save_data(data)
    cur_key = season_key(data["cur_year"], data["cur_season"])
    write_html(data, cur_key)
    log(lines, f"レビュー補完 合計: 表示対象{total_target}作品 / "
               f"レビューあり{total_with}作品 / 今回取得{total_fetched}作品")
    log(lines, f"レビュー補完 終了: {datetime.now(JST).isoformat()}")
    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"backfill-reviews-{started.strftime('%Y%m%d-%H%M%S')}.log"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ログ: {log_file}")
    return 0


def main():
    if "--backfill-images" in sys.argv:
        return backfill_images()
    if "--backfill-reviews" in sys.argv:
        return backfill_reviews()
    started = datetime.now(JST)
    lines = [f"実行開始: {started.isoformat()}"]
    cur_year, cur_season = current_season(started)
    cur_key = season_key(cur_year, cur_season)
    py, ps = prev_season(cur_year, cur_season)
    refresh = {cur_key, season_key(py, ps)}
    log(lines, f"現在の季節: {season_label(cur_year, cur_season)} / 前の季節: {season_label(py, ps)}")

    data_path = BASE / "data.json"
    data = {"floor": FLOOR_REVIEWS, "seasons": {}}
    if data_path.exists():
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception as e:
            log(lines, f"警告: 既存 data.json が読めないため空から開始する: {e}")
            data = {"floor": FLOOR_REVIEWS, "seasons": {}}
    data["floor"] = FLOOR_REVIEWS

    targets = all_seasons(cur_year, cur_season)
    failures = []
    for (y, s) in targets:
        k = season_key(y, s)
        if k in data["seasons"] and k not in refresh:
            continue
        try:
            entry = fetch_season(y, s, lines)
            data["seasons"][k] = entry
            ranked = rank_works(entry["works"])
            scored = sum(1 for w in entry["works"] if w["score"] is not None)
            log(lines, f"{entry['label']}: 全{entry['total']}作品 / 点数あり{scored}件 / "
                      f"下限以上{sum(1 for w in entry['works'] if w['score'] is not None and w['reviews'] >= FLOOR_REVIEWS)}件 / "
                      f"表示{len(ranked)}件")
        except Exception as e:
            failures.append(k)
            log(lines, f"取得失敗: {season_label(y, s)}: {type(e).__name__}: {e}"
                       f"（既存データがあればそのまま残す）")
        data["cur_year"], data["cur_season"] = cur_year, cur_season
        data["updated"] = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
        save_data(data)

    write_html(data, cur_key)
    got = len(data["seasons"])
    log(lines, f"合計: 対象{len(targets)}季節 / データあり{got}季節 / 失敗{failures if failures else 'なし'}")
    log(lines, f"実行終了: {datetime.now(JST).isoformat()}")

    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"run-{started.strftime('%Y%m%d-%H%M%S')}.log"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ログ: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
