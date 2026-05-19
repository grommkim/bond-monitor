#!/usr/bin/env python3
"""
채권 시장 모니터 — HTML 대시보드
평일 하루 2회(오전 9시·오후 3시) 자동 업데이트
"""

import json, os, re, sys
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("pip3 install requests 를 먼저 실행하세요.")

# ── 설정 ────────────────────────────────────────────────────────────
OUTPUT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kofia_cache.json")
LOOKBACK_DAYS = 30

KOFIA_URL = "https://www.kofiabond.or.kr/proframeWeb/XMLSERVICES/"
KOFIA_HDR = {
    "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0",
    "Content-Type": "text/xml; charset=utf-8",
    "Referer":      "https://www.kofiabond.or.kr/",
}

# 한전 포함 전체 공기업 목록
KEPCO = "한국전력공사"
OTHER_ORGS = [
    "한국도로공사","한국가스공사","한국수자원공사","한국토지주택공사",
    "인천국제공항공사","한국주택금융공사","한국장학재단","한국농어촌공사",
    "한국산업단지공단","한국자산관리공사","국가철도공단","부산항만공사",
]
ALL_ORGS = [KEPCO] + OTHER_ORGS

# KOFIA val키 → 개월수
VAL_MONTHS = {
    "val1":3,"val2":6,"val3":9,"val4":12,"val5":18,
    "val6":24,"val7":30,"val8":36,"val9":48,"val10":60,
    "val11":84,"val12":120,
}

# 민평금리 3사 (나이스피앤아이·한국자산평가·KIS자산평가)
THREE_COMPANIES = {"나이스피앤아이", "한국자산평가", "KIS자산평가"}

def avg3(vals: list):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 3) if len(v) >= 2 else (v[0] if v else None)

# ── 날짜 유틸 ────────────────────────────────────────────────────────

def last_biz(d=None):
    d = d or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def prev_biz(d=None):
    d = (d or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def biz_days_list(n=20):
    days, d = [], last_biz()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))

def issue_tenor_label(iss_dt: str, mat_dt: str) -> str:
    """발행일~만기일로 실제 발행 만기 계산 → '3년', '5년' 등"""
    if len(iss_dt) != 8 or len(mat_dt) != 8:
        return ""
    try:
        iss = date(int(iss_dt[:4]), int(iss_dt[4:6]), int(iss_dt[6:]))
        mat = date(int(mat_dt[:4]), int(mat_dt[4:6]), int(mat_dt[6:]))
    except ValueError:
        return ""
    total_m = (mat.year - iss.year) * 12 + (mat.month - iss.month)
    if mat.day < iss.day:
        total_m -= 1
    std = [3, 6, 12, 18, 24, 36, 48, 60, 84, 120]
    nearest = min(std, key=lambda x: abs(x - total_m))
    if abs(nearest - total_m) > 3:
        return f"{total_m // 12}년{total_m % 12}개월" if total_m >= 12 else f"{total_m}개월"
    if nearest < 12:
        return f"{nearest}개월"
    if nearest % 12 == 0:
        return f"{nearest // 12}년"
    return f"{nearest // 12}년{nearest % 12}개월"

def closest_val_key(remain_code: str):
    """잔존기간 코드(YYMMDD) → 가장 가까운 val키. 10년 초과 None."""
    if not remain_code or len(remain_code) < 4:
        return None
    try:
        yy, mm = int(remain_code[:2]), int(remain_code[2:4])
        total = yy * 12 + mm
    except ValueError:
        return None
    if total < 1 or total > 150:
        return None
    return min(VAL_MONTHS, key=lambda k: abs(VAL_MONTHS[k] - total))

# ── ProFrame 공통 ────────────────────────────────────────────────────

def kofia_post(svc, fn, fields: dict) -> str:
    dto_name = list(fields.keys())[0]
    inner    = "".join(f"  <{k}>{v}</{k}>\n" for k, v in fields[dto_name].items())
    xml = (f'<?xml version="1.0" encoding="utf-8"?>\n<message>\n'
           f'  <proframeHeader>\n'
           f'    <pfmAppName>BIS-KOFIABOND</pfmAppName>\n'
           f'    <pfmSvcName>{svc}</pfmSvcName>\n'
           f'    <pfmFnName>{fn}</pfmFnName>\n'
           f'  </proframeHeader>\n  <systemHeader></systemHeader>\n'
           f'  <{dto_name}>\n{inner}  </{dto_name}>\n</message>')
    try:
        r = requests.post(KOFIA_URL, data=xml.encode("utf-8"), headers=KOFIA_HDR, timeout=15)
        return r.content.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"    KOFIA 오류: {e}")
        return ""

def kofia_items(txt, dto="BISComDspDatDTO"):
    return re.findall(f"<{dto}>(.*?)</{dto}>", txt, re.DOTALL)

def pval(row, k):
    m = re.search(f"<{k}>(.*?)</{k}>", row)
    return m.group(1).strip() if m else ""

# ── 1. 국고채·한전채 민평금리 이력 ──────────────────────────────────

def fetch_ktb_day(yyyymmdd):
    """국고채 3사 평균 3년물 → {"국고채": float}"""
    txt = kofia_post("BISBndSrtPrcSrchSO", "selectDay",
                     {"BISBndSrtPrcDayDTO": {
                         "standardDt": yyyymmdd, "reportCompCd": "A10000",
                         "applyGbCd": "C02",
                         "val1":"Y","val2":"Y","val3":"Y","val4":"Y","val5":"Y",
                     }})
    company_vals = []
    for row in kofia_items(txt, "BISBndSrtPrcDayDTO"):
        if (pval(row,"largeCategoryMrk")=="국채" and
                pval(row,"typeNmMrk")=="국고채권" and
                pval(row,"koreanShotNm") in THREE_COMPANIES):
            v = pval(row, "val8")
            if v:
                try: company_vals.append(float(v))
                except ValueError: pass
    result = avg3(company_vals)
    return {"국고채": result} if result else {}

def fetch_org_minp(yyyymmdd):
    """공기업별 3사 평균 전 만기 → {org: {val_key: rate}}"""
    txt = kofia_post("BISBndSrtPrcSrchSO", "selectDay2",
                     {"BISBndSrtPrcDayDTO": {
                         "standardDt": yyyymmdd, "reportCompCd": "A10000",
                         "applyGbCd": "C02",
                         "val1":"Y","val2":"Y","val3":"Y","val4":"Y","val5":"Y",
                     }})
    # 3사별 값 수집
    raw = {}  # {org: {vk: [val, ...]}}
    for row in kofia_items(txt, "BISBndSrtPrcDayDTO"):
        org  = pval(row, "typeNmMrk")
        comp = pval(row, "koreanShotNm")
        if comp not in THREE_COMPANIES:
            continue
        for vk in VAL_MONTHS:
            v = pval(row, vk)
            if v:
                try:
                    raw.setdefault(org, {}).setdefault(vk, []).append(float(v))
                except ValueError:
                    pass
    # 3사 평균 계산
    result = {}
    for org, vk_map in raw.items():
        avgs = {vk: avg3(vals) for vk, vals in vk_map.items() if avg3(vals) is not None}
        if avgs:
            result[org] = avgs
    return result

def collect_kofia_history():
    print("[ KOFIA 국고채·한전채 민평금리 수집 ]")
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    biz_list  = biz_days_list(LOOKBACK_DAYS)
    need = [d for d in biz_list
            if not (cache.get(d.strftime("%Y-%m-%d"), {}).get("국고채")
                    and cache.get(d.strftime("%Y-%m-%d"), {}).get("한전채"))]

    fetched = 0
    for d in sorted(need, reverse=True):
        if fetched >= 7:
            break
        dk = d.strftime("%Y-%m-%d")
        ds = d.strftime("%Y%m%d")
        data = {}
        data.update(fetch_ktb_day(ds))
        org_data = fetch_org_minp(ds)
        kepco = org_data.get(KEPCO, {})
        if kepco.get("val8"):
            data["한전채"] = kepco["val8"]
        if data.get("국고채") and data.get("한전채"):
            cache[dk] = data
            print(f"  ✓ {dk}: 국고={data['국고채']}%, 한전={data['한전채']}%")
            fetched += 1

    cutoff = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    cache  = {k: v for k, v in cache.items() if k >= cutoff and v.get("국고채") and v.get("한전채")}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    chart  = {d.strftime("%Y-%m-%d"): cache[d.strftime("%Y-%m-%d")]
              for d in biz_list if cache.get(d.strftime("%Y-%m-%d"))}
    latest = {}
    for d in reversed(biz_list):
        dk = d.strftime("%Y-%m-%d")
        if cache.get(dk):
            latest = {**cache[dk], "날짜": dk}
            break
    return chart, latest

# ── 2. 발행실적 수집 (한전 포함 전체) ───────────────────────────────

def collect_issuance(days=7):
    print(f"[ KOFIA 발행실적 수집 (최근 {days}일, 한전 포함) ]")
    end_dt   = last_biz()
    start_dt = end_dt - timedelta(days=days + 5)

    txt = kofia_post("BISIssInfoSntcSrchSO", "list",
                     {"BISComDspDatDTO": {
                         "val1": "ISS",
                         "val2": start_dt.strftime("%Y%m%d"),
                         "val3": end_dt.strftime("%Y%m%d"),
                         "val4": "3",
                         "val5": "", "val6": "", "val7": "",
                     }})
    rows = kofia_items(txt, "BISComDspDatDTO")
    cnt  = re.search(r"<dbio_total_count_>(\d+)", txt)
    print(f"  특수채 전체 {cnt.group(1) if cnt else '?'}건 처리 중")

    result = []
    for row in rows:
        name   = pval(row, "val1")
        iss_dt = pval(row, "val3")
        mat_dt = pval(row, "val4")
        remain = pval(row, "val5")
        amount = pval(row, "val6")
        coupon = pval(row, "val9")

        # 한전 또는 기타 공기업 판별
        if KEPCO in name:
            org = KEPCO
        else:
            org = next((o for o in OTHER_ORGS if o in name), None)
        if not org:
            continue
        if "MBS" in name or "유동화" in name:
            continue

        tenor   = issue_tenor_label(iss_dt, mat_dt)
        iss_fmt = f"{iss_dt[:4]}-{iss_dt[4:6]}-{iss_dt[6:]}" if len(iss_dt)==8 else iss_dt
        mat_fmt = f"{mat_dt[:4]}-{mat_dt[4:6]}-{mat_dt[6:]}" if len(mat_dt)==8 else ""
        result.append({
            "org":      org,
            "is_kepco": org == KEPCO,
            "name":     name[:35],
            "date":     iss_fmt,
            "mat_dt":   mat_fmt,
            "tenor":    tenor,
            "remain":   remain,
            "amount":   f"{int(amount):,}" if amount.isdigit() else amount,
            "rate":     coupon,
            "minp":     None,
            "minp_date": "",
        })

    result.sort(key=lambda x: (-int(x["date"].replace("-", "")), x["org"]))

    # 민평금리 매핑 (발행일 전 영업일 기준)
    dates_needed = {}
    for r in result:
        try:
            iss_d = date.fromisoformat(r["date"])
        except ValueError:
            continue
        ld = prev_biz(iss_d)
        dates_needed.setdefault(ld.strftime("%Y%m%d"), ld.strftime("%m/%d"))

    minp_by_date = {}
    for ds, lbl in dates_needed.items():
        print(f"  민평금리 조회: {ds[:4]}-{ds[4:6]}-{ds[6:]}")
        minp_by_date[ds] = fetch_org_minp(ds)

    for r in result:
        try:
            iss_d = date.fromisoformat(r["date"])
        except ValueError:
            continue
        ld  = prev_biz(iss_d)
        ds  = ld.strftime("%Y%m%d")
        lbl = ld.strftime("%m/%d")
        org_minp = minp_by_date.get(ds, {}).get(r["org"], {})
        vk = closest_val_key(r["remain"])
        if vk and vk in org_minp:
            r["minp"]      = org_minp[vk]
            r["minp_date"] = lbl

    print(f"  → 발행실적 총 {len(result)}건 (한전 {sum(1 for r in result if r['is_kepco'])}건 포함)")
    return result

# ── 3. 차입금 현황 관리 ─────────────────────────────────────────────

DEBT_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debt_positions.json")
DEBT_HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debt_history.json")
EXCEL_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "업로드 파일(26.5.19).xlsx")
DEBT_BOND_CATS = {"전력채", "단기사채", "외화채권"}

def debt_classify(name):
    if name in {"전력채","단기사채","외화채권","중장기기업어음","은행차입"}:
        return name
    if name in {"나이지리아","남북협력기금","농어촌융자금"}:
        return "기타"
    return None

def init_debt_from_excel():
    try:
        import openpyxl
    except ImportError:
        print("  openpyxl 없음 — pip3 install openpyxl"); return []
    if not os.path.exists(EXCEL_FILE):
        print(f"  엑셀 파일 없음: {EXCEL_FILE}"); return []
    wb = openpyxl.load_workbook(EXCEL_FILE, data_only=True)
    ws = wb['raw']
    positions = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=3, max_row=ws.max_row, values_only=True), start=3):
        name = row[4]; bal = row[9]; cat = debt_classify(name)
        if not cat or not bal or bal <= 0: continue
        mat = row[16]; iss = row[15]; rate = row[8]
        def _d(v):
            if hasattr(v,"date"): return v.date().isoformat()
            return v.isoformat() if v else None
        mat_s, iss_s = _d(mat), _d(iss)
        uid = f"{cat}|{iss_s}|{mat_s}|{int(bal)}|r{row_idx}"
        positions.append({"id":uid,"category":cat,"amount":int(bal),
            "issuance_date":iss_s,"maturity_date":mat_s,
            "rate":float(rate) if isinstance(rate,(int,float)) and rate else None,
            "source":"excel"})
    print(f"  엑셀 초기화: {len(positions)}건 로드")
    return positions

def load_debt_positions():
    if os.path.exists(DEBT_FILE):
        with open(DEBT_FILE, encoding="utf-8") as f: return json.load(f)
    print("[ 차입금 초기화: 엑셀에서 로드 ]")
    positions = init_debt_from_excel()
    save_debt_positions(positions)
    return positions

def save_debt_positions(positions):
    with open(DEBT_FILE,"w",encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)

def get_debt_summary(positions):
    from collections import defaultdict
    by = defaultdict(int)
    for p in positions: by[p["category"]] += p["amount"]
    사채 = sum(by[c] for c in DEBT_BOND_CATS)
    사채외 = sum(v for c,v in by.items() if c not in DEBT_BOND_CATS)
    return {"전력채":by["전력채"],"단기사채":by["단기사채"],"외화채권":by["외화채권"],
            "중장기기업어음":by["중장기기업어음"],"은행차입":by["은행차입"],"기타":by["기타"],
            "사채":사채,"사채외":사채외,"합계":사채+사채외}

def _load_history():
    if not os.path.exists(DEBT_HISTORY): return {}
    with open(DEBT_HISTORY,encoding="utf-8") as f: return json.load(f)

def save_debt_snapshot(summary, date_str):
    h = _load_history(); h[date_str] = summary
    if len(h) > 90: del h[sorted(h)[0]]
    with open(DEBT_HISTORY,"w",encoding="utf-8") as f: json.dump(h,f,ensure_ascii=False,indent=2)

def get_snapshot(date_str): return _load_history().get(date_str)

def update_debt_pm(positions, issuances):
    today_s = last_biz().isoformat()
    before  = len(positions)
    positions = [p for p in positions if not p["maturity_date"] or p["maturity_date"] > today_s]
    if before-len(positions): print(f"  만기 차감: {before-len(positions)}건")
    existing_keys = {(p["category"], p.get("issuance_date",""), p.get("maturity_date","") or "", p["amount"])
                     for p in positions}
    added = 0
    for iss in issuances:
        if not iss.get("is_kepco"): continue
        try: amt = int(iss["amount"].replace(",","")) * 100_000_000
        except (ValueError,AttributeError): continue
        mat_s = iss.get("mat_dt","") or ""
        key   = ("전력채", iss["date"], mat_s, amt)
        if key in existing_keys: continue
        uid = f"전력채|{iss['date']}|{mat_s}|{amt}|kofia"
        positions.append({"id":uid,"category":"전력채","amount":amt,
            "issuance_date":iss["date"],"maturity_date":mat_s or None,
            "rate":float(iss["rate"]) if iss.get("rate") else None,"source":"kofia"})
        existing_keys.add(key); added += 1
    if added: print(f"  신규 전력채 추가: {added}건")
    return positions

def collect_debt(issuances):
    print("[ 차입금 현황 업데이트 ]")
    KST   = timezone(timedelta(hours=9))
    is_pm = datetime.now(KST).hour >= 15
    today = last_biz(); yesterday = prev_biz(today)
    positions = load_debt_positions()
    if is_pm:
        positions = update_debt_pm(positions, issuances)
        save_debt_positions(positions)
        summary = get_debt_summary(positions)
        save_debt_snapshot(summary, today.isoformat())
        as_of  = today.isoformat()
        prev_s = get_snapshot(yesterday.isoformat())
    else:
        as_of  = yesterday.isoformat()
        snap   = get_snapshot(yesterday.isoformat())
        summary = snap if snap else get_debt_summary(positions)
        prev_s  = get_snapshot(prev_biz(yesterday).isoformat())
    return summary, prev_s, as_of, is_pm


# ── 4. HTML 생성 ────────────────────────────────────────────────────

def build_chart_data(chart):
    dates  = sorted(chart.keys())
    labels = [d[5:].replace("-", "/") for d in dates]
    ds = [
        {"label":"국고채(3년) 민평","data":[chart[d]["국고채"] for d in dates],
         "type":"line","borderColor":"#2563eb","backgroundColor":"transparent",
         "borderWidth":2.5,"pointRadius":4,"pointHoverRadius":6,"tension":0.3,"yAxisID":"y","order":1},
        {"label":"한전채(3년) 민평","data":[chart[d]["한전채"] for d in dates],
         "type":"line","borderColor":"#f97316","backgroundColor":"transparent",
         "borderWidth":2.5,"pointRadius":4,"pointHoverRadius":6,"tension":0.3,"yAxisID":"y","order":2},
        {"label":"스프레드(한전-국고)","data":[round(chart[d]["한전채"]-chart[d]["국고채"],4) for d in dates],
         "type":"line","fill":"start","borderColor":"rgba(20,184,166,0.85)",
         "backgroundColor":"rgba(20,184,166,0.18)","borderWidth":1.5,
         "pointRadius":2,"pointHoverRadius":5,"tension":0.3,"yAxisID":"y2","order":3},
    ]
    return json.dumps(labels, ensure_ascii=False), json.dumps(ds, ensure_ascii=False)


def issue_row_html(r, show_minp=True):
    """발행실적 1행 HTML"""
    tenor = r["tenor"] or "–"
    # 만기 뱃지 색
    t = tenor
    if any(x in t for x in ["3년","2년6개월"]):
        bc = "badge-3y"
    elif any(x in t for x in ["5년","4년"]):
        bc = "badge-5y"
    else:
        bc = "badge-10y"

    amt_html  = f'{r["amount"]}억원'
    try:
        rate_html = f'{float(r["rate"]):.2f}%'
    except (ValueError, TypeError):
        rate_html = f'{r["rate"]}%' if r["rate"] else "–"

    if r["minp"] is not None:
        minp_html = (f'<span style="color:#0d9488;font-weight:600">{r["minp"]:.3f}%</span>'
                     f'<span style="font-size:.72rem;color:#94a3b8;margin-left:3px">({r["minp_date"]})</span>')
        try:
            diff = round(float(r["rate"]) - r["minp"], 3)
            if diff > 0:
                diff_html = f'<span style="color:#dc2626;font-weight:600">+{diff:.3f}%p</span>'
            elif diff < 0:
                diff_html = f'<span style="color:#2563eb;font-weight:600">{diff:.3f}%p</span>'
            else:
                diff_html = '<span style="color:#94a3b8">0.000%p</span>'
        except (ValueError, TypeError):
            diff_html = "–"
    else:
        minp_html = '<span style="color:#cbd5e1">–</span>'
        diff_html = '<span style="color:#cbd5e1">–</span>'

    return (f'<tr>'
            f'<td style="white-space:nowrap">{r["date"]}</td>'
            f'<td><strong>{r["org"]}</strong></td>'
            f'<td style="font-size:.81rem;color:#64748b;white-space:nowrap">{r["name"]}</td>'
            f'<td style="white-space:nowrap"><span class="badge {bc}">{tenor}</span></td>'
            f'<td style="white-space:nowrap">{amt_html}</td>'
            f'<td class="rate" style="white-space:nowrap">{rate_html}</td>'
            f'<td style="white-space:nowrap">{minp_html}</td>'
            f'<td style="white-space:nowrap">{diff_html}</td>'
            f'</tr>')


def today_section_html(issuances):
    """금일 발행현황 섹션 — 항상 오늘 날짜 표시"""
    today     = last_biz()
    yesterday = prev_biz(today)
    today_str = today.strftime("%Y-%m-%d")
    yest_str  = yesterday.strftime("%Y-%m-%d")
    section_date = today.strftime("%Y년 %m월 %d일")

    today_all = [r for r in issuances if r["date"] == today_str]
    has_today = len(today_all) > 0
    KST = timezone(timedelta(hours=9))
    is_afternoon = datetime.now(KST).hour >= 15

    if has_today:
        display_rows = today_all
        update_note  = ''
    elif is_afternoon:
        display_rows = []
        update_note  = ''
    else:
        display_rows = [r for r in issuances if r["date"] == yest_str]
        update_note  = '⏰ 금일 발행 데이터는 오후 3시 업데이트 예정 · 현재 전일({}) 기준'.format(yesterday.strftime("%m/%d"))

    kepco_rows  = [r for r in display_rows if r["is_kepco"]]
    others_rows = [r for r in display_rows if not r["is_kepco"]]

    def mini_table(rows, empty_msg):
        if not rows:
            return f'<p style="color:#94a3b8;font-size:.87rem;padding:14px 0">{empty_msg}</p>'
        thead = ('<thead><tr>'
                 '<th>발행일</th><th>발행기관</th><th>종목명</th>'
                 '<th>만기</th><th>발행금액</th><th>발행금리</th>'
                 '<th>전일 민평</th><th>발행-민평</th>'
                 '</tr></thead>')
        tbody = "\n".join(issue_row_html(r) for r in rows)
        return f'<div class="table-wrap"><table>{thead}<tbody>{tbody}</tbody></table></div>'

    kepco_html  = mini_table(kepco_rows,  "발행 없음")
    others_html = mini_table(others_rows, "발행 없음")

    note_html = f'<p style="font-size:.82rem;color:#64748b;margin-bottom:14px">{update_note}</p>' if update_note else ''
    return f"""
<section>
  <h2 style="border-left-color:#f97316">🔔 발행현황 ({section_date})</h2>
  {note_html}
  <div class="today-grid">
    <div>
      <h3 class="sub-h3" style="color:#f97316">⚡ 한국전력공사</h3>
      {kepco_html}
    </div>
    <div>
      <h3 class="sub-h3" style="color:#2563eb">🏛 타기관 공사채</h3>
      {others_html}
    </div>
  </div>
</section>"""


def recent_section_html(issuances):
    """최근 7일 전체 발행실적"""
    if not issuances:
        return '<tr><td colspan="8" style="text-align:center;color:#94a3b8;padding:20px">발행 내역 없음</td></tr>'
    return "\n".join(issue_row_html(r) for r in issuances)


def debt_section_html(summary, prev_s, as_of, is_pm):
    def 조(v): return f"{v/1e12:.2f}조원"
    def diff_td(key, light=False):
        if not prev_s: return '<td style="color:#94a3b8">–</td>'
        d = summary[key] - prev_s.get(key, summary[key])
        if d == 0: return f'<td style="color:{"rgba(255,255,255,.4)" if not light else "#94a3b8"}">±0</td>'
        color = ("#fca5a5" if not light else "#dc2626") if d > 0 else ("#93c5fd" if not light else "#2563eb")
        sign  = "+" if d > 0 else ""
        return f'<td style="color:{color};font-weight:600;white-space:nowrap">{sign}{d/1e12:.2f}조</td>'

    rows = (
        f'<tr style="background:#eef2ff"><td colspan="3" style="font-weight:700;color:#3730a3;font-size:.82rem;padding:7px 12px">📌 사채 (전력채 · 단기사채 · 외화채권)</td></tr>'
        f'<tr><td style="padding-left:18px;color:#475569">전력채</td><td>{조(summary["전력채"])}</td><td style="color:#94a3b8">–</td></tr>'
        f'<tr><td style="padding-left:18px;color:#475569">단기사채</td><td>{조(summary["단기사채"])}</td><td style="color:#94a3b8">–</td></tr>'
        f'<tr><td style="padding-left:18px;color:#475569">외화채권</td><td>{조(summary["외화채권"])}</td><td style="color:#94a3b8">–</td></tr>'
        f'<tr style="background:#f1f5f9;font-weight:700"><td>사채 소계</td><td>{조(summary["사채"])}</td>{diff_td("사채",True)}</tr>'
        f'<tr style="background:#f0fdf4"><td colspan="3" style="font-weight:700;color:#166534;font-size:.82rem;padding:7px 12px">📌 사채외 (중장기기업어음 · 은행차입 · 기타)</td></tr>'
        f'<tr><td style="padding-left:18px;color:#475569">중장기기업어음</td><td>{조(summary["중장기기업어음"])}</td><td style="color:#94a3b8">–</td></tr>'
        f'<tr><td style="padding-left:18px;color:#475569">은행차입</td><td>{조(summary["은행차입"])}</td><td style="color:#94a3b8">–</td></tr>'
        f'<tr style="background:#f1f5f9;font-weight:700"><td>사채외 소계</td><td>{조(summary["사채외"])}</td>{diff_td("사채외",True)}</tr>'
        f'<tr style="background:#0f2a4a;color:#fff"><td style="font-weight:700">총 차입금</td>'
        f'<td style="font-weight:700;font-size:1rem">{조(summary["합계"])}</td>{diff_td("합계")}</tr>'
    )
    note = "" if is_pm else "⏰ 전일 현황 기준 · 당일 발행 반영은 오후 3시 업데이트 예정"
    note_html = f'<p style="font-size:.82rem;color:#64748b;margin-bottom:14px">{note}</p>' if note else ""
    return f"""
<section>
  <h2 style="border-left-color:#6366f1">💰 차입금 현황 ({as_of} 기준)</h2>
  {note_html}
  <div class="table-wrap" style="max-width:480px">
    <table>
      <thead><tr><th>구분</th><th>잔액</th><th>전일비</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>"""


def generate_html(chart, latest, issuances, debt_summary=None, debt_prev=None, debt_as_of=None, debt_is_pm=False):
    today_str = date.today().strftime("%Y년 %m월 %d일")
    latest_dt = latest.get("날짜","–")
    ktb_v  = latest.get("국고채", 0)
    kep_v  = latest.get("한전채", 0)
    sprd_v = round(kep_v - ktb_v, 3) if ktb_v and kep_v else 0

    cards_html = ""
    for label, val, color, unit in [
        ("국고채(3년) 민평", ktb_v,  "#2563eb", "%"),
        ("한전채(3년) 민평", kep_v,  "#f97316", "%"),
        ("스프레드(한전-국고)", sprd_v, "#0d9488", "%p"),
    ]:
        cards_html += f"""
<div class="card" style="border-top:3px solid {color}">
  <div class="cn">{label}</div>
  <div class="cv" style="color:{color}">{val:.3f}<span class="pct">{unit}</span></div>
  <div class="cd">민평평균 · {latest_dt} 기준</div>
</div>"""

    labels_json, datasets_json = build_chart_data(chart)
    today_html  = today_section_html(issuances)
    recent_html = recent_section_html(issuances)
    debt_html   = debt_section_html(debt_summary, debt_prev, debt_as_of, debt_is_pm) if debt_summary else ""

    THEAD = ('<thead><tr>'
             '<th>발행일</th><th>발행기관</th><th>종목명</th>'
             '<th>만기</th><th>발행금액</th><th>발행금리</th>'
             '<th>전일 민평</th><th>발행-민평</th>'
             '</tr></thead>')

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>채권 시장 모니터 — {today_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Noto Sans KR',sans-serif;background:#f0f4f8;color:#1e293b;min-height:100vh}}
a{{color:#2563eb;text-decoration:none}}a:hover{{text-decoration:underline}}
header{{background:linear-gradient(135deg,#0f2a4a 0%,#1d4ed8 100%);color:#fff;padding:22px 32px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
header h1{{font-size:1.55rem;font-weight:700;letter-spacing:-.5px}}
.sub{{font-size:.83rem;opacity:.75}}
main{{max-width:1400px;margin:0 auto;padding:28px 20px}}
section{{margin-bottom:40px}}
h2{{font-size:.98rem;font-weight:700;color:#0f2a4a;border-left:4px solid #2563eb;padding-left:10px;margin-bottom:16px}}
.sub-h3{{font-size:.88rem;font-weight:600;margin-bottom:10px;padding-left:4px}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:4px}}
@media(max-width:640px){{.cards{{grid-template-columns:1fr}}}}
.card{{background:#fff;border-radius:14px;padding:22px 20px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.cn{{font-size:.78rem;color:#64748b;margin-bottom:8px;font-weight:500}}
.cv{{font-size:2rem;font-weight:700}}
.pct{{font-size:1.1rem;font-weight:400;margin-left:2px}}
.cd{{font-size:.72rem;color:#94a3b8;margin-top:6px}}
.today-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:900px){{.today-grid{{grid-template-columns:1fr}}}}
.chart-box{{background:#fff;border-radius:14px;padding:24px 20px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.note{{margin-top:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:.8rem;color:#475569;line-height:1.7}}
.table-wrap{{background:#fff;border-radius:12px;overflow-x:auto;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
thead tr{{background:#0f2a4a;color:#fff}}
thead th{{padding:11px 12px;font-weight:600;text-align:left;white-space:nowrap}}
tbody tr{{border-bottom:1px solid #f1f5f9;transition:background .12s}}
tbody tr:hover{{background:#f8fafc}}
tbody td{{padding:10px 12px;vertical-align:middle}}
td.rate{{font-family:'Courier New',monospace;font-size:.92rem;font-weight:700;color:#1d4ed8}}
.badge{{display:inline-block;padding:2px 9px;border-radius:20px;font-size:.73rem;font-weight:700}}
.badge-3y{{background:#dbeafe;color:#1d4ed8}}
.badge-5y{{background:#dcfce7;color:#15803d}}
.badge-10y{{background:#fce7f3;color:#be185d}}
footer{{text-align:center;padding:22px;font-size:.78rem;color:#94a3b8;line-height:1.9}}
</style>
</head>
<body>
<header>
  <div><h1>📊 채권 시장 모니터</h1>
  <div class="sub">국고채 · 한전채 민평금리 · 공사채 발행현황</div></div>
  <div class="sub">기준일 <strong style="color:#fff">{today_str}</strong></div>
</header>
<main>

<section>
  <h2>직전 영업일 금리 요약</h2>
  <div class="cards">{cards_html}</div>
</section>

{today_html}

{debt_html}

<section>
  <h2>국고채 3년 · 한전채 3년 · 스프레드 추이 (최근 약 4주, 민평 기준)</h2>
  <div class="chart-box">
    <canvas id="yieldChart" style="max-height:430px"></canvas>
  </div>
  <div class="note">
    📌 KOFIA 채권평가 <strong>3사 평균</strong>(나이스피앤아이·한국자산평가·KIS자산평가) 기준 · 영업일만 표시 ·
    <a href="https://www.kofiabond.or.kr" target="_blank" rel="noopener">kofiabond.or.kr</a> 제공
  </div>
</section>

<section>
  <h2>최근 7일 공사채 발행실적 (한전 포함)</h2>
  <div class="table-wrap">
    <table>
      {THEAD}
      <tbody>{recent_html}</tbody>
    </table>
  </div>
  <div class="note">
    📌 전일 민평: 발행일 기준 전 영업일 KOFIA 3사 평균(나이스·한국자산평가·KIS) · 만기는 발행 시점 기준 ·
    <a href="https://www.kofiabond.or.kr" target="_blank">kofiabond.or.kr → 발행시장 → 발행정보종합</a>
  </div>
</section>

</main>
<footer>
  데이터: <a href="https://www.kofiabond.or.kr" target="_blank">금융투자협회 KOFIA</a> ·
  평일 오전 9시·오후 3시 자동 업데이트 · 마지막 생성: {today_str}
</footer>

<script>
const labels   = {labels_json};
const datasets = {datasets_json};
new Chart(document.getElementById('yieldChart'), {{
  type:'line', data:{{labels,datasets}},
  options:{{
    responsive:true,
    interaction:{{mode:'index',intersect:false}},
    plugins:{{
      legend:{{position:'top',labels:{{font:{{family:"'Noto Sans KR',sans-serif",size:12}},usePointStyle:true,padding:22}}}},
      tooltip:{{callbacks:{{label:ctx=>` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toFixed(3)}}${{ctx.datasetIndex===2?'%p':'%'}}`}}}}
    }},
    scales:{{
      x:{{type:'category',grid:{{color:'#f1f5f9'}},ticks:{{font:{{family:"'Noto Sans KR'",size:11}},maxRotation:0,autoSkip:true,maxTicksLimit:15}}}},
      y:{{position:'left',min:3.0,title:{{display:true,text:'금리 (%)',font:{{size:11}},color:'#64748b'}},grid:{{color:'#f1f5f9'}},ticks:{{font:{{family:"'Noto Sans KR'",size:11}},callback:v=>v.toFixed(2)+'%'}}}},
      y2:{{position:'right',min:0.250,title:{{display:true,text:'스프레드 (%p)',font:{{size:11}},color:'#0d9488'}},ticks:{{stepSize:0.05,font:{{family:"'Noto Sans KR'",size:11}},color:'#0d9488',callback:v=>v.toFixed(3)+'%p'}},grid:{{drawOnChartArea:false}}}}
    }}
  }}
}});
</script>
</body>
</html>"""


# ── 메인 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print(f" 채권 시장 모니터 — {date.today()}")
    print("=" * 52)

    chart, latest = collect_kofia_history()
    issuances     = collect_issuance(days=7)
    d_sum, d_prev, d_as_of, d_pm = collect_debt(issuances)

    html = generate_html(chart, latest, issuances,
                         debt_summary=d_sum, debt_prev=d_prev,
                         debt_as_of=d_as_of, debt_is_pm=d_pm)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅  {OUTPUT} 생성 완료")
    cwd = os.path.dirname(os.path.abspath(__file__))
    print(f"   자동 실행: 0 0,6 * * 1-5 cd {cwd} && python3 update.py >> cron.log 2>&1")
