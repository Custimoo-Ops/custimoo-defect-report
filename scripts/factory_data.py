#!/usr/bin/env python3
"""Generate factory-level failure report data. Can be run standalone or imported."""

import os, json, re, urllib.request
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timezone
import remake_backend_actions

REPORT_START = '2025-10-01'
_now = datetime.now(timezone.utc)
REPORT_END = (datetime(_now.year + (1 if _now.month == 12 else 0), 1 if _now.month == 12 else _now.month + 1, 1, tzinfo=timezone.utc)).strftime('%Y-%m-%d')

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj)
        return super().default(obj)

EXCLUDED_FACTORIES = {"Augusta De Mexico"}

def is_excluded_factory(name):
    return norm_factory(name) in EXCLUDED_FACTORIES

def norm_factory(name):
    if not name: return "(unknown)"
    n = name.strip().lower()
    if "mavic" in n: return "Mavic Sports"
    if "selberian" in n or "seleberian" in n: return "Selberian Sports Wear"
    if "silver" in n or "star" in n: return "Silver-Star Group"
    if "karrizo" in n or "karizzo" in n: return "Karrizo"
    if "rajco" in n: return "Rajco"
    if "zurdox" in n: return "Zurdox Factory"
    if "rival" in n: return "Rival Kit"
    if "sportena" in n: return "Sportena"
    if "dksportswear" in n: return "dksportswear"
    if "hummel" in n: return "Hummel"
    if "custimoo" in n: return "Custimoo factory"
    if "augusta" in n: return "Augusta De Mexico"
    return name.strip()[:30]

MANUAL_EXACT = {
    "22643": 30, "24722": 143, "24516": 20, "23613": 10,
    "22007": 2, "24753": 1, "23939": 13, "23553": 44,
    "20153": 12, "22541": 3, "20126": 2, "22341": 2,
    "24728": 202, "23055": 165, "22699": 166, "18913": 135,
    "21708": 521, "22788": 304, "21163": 388, "19345": 264,
    "22412": 269, "22595": 150, "21599": 162, "24059": 134,
    "21685": 200, "23010": 3, "23375": 1, "23427": 50,
    "23514": 31, "23690": 15, "23719": 100, "23761": 12,
    "23762": 13, "24096": 50, "24262": 50, "19810": 2,
    "20234": 21, "21545": 10, "19636": 2, "20416": 18,
    "20371": 18, "20985": 2, "19711": 10, "20055": 1,
    "20123": 1, "21247": 10, "22972": 2, "22983": 1,
    "19192": 63, "21019": 1, "21254": 1, "21575": 33,
    "20044": 4, "20506": 25, "20617": 24, "20919": 5,
    "21288": 30, "21560": 12, "21598": 12, "21704": 6,
    "22019": 10, "22886": 16, "20243": 3, "20694": 1,
    "20893": 50,
    # June 2026 new defects
    "25460": 3,   # 2 jerseys wrong number, 1 missing size — Jun 8
    # Updated affected quantities (Lars, Jun 16)
    "22412": 53,  # was 269 — actual affected per latest review
    "24728": 100, # was 202 — socks remake, actual affected
    "24262": 1,   # was 50 — actual affected per latest review
    "23005": 21,  # missing — 21 shirts, Silver-Star, branding+logo issues, Apr 16
}

MANUAL_ZERO = {
    "24040": 0, "23298": 0, "19937": 0, "20611": 0, "21274": 0,
    "21365": 0, "22220": 0, "22394": 0, "22874": 0, "23113": 0,
    "22906": 0, "19005": 0, "20988": 0, "22513": 0, "22558": 0,
    "22585": 0, "22608": 0, "23311": 0, "23955": 0, "21988": 0,
    "21545": 0, "24976": 0, "23650": 0, "24802": 0, "23834": 0,
    "17293": 0, "22784": 0, "23309": 0, "20889": 0, "21879": 0,
    "22099": 0, "22612": 0, "22769": 0, "21183": 0, "23646": 0,
    # Delays (not production defects)
    "22475": 0, "24555": 0,
}

MANUAL = {**MANUAL_EXACT, **MANUAL_ZERO}

NON_DEFECT_KEYWORDS = (
    'delay', 'delayed', 'late', 'not shipped', 'ship the next day', 'shipping delay',
    'samples have been quite delayed', 'sample delay', 'communication', 'update missed'
)
DEFECT_KEYWORDS = (
    'wrong', 'missing', 'defect', 'defective', 'incorrect', 'broken', 'damaged',
    'remake', 'rejected', 'quality', 'size', 'number', 'logo', 'branding'
)

def is_non_defect_fu(mentions):
    """Return True for FU emails that are operational/delay reports, not physical defects."""
    text = ' '.join((m.get('subject', '') + ' ' + m.get('body', '')) for m in mentions).lower()
    has_non_defect = any(k in text for k in NON_DEFECT_KEYWORDS)
    has_defect = any(k in text for k in DEFECT_KEYWORDS)
    # If it is explicitly a delay/process report and has no concrete product defect wording, do not count it.
    return has_non_defect and not has_defect

# Factory overrides — for multi-factory orders, specify which factory the defect belongs to
# Default is the first matching factory from order_items
MANUAL_FACTORY = {
    "24728": "Mavic Sports",  # socks made by Mavic, shirts by Rajco — only socks defective
}

def generate(defects_only=False):
    """Generate factory defect data. Returns dict."""
    import pymysql
    pw = os.environ.get("CUSTIMOO_DB_PASSWORD", "")
    conn = pymysql.connect(host="127.0.0.1", port=3307, database="custimoo_backend_prod", user="custimoo_backend_usr", password=pw, connect_timeout=10)
    cur = conn.cursor()

    # Fetch fu messages (optional — falls back to manual data)
    try:
        tenant = os.environ["CUSTIMOO_GRAPH_TENANT_ID"]
        client_id = os.environ["CUSTIMOO_GRAPH_CLIENT_ID"]
        client_secret = os.environ["CUSTIMOO_GRAPH_CLIENT_SECRET"]
        url = "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % tenant
        body = "client_id=%s&client_secret=%s&scope=https://graph.microsoft.com/.default&grant_type=client_credentials" % (client_id, client_secret)
        req = urllib.request.Request(url, data=body.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = json.loads(urllib.request.urlopen(req).read().decode())["access_token"]
    except Exception as e:
        print("Graph API unavailable (using manual defects only):", e)
        token = None

    all_msgs = []
    if token:
        url = "https://graph.microsoft.com/v1.0/users/fu@custimoo.com/messages?$filter=receivedDateTime%20ge%202025-10-28T00:00:00Z&$top=100&$select=subject,receivedDateTime,body&$orderby=receivedDateTime%20asc"
        while url:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Prefer": "outlook.body-content-type=text"})
            resp = json.loads(urllib.request.urlopen(req).read().decode())
            all_msgs.extend(resp.get("value", []))
            url = resp.get("@odata.nextLink")
    else:
        print("Skipping Microsoft Graph — no token available")

    all_order_nums = set()
    all_msgs_formatted = []
    # Track the first fu email date for each order
    first_fu_month = {}
    for m in all_msgs:
        subj = m["subject"]
        content = m.get("body", {}).get("content", "")[:2000]
        received_dt = m["receivedDateTime"][:7]
        nums_text = re.sub(r"https?://\S+", " ", subj + " " + content)
        nums = re.findall(r"(?:order|Order|ORDER)?\s*#?\s*(\d{4,6})", nums_text)
        for n in set(n for n in nums if 10000 <= int(n) <= 99999):
            all_order_nums.add(n)
            all_msgs_formatted.append({"ono": n, "subject": subj, "body": content})
            # Track first time this order appears in fu
            if n not in first_fu_month:
                first_fu_month[n] = received_dt

    order_nums_list = list(all_order_nums)
    if order_nums_list:
        qs = ",".join(["%s"] * len(order_nums_list))

        cur.execute("SELECT o.order_no, o.created_at, COALESCE(oi.factory_name, '(unknown)') as raw_factory, CAST(JSON_EXTRACT(o.price_info, '$.total_quantity') AS SIGNED) as qty FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id WHERE o.order_no IN (%s)" % qs, order_nums_list)
        rows = cur.fetchall()
    else:
        rows = []

    order_map = {}
    for r in rows:
        ono = str(r[0])
        month = str(r[1])[:7] if r[1] else "?"
        order_map[ono] = {'month': month, 'factory': norm_factory(r[2]), 'qty': r[3] or 0}

    factory_defects = defaultdict(int)
    factory_orders_set = defaultdict(set)
    monthly_factory_defects = defaultdict(lambda: defaultdict(int))
    monthly_factory_defect_orders = defaultdict(lambda: defaultdict(set))

    # Use the full extractor logic from report.py (manual + extracted + estimate)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from report import extract_qty, classify_product

    for ono, o in order_map.items():
        f = o['factory']
        # Use fu complaint date, not order creation date
        fu_month = first_fu_month.get(ono, o['month'])

        if ono in MANUAL:
            affected = MANUAL[ono]
            if affected == 0:
                continue
            # Check for factory override (multi-factory orders)
            if ono in MANUAL_FACTORY:
                f = MANUAL_FACTORY[ono]
        else:
            mentions = [m for m in all_msgs_formatted if m.get("ono") == ono]
            if is_non_defect_fu(mentions):
                continue
            affected = extract_qty(mentions)
            if affected is None:
                continue
            if o.get('qty') and affected > o.get('qty'):
                continue
            _ptype, is_product = classify_product(ono, conn)
            if not is_product:
                continue
        if f in EXCLUDED_FACTORIES:
            continue

        factory_defects[f] += affected
        factory_orders_set[f].add(ono)
        monthly_factory_defects[f][fu_month] += affected
        monthly_factory_defect_orders[f][fu_month].add(ono)

    cur.execute("""
SELECT oi.status_updated_at AS shipping_date,
       COALESCE(oi.factory_name, '(unknown)') as raw_factory,
       CAST(JSON_EXTRACT(o.price_info, '$.total_quantity') AS SIGNED) as qty,
       o.order_no,
       o.order_type_symbol
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE oi.status_updated_at >= %s
  AND oi.status_updated_at < %s
  AND (oi.status IN ('shipped','completed') OR oi.shipping_status IS NOT NULL)
""", (REPORT_START, REPORT_END))

    factory_month_pipe = defaultdict(lambda: defaultdict(lambda: {'qty':0, 'orders':0, 'remake_qty':0, 'remake_orders':0}))
    seen_order_factories = set()
    seen_remake_order_factories = set()
    for r in cur.fetchall():
        month = str(r[0])[:7] if r[0] else "?"
        f = norm_factory(r[1])
        if f in EXCLUDED_FACTORIES:
            continue
        qty = r[2] or 0
        ono = str(r[3])
        order_type_symbol = r[4]
        factory_month_pipe[f][month]['qty'] += qty
        # Count each order once per factory per month
        key = (f, month, ono)
        if key not in seen_order_factories:
            seen_order_factories.add(key)
            factory_month_pipe[f][month]['orders'] += 1
        if order_type_symbol == 'R' and not remake_backend_actions.is_excluded_remake(ono):
            factory_month_pipe[f][month]['remake_qty'] += qty
            if key not in seen_remake_order_factories:
                seen_remake_order_factories.add(key)
                factory_month_pipe[f][month]['remake_orders'] += 1

    # Totals are derived from non-excluded factory rows so excluded factories (e.g. Augusta) are removed everywhere.
    total_monthly_pipe = defaultdict(int)
    total_monthly_orders = defaultdict(int)
    for f, month_map in factory_month_pipe.items():
        if f in EXCLUDED_FACTORIES:
            continue
        for month, vals in month_map.items():
            total_monthly_pipe[month] += vals.get('qty', 0)
            total_monthly_orders[month] += vals.get('orders', 0)

    # Remake (no invoice) counts by shipping month, with backend-action exclusions applied.
    remake_by_month = {}
    cur2 = conn.cursor()
    excluded_remake_orders = sorted(remake_backend_actions.EXCLUDED_REMAKE_ORDERS)
    exclude_sql = ''
    params = [REPORT_START, REPORT_END]
    if excluded_remake_orders:
        exclude_sql = ' AND o.order_no NOT IN (' + ','.join(['%s'] * len(excluded_remake_orders)) + ')'
        params.extend(excluded_remake_orders)
    cur2.execute("""
SELECT DATE_FORMAT(oi.status_updated_at, '%%Y-%%m') as month,
       COUNT(DISTINCT o.id) as cnt,
       COALESCE(SUM(CAST(JSON_EXTRACT(o.price_info, '$.total_quantity') AS SIGNED)), 0) as qty
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE o.order_type_symbol = 'R'
  AND oi.status_updated_at >= %s
  AND oi.status_updated_at < %s
  AND (oi.status IN ('shipped','completed') OR oi.shipping_status IS NOT NULL)
""" + exclude_sql + """
GROUP BY month
ORDER BY month
""", params)
    for r in cur2.fetchall():
        month = r[0]
        remake_by_month[month] = {'orders': r[1], 'qty': r[2]}
    cur2.close()

    monthly_defects = defaultdict(int)
    defect_order_count = defaultdict(int)  # unique orders with defects per month
    seen_defect_orders_per_month = defaultdict(set)
    for ono, qty in MANUAL_EXACT.items():
        o = order_map.get(ono)
        if o and o.get('factory') in EXCLUDED_FACTORIES:
            continue
        fu_month = first_fu_month.get(ono, o['month'] if o else '?')
        if fu_month in total_monthly_pipe:
            monthly_defects[fu_month] += qty
            defect_order_count[fu_month] += 1  # count unique orders per month

    factory_stats = []
    for f in sorted(factory_defects.keys(), key=lambda x: -factory_defects[x]):
        if factory_defects[f] == 0: continue
        if f in EXCLUDED_FACTORIES: continue
        if f == "(unknown)": continue  # skip unassigned factory
        total_vol = sum(factory_month_pipe[f][m]['qty'] for m in factory_month_pipe[f])
        total_ords = sum(factory_month_pipe[f][m]['orders'] for m in factory_month_pipe[f])
        total_remake_ords = sum(factory_month_pipe[f][m].get('remake_orders', 0) for m in factory_month_pipe[f])
        total_remake_qty = sum(factory_month_pipe[f][m].get('remake_qty', 0) for m in factory_month_pipe[f])
        rate = (factory_defects[f] / total_vol * 100) if total_vol > 0 else 0
        order_rate = (len(factory_orders_set[f]) / total_ords * 100) if total_ords > 0 else 0
        factory_stats.append({
            'name': f,
            'volume': total_vol,
            'orders': total_ords,
            'defects': factory_defects[f],
            'defect_orders': len(factory_orders_set[f]),
            'remake_orders': total_remake_ords,
            'remake_qty': total_remake_qty,
            'rate': round(rate, 2),
            'order_rate': round(order_rate, 2),
        })

    factory_stats.sort(key=lambda x: -x['rate'])
    all_months = sorted(total_monthly_pipe.keys())

    factory_monthly_data = []
    for f in ['Mavic Sports', 'Selberian Sports Wear', 'Silver-Star Group', 'Karrizo', 'Rajco']:
        fdata = {'name': f, 'defects': [], 'volumes': [], 'orders': [], 'defect_orders': [], 'remake_orders': [], 'remake_qty': []}
        for m in all_months:
            d = monthly_factory_defects[f].get(m, 0)
            pv = factory_month_pipe[f].get(m, {}).get('qty', 0)
            po = factory_month_pipe[f].get(m, {}).get('orders', 0)
            ro = factory_month_pipe[f].get(m, {}).get('remake_orders', 0)
            rq = factory_month_pipe[f].get(m, {}).get('remake_qty', 0)
            do = len(monthly_factory_defect_orders[f].get(m, set()))
            fdata['defects'].append(d)
            fdata['volumes'].append(pv)
            fdata['orders'].append(po)
            fdata['defect_orders'].append(do)
            fdata['remake_orders'].append(ro)
            fdata['remake_qty'].append(rq)
        factory_monthly_data.append(fdata)

    conn.close()
    return {
        'months': all_months,
        'total_monthly': {m: {'qty': total_monthly_pipe.get(m, 0), 'orders': total_monthly_orders.get(m, 0)} for m in all_months},
        'monthly_defects': {m: monthly_defects.get(m, 0) for m in all_months},
        'defect_order_count': dict(defect_order_count),
        'remake_by_month': remake_by_month,
        'factories': factory_stats,
        'factory_monthly': factory_monthly_data,
    }

if __name__ == "__main__":
    data = generate()
    print(json.dumps(data, cls=DecimalEncoder))
