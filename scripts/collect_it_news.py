# -*- coding: utf-8 -*-
"""
IT 뉴스 리서치 대시보드 자동 수집
실행: python collect_it_news.py (로컬: 09.IT뉴스_업데이트.bat / 클라우드: GitHub Actions .github/workflows/update.yml)

이 스크립트는 deploy-repo(=이 파일이 속한 git 저장소, nutrione-serena/it-news-dashboard) 안에서
동작하도록 되어 있다 — data/news_db.json, index.html 모두 이 저장소 안의 파일이라 로컬에서 실행하든
GitHub Actions(매일 자동 실행)에서 실행하든 같은 데이터베이스를 공유한다.

- FEEDS 에 등록된 국내/해외 IT 전문 매체 RSS를 수집
- 신규 기사만 골라 카테고리(AI/신기술, UI/UX, 법규/정책, 기타) + 기획자 주목(★) 자동 분류
- data/news_db.json 에 누적 저장 (기존 기사는 유지, 중복은 링크 기준으로 제외)
- index.html 을 전체 데이터 기준으로 다시 생성
- git commit + push (로컬: 사용자 git 자격증명 / Actions: GITHUB_TOKEN) 로 GitHub Pages에 자동 반영
- 로컬 실행 시에는 먼저 git pull 로 GitHub Actions가 그 사이 수집해둔 내용을 먼저 받아온 뒤 이어서 수집한다
"""
import json
import os
import re
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # deploy-repo/
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "news_db.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "index.html")
PAGES_URL = "https://nutrione-serena.github.io/it-news-dashboard/"

os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 국내/해외 IT 전문 매체 RSS. 새 매체를 추가하려면 이 목록에 한 줄만 추가하면 된다.
FEEDS = [
    {"name": "전자신문", "region": "국내", "url": "https://rss.etnews.com/Section901.xml"},
    {"name": "블로터", "region": "국내", "url": "https://www.bloter.net/rss/allArticle.xml"},
    {"name": "AI타임스", "region": "국내", "url": "https://www.aitimes.com/rss/allArticle.xml"},
    {"name": "테크M", "region": "국내", "url": "https://www.techm.kr/rss/allArticle.xml"},
    {"name": "TechCrunch", "region": "해외", "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge", "region": "해외", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica", "region": "해외", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Smashing Magazine", "region": "해외", "url": "https://www.smashingmagazine.com/feed/"},
    {"name": "UX Collective", "region": "해외", "url": "https://uxdesign.cc/feed"},
    {"name": "MIT Technology Review", "region": "해외", "url": "https://www.technologyreview.com/feed/"},
]

CATEGORY_KEYWORDS = {
    "법규/정책": [
        "법", "법안", "법률", "개정", "시행령", "시행규칙", "규제", "과징금", "공정거래",
        "개인정보보호법", "전자상거래법", "전자상거래", "AI기본법", "규정", "가이드라인",
        "law", "regulation", "policy", "compliance", "bill", "gdpr", "act ",
    ],
    "UI/UX": [
        "ui", "ux", "디자인", "사용자경험", "사용자 경험", "사용성", "접근성", "인터페이스",
        "다크모드", "리디자인", "design", "interface", "usability", "accessibility",
        "user experience", "wireframe", "prototyp",
    ],
    "AI/신기술": [
        "ai", "인공지능", "gpt", "llm", "생성형", "머신러닝", "딥러닝", "챗봇", "로봇",
        "반도체", "양자", "클라우드", "메타버스", "자율주행", "알고리즘", "온디바이스",
        "artificial intelligence", "machine learning", "neural", "chatbot", "chip",
        "quantum", "semiconductor", "generative",
    ],
}
CATEGORY_ORDER = ["법규/정책", "AI/신기술", "UI/UX"]

# 기획자 관점에서 "챙겨봐야 할" 기사를 자동으로 눈에 띄게 표시하기 위한 키워드.
# 일반적인 단어(출시/공개 등)는 거의 모든 기사에 걸려 의미가 없으므로,
# 실제로 제품/정책 의사결정에 영향을 주는 구체적인 표현 위주로 구성했다.
# 필요하면 이 리스트만 조정하면 된다 (하나라도 걸리면 주목 표시).
PLANNER_KEYWORDS = [
    "정식 출시", "베타 출시", "먼저 만나보", "신규 서비스 출시", "서비스 종료", "단계적 종료",
    "정책 변경", "약관 개정", "이용약관 변경", "개인정보 처리방침", "요금제 변경", "가격 정책",
    "시행령 개정", "시행일", "단계적 시행", "유예기간", "의무화", "과징금", "제재",
    "규제 샌드박스", "가이드라인 발표", "전면 개편", "리디자인", "UX 개편", "인터페이스 개편",
    "접근성 의무", "대규모 업데이트", "정책 발표",
    "policy change", "terms of service", "redesign", "relaunch", "sunset", "deprecat",
    "pricing change", "accessibility mandate", "compliance deadline", "rolling out", "rollout",
]


def categorize(title, summary):
    text = f"{title} {summary}".lower()
    tags = []
    for cat in CATEGORY_ORDER:
        if any(kw in text for kw in CATEGORY_KEYWORDS[cat]):
            tags.append(cat)
    return tags or ["기타"]


def planner_flag(title, summary):
    text = f"{title} {summary}".lower()
    hits = [kw for kw in PLANNER_KEYWORDS if kw.lower() in text]
    return bool(hits), hits[:3]


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    if root.tag.endswith("feed"):
        for entry in root.findall("a:entry", ATOM_NS):
            title = (entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
            link_el = entry.find("a:link[@rel='alternate']", ATOM_NS)
            if link_el is None:
                link_el = entry.find("a:link", ATOM_NS)
            link = link_el.get("href") if link_el is not None else ""
            summary = entry.findtext("a:summary", default="", namespaces=ATOM_NS) or entry.findtext(
                "a:content", default="", namespaces=ATOM_NS
            )
            pub = entry.findtext("a:updated", default="", namespaces=ATOM_NS) or entry.findtext(
                "a:published", default="", namespaces=ATOM_NS
            )
            items.append({"title": title, "link": link, "summary": summary, "pub": pub})
    else:
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            summary = item.findtext("description") or ""
            pub = item.findtext("pubDate") or ""
            items.append({"title": title, "link": link, "summary": summary, "pub": pub})
    return items


def fetch_feed(feed):
    try:
        resp = requests.get(feed["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return parse_feed(resp.content)
    except Exception as exc:
        print(f"  [경고] {feed['name']} 수집 실패: {exc}")
        return []


def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_db(records):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def collect():
    db = load_db()
    known_links = {r["link"] for r in db}
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_count = 0

    for feed in FEEDS:
        print(f"[수집] {feed['name']} ({feed['region']})")
        raw_items = fetch_feed(feed)
        for raw in raw_items:
            link = raw["link"].strip()
            if not link or link in known_links:
                continue
            known_links.add(link)
            summary = clean_html(raw["summary"])
            dt = parse_date(raw["pub"])
            flag, flag_hits = planner_flag(raw["title"], summary)
            record = {
                "title": raw["title"].strip(),
                "link": link,
                "summary": summary,
                "source": feed["name"],
                "region": feed["region"],
                "published": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "published_sort": dt.astimezone(timezone.utc).isoformat() if dt else "",
                "collected_at": collected_at,
                "tags": categorize(raw["title"], summary),
                "flag": flag,
                "flag_hits": flag_hits,
            }
            db.append(record)
            new_count += 1

    # 카테고리/주목 키워드를 나중에 조정했을 때도 과거 기사에 소급 적용되도록,
    # 매번 전체 레코드에 대해 다시 계산한다 (재수집이 아니라 로컬 재분류라 비용이 적음).
    for r in db:
        r["tags"] = categorize(r["title"], r.get("summary", ""))
        r["flag"], r["flag_hits"] = planner_flag(r["title"], r.get("summary", ""))

    db.sort(key=lambda r: r.get("published_sort") or "", reverse=True)
    save_db(db)
    print(f"\n신규 기사 {new_count}건 추가 (전체 누적 {len(db)}건, 기획자 주목 {sum(1 for r in db if r['flag'])}건)")
    return db, new_count


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>IT 뉴스 리서치 대시보드</title>
<style>
  :root {{
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e9ef;
    --muted: #9aa4b2; --accent: #5b8cff; --tag-ai: #5b8cff; --tag-uiux: #33c48d;
    --tag-law: #ff9f43; --tag-etc: #7a8290; --flag: #ffd166;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Pretendard", "Malgun Gothic", -apple-system, sans-serif;
  }}
  header {{
    padding: 24px 32px 12px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--bg); z-index: 10;
  }}
  h1 {{ margin: 0 0 4px; font-size: 20px; }}
  .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 16px; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-bottom: 16px; }}
  input[type="text"] {{
    flex: 1; min-width: 220px; padding: 9px 14px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--panel); color: var(--text); font-size: 14px;
  }}
  .chip {{
    padding: 7px 14px; border-radius: 20px; border: 1px solid var(--border);
    background: var(--panel); color: var(--muted); cursor: pointer; font-size: 13px; user-select: none;
  }}
  .chip.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  main {{ padding: 16px 32px 48px; }}
  .count {{ color: var(--muted); font-size: 13px; margin: 0 0 12px; }}
  .card {{
    border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px;
    margin-bottom: 12px; background: var(--panel);
  }}
  .card-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }}
  .card-title {{ font-size: 15px; font-weight: 600; }}
  .card-title a {{ color: var(--text); text-decoration: none; }}
  .card-title a:hover {{ color: var(--accent); text-decoration: underline; }}
  .card-date {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
  .card-summary {{ color: var(--muted); font-size: 13px; margin: 8px 0; line-height: 1.5; }}
  .card-bottom {{ display: flex; gap: 6px; flex-wrap: wrap; align-items: center; font-size: 12px; }}
  .src {{ color: var(--muted); }}
  .tag {{ padding: 2px 9px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
  .tag.ai {{ background: rgba(91,140,255,.15); color: var(--tag-ai); }}
  .tag.uiux {{ background: rgba(51,196,141,.15); color: var(--tag-uiux); }}
  .tag.law {{ background: rgba(255,159,67,.15); color: var(--tag-law); }}
  .tag.etc {{ background: rgba(122,130,144,.2); color: var(--tag-etc); }}
  .tag.flag {{ background: rgba(255,209,102,.18); color: var(--flag); }}
  .card.flagged {{ border-left: 3px solid var(--flag); }}
  .star {{ color: var(--flag); margin-right: 4px; }}
  .empty {{ color: var(--muted); padding: 40px; text-align: center; }}
  .more-btn {{
    display: block; margin: 8px auto 0; padding: 9px 20px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--panel); color: var(--text);
    cursor: pointer; font-size: 13px;
  }}
  .more-btn:hover {{ border-color: var(--accent); }}
  .limit-note {{ color: var(--muted); font-size: 12px; margin: 4px 0 12px; }}
</style>
</head>
<body>
<header>
  <h1>IT 뉴스 리서치 대시보드</h1>
  <div class="meta">마지막 업데이트: {generated_at} · 전체 누적 {total_count}건(대시보드엔 최근 {embedded_count}건 표시) · 기획자 주목 {flag_count}건 · 매체 {feed_count}곳 · 매일 자동 갱신</div>
  <div class="controls">
    <input type="text" id="search" placeholder="제목·요약 검색 (예: AI, GDPR, 다크모드)">
    <div class="chip active" data-cat="전체">전체</div>
    <div class="chip" data-cat="__flag">★ 기획자 주목</div>
    <div class="chip" data-cat="AI/신기술">AI/신기술</div>
    <div class="chip" data-cat="UI/UX">UI/UX</div>
    <div class="chip" data-cat="법규/정책">법규/정책</div>
    <div class="chip" data-cat="기타">기타</div>
    <div class="chip" data-cat="__국내">국내</div>
    <div class="chip" data-cat="__해외">해외</div>
  </div>
</header>
<main>
  <p class="count" id="count"></p>
  <div id="list"></div>
  <button class="more-btn" id="moreBtn" hidden></button>
</main>
<script>
const DATA = {data_json};
const PAGE_SIZE = 150;

const TAG_CLASS = {{"AI/신기술": "ai", "UI/UX": "uiux", "법규/정책": "law", "기타": "etc"}};

let activeCat = "전체";
let activeRegion = null;
let onlyFlag = false;
let shown = PAGE_SIZE;
const searchInput = document.getElementById("search");
const listEl = document.getElementById("list");
const countEl = document.getElementById("count");
const moreBtn = document.getElementById("moreBtn");

document.querySelectorAll(".chip").forEach(chip => {{
  chip.addEventListener("click", () => {{
    const cat = chip.dataset.cat;
    if (cat === "__flag") {{
      onlyFlag = !onlyFlag;
      chip.classList.toggle("active", onlyFlag);
    }} else if (cat.startsWith("__")) {{
      const region = cat.slice(2);
      if (activeRegion === region) {{
        activeRegion = null; chip.classList.remove("active");
      }} else {{
        document.querySelectorAll(".chip[data-cat^='__'][data-cat!='__flag']").forEach(c => c.classList.remove("active"));
        activeRegion = region; chip.classList.add("active");
      }}
    }} else {{
      document.querySelectorAll(".chip:not([data-cat^='__'])").forEach(c => c.classList.remove("active"));
      activeCat = cat; chip.classList.add("active");
    }}
    shown = PAGE_SIZE;
    render();
  }});
}});

let searchTimer = null;
searchInput.addEventListener("input", () => {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{ shown = PAGE_SIZE; render(); }}, 150);
}});
moreBtn.addEventListener("click", () => {{ shown += PAGE_SIZE; render(); }});

function render() {{
  const q = searchInput.value.trim().toLowerCase();
  let rows = DATA.filter(r => activeCat === "전체" || r.tags.includes(activeCat));
  if (activeRegion) rows = rows.filter(r => r.region === activeRegion);
  if (onlyFlag) rows = rows.filter(r => r.flag);
  if (q) rows = rows.filter(r => (r.title + " " + r.summary).toLowerCase().includes(q));

  countEl.textContent = rows.length + "건";
  if (rows.length === 0) {{
    listEl.innerHTML = '<div class="empty">해당 조건의 기사가 없습니다.</div>';
    moreBtn.hidden = true;
    return;
  }}
  const visible = rows.slice(0, shown);
  listEl.innerHTML = visible.map(r => `
    <div class="card ${{r.flag ? 'flagged' : ''}}">
      <div class="card-top">
        <div class="card-title">${{r.flag ? '<span class="star" title="기획자 주목: ' + escapeHtml((r.flag_hits||[]).join(', ')) + '">★</span>' : ''}}<a href="${{r.link}}" target="_blank" rel="noopener">${{escapeHtml(r.title)}}</a></div>
        <div class="card-date">${{r.published || r.collected_at}}</div>
      </div>
      ${{r.summary ? `<div class="card-summary">${{escapeHtml(r.summary)}}</div>` : ""}}
      <div class="card-bottom">
        <span class="src">${{r.source}} · ${{r.region}}</span>
        ${{r.tags.map(t => `<span class="tag ${{TAG_CLASS[t] || 'etc'}}">${{t}}</span>`).join("")}}
      </div>
    </div>
  `).join("");
  moreBtn.hidden = rows.length <= shown;
  moreBtn.textContent = `더 보기 (${{rows.length - shown}}건 더 있음)`;
}}

function escapeHtml(s) {{
  return (s || "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
}}

render();
</script>
</body>
</html>
"""

# 대시보드 HTML에는 최신 EMBED_MAX건까지만 인라인으로 넣는다 (기획자 주목 표시된 기사는
# 오래됐어도 전부 포함). 전체 누적 데이터는 data/news_db.json에 그대로 남아있으니
# 필요하면 별도로 열어보면 된다. HTML이 계속 커져서 검색/스크롤이 느려지는 걸 막기 위함.
EMBED_MAX = 1500


def build_dashboard(db):
    feed_names = sorted({r["source"] for r in db})
    flagged = [r for r in db if r["flag"]]
    recent = db[:EMBED_MAX]
    embed_ids = {r["link"] for r in recent}
    for r in flagged:
        if r["link"] not in embed_ids:
            recent.append(r)
            embed_ids.add(r["link"])
    recent.sort(key=lambda r: r.get("published_sort") or "", reverse=True)

    html = DASHBOARD_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_count=len(db),
        embedded_count=len(recent),
        flag_count=len(flagged),
        feed_count=len(feed_names),
        data_json=json.dumps(recent, ensure_ascii=False),
    )
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def run_git(args):
    return subprocess.run(
        ["git"] + args, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8"
    )


def git_pull_latest():
    """로컬 실행 시, GitHub Actions가 그 사이 수집해둔 최신 데이터를 먼저 받아온다.
    실패해도(오프라인 등) 스크립트를 막지 않고 로컬에 있던 데이터로 계속 진행한다."""
    result = run_git(["pull", "--rebase"])
    if result.returncode != 0:
        print(f"  [경고] git pull 실패 — 로컬 데이터로 계속 진행합니다: {result.stderr.strip()}")
    else:
        print("  git pull 완료 (원격의 최신 데이터를 받아왔습니다)")


def publish_to_git():
    """data/news_db.json + index.html 변경분을 git commit + push.
    (nutrione-serena/it-news-dashboard, GitHub Pages) 실패해도 로컬 대시보드 생성 자체는 이미 끝난 뒤라
    전체 스크립트를 중단시키지 않고 경고만 출력한다."""
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        print("  [안내] git 저장소가 아니라 배포를 건너뜁니다.")
        return False

    add = run_git(["add", "index.html", "data/news_db.json"])
    if add.returncode != 0:
        print(f"  [경고] git add 실패: {add.stderr}")
        return False

    diff = run_git(["diff", "--cached", "--name-only"])
    if not diff.stdout.strip():
        print("  변경된 내용 없음 (이미 최신 상태) — push 생략")
        return True

    commit = run_git(["commit", "-m", f"대시보드 업데이트 {datetime.now().strftime('%Y-%m-%d %H:%M')}"])
    if commit.returncode != 0:
        print(f"  [경고] git commit 실패: {commit.stderr}")
        return False

    push = run_git(["push"])
    if push.returncode != 0:
        print(f"  [경고] git push 실패 (인터넷 연결/로그인 확인): {push.stderr}")
        return False

    print("  git push 완료")
    return True


def main():
    print("=== IT 뉴스 수집 시작 ===")
    if not IS_CI:
        git_pull_latest()

    db, new_count = collect()
    build_dashboard(db)
    print(f"대시보드 생성 완료: {DASHBOARD_PATH}")

    print("=== GitHub Pages 배포 ===")
    pushed = publish_to_git()

    if IS_CI:
        return

    if pushed:
        print(f"배포 완료! 1~2분 내 반영: {PAGES_URL}")
        webbrowser.open(PAGES_URL)
    else:
        webbrowser.open(f"file:///{DASHBOARD_PATH.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
