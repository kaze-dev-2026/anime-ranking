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
    """作品ページは JSON-LD が冒頭にあるため、必要な部分だけ読んで打ち切る。"""
    req = urllib.request.Request(url, headers=UA)
    buf = b""
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        while len(buf) < 96 * 1024:
            chunk = r.read(16384)
            if not chunk:
                break
            buf += chunk
            text = buf.decode("utf-8", "replace")
            i = text.find("TVSeason")
            if i >= 0 and "</script>" in text[i:]:
                break
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


def parse_detail(text, url):
    """作品ページの JSON-LD から、題名・点数・評価レビュー件数・あらすじ・開始日を取る。"""
    work = {"url": url, "title": "", "score": None, "reviews": 0,
            "synopsis": "", "start": "", "image": ""}
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


def card_html(rank, w):
    score = f"{w['score']:.1f}"
    start = f'<span class="start">放送開始：{html.escape(w["start"])}</span>' if w["start"] else ""
    syn = f'<p class="desc">{html.escape(w["synopsis"])}</p>' if w["synopsis"] else ""
    thumb = ""
    if w.get("image"):
        thumb = (f'<div class="thumb"><img src="{html.escape(w["image"], quote=True)}" '
                 f'alt="{html.escape(w["title"], quote=True)}" loading="lazy" '
                 f'onerror="this.parentNode.style.display=\'none\'"></div>')
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
                               "image": w.get("image", "")}
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
.thumb { flex: 0 0 auto; }
.thumb img {
  display: block;
  width: 88px;
  height: 123px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #22222c;
}
.meta { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
.meta .rank { font-weight: 900; font-size: 1.05rem; color: var(--accent); margin-right: 4px; }
.meta .start { margin-left: auto; color: var(--muted); font-size: 0.8rem; }
.card h2 { margin: 10px 0 0; font-size: 1.13rem; font-weight: 700; line-height: 1.55; }
.card h2 a { color: var(--text); text-decoration: none; }
.card h2 a:hover { color: var(--accent); text-decoration: underline; }
.desc { margin: 8px 0 0; color: var(--sub); font-size: 0.92rem; }
footer { margin-top: 40px; color: var(--muted); font-size: 0.78rem; text-align: center; }
@media (max-width: 560px) {
  .wrap { padding: 0 12px 40px; }
  header { padding: 22px 0 18px; }
  .card { padding: 14px 16px 12px; border-radius: 10px; gap: 12px; }
  .thumb img { width: 68px; height: 95px; }
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
<script>
var DATA = EMBED;
var CUR = "CURKEY";
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
  });
}
function card(w) {
  var start = w.start ? '<span class="start">放送開始：' + esc(w.start) + '</span>' : '';
  var syn = w.synopsis ? '<p class="desc">' + esc(w.synopsis) + '</p>' : '';
  var thumb = w.image ? '<div class="thumb"><img src="' + esc(w.image) + '" alt="' + esc(w.title)
    + '" loading="lazy" onerror="this.parentNode.style.display=\'none\'"></div>' : '';
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
}
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
  show(CUR);
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


def main():
    if "--backfill-images" in sys.argv:
        return backfill_images()
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
