# -*- coding: utf-8 -*-
"""
毎日チェックする世界経済・政治ニュースのダイジェストを生成するスクリプト。
config.json に列挙した公的機関等のRSSフィードを取得し、カテゴリ別・重要度別に
整理した静的HTMLレポート（画像・要約・会計監査への影響コメント付き）を
reports/ に出力する。

使い方:
    py fetch_news.py
(run.bat をダブルクリックしても実行できます)

カテゴリ・取得元・キーワードフィルタは config.json を編集することで
自由に追加・変更できる（コード変更は不要）。
"""

import html
import json
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
REPORTS_DIR = BASE_DIR / "reports"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
FETCH_TIMEOUT_SEC = 15
ARTICLE_FETCH_TIMEOUT_SEC = 8
ARTICLE_MAX_BYTES = 80_000
META_FETCH_WORKERS = 8
SUMMARY_MAX_LEN = 220
DEFAULT_ICON = "📰"

# ---------------------------------------------------------------------------
# 重要度（社会的インパクト）の簡易判定ルール
# ---------------------------------------------------------------------------
IMPORTANCE_HIGH_KEYWORDS = [
    "決定", "合意", "最高値", "最安値", "利上げ", "利下げ", "緊急", "速報",
    "行政処分", "リコール", "規制", "改正", "施行", "破綻", "危機", "制裁",
    "条約", "首脳", "サミット", "戦闘", "攻撃", "死亡", "倒産", "買収",
    "上場", "暴落", "急落", "急騰", "金融政策決定会合", "戦争",
]
IMPORTANCE_MID_KEYWORDS = [
    "見通し", "方針", "検討", "会議", "声明", "発売", "公表", "調査", "報告",
    "協力", "発表", "会談",
]


def score_importance(title, description):
    text = (title or "") + (description or "")
    high = sum(1 for kw in IMPORTANCE_HIGH_KEYWORDS if kw in text)
    mid = sum(1 for kw in IMPORTANCE_MID_KEYWORDS if kw in text)
    if high >= 2:
        return "高"
    if high >= 1:
        return "高" if mid >= 1 else "中"
    if mid >= 1:
        return "中"
    return "低"


IMPORTANCE_ORDER = {"高": 0, "中": 1, "低": 2}
IMPORTANCE_COLOR = {"高": "#c0392b", "中": "#b8860b", "低": "#6b7686"}

# ---------------------------------------------------------------------------
# 会計・監査への影響コメント（ルールベース簡易生成）
# ---------------------------------------------------------------------------
AUDIT_IMPACT_RULES = [
    (["利上げ", "利下げ", "金融政策", "政策金利", "金利"],
     "金利変動は固定利付資産・負債の公正価値評価、退職給付債務の算定に用いる割引率、減損テストの将来キャッシュ・フロー評価に影響する可能性があります。"),
    (["円安", "円高", "為替"],
     "外貨建資産・負債の換算差額、在外子会社の財務諸表換算（換算調整勘定）、ヘッジ会計の有効性評価に留意が必要です。"),
    (["株価", "日経平均", "株式市場", "時価総額", "NYダウ"],
     "保有有価証券の期末時価評価や、自己株式・株式報酬の公正価値測定への影響が考えられます。"),
    (["原油", "エネルギー価格", "資源価格", "原材料"],
     "原材料コストの変動は棚卸資産の評価（低価法）や原価計算、固定資産の減損評価に影響を与える可能性があります。"),
    (["リコール", "不具合", "品質問題"],
     "リコール対応費用の見積りに伴う製品保証引当金の計上や、偶発損失の注記開示の要否を検討する必要があります。"),
    (["半導体", "サプライチェーン", "供給"],
     "サプライチェーンの混乱は在庫評価や仕入先への前渡金の回収可能性評価に影響する可能性があります。"),
    (["行政処分", "検査", "規制", "内閣府令", "改正", "施行"],
     "監督当局の方針転換は、関連業種のコンプライアンス対応や内部統制の評価範囲の見直しに直結する可能性があります。"),
    (["IFRS", "ASBJ", "SSBJ", "会計基準", "開示", "サステナビリティ開示", "有価証券報告書", "内部統制"],
     "新基準・新たな開示要求への対応として、会計方針の選択や注記情報の拡充など実務対応の検討が必要です。"),
    (["新型", "発売", "モデル", "EV", "電気自動車", "自動運転"],
     "新型車・新製品の投入に伴う研究開発費の資産化判定や、設備投資・金型の減価償却期間の見直しに留意が必要です。"),
    (["G7", "首脳", "サミット", "制裁", "条約", "協定", "戦闘", "ウクライナ", "ロシア", "イラン", "ホルムズ"],
     "地政学リスクの高まりは、海外関係会社のゴーイングコンサーン評価や、資源・物流コストの変動を通じた財務への影響に留意が必要です。"),
    (["決算", "赤字", "黒字", "倒産", "買収", "M&A"],
     "対象企業の財務状況の変化は、のれんや投資先の減損評価、継続企業の前提（ゴーイングコンサーン）の検討に関わる可能性があります。"),
]
DEFAULT_AUDIT_COMMENT = "現時点で会計処理への直接的な影響は限定的と考えられますが、関連する取引・状況の変化には留意してください。"


def generate_audit_comment(title, description):
    text = (title or "") + (description or "")
    matched = []
    for keywords, comment in AUDIT_IMPACT_RULES:
        if any(kw in text for kw in keywords):
            matched.append(comment)
        if len(matched) >= 2:
            break
    if not matched:
        return DEFAULT_AUDIT_COMMENT
    return " ".join(matched)


# ---------------------------------------------------------------------------
# RSS取得・解析
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_bytes(url, max_bytes=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        return resp.read(max_bytes) if max_bytes else resp.read()


def parse_pubdate(raw):
    """RSSのpubDate文字列を解析し、(比較用UTC datetime か None, 表示用文字列) を返す。"""
    if not raw:
        return None, "日時不明"
    text = raw.strip().replace(" JST", " +0900")
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        dt_utc = dt.astimezone(timezone.utc)
        dt_jst = dt.astimezone(timezone(timedelta(hours=9)))
        return dt_utc.replace(tzinfo=None), dt_jst.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None, raw.strip()


def matches_keywords(title, description, keywords):
    if not keywords:
        return True
    haystack = (title or "") + (description or "")
    return any(kw in haystack for kw in keywords)


MEDIA_NS = "{http://search.yahoo.com/mrss/}"


def extract_enclosure_image(item):
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.get("url")
        media_type = enclosure.get("type", "")
        if url and media_type.startswith("image"):
            return url
    thumb = item.find(MEDIA_NS + "thumbnail")
    if thumb is not None and thumb.get("url"):
        return thumb.get("url")
    return None


def fetch_source_items(source):
    name = source["name"]
    url = source["url"]
    keywords = source.get("keywords") or []
    try:
        raw = fetch_bytes(url)
        root = ET.fromstring(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, OSError) as e:
        print("  [警告] 取得失敗: {} ({}) -> {}".format(name, url, e))
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        pubdate_raw = item.findtext("pubDate")
        sort_key, display_date = parse_pubdate(pubdate_raw)

        if not title or not link:
            continue
        if not matches_keywords(title, description, keywords):
            continue

        items.append({
            "title": title,
            "link": link,
            "description": description,
            "source": name,
            "sort_key": sort_key,
            "display_date": display_date,
            "image": extract_enclosure_image(item),
        })
    return items


def build_category_items(category, max_age_days, max_items):
    all_items = []
    for source in category["sources"]:
        all_items.extend(fetch_source_items(source))

    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    filtered = [it for it in all_items if it["sort_key"] is None or it["sort_key"] >= cutoff]

    # 同一URLの重複を除去（複数ソース・複数カテゴリで同じ記事が出る場合に対応）
    seen = set()
    deduped = []
    for it in filtered:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        deduped.append(it)

    deduped.sort(key=lambda it: it["sort_key"] or datetime.min, reverse=True)
    return deduped[:max_items]


# ---------------------------------------------------------------------------
# 元記事ページからの画像・要約（og:image / og:description）取得
# ---------------------------------------------------------------------------

OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_IMAGE_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)
OG_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_DESC_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:description["\']',
    re.IGNORECASE,
)


def fetch_article_meta(url):
    """記事ページから og:image / og:description を抽出する（取得失敗時は (None, None)）。"""
    try:
        raw = fetch_bytes_with_timeout(url)
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return None, None

    image = None
    m = OG_IMAGE_RE.search(text) or OG_IMAGE_RE_REV.search(text)
    if m:
        image = html.unescape(m.group(1)).strip()

    desc = None
    m2 = OG_DESC_RE.search(text) or OG_DESC_RE_REV.search(text)
    if m2:
        desc = html.unescape(m2.group(1)).strip()

    return image, desc


def fetch_bytes_with_timeout(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=ARTICLE_FETCH_TIMEOUT_SEC) as resp:
        return resp.read(ARTICLE_MAX_BYTES)


def enrich_items_with_article_meta(items):
    """画像または要約が欠けている記事について、元記事ページから補完する（並列実行）。"""
    targets = [it for it in items if not it["image"] or not it["description"]]
    if not targets:
        return

    print("記事ページから画像・要約を補完中: {} 件".format(len(targets)))
    with ThreadPoolExecutor(max_workers=META_FETCH_WORKERS) as executor:
        future_to_item = {executor.submit(fetch_article_meta, it["link"]): it for it in targets}
        for future in as_completed(future_to_item):
            it = future_to_item[future]
            try:
                image, desc = future.result()
            except Exception:
                image, desc = None, None
            if not it["image"] and image:
                it["image"] = image
            if not it["description"] and desc:
                it["description"] = desc


def drop_generic_duplicate_images(items):
    """同一画像URLが複数の記事で使われている場合は、サイト共通バナー等とみなして除外する。"""
    counts = {}
    for it in items:
        if it["image"]:
            counts[it["image"]] = counts.get(it["image"], 0) + 1
    for it in items:
        if it["image"] and counts.get(it["image"], 0) > 1:
            it["image"] = None


def truncate_summary(text, max_len=SUMMARY_MAX_LEN):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


# ---------------------------------------------------------------------------
# HTML生成
# ---------------------------------------------------------------------------

def escape_html(text):
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


PAGE_CSS = """
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { -webkit-text-size-adjust: 100%; }
  body { font-family: "Hiragino Kaku Gothic ProN", "Meiryo", "Segoe UI", sans-serif;
         background: #f5f6f8; color: #1f2329; margin: 0; padding: 0 0 60px; }
  header { background: #16243f; color: #fff; padding: 18px 16px; }
  header h1 { margin: 0 0 4px; font-size: 19px; }
  header .ts { font-size: 12px; color: #b7c2d6; }
  header .disclaimer { font-size: 11px; color: #8fa0c2; margin-top: 8px; line-height: 1.5; }
  nav { background: #1f3258; padding: 10px 12px; display: flex; flex-wrap: nowrap; gap: 10px;
        position: sticky; top: 0; z-index: 10; overflow-x: auto; -webkit-overflow-scrolling: touch;
        scrollbar-width: none; }
  nav::-webkit-scrollbar { display: none; }
  nav a { color: #d7e2f5; text-decoration: none; font-size: 14px; flex: 0 0 auto;
          padding: 6px 10px; background: rgba(255,255,255,0.08); border-radius: 14px; white-space: nowrap; }
  nav a:hover, nav a:active { background: rgba(255,255,255,0.18); }
  main { max-width: 820px; margin: 0 auto; padding: 14px 10px; }
  .category { margin-bottom: 32px; }
  .category h2 { font-size: 18px; border-left: 5px solid #2b59c3; padding-left: 10px; margin-bottom: 12px; }
  .card { background: #fff; border-radius: 12px; padding: 0; margin-bottom: 14px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden;
          display: flex; flex-direction: column; gap: 0; }
  .card .thumb-wrap { width: 100%; position: relative; background: #eef1f6; }
  .thumb { width: 100%; height: 180px; object-fit: cover; display: block; }
  .thumb-fallback { width: 100%; height: 180px; display: flex; align-items: center; justify-content: center;
                     font-size: 52px; background: linear-gradient(135deg,#e7ecf5,#cfd8e8); }
  .card-body { padding: 14px 16px; flex: 1; min-width: 0; position: relative; }
  .badge-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .badge { font-size: 12px; font-weight: 700; color: #fff; padding: 3px 10px; border-radius: 10px; }
  .card-title { font-size: 17px; font-weight: 600; color: #16243f; text-decoration: none; display: block;
                line-height: 1.4; }
  .card-title:hover { text-decoration: underline; }
  .card-meta { font-size: 13px; color: #6b7686; margin: 6px 0 8px; }
  .card-desc { font-size: 14.5px; color: #3a4150; margin: 0 0 10px; line-height: 1.6; }
  .audit-comment { font-size: 13px; color: #1d4f6b; background: #eaf3fa; border-left: 3px solid #2b86c5;
                    padding: 8px 10px; border-radius: 4px; margin: 8px 0; line-height: 1.55; }
  .audit-comment b { color: #0d3a52; }
  .card-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; }
  .source-link { font-size: 13.5px; color: #2b59c3; text-decoration: none; padding: 6px 0; }
  .source-link:hover { text-decoration: underline; }
  .fav-btn { border: none; background: none; cursor: pointer; font-size: 28px; color: #c7ccd6; line-height: 1;
             padding: 4px 8px; margin: -4px -8px; }
  .fav-btn.active { color: #e3a008; }
  .empty { color: #8a93a3; font-size: 13px; }
  #favorites .card { border: 1px dashed #d8b34d; }
  .fav-remove { font-size: 13px; color: #b23b3b; background: none; border: 1px solid #b23b3b;
                border-radius: 10px; padding: 4px 12px; cursor: pointer; }

  /* タブレット・PCなど画面が広い場合はサムネイルを左に並べて表示 */
  @media (min-width: 640px) {
    header { padding: 24px 20px; }
    header h1 { font-size: 22px; }
    main { padding: 20px; }
    .card { flex-direction: row; }
    .card .thumb-wrap { flex: 0 0 160px; width: 160px; }
    .thumb, .thumb-fallback { width: 160px; height: 160px; }
  }
"""

PAGE_JS = """
function favStorageKey() { return 'news_digest_favorites'; }

function loadFavorites() {
  try {
    return JSON.parse(localStorage.getItem(favStorageKey()) || '[]');
  } catch (e) {
    return [];
  }
}

function saveFavorites(favs) {
  localStorage.setItem(favStorageKey(), JSON.stringify(favs));
}

function isFavorited(link, favs) {
  return favs.some(function (f) { return f.link === link; });
}

function toggleFavorite(btn) {
  var favs = loadFavorites();
  var link = btn.getAttribute('data-link');
  var idx = favs.findIndex(function (f) { return f.link === link; });
  if (idx >= 0) {
    favs.splice(idx, 1);
  } else {
    favs.push({
      link: link,
      title: btn.getAttribute('data-title'),
      source: btn.getAttribute('data-source'),
      date: btn.getAttribute('data-date'),
      summary: btn.getAttribute('data-summary'),
      category: btn.getAttribute('data-category'),
      image: btn.getAttribute('data-image'),
      icon: btn.getAttribute('data-icon'),
      importance: btn.getAttribute('data-importance'),
      comment: btn.getAttribute('data-comment')
    });
  }
  saveFavorites(favs);
  refreshFavoriteButtons();
  renderFavoritesSection();
}

function refreshFavoriteButtons() {
  var favs = loadFavorites();
  document.querySelectorAll('.fav-btn').forEach(function (btn) {
    var link = btn.getAttribute('data-link');
    if (isFavorited(link, favs)) {
      btn.textContent = '★';
      btn.classList.add('active');
    } else {
      btn.textContent = '☆';
      btn.classList.remove('active');
    }
  });
}

function escapeHtml(text) {
  var div = document.createElement('div');
  div.textContent = text || '';
  return div.innerHTML;
}

function favoriteCardHtml(f) {
  var safeLink = escapeHtml(f.link);
  var icon = escapeHtml(f.icon || '📰');
  var thumb = f.image
    ? '<img class="thumb" src="' + escapeHtml(f.image) + '" onerror="this.outerHTML=\\'<div class=&quot;thumb-fallback&quot;>' + icon + '</div>\\'">'
    : '<div class="thumb-fallback">' + icon + '</div>';
  return (
    '<article class="card">' +
    '<div class="thumb-wrap">' + thumb + '</div>' +
    '<div class="card-body">' +
    '<div class="badge-row"><span class="badge" style="background:#8a93a3;">' + escapeHtml(f.category) + '</span></div>' +
    '<a class="card-title" href="' + safeLink + '" target="_blank" rel="noopener">' + escapeHtml(f.title) + '</a>' +
    '<div class="card-meta">' + escapeHtml(f.source) + ' ・ ' + escapeHtml(f.date) + '</div>' +
    '<p class="card-desc">' + escapeHtml(f.summary) + '</p>' +
    '<div class="audit-comment"><b>会計・監査への影響(参考):</b> ' + escapeHtml(f.comment) + '</div>' +
    '<div class="card-footer">' +
    '<a class="source-link" href="' + safeLink + '" target="_blank" rel="noopener">元記事を読む &rarr;</a>' +
    '<button class="fav-remove" data-link="' + safeLink + '">削除</button>' +
    '</div></div></article>'
  );
}

function renderFavoritesSection() {
  var favs = loadFavorites();
  var countEl = document.getElementById('fav-count');
  if (countEl) { countEl.textContent = favs.length; }
  var container = document.getElementById('favorites-list');
  if (!container) { return; }
  if (favs.length === 0) {
    container.innerHTML = '<p class="empty">お気に入りはまだありません。各記事の☆ボタンで登録できます。</p>';
    return;
  }
  container.innerHTML = favs.map(favoriteCardHtml).join('');
  container.querySelectorAll('.fav-remove').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var link = btn.getAttribute('data-link');
      var favs2 = loadFavorites().filter(function (f) { return f.link !== link; });
      saveFavorites(favs2);
      refreshFavoriteButtons();
      renderFavoritesSection();
    });
  });
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.fav-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { toggleFavorite(btn); });
  });
  refreshFavoriteButtons();
  renderFavoritesSection();
});
"""


def render_thumb(item, icon):
    if item["image"]:
        return (
            '<img class="thumb" src="{src}" alt="" loading="lazy" '
            'onerror="this.outerHTML=\'<div class=&quot;thumb-fallback&quot;>{icon}</div>\'">'
        ).format(src=escape_html(item["image"]), icon=icon)
    return '<div class="thumb-fallback">{icon}</div>'.format(icon=icon)


def render_card(item, category_name, icon):
    summary = truncate_summary(item["description"])
    comment = generate_audit_comment(item["title"], item["description"])
    importance = item["importance"]
    color = IMPORTANCE_COLOR[importance]

    return """
            <article class="card">
              <div class="thumb-wrap">{thumb}</div>
              <div class="card-body">
                <div class="badge-row"><span class="badge" style="background:{color};">重要度: {importance}</span></div>
                <a class="card-title" href="{link}" target="_blank" rel="noopener">{title}</a>
                <div class="card-meta">{source} ・ {date}</div>
                <p class="card-desc">{summary}</p>
                <div class="audit-comment"><b>会計・監査への影響(参考):</b> {comment}</div>
                <div class="card-footer">
                  <a class="source-link" href="{link}" target="_blank" rel="noopener">元記事を読む &rarr;</a>
                  <button class="fav-btn" data-link="{link_attr}" data-title="{title_attr}"
                          data-source="{source_attr}" data-date="{date_attr}" data-summary="{summary_attr}"
                          data-category="{category_attr}" data-image="{image_attr}" data-icon="{icon}"
                          data-importance="{importance}" data-comment="{comment_attr}">☆</button>
                </div>
              </div>
            </article>""".format(
        thumb=render_thumb(item, icon),
        color=color,
        importance=importance,
        link=escape_html(item["link"]),
        title=escape_html(item["title"]),
        source=escape_html(item["source"]),
        date=escape_html(item["display_date"]),
        summary=escape_html(summary),
        comment=escape_html(comment),
        link_attr=escape_html(item["link"]),
        title_attr=escape_html(item["title"]),
        source_attr=escape_html(item["source"]),
        date_attr=escape_html(item["display_date"]),
        summary_attr=escape_html(summary),
        category_attr=escape_html(category_name),
        image_attr=escape_html(item["image"] or ""),
        icon=icon,
        comment_attr=escape_html(comment),
    )


def render_html(generated_at, categories_with_items):
    nav_links = "\n".join(
        '<a href="#{id}">{icon} {name}</a>'.format(id=cat["id"], icon=cat.get("icon", DEFAULT_ICON), name=escape_html(cat["name"]))
        for cat, _ in categories_with_items
    )
    nav_links = '<a href="#favorites">★ お気に入り(<span id="fav-count">0</span>)</a>\n' + nav_links

    sections = []
    for cat, items in categories_with_items:
        icon = cat.get("icon", DEFAULT_ICON)
        if items:
            cards = "\n".join(render_card(it, cat["name"], icon) for it in items)
        else:
            cards = '<p class="empty">該当する記事が見つかりませんでした。</p>'

        sections.append(
            '\n        <section id="{id}" class="category">\n'
            '          <h2>{icon} {name}</h2>\n'
            '          {cards}\n'
            '        </section>'.format(id=cat["id"], icon=icon, name=escape_html(cat["name"]), cards=cards)
        )

    favorites_section = (
        '\n        <section id="favorites" class="category">\n'
        '          <h2>★ お気に入り</h2>\n'
        '          <div id="favorites-list"></div>\n'
        '        </section>'
    )

    html_doc = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>世界経済・政治ニュース ダイジェスト</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>世界経済・政治ニュース ダイジェスト</h1>
  <div class="ts">生成日時: {generated_at}</div>
  <div class="disclaimer">※重要度判定および「会計・監査への影響」コメントはキーワードに基づく簡易的な自動生成です。実務上の判断には必ず原文・専門的知見を確認してください。</div>
</header>
<nav>
{nav_links}
</nav>
<main>
{favorites_section}
{sections}
</main>
<script>{js}</script>
</body>
</html>
""".format(
        css=PAGE_CSS,
        generated_at=escape_html(generated_at),
        nav_links=nav_links,
        favorites_section=favorites_section,
        sections="".join(sections),
        js=PAGE_JS,
    )
    return html_doc


def main():
    config = load_config()
    max_items = config.get("max_items_per_category", 12)
    max_age_days = config.get("max_age_days", 14)
    categories = config["categories"]

    REPORTS_DIR.mkdir(exist_ok=True)

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    print("ニュース取得開始: {} JST".format(now_jst.strftime("%Y-%m-%d %H:%M")))

    categories_with_items = []
    all_items = []
    for cat in categories:
        print("取得中: {}".format(cat["name"]))
        items = build_category_items(cat, max_age_days, max_items)
        print("  -> {} 件".format(len(items)))
        categories_with_items.append((cat, items))
        all_items.extend(items)

    # 記事間で同じ link を共有するオブジェクトは重複しうるため、id(link)単位で一意化して補完
    unique_items = list({id(it): it for it in all_items}.values())
    enrich_items_with_article_meta(unique_items)
    drop_generic_duplicate_images(unique_items)

    for it in unique_items:
        it["importance"] = score_importance(it["title"], it["description"])

    # 重要度順（高→中→低）→ カテゴリ内は日時順（新しい順）で並べ替え
    # sort()はstableなので、先に日時降順→次に重要度昇順の順で適用すると
    # 同一重要度内では日時降順が保たれる。
    for _, items in categories_with_items:
        items.sort(key=lambda it: it["sort_key"] or datetime.min, reverse=True)
        items.sort(key=lambda it: IMPORTANCE_ORDER[it["importance"]])

    generated_at = now_jst.strftime("%Y-%m-%d %H:%M") + " JST"
    html_doc = render_html(generated_at, categories_with_items)

    dated_path = REPORTS_DIR / "{}.html".format(now_jst.strftime("%Y-%m-%d"))
    index_path = BASE_DIR / "index.html"

    dated_path.write_text(html_doc, encoding="utf-8")
    index_path.write_text(html_doc, encoding="utf-8")

    print("完了: {}".format(dated_path))
    print("完了: {}".format(index_path))


if __name__ == "__main__":
    sys.exit(main())
