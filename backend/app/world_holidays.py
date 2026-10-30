"""
Worldwide Public Holidays — data source for the "Worldwide Public Holidays"
sub-tab of Calendars > Calendars (frontend/src/pages/WorldHolidaysCalendar.jsx).

SOURCE
------
https://holidays-calendar.net (Traditional Chinese edition). One page per
region and year, e.g.

    https://holidays-calendar.net/calendar_zh_tw/hongkong_zh_tw.html        (current year)
    https://holidays-calendar.net/2027/calendar_zh_tw/hongkong_zh_tw.html   (other years)

The site has no API, so this module reads the public HTML pages server-side
(same "backend proxy" idea as fs_calendar.py) and turns each page's holiday
list (`.hol-item` rows) into plain per-day events for the calendar grid.

IMPORTANT BEHAVIOURS (all learned from the live pages, 2026-10)
---------------------------------------------------------------
* The page is NOT an official source. Its own footer points to each
  government's list and says the government list prevails. The route
  response carries `source_url` so the UI can link back to the page.
* A region/year page can exist but still hold a PLACEHOLDER: the China
  "2027" page currently shows the 2026 table under a title that still says
  2026. `_parse_page()` therefore checks that the year in the page title
  matches the requested year and reports `available=False` otherwise,
  rather than showing last year's dates as next year's.
* The site marks regions whose schedule is not yet confirmed with a
  `mitei` (Japanese "undecided") class in its country menu. That is passed
  through as `provisional=True`, but only for a year that has not started
  yet (the flag is the same on every page, even for already-settled years).
* Date cells come in several shapes: single day, range ("2月17日–19日"),
  floating rule ("1月第三個星期一"), approximate ("5月中旬左右"), half day
  ("12月24日（下午）") and substitute days after the main date
  ("4月5日<br>4月6日補假"). All are handled in `_parse_date_cell()`.
  A row whose date cannot be understood is returned in `unparsed` instead
  of being dropped silently.
* Only the date and holiday name are kept. The site's descriptive text is
  not copied (its footer says the content is copyright protected); the UI
  links to the source page for details.

POLITENESS
----------
Every (region, year) page is fetched at most once per 24 hours per ECS task
(TTLCache, single-flight), at most 4 pages at a time, with an identifying
User-Agent. Holiday lists change at most a few times a year.
"""
from __future__ import annotations

import calendar
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

SOURCE_HOST = "https://holidays-calendar.net"
USER_AGENT = "CloudInfraPortal-HolidayCalendar/1.0 (internal IT portal; contact: cloud infra team)"
_CACHE = TTLCache(ttl_seconds=24 * 60 * 60)
_MAX_PARALLEL_FETCHES = 4

# key -> (slug used by the site, Traditional Chinese name, English name, short code)
REGIONS: dict[str, dict] = {
    "hongkong":    {"slug": "hongkong",    "zh": "香港",     "en": "Hong Kong",   "code": "HK"},
    # The China page lists one row per festival with only the statutory date;
    # the extended leave blocks and make-up working days sit in the page's
    # description text, which this module deliberately does not copy.
    "china":       {"slug": "china",       "zh": "中國大陸", "en": "Mainland China", "code": "CN",
                    "caveat": "Shows the statutory holiday date only. The State Council's full leave blocks and "
                              "make-up working days are on the source page."},
    "macau":       {"slug": "macau",       "zh": "澳門",     "en": "Macau",       "code": "MO"},
    "taiwan":      {"slug": "taiwan",      "zh": "台灣",     "en": "Taiwan",      "code": "TW"},
    "japan":       {"slug": "japan",       "zh": "日本",     "en": "Japan",       "code": "JP"},
    "southkorea":  {"slug": "southkorea",  "zh": "韓國",     "en": "South Korea", "code": "KR"},
    "singapore":   {"slug": "singapore",   "zh": "新加坡",   "en": "Singapore",   "code": "SG"},
    "thailand":    {"slug": "thailand",    "zh": "泰國",     "en": "Thailand",    "code": "TH"},
    "vietnam":     {"slug": "vietnam",     "zh": "越南",     "en": "Vietnam",     "code": "VN"},
    "indonesia":   {"slug": "indonesia",   "zh": "印度尼西亞", "en": "Indonesia", "code": "ID"},
    "philippine":  {"slug": "philippine",  "zh": "菲律賓",   "en": "Philippines", "code": "PH"},
    "usa":         {"slug": "usa",         "zh": "美國",     "en": "United States", "code": "US"},
    "uk":          {"slug": "uk",          "zh": "英國",     "en": "United Kingdom", "code": "GB"},
}

# The site only publishes a small window of years. Requests outside it are
# rejected up front instead of being sent to the site as 404 probes.
MIN_YEAR = 2020
MAX_YEAR_AHEAD = 2  # current year + 2


class WorldHolidaysError(Exception):
    """`message` is safe to show to end users."""
    def __init__(self, message: str, status: int = 502):
        self.message = message
        self.status = status
        super().__init__(message)


# ── Date-cell parsing ────────────────────────────────────────────────────────

_NTH = {"第一": 1, "第二": 2, "第三": 3, "第四": 4, "第五": 5, "最後": -1, "最后": -1}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_RE_RANGE = re.compile(r"(\d{1,2})月\s*(\d{1,2})日\s*(?:[–—~～-]\s*(?:(\d{1,2})月\s*)?(\d{1,2})日)?")
# "1月第三個星期一", "5月最後一個星期一"
_RE_FLOATING = re.compile(r"(\d{1,2})月\s*(第[一二三四五]|最[後后])\s*(?:一?[個个])?\s*星期\s*([一二三四五六日天])")
# "5月中旬左右", "3月上中旬 (未確定)", "5月左右"
_RE_APPROX = re.compile(r"(\d{1,2})月\s*(?:(上中旬|上旬|中旬|下旬)\s*(?:左右)?|左右)")
_APPROX_DAY = {"上旬": 5, "上中旬": 10, None: 15, "中旬": 15, "下旬": 25}
_RE_SUB_DAY = re.compile(r"(?:(\d{1,2})月\s*)?(\d{1,2})日")


def _nth_weekday(year: int, month: int, nth: int, weekday: int) -> date:
    """nth (1-based) weekday of a month; nth == -1 means the last one."""
    if nth == -1:
        last = date(year, month, calendar.monthrange(year, month)[1])
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _cell_lines(cell) -> list[str]:
    for br in cell.find_all("br"):
        br.replace_with("\n")
    text = cell.get_text()
    return [re.sub(r"[ \u2002\u2003\u00a0]+", " ", ln).strip() for ln in text.split("\n") if ln.strip()]


def _parse_date_cell(cell, year: int) -> Optional[dict]:
    """Turns a `.hol-date` cell into
    {"days": [(date, kind)], "half_day": bool, "approximate": bool}
    or None when the shape is not understood."""
    lines = _cell_lines(cell)
    if not lines:
        return None
    first = lines[0]
    rest = " ".join(lines[1:]) if len(lines) > 1 else ""
    half_day = "下午" in first or "上午" in first
    approximate = False
    main_days: list[date] = []

    m = _RE_FLOATING.search(first)
    if m:
        month, nth, wd = int(m.group(1)), _NTH[m.group(2)], _WEEKDAY[m.group(3)]
        main_days = [_nth_weekday(year, month, nth, wd)]
    elif _RE_APPROX.search(first):
        am = _RE_APPROX.search(first)
        main_days = [date(year, int(am.group(1)), _APPROX_DAY[am.group(2)])]
        approximate = True
    else:
        m = _RE_RANGE.search(first)
        if not m:
            return None
        sm, sd = int(m.group(1)), int(m.group(2))
        start = _safe_date(year, sm, sd)
        if start is None:
            return None
        if m.group(4):
            em = int(m.group(3)) if m.group(3) else sm
            end = _safe_date(year, em, int(m.group(4)))
            if end is None or end < start:
                return None
            main_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        else:
            main_days = [start]
        # Anything after the main date on the same line (e.g. USA "7月4日 3日補假").
        rest = (first[m.end():] + " " + rest).strip()

    days: list[tuple[date, str]] = [(d, "holiday") for d in main_days]

    if "補假" in rest or "补假" in rest:
        clause = re.split(r"補假|补假", rest)[0]
        anchor = main_days[-1]
        for sm_s, sd_s in _RE_SUB_DAY.findall(clause):
            sub_day = int(sd_s)
            sub_month = int(sm_s) if sm_s else anchor.month
            sub_year = year
            if sm_s and sub_month < anchor.month - 6:
                sub_year = year + 1           # e.g. Dec 31 -> "1月2日補假"
            elif not sm_s and sub_day < anchor.day and (anchor.day - sub_day) > 15:
                sub_month = anchor.month + 1  # e.g. 30日 -> "1日補假" (next month)
                if sub_month > 12:
                    sub_month, sub_year = 1, year + 1
            sub = _safe_date(sub_year, sub_month, sub_day)
            if sub is not None and all(sub != d for d, _ in days):
                days.append((sub, "substitute"))

    return {"days": days, "half_day": half_day, "approximate": approximate}


def _clean_name(name_cell) -> tuple[str, str]:
    """Returns (Chinese name, English name or '')."""
    for junk in name_cell.select(".dp-mobile-none"):
        junk.decompose()
    en = ""
    en_el = name_cell.select_one(".en-holname")
    if en_el is not None:
        en = re.sub(r"\s+", " ", en_el.get_text(" ", strip=True))
        en_el.decompose()
    raw = name_cell.get_text(" ", strip=True)
    # UK rows put the English name after an em-space (U+2003) inside the same
    # cell. Only that separator is trusted: other names legitimately mix
    # scripts, e.g. Indonesia "共同假期 (cuti bersama)".
    if not en and "\u2003" in raw:
        left, right = raw.split("\u2003", 1)
        if re.search(r"[A-Za-z]{3}", right) and not re.search(r"[\u4e00-\u9fff]", right):
            raw, en = left, re.sub(r"\s+", " ", right).strip()
    zh = re.sub(r"[\s\u2002\u2003\u00a0]+", " ", raw).strip()
    return zh, en


# ── Page fetch + parse ───────────────────────────────────────────────────────

def _page_urls(slug: str, year: int) -> list[str]:
    return [
        f"{SOURCE_HOST}/{year}/calendar_zh_tw/{slug}_zh_tw.html",
        f"{SOURCE_HOST}/calendar_zh_tw/{slug}_zh_tw.html",
    ]


def _title_year(soup: BeautifulSoup) -> Optional[int]:
    el = soup.select_one(".page-title") or soup.find("title")
    m = re.search(r"(20\d{2})", el.get_text(" ", strip=True)) if el else None
    return int(m.group(1)) if m else None


def _parse_page(html: str, region_key: str, year: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    page_year = _title_year(soup)
    slug = REGIONS[region_key]["slug"]
    # The site flags the region in its menu on every page, including pages for
    # years that are long settled (China 2026), so it is only meaningful for a
    # year that has not started yet.
    flagged = any(
        (a.get("href") or "").startswith(f"{slug}_zh_tw")
        for a in soup.select("ul.side-menu-country li.mitei a")
    )
    provisional = flagged and year > date.today().year
    result = {
        "region": region_key,
        "year": year,
        "available": True,
        "provisional": provisional,
        "holidays": [],
        "unparsed": [],
    }
    if page_year != year:
        # Placeholder page (see module docstring) — never show another year's dates.
        result["available"] = False
        result["reason"] = "Not published yet for this year"
        return result

    for idx, item in enumerate(soup.select(".hol-item")):
        date_cell, name_cell = item.select_one(".hol-date"), item.select_one(".hol-name")
        if date_cell is None or name_cell is None:
            continue
        scope = [b.get_text(strip=True) for b in item.select('[class*="badge"]') if b.get_text(strip=True)]
        raw_date = re.sub(r"\s+", " ", date_cell.get_text(" ", strip=True))
        zh, en = _clean_name(name_cell)
        parsed = _parse_date_cell(date_cell, year)
        if parsed is None or not parsed["days"]:
            result["unparsed"].append({"name": zh, "date_text": raw_date})
            continue
        for d, kind in parsed["days"]:
            result["holidays"].append({
                "id": f"{region_key}-{year}-{idx}-{d.isoformat()}",
                "date": d.isoformat(),
                "name": zh,
                "en_name": en,
                "kind": kind,                       # "holiday" | "substitute"
                "half_day": parsed["half_day"],
                "approximate": parsed["approximate"],
                "scope": scope,                     # e.g. UK: only Scotland / only Northern Ireland
            })
    result["holidays"].sort(key=lambda h: (h["date"], h["kind"]))
    return result


def _fetch_region_year(region_key: str, year: int) -> dict:
    slug = REGIONS[region_key]["slug"]
    urls = _page_urls(slug, year)
    # Default answer when no candidate URL holds this year's page.
    unavailable = {
        "region": region_key, "year": year, "available": False, "provisional": False,
        "holidays": [], "unparsed": [], "reason": "Not published yet for this year",
        "source_url": urls[0],
    }
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for url in urls:
            try:
                resp = client.get(url)
            except httpx.HTTPError as e:
                logger.error("world_holidays: GET %s failed: %s", url, e)
                raise WorldHolidaysError("Could not reach the holidays source") from e
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                logger.error("world_holidays: GET %s -> %s", url, resp.status_code)
                raise WorldHolidaysError(f"Holidays source returned HTTP {resp.status_code}")
            resp.encoding = "utf-8"
            parsed = _parse_page(resp.text, region_key, year)
            parsed["source_url"] = url
            if not parsed["available"]:
                # Placeholder / other-year page: keep looking at the next candidate.
                unavailable["provisional"] = parsed["provisional"]
                continue
            if not parsed["holidays"] and not parsed["unparsed"]:
                logger.error("world_holidays: %s %s page parsed to zero holidays (layout change?)", region_key, year)
                raise WorldHolidaysError("Holidays source layout changed; no holidays could be read")
            return parsed
    return unavailable


def _region_year(region_key: str, year: int, force_refresh: bool) -> dict:
    return _CACHE.get_or_fetch(
        (region_key, year), lambda: _fetch_region_year(region_key, year), force_refresh=force_refresh,
    )


# ── Public API ───────────────────────────────────────────────────────────────

def list_regions() -> list[dict]:
    return [
        {"key": k, "code": v["code"], "en": v["en"], "zh": v["zh"], "caveat": v.get("caveat", "")}
        for k, v in REGIONS.items()
    ]


def _validate_year(year: int) -> None:
    if not (MIN_YEAR <= year <= date.today().year + MAX_YEAR_AHEAD):
        raise WorldHolidaysError("Year is outside the range the source publishes", status=400)


def get_holidays(year: int, region_keys: list[str], force_refresh: bool = False) -> dict:
    """Holidays for several regions in one year. A region that fails is
    reported in `errors` and does not stop the others."""
    _validate_year(year)
    keys = list(dict.fromkeys(region_keys))
    unknown = [k for k in keys if k not in REGIONS]
    if unknown:
        raise WorldHolidaysError(f"Unknown region: {', '.join(unknown)}", status=400)
    if not keys:
        raise WorldHolidaysError("Pick at least one region", status=400)

    regions: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def work(key: str):
        try:
            return key, _region_year(key, year, force_refresh), None
        except WorldHolidaysError as e:
            return key, None, e.message
        except Exception as e:  # parser bug on an unexpected layout — don't take down other regions
            logger.exception("world_holidays: unexpected error for %s %s: %s", key, year, e)
            return key, None, "Could not read holidays for this region"

    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_FETCHES, len(keys))) as pool:
        for key, data, err in pool.map(work, keys):
            if err:
                errors[key] = err
            else:
                regions[key] = data

    ages = [r.get("cache_age_seconds", 0) for r in regions.values()]
    return {
        "year": year,
        "regions": regions,
        "errors": errors,
        "cache_age_seconds": max(ages) if ages else 0,
        "source": {"name": "holidays-calendar.net", "url": SOURCE_HOST},
    }


def reset_cache_for_tests() -> None:
    _CACHE.reset_for_tests()
