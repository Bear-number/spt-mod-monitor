#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPT（单机塔科夫）MOD 监控（sp-mod.com）——自己抓取、翻页、静默比对缓存，只落盘不回显

sp-mod.com 没有拦截，直连可取（2026-10-22 本机实测 200 / 约 378KB / 每页 12 张卡片），所以这个脚本
自己抓页面，不靠浏览器、也不把数据打到 stdout：抓 → 解析 → 与缓存比 → 有变化才写 report，退出码带状态。

退出码：0 无变化（report 已删） / 1 有变化（去读 report） / 2 抓到了但解析不出卡片 / 3 抓取或读写失败

用法：
  py -3 spt_monitor.py --update                # 自己抓取并翻页，比对后写缓存
  py -3 spt_monitor.py --hours 48 --print      # 给人看一眼用 --print
  py -3 spt_monitor.py --from-file page.html   # 用本地 HTML（调试或直连挂了的兜底），此时不翻页
  py -3 spt_monitor.py --selftest --print      # 离线自检，不联网、不碰真缓存

翻页：默认最多翻 5 页，**翻到某页最旧的更新时间已经早于窗口起点就停**（否则只取第一页会静默漏报）。
判更新：只认页面 `<time datetime>` 的绝对时间**严格晚于**缓存里记的那个；相对说法一律不拿来判更新。

缓存 `spt_cache.json`：`ids`、`mods`（{id: {title, slug, link, version, updated}}）、`last_check`、
`last_error`、`last_error_time`。
"""
import sys, os, re, json, time, html, argparse, tempfile
from datetime import datetime, timezone, timedelta
from urllib import request as _request

BASE = "https://sp-mod.com"
LIST_URL = f"{BASE}/mods?order=updated"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DATA = r"D:\Alife\Storage\SkillData"
CACHE = os.path.join(DATA, "spt_cache.json")
REPORT = os.path.join(DATA, "spt_report.txt")
ERROR = os.path.join(DATA, "spt_error.txt")
STATUS = os.path.join(DATA, "spt_status.txt")
NAME = "SPT"
SKILL = "spt-mod-monitor"

EXIT_NO_CHANGE = 0
EXIT_CHANGED = 1
EXIT_NO_ITEMS = 2
EXIT_FAILED = 3

LOCAL_TZ = timezone(timedelta(hours=8))
EPOCH = datetime.fromtimestamp(0, timezone.utc)
RELATIVE = re.compile(r"^(\d+\s*(second|minute|hour|day|week|month)s?\s*ago|\d+\s*(秒|分钟|小时|天|周|个月)前)", re.I)
CARD = re.compile(r'href="(?:https://sp-mod\.com)?/mod/(\d+)/([^"?#]+)"')
TIME_TAG = re.compile(r'<time[^>]*datetime="([^"]+)"')
H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
SPAN = re.compile(r"<span[^>]*>(.*?)</span>", re.S)

MAX_PAGES = 5
FETCH_TIMEOUT = 20


def parse_abs(raw):
    """页面时间串 → UTC datetime；空串、相对说法、认不出的一律回 None（不拿去判更新）"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        # 历史缓存里 date 可能存成 epoch 秒浮点（nexus 那边实测有 50 条），0.0 是占位不当时间看
        return datetime.fromtimestamp(raw, timezone.utc) if raw > 10 ** 8 else None
    text = str(raw or "").strip()
    if not text or text in ("?", "-") or RELATIVE.match(text):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment.astimezone(timezone.utc)


def show(moment):
    return moment.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M") if moment else "时间未知"


def load_cache():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            pass
    return {"ids": [], "mods": {}, "last_check": None, "last_error": None, "last_error_time": None}


def save_cache(cache):
    os.makedirs(DATA, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)


def write_error(reason, quiet, code):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(ERROR, "w", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(LOCAL_TZ).isoformat()} [{SKILL}] {reason}\n")
        cache = load_cache()
        cache["last_error"] = reason
        cache["last_error_time"] = datetime.now(LOCAL_TZ).isoformat()
        save_cache(cache)
    except Exception:
        pass
    if not quiet:
        print(f"[{NAME}] 退出码 {code}：{reason}")
    return code


def fetch(url):
    """抓一页 HTML；网络类失败退避后重试一次，最终失败抛出去"""
    last_error = None
    for attempt in (1, 2):
        try:
            req = _request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
            with _request.urlopen(req, timeout=FETCH_TIMEOUT) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as error:
            last_error = error
            if attempt == 1:
                time.sleep(2)
    raise RuntimeError(f"{type(last_error).__name__}: {last_error}")


def parse_page(text):
    """每张卡片取链接、标题/版本（h3 里的 span）、更新时间（卡片内的 time[datetime]）"""
    items, seen = [], set()
    for match in CARD.finditer(text):
        mid, slug = match.group(1), match.group(2)
        if mid in seen:
            continue
        seen.add(mid)
        segment = text[match.start():match.start() + 6000]
        time_match = TIME_TAG.search(segment)
        title, version = "", ""
        h3 = H3.search(segment)
        if h3:
            spans = [re.sub(r"\s+", " ", raw).strip() for raw in SPAN.findall(h3.group(1))]
            spans = [value for value in spans if value]
            if spans:
                title = spans[0]
                version = spans[-1] if len(spans) > 1 else ""
        items.append({"id": mid, "slug": slug, "title": html.unescape(title) or f"MOD {mid}",
                      "version": version,
                      "raw_time": time_match.group(1) if time_match else "",
                      "link": f"{BASE}/mod/{mid}/{slug}"})
    return items


def collect_pages(hours, max_pages):
    """自己翻页：翻到某页最旧时间早于窗口起点就停，避免只取第一页的静默漏报"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, pages = [], 0
    for page in range(1, max_pages + 1):
        text = fetch(f"{LIST_URL}&page={page}" if page > 1 else LIST_URL)
        found = parse_page(text)
        if not found:
            return items, pages, True
        pages = page
        known_ids = set(item["id"] for item in items)
        items.extend(item for item in found if item["id"] not in known_ids)
        moments = [moment for moment in (parse_abs(item["raw_time"]) for item in found) if moment]
        if not moments or min(moments) < cutoff:
            return items, pages, True
    return items, pages, False


def collect(items, cache, hours):
    known = set(str(x) for x in cache.get("ids", [])) | set((cache.get("mods") or {}).keys())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    mods = cache.get("mods") or {}
    rows, notes = [], {"unknown_time": 0, "outside": 0, "untitled": 0, "already": 0, "backfill": []}
    for item in items:
        moment = parse_abs(item["raw_time"])
        old = mods.get(item["id"]) or {}
        old_moment = parse_abs(old.get("updated") or old.get("date") or old.get("time") or "")
        if item["id"] not in known:
            tag = "新"
        elif moment is not None and old_moment is not None and moment > old_moment:
            tag = "更"
        elif moment is not None and old_moment is None:
            notes["backfill"].append((item["id"], item["raw_time"]))
            notes["already"] += 1
            continue
        else:
            notes["already"] += 1
            continue
        if moment is not None and moment < cutoff:
            notes["outside"] += 1
            continue
        if moment is None:
            notes["unknown_time"] += 1
        if item["title"].startswith("MOD "):
            notes["untitled"] += 1
        rows.append((tag, item, moment))
    rows.sort(key=lambda row: row[2] or EPOCH, reverse=True)
    return rows, notes, cutoff


def label(item):
    return f"{item['title']} v{item['version']}" if item["version"] else item["title"]


def build_report(rows, notes, items, pages, coverage, cutoff, hours):
    times = [moment for moment in (parse_abs(item["raw_time"]) for item in items) if moment]
    oldest = min(times) if times else None
    lines = [f"[{NAME}] 最近{hours}小时：新增 {sum(1 for r in rows if r[0] == '新')} 条、"
             f"更新 {sum(1 for r in rows if r[0] == '更')} 条（翻了 {pages} 页、抓到 {len(items)} 条"
             + (f"，窗口外 {notes['outside']} 条" if notes["outside"] else "") + "）"]
    for index, (tag, item, moment) in enumerate(rows, 1):
        lines.append(f"{index}. {tag} {label(item)} - {show(moment)} - {item['link']}")
    if notes["untitled"]:
        lines.append(f"[标题告警] {notes['untitled']} 条没解析出标题，按 MOD 号报")
    if notes["unknown_time"]:
        lines.append(f"[时间告警] {notes['unknown_time']} 条没有绝对时间，按ID判了新增、没拿它判更新")
    if notes["backfill"]:
        lines.append(f"[回填] {len(notes['backfill'])} 条缓存里原本没有绝对时间，这次补上了")
    if coverage:
        lines.append(coverage)
    return lines


def apply_update(cache, rows, notes):
    known = set(str(x) for x in cache.get("ids", []))
    for tag, item, moment in rows:
        if item["id"] not in known:
            cache.setdefault("ids", []).append(item["id"])
            known.add(item["id"])
        entry = cache.setdefault("mods", {}).setdefault(item["id"], {})
        entry.update({"title": item["title"], "slug": item["slug"], "link": item["link"],
                      "version": item["version"] or entry.get("version") or "",
                      "updated": item["raw_time"] or entry.get("updated") or "时间未知"})
    for mid, raw_time in notes["backfill"]:
        cache.setdefault("mods", {}).setdefault(mid, {})["updated"] = raw_time


def scan(hours, max_pages, update, quiet, from_file=None):
    """跑一遍并把一行纯计数写进 status（抓到几条、缓存已含几条），再原样返回结果"""
    code, count, stats = sweep(hours, max_pages, update, quiet, from_file)
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(STATUS, "w", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(LOCAL_TZ).strftime('%m-%d %H:%M')} [{NAME}] 退出码{code}"
                         f" 抓到{stats['got']} 缓存已含{stats['already']} 窗口外{stats['outside']}"
                         f" 新增{stats['new']} 更新{stats['updated']} 窗口{hours}小时 翻了{stats['pages']}页"
                         + (" [覆盖告警]" if stats.get("coverage") else "")
                         + (f" {stats['note']}" if stats.get("note") else "") + "\n")
    except Exception:
        pass
    return code, count


def sweep(hours, max_pages, update, quiet, from_file=None):
    """抓取（或读本地 HTML）→ 比对 → 落盘。返回 (退出码, 要报的条数, 计数)"""
    stats = {"got": 0, "already": 0, "outside": 0, "new": 0, "updated": 0,
             "unknown_time": 0, "untitled": 0, "pages": 0, "coverage": None, "note": ""}
    if from_file:
        try:
            with open(from_file, encoding="utf-8") as handle:
                items = parse_page(handle.read())
        except Exception as read_error:
            return write_error(f"读不到本地 HTML {from_file}：{read_error}", quiet, EXIT_FAILED), 0, stats
        if not items:
            stats["note"] = "本地 HTML 里没解析出卡片"
            return write_error(f"本地 HTML {from_file} 里没解析出卡片", quiet, EXIT_NO_ITEMS), 0, stats
        pages = 1
        coverage = "[覆盖告警] --from-file 只喂了一页，覆盖不住窗口——要完整就让脚本自己抓（去掉 --from-file）"
    else:
        try:
            items, pages, covered = collect_pages(hours, max_pages)
        except RuntimeError as fetch_error:
            stats["note"] = "抓取失败（已重试一次）"
            return write_error(f"抓取失败（已重试一次）：{fetch_error}", quiet, EXIT_FAILED), 0, stats
        if not items:
            stats["note"] = "页面里没卡片，多半改版或被挡"
            return write_error("抓到的页面里没有卡片（改版或被挡）", quiet, EXIT_NO_ITEMS), 0, stats
        coverage = None if covered else f"[覆盖告警] 翻到第 {pages} 页窗口还没被覆盖住，把 --pages 调大再跑（上限 20）"

    stats["got"] = len(items)
    stats["pages"] = pages
    stats["coverage"] = coverage
    cache = load_cache()
    rows, notes, cutoff = collect(items, cache, hours)
    stats.update({"already": notes["already"], "outside": notes["outside"], "untitled": notes["untitled"],
                  "unknown_time": notes["unknown_time"],
                  "new": sum(1 for row in rows if row[0] == "新"), "updated": sum(1 for row in rows if row[0] == "更")})
    lines = build_report(rows, notes, items, pages, coverage, cutoff, hours)

    if rows:
        with open(REPORT, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    elif os.path.exists(REPORT):
        os.remove(REPORT)

    if update:
        apply_update(cache, rows, notes)
        cache["last_check"] = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        cache["last_error"] = None
        save_cache(cache)

    if not quiet:
        print("\n".join(lines) if rows
              else f"[{NAME}] 无新增/更新（翻了 {pages} 页、抓到 {len(items)} 条，缓存已含 {stats['already']} 条，窗口 {hours} 小时）")
    return ((EXIT_CHANGED, len(rows), stats) if rows else (EXIT_NO_CHANGE, 0, stats))


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--pages", type=int, default=MAX_PAGES)
    parser.add_argument("--from-file", dest="from_file")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--print", dest="echo", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest(args.echo)

    return scan(hours=max(1, args.hours), max_pages=min(20, max(1, args.pages)),
                update=args.update, quiet=not args.echo, from_file=args.from_file)[0]


def _selftest(echo):
    """离线自检：不联网（用本地 HTML），缓存/report/error 改到临时目录"""
    global DATA, CACHE, REPORT, ERROR, STATUS
    DATA = tempfile.mkdtemp(prefix="monitor_selftest_")
    CACHE = os.path.join(DATA, "cache.json")
    REPORT = os.path.join(DATA, "report.txt")
    ERROR = os.path.join(DATA, "error.txt")
    STATUS = os.path.join(DATA, "status.txt")

    sample = ('<a href="https://sp-mod.com/mod/3194/openbarters" class="x">'
              '<h3><span>Open Barters</span><span>1.2.3</span></h3>'
              '<time datetime="2026-10-22T02:27:20+00:00">2 hours ago</time></a>'
              '<a href="/mod/3195/two" class="x"><h3><span>两</span></h3>'
              '<time datetime="2026-10-20T23:07:00+00:00"></time></a>')
    checks = []
    items = parse_page(sample)
    checks.append(("解析·两张卡片", len(items), 2))
    checks.append(("解析·标题与版本", items[0]["title"] + "/" + items[0]["version"], "Open Barters/1.2.3"))
    checks.append(("解析·取的是 datetime 而不是文字", items[0]["raw_time"], "2026-10-22T02:27:20+00:00"))
    checks.append(("解析·没有版本的只给标题", items[1]["version"], ""))
    checks.append(("解析·相对时间不认", parse_abs(items[0]["raw_time"]) is not None, True))
    checks.append(("解析·认得 2 hours ago 是相对串", parse_abs("2 hours ago"), None))

    seen = {"ids": ["3194"], "mods": {"3194": {"updated": "2026-10-22T02:27:20+00:00"}}}
    rows, _, _ = collect(items, seen, 24 * 365)
    checks.append(("判新增·只报没见过的那张", [r[1]["id"] for r in rows], ["3195"]))
    checks.append(("无变化·时间相同不报", len(collect(parse_page(sample), seen, 24 * 365)[0]), 1))
    later = parse_page(sample.replace("2026-10-22T02:27:20", "2026-10-23T02:27:20"))
    checks.append(("判更新·严格晚于才报", len(collect(later, seen, 24 * 365)[0]), 2))
    old_page = ('<a href="/mod/4001/old-a"><h3><span>旧甲</span></h3>'
                '<time datetime="2026-10-01T10:00:00+00:00"></time></a>'
                '<a href="/mod/4002/old-b"><h3><span>旧乙</span></h3>'
                '<time datetime="2026-10-02T10:00:00+00:00"></time></a>')
    old_cache = {"ids": ["4001", "4002"],
                 "mods": {"4001": {"updated": "2026-01-01T00:00:00+00:00"},
                          "4002": {"updated": "2026-01-01T00:00:00+00:00"}}}
    checks.append(("窗口·是更新但已在窗口外就不报", len(collect(parse_page(old_page), old_cache, 1)[0]), 0))
    checks.append(("窗口·窗口放宽到一年就能报出来", len(collect(parse_page(old_page), old_cache, 24 * 365)[0]), 2))

    # 真缓存里的历史形状：date 是 epoch 浮点、0.0 是占位（nexus 那边实测有 50 条这种）
    checks.append(("历史缓存·epoch浮点认得出", parse_abs(1788222600.0) is not None, True))
    checks.append(("历史缓存·0.0 当没有时间", parse_abs(0.0), None))
    checks.append(("历史缓存·None 不炸", parse_abs(None), None))
    epoch_page = sample.replace("2026-10-22T02:27:20", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    checks.append(("历史缓存·晚于epoch值就判更新",
                    len(collect(parse_page(epoch_page), {"ids": ["3194"], "mods": {"3194": {"date": 1788222600.0}}},
                                24 * 365)[0]) >= 1, True))

    html_path = os.path.join(DATA, "page.html")
    open(html_path, "w", encoding="utf-8").write(sample)
    checks.append(("退出码·本地HTML第一次是1",
                   scan(hours=24 * 365, max_pages=1, update=False, quiet=True, from_file=html_path)[0], EXIT_CHANGED))
    checks.append(("report 落了盘", os.path.exists(REPORT), True))
    scan(hours=24 * 365, max_pages=1, update=True, quiet=True, from_file=html_path)
    checks.append(("缓存写进去了", "3194" in (load_cache().get("mods") or {}), True))
    checks.append(("退出码·同一份再跑是0",
                   scan(hours=24 * 365, max_pages=1, update=False, quiet=True, from_file=html_path)[0], EXIT_NO_CHANGE))
    checks.append(("无变化时 report 被删掉", os.path.exists(REPORT), False))
    status = open(STATUS, encoding="utf-8").read()
    checks.append(("status 说清是缓存已含、不是没抓到", "抓到2 缓存已含2 窗口外0 新增0 更新0" in status, True))
    checks.append(("status 里不带卡片名（不往对话塞数据）", "Open Barters" in status, False))
    bad = os.path.join(DATA, "empty.html")
    open(bad, "w", encoding="utf-8").write("<html>没有卡片</html>")
    checks.append(("退出码·解析不出卡片是2",
                   scan(hours=24, max_pages=1, update=False, quiet=True, from_file=bad)[0], EXIT_NO_ITEMS))
    checks.append(("退出码·读不到文件是3",
                   scan(hours=24, max_pages=1, update=False, quiet=True,
                        from_file=os.path.join(DATA, "没这个文件.html"))[0], EXIT_FAILED))

    failed = [label for label, actual, want in checks if actual != want]
    if echo:
        for label, actual, want in checks:
            print(("PASS  " if actual == want else "FAIL  ") + label + ("" if actual == want else f" → {actual!r}≠{want!r}"))
        print(f"SELFTEST {'OK' if not failed else 'FAIL'}（{len(checks)} 项）")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
