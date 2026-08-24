#!/usr/bin/env python3
"""Generate interactive HTML Defect Report with drill-down."""
import sys, os, json
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timezone
import csv, gzip, io, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factory_data
import remake_backend_actions
import db

# Generate summary data
try:
    FACTORY_SHARE_TOKENS = json.loads(os.environ.get('FACTORY_SHARE_TOKENS', '{}'))
except Exception:
    FACTORY_SHARE_TOKENS = {}
FACTORY_SHARE_BUILD = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
FACTORY_SHARE_LINKS_JSON = json.dumps({slug: '/factory/' + slug + '?token=' + str(token) + '&v=' + FACTORY_SHARE_BUILD for slug, token in FACTORY_SHARE_TOKENS.items()})
data = factory_data.generate()
QARMA_SCOPE = {tuple(x) for x in data.get('qarma_scope', [])}
hummel_account_data = factory_data.generate(customer_company='Hummel Pro NA')

months = data['months']
total_monthly = {m: {'qty': v['qty'], 'orders': v.get('orders', 0)} for m, v in data['total_monthly'].items()}
monthly_defects = data['monthly_defects']
defect_order_count = data.get('defect_order_count', {})
remake_by_month = data.get('remake_by_month', {})
factories = data['factories']
factory_monthly_data = data['factory_monthly']


def norm_qarma_supplier(name):
    n = (name or '').strip()
    low = n.lower()
    if 'mavic' in low:
        return 'Mavic Sports'
    if 'silver' in low:
        return 'Silver-Star Group'
    if 'selber' in low or 'seleber' in low:
        return 'Selberian Sports Wear'
    if 'karrizo' in low or 'karizzo' in low:
        return 'Karrizo'
    if 'rajco' in low:
        return 'Rajco'
    if 'custimoo' in low:
        return 'Custimoo factory'
    if 'augusta' in low:
        return 'Augusta De Mexico'
    return n or '(unknown)'

QARMA_STATS_CACHE = {}
QARMA_ORDER_STATS_CACHE = {}
QARMA_SOURCE_URL = os.environ.get('QARMA_INSPECTIONS_URL', '')
QARMA_SOURCE_META = {'ok': False, 'rows': 0, 'filtered_rows': 0, 'source': '[REDACTED]', 'error': ''}
QARMA_ROWS_CACHE = None

def dt_to_month(v):
    import datetime
    if isinstance(v, datetime.datetime):
        return v.strftime('%Y-%m')
    if isinstance(v, datetime.date):
        return v.strftime('%Y-%m')
    if v:
        return str(v)[:7]
    return None

def safe_int(v):
    try:
        if v is None or v == '':
            return 0
        return int(float(str(v).replace(',', '').strip()))
    except Exception:
        return 0

def is_qarma_final_candidate(row):
    return (
        row.get('Status') == 'Report'
        and str(row.get('Inspection type') or '').strip() == 'Final'
        and str(row.get('Conclusion') or '').strip() in ('Approved', 'Rejected')
        and str(row.get('Supplier qc') or '').strip().lower() != 'true'
        and str(row.get('Inspector email') or '').strip().lower().endswith('@custimoo.com')
    )

def is_qarma_included(row):
    return is_qarma_final_candidate(row) and not str(row.get('Reinspection of') or '').strip()

def load_qarma_rows():
    """Read Qarma's internal direct CSV.GZ export. No API key; refreshes roughly hourly."""
    global QARMA_ROWS_CACHE, QARMA_SOURCE_META
    if QARMA_ROWS_CACHE is not None:
        return QARMA_ROWS_CACHE
    try:
        req = urllib.request.Request(QARMA_SOURCE_URL, headers={'Accept': 'text/csv,application/gzip,*/*', 'User-Agent': 'custimoo-defect-report/1.0'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            text = io.TextIOWrapper(gz, encoding='utf-8-sig', newline='')
            rows = list(csv.DictReader(text))
        QARMA_SOURCE_META = {'ok': True, 'rows': len(rows), 'filtered_rows': 0, 'source': '[REDACTED]', 'error': ''}
        QARMA_ROWS_CACHE = rows
        return rows
    except Exception as e:
        QARMA_SOURCE_META = {'ok': False, 'rows': 0, 'filtered_rows': 0, 'source': '[REDACTED]', 'error': str(e)[:200]}
        QARMA_ROWS_CACHE = []
        print('Warning: could not load Qarma CSV:', e)
        return []

def iter_qarma_rows(month_filter=None):
    months_allowed = set(month_filter) if month_filter else set(months)
    filtered = 0
    for row in load_qarma_rows():
        if not is_qarma_included(row):
            continue
        month = dt_to_month(row.get('Scheduled inspection date') or row.get('Inspection end time'))
        if month not in months_allowed:
            continue
        filtered += 1
        yield row, month
    if not month_filter:
        QARMA_SOURCE_META['filtered_rows'] = filtered

def load_qarma_stats(month_filter=None):
    """Aggregate Qarma physical QC by supplier for the report window.
    Sample qty is deduped by Report inspection id; defect pieces are summed from minor/major/critical pieces affected.
    """
    cache_key = tuple(month_filter) if month_filter else tuple(months)
    if cache_key in QARMA_STATS_CACHE:
        return QARMA_STATS_CACHE[cache_key]
    stats = defaultdict(lambda: {'sample_qty': 0, 'defects': 0, 'reports': set(), 'orders': set(), 'reinspection_orders': set(), 'rejected_orders': set()})
    months_allowed = set(month_filter) if month_filter else set(months)
    for raw_row in load_qarma_rows():
        if not is_qarma_final_candidate(raw_row) or not str(raw_row.get('Reinspection of') or '').strip():
            continue
        raw_month = dt_to_month(raw_row.get('Scheduled inspection date') or raw_row.get('Inspection end time'))
        raw_order = str(raw_row.get('Order number') or '').strip()
        if raw_month in months_allowed and raw_order:
            stats[norm_qarma_supplier(raw_row.get('Supplier name'))]['reinspection_orders'].add(raw_order)
    seen_report_sample = set()
    for row, month in iter_qarma_rows(month_filter):
        f = norm_qarma_supplier(row.get('Supplier name'))
        report_id = str(row.get('Report inspection id') or row.get('Inspection id') or '')
        order_no = str(row.get('Order number') or '').strip()
        sample_qty = safe_int(row.get('Actual sample quantity'))
        defects = safe_int(row.get('Minor defects pieces affected')) + safe_int(row.get('Major defects pieces affected')) + safe_int(row.get('Critical defects pieces affected'))
        is_rejected = str(row.get('Conclusion') or '').strip() == 'Rejected'
        stats[f]['defects'] += defects
        if order_no and is_rejected:
            stats[f]['rejected_orders'].add(order_no)
        if order_no:
            stats[f]['orders'].add(order_no)
            if str(row.get('Reinspection of') or '').strip(): stats[f]['reinspection_orders'].add(order_no)
        if report_id:
            stats[f]['reports'].add(report_id)
            key = (f, report_id)
            if key not in seen_report_sample:
                seen_report_sample.add(key)
                stats[f]['sample_qty'] += sample_qty
    out = {}
    for f, v in stats.items():
        sample = v['sample_qty']
        defects = v['defects']
        out[f] = {
            'sample_qty': sample,
            'defects': defects,
            'rate': round(defects / sample * 100, 2) if sample > 0 else 0,
            'inspections': len(v['reports']),
            'orders_checked': len(v['orders']),
            'orders_with_reinspection': len(v.get('reinspection_orders', set())),
            'rejected_orders': len(v.get('rejected_orders', set())),
            'order_rate': round(len(v.get('rejected_orders', set())) / len(v['orders']) * 100, 2) if len(v['orders']) > 0 else 0,
        }
    QARMA_STATS_CACHE[cache_key] = out
    return out

def load_qarma_order_stats(month_filter=None):
    """Aggregate Qarma physical QC by backend order number for SKU/Admin groupings."""
    cache_key = tuple(month_filter) if month_filter else tuple(months)
    if cache_key in QARMA_ORDER_STATS_CACHE:
        return QARMA_ORDER_STATS_CACHE[cache_key]
    stats = defaultdict(lambda: {'sample_qty': 0, 'defects': 0, 'reports': set()})
    seen_report_sample = set()
    for row, month in iter_qarma_rows(month_filter):
        order_no = str(row.get('Order number') or '').strip()
        if not order_no:
            continue
        report_id = str(row.get('Report inspection id') or row.get('Inspection id') or '')
        sample_qty = safe_int(row.get('Actual sample quantity'))
        defects = safe_int(row.get('Minor defects pieces affected')) + safe_int(row.get('Major defects pieces affected')) + safe_int(row.get('Critical defects pieces affected'))
        is_rejected = str(row.get('Conclusion') or '').strip() == 'Rejected'
        stats[order_no]['defects'] += defects
        if is_rejected:
            stats[order_no]['is_rejected'] = True
        if report_id:
            stats[order_no]['reports'].add(report_id)
            key = (order_no, report_id)
            if key not in seen_report_sample:
                seen_report_sample.add(key)
                stats[order_no]['sample_qty'] += sample_qty
    out = {}
    for ono, v in stats.items():
        sample = v['sample_qty']
        defects = v['defects']
        out[ono] = {
            'sample_qty': sample,
            'defects': defects,
            'rate': round(defects / sample * 100, 2) if sample > 0 else 0,
            'orders_checked': 1,
            'rejected_orders': 1 if v.get('is_rejected') else 0,
            'order_rate': 100 if v.get('is_rejected') else 0,
            'inspections': len(v['reports']),
        }
    QARMA_ORDER_STATS_CACHE[cache_key] = out
    return out

def load_qarma_stats_scoped(month_filter=None):
    """Qarma metrics restricted to the same completed/shipped Bronze order scope."""
    cache_key = ('scoped', tuple(month_filter) if month_filter else tuple(months))
    if cache_key in QARMA_STATS_CACHE: return QARMA_STATS_CACHE[cache_key]
    wanted = set(month_filter) if month_filter else set(months)
    allowed = {(o, f) for o, f, m, q in QARMA_SCOPE if m in wanted}
    order_qty = {o: q for o, f, m, q in QARMA_SCOPE if m in wanted}
    stats = defaultdict(lambda: {'sample_qty': 0, 'defects': 0, 'reports': set(), 'orders': set(), 'reinspection_orders': set(), 'rejected_orders': set()})
    seen = set()
    order_samples = defaultdict(lambda: defaultdict(int))
    for raw in load_qarma_rows():
        order = str(raw.get('Order number') or '').strip()
        factory = norm_qarma_supplier(raw.get('Supplier name'))
        if not order or (order, factory) not in allowed or not is_qarma_final_candidate(raw): continue
        if str(raw.get('Reinspection of') or '').strip():
            stats[factory]['reinspection_orders'].add(order)
            continue
        stats[factory]['orders'].add(order)
        if str(raw.get('Conclusion') or '').strip() == 'Rejected': stats[factory]['rejected_orders'].add(order)
        report_id = str(raw.get('Report inspection id') or raw.get('Inspection id') or '').strip()
        if report_id:
            stats[factory]['reports'].add(report_id)
            key = (factory, report_id)
            if key not in seen:
                seen.add(key)
                order_samples[factory][order] += safe_int(raw.get('Actual sample quantity'))
        stats[factory]['defects'] += sum(safe_int(raw.get(k)) for k in ('Minor defects pieces affected','Major defects pieces affected','Critical defects pieces affected'))
    out = {}
    for f, v in stats.items():
        checked_qty = sum(min(sample, order_qty.get(order, sample)) for order, sample in order_samples[f].items())
        out[f] = {'sample_qty': checked_qty, 'defects': v['defects'], 'rate': round(v['defects']/checked_qty*100,2) if checked_qty else 0, 'inspections': len(v['reports']), 'orders_checked': len(v['orders']), 'orders_with_reinspection': len(v['reinspection_orders']), 'rejected_orders': len(v['rejected_orders']), 'order_rate': round(len(v['rejected_orders'])/len(v['orders'])*100,2) if v['orders'] else 0}
    QARMA_SOURCE_META['filtered_rows'] = sum(1 for raw in load_qarma_rows() if is_qarma_included(raw))
    QARMA_STATS_CACHE[cache_key] = out
    return out

qarma_stats = load_qarma_stats_scoped()

month_labels = {
    "2025-10": "Oct 2025", "2025-11": "Nov 2025", "2025-12": "Dec 2025",
    "2026-01": "Jan 2026", "2026-02": "Feb 2026", "2026-03": "Mar 2026",
    "2026-04": "Apr 2026", "2026-05": "May 2026", "2026-06": "Jun 2026",
}
_current_month_key = datetime.now(timezone.utc).strftime('%Y-%m')
for _m in sorted(total_monthly.keys()):
    if _m not in month_labels:
        _dt = datetime.strptime(_m + '-01', '%Y-%m-%d')
        month_labels[_m] = _dt.strftime('%b %Y')
if _current_month_key in month_labels:
    month_labels[_current_month_key] = month_labels[_current_month_key].rstrip('*') + '*'

total_volume = sum(d['qty'] for d in total_monthly.values())
total_orders = sum(d['orders'] for d in total_monthly.values())
total_defects = sum(monthly_defects.values())
total_defect_orders = sum(defect_order_count.values())
total_rate = round(total_defects / total_volume * 100, 2) if total_volume > 0 else 0
total_order_rate = round(total_defect_orders / total_orders * 100, 2) if total_orders > 0 else 0

all_months_list = sorted(total_monthly.keys())
last_3 = all_months_list[-3:] if len(all_months_list) >= 3 else all_months_list
rolling_volume = sum(total_monthly.get(m, {}).get('qty', 0) for m in last_3)
rolling_orders = sum(total_monthly.get(m, {}).get('orders', 0) for m in last_3)
rolling_defects = sum(monthly_defects.get(m, 0) for m in last_3)
rolling_defect_orders = sum(defect_order_count.get(m, 0) for m in last_3)
rolling_remake_orders = sum(remake_by_month.get(m, {}).get('orders', 0) for m in last_3)
rolling_remake_qty = sum(remake_by_month.get(m, {}).get('qty', 0) for m in last_3)
rolling_rate = round(rolling_defects / rolling_volume * 100, 2) if rolling_volume > 0 else 0
rolling_order_rate = round(rolling_defect_orders / rolling_orders * 100, 2) if rolling_orders > 0 else 0
rolling_remake_order_rate = round(rolling_remake_orders / rolling_orders * 100, 2) if rolling_orders > 0 else 0
last_month_label = month_labels.get(last_3[-1], last_3[-1]) if last_3 else ""

all_months_sorted = sorted(total_monthly.keys())
report_month_labels = [month_labels.get(m, m) for m in all_months_sorted]

for f in factories:
    f['qarma'] = qarma_stats.get(f['name'], {'sample_qty': 0, 'defects': 0, 'rate': 0, 'inspections': 0, 'orders_checked': 0})

monthly_rows = []
for m in months:
    vol = total_monthly.get(m, {}).get('qty', 0)
    defs = monthly_defects.get(m, 0)
    rate = round(defs / vol * 100, 2) if vol > 0 else 0
    monthly_rows.append({'month': month_labels.get(m, m), 'volume': vol, 'defects': defs, 'rate': rate})

report_data = {
    'months': report_month_labels,
    'monthlyVolume': [r['volume'] for r in monthly_rows],
    'monthlyOrders': [total_monthly.get(m, {}).get('orders', 0) for m in months],
    'monthlyDefects': [r['defects'] for r in monthly_rows],
    'monthlyDefectOrders': [defect_order_count.get(m, 0) for m in months],
    'monthlyRemakeOrders': [remake_by_month.get(m, {}).get('orders', 0) for m in months],
    'monthlyRemakeQty': [remake_by_month.get(m, {}).get('qty', 0) for m in months],
    'monthlyRate': [r['rate'] for r in monthly_rows],
    'totalVolume': total_volume,
    'totalOrders': total_orders,
    'totalDefects': total_defects,
    'totalDefectOrders': total_defect_orders,
    'totalRemakeOrders': sum(remake_by_month.get(m, {}).get('orders', 0) for m in months),
    'totalRemakeQty': sum(remake_by_month.get(m, {}).get('qty', 0) for m in months),
    'totalRate': total_rate,
    'totalOrderRate': total_order_rate,
    'rollingRate': rolling_rate,
    'rollingOrderRate': rolling_order_rate,
    'rollingRemakeOrderRate': rolling_remake_order_rate,
    'rollingRemakeOrders': rolling_remake_orders,
    'rollingRemakeQty': rolling_remake_qty,
    'rollingOrders': rolling_orders,
    'rollingLabel': (month_labels.get(last_3[0], last_3[0]) + " – " + month_labels.get(last_3[-1], last_3[-1])) if len(last_3) >= 2 else '',
    'factories': factories,
    'factoryMonthly': factory_monthly_data,
    'qarmaSource': QARMA_SOURCE_META,
}

# ── YTD 2026 data (Jan–Jun) ──
ytd_months = [m for m in all_months_sorted if m.startswith("2026")]
ytd_month_labels = [month_labels.get(m, m) for m in ytd_months]
ytd_volume = sum(total_monthly.get(m, {}).get('qty', 0) for m in ytd_months)
ytd_orders = sum(total_monthly.get(m, {}).get('orders', 0) for m in ytd_months)
ytd_defects = sum(monthly_defects.get(m, 0) for m in ytd_months)
ytd_defect_orders = sum(defect_order_count.get(m, 0) for m in ytd_months)
ytd_rate = round(ytd_defects / ytd_volume * 100, 2) if ytd_volume > 0 else 0
ytd_order_rate = round(ytd_defect_orders / ytd_orders * 100, 2) if ytd_orders > 0 else 0

ytd_qarma_stats = load_qarma_stats_scoped(ytd_months)

# YTD factory totals
ytd_factories = []
for f in factories:
    fname = f['name']
    # Recalculate using only 2026 months
    fd = next((fd for fd in factory_monthly_data if fd['name'] == fname), None)
    if fd:
        ytd_def = sum(fd['defects'][i] for i, m in enumerate(all_months_sorted) if m.startswith("2026") and i < len(fd['defects']))
        ytd_vol = sum(fd['volumes'][i] for i, m in enumerate(all_months_sorted) if m.startswith("2026") and i < len(fd['volumes']))
        ytd_orders_f = sum(fd.get('orders', [])[i] for i, m in enumerate(all_months_sorted) if m.startswith("2026") and i < len(fd.get('orders', [])))
        ytd_defect_orders_f = sum(fd.get('defect_orders', [])[i] for i, m in enumerate(all_months_sorted) if m.startswith("2026") and i < len(fd.get('defect_orders', [])))
        ytd_remake_orders_f = sum(fd.get('remake_orders', [])[i] for i, m in enumerate(all_months_sorted) if m.startswith("2026") and i < len(fd.get('remake_orders', [])))
        ytd_remake_qty_f = sum(fd.get('remake_qty', [])[i] for i, m in enumerate(all_months_sorted) if m.startswith("2026") and i < len(fd.get('remake_qty', [])))
        ytd_rate_f = round(ytd_def / ytd_vol * 100, 2) if ytd_vol > 0 else 0
        ytd_order_rate_f = round(ytd_defect_orders_f / ytd_orders_f * 100, 2) if ytd_orders_f > 0 else 0
        ytd_factories.append({'name': fname, 'volume': ytd_vol, 'orders': ytd_orders_f, 'defects': ytd_def, 'defect_orders': ytd_defect_orders_f, 'remake_orders': ytd_remake_orders_f, 'remake_qty': ytd_remake_qty_f, 'rate': ytd_rate_f, 'order_rate': ytd_order_rate_f, 'qarma': ytd_qarma_stats.get(fname, {'sample_qty': 0, 'defects': 0, 'rate': 0, 'inspections': 0, 'orders_checked': 0})})
ytd_factories.sort(key=lambda x: -x['rate'])


DATA_JSON = json.dumps(report_data, cls=factory_data.DecimalEncoder)

def account_report_json(account_data):
    months = account_data['months']
    monthly = account_data['total_monthly']
    remakes = account_data.get('remake_by_month', {})
    return {
        'months': [month_labels.get(m, m) for m in months],
        'monthlyVolume': [monthly.get(m, {}).get('qty', 0) for m in months],
        'monthlyOrders': [monthly.get(m, {}).get('orders', 0) for m in months],
        'monthlyRemakeOrders': [remakes.get(m, {}).get('orders', 0) for m in months],
        'monthlyRemakeQty': [remakes.get(m, {}).get('qty', 0) for m in months],
        'totalVolume': sum(monthly.get(m, {}).get('qty', 0) for m in months),
        'totalOrders': sum(monthly.get(m, {}).get('orders', 0) for m in months),
        'totalRemakeOrders': sum(remakes.get(m, {}).get('orders', 0) for m in months),
        'totalRemakeQty': sum(remakes.get(m, {}).get('qty', 0) for m in months),
        'factories': [{
            'name': f['name'], 'volume': f.get('volume', 0), 'orders': f.get('orders', 0),
            'remake_orders': f.get('remake_orders', 0), 'remake_qty': f.get('remake_qty', 0)
        } for f in account_data.get('factories', [])],
        'factoryMonthly': account_data.get('factory_monthly', [])
    }

HUMMEL_DATA_JSON = json.dumps(account_report_json(hummel_account_data), cls=factory_data.DecimalEncoder)
YTD_DATA_JSON = json.dumps({
    'months': ytd_month_labels,
    'monthKeys': ytd_months,
    'monthlyVolume': [total_monthly.get(m, {}).get('qty', 0) for m in ytd_months],
    'monthlyOrders': [total_monthly.get(m, {}).get('orders', 0) for m in ytd_months],
    'monthlyRemakeOrders': [remake_by_month.get(m, {}).get('orders', 0) for m in ytd_months],
    'monthlyRemakeQty': [remake_by_month.get(m, {}).get('qty', 0) for m in ytd_months],
    'monthlyDefects': [monthly_defects.get(m, 0) for m in ytd_months],
    'monthlyDefectOrders': [defect_order_count.get(m, 0) for m in ytd_months],
    'volume': ytd_volume,
    'orders': ytd_orders,
    'defects': ytd_defects,
    'defectOrders': ytd_defect_orders,
    'rate': ytd_rate,
    'orderRate': ytd_order_rate,
    'cumulativeVolume': [sum(total_monthly.get(m2, {}).get('qty', 0) for m2 in ytd_months[:i+1]) for i in range(len(ytd_months))],
    'cumulativeOrders': [sum(total_monthly.get(m2, {}).get('orders', 0) for m2 in ytd_months[:i+1]) for i in range(len(ytd_months))],
    'cumulativeDefects': [sum(monthly_defects.get(m2, 0) for m2 in ytd_months[:i+1]) for i in range(len(ytd_months))],
    'cumulativeDefectOrders': [sum(defect_order_count.get(m2, 0) for m2 in ytd_months[:i+1]) for i in range(len(ytd_months))],
    'cumulativeRate': [round(
        sum(monthly_defects.get(m2, 0) for m2 in ytd_months[:i+1]) /
        sum(total_monthly.get(m2, {}).get('qty', 0) for m2 in ytd_months[:i+1]) * 100, 2
    ) if sum(total_monthly.get(m2, {}).get('qty', 0) for m2 in ytd_months[:i+1]) > 0 else 0 for i in range(len(ytd_months))],
    'cumulativeOrderRate': [round(
        sum(defect_order_count.get(m2, 0) for m2 in ytd_months[:i+1]) /
        sum(total_monthly.get(m2, {}).get('orders', 0) for m2 in ytd_months[:i+1]) * 100, 2
    ) if sum(total_monthly.get(m2, {}).get('orders', 0) for m2 in ytd_months[:i+1]) > 0 else 0 for i in range(len(ytd_months))],
    'factories': ytd_factories,
}, cls=factory_data.DecimalEncoder)

FACTORY_COLORS = json.dumps({
    "Mavic Sports": "rgba(217, 45, 32, 0.85)",
    "Selberian Sports Wear": "rgba(247, 144, 9, 0.85)",
    "Silver-Star Group": "rgba(124, 58, 237, 0.82)",
    "Karrizo": "rgba(18, 183, 106, 0.85)",
    "Rajco": "rgba(31, 111, 235, 0.85)",
})

# Load order-level data — use factory_data.MANUAL for consistency
import urllib.request, urllib.parse, re

conn = db.connect()
cur = conn.cursor()

# FU/customer-feedback data is intentionally excluded from the shared report.
first_fu_month = {}
groups = defaultdict(list)

ALL_ORDER_NUMS = set()
qs = ",".join(["%s"] * len(ALL_ORDER_NUMS))

order_factory = {}
order_qty = {}
db_order_nums = set()
if ALL_ORDER_NUMS:
    cur.execute("""
        SELECT o.order_no,
               COALESCE(NULLIF(o.price_info->>'total_quantity', '')::numeric, 0)::int,
               COALESCE(oi.factory_name, '(unknown)')
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id AND oi.deleted_at IS NULL
        WHERE o.order_no = ANY(%s)
          AND o.deleted_at IS NULL
    """, (list(map(str, ALL_ORDER_NUMS)),))
else:
    cur.execute("SELECT NULL, NULL, NULL WHERE false")
for r in cur.fetchall():
    ono = str(r[0])
    db_order_nums.add(ono)
    order_factory[ono] = factory_data.norm_factory(r[2])
    order_qty[ono] = r[1] or 0

from report import classify_product, extract_qty
product_types = {}
for ono in ALL_ORDER_NUMS:
    cat, is_prod = classify_product(ono, conn)
    product_types[ono] = cat

def categorize_root_cause(root, issue='', corrective='', remarks=''):
    """Return (category, confidence). Only >=90 is accepted; otherwise Uncategorized."""
    import re
    text = ' '.join(str(x or '') for x in (root, issue, corrective, remarks)).lower()
    text = re.sub(r'\s+', ' ', text)
    if not text.strip():
        return 'Uncategorized', 0
    rules = [
        ('Missing item / packing', 96, ['missing pcs', 'missing jerseys', 'missing pieces', 'additional cemara', 'packing table', 'camera added into packing', 'camara added into packing']),
        ('Missing branding / logo', 95, ['missing branding', 'brand logo', 'branding logo was missed', 'branding logos added', 'logo was missed']),
        ('Wrong badge / logo', 94, ['wrong badge', 'badge color incorrect', 'badge colour incorrect', 'produced badge', 'prduced badge', 'wrong logo', 'logo size', '3d logo peel', 'wrong colour in logo', 'wrong color in logo']),
        ('Wrong number / sizing', 94, ['number sizing wrong', 'wrong number', 'wrong size', 'size of logo', 'sizes wrong']),
        ('Spec / tech pack mismatch', 93, ['tds was correct', 'teck pack was wrong', 'tech pack was wrong', 'only check tds', 'as per teckpack', 'as per techpack', 'customer not informed', 'not aware', 'rawling specs']),
        ('Embroidery / decoration method', 94, ['embroidery', 'emb.', '3000/4000', 'sublimation', 'twill patch', 'zigzag']),
        ('Color issue', 93, ['wrong color', 'wrong colour', 'buttons color', 'pantone', 'color incorrect', 'colour incorrect']),
        ('Garment construction / stitching', 94, ['broken stitch', 'stitch at zips', 'defective zippers', 'notching', 'sleeve', 'zipper', 'zips']),
        ('Fabric / material', 94, ['fabric', 'felt cheaper', 'approve sample fabric']),
        ('Accessory / trims', 93, ['necktape', 'neck tape', 'tag', 'buttons']),
        ('Factory supplied component', 92, ['provided woven label', 'sourcing anything', 'provided 3d logos', 'we provided']),
        ('No production issue', 95, ['no issue with production', 'was correct', 'shipped / video provided', 'not shipped yet']),
        ('QC overlooked', 91, ['qc overlooked', 'overlooked by qc', 'qc guy', 'qc for more focus', 'during final inspection', 'aql audit']),
    ]
    for category, confidence, needles in rules:
        if any(n in text for n in needles):
            return category, confidence
    return 'Uncategorized', 0

def load_issue_categories():
    import re
    from pathlib import Path
    import openpyxl
    candidates = sorted(Path('/Users/lakr-macmini/Desktop/qarma').glob('Order*Issues*Monthly*.xlsx'), key=lambda x: x.stat().st_mtime, reverse=True)
    if not candidates:
        return {}
    ws = openpyxl.load_workbook(candidates[0], read_only=True, data_only=True).active
    out = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        month = str(r[0] or '').strip()
        order = str(r[1] or '').strip()
        if not month or not order:
            continue
        ml = month.lower()
        if 'subtotal' in ml or 'grand total' in ml or '—' in month:
            continue
        m = re.search(r'\d{4,6}', order)
        if not m:
            continue
        order_no = m.group(0)
        root = r[8] if len(r) > 8 else None
        issue = r[6] if len(r) > 6 else None
        corrective = r[12] if len(r) > 12 else None
        remarks = r[16] if len(r) > 16 else None
        category, confidence = categorize_root_cause(root, issue, corrective, remarks)
        if confidence < 90:
            category = 'Uncategorized'
        out[order_no] = {
            'month': month,
            'issue': issue or '',
            'root_cause': root or '',
            'corrective_action': corrective or '',
            'remarks': remarks or '',
            'category': category,
            'confidence': confidence,
        }
    return out

ISSUE_CATEGORIES = load_issue_categories()



order_details = []
for ono in ALL_ORDER_NUMS:
    related_msgs = groups.get(ono, [])
    if ono in factory_data.MANUAL:
        affected = factory_data.MANUAL[ono]
        if affected == 0:
            continue
        source = 'manual'
    else:
        if factory_data.is_non_defect_fu(related_msgs):
            continue
        affected = extract_qty(related_msgs)
        if affected is None:
            continue
        source = 'parsed_fu'

    fu_month = first_fu_month.get(ono, "?")
    factory = order_factory.get(ono, "(unknown)")
    if ono in factory_data.MANUAL_FACTORY:
        factory = factory_data.MANUAL_FACTORY[ono]
    if factory in getattr(factory_data, 'EXCLUDED_FACTORIES', set()): continue
    if factory == "(unknown)": continue
    if ono not in db_order_nums: continue  # phantom ID from email URL, not a real order
    qty = order_qty.get(ono, 0)
    if source == 'parsed_fu' and qty and affected > qty:
        continue
    pt = product_types.get(ono, "Unknown")
    
    subjects = " | ".join(set(m["subject"] for m in related_msgs))[:200]
    snippet = (related_msgs[0]["body"][:300] if related_msgs else "")[:300]

    order_details.append({
        "order": ono,
        "affected": affected,
        "total_qty": qty,
        "fu_month": fu_month,
        "factory": factory,
        "product_type": pt,
        "subjects": subjects,
        "snippet": snippet,
        "category": ISSUE_CATEGORIES.get(ono, {}).get("category", "Uncategorized"),
        "root_cause": ISSUE_CATEGORIES.get(ono, {}).get("root_cause", ""),
        "category_confidence": ISSUE_CATEGORIES.get(ono, {}).get("confidence", 0),
        "source": source,
    })

order_details.sort(key=lambda x: -x["affected"])

counted_orders = {d['order'] for d in order_details}
fu_review_rows = []
for ono in sorted(db_order_nums, key=lambda n: (first_fu_month.get(n, ''), n), reverse=True):
    related_msgs = groups.get(ono, [])
    if not related_msgs:
        continue
    if ono in counted_orders:
        status = 'Counted in FU defects'
        affected_review = next((d['affected'] for d in order_details if d['order'] == ono), '')
    elif ono in factory_data.MANUAL_ZERO:
        status = 'Excluded — manual non-defect'
        affected_review = ''
    elif factory_data.is_non_defect_fu(related_msgs):
        status = 'Excluded — delay/process, not physical defect'
        affected_review = ''
    else:
        parsed_qty = extract_qty(related_msgs)
        if parsed_qty is None:
            status = 'Needs affected qty review'
            affected_review = ''
        else:
            status = 'Parsed but not counted — check product/factory filter'
            affected_review = parsed_qty
    fu_review_rows.append({
        'order': ono,
        'fu_month': first_fu_month.get(ono, '?'),
        'status': status,
        'affected': affected_review,
        'factory': order_factory.get(ono, '(unknown)'),
        'total_qty': order_qty.get(ono, 0),
        'subjects': ' | '.join(set(m['subject'] for m in related_msgs))[:200],
        'snippet': (related_msgs[0]['body'][:300] if related_msgs else '')[:300],
    })

def collect_sku_text(obj):
    parts = []
    keys = {'sku_id','sku_name','sku_number','description','product_name','product_display_name','design_nick_name','new_product_name','style_name'}
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in keys and v is not None:
                    parts.append(str(v))
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return ' | '.join(parts)

def clean_sku_text(t):
    import re, html
    t = html.unescape(t or '')
    t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def classify_sku_text(t):
    import re
    low = (t or '').lower()
    series = sorted(set(re.findall(r'\b([1234])000\s*(?:-|\s)?(?:series|se)?\b', low)))
    series = [x + '000' for x in series]
    sublimated = bool(re.search(r'\bsublimat(?:ed|ion|e)?\b|\bfully\s+sub\b|\bsub\b', low))
    return series, sublimated


# ── Summary breakdown groupings (SKU / Order Admin) ──
def classify_order_series_from_text(text):
    series, sublimated = classify_sku_text(text)
    return series or ['No series found'], sublimated

def rollup_sku_groups(series):
    out = set()
    for ser in series:
        if ser in ('1000', '2000'):
            out.add('Sublimation')
        elif ser in ('3000', '4000'):
            out.add('Embroidery')
        else:
            out.add(ser)
    return sorted(out)


def classify_sport_text(text):
    t = ' ' + (text or '').lower() + ' '
    sport_patterns = [
        ('Hockey', ['hockey']),
        ('Basketball', ['basketball']),
        ('Baseball', ['baseball', 'softball']),
        ('Football', ['football', 'gridiron']),
        ('Soccer', ['soccer', 'football jersey']),
        ('Lacrosse', ['lacrosse']),
        ('Volleyball', ['volleyball']),
        ('Rugby', ['rugby']),
        ('Cycling', ['cycling', 'biking', 'bike jersey']),
        ('Running', ['running', 'track and field', 'athletics']),
        ('Esports', ['esport', 'e-sport', 'gaming jersey']),
        ('Training', ['training', 'warmup', 'warm-up']),
    ]
    found = []
    for label, needles in sport_patterns:
        if any(n in t for n in needles):
            found.append(label)
    return sorted(set(found)) or ['No sport found']


def windows_login_from_email(email, fallback='(unknown)'):
    email = str(email or '').strip()
    if '@' in email:
        return email.split('@', 1)[0].lower()
    fb = str(fallback or '').strip()
    if '@' in fb:
        return fb.split('@', 1)[0].lower()
    return fb or '(unknown)'

NON_DESIGNER_LOGINS = {'factory', 'orders', 'sales', 'super', 'admin', 'support', 'no-reply', 'noreply'}
DAILY_TASKS_CHAT_ID = '19:f15713ebba454e01924ef82b3363e6df@thread.v2'


def graph_token_from_env():
    tenant = os.environ.get('CUSTIMOO_GRAPH_TENANT_ID')
    client_id = os.environ.get('CUSTIMOO_GRAPH_CLIENT_ID')
    client_secret = os.environ.get('CUSTIMOO_GRAPH_CLIENT_SECRET')
    if not all([tenant, client_id, client_secret]):
        return None
    body = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials',
    }).encode()
    req = urllib.request.Request(
        f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
        data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())['access_token']


def clean_teams_html(text):
    htmllib = __import__('html')
    return htmllib.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text or ''))).strip()


def teams_windows_login_from_path_user(path_user):
    raw = str(path_user or '').strip()
    if not raw:
        return ''
    return re.sub(r'[^a-z0-9]+', '', raw.lower())


def title_from_windows_login(path_user):
    raw = str(path_user or '').strip()
    if not raw:
        return ''
    spaced = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', raw)
    spaced = re.sub(r'[^A-Za-z0-9]+', ' ', spaced).strip()
    return spaced.title() if spaced else raw


def load_production_file_designers_from_teams():
    """Map order_no -> Windows login(s) from Daily Tasks production-file upload paths.

    Designers post OneDrive-synced paths like:
      C:\\Users\\MoazSaeed\\...\\26015 - TKT#6753Whalers\\4_Production
    We attribute the order to the Windows user in that path, only for messages that look
    like production-file uploads (not sample print / TDS-only updates).
    """
    out = defaultdict(set)
    try:
        token = graph_token_from_env()
        if not token:
            return out
        cutoff = '2026-01-01T00:00:00Z'
        url = f'https://graph.microsoft.com/v1.0/chats/{urllib.parse.quote(DAILY_TASKS_CHAT_ID, safe="")}/messages?$top=50'
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        pages = 0
        path_re = re.compile(r'[A-Za-z]:[\\]+Users[\\]+([^\\]+)[\\]+.*?[\\]+(\d{5})\s*-.*?(?:4_Production|Production\s*File|production\.svg|[\\]+Production(?:[\\]+|\b))', re.I)
        loose_re = re.compile(r'\b(\d{5})\b.*?\bproduction\s*file\b', re.I)
        while url and pages < 200:
            pages += 1
            data = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45).read().decode())
            stop = False
            for msg in data.get('value', []):
                created = msg.get('createdDateTime') or ''
                if created and created < cutoff:
                    stop = True
                    continue
                content = clean_teams_html((msg.get('body') or {}).get('content', ''))
                if 'production' not in content.lower():
                    continue
                sender = ((msg.get('from') or {}).get('user') or {})
                sender_name = str(sender.get('displayName') or '').strip()
                for login_raw, order_no in path_re.findall(content):
                    login = teams_windows_login_from_path_user(login_raw)
                    if login and login not in NON_DESIGNER_LOGINS:
                        display_name = sender_name or title_from_windows_login(login_raw)
                        out[str(order_no)].add(display_name)
                # Fallback for messages like "26080 Production file as per previous orders.";
                # use Teams sender email/userIdentity if no C:\\Users path exists.
                if not path_re.search(content):
                    sender_login = windows_login_from_email(str(sender.get('email') or ''), sender_name)
                    sender_login = teams_windows_login_from_path_user(sender_login)
                    if sender_login and sender_login not in NON_DESIGNER_LOGINS:
                        display_name = sender_name or title_from_windows_login(sender_login)
                        for order_no in loose_re.findall(content):
                            out[str(order_no)].add(display_name)
            if stop:
                break
            url = data.get('@odata.nextLink')
    except Exception as e:
        print('Warning: could not load Teams production-file designers:', e)
    return out

def collect_product_design_ids(raw):
    ids = set()
    if not raw:
        return ids
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except Exception:
        return ids
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == 'design_id' and isinstance(v, int):
                    ids.add(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(parsed)
    return ids

production_file_designers_by_order = {}  # Designer exception leaderboard disabled: no reliable connection.

# Map product design ids to actual designer Windows logins via design file creators.
cur.execute("""
SELECT df.id, u.email, u.name
FROM design_files df
LEFT JOIN container_files cf ON cf.id = df.file_id
LEFT JOIN users u ON u.id = cf.created_by
""")
design_file_creators = {str(df_id): windows_login_from_email(email, name) for df_id, email, name in cur.fetchall() if email or name}
cur.execute("""
SELECT id, front_design_id, back_design_id, production_design_id, frontsafezone_design_id, backsafezone_design_id,
       productionsafezone_design_id, frontboundary_design_id, backboundary_design_id, sizes_design_id
FROM product_designs
""")
product_design_creators = defaultdict(set)
for row in cur.fetchall():
    pd_id = str(row[0])
    for df_id in row[1:]:
        login = design_file_creators.get(str(df_id)) if df_id is not None else None
        if login and login != '(unknown)' and login not in NON_DESIGNER_LOGINS:
            product_design_creators[pd_id].add(login)

# Build per-order SKU text, admin login, and actual designer login(s) for all backend orders in report window.
cur.execute("""
SELECT o.order_no, COALESCE(NULLIF(o.price_info->>'total_quantity', '')::numeric, 0)::int AS qty,
       COALESCE(ship.shipped_at, oi.status_updated_at) AS shipping_date,
       COALESCE(u.name, u.email::text, '(unknown)') AS admin_name,
       u.email::text AS admin_email,
       COALESCE(oi.factory_name, '(unknown)') AS raw_factory,
       oi.factory_products, oi.order_line
FROM orders o
LEFT JOIN users u ON u.id = o.order_administrator_id
LEFT JOIN order_items oi ON oi.order_id = o.id
LEFT JOIN LATERAL (SELECT MAX(COALESCE(a.created_at,a.updated_at)) AS shipped_at FROM order_item_activities a WHERE a.order_item_id=oi.id AND a.status::text='shipped') ship ON true
WHERE COALESCE(ship.shipped_at, oi.status_updated_at) >= %s
  AND COALESCE(ship.shipped_at, oi.status_updated_at) < %s
  AND o.deleted_at IS NULL
  AND oi.deleted_at IS NULL
  AND oi.status::text = 'completed'
""", (factory_data.REPORT_START, factory_data.REPORT_END))
all_order_meta = defaultdict(lambda: {'qty': 0, 'admin': '(unknown)', 'designers': set(), 'texts': [], 'month': '?'})
for ono, qty, shipping_date, admin_name, admin_email, raw_factory, factory_products, order_line in cur.fetchall():
    if factory_data.norm_factory(raw_factory) in getattr(factory_data, 'EXCLUDED_FACTORIES', set()):
        continue
    ono = str(ono)
    all_order_meta[ono]['qty'] = max(all_order_meta[ono]['qty'], int(qty or 0))
    all_order_meta[ono]['admin'] = admin_name or '(unknown)'
    all_order_meta[ono]['month'] = str(shipping_date)[:7] if shipping_date else '?'
    for raw in (factory_products, order_line):
        if not raw:
            continue
        try:
            parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except Exception:
            continue
        txt = clean_sku_text(collect_sku_text(parsed))
        if txt:
            all_order_meta[ono]['texts'].append(txt)

# For any order with Qarma physical-QC data, Qarma is authoritative for denominator qty/date.
# Preserve backend admin/SKU text where available, but replace qty/month with Qarma shipment data.
qarma_meta_qty = defaultdict(int)
qarma_meta_month = {}
for qr in factory_data.load_qarma_shipment_rows():
    ono = str(qr.get('order_no') or '')
    if not ono:
        continue
    qarma_meta_qty[ono] += int(qr.get('qty') or 0)
    qarma_meta_month[ono] = max(qarma_meta_month.get(ono, ''), qr.get('month') or '')
for ono, qty in qarma_meta_qty.items():
    all_order_meta[ono]['qty'] = qty
    if qarma_meta_month.get(ono):
        all_order_meta[ono]['month'] = qarma_meta_month[ono]

def empty_group():
    return {'volume': 0, 'orders_set': set(), 'defects': 0, 'defect_orders_set': set(), 'remake_orders_set': set(), 'remake_qty_total': 0, 'qarma_order_set': set(), 'qarma': {'sample_qty': 0, 'defects': 0, 'rate': 0, 'orders_checked': 0, 'inspections': 0, 'rejected_orders': 0, 'order_rate': 0}}

def finalize_groups(groups):
    rows = []
    for name, g in groups.items():
        vol = g['volume']
        orders_count = len(g['orders_set'])
        defect_orders_count = len(g['defect_orders_set'])
        remake_orders_count = len(g['remake_orders_set'])
        remake_orders_qty = g.get('remake_qty_total', 0)
        rows.append({
            'name': name,
            'volume': vol,
            'orders': orders_count,
            'defects': g['defects'],
            'defect_orders': defect_orders_count,
            'remake_orders': remake_orders_count,
            'remake_qty': remake_orders_qty,
            'rate': round(g['defects'] / vol * 100, 2) if vol > 0 else 0,
            'order_rate': round(defect_orders_count / orders_count * 100, 2) if orders_count > 0 else 0,
            'qarma': {**g.get('qarma', {'sample_qty': 0, 'defects': 0, 'rate': 0, 'orders_checked': 0, 'inspections': 0, 'rejected_orders': 0, 'order_rate': 0}), 'orders_checked': len(g.get('qarma_order_set', set())), 'rate': round(g.get('qarma', {}).get('defects', 0) / g.get('qarma', {}).get('sample_qty', 0) * 100, 2) if g.get('qarma', {}).get('sample_qty', 0) > 0 else 0, 'order_rate': round(g.get('qarma', {}).get('rejected_orders', 0) / len(g.get('qarma_order_set', set())) * 100, 2) if len(g.get('qarma_order_set', set())) > 0 else 0},
        })
    rows.sort(key=lambda x: -x['rate'])
    return rows

# Build remake orders lookup
remake_cur = conn.cursor()
remake_cur.execute("SELECT o.order_no FROM orders o WHERE o.order_type_symbol IN ('R', 'Ri') AND o.created_at >= %s AND o.created_at < %s AND o.deleted_at IS NULL", (factory_data.REPORT_START, factory_data.REPORT_END))
REMAKE_ORDERS = set(str(r[0]) for r in remake_cur.fetchall()) - remake_backend_actions.EXCLUDED_REMAKE_ORDERS
remake_cur.close()
# Don't close main conn — used later

sku_groups = defaultdict(empty_group)
sport_groups = defaultdict(empty_group)
admin_groups = defaultdict(empty_group)
category_groups = defaultdict(empty_group)
for ono, meta in all_order_meta.items():
    text = ' | '.join(meta.get('texts', []))
    series, _sublimated = classify_order_series_from_text(text)
    for ser in rollup_sku_groups(series):
        sku_groups[ser]['volume'] += meta['qty']
        sku_groups[ser]['orders_set'].add(ono)
    for sport in classify_sport_text(text):
        sport_groups[sport]['volume'] += meta['qty']
        sport_groups[sport]['orders_set'].add(ono)
    admin = meta.get('admin') or '(unknown)'
    admin_groups[admin]['volume'] += meta['qty']
    admin_groups[admin]['orders_set'].add(ono)
    is_remake = ono in REMAKE_ORDERS
    if is_remake:
        for ser in rollup_sku_groups(series):
            sku_groups[ser]['remake_orders_set'].add(ono)
            sku_groups[ser]['remake_qty_total'] += meta['qty']
        for sport in classify_sport_text(text):
            sport_groups[sport]['remake_orders_set'].add(ono)
            sport_groups[sport]['remake_qty_total'] += meta['qty']
        for credit_admin, credit_qty, credit_key in remake_backend_actions.remake_admin_allocations(ono, admin, meta.get('qty', 0)):
            if credit_admin != admin:
                admin_groups[credit_admin]['volume'] += credit_qty
                admin_groups[credit_admin]['orders_set'].add(credit_key)
            admin_groups[credit_admin]['remake_orders_set'].add(credit_key)
            admin_groups[credit_admin]['remake_qty_total'] += credit_qty

order_lookup = {d['order']: d for d in order_details}
for ono, d in order_lookup.items():
    meta = all_order_meta.get(ono, {})
    text = ' | '.join(meta.get('texts', []))
    series, _sublimated = classify_order_series_from_text(text)
    for ser in rollup_sku_groups(series):
        sku_groups[ser]['defects'] += d.get('affected', 0)
        sku_groups[ser]['defect_orders_set'].add(ono)
    for sport in classify_sport_text(text):
        sport_groups[sport]['defects'] += d.get('affected', 0)
        sport_groups[sport]['defect_orders_set'].add(ono)
    admin = meta.get('admin') or '(unknown)'
    admin_groups[admin]['defects'] += d.get('affected', 0)
    admin_groups[admin]['defect_orders_set'].add(ono)
    cat = d.get('category') or ISSUE_CATEGORIES.get(ono, {}).get('category', 'Uncategorized')
    if cat != 'Uncategorized' or ono in ISSUE_CATEGORIES:
        category_groups[cat]['volume'] += meta.get('qty', 0)
        category_groups[cat]['orders_set'].add(ono)
        category_groups[cat]['defects'] += d.get('affected', 0)
        category_groups[cat]['defect_orders_set'].add(ono)

# Add Qarma physical QC metrics into the same SKU/Admin groups, by Qarma order number.
qarma_order_stats = load_qarma_order_stats()
for ono, q in qarma_order_stats.items():
    meta = all_order_meta.get(ono)
    if not meta:
        continue
    text = ' | '.join(meta.get('texts', []))
    series, _sublimated = classify_order_series_from_text(text)
    admin = meta.get('admin') or '(unknown)'
    for ser in series:
        sku_groups[ser]['qarma']['sample_qty'] += q.get('sample_qty', 0)
        sku_groups[ser]['qarma']['defects'] += q.get('defects', 0)
        sku_groups[ser]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
        sku_groups[ser]['qarma']['inspections'] += q.get('inspections', 0)
        sku_groups[ser]['qarma_order_set'].add(ono)
    admin_groups[admin]['qarma']['sample_qty'] += q.get('sample_qty', 0)
    admin_groups[admin]['qarma']['defects'] += q.get('defects', 0)
    admin_groups[admin]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
    admin_groups[admin]['qarma']['inspections'] += q.get('inspections', 0)
    admin_groups[admin]['qarma_order_set'].add(ono)
    cat = ISSUE_CATEGORIES.get(ono, {}).get('category')
    if cat:
        category_groups[cat]['qarma']['sample_qty'] += q.get('sample_qty', 0)
        category_groups[cat]['qarma']['defects'] += q.get('defects', 0)
        category_groups[cat]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
        category_groups[cat]['qarma']['inspections'] += q.get('inspections', 0)
        category_groups[cat]['qarma_order_set'].add(ono)

GROUPING_JSON = json.dumps({
    'sku': finalize_groups(sku_groups),
    'sport': finalize_groups(sport_groups),
    'admin': finalize_groups(admin_groups),
    'category': finalize_groups(category_groups),
}, default=str)
GROUPING_JSON_SAFE = GROUPING_JSON.replace('<', '\\u003C').replace('>', '\\u003E')

# Completed order detail used by token-protected factory share pages.
_factory_share_cur = conn.cursor()
_factory_share_cur.execute("""
SELECT o.order_no,
       o.created_at AS created_date,
       max(CASE WHEN a.status::text='completed' THEN COALESCE(a.created_at,a.updated_at) END) AS completed_date,
       max(CASE WHEN a.status::text='shipped' THEN COALESCE(a.created_at,a.updated_at) END) AS actual_shipping_date,
       max(CASE WHEN oi.status::text IN ('shipped','completed') OR oi.shipping_status IS NOT NULL THEN oi.status_updated_at END) AS backend_shipping_date,
       COALESCE(NULLIF(o.price_info->>'total_quantity','')::numeric,0)::int AS qty,
       COALESCE(string_agg(DISTINCT oi.factory_name, ', ' ORDER BY oi.factory_name),'(unknown)') AS factories,
       COALESCE(NULLIF(co.company_name,''), NULLIF(BTRIM(CONCAT_WS(' ',c.first_name,c.last_name)),''),'(unknown)') AS customer,
       o.customer_reference_no,
       o.order_type_symbol
FROM orders o
JOIN order_items oi ON oi.order_id=o.id AND oi.deleted_at IS NULL
LEFT JOIN order_item_activities a ON a.order_item_id=oi.id
LEFT JOIN customers c ON c.id=o.customer_id
LEFT JOIN companies co ON co.id=c.company_id
WHERE o.deleted_at IS NULL AND oi.status::text='completed'
GROUP BY o.order_no,o.created_at,o.price_info,co.company_name,c.first_name,c.last_name,o.customer_reference_no,o.order_type_symbol
""")
FACTORY_SHARE_ORDERS = {}
_factory_qarma_details = {}
for _qr in load_qarma_rows():
    _qo = str(_qr.get('Order number') or '').strip()
    if not _qo or not is_qarma_final_candidate(_qr): continue
    _qm = dt_to_month(_qr.get('Scheduled inspection date') or _qr.get('Inspection end time'))
    _qd = _factory_qarma_details.setdefault(_qo, {'rejected': False, 'reinspected': False, 'comments': [], 'links': [], 'months': set()})
    if _qm: _qd['months'].add(_qm)
    if str(_qr.get('Conclusion') or '').strip() == 'Rejected': _qd['rejected'] = True
    if str(_qr.get('Reinspection of') or '').strip(): _qd['reinspected'] = True
    _comment = str(_qr.get('Inspector comment') or '').strip()
    if _comment and _comment not in _qd['comments']: _qd['comments'].append(_comment)
    _link = str(_qr.get('Link to report') or '').strip()
    if _link and _link not in _qd['links']: _qd['links'].append(_link)
for _order,_created,_date,_actual_shipping_date,_shipping_date,_qty,_factories,_customer,_customer_ref,_otype in _factory_share_cur.fetchall():
    _q = qarma_order_stats.get(str(_order), {})
    _qd = _factory_qarma_details.get(str(_order), {})
    _row = {'order': str(_order), 'created_date': str(_created)[:19] if _created else '', 'completed_date': str(_date)[:19] if _date else '', 'actual_shipping_date': str(_actual_shipping_date)[:19] if _actual_shipping_date else '', 'backend_shipping_date': str(_shipping_date)[:19] if _shipping_date else '', 'qty': int(_qty or 0), 'customer': _customer or '(unknown)', 'customer_ref': str(_customer_ref or ''), 'qarma_checked': bool(_q), 'qarma_error': bool(_q.get('defects', 0)), 'qarma_rejected': bool(_qd.get('rejected')), 'qarma_reinspected': bool(_qd.get('reinspected')), 'qarma_months': sorted(_qd.get('months', set())), 'qarma_comment': ' · '.join(_qd.get('comments', [])), 'qarma_report_link': (_qd.get('links') or [''])[0], 'remake': str(_order) in REMAKE_ORDERS}
    for _factory in str(_factories or '(unknown)').split(','):
        FACTORY_SHARE_ORDERS.setdefault(factory_data.norm_factory(_factory.strip()), []).append(_row)
_factory_share_cur.close()
for _factory in FACTORY_SHARE_ORDERS:
    FACTORY_SHARE_ORDERS[_factory].sort(key=lambda r: r.get('actual_shipping_date') or r.get('completed_date',''), reverse=True)
FACTORY_SHARE_ORDERS_JSON = json.dumps(FACTORY_SHARE_ORDERS, cls=factory_data.DecimalEncoder)


def build_error_tracking():
    from collections import defaultdict
    et = {
        'factory': defaultdict(lambda: defaultdict(lambda: {'order_count': 0, 'defect_qty': 0, 'order_nums': []})),
        'sku': defaultdict(lambda: defaultdict(lambda: {'order_count': 0, 'defect_qty': 0, 'order_nums': []})),
        'sport': defaultdict(lambda: defaultdict(lambda: {'order_count': 0, 'defect_qty': 0, 'order_nums': []})),
        'admin': defaultdict(lambda: defaultdict(lambda: {'order_count': 0, 'defect_qty': 0, 'order_nums': []})),
    }
    for d in order_details:
        ono = d['order']
        cat_info = ISSUE_CATEGORIES.get(ono)
        if not cat_info:
            continue
        category = cat_info['category']
        defect = d.get('affected', 0)
        meta = all_order_meta.get(ono, {})
        text = ' | '.join(meta.get('texts', []))
        factory = d.get('factory', 'Unknown')
        et['factory'][factory][category]['order_count'] += 1
        et['factory'][factory][category]['defect_qty'] += defect
        et['factory'][factory][category]['order_nums'].append(ono)
        admin = meta.get('admin', 'Unknown')
        et['admin'][admin][category]['order_count'] += 1
        et['admin'][admin][category]['defect_qty'] += defect
        et['admin'][admin][category]['order_nums'].append(ono)
        series, _ = classify_order_series_from_text(text)
        for grp in rollup_sku_groups(series):
            et['sku'][grp][category]['order_count'] += 1
            et['sku'][grp][category]['defect_qty'] += defect
            et['sku'][grp][category]['order_nums'].append(ono)
        for sport in classify_sport_text(text):
            et['sport'][sport][category]['order_count'] += 1
            et['sport'][sport][category]['defect_qty'] += defect
            et['sport'][sport][category]['order_nums'].append(ono)
    result = {}
    for mode in ('factory','sku','sport','admin'):
        result[mode] = {}
        for group in sorted(et[mode]):
            cats = sorted(et[mode][group].items(), key=lambda x: -x[1]['defect_qty'])
            result[mode][group] = [{'category':c,'order_count':info['order_count'],'defect_qty':info['defect_qty'],'order_nums':info['order_nums']} for c,info in cats]
    return result

ERROR_TRACKING = build_error_tracking()
ERROR_TRACKING_JSON = json.dumps(ERROR_TRACKING, default=str)
ERROR_TRACKING_JSON_SAFE = ERROR_TRACKING_JSON.replace('<','\\u003C').replace('>','\\u003E')


# ── Summary period slices ──
def period_months_for(key):
    ordered = all_months_sorted
    current = ordered[-1] if ordered else ''
    prev = ordered[-2] if len(ordered) >= 2 else current
    if key == 'all':
        return ordered
    if key == 'last_3':
        return ordered[-3:]
    if key == 'last_6':
        return ordered[-6:]
    if key == 'last_month':
        return [prev]
    if key == 'mtd':
        return [current]
    if key == 'ytd':
        year = current[:4]
        return [m for m in ordered if m.startswith(year)]
    if key == 'quarter':
        year = current[:4]
        month_num = int(current[5:7]) if current and current[5:7].isdigit() else 1
        q_start = ((month_num - 1) // 3) * 3 + 1
        wanted = {f"{year}-{m:02d}" for m in range(q_start, q_start + 3)}
        return [m for m in ordered if m in wanted]
    return ordered

def period_label(month_keys):
    if not month_keys:
        return ''
    if len(month_keys) == 1:
        return month_labels.get(month_keys[0], month_keys[0])
    return month_labels.get(month_keys[0], month_keys[0]) + ' – ' + month_labels.get(month_keys[-1], month_keys[-1])

def qarma_empty():
    return {'sample_qty': 0, 'defects': 0, 'rate': 0, 'inspections': 0, 'orders_checked': 0, 'orders_with_reinspection': 0, 'rejected_orders': 0, 'order_rate': 0}

def factory_rows_for_months(month_keys):
    qstats = load_qarma_stats_scoped(month_keys)
    rows = []
    month_set = set(month_keys)
    for fd in factory_monthly_data:
        vols = [fd.get('volumes', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('volumes', []))]
        orders = [fd.get('orders', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('orders', []))]
        defs = [fd.get('defects', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('defects', []))]
        def_orders = [fd.get('defect_orders', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('defect_orders', []))]
        remake_orders = [fd.get('remake_orders', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('remake_orders', []))]
        remake_checked = [fd.get('remake_orders_checked_by_qarma', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('remake_orders_checked_by_qarma', []))]
        remake_unchecked = [fd.get('remake_orders_not_checked_by_qarma', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('remake_orders_not_checked_by_qarma', []))]
        remake_qty = [fd.get('remake_qty', [])[i] for i, m in enumerate(all_months_sorted) if m in month_set and i < len(fd.get('remake_qty', []))]
        vol = sum(vols); ords = sum(orders); defect = sum(defs); defect_orders = sum(def_orders)
        remake_orders_total = sum(remake_orders); remake_qty_total = sum(remake_qty)
        if vol == 0 and defect == 0:
            continue
        rows.append({
            'name': fd['name'], 'volume': vol, 'orders': ords, 'defects': defect,
            'defect_orders': defect_orders,
            'remake_orders': remake_orders_total,
            'remake_orders_checked_by_qarma': sum(remake_checked),
            'remake_orders_not_checked_by_qarma': sum(remake_unchecked),
            'remake_qty': remake_qty_total,
            'rate': round(defect / vol * 100, 2) if vol > 0 else 0,
            'order_rate': round(defect_orders / ords * 100, 2) if ords > 0 else 0,
            'qarma': qstats.get(fd['name'], qarma_empty()),
            'monthly': {'volumes': vols, 'defects': defs, 'orders': orders, 'defect_orders': def_orders, 'remake_qty': remake_qty, 'remake_orders': remake_orders}
        })
    rows.sort(key=lambda x: -x['rate'])
    return rows

def build_groupings_for_months(month_keys):
    month_set = set(month_keys)
    sku_groups = defaultdict(empty_group)
    sport_groups = defaultdict(empty_group)
    admin_groups = defaultdict(empty_group)
    category_groups = defaultdict(empty_group)
    for ono, meta in all_order_meta.items():
        if meta.get('month') not in month_set:
            continue
        text = ' | '.join(meta.get('texts', []))
        is_remake = ono in REMAKE_ORDERS
        series, _sublimated = classify_order_series_from_text(text)
        for ser in rollup_sku_groups(series):
            sku_groups[ser]['volume'] += meta['qty']
            sku_groups[ser]['orders_set'].add(ono)
            if is_remake:
                sku_groups[ser]['remake_orders_set'].add(ono)
                sku_groups[ser]['remake_qty_total'] += meta['qty']
        for sport in classify_sport_text(text):
            sport_groups[sport]['volume'] += meta['qty']
            sport_groups[sport]['orders_set'].add(ono)
            if is_remake:
                sport_groups[sport]['remake_orders_set'].add(ono)
                sport_groups[sport]['remake_qty_total'] += meta['qty']
        admin = meta.get('admin') or '(unknown)'
        admin_groups[admin]['volume'] += meta['qty']
        admin_groups[admin]['orders_set'].add(ono)
        if is_remake:
            for credit_admin, credit_qty, credit_key in remake_backend_actions.remake_admin_allocations(ono, admin, meta.get('qty', 0)):
                if credit_admin != admin:
                    admin_groups[credit_admin]['volume'] += credit_qty
                    admin_groups[credit_admin]['orders_set'].add(credit_key)
                admin_groups[credit_admin]['remake_orders_set'].add(credit_key)
                admin_groups[credit_admin]['remake_qty_total'] += credit_qty
    for ono, d in order_lookup.items():
        if d.get('fu_month') not in month_set:
            continue
        meta = all_order_meta.get(ono, {})
        text = ' | '.join(meta.get('texts', []))
        series, _sublimated = classify_order_series_from_text(text)
        for ser in rollup_sku_groups(series):
            sku_groups[ser]['defects'] += d.get('affected', 0)
            sku_groups[ser]['defect_orders_set'].add(ono)
        for sport in classify_sport_text(text):
            sport_groups[sport]['defects'] += d.get('affected', 0)
            sport_groups[sport]['defect_orders_set'].add(ono)
        admin = meta.get('admin') or '(unknown)'
        admin_groups[admin]['defects'] += d.get('affected', 0)
        admin_groups[admin]['defect_orders_set'].add(ono)
        cat = d.get('category') or ISSUE_CATEGORIES.get(ono, {}).get('category', 'Uncategorized')
        if cat != 'Uncategorized' or ono in ISSUE_CATEGORIES:
            category_groups[cat]['volume'] += meta.get('qty', 0)
            category_groups[cat]['orders_set'].add(ono)
            category_groups[cat]['defects'] += d.get('affected', 0)
            category_groups[cat]['defect_orders_set'].add(ono)
    qstats_order = load_qarma_order_stats(month_keys)
    for ono, q in qstats_order.items():
        meta = all_order_meta.get(ono)
        if not meta or meta.get('month') not in month_set:
            continue
        text = ' | '.join(meta.get('texts', []))
        series, _sublimated = classify_order_series_from_text(text)
        admin = meta.get('admin') or '(unknown)'
        for ser in rollup_sku_groups(series):
            sku_groups[ser]['qarma']['sample_qty'] += q.get('sample_qty', 0)
            sku_groups[ser]['qarma']['defects'] += q.get('defects', 0)
            sku_groups[ser]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
            sku_groups[ser]['qarma']['inspections'] += q.get('inspections', 0)
            sku_groups[ser]['qarma_order_set'].add(ono)
        for sport in classify_sport_text(text):
            sport_groups[sport]['qarma']['sample_qty'] += q.get('sample_qty', 0)
            sport_groups[sport]['qarma']['defects'] += q.get('defects', 0)
            sport_groups[sport]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
            sport_groups[sport]['qarma']['inspections'] += q.get('inspections', 0)
            sport_groups[sport]['qarma_order_set'].add(ono)
        admin_groups[admin]['qarma']['sample_qty'] += q.get('sample_qty', 0)
        admin_groups[admin]['qarma']['defects'] += q.get('defects', 0)
        admin_groups[admin]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
        admin_groups[admin]['qarma']['inspections'] += q.get('inspections', 0)
        admin_groups[admin]['qarma_order_set'].add(ono)
        cat = ISSUE_CATEGORIES.get(ono, {}).get('category')
        if cat:
            category_groups[cat]['qarma']['sample_qty'] += q.get('sample_qty', 0)
            category_groups[cat]['qarma']['defects'] += q.get('defects', 0)
            category_groups[cat]['qarma']['rejected_orders'] += q.get('rejected_orders', 0)
            category_groups[cat]['qarma']['inspections'] += q.get('inspections', 0)
            category_groups[cat]['qarma_order_set'].add(ono)
    return {'sku': finalize_groups(sku_groups), 'sport': finalize_groups(sport_groups), 'admin': finalize_groups(admin_groups), 'category': finalize_groups(category_groups)}

def build_exception_leaders(month_keys):
    month_set = set(month_keys)
    def empty_person():
        return {'orders_set': set(), 'remake_orders_set': set(), 'qty': 0, 'remake_qty': 0}
    groups = {'admin': defaultdict(empty_person), 'designer': defaultdict(empty_person)}
    for ono, meta in all_order_meta.items():
        if meta.get('month') not in month_set:
            continue
        is_remake = ono in REMAKE_ORDERS
        qty = meta.get('qty', 0) or 0
        admin_name = str(meta.get('admin') or '(unknown)').strip()
        designers = meta.get('designers') if isinstance(meta.get('designers'), (set, list, tuple)) else []
        people_by_mode = {'admin': [admin_name] if admin_name and admin_name != '(unknown)' else [],
                          'designer': sorted(designers)}
        for mode, names in people_by_mode.items():
            for name in names:
                if not name or name == '(unknown)':
                    continue
                g = groups[mode][name]
                g['orders_set'].add(ono)
                g['qty'] += qty
                if is_remake and mode != 'admin':
                    g['remake_orders_set'].add(ono)
                    g['remake_qty'] += qty
        if is_remake:
            for credit_admin, credit_qty, credit_key in remake_backend_actions.remake_admin_allocations(ono, admin_name, qty):
                if not credit_admin or credit_admin == '(unknown)':
                    continue
                g = groups['admin'][credit_admin]
                if credit_admin != admin_name:
                    g['orders_set'].add(credit_key)
                    g['qty'] += credit_qty
                g['remake_orders_set'].add(credit_key)
                g['remake_qty'] += credit_qty
    out = {}
    for mode, people in groups.items():
        rows = []
        for name, g in people.items():
            orders = len(g['orders_set'])
            remake_orders = len(g['remake_orders_set'])
            if orders <= 0:
                continue
            rows.append({
                'name': name,
                'orders': orders,
                'remake_orders': remake_orders,
                'remake_qty': g['remake_qty'],
                'rate': round(remake_orders / orders * 100, 2),
            })
        if mode == 'designer':
            # Teams/OneDrive production-upload coverage is sparse, so keep lower-volume designers visible.
            qualified = rows
        else:
            qualified = [r for r in rows if r['orders'] >= 10]
            if len(qualified) < 3:
                qualified = [r for r in rows if r['orders'] >= 5]
            if len(qualified) < 3:
                qualified = rows
        qualified.sort(key=lambda r: (r['rate'], r['remake_orders'], -r['orders'], r['name']))
        out[mode] = qualified[:10]
    return out


def build_period_payload(key, display_name):
    mkeys = period_months_for(key)
    labels = [month_labels.get(m, m) for m in mkeys]
    vol = [total_monthly.get(m, {}).get('qty', 0) for m in mkeys]
    ords = [total_monthly.get(m, {}).get('orders', 0) for m in mkeys]
    defs = [monthly_defects.get(m, 0) for m in mkeys]
    def_orders = [defect_order_count.get(m, 0) for m in mkeys]
    remake_orders = [remake_by_month.get(m, {}).get('orders', 0) for m in mkeys]
    remake_qty = [remake_by_month.get(m, {}).get('qty', 0) for m in mkeys]
    rates = [round(defs[i] / vol[i] * 100, 2) if vol[i] > 0 else 0 for i in range(len(mkeys))]
    total_vol = sum(vol); total_orders_p = sum(ords); total_defs = sum(defs); total_def_orders = sum(def_orders)
    total_remake_orders_p = sum(remake_orders); total_remake_qty_p = sum(remake_qty)
    rows = factory_rows_for_months(mkeys)
    # Prev period
    prev = {}
    all_ord = all_months_sorted
    ci = {m:i for i,m in enumerate(all_ord)}
    n = len(mkeys)
    if n > 1 and n in (3,6):
        start_m = mkeys[0]
        si = ci.get(start_m, -1)
        if si >= n:
            pm = all_ord[si-n:si]
            pv = [total_monthly.get(m,{}).get('qty',0) for m in pm]
            po = [total_monthly.get(m,{}).get('orders',0) for m in pm]
            pd2 = [monthly_defects.get(m,0) for m in pm]
            pdo = [defect_order_count.get(m,0) for m in pm]
            pro = [remake_by_month.get(m,{}).get('orders',0) for m in pm]
            prq = [remake_by_month.get(m,{}).get('qty',0) for m in pm]
            pr = [round(pd2[i]/pv[i]*100,2) if pv[i]>0 else 0 for i in range(len(pm))]
            total_v = sum(pv); total_o = sum(po); total_d = sum(pd2); total_do = sum(pdo)
            prev = {
                'months': [month_labels.get(m,m) for m in pm],
                'monthKeys': pm,
                'monthlyVolume': pv, 'monthlyRate': pr,
                'monthlyDefects': pd2, 'monthlyOrders': po, 'monthlyDefectOrders': pdo,
                'monthlyRemakeOrders': pro, 'monthlyRemakeQty': prq,
                'totalVolume': total_v, 'totalOrders': total_o,
                'totalDefects': total_d, 'totalDefectOrders': total_do,
                'totalRemakeOrders': sum(pro), 'totalRemakeQty': sum(prq),
                'totalRate': round(total_d/total_v*100,2) if total_v>0 else 0,
                'totalOrderRate': round(total_do/total_o*100,2) if total_o>0 else 0,
                'factories': factory_rows_for_months(pm),
                'groupings': build_groupings_for_months(pm),
                'exceptionLeaders': build_exception_leaders(pm),
            }
    return {
        'key': key, 'name': display_name, 'label': period_label(mkeys), 'monthKeys': mkeys, 'months': labels,
        'monthlyVolume': vol, 'monthlyOrders': ords, 'monthlyDefects': defs, 'monthlyDefectOrders': def_orders, 'monthlyRate': rates,
        'monthlyRemakeOrders': remake_orders, 'monthlyRemakeQty': remake_qty,
        'totalVolume': total_vol, 'totalOrders': total_orders_p, 'totalDefects': total_defs, 'totalDefectOrders': total_def_orders,
        'totalRemakeOrders': total_remake_orders_p, 'totalRemakeQty': total_remake_qty_p,
        'totalRate': round(total_defs / total_vol * 100, 2) if total_vol > 0 else 0,
        'totalOrderRate': round(total_def_orders / total_orders_p * 100, 2) if total_orders_p > 0 else 0,
        'factories': rows,
        'groupings': build_groupings_for_months(mkeys),
        'exceptionLeaders': build_exception_leaders(mkeys),
        'prev': prev,
    }

PERIOD_DEFS = [
    ('all', 'All'),
    ('last_3', 'Last 3 months'),
    ('last_6', 'Last 6 months'),
    ('last_month', 'Last month'),
    ('mtd', 'MTD'),
    ('ytd', 'YTD'),
    ('quarter', 'Quarter'),
]
PERIODS = {k: build_period_payload(k, label) for k, label in PERIOD_DEFS}
PERIODS_JSON = json.dumps(PERIODS, cls=factory_data.DecimalEncoder)
PERIODS_JSON_SAFE = PERIODS_JSON.replace('<', '\\\\u003C').replace('>', '\\\\u003E')

# ── Remake Management data ──
# Primary source: exported by-admin remake report from iCloud Desktop, committed as JSON
# so GitHub Actions/Fly deploys do not depend on local iCloud files.
remake_json_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'remake_report_by_admin.json')
if os.path.exists(remake_json_path):
    with open(remake_json_path, 'r', encoding='utf-8') as f:
        REMAKE_MGMT = json.load(f)
else:
    # Fallback only: raw remake orders from backend, without category/comment/flag context.
    remake_cur = conn.cursor()
    remake_cur.execute("""
SELECT o.order_no,
       COALESCE(SUM(COALESCE(NULLIF(product->'prices'->>'total_quantity', '')::numeric, 0)), 0)::int as qty,
       COALESCE(u.name, u.email::text, '(unknown)') AS admin_name,
       to_char(o.created_at, 'YYYY-MM') as month,
       COALESCE(string_agg(DISTINCT oi.factory_name, ', ' ORDER BY oi.factory_name), '(unknown)') as factories
FROM orders o
LEFT JOIN users u ON u.id = o.order_administrator_id
LEFT JOIN order_items oi ON oi.order_id = o.id AND oi.deleted_at IS NULL
LEFT JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(oi.factory_products) = 'array' THEN oi.factory_products ELSE '[]'::jsonb END) AS product ON TRUE
WHERE o.order_type_symbol IN ('R', 'Ri')
  AND o.created_at >= %s
  AND o.created_at < %s
  AND o.deleted_at IS NULL
GROUP BY o.order_no, u.name, u.email, o.created_at
ORDER BY qty DESC
""", (factory_data.REPORT_START, factory_data.REPORT_END))
    REMAKE_MGMT = [{"order": str(r[0]), "qty": int(r[1]) if r[1] else 0, "admin": r[2], "month": str(r[3])[:7] if r[3] else "?", "factory": r[4], "category": "", "comment": "", "flag": ""} for r in remake_cur.fetchall()]
    remake_cur.close()

# The committed by-admin export carries the human annotations, but can lag the
# backend. Add any newer backend remake orders without disturbing saved notes.
remake_cur = conn.cursor()
remake_cur.execute("""
SELECT o.order_no,
       COALESCE(SUM(COALESCE(NULLIF(product->'prices'->>'total_quantity', '')::numeric, 0)), 0)::int,
       COALESCE(u.name, u.email::text, '(unknown)'),
       to_char(o.created_at, 'YYYY-MM'),
       COALESCE(string_agg(DISTINCT oi.factory_name, ', ' ORDER BY oi.factory_name), '(unknown)')
FROM orders o
LEFT JOIN users u ON u.id = o.order_administrator_id
LEFT JOIN order_items oi ON oi.order_id = o.id AND oi.deleted_at IS NULL
LEFT JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(oi.factory_products) = 'array' THEN oi.factory_products ELSE '[]'::jsonb END) AS product ON TRUE
WHERE o.order_type_symbol IN ('R', 'Ri')
  AND o.created_at >= %s
  AND o.created_at < %s
  AND o.deleted_at IS NULL
GROUP BY o.order_no, u.name, u.email, o.created_at
""", (factory_data.REPORT_START, factory_data.REPORT_END))
existing_remake_orders = {str(r.get('order') or '').replace('#', '').strip() for r in REMAKE_MGMT}
for _row in remake_cur.fetchall():
    _order = str(_row[0])
    if _order in existing_remake_orders:
        continue
    REMAKE_MGMT.append({
        "order": _order,
        "qty": int(_row[1]) if _row[1] else 0,
        "admin": _row[2],
        "month": str(_row[3])[:7] if _row[3] else "?",
        "factory": _row[4],
        "category": "",
        "comment": "",
        "flag": "",
    })
remake_cur.close()

# Add externally identified missing remake/issues to the management work queue.
_missing_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'remakes_missing_from_lars.json')
if os.path.exists(_missing_path):
    with open(_missing_path, 'r', encoding='utf-8') as _f:
        _missing_rows = json.load(_f)
    _existing = {str(r.get('order') or '').replace('#', '').strip() for r in REMAKE_MGMT}
    for _m in _missing_rows:
        _mo = str(_m.get('order') or '').replace('#', '').strip()
        if not _mo or _mo in _existing:
            continue
        REMAKE_MGMT.append({
            'order': _mo, 'qty': int(_m.get('qty') or 0), 'customer': '(unknown)',
            'admin': _m.get('admin') or '(unknown)', 'factory': '(unknown)', 'month': '?',
            'category': '', 'culprit': '', 'comment': '', 'original_order': '', 'flag': '',
            'source': _m.get('source') or 'External list',
            'verification_status': 'Needs verification' if _m.get('needs_verification') else 'Confirmed portal remake',
        })
        _existing.add(_mo)

# Add Bronze customer/company names to every remake row.
_remake_customer_cur = conn.cursor()
_remake_customer_cur.execute("""
SELECT o.order_no, COALESCE(NULLIF(co.company_name, ''), NULLIF(BTRIM(CONCAT_WS(' ', c.first_name, c.last_name)), ''), '(unknown)')
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id
LEFT JOIN companies co ON co.id = c.company_id
WHERE o.order_type_symbol IN ('R', 'Ri') AND o.deleted_at IS NULL
""")
_remake_customer_by_order = {str(r[0]).replace('#', '').strip(): str(r[1] or '(unknown)') for r in _remake_customer_cur.fetchall()}
_remake_customer_cur.close()
for _r in REMAKE_MGMT:
    _r['customer'] = _remake_customer_by_order.get(str(_r.get('order') or '').replace('#', '').strip(), _r.get('customer') or '(unknown)')

# Infer original order references from existing comments, without replacing manual values.
_original_order_re = re.compile(r'(?:\b(?:reorder|remake|replacement)\b[^#\n]{0,80}?|\b(?:repeat order|same issue as|from order|for order|of order)\b[^#\n]{0,40}?|\border\b\s+)(?:#\s*)?(\d{4,})', re.IGNORECASE)
for _r in REMAKE_MGMT:
    if str(_r.get('original_order') or '').strip():
        continue
    _match = _original_order_re.search(str(_r.get('comment') or ''))
    if _match:
        _r['original_order'] = _match.group(1)

# Apply permanent factory exclusions to this management tab as well.
def _remake_factory_is_excluded(factory_text):
    return any(factory_data.is_excluded_factory(part.strip()) for part in str(factory_text or '').split(','))
REMAKE_MGMT = [r for r in REMAKE_MGMT if not _remake_factory_is_excluded(r.get('factory'))]

for _r in REMAKE_MGMT:
    _order = str(_r.get('order') or '').replace('#', '').strip()
    _note = remake_backend_actions.admin_action_note(_order)
    if not _note:
        continue
    _existing_comment = str(_r.get('comment') or '').strip()
    _r['comment'] = (_note + (' · ' + _existing_comment if _existing_comment else ''))[:800]
    if _order in remake_backend_actions.CANCELLED:
        _r['flag'] = 'Cancelled'
    elif _order in remake_backend_actions.NOT_REMAKE:
        _r['flag'] = 'Not remake'
    elif _order in remake_backend_actions.ADMIN_CHANGES:
        _r['flag'] = 'Admin changed'
# ── QC rejection work queue ──
# One row per eligible rejected order; repeated item/report rows are aggregated.
_qc_rejection_groups = {}
_qc_final_approved_orders = set()
_qc_final_approval_details = {}
_qc_reinspection_details = {}
for _row in load_qarma_rows():
    _row_order = str(_row.get('Order number') or '').strip()
    if is_qarma_final_candidate(_row):
        if _row_order and str(_row.get('Reinspection of') or '').strip():
            _reinspect_id = str(_row.get('Report inspection id') or _row.get('Inspection id') or _row.get('Link to report') or '').strip()
            _reinspect_date = str(_row.get('Scheduled inspection date') or _row.get('Inspection end time') or '')[:19]
            _rd = _qc_reinspection_details.setdefault(_row_order, {'ids': set(), 'results': set(), 'latest_date': ''})
            if _reinspect_id: _rd['ids'].add(_reinspect_id)
            _rd['results'].add(str(_row.get('Conclusion') or '').strip() or 'Unknown')
            if _reinspect_date > _rd['latest_date']: _rd['latest_date'] = _reinspect_date
        if str(_row.get('Conclusion') or '').strip() == 'Approved' and _row_order:
            _approval_date = str(_row.get('Scheduled inspection date') or _row.get('Inspection end time') or '')[:19]
            _is_reinspection = bool(str(_row.get('Reinspection of') or '').strip())
            _approval_type = 'Approved reinspection' if _is_reinspection else 'Approved final inspection'
            _existing = _qc_final_approval_details.get(_row_order)
            if not _existing or _approval_date > _existing.get('date', ''):
                _qc_final_approval_details[_row_order] = {'date': _approval_date, 'type': _approval_type}
            if not _is_reinspection:
                _qc_final_approved_orders.add(_row_order)
    if not is_qarma_included(_row):
        continue
    if str(_row.get('Conclusion') or '').strip() != 'Rejected':
        continue
    _order = _row_order
    if not _order:
        continue
    _date = _row.get('Scheduled inspection date') or _row.get('Inspection end time') or ''
    _key = _order
    _g = _qc_rejection_groups.setdefault(_key, {
        'order': _order,
        'backend_id': '',
        'month': dt_to_month(_date) or '?',
        'date': str(_date)[:19],
        'factory': norm_qarma_supplier(_row.get('Supplier name')),
        'items': set(),
        'inspectors': set(),
        'comments': [],
        'inspections': set(),
        'inspection_quantities': {},
        'severities': set(),
        'total_qty': 0,
        'sample_qty': 0,
        'defects_qty': 0,
        'final_approved': False,
        'final_approval_type': '',
        'final_approval_date': '',
        'reinspection_status': 'No',
        'reinspection_count': 0,
        'latest_reinspection_date': '',
        'shipped': False,
        'shipping_date': '',
        'tracking_no': '',
        'tracking_link': '',
        'error_type': '',
        'avoidance_action': '',
        'work_comment': '',
    })
    for _severity, _field in (
        ('Minor', 'Minor defects pieces affected'),
        ('Major', 'Major defects pieces affected'),
        ('Critical', 'Critical defects pieces affected'),
    ):
        if safe_int(_row.get(_field)) > 0:
            _g['severities'].add(_severity)
    if _row.get('Item name'): _g['items'].add(str(_row['Item name']).strip())
    if _row.get('Inspector name'): _g['inspectors'].add(str(_row['Inspector name']).strip())
    if _row.get('Report inspection id') or _row.get('Inspection id'):
        _g['inspections'].add(str(_row.get('Report inspection id') or _row.get('Inspection id')))
    _inspection_id = str(_row.get('Report inspection id') or _row.get('Inspection id') or _row.get('Link to report') or (_order + '|' + str(_date))).strip()
    _row_defects_qty = sum(safe_int(_row.get(k)) for k in ('Minor defects pieces affected', 'Major defects pieces affected', 'Critical defects pieces affected'))
    _inspection_qty = _g['inspection_quantities'].setdefault(_inspection_id, {'total_qty': 0, 'sample_qty': 0, 'defects_qty': 0})
    _inspection_qty['total_qty'] = max(_inspection_qty['total_qty'], safe_int(_row.get('Original total quantity')))
    _inspection_qty['sample_qty'] = max(_inspection_qty['sample_qty'], safe_int(_row.get('Actual sample quantity')))
    _inspection_qty['defects_qty'] = max(_inspection_qty['defects_qty'], _row_defects_qty)
    _g['total_qty'] = max(_g['total_qty'], _inspection_qty['total_qty'])
    _g['sample_qty'] = sum(x['sample_qty'] for x in _g['inspection_quantities'].values())
    _g['defects_qty'] = min(sum(x['defects_qty'] for x in _g['inspection_quantities'].values()), _g['total_qty']) if _g['total_qty'] > 0 else sum(x['defects_qty'] for x in _g['inspection_quantities'].values())
    if _row.get('Inspector comment'):
        _g['comments'].append(str(_row['Inspector comment']).strip())

# Check backend shipment state for the rejected order numbers.
_qc_order_numbers = list(_qc_rejection_groups)
if _qc_order_numbers:
    _qc_ship_cur = conn.cursor()
    _qc_ship_cur.execute("""
SELECT o.id, o.order_no,
       bool_or(ship.shipped_at IS NOT NULL OR oi.status::text IN ('shipped','completed') OR oi.shipping_status IS NOT NULL) AS shipped,
       max(COALESCE(ship.shipped_at, CASE WHEN oi.status::text IN ('shipped','completed') OR oi.shipping_status IS NOT NULL THEN oi.status_updated_at END)) AS shipping_date,
       string_agg(DISTINCT NULLIF(BTRIM(oi.tracking_no), ''), ' · ') AS tracking_no,
       string_agg(DISTINCT NULLIF(BTRIM(oi.tracking_link), ''), ' · ') AS tracking_link
FROM orders o
JOIN order_items oi ON oi.order_id = o.id AND oi.deleted_at IS NULL
LEFT JOIN LATERAL (SELECT MAX(COALESCE(a.created_at,a.updated_at)) AS shipped_at FROM order_item_activities a WHERE a.order_item_id=oi.id AND a.status::text='shipped') ship ON true
WHERE o.order_no = ANY(%s)
  AND o.deleted_at IS NULL
GROUP BY o.id, o.order_no
""", (_qc_order_numbers,))
    _qc_shipment_state = {str(r[1]): (str(r[0]), bool(r[2]), str(r[3])[:19] if r[3] else '', str(r[4] or ''), str(r[5] or '')) for r in _qc_ship_cur.fetchall()}
    _qc_ship_cur.close()
else:
    _qc_shipment_state = {}

QC_REJECTIONS = []
for _g in _qc_rejection_groups.values():
    _g['items'] = ', '.join(sorted(_g['items']))
    _g['inspectors'] = ', '.join(sorted(_g['inspectors']))
    _g['severities'] = ', '.join(x for x in ('Critical', 'Major', 'Minor') if x in _g['severities']) or 'Not specified'
    _g['inspections'] = len(_g['inspections'])
    _g['final_approved'] = _g['order'] in _qc_final_approval_details
    if _g['final_approved']:
        _g['final_approval_type'] = _qc_final_approval_details[_g['order']]['type']
        _g['final_approval_date'] = _qc_final_approval_details[_g['order']]['date']
    _reinspect = _qc_reinspection_details.get(_g['order'])
    if _reinspect:
        _g['reinspection_count'] = len(_reinspect['ids']) or len(_reinspect['results'])
        _g['latest_reinspection_date'] = _reinspect['latest_date']
        _g['reinspection_status'] = ' · '.join(sorted(_reinspect['results']))
    _g['backend_id'], _g['shipped'], _g['shipping_date'], _g['tracking_no'], _g['tracking_link'] = _qc_shipment_state.get(_g['order'], ('', False, '', '', ''))
    _g['qc_comment'] = ' · '.join(dict.fromkeys(x for x in _g['comments'] if x))[:1200]
    del _g['comments']
    _g.pop('inspection_quantities', None)
    QC_REJECTIONS.append(_g)
QC_REJECTIONS.sort(key=lambda r: (r.get('month') or '', int(r.get('order') or 0)), reverse=True)
REINSPECTION_SUMMARY = {
    'orders': len(_qc_reinspection_details),
    'events': sum(len(v['ids']) or len(v['results']) for v in _qc_reinspection_details.values()),
    'approved_events': sum(1 for v in _qc_reinspection_details.values() for result in v['results'] if result == 'Approved'),
    'rejected_events': sum(1 for v in _qc_reinspection_details.values() for result in v['results'] if result == 'Rejected'),
}
REINSPECTION_SUMMARY_JSON = json.dumps(REINSPECTION_SUMMARY)
QC_REJECTIONS_JSON = json.dumps(QC_REJECTIONS, cls=factory_data.DecimalEncoder).replace('<', '\\\\u003C').replace('>', '\\\\u003E')

REMAKE_MGMT_JSON = json.dumps(REMAKE_MGMT, cls=factory_data.DecimalEncoder)
REMAKE_MGMT_JSON = REMAKE_MGMT_JSON.replace('<', '\\u003C').replace('>', '\\u003E')
conn.close()

# ── Remake Mgmt SAS token for universal save ──
from azure.storage.blob import BlobServiceClient, ContentSettings, generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta, timezone
AZURE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT", "custimoolivedata")
AZURE_KEY = os.environ.get("AZURE_STORAGE_KEY", "")
def make_blob_sas_url(blob_name):
    if not AZURE_KEY:
        return ''
    sas_token = generate_blob_sas(
        account_name=AZURE_ACCOUNT,
        container_name='$web',
        blob_name=blob_name,
        account_key=AZURE_KEY,
        permission=BlobSasPermissions(read=True, write=True, create=True),
        expiry=datetime.now(timezone.utc) + timedelta(days=1),
        api_version='2021-12-02'
    )
    return f'https://{AZURE_ACCOUNT}.blob.core.windows.net/$web/{blob_name}?{sas_token}'

def ensure_shared_blob(blob_name, initial_payload):
    """Create a shared annotation blob once; never overwrite user edits during report generation."""
    if not AZURE_KEY:
        return False
    service = BlobServiceClient(account_url=f'https://{AZURE_ACCOUNT}.blob.core.windows.net', credential=AZURE_KEY)
    blob = service.get_blob_client(container='$web', blob=blob_name)
    if not blob.exists():
        blob.upload_blob(json.dumps(initial_payload), overwrite=False, content_settings=ContentSettings(content_type='application/json'))
        return True
    return False

REMAKE_SAS_URL = ''
QC_REJECTIONS_SAS_URL = ''
try:
    ensure_shared_blob('remake-mgmt-data.json', [])
    ensure_shared_blob('qc-rejections-data.json', [])
    REMAKE_SAS_URL = make_blob_sas_url('remake-mgmt-data.json')
    QC_REJECTIONS_SAS_URL = make_blob_sas_url('qc-rejections-data.json')
except Exception as e:
    print("Warning: could not initialize shared annotation blobs or generate SAS token:", e)




ORDERS_JSON_SAFE = '[]'
FU_REVIEW_JSON_SAFE = '[]'

MONTH_KEYS = json.dumps(all_months_sorted)
generation_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Custimoo — Defect Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {{
    --bg: #f5f7fb;
    --card: #ffffff;
    --text: #172033;
    --muted: #667085;
    --border: #e6eaf2;
    --accent: #1f6feb;
    --shadow: 0 10px 30px rgba(16, 24, 40, 0.08);
    --radius: 18px;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: Inter, Segoe UI, Arial, sans-serif; background: var(--bg); color: var(--text); }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
  .hero {{ background: linear-gradient(135deg, #0f172a 0%, #1d4ed8 100%); color: #fff; border-radius: 24px; padding: 26px 28px; box-shadow: var(--shadow); margin-bottom: 18px; }}
  .hero h1 {{ margin: 0 0 6px; font-size: 28px; font-weight: 800; letter-spacing: -0.5px; }}
  .hero p {{ margin: 0; color: rgba(255,255,255,0.85); font-size: 13px; }}
  .tabs {{ display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }}
  .tab {{ background: #fff; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 10px 18px; font-size: 13px; font-weight: 700; cursor: pointer; box-shadow: var(--shadow); transition: all 0.15s ease; }}
  .tab:hover {{ color: var(--accent); }}
  .tab.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .page {{ display: none; }}
  .page.active {{ display: block; }}
  .exec-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 22px; margin-bottom: 16px; }}
  .metric .label {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; }}
  .metric .value {{ font-size: 48px; font-weight: 800; line-height: 1; margin-bottom: 8px; color: var(--accent); }}
  .metric .sub {{ color: var(--muted); font-size: 12px; line-height: 1.5; }}
  .section-title {{ font-size: 18px; font-weight: 800; margin: 0 0 14px; }}
  .section-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px; }}
  .section-head h3 {{ margin: 0; }}
  .reset-btn {{ background: #f3f4f6; color: var(--muted); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer; display: none; }}
  .reset-btn:hover {{ background: #e9ecf2; color: var(--text); }}
  .reset-btn.show {{ display: inline-block; }}
  .delta {{ font-size: 10px; margin-left: 4px; vertical-align: 1px; }}
  .delta.good {{ color: #16a34a; }}
  .delta.bad {{ color: #dc2626; }}
  .delta.neutral {{ color: #667085; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ padding: 11px 10px; border-bottom: 1px solid var(--border); text-align: left; }}
  th {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); background: #fafbff; }}
  .wrap:has(#remake-mgmt.active) {{ max-width: 2400px; }}
  .wrap:has(#factory-share-page.active) {{ max-width: none; width: 100%; padding-left: 20px; padding-right: 20px; }}
  .factory-share-orders-scroll {{ overflow: visible; max-height: none; width: 100%; }}
  .factory-share-orders-scroll table {{ width: 100%; min-width: 0; table-layout: fixed; font-size: 12px; }}
  .factory-share-orders-scroll th, .factory-share-orders-scroll td {{ overflow-wrap: anywhere; word-break: normal; }}
  .factory-share-orders-scroll th:nth-child(1) {{ width: 5%; }}
  .factory-share-orders-scroll th:nth-child(2) {{ width: 9%; }}
  .factory-share-orders-scroll th:nth-child(3), .factory-share-orders-scroll th:nth-child(4), .factory-share-orders-scroll th:nth-child(5) {{ width: 9%; }}
  .factory-share-orders-scroll th:nth-child(6) {{ width: 10%; }}
  .factory-share-orders-scroll th:nth-child(7) {{ width: 6%; }}
  .factory-share-orders-scroll th:nth-child(8), .factory-share-orders-scroll th:nth-child(9), .factory-share-orders-scroll th:nth-child(10) {{ width: 7%; }}
  .factory-share-orders-scroll th:nth-child(11) {{ width: 20%; }}
  .factory-share-orders-scroll th:nth-child(12) {{ width: 6%; }}
  .factory-share-orders-scroll th:nth-child(13) {{ width: 5%; }}
  .factory-share-orders-scroll td {{ vertical-align: top; }}
  .factory-share-orders-scroll td:nth-child(11) {{ white-space: pre-wrap; }}
  .remake-mgmt-scroll .remake-table {{ min-width: 2200px; table-layout: fixed; }}
  .remake-mgmt-scroll .remake-table th:nth-child(1) {{ width: 90px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(2) {{ width: 150px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(3) {{ width: 70px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(4) {{ width: 190px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(5) {{ width: 170px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(6) {{ width: 180px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(7) {{ width: 95px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(8) {{ width: 150px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(9) {{ width: 180px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(10) {{ width: 190px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(11) {{ width: 220px; }}
  .remake-mgmt-scroll .remake-table th:nth-child(12) {{ width: 700px; }}
  .remake-mgmt-scroll .remake-edit {{ width: 100%; min-width: 0; box-sizing: border-box; }}
  .wrap:has(#qc-rejections.active) {{ max-width: 1800px; }}
  .qc-rejections-scroll {{ overflow-x: hidden; max-height: none; }}
  .qc-rejections-table, .qc-rejections-table tbody, .qc-rejections-table tr, .qc-rejections-table td {{ display: block; width: auto; }}
  .qc-rejections-table {{ min-width: 0; border-collapse: separate; border-spacing: 0 14px; table-layout: fixed; }}
  .qc-rejections-table thead {{ display: none; }}
  .qc-rejections-table tr {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--card); box-shadow: 0 1px 3px rgba(16,24,40,.06); }}
  .qc-rejections-table td {{ min-height: 48px; padding: 9px 12px 9px 118px; position: relative; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; vertical-align: top; }}
  .qc-rejections-table td::before {{ content: attr(data-label); position: absolute; left: 12px; top: 10px; width: 96px; font-size: 10px; line-height: 1.2; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
  .qc-rejections-table td:nth-last-child(-n+4) {{ border-bottom: 0; }}
  .qc-rejections-table .qc-comment {{ width: auto; min-width: 0; max-width: none; white-space: pre-wrap; }}
  .qc-rejections-table td:nth-child(5), .qc-rejections-table td:nth-child(18), .qc-rejections-table td:nth-child(19) {{ grid-column: 1 / -1; }}
  .qc-rejections-table .qc-edit {{ width: 100%; min-width: 0; max-width: 100%; box-sizing: border-box; }}
  .qc-rejections-table textarea.qc-avoidance {{ min-height: 72px; resize: vertical; font: inherit; padding: 8px; }}
  @media (max-width: 1400px) {{
    .wrap:has(#qc-rejections.active) {{ max-width: 100%; }}
    .qc-rejections-table tr {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .qc-rejections-table td:nth-last-child(-n+2) {{ border-bottom: 0; }}
  }}
  @media (max-width: 700px) {{
    .qc-rejections-table tr {{ grid-template-columns: 1fr; }}
    .qc-rejections-table td, .qc-rejections-table td:nth-child(5), .qc-rejections-table td:nth-child(15), .qc-rejections-table td:nth-child(16) {{ grid-column: auto; }}
  }}
  tr.clickable {{ cursor: pointer; transition: background 0.12s ease; }}
  tr.clickable:hover {{ background: #f0f6ff; }}
  tr.selected {{ background: #eaf2ff !important; box-shadow: inset 4px 0 0 var(--accent); }}
  .right {{ text-align: right; }}
  .pct-pill {{ display: inline-block; padding: 4px 12px; border-radius: 999px; font-weight: 700; font-size: 13px; cursor: pointer; transition: opacity 0.15s; }}
  .pct-pill:hover {{ opacity: 0.7; }}
  .pct-high {{ background: #fef3f2; color: #b42318; }}
  .pct-mid {{ background: #fffaeb; color: #b54708; }}
  .pct-low {{ background: #ecfdf3; color: #027a48; }}
  .chart-wrap {{ position: relative; height: 300px; width: 100%; }}
  .footnote {{ color: var(--muted); font-size: 12px; margin-top: 8px; font-style: italic; }}
  .trend-bar {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; padding: 10px 14px; background: #f9fafb; border-radius: 10px; flex-wrap: wrap; }}
  .trend-label {{ font-size: 13px; color: var(--muted); }}
  .trend-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 999px; font-weight: 700; font-size: 13px; }}
  .trend-up {{ background: #fef3f2; color: #b42318; }}
  .trend-down {{ background: #ecfdf3; color: #027a48; }}
  .trend-flat {{ background: #f3f4f6; color: #475467; }}
  .pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 12px; }}
  .status-good {{ background: #ecfdf3; color: #027a48; }}
  .status-warn {{ background: #fffaeb; color: #b54708; }}
  .status-bad {{ background: #fef3f2; color: #b42318; }}
  .status-neutral {{ background: #f2f4f7; color: #344054; }}
  .factory-name {{ font-weight: 700; font-size: 16px; color: var(--text); }}
  .hint {{ font-size: 12px; color: var(--accent); margin-bottom: 10px; }}
  ul.clean {{ margin: 0; padding-left: 20px; line-height: 1.9; font-size: 14px; }}
  .in-progress {{ color: #b54708; font-weight: 600; }}
  
  /* Drill-down panel */
  .drill-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1000; align-items: flex-start; justify-content: center; padding-top: 40px; }}
  .drill-overlay.show {{ display: flex; }}
  .drill-panel {{ background: var(--card); border-radius: var(--radius); box-shadow: 0 20px 60px rgba(0,0,0,0.25); width: 90%; max-width: 780px; max-height: 85vh; overflow-y: auto; padding: 24px; }}
  .drill-title {{ font-size: 18px; font-weight: 800; margin-bottom: 14px; }}
  .drill-close {{ float: right; background: #f3f4f6; border: none; border-radius: 8px; padding: 6px 12px; font-size: 13px; font-weight: 600; cursor: pointer; }}
  .drill-close:hover {{ background: #e5e7eb; }}
  .drill-count {{ font-size: 13px; color: var(--muted); margin-bottom: 12px; }}
  .drill-table {{ font-size: 13px; }}
  .drill-table td {{ padding: 8px 8px; vertical-align: top; }}
  .drill-table .order-num {{ font-weight: 600; color: var(--accent); }}
  .drill-snippet {{ font-size: 11px; color: var(--muted); line-height: 1.4; max-height: 40px; overflow: hidden; }}
  @media (max-width: 700px) {{ .exec-grid {{ grid-template-columns: 1fr; }} .wrap {{ padding: 16px; }} .metric .value {{ font-size: 38px; }} }}
</style>
</head>
<body>
<div id="refresh-bar" style="text-align:center;padding:8px 16px;background:#1a1a2e;border-bottom:1px solid #2a2a4e;font-size:13px;color:#aaa;">
  <span id="last-update">Last updated: {generation_time}</span>
  <button id="refresh-btn" onclick="doRefresh()" style="margin-left:12px;padding:4px 16px;background:#0f3460;color:white;border:1px solid #16213e;border-radius:4px;cursor:pointer;">Refresh Report</button>
  <a href="/dqc" style="margin-left:12px;color:#4ecca3;font-weight:700;text-decoration:none;">Open Digital QC Usage</a>
  <span id="refresh-msg" style="margin-left:10px;"></span>
</div>
<script>
async function doRefresh(){{var b=document.getElementById('refresh-btn'),m=document.getElementById('refresh-msg');b.disabled=!0;b.textContent='Refreshing...';m.textContent='';try{{var r=await fetch('/api/refresh'),d=await r.json();if(d.ok){{m.textContent='✓ '+d.message;m.style.color='#4ecca3';var t=0,c=setInterval(async()=>{{var s=await fetch('/api/status'),sd=await s.json();if(sd.conclusion==='success'){{clearInterval(c);location.reload();}}if(++t>60)clearInterval(c);}},2e3)}}else{{m.textContent='✗ '+(d.error||'Failed');m.style.color='#e94560'}}}}catch(e){{m.textContent='✗ Network error';m.style.color='#e94560'}}b.disabled=!1;b.textContent='Refresh Report'}}
</script>
<div class="wrap">
  <div class="hero">
    <h1>Custimoo — Defect Report</h1>
    <p>Reporting Period: {report_month_labels[0]} – {report_month_labels[-1]} ({report_month_labels[-1]} still in progress)</p>
  </div>
  <div class="tabs">
    <button class="tab active" data-target="summary">Summary</button>
    <button class="tab" data-target="ytd">YTD 2026</button>
    <button class="tab" data-target="details">Details</button>
    <button class="tab" data-target="methodology">Methodology</button>
    <button class="tab" data-target="remake-mgmt">Remake Mgmt</button>
    <button class="tab" data-target="remake-analysis">Remake Analysis</button>
    <button class="tab" data-target="qc-rejections">QC Rejections</button>
    <button class="tab" data-target="qc-analysis">QC Analysis</button>
    <button class="tab" data-target="dqc-usage">DQC Usage</button>
  </div>
  <section id="summary" class="page active">
    <div class="exec-grid">
      <div class="card metric"><div class="label">3-Month Rolling Remake Rate</div><div class="value" id="rollingRate"></div><div class="sub" id="rollingSub"></div></div>
      <div class="card metric"><div class="label" id="selectedRateLabel">Selected Period Remake Rate</div><div class="value" id="totalRate"></div><div class="sub" id="totalSub"></div></div>
    </div>
    <div class="card metric"><div class="label" id="goalRateLabel">2026 Goal — Remake QTY Error Rate</div><div class="value" id="goalRate">0.50%</div><div class="sub" id="goalSub">Goal for 2026: ≤0.50% remake-qty error rate.</div></div>
    <div class="card">
      <div class="section-head"><h3 class="section-title" id="breakdownTitle">Remake / Qarma Breakdown — Factories</h3><div style="display:flex;align-items:center;gap:8px;margin:0"><label for="periodFilter" class="muted" style="font-size:13px;font-weight:700">Period:</label><select id="periodFilter" class="filter-select"><option value="all">All</option><option value="last_3">Last 3 months</option><option value="last_6">Last 6 months</option><option value="last_month">Last month</option><option value="mtd">MTD</option><option value="ytd">YTD</option><option value="quarter">Quarter</option></select><label for="measureFilter" class="muted" style="font-size:13px;font-weight:700">Measure:</label><select id="measureFilter" class="filter-select"><option value="qty">Qty</option><option value="orders">No of Orders</option></select><label for="breakdownFilter" class="muted" style="font-size:13px;font-weight:700">Filter:</label><select id="breakdownFilter" class="filter-select"><option value="all">All</option><option value="factory">Factories</option><option value="sku">SKU</option><option value="sport">Sports</option><option value="category">Category</option><option value="admin">Order Admin</option></select></div></div>
      <div class="hint" id="breakdownHint">Factory view combines backend remake data with Qarma physical QC catch data from the live daily CSV export.</div>
      <table id="factoryTable"><thead><tr><th>Factory</th><th class="right">Total Order QTY</th><th class="right">Qarma QTY Checked</th><th class="right">Qarma QC Coverage%</th><th class="right">Qarma Defects QTY</th><th class="right">Remake QTY</th><th class="right">Remake QTY Err%</th><th class="right">Qarma Err%</th><th class="right">Qarma QC to 0.5% / 0.2%</th></tr></thead><tbody id="factoryBody"></tbody></table>
    </div>
    <div class="card" id="actionPlanCard">
      <h3 class="section-title">Actionplan Sanity Check</h3>
      <div class="hint" id="actionPlanSummary">Qarma coverage is only one lever. Remake QTY error also includes QC escapes and issues physical QC cannot catch. Silver-Star Group and Rajco have only recently started on-site QC, so their Qarma sanity-check interpretation is marked as more data needed.</div>
      <ul class="clean">
        <li><strong>Coverage gap → Unchecked QTY:</strong> the table shows how much shipped quantity was not checked in Qarma. 100% QC closes this blind spot; the <strong>Implied Unchecked Err%</strong> estimates how much remake pressure sits in that unchecked quantity.</li>
        <li><strong>QC escape rate → Ratio vs Qarma:</strong> when <strong>Implied Unchecked Err%</strong> is much higher than <strong>Qarma Err%</strong>, coverage alone is unlikely to explain the remakes. It points to defects missed by physical QC and requires tighter AQL/sampling, checklist calibration, and factory RCA.</li>
        <li><strong>Non-QC-catchable issues → Interpretation:</strong> delays, paperwork/admin errors, packing/transit/rework issues, and other non-physical-QC remake causes can inflate Remake QTY Err% even if Qarma coverage improves. These are called out in the interpretation instead of being treated as solvable by more checks.</li>
      </ul>
      <table><thead><tr><th>Factory</th><th class="right">Qarma Err%</th><th class="right">Unchecked QTY</th><th class="right">Implied Unchecked Err%</th><th class="right">Ratio vs Qarma</th><th>Interpretation</th></tr></thead><tbody id="actionPlanDiagnosticsBody"></tbody></table>
      <div class="footnote">Unchecked QTY = Total Order QTY − Qarma QTY Checked. Implied unchecked err% = Remake QTY / Unchecked QTY. This is a directional sanity check, not proof of cause: remakes can include QC misses and non-QC-catchable errors.</div>
    </div>
    <div class="card">
      <div class="section-head"><div><h3 class="section-title" id="exceptionLeadersTitle">Exception Leaders — Fewest Remake Orders</h3><div class="hint" id="exceptionLeadersSub">Based on selected period. Exception rate = remake orders / total orders.</div></div></div>
      <div class="exec-grid">
        <div>
          <h4 style="margin:0 0 10px">🏆 Order Admins</h4>
          <table><thead><tr><th>Rank</th><th>Order Admin</th><th class="right">Exception Rate</th><th class="right">Remake Orders</th><th class="right">Total Orders</th></tr></thead><tbody id="adminLeadersBody"></tbody></table>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="section-head"><h3 class="section-title" id="trendTitle">Monthly Trend &mdash; All Factories</h3><button class="reset-btn" id="resetBtn">&larr; Show all factories</button></div>
      <div class="trend-bar" id="trendBar" style="display:none">
        <span class="factory-name" id="trendFactory"></span>
        <span class="trend-label">Trend (Oct &rarr; Apr):</span>
        <span class="trend-pill" id="trendPill"></span>
        <span class="trend-label" id="trendDelta"></span>
      </div>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
      <div class="footnote">Click any bar or line point to drill into the orders for that month · {report_month_labels[-1]} still in progress</div>
    </div>
  </section>
  <section id="ytd" class="page">
    <div class="exec-grid">
      <div class="card metric"><div class="label">YTD 2026 Total Order QTY</div><div class="value" id="ytdVolume"></div><div class="sub">Jan – Jun 2026*</div></div>
      <div class="card metric"><div class="label" id="periodKpiLabel">YTD 2026 Remakes</div><div class="value" id="periodKpiValue"></div><div class="sub" id="periodKpiSub"></div></div>
    </div>
    <div class="card">
      <div class="section-head"><h3 class="section-title" id="ytdChartTitle">YTD Cumulative Total Order QTY</h3><div style="display:flex;align-items:center;gap:8px;margin:0"><label for="ytdMeasureFilter" class="muted" style="font-size:13px;font-weight:700">Measure:</label><select id="ytdMeasureFilter" class="filter-select"><option value="qty">Qty</option><option value="orders" selected>No of Orders</option></select></div></div>
      <div class="chart-wrap"><canvas id="ytdChart"></canvas></div>
      <div class="footnote">Blue bars show accumulated volume/orders. Red line shows accumulated remake percentage for the selected measure.</div>
    </div>
    <div class="card">
      <h3 class="section-title" id="ytdMonthlyTitle">YTD Monthly Accumulated QTY</h3>
      <table><thead><tr id="ytdMonthlyHead"></tr></thead><tbody id="ytdMonthlyBody"></tbody></table>
    </div>
    <div class="card">
      <h3 class="section-title">YTD 2026 &mdash; Per-Factory Remake Rate</h3>
      <table><thead><tr id="ytdFactoryHead"></tr></thead><tbody id="ytdFactoryBody"></tbody></table>
    </div>
  </section>
  <section id="details" class="page">
    <div class="card">
      <h3 class="section-title">Month-wise Summary</h3>
      <div class="hint">Month-wise volume, remake orders, and remake quantity.</div>
      <table><thead><tr><th>Month</th><th class="right">Total Order QTY</th><th class="right">Orders</th><th class="right">Remake Orders</th><th class="right">Remake QTY</th><th class="right">Remake / Total Order QTY</th></tr></thead><tbody id="monthlyBody"></tbody></table>
    </div>
    <div class="card">
      <h3 class="section-title" id="detailsFactoryTitle">Factory &times; Month Remake QTY</h3>
      <div class="hint" id="detailsFactoryHint">Monthly remake quantity by factory.</div>
      <table><thead><tr id="factoryMonthHead"><th>Factory</th></tr></thead><tbody id="factoryMonthBody"></tbody></table>
      <div class="footnote">{report_month_labels[-1]} still in progress</div>
    </div>
  </section>
  <section id="methodology" class="page">
    <div class="card">
      <h3 class="section-title">Included Product Groups</h3>
      <ul class="clean"><li>Jersey</li><li>Socks</li><li>Pants / Knickers</li><li>Shorts</li><li>Hoodie / Outerwear</li><li>Jacket</li><li>Shirt</li><li>Bags</li><li>Polo</li></ul>
    </div>
    <div class="card">
      <h3 class="section-title">How the Numbers Are Calculated</h3>
      <ul class="clean">
        <li>Reporting window is <strong>{report_month_labels[0]} – {report_month_labels[-1]}</strong>.</li>
        <li>Total month volume uses proper products only (excludes name plates, fight straps, logo patches, accessories).</li>
        <li>The shared report uses <strong>remake orders</strong> from the backend and <strong>Qarma physical QC</strong> from the live Qarma export.</li>
        <li>Factory comparisons use Qarma physical-QC shipment/order quantity and scheduled inspection date when a Qarma row exists; otherwise they fall back to shipped order quantity per factory from the bronze backend database, bucketed by the activity-history <strong>shipped</strong> timestamp, with <strong>status_updated_at</strong> used only as a fallback when no shipped activity exists.</li>
        <li>Qarma physical QC uses the live Qarma <strong>inspections.csv.gz</strong> export, which refreshes roughly hourly. The report run picks up the newest Qarma export automatically.</li>
        <li>Qarma sample quantity is deduplicated by <strong>Report inspection id</strong>; Qarma defects are Minor + Major + Critical defect pieces affected.</li>
        <li>Remakes are bucketed by backend order month; Qarma is bucketed by inspection month from the Qarma export.</li>
        <li>{report_month_labels[-1]} is <span class="in-progress">still in progress</span>.</li>
        <li>Click any number in the report to drill into the specific orders behind it.</li>
      </ul>
    </div>
    <div class="card">
      <h3 class="section-title">Actionplan Interpretation</h3>
      <p class="muted">The Actionplan column is not a promise that more Qarma checks alone will bring remake QTY error below 0.5%. Remake QTY error is a blend of three buckets:</p>
      <ul class="clean">
        <li><strong>Coverage gap:</strong> defects in the quantity not physically checked by Qarma. 100% QC closes this blind spot.</li>
        <li><strong>QC escape rate:</strong> defects in checked lots that physical QC missed. This requires tighter AQL/sampling limits, inspector calibration, checklist updates, and factory-side corrective-action tracking.</li>
        <li><strong>Non-QC-catchable issues:</strong> delays, paperwork/admin errors, packing/transit/rework redo, and other reasons that physical QC cannot catch by nature.</li>
      </ul>
      <p class="muted">Therefore, when Actionplan says <strong>100% QC</strong>, read it as: close the coverage gap and also investigate root cause. If the implied unchecked rate is much higher than Qarma Err%, uninspected lots look worse than checked lots. If it is much lower, Qarma sampling is likely targeted at risky lots and not representative of total production.</p>
    </div>
  </section>
  <section id="remake-mgmt" class="page">
    <div class="card">
      <h3 class="section-title">Remake Management — Order Admin Review</h3>
      <p class="muted">By-admin remake list from <strong>Remake_Report_By_Admin.html</strong>. Net excludes Cancelled, Disputed, Ex/Other Admin, and No Record rows.</p>
      <p class="hint">New unhandled orders appear first. Use the editable Category, Culprit, and Comment fields to work each order. An order is treated as handled once Category, Culprit, or Comment has been entered. Changes save automatically.</p>
      <div class="remake-filter-row" style="display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
        <label class="muted" style="font-size:13px;font-weight:700">Filter by admin:</label>
        <select id="remakeAdminFilter" class="filter-select" style="max-width:200px">
          <option value="">All admins</option>
        </select>
        <label class="muted" style="font-size:13px;font-weight:700">Filter by month:</label>
        <select id="remakeMonthFilter" class="filter-select" style="max-width:140px">
          <option value="">All months</option>
        </select>
        <span style="margin-left:auto;font-size:13px;color:var(--muted)" id="remakeCount">272 remakes</span>
        <span id="remakeSaveStatus" class="muted" style="font-size:13px">Changes save automatically</span>
      </div>
      <div class="remake-mgmt-scroll">
        <table class="remake-table"><thead>
          <tr><th>Order</th><th>Original Order</th><th class="right">QTY</th><th>Customer</th><th>Admin</th><th>Factory</th><th>Month</th><th>Source</th><th>Verification</th><th style="min-width:180px">Category</th><th style="min-width:180px">Culprit</th><th style="min-width:320px">Comment</th></tr>
        </thead><tbody id="remakeMgmtBody"></tbody></table>
      </div>
    </div>
  </section>
  <section id="remake-analysis" class="page">
    <div class="card"><h3 class="section-title">Remake Forensics — Categories and Culprits</h3><div class="hint">Orders are grouped from the shared Remake Mgmt annotations. <strong>Not yet forensically reviewed</strong> means Category, Culprit, and Comment are all empty.</div><div id="remakeAnalysisKpis" class="exec-grid"></div></div>
    <div class="card"><h3 class="section-title">Unreviewed Remake Orders</h3><div style="overflow:auto;max-height:55vh"><table><thead><tr><th>Order</th><th>Customer</th><th>QTY</th><th>Factory</th><th>Admin</th><th>Month</th></tr></thead><tbody id="remakeUnreviewedBody"></tbody></table></div></div>
    <div class="card"><h3 class="section-title">By Category</h3><table><thead><tr><th>Category</th><th class="right">Orders</th><th class="right">QTY</th></tr></thead><tbody id="remakeCategoryBody"></tbody></table></div>
    <div class="card"><h3 class="section-title">By Culprit</h3><table><thead><tr><th>Culprit</th><th class="right">Orders</th><th class="right">QTY</th></tr></thead><tbody id="remakeCulpritBody"></tbody></table></div>
  </section>
  <section id="qc-analysis" class="page">
    <div class="card"><h3 class="section-title">QC Rejection Forensics — Error Types and Prevention</h3><div class="hint">Orders are grouped from the shared QC Rejections annotations. <strong>Not yet forensically reviewed</strong> means Error Type, How to Avoid, and Work Notes are all empty.</div><div id="qcAnalysisKpis" class="exec-grid"></div></div>
    <div class="card"><h3 class="section-title">Unreviewed QC Rejections</h3><div style="overflow:auto;max-height:55vh"><table><thead><tr><th>Order</th><th>Factory</th><th>Order QTY</th><th>Defect QTY</th><th>QC Date</th></tr></thead><tbody id="qcUnreviewedBody"></tbody></table></div></div>
    <div class="card"><h3 class="section-title">By Error Type</h3><table><thead><tr><th>Error Type</th><th class="right">Orders</th><th class="right">Defect QTY</th></tr></thead><tbody id="qcErrorTypeBody"></tbody></table></div>
    <div class="card"><h3 class="section-title">Prevention Actions</h3><table><thead><tr><th>How to Avoid</th><th class="right">Orders</th></tr></thead><tbody id="qcPreventionBody"></tbody></table></div>
  </section>
  <section id="qc-rejections" class="page">
    <div class="card">
      <h3 class="section-title">QC Rejections — Mistake Prevention Work Queue</h3>
      <p class="muted">Eligible final Qarma inspections rejected by Custimoo QC. Repeated inspection/item rows are grouped by order. Use the fields to assign responsibility and record how to avoid the mistake.</p>
      <div class="hint">Attribution options are intentionally limited to <strong>Investigating</strong>, <strong>Custimoo error</strong>, <strong>Factory error</strong>, and <strong>BOTH</strong>. Severity comes from Qarma affected-piece fields. <strong>Final QC Approved</strong> is Yes when the order has an approved eligible final inspection or approved final reinspection; the approval type and date are shown. <strong>Tracking</strong> shows the backend tracking number and/or clickable tracking link; shipped orders without either show <strong>No tracking</strong>.</div>
      <div class="remake-filter-row" style="display:flex;gap:12px;align-items:center;margin:12px 0;flex-wrap:wrap">
        <span style="font-size:13px;color:var(--muted)" id="qcRejectionCount">0 rejected orders</span>
        <span id="qcRejectionSaveStatus" class="muted" style="font-size:13px;margin-left:auto">Changes save automatically</span>
      </div>
      <div class="qc-rejections-scroll">
        <table class="remake-table qc-rejections-table"><thead><tr>
          <th>Order</th><th>QC Date</th><th>Factory</th><th>Items</th><th class="qc-comment">QC Comment</th><th class="right">Order QTY</th><th class="right">QTY Checked</th><th class="right">Defect QTY</th><th>Severity</th><th>Inspector</th><th>Final QC Approved</th><th>Reinspection</th><th>Reinspection Count</th><th>Latest Reinspection Date</th><th>Shipped</th><th>Tracking</th><th style="min-width:170px">Error Type</th><th style="min-width:260px">How to Avoid</th><th style="min-width:320px">Work Notes</th>
        </tr></thead><tbody id="qcRejectionBody"></tbody></table>
      </div>
    </div>
  </section>
  <section id="dqc-usage" class="page">
    <div class="card">
      <div class="section-head"><h3 class="section-title">Digital QC Usage</h3><div style="display:flex;align-items:center;gap:8px;margin:0;flex-wrap:wrap"><label class="muted" style="font-size:13px;font-weight:700">From:</label><input id="dqcFrom" type="date" class="filter-select" style="max-width:150px"><label class="muted" style="font-size:13px;font-weight:700">To:</label><input id="dqcTo" type="date" class="filter-select" style="max-width:150px"><button class="reset-btn" id="dqcRefreshBtn">Refresh</button><button class="reset-btn" id="dqcCsvBtn">CSV</button><button class="reset-btn" id="dqcXlsxBtn">Excel</button></div></div>
      <div class="hint" id="dqcGenerated">Loads audit runs from the central DQC logging API. Each row is one plugin audit run.</div>
    </div>
    <div class="exec-grid">
      <div class="card metric"><div class="label">Total Audits</div><div class="value" id="dqcTotal">–</div><div class="sub">Selected period</div></div>
      <div class="card metric"><div class="label">PASSED</div><div class="value" id="dqcPassed">–</div><div class="sub">Audit verdicts marked passed</div></div>
      <div class="card metric"><div class="label">REJECTED</div><div class="value" id="dqcRejected">–</div><div class="sub">Audit verdicts marked rejected</div></div>
      <div class="card metric"><div class="label">Users</div><div class="value" id="dqcUsers">–</div><div class="sub">Unique users running DQC</div></div>
    </div>
    <div class="card">
      <h3 class="section-title">Per-user Count</h3>
      <table><thead><tr><th>User</th><th class="right">Audits</th></tr></thead><tbody id="dqcUserBody"><tr><td colspan="2">Loading…</td></tr></tbody></table>
    </div>
    <div class="card">
      <h3 class="section-title">All DQC Runs</h3>
      <table><thead><tr><th>Date</th><th>User</th><th>Order</th><th>Verdict</th><th>Rejection Reason</th><th>DQC Skill Version</th><th>Timestamp UTC</th></tr></thead><tbody id="dqcRunBody"><tr><td colspan="7">Loading…</td></tr></tbody></table>
    </div>
  </section>
  <section id="factory-share-page" class="page">
    <div class="card"><h3 class="section-title" id="factoryShareTitle">Factory Performance</h3><div class="hint">Factory-specific shared view. Period: <select id="factorySharePeriod"><option value="all">All</option><option value="last_3">Last 3 months</option><option value="last_6">Last 6 months</option><option value="last_month">Last month</option><option value="mtd">MTD</option><option value="ytd">YTD</option><option value="quarter">Quarter</option></select></div></div>
    <div class="exec-grid" id="factoryShareKpis"></div>
    <div class="card"><h3 class="section-title">Analysis Progress</h3><div class="exec-grid" id="factoryShareProgress"></div></div>
    <div class="card"><h3 class="section-title">Qarma Errors</h3><div style="overflow:auto"><table><thead><tr><th>Order</th><th>Actual Shipping Date</th><th>Qarma Rejected</th><th>Reinspected</th><th>Qarma Comment</th><th>Qarma Report</th></tr></thead><tbody id="factoryShareQarmaErrors"></tbody></table></div></div>
    <div class="card"><h3 class="section-title">Remake Analysis</h3><div style="overflow:auto"><table><thead><tr><th>Order</th><th>QTY</th><th>Category</th><th>Culprit</th><th>Subcategory</th><th>Source</th><th>Verification</th><th>Comment</th></tr></thead><tbody id="factoryShareRemakeAnalysis"></tbody></table></div></div>
    <div class="card"><h3 class="section-title">QC Analysis</h3><div style="overflow:auto"><table><thead><tr><th>Order</th><th>QC Date</th><th>QC Comment</th><th>Error Type</th><th>Reinspection</th><th>How to Avoid</th><th>Work Notes</th></tr></thead><tbody id="factoryShareQcAnalysis"></tbody></table></div></div>
    <div class="card"><h3 class="section-title" id="factoryShareSelectedTitle">Selected Orders — All completed orders in period</h3><div class="factory-share-orders-scroll"><table><thead><tr><th>Order</th><th>Customer Ref</th><th>Actual Shipping Date</th><th>Completed Date</th><th>Backend Shipping Status Date</th><th>Customer</th><th class="right">Order QTY</th><th>Qarma Checked</th><th>Qarma Rejected</th><th>Reinspected</th><th>Qarma Comment</th><th>Qarma Report</th><th>Remake</th></tr></thead><tbody id="factoryShareOrders"></tbody></table></div></div>
    <div class="card"><h3 class="section-title">Monthly Detail</h3><table><thead><tr><th>Month</th><th class="right">Orders</th><th class="right">Order QTY</th><th class="right">Remakes</th><th class="right">Remake QTY</th><th class="right">Qarma Checked</th><th class="right">Qarma Errors</th></tr></thead><tbody id="factoryShareMonthly"></tbody></table></div>
  </section>
  <section id="hummel-pro-na" class="page">
    <div class="card"><h3 class="section-title">Hummel PRO NA — Customer Account View</h3><div class="hint">Hidden account-specific view. Data is restricted to Bronze orders linked to company Hummel Pro NA, with Qarma and remake values filtered to those orders.</div></div>
    <div class="exec-grid">
      <div class="card metric"><div class="label">Total Orders</div><div class="value" id="hummelTotalOrders"></div></div>
      <div class="card metric"><div class="label">Total Order QTY</div><div class="value" id="hummelTotalQty"></div></div>
      <div class="card metric"><div class="label">Remake Orders</div><div class="value" id="hummelRemakeOrders"></div><div class="sub" id="hummelOrderErrorRate"></div></div>
      <div class="card metric"><div class="label">Remake QTY</div><div class="value" id="hummelRemakeQty"></div><div class="sub" id="hummelQtyErrorRate"></div></div>
    </div>
    <div class="card"><h3 class="section-title">Month-wise Hummel PRO NA Summary</h3><table><thead><tr><th>Month</th><th class="right">Orders</th><th class="right">Order QTY</th><th class="right">Remake Orders</th><th class="right">Remake Orders Error%</th><th class="right">Remake QTY</th><th class="right">Remake QTY Error%</th></tr></thead><tbody id="hummelMonthlyBody"></tbody></table></div>
    <div class="card"><h3 class="section-title">Hummel PRO NA by Factory</h3><table><thead><tr><th>Factory</th><th class="right">Orders</th><th class="right">Order QTY</th><th class="right">Remake Orders</th><th class="right">Remake Orders Error%</th><th class="right">Remake QTY</th><th class="right">Remake QTY Error%</th></tr></thead><tbody id="hummelFactoryBody"></tbody></table></div>
  </section>

<!-- Drill-down overlay -->
<div class="drill-overlay" id="drillOverlay">
  <div class="drill-panel">
    <button class="drill-close" id="drillClose">✕ Close</button>
    <div class="drill-title" id="drillTitle">Orders</div>
    <div class="drill-count" id="drillCount"></div>
    <table class="drill-table" id="drillTable"><thead><tr><th>Order</th><th class="right">Affected</th><th>Product</th><th>Factory</th><th>Issue</th></tr></thead><tbody id="drillBody"></tbody></table>
  </div>
</div>

<script>
const DATA = {DATA_JSON};
  const FACTORY_SHARE_LINKS = {FACTORY_SHARE_LINKS_JSON};
  const FACTORY_SHARE_ORDERS = {FACTORY_SHARE_ORDERS_JSON};
const HUMMEL_DATA = {HUMMEL_DATA_JSON};
const YTD = {YTD_DATA_JSON};
const FACTORY_COLORS = {FACTORY_COLORS};
const MONTH_KEYS = {MONTH_KEYS};
const GROUPINGS = {GROUPING_JSON_SAFE};
const PERIODS = {PERIODS_JSON_SAFE};
const REMAKES = {REMAKE_MGMT_JSON};
const REMAKE_SAVE_URL = '{REMAKE_SAS_URL}';
const REMAKE_DATA_URL = 'https://custimoolivedata.z13.web.core.windows.net/remake-mgmt-data.json';
const QC_REJECTIONS = {QC_REJECTIONS_JSON};
  const REINSPECTION_SUMMARY = {REINSPECTION_SUMMARY_JSON};
const QC_REJECTIONS_SAVE_URL = '{QC_REJECTIONS_SAS_URL}';
const QC_REJECTIONS_DATA_URL = 'https://custimoolivedata.z13.web.core.windows.net/qc-rejections-data.json';
const QC_BACKEND_URL = 'https://admin.custimoo.com';

const MONTH_LABELS = {{}};
MONTH_KEYS.forEach((k, i) => {{ MONTH_LABELS[k] = DATA.months[i]; }});
let ACTIVE_PERIOD = 'ytd';
let ACTIVE_DATA = PERIODS[ACTIVE_PERIOD] || DATA;
let ACTIVE_MONTH_KEYS = ACTIVE_DATA.monthKeys || MONTH_KEYS;
let ACTIVE_GROUPINGS = ACTIVE_DATA.groupings || GROUPINGS;
let ACTIVE_MEASURE = 'orders';
function activeMonthSet() {{ return new Set(ACTIVE_MONTH_KEYS); }}

// ── Utility ──

// ── Tabs ──
document.querySelectorAll('.tab[data-target]').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    document.querySelectorAll('.tab[data-target]').forEach(function(b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
    btn.classList.add('active');
    document.getElementById(btn.dataset.target).classList.add('active');
    if (window.location.pathname.toLowerCase() !== '/hummel-pro-na') history.replaceState(null, '', '#' + btn.dataset.target);
    if (btn.dataset.target === 'ytd') setTimeout(renderYtdChart, 0);
    if (btn.dataset.target === 'dqc-usage') setTimeout(loadDqcUsage, 0);
    if (trendChart) setTimeout(function() {{ trendChart.resize(); }}, 0);
  }});
}});
const savedTab = window.location.hash.replace(/^#/, '').toLowerCase();
if (savedTab && savedTab !== 'hummel-pro-na') {{
  const savedButton = document.querySelector('.tab[data-target="' + savedTab + '"]');
  if (savedButton) savedButton.click();
}}

// ── Drill-down ──
function dqcQs() {{
  var p = new URLSearchParams();
  var f = document.getElementById('dqcFrom');
  var t = document.getElementById('dqcTo');
  if (f && f.value) p.set('from', f.value);
  if (t && t.value) p.set('to', t.value);
  var s = p.toString();
  return s ? '?' + s : '';
}}
function dqcDownload(path) {{ window.location.href = path + dqcQs(); }}
async function loadDqcUsage() {{
  var msg = document.getElementById('dqcGenerated');
  if (!msg) return;
  msg.textContent = 'Loading DQC usage…';
  try {{
    var r = await fetch('/api/dqc/events' + dqcQs());
    var d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    var ev = d.events || [];
    var vc = {{PASSED:0, REJECTED:0, UNKNOWN:0}};
    var uc = {{}};
    function escHtml(v) {{ return String(v == null ? '' : v).replace(/[&<>"']/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}
    function dqcUser(e) {{
      var reviewer = e.reviewer && typeof e.reviewer === 'object' ? e.reviewer : {{}};
      return e.display_user || reviewer.name || e.windows_login || e.windows_user || e.windows_username || e.login_name || e.username || e.user || '(unknown)';
    }}
    ev.forEach(function(e) {{
      var v = String(e.verdict || e.status || 'UNKNOWN').toUpperCase();
      if (v === 'PASS') v = 'PASSED';
      if (v === 'FAIL' || v === 'FAILED') v = 'REJECTED';
      vc[v] = (vc[v] || 0) + 1;
      var u = dqcUser(e);
      uc[u] = (uc[u] || 0) + 1;
    }});
    document.getElementById('dqcTotal').textContent = ev.length.toLocaleString();
    document.getElementById('dqcPassed').textContent = (vc.PASSED || 0).toLocaleString();
    document.getElementById('dqcRejected').textContent = (vc.REJECTED || 0).toLocaleString();
    document.getElementById('dqcUsers').textContent = Object.keys(uc).length.toLocaleString();
    msg.textContent = 'API generated: ' + (d.generated_at || 'n/a') + ' · ' + ev.length.toLocaleString() + ' audit runs' + (d.stale_error ? ' · Warning: ' + d.stale_error : '');
    var users = Object.entries(uc).sort(function(a,b) {{ return b[1] - a[1]; }});
    document.getElementById('dqcUserBody').innerHTML = users.length ? users.map(function(x) {{ return '<tr><td>' + escHtml(x[0]) + '</td><td class="right">' + x[1].toLocaleString() + '</td></tr>'; }}).join('') : '<tr><td colspan="2">No users</td></tr>';
    document.getElementById('dqcRunBody').innerHTML = ev.length ? ev.map(function(e) {{
      var verdict = String(e.verdict || e.status || 'UNKNOWN').toUpperCase();
      if (verdict === 'PASS') verdict = 'PASSED';
      if (verdict === 'FAIL' || verdict === 'FAILED') verdict = 'REJECTED';
      var reason = e.rejection_reason || e.reject_reason || e.reason || e.failure_reason || e.qc_reason || e.notes || e.message || '—';
      var version = e.dqc_skill_version || e.version || '';
      return '<tr><td>' + escHtml((e.ts || '').slice(0,10)) + '</td><td>' + escHtml(dqcUser(e)) + '</td><td>' + escHtml(e.order || e.order_no || '') + '</td><td><strong>' + escHtml(verdict) + '</strong></td><td>' + escHtml(reason) + '</td><td>' + escHtml(version) + '</td><td>' + escHtml(e.ts || '') + '</td></tr>';
    }}).join('') : '<tr><td colspan="7">No audits logged</td></tr>';
  }} catch(e) {{
    msg.innerHTML = '<span style="color:#b42318;font-weight:700">' + escHtml(e.message) + '</span>';
  }}
}}
var dqcRefreshBtn = document.getElementById('dqcRefreshBtn');
if (dqcRefreshBtn) dqcRefreshBtn.addEventListener('click', loadDqcUsage);
var dqcCsvBtn = document.getElementById('dqcCsvBtn');
if (dqcCsvBtn) dqcCsvBtn.addEventListener('click', function() {{ dqcDownload('/api/dqc.csv'); }});
var dqcXlsxBtn = document.getElementById('dqcXlsxBtn');
if (dqcXlsxBtn) dqcXlsxBtn.addEventListener('click', function() {{ dqcDownload('/api/dqc.xlsx'); }});

// ── Drill-down ──
function showDrill(title, orders) {{
  document.getElementById('drillTitle').textContent = title;
  document.getElementById('drillCount').textContent = orders.length + ' order' + (orders.length !== 1 ? 's' : '') + ' · ' + orders.reduce((s,o) => s + o.affected, 0).toLocaleString() + ' affected items';
  document.getElementById('drillBody').innerHTML = orders.map(o => 
    '<tr><td class="order-num">#' + o.order + '</td><td class="right">' + o.affected.toLocaleString() + '</td>'
    + '<td>' + o.product_type + '</td><td>' + o.factory + '</td>'
    + '<td style="font-size:12px;color:var(--muted);max-width:250px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + o.snippet.replace(/"/g,'&quot;') + '">' + o.subjects.slice(0, 60) + '</td></tr>'
  ).join('');
  document.getElementById('drillOverlay').classList.add('show');
}}

document.getElementById('drillClose').addEventListener('click', function() {{
  document.getElementById('drillOverlay').classList.remove('show');
}});
document.getElementById('drillOverlay').addEventListener('click', function(e) {{
  if (e.target.id === 'drillOverlay') document.getElementById('drillOverlay').classList.remove('show');
}});

function esc(s) {{ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}

function pctPill(r) {{
  return r >= 2.0 ? '<span class="pct-pill pct-high">'+r.toFixed(2)+'%</span>'
       : r >= 1.0 ? '<span class="pct-pill pct-mid">'+r.toFixed(2)+'%</span>'
       : '<span class="pct-pill pct-low">'+r.toFixed(2)+'%</span>';
}}
function aggregateFactories(list) {{
  return (list || []).reduce(function(acc, f) {{
    const q = f.qarma || {{}};
    acc.volume += f.volume || 0;
    acc.orders += f.orders || 0;
    acc.remake_orders += f.remake_orders || 0;
    acc.remake_orders_checked_by_qarma += f.remake_orders_checked_by_qarma || 0;
    acc.remake_orders_not_checked_by_qarma += f.remake_orders_not_checked_by_qarma || 0;
    acc.remake_qty += f.remake_qty || 0;
    acc.qarma.sample_qty += q.sample_qty || 0;
    acc.qarma.defects += q.defects || 0;
    acc.qarma.inspections += q.inspections || 0;
    acc.qarma.orders_checked += q.orders_checked || 0;
    acc.qarma.orders_with_reinspection += q.orders_with_reinspection || 0;
    acc.qarma.rejected_orders += q.rejected_orders || 0;
    return acc;
  }}, {{volume:0, orders:0, remake_orders:0, remake_orders_checked_by_qarma:0, remake_orders_not_checked_by_qarma:0, remake_qty:0, qarma:{{sample_qty:0, defects:0, inspections:0, orders_checked:0, orders_with_reinspection:0, rejected_orders:0}}}});
}}
function valWithDelta(html) {{ return html; }}
function qarmaRate(q, totalOrderQty) {{ return (totalOrderQty || 0) > 0 ? (q.defects || 0) / totalOrderQty * 100 : 0; }}
function qarmaOrderRate(q, totalOrders) {{ return (totalOrders || 0) > 0 ? (q.rejected_orders || 0) / totalOrders * 100 : 0; }}
function actionPlanQty(f) {{
  // More Qarma quantity needed so remake_qty / Qarma Quantity Checked is below the 0.5% goal.
  // Capped by shipped quantity: Qarma cannot check more pieces than were shipped/produced.
  const remakeQty = f.remake_qty || 0;
  const checkedQty = (f.qarma || {{}}).sample_qty || 0;
  const shippedQty = f.volume || 0;
  if (remakeQty <= 0) return 0;
  const targetCheckedQty = Math.floor(remakeQty / 0.005) + 1;
  if (targetCheckedQty > shippedQty) return null;
  return Math.max(0, targetCheckedQty - checkedQty);
}}
function actionPlanInputs(f) {{
  const q = f.qarma || {{}};
  const all = aggregateFactories(ACTIVE_DATA.factories || []);
  const allQ = all.qarma || {{}};
  const isOrders = ACTIVE_MEASURE === 'orders';
  const remakeQty = isOrders ? (f.remake_orders || 0) : (f.remake_qty || 0);
  const checkedQty = isOrders ? (q.orders_checked || 0) : (q.sample_qty || 0);
  const shippedQty = isOrders ? (f.orders || 0) : (f.volume || 0);
  const totalDefects = isOrders ? (allQ.rejected_orders || 0) : (allQ.defects || 0);
  const totalChecked = isOrders ? (allQ.orders_checked || 0) : (allQ.sample_qty || 0);
  const qarmaCatchRate = totalChecked > 0 ? totalDefects / totalChecked : 0;
  const unit = isOrders ? 'orders' : 'pcs';
  const shippedLabel = isOrders ? 'Total Number of Orders' : 'Total Order QTY';
  const checkedLabel = isOrders ? 'Current Qarma Number of Orders' : 'Current Qarma QTY Checked';
  const remakeLabel = isOrders ? 'Remake Orders' : 'Remake QTY';
  const catchLabel = isOrders ? 'rejected orders' : 'defects';
  const coverageLabel = isOrders ? 'Qarma order coverage' : 'Qarma QC coverage';
  return {{ remakeQty, checkedQty, shippedQty, totalDefects, totalChecked, qarmaCatchRate, unit, shippedLabel, checkedLabel, remakeLabel, catchLabel, coverageLabel }};
}}
function actionPlanTarget(f, targetRate) {{
  const x = actionPlanInputs(f);
  if (x.remakeQty <= 0) return {{ label: 'On target', targetRemaining: 0, toCatch: 0, additionalChecks: 0, totalChecks: x.checkedQty, coveragePct: x.shippedQty > 0 ? x.checkedQty / x.shippedQty * 100 : 0, capped: false }};
  if (x.shippedQty <= 0 || x.qarmaCatchRate <= 0) return {{ label: 'No Qarma data', targetRemaining: 0, toCatch: 0, additionalChecks: 0, totalChecks: x.checkedQty, coveragePct: 0, capped: false }};
  const targetRemaining = targetRate * x.shippedQty;
  const toCatch = Math.max(0, x.remakeQty - targetRemaining);
  const additionalChecks = Math.ceil(toCatch / x.qarmaCatchRate);
  const totalChecks = x.checkedQty + additionalChecks;
  const coveragePct = totalChecks / x.shippedQty * 100;
  let label = coveragePct.toFixed(1) + '%';
  if (totalChecks > x.shippedQty) label = '100%+';
  if (toCatch <= 0) label = 'On target (' + (x.checkedQty / x.shippedQty * 100).toFixed(1) + '%)';
  return {{ label, targetRemaining, toCatch, additionalChecks, totalChecks, coveragePct, capped: totalChecks > x.shippedQty }};
}}
function actionPlanText(f) {{
  const t05 = actionPlanTarget(f, 0.005);
  const t02 = actionPlanTarget(f, 0.002);
  return t05.label + ' / ' + t02.label;
}}
function escapeAttr(s) {{
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}}
function actionPlanTooltip(f) {{
  const x = actionPlanInputs(f);
  if (x.shippedQty <= 0) return 'No base volume/orders';
  if (x.qarmaCatchRate <= 0) return 'No Qarma data';
  function fmt(n) {{ return Math.round(n).toLocaleString(); }}
  function pct(n, d) {{ return d > 0 ? (n / d * 100).toFixed(1) + '%' : '—'; }}
  function line(targetLabel, targetRate) {{
    const t = actionPlanTarget(f, targetRate);
    return targetLabel + ' target\\n'
      + 'Target remaining remakes = ' + targetLabel + ' × ' + fmt(x.shippedQty) + ' = ' + fmt(t.targetRemaining) + ' ' + x.unit + '\\n'
      + 'Remakes to catch = ' + fmt(x.remakeQty) + ' − ' + fmt(t.targetRemaining) + ' = ' + fmt(t.toCatch) + ' ' + x.unit + '\\n'
      + 'Additional Qarma checks = ceil(' + fmt(t.toCatch) + ' / ' + (x.qarmaCatchRate * 100).toFixed(2) + '%) = ' + fmt(t.additionalChecks) + ' ' + x.unit + '\\n'
      + 'Total Qarma checks = ' + fmt(x.checkedQty) + ' + ' + fmt(t.additionalChecks) + ' = ' + fmt(t.totalChecks) + ' ' + x.unit + '\\n'
      + 'Coverage needed = ' + fmt(t.totalChecks) + ' / ' + fmt(x.shippedQty) + ' = ' + (t.capped ? '100%+' : t.coveragePct.toFixed(1) + '%');
  }}
  return x.coverageLabel + ' calculation for ' + f.name + '\\n'
    + 'Uses total average Qarma catch rate across all factories in the selected measure mode:\\n'
    + fmt(x.totalDefects) + ' ' + x.catchLabel + ' / ' + fmt(x.totalChecked) + ' checked ' + x.unit + ' = ' + (x.qarmaCatchRate * 100).toFixed(2) + '%\\n\\n'
    + 'Factory inputs:\\n'
    + x.shippedLabel + ' = ' + fmt(x.shippedQty) + '\\n'
    + x.checkedLabel + ' = ' + fmt(x.checkedQty) + ' (' + pct(x.checkedQty, x.shippedQty) + ')\\n'
    + x.remakeLabel + ' = ' + fmt(x.remakeQty) + '\\n\\n'
    + line('0.5%', 0.005) + '\\n\\n'
    + line('0.2%', 0.002);
}}
function measureTooltip(label, formula) {{ return label + ' — ' + (ACTIVE_MEASURE === 'orders' ? 'No of Orders' : 'QTY') + '\\n\\n' + formula; }}
function qtyRulesTooltip() {{ return measureTooltip('Total Order QTY', 'orders.deleted_at IS NULL; order_items.status is not order_cancel; period uses orders.created_at; quantity is summed from factory_products → prices → sizes → quantity; price_info.total_quantity is fallback only; excluded factories are excluded from factory detail rows.'); }}
function qarmaRulesTooltip() {{ return measureTooltip('Qarma measures', 'eligible original final inspections only; Status = Report; Inspection type = Final; Conclusion = Approved or Rejected; Supplier QC ≠ true; inspector email ends @custimoo.com; reinspection rows excluded from checked-QTY totals; duplicate rows deduplicated by report ID; order must match Bronze scope; checked QTY capped at matched Bronze order QTY.'); }}
function measureCells(f, q) {{
  const qarmaErrPct = ACTIVE_MEASURE === 'orders' ? qarmaOrderRate(q, f.orders) : qarmaRate(q, f.volume);
  if (ACTIVE_MEASURE === 'orders') {{
    return '<td class="right" title="' + escapeAttr(measureTooltip('Total Orders', 'Distinct orders with orders.deleted_at IS NULL and order_items.status not equal to order_cancel, bucketed by orders.created_at.')) + '">' + (f.orders || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(qarmaRulesTooltip()) + '">' + (q.orders_checked || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Orders with Reinspection', 'Distinct orders with at least one eligible reinspection record.')) + '">' + (q.orders_with_reinspection || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Qarma Error Orders', 'Distinct eligible orders with a rejected original Qarma inspection.')) + '">' + (q.rejected_orders || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Qarma Err%', 'Rejected Qarma orders divided by Qarma checked orders.')) + '">' + pctPill(qarmaErrPct) + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Remake Orders', 'Distinct backend R/Ri orders in the period, excluding explicit cancelled and not-remake actions.')) + '">' + (f.remake_orders || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Remakes — Qarma Checked', 'Distinct remake orders that match an eligible Qarma checked order.')) + '">' + (f.remake_orders_checked_by_qarma || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Remakes — Qarma Not Checked', 'Distinct remake orders without a matching eligible Qarma checked order.')) + '">' + (f.remake_orders_not_checked_by_qarma || 0).toLocaleString() + '</td>'
      + '<td class="right" title="' + escapeAttr(measureTooltip('Remake Orders Err%', 'Remake Orders divided by Total Orders.')) + '">' + pctPill((f.orders || 0) > 0 ? (f.remake_orders || 0) / f.orders * 100 : 0) + '</td>';
  }}
  // qty mode
  return '<td class="right" title="' + escapeAttr(qtyRulesTooltip()) + '">' + (f.volume || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(qarmaRulesTooltip()) + '">' + (q.sample_qty || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(qarmaRulesTooltip()) + '">' + (q.orders_with_reinspection || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(qarmaRulesTooltip()) + '">' + ((f.volume || 0) > 0 ? (((q.sample_qty || 0) / f.volume * 100).toFixed(1) + '%') : '—') + '</td>'
    + '<td class="right" title="' + escapeAttr(qarmaRulesTooltip()) + '">' + (q.defects || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(qarmaRulesTooltip()) + '">' + pctPill(qarmaErrPct) + '</td>'
    + '<td class="right" title="' + escapeAttr(measureTooltip('Remake QTY', 'Sum of product-size QTY for backend R/Ri remake orders, excluding explicit cancelled and not-remake actions.')) + '">' + (f.remake_qty || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(measureTooltip('Remakes — Qarma Checked', 'Distinct remake orders matched to eligible Qarma checked orders.')) + '">' + (f.remake_orders_checked_by_qarma || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(measureTooltip('Remakes — Qarma Not Checked', 'Distinct remake orders without eligible Qarma coverage.')) + '">' + (f.remake_orders_not_checked_by_qarma || 0).toLocaleString() + '</td>'
    + '<td class="right" title="' + escapeAttr(measureTooltip('Remake QTY Err%', 'Remake QTY divided by Total Order QTY.')) + '">' + pctPill((f.volume || 0) > 0 ? (f.remake_qty || 0) / f.volume * 100 : 0) + '</td>';
}}
function measureHeaders() {{
  if (ACTIVE_MEASURE === 'orders') {{
    return '<th class="right">Total Number of Orders</th>'
      + '<th class="right">Qarma Number of Orders</th>'
      + '<th class="right">Orders with Reinspection</th>'
      + '<th class="right">No of orders with error in Qarma</th>'
      + '<th class="right">Qarma Err%</th>'
      + '<th class="right">Remake Orders</th>'
      + '<th class="right">Remakes — Qarma Checked</th>'
      + '<th class="right">Remakes — Qarma Not Checked</th>'
      + '<th class="right">Remake Orders Err%</th>';
  }}
  return '<th class="right">Total Order QTY</th>'
    + '<th class="right">Qarma QTY Checked</th>'
    + '<th class="right">Orders with Reinspection</th>'
    + '<th class="right">Qarma QC Coverage%</th>'
    + '<th class="right">Qarma Defects QTY</th>'
    + '<th class="right">Qarma Err%</th>'
    + '<th class="right">Remake QTY</th>'
    + '<th class="right">Remakes — Qarma Checked</th>'
    + '<th class="right">Remakes — Qarma Not Checked</th>'
    + '<th class="right">Remake QTY Err%</th>';
}}
function factoryRow(f, opts) {{
  opts = opts || {{}};
  const cls = opts.cls || '';
  const clickable = opts.clickable ? ' clickable' : '';
  const dataFactory = opts.clickable ? ' data-factory="' + f.name + '"' : '';
  const q = f.qarma || {{}};
  const share = FACTORY_SHARE_LINKS[factorySlug(f.name)];
  const shareHref = share ? share + '&period=' + encodeURIComponent(ACTIVE_PERIOD) : '';
  const label = share ? '<a href="' + escapeAttr(shareHref) + '" style="color:#2563eb;text-decoration:underline"><strong>' + esc(f.name) + '</strong></a>' : '<strong>' + esc(f.name) + '</strong>';
  let row = '<tr class="' + (cls + clickable).trim() + '"' + dataFactory + '><td>' + label + '</td>'
    + measureCells(f, q)
    + '<td class="right" title="' + escapeAttr(actionPlanTooltip(f)) + '"><strong>' + actionPlanText(f) + '</strong></td>';
  return row + '</tr>';
}}
function setBreakdownHeader(mode) {{
  const thead = document.querySelector('#factoryTable thead tr');
  const first = mode === 'all' ? 'All' : (mode === 'factory' ? 'Factory' : (mode === 'sku' ? 'SKU / Series' : (mode === 'sport' ? 'Sport' : (mode === 'category' ? 'Category' : 'Order Admin'))));
  thead.innerHTML = '<th>' + first + '</th>' + measureHeaders() + '<th class="right">Qarma QC to 0.5% / 0.2%</th>';
  document.getElementById('breakdownTitle').textContent = mode === 'all' ? 'Remake / Qarma Breakdown — All' : (mode === 'factory' ? 'Remake / Qarma Breakdown — Factories' : (mode === 'sku' ? 'Remake / Qarma Breakdown — SKU' : (mode === 'sport' ? 'Remake / Qarma Breakdown — Sports' : (mode === 'category' ? 'Remake / Qarma Breakdown — Category' : 'Remake / Qarma Breakdown — Order Admin'))));
  const qsrc = DATA.qarmaSource || {{}};
  const qnote = qsrc.ok ? (' Qarma source: live CSV · ' + (qsrc.filtered_rows || 0).toLocaleString() + ' included rows / ' + (qsrc.rows || 0).toLocaleString() + ' raw rows; Qarma export refreshes roughly hourly.') : (' Qarma source unavailable: ' + (qsrc.error || 'unknown error'));
  document.getElementById('breakdownHint').textContent = (mode === 'factory' ? 'Factory view combines backend remake data with Qarma physical QC catch data.' : 'Selected grouping combines backend remake data with Qarma measures where order matching is available.') + qnote;
}}
function renderFactoryTable(tbodyId, list, clickable, opts) {{
  const total = aggregateFactories(list || []); total.name = 'Total';
  const noMavicList = (list || []).filter(function(f) {{ return f.name !== 'Mavic Sports'; }});
  const noMavic = aggregateFactories(noMavicList); noMavic.name = 'Total excl. Mavic Sports';
  document.getElementById(tbodyId).innerHTML = (list || []).map(function(f) {{ return factoryRow(f, {{clickable: clickable}}); }}).join('') + factoryRow(total, {{cls:'total-row'}}) + factoryRow(noMavic, {{cls:'no-mavic-row'}});
}}
function renderActionPlanDiagnostics(mode) {{
  const body = document.getElementById('actionPlanDiagnosticsBody');
  if (!body) return;
  let rows = [];
  if (mode === 'factory') rows = ACTIVE_DATA.factories || [];
  else if (mode === 'all') {{ const total = aggregateFactories(ACTIVE_DATA.factories || []); total.name = 'All'; rows = [total]; }}
  else rows = ((ACTIVE_GROUPINGS || {{}})[mode] || []);
  rows = rows.filter(function(f) {{ return (f.volume || 0) > 0 && ((f.qarma || {{}}).sample_qty || 0) > 0; }});
  if (!rows.length) {{ body.innerHTML = '<tr><td colspan="6">No Qarma coverage data for selected grouping.</td></tr>'; return; }}
  body.innerHTML = rows.map(function(f) {{
    const q = f.qarma || {{}};
    const checked = q.sample_qty || 0;
    const unchecked = Math.max(0, (f.volume || 0) - checked);
    const qRate = qarmaRate(q, f.volume);
    const implied = unchecked > 0 ? (f.remake_qty || 0) / unchecked * 100 : null;
    const ratio = (implied !== null && qRate > 0) ? implied / qRate : null;
    let interp = 'Coverage gap check';
    if (['Rajco','Silver-Star Group'].includes(f.name)) interp = 'More data needed: on-site QC only recently started, so current Qarma sample is not mature enough for accuracy';
    else if (implied === null) interp = 'No unchecked QTY';
    else if (ratio !== null && ratio >= 1.5) interp = 'Unchecked looks worse; 100% QC helps, but QC escapes/non-QC causes still need RCA';
    else if (ratio !== null && ratio <= 0.5) interp = 'Qarma sample likely risk-targeted; do not treat Qarma Err% as population rate';
    else interp = 'Unchecked broadly tracks checked lots; coverage is a reasonable lever';
    return '<tr><td><strong>' + esc(f.name) + '</strong></td><td class="right">' + qRate.toFixed(2) + '%</td><td class="right">' + unchecked.toLocaleString() + '</td><td class="right">' + (implied === null ? '—' : implied.toFixed(2) + '%') + '</td><td class="right">' + (ratio === null ? '—' : ratio.toFixed(1) + 'x') + '</td><td>' + interp + '</td></tr>';
  }}).join('');
}}
function renderGroupingTable(mode) {{
  setBreakdownHeader(mode);
  var filter = document.getElementById('breakdownFilter'); if (filter && filter.value !== mode) filter.value = mode;
  if (mode === 'factory') {{ renderFactoryTable('factoryBody', ACTIVE_DATA.factories || [], true, {{}}); renderActionPlanDiagnostics(mode); return; }}
  if (mode === 'all') {{ const total = aggregateFactories(ACTIVE_DATA.factories || []); total.name = 'All'; document.getElementById('factoryBody').innerHTML = factoryRow(total, {{cls:'total-row'}}); renderActionPlanDiagnostics(mode); return; }}
  const rows = ((ACTIVE_GROUPINGS || {{}})[mode] || []);
  const total = aggregateFactories(rows); total.name = 'Total';
  document.getElementById('factoryBody').innerHTML = rows.map(function(r) {{ return factoryRow(r, {{}}); }}).join('') + factoryRow(total, {{cls:'total-row'}});
  renderActionPlanDiagnostics(mode);
}}
function leaderRank(i) {{ return i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : String(i + 1); }}
function renderLeaderRows(rows, emptyLabel) {{
  if (!rows || !rows.length) return '<tr><td colspan="5">No qualifying ' + emptyLabel + ' in selected period</td></tr>';
  return rows.map(function(r, i) {{ return '<tr><td><strong>' + leaderRank(i) + '</strong></td><td>' + esc(r.name) + '</td><td class="right"><strong>' + (r.rate || 0).toFixed(2) + '%</strong></td><td class="right">' + (r.remake_orders || 0).toLocaleString() + '</td><td class="right">' + (r.orders || 0).toLocaleString() + '</td></tr>'; }}).join('');
}}
function renderExceptionLeaders() {{
  const d = ACTIVE_DATA || {{}};
  const leaders = d.exceptionLeaders || {{admin: []}};
  const label = d.label || ((d.months || [])[0] + ' – ' + (d.months || [])[(d.months || []).length - 1]);
  document.getElementById('exceptionLeadersSub').textContent = 'Selected period: ' + (d.name || 'Selected period') + ' · ' + label + ' · exception rate = remake orders / total orders.';
  document.getElementById('adminLeadersBody').innerHTML = renderLeaderRows(leaders.admin || [], 'order admins');
}}
function updateSummaryStats() {{
  const d = ACTIVE_DATA;
  const label = d.label || (d.months[0] + ' – ' + d.months[d.months.length-1]);
  const rollingData = PERIODS.last_3 || DATA;
  const rollingAgg = aggregateFactories(rollingData.factories || []);
  const selectedAgg = aggregateFactories(d.factories || []);
  const isOrders = ACTIVE_MEASURE === 'orders';
  const rollingNum = isOrders ? (rollingAgg.remake_orders || 0) : (rollingAgg.remake_qty || 0);
  const rollingDen = isOrders ? (rollingAgg.orders || 0) : (rollingAgg.volume || 0);
  const selectedNum = isOrders ? (selectedAgg.remake_orders || 0) : (selectedAgg.remake_qty || 0);
  const selectedDen = isOrders ? (selectedAgg.orders || 0) : (selectedAgg.volume || 0);
  const rollingRemakeRate = rollingDen > 0 ? (rollingNum / rollingDen * 100) : 0;
  const selectedRemakeRate = selectedDen > 0 ? (selectedNum / selectedDen * 100) : 0;
  const numLabel = isOrders ? 'remake orders' : 'remake qty';
  const denLabel = isOrders ? 'total orders' : 'total order qty';
  const modeLabel = isOrders ? 'Orders' : 'QTY';
  document.getElementById('rollingRate').textContent = rollingRemakeRate.toFixed(2) + '%';
  document.getElementById('rollingSub').textContent = 'Fixed 3-month rolling window · ' + (DATA.rollingLabel || '') + ' · ' + rollingNum.toLocaleString() + ' ' + numLabel + ' / ' + rollingDen.toLocaleString() + ' ' + denLabel;
  document.getElementById('selectedRateLabel').textContent = 'Selected Period Remake Rate';
  document.getElementById('totalRate').textContent = selectedRemakeRate.toFixed(2) + '%';
  document.getElementById('totalSub').textContent = 'Selected period: ' + (d.name || 'Selected period') + ' · ' + label + ' · ' + selectedNum.toLocaleString() + ' ' + numLabel + ' / ' + selectedDen.toLocaleString() + ' ' + denLabel;
  const ytd = PERIODS.ytd || DATA;
  const ytdAgg = aggregateFactories(ytd.factories || []);
  const ytdNum = isOrders ? (ytdAgg.remake_orders || 0) : (ytdAgg.remake_qty || 0);
  const ytdDen = isOrders ? (ytdAgg.orders || 0) : (ytdAgg.volume || 0);
  const ytdRate = ytdDen > 0 ? (ytdNum / ytdDen * 100) : 0;
  const gap = ytdRate - 0.5;
  const goalText = gap <= 0 ? 'On target' : (gap.toFixed(2) + ' percentage points above goal');
  document.getElementById('goalRateLabel').textContent = '2026 Goal — Remake ' + modeLabel + ' Error Rate';
  document.getElementById('goalSub').textContent = 'Goal for 2026: ≤0.50% ' + numLabel + ' error rate. Current YTD: ' + ytdRate.toFixed(2) + '% (' + ytdNum.toLocaleString() + ' ' + numLabel + ' / ' + ytdDen.toLocaleString() + ' ' + denLabel + ') · ' + goalText;
}}
function applyPeriod(key) {{
  ACTIVE_PERIOD = key;
  ACTIVE_DATA = PERIODS[key] || DATA;
  ACTIVE_MONTH_KEYS = ACTIVE_DATA.monthKeys || MONTH_KEYS;
  ACTIVE_GROUPINGS = ACTIVE_DATA.groupings || GROUPINGS;
  updateSummaryStats();
  updatePeriodKpis();
  renderExceptionLeaders();
  renderGroupingTable((document.getElementById('breakdownFilter') || {{value:'factory'}}).value);
  renderTrendChart(null);
  renderDetails();
}}
var breakdownFilter = document.getElementById('breakdownFilter');
if (breakdownFilter) {{ breakdownFilter.value = 'factory'; breakdownFilter.addEventListener('change', function() {{ renderGroupingTable(breakdownFilter.value); }}); }}
var periodFilter = document.getElementById('periodFilter');
if (periodFilter) {{ periodFilter.value = ACTIVE_PERIOD; periodFilter.addEventListener('change', function() {{ applyPeriod(periodFilter.value); }}); }}
var measureFilter = document.getElementById('measureFilter');
if (measureFilter) {{ measureFilter.value = ACTIVE_MEASURE; measureFilter.addEventListener('change', function() {{ ACTIVE_MEASURE = measureFilter.value; updateSummaryStats(); renderGroupingTable((document.getElementById('breakdownFilter') || {{value:'factory'}}).value); renderTrendChart(currentTrendFactory); renderDetails(); }}); }}

function renderDetails() {{
  const months = ACTIVE_DATA.months || [];
  const volume = ACTIVE_DATA.monthlyVolume || [];
  const orders = ACTIVE_DATA.monthlyOrders || [];
  const remakeOrders = ACTIVE_DATA.monthlyRemakeOrders || [];
  const remakeQty = ACTIVE_DATA.monthlyRemakeQty || [];
  document.getElementById('monthlyBody').innerHTML = months.map(function(m, i) {{
    const v = volume[i] || 0;
    const rq = remakeQty[i] || 0;
    const rate = v > 0 ? rq / v * 100 : 0;
    return '<tr><td><strong>' + esc(m) + '</strong></td><td class="right">' + v.toLocaleString() + '</td><td class="right">' + (orders[i] || 0).toLocaleString() + '</td><td class="right">' + (remakeOrders[i] || 0).toLocaleString() + '</td><td class="right">' + rq.toLocaleString() + '</td><td class="right">' + rate.toFixed(2) + '%</td></tr>';
  }}).join('');
  const head = document.getElementById('factoryMonthHead');
  head.innerHTML = '<th>Factory</th>' + months.map(function(m) {{ return '<th class="right">' + esc(m) + '</th>'; }}).join('');
  const useOrders = ACTIVE_MEASURE === 'orders';
  document.getElementById('detailsFactoryTitle').textContent = 'Factory × Month ' + (useOrders ? 'Remake Orders' : 'Remake QTY');
  document.getElementById('detailsFactoryHint').textContent = 'Monthly ' + (useOrders ? 'remake orders' : 'remake quantity') + ' by factory; follows the selected measure.';
  document.getElementById('factoryMonthBody').innerHTML = (ACTIVE_DATA.factories || []).map(function(f) {{
    const monthly = f.monthly || {{}};
    const values = monthly[useOrders ? 'remake_orders' : 'remake_qty'] || [];
    return '<tr><td><strong>' + esc(f.name || '') + '</strong></td>' + months.map(function(_, i) {{ return '<td class="right">' + (values[i] || 0).toLocaleString() + '</td>'; }}).join('') + '</tr>';
  }}).join('');
}}
let YTD_MEASURE = 'orders';
document.getElementById('ytdVolume').textContent = DATA.totalVolume.toLocaleString();

function updatePeriodKpis() {{
  var d = ACTIVE_DATA;
  var label = d.name || 'YTD 2026';
  var remOrders = d.totalRemakeOrders || 0;
  var remQty = d.totalRemakeQty || 0;
  if (YTD_MEASURE === 'orders') {{
    document.getElementById('ytdVolume').textContent = (d.totalOrders || 0).toLocaleString();
    document.getElementById('periodKpiValue').textContent = remOrders.toLocaleString();
    var rateO = (d.totalOrders || 0) > 0 ? remOrders / d.totalOrders * 100 : 0;
    document.getElementById('periodKpiSub').textContent = label + ' · ' + rateO.toFixed(2) + '% of ' + (d.totalOrders || 0).toLocaleString() + ' orders are remakes';
  }} else {{
    document.getElementById('ytdVolume').textContent = (d.totalVolume || 0).toLocaleString();
    document.getElementById('periodKpiValue').textContent = remQty.toLocaleString();
    var rateQ = (d.totalVolume || 0) > 0 ? remQty / d.totalVolume * 100 : 0;
    document.getElementById('periodKpiSub').textContent = label + ' · ' + rateQ.toFixed(2) + '% of ' + (d.totalVolume || 0).toLocaleString() + ' items · ' + remQty.toLocaleString() + ' remake qty';
  }}
  document.getElementById('periodKpiLabel').textContent = label + ' Remakes';
}}
function updateYtdKpis() {{
  updatePeriodKpis();
  if (YTD_MEASURE === 'orders') {{
    document.querySelector('#ytdVolume').closest('.metric').querySelector('.label').textContent = 'Selected Period No of Orders';
  }} else {{
    document.querySelector('#ytdVolume').closest('.metric').querySelector('.label').textContent = 'Selected Period Total Order QTY';
  }}
}}
updateYtdKpis();

// ── Charts ──
const chartBaseOptions = {{
  responsive: true,
  maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{ legend: {{ position: 'bottom' }} }},
  scales: {{
    y: {{ beginAtZero: true, title: {{ display: true, text: 'Remake QTY' }} }},
    y1: {{ beginAtZero: true, position: 'right', grid: {{ drawOnChartArea: false }}, title: {{ display: true, text: 'Remake %' }} }}
  }}
}};

let trendChart;
let currentTrendFactory = null;
function remakeRates(denoms, remakeValues) {{ return denoms.map(function(v, i) {{ return v > 0 ? +((remakeValues[i] || 0) / v * 100).toFixed(2) : 0; }}); }}
function trendMeasureConfig() {{
  const ordersMode = ACTIVE_MEASURE === 'orders';
  return {{
    valuesLabel: ordersMode ? 'Total Orders' : 'Total Order QTY',
    rateLabel: ordersMode ? 'Remake % (Orders)' : 'Remake % (Qty)',
    axisLabel: ordersMode ? 'Orders' : 'Order QTY'
  }};
}}
function buildTrendDatasets(factoryName) {{
  const cfg = trendMeasureConfig();
  if (!factoryName) {{
    const totalValues = ACTIVE_DATA[ACTIVE_MEASURE === 'orders' ? 'monthlyOrders' : 'monthlyVolume'] || [];
    const remakeValues = ACTIVE_DATA[ACTIVE_MEASURE === 'orders' ? 'monthlyRemakeOrders' : 'monthlyRemakeQty'] || [];
    return [
      {{ type: 'bar', label: cfg.valuesLabel, data: totalValues, backgroundColor: 'rgba(124, 58, 237, 0.25)', borderColor: 'rgba(124, 58, 237, 0.8)', borderWidth: 1, yAxisID: 'y' }},
      {{ type: 'line', label: cfg.rateLabel, data: remakeRates(totalValues, remakeValues), borderColor: '#ef4444', backgroundColor: '#ef4444', tension: 0.25, yAxisID: 'y1' }}
    ];
  }}
  const fd = (ACTIVE_DATA.factories || []).find(function(x) {{ return x.name === factoryName; }});
  if (!fd) return [];
  const monthly = fd.monthly || {{}};
  const totalValues = monthly[ACTIVE_MEASURE === 'orders' ? 'orders' : 'volumes'] || [];
  const remakeValues = monthly[ACTIVE_MEASURE === 'orders' ? 'remake_orders' : 'remake_qty'] || [];
  return [
    {{ type: 'bar', label: factoryName + ' ' + cfg.valuesLabel, data: totalValues, backgroundColor: 'rgba(124, 58, 237, 0.25)', borderColor: 'rgba(124, 58, 237, 0.8)', borderWidth: 1, yAxisID: 'y' }},
    {{ type: 'line', label: factoryName + ' ' + cfg.rateLabel, data: remakeRates(totalValues, remakeValues), borderColor: FACTORY_COLORS[factoryName] || '#ef4444', backgroundColor: FACTORY_COLORS[factoryName] || '#ef4444', tension: 0.25, yAxisID: 'y1' }}
  ];
}}
function renderTrendChart(factoryName) {{
  currentTrendFactory = factoryName || null;
  const cfg = trendMeasureConfig();
  document.getElementById('trendTitle').textContent = factoryName ? ('Monthly ' + cfg.valuesLabel + ' Trend — ' + factoryName) : 'Monthly ' + cfg.valuesLabel + ' Trend — All Factories';
  document.getElementById('resetBtn').style.display = factoryName ? 'inline-block' : 'none';
  document.getElementById('trendBar').style.display = factoryName ? 'flex' : 'none';
  if (factoryName) {{
    document.getElementById('trendFactory').textContent = factoryName;
    const fd = (ACTIVE_DATA.factories || []).find(function(x) {{ return x.name === factoryName; }});
    const monthly2 = fd && fd.monthly ? fd.monthly : {{}};
    const denoms2 = monthly2[ACTIVE_MEASURE === 'orders' ? 'orders' : 'volumes'] || [];
    const rem2 = monthly2[ACTIVE_MEASURE === 'orders' ? 'remake_orders' : 'remake_qty'] || [];
    const first = denoms2[0] > 0 ? (rem2[0] || 0) / denoms2[0] * 100 : 0;
    const lastIdx = denoms2.length - 1;
    const last = denoms2[lastIdx] > 0 ? (rem2[lastIdx] || 0) / denoms2[lastIdx] * 100 : 0;
    const delta = last - first;
    document.getElementById('trendPill').textContent = last.toFixed(2) + '%';
    document.getElementById('trendDelta').textContent = (delta >= 0 ? '+' : '') + delta.toFixed(2) + ' pp vs first month';
  }}
  chartBaseOptions.scales.y.title.text = cfg.axisLabel;
  chartBaseOptions.scales.y1.title.text = cfg.rateLabel;
  const ctx = document.getElementById('trendChart').getContext('2d');
  if (trendChart) trendChart.destroy();
  trendChart = new Chart(ctx, {{ data: {{ labels: ACTIVE_DATA.months, datasets: buildTrendDatasets(factoryName) }}, options: chartBaseOptions }});
}}
document.getElementById('resetBtn').addEventListener('click', function() {{ renderTrendChart(null); }});
applyPeriod(ACTIVE_PERIOD);

function cumulative(arr) {{ return arr.map(function(_, i) {{ return arr.slice(0, i + 1).reduce(function(a,b){{return a+b;}}, 0); }}); }}
function ytdCumulativeTable() {{
  const head = document.getElementById('ytdMonthlyHead');
  const body = document.getElementById('ytdMonthlyBody');
  const remOrdersCum = cumulative(YTD.monthlyRemakeOrders || []);
  const remQtyCum = cumulative(YTD.monthlyRemakeQty || []);
  if (YTD_MEASURE === 'orders') {{
    head.innerHTML = '<th>Month</th><th class="right">Monthly Orders</th><th class="right">Accumulated Orders</th><th class="right">Monthly Remake Orders</th><th class="right">Accumulated Remake Orders</th><th class="right">Accumulated Remake %</th>';
    body.innerHTML = YTD.months.map(function(m, i) {{
      const rate = (YTD.cumulativeOrders[i] || 0) > 0 ? remOrdersCum[i] / YTD.cumulativeOrders[i] * 100 : 0;
      return '<tr><td><strong>' + m + '</strong></td><td class="right">' + (YTD.monthlyOrders[i] || 0).toLocaleString() + '</td><td class="right">' + (YTD.cumulativeOrders[i] || 0).toLocaleString() + '</td><td class="right">' + ((YTD.monthlyRemakeOrders || [])[i] || 0).toLocaleString() + '</td><td class="right">' + remOrdersCum[i].toLocaleString() + '</td><td class="right">' + pctPill(rate) + '</td></tr>';
    }}).join('');
    document.getElementById('ytdMonthlyTitle').textContent = 'YTD Monthly Accumulated Orders + Remakes';
  }} else {{
    head.innerHTML = '<th>Month</th><th class="right">Monthly Order QTY</th><th class="right">Accumulated Order QTY</th><th class="right">Monthly Remake QTY</th><th class="right">Accumulated Remake QTY</th><th class="right">Accumulated Remake %</th>';
    body.innerHTML = YTD.months.map(function(m, i) {{
      const rate = (YTD.cumulativeVolume[i] || 0) > 0 ? remQtyCum[i] / YTD.cumulativeVolume[i] * 100 : 0;
      return '<tr><td><strong>' + m + '</strong></td><td class="right">' + (YTD.monthlyVolume[i] || 0).toLocaleString() + '</td><td class="right">' + (YTD.cumulativeVolume[i] || 0).toLocaleString() + '</td><td class="right">' + ((YTD.monthlyRemakeQty || [])[i] || 0).toLocaleString() + '</td><td class="right">' + remQtyCum[i].toLocaleString() + '</td><td class="right">' + pctPill(rate) + '</td></tr>';
    }}).join('');
    document.getElementById('ytdMonthlyTitle').textContent = 'YTD Monthly Accumulated QTY + Remakes';
  }}
}}
function renderYtdFactoryTable() {{
  const head = document.getElementById('ytdFactoryHead');
  const prev = ACTIVE_MEASURE;
  ACTIVE_MEASURE = YTD_MEASURE;
  head.innerHTML = '<th>Factory</th>' + measureHeaders() + '<th class="right">Qarma Err%</th><th class="right">Qarma QC to 0.5% / 0.2%</th>';
  renderFactoryTable('ytdFactoryBody', YTD.factories || [], false, {{}});
  ACTIVE_MEASURE = prev;
}}
let ytdChart = null;
function renderYtdChart() {{
  const el = document.getElementById('ytdChart');
  if (!el || !el.offsetParent) return;
  const isOrders = YTD_MEASURE === 'orders';
  const remOrdersCum = cumulative(YTD.monthlyRemakeOrders || []);
  const remQtyCum = cumulative(YTD.monthlyRemakeQty || []);
  const barData = isOrders ? YTD.cumulativeOrders : YTD.cumulativeVolume;
  const rateData = barData.map(function(v, i) {{ return v > 0 ? +(((isOrders ? remOrdersCum[i] : remQtyCum[i]) / v * 100).toFixed(2)) : 0; }});
  const barLabel = isOrders ? 'Accumulated No of Orders' : 'Accumulated Total Order QTY';
  const rateLabel = isOrders ? 'Accumulated Remake % (Orders)' : 'Accumulated Remake % (Qty)';
  document.getElementById('ytdChartTitle').textContent = isOrders ? 'YTD Accumulated Orders + Remake %' : 'YTD Accumulated QTY + Remake %';
  if (ytdChart) ytdChart.destroy();
  ytdChart = new Chart(el.getContext('2d'), {{
    data: {{ labels: YTD.months, datasets: [
      {{ type: 'bar', label: barLabel, data: barData, backgroundColor: 'rgba(31,111,235,0.25)', borderColor: 'rgba(31,111,235,0.8)', borderWidth: 1, yAxisID: 'y' }},
      {{ type: 'line', label: rateLabel, data: rateData, borderColor: '#ef4444', backgroundColor: '#ef4444', tension: 0.25, pointRadius: 5, pointHoverRadius: 7, yAxisID: 'y1' }}
    ] }},
    options: {{ ...chartBaseOptions, scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: barLabel }} }}, y1: {{ beginAtZero: true, position: 'right', grid: {{ drawOnChartArea: false }}, title: {{ display: true, text: 'Accumulated Remake %' }} }} }} }}
  }});
}}
function applyYtdMeasure() {{
  const sel = document.getElementById('ytdMeasureFilter');
  YTD_MEASURE = sel ? sel.value : 'qty';
  updateYtdKpis(); ytdCumulativeTable(); renderYtdFactoryTable(); renderYtdChart();
}}
const ytdMeasureFilter = document.getElementById('ytdMeasureFilter');
if (ytdMeasureFilter) {{ ytdMeasureFilter.value = YTD_MEASURE; ytdMeasureFilter.addEventListener('change', applyYtdMeasure); }}
ytdCumulativeTable();
renderYtdFactoryTable();

// ── Remake Management ──
var remakeData = {{}};
var remakeSaveTimer = null;
var remakeDirtyFields = new Map();
var remakeRows = REMAKES.slice();
const REMAKE_CATEGORIES = ['Color Mismatch','Customer Change','Damaged / Soiled','Fabric / Material','Logo / Design','No Record Found','Other','Print / Sublimation','Quantity Short / Missing','Sizing / Fit','Stitching / Construction','Uncategorized','Wrong Product / SKU'];
const REMAKE_CULPRITS = ['Factory','Customer','Merchant','Shipping courier','Custimoo'];
const CUSTIMOO_SUBCATEGORIES = ['Design','Administrative','Shipping','External'];
function remakeCulpritOptions(selected) {{
  const known = REMAKE_CULPRITS.indexOf(selected) >= 0;
  return '<option value="">Select culprit</option>' + (selected && !known ? '<option value="' + escapeAttr(selected) + '" selected>Legacy: ' + esc(selected) + '</option>' : '') + REMAKE_CULPRITS.map(function(v) {{ return '<option value="' + escapeAttr(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(v) + '</option>'; }}).join('');
}}
function custimooSubcategoryOptions(selected) {{ return '<option value="">Select subcategory</option>' + CUSTIMOO_SUBCATEGORIES.map(function(v) {{ return '<option value="' + escapeAttr(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(v) + '</option>'; }}).join(''); }}
function renderCustimooSubcategory(row, order) {{ return row.culprit === 'Custimoo' ? '<select class="remake-edit remake-culprit-subcategory" aria-label="Custimoo subcategory for order ' + escapeAttr(order) + '">' + custimooSubcategoryOptions(row.culprit_subcategory || '') + '</select>' : '<span class="muted">—</span>'; }}
function updateCustimooSubcategoryCell(row, order, input) {{
  const cell = input.closest('td');
  if (!cell) return;
  const old = cell.querySelector('.remake-culprit-subcategory');
  if (old) old.remove();
  const placeholder = cell.querySelector('.muted');
  if (placeholder) placeholder.remove();
  if (row.culprit === 'Custimoo') cell.insertAdjacentHTML('beforeend', renderCustimooSubcategory(row, order));
}}
function remakeCategoryOptions(selected) {{
  return '<option value="">Select category</option>' + REMAKE_CATEGORIES.map(function(category) {{
    return '<option value="' + escapeAttr(category) + '"' + (category === selected ? ' selected' : '') + '>' + esc(category) + '</option>';
  }}).join('');
}}

const EXCLUDED_REMAKE_FLAGS = new Set(['Cancelled','Disputed','Ex/Other Admin','No Record','Not remake']);
function remakeIsExcluded(r) {{ return EXCLUDED_REMAKE_FLAGS.has(r.flag || ''); }}
function remakeIsHandled(r) {{
  return Boolean(String(r.category || '').trim() || String(r.culprit || '').trim() || String(r.comment || '').trim());
}}
function remakeOrderKey(r) {{ return String(r.order || '').replace(/^#/, '').trim(); }}
function renderRemakeMgmt(filterAdmin, filterMonth) {{
  let rows = remakeRows;
  if (filterAdmin) rows = rows.filter(function(r) {{ return r.admin === filterAdmin; }});
  if (filterMonth) rows = rows.filter(function(r) {{ return r.month === filterMonth; }});
  rows = rows.slice().sort(function(a,b) {{
    const excludedCmp = remakeIsExcluded(a) - remakeIsExcluded(b);
    if (excludedCmp) return excludedCmp;
    const handledCmp = remakeIsHandled(a) - remakeIsHandled(b);
    if (handledCmp) return handledCmp;
    const monthCmp = String(b.month || '').localeCompare(String(a.month || ''));
    if (monthCmp) return monthCmp;
    const orderA = parseInt(String(a.order || '').replace(/\D/g, ''), 10) || 0;
    const orderB = parseInt(String(b.order || '').replace(/\D/g, ''), 10) || 0;
    return orderB - orderA;
  }});
  const tbody = document.getElementById('remakeMgmtBody');
  tbody.innerHTML = rows.map(function(r) {{
    const order = remakeOrderKey(r);
    return '<tr data-order="' + escapeAttr(order) + '">'
      + '<td class="order-num">#' + esc(order) + '</td>'
      + '<td><input class="remake-edit remake-original-order" aria-label="Original order number for remake ' + escapeAttr(order) + '" value="' + escapeAttr(r.original_order || '') + '" placeholder="Original order"></td>'
      + '<td class="right">' + (r.qty || 0).toLocaleString() + '</td>'
      + '<td>' + esc(r.customer || '(unknown)') + '</td>'
      + '<td>' + esc(r.admin || '') + '</td>'
      + '<td>' + esc(r.factory || '') + '</td>'
      + '<td>' + esc(r.month || '') + '</td>'
      + '<td>' + esc(r.source || 'Backend remake') + '</td>'
      + '<td>' + esc(r.verification_status || 'Confirmed backend remake') + '</td>'
      + '<td><select class="remake-edit remake-category" aria-label="Category for order ' + escapeAttr(order) + '">' + remakeCategoryOptions(r.category || '') + '</select></td>'
      + '<td><select class="remake-edit remake-culprit" aria-label="Culprit for order ' + escapeAttr(order) + '">' + remakeCulpritOptions(r.culprit || '') + '</select>' + renderCustimooSubcategory(r, order) + '</td>'
      + '<td><input class="remake-edit remake-comment" aria-label="Comment for order ' + escapeAttr(order) + '" value="' + escapeAttr(r.comment || '') + '" placeholder="Comment"></td>'
      + '</tr>';
  }}).join('') || '<tr><td colspan="12">No remakes match the filters.</td></tr>';
  const grossQty = rows.reduce(function(s,r) {{ return s + (r.qty || 0); }}, 0);
  const netRows = rows.filter(function(r) {{ return !remakeIsExcluded(r); }});
  const netQty = netRows.reduce(function(s,r) {{ return s + (r.qty || 0); }}, 0);
  document.getElementById('remakeCount').textContent = rows.length + ' rows · ' + grossQty.toLocaleString() + ' gross pcs · ' + netQty.toLocaleString() + ' net pcs';
}}
function setRemakeSaveStatus(text) {{
  const el = document.getElementById('remakeSaveStatus');
  if (el) el.textContent = text;
}}
function saveRemakes() {{
  if (!REMAKE_SAVE_URL) {{ setRemakeSaveStatus('Local only — save endpoint unavailable'); return; }}
  setRemakeSaveStatus('Saving…');
  const dirty = new Map(remakeDirtyFields);
  fetch(REMAKE_DATA_URL + '?merge=' + Date.now()).then(function(resp) {{ if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); }}).then(function(saved) {{
    const latest = Array.isArray(saved) ? saved : Object.keys(saved || {{}}).map(function(k) {{ return Object.assign({{}}, saved[k] || {{}}, {{order:String(k).replace(/^#/,'').trim()}}); }});
    const byOrder = new Map(latest.map(function(r) {{ return [remakeOrderKey(r), r]; }}));
    remakeRows.forEach(function(r) {{ const s=byOrder.get(remakeOrderKey(r)); const dirtyFields=dirty.get(remakeOrderKey(r)) || new Set(); if (s) {{ if (!dirtyFields.has('original_order')) r.original_order=s.original_order||r.original_order||''; if (!dirtyFields.has('culprit')) {{ r.culprit=s.culprit||r.culprit||''; r.culprit_subcategory=s.culprit_subcategory||r.culprit_subcategory||''; }} if (!dirtyFields.has('category')) r.category=s.category||r.category||''; if (!dirtyFields.has('comment')) r.comment=s.comment||r.comment||''; }} }});
    dirty.forEach(function(fields, key) {{ const row=remakeRows.find(function(r) {{ return remakeOrderKey(r)===key; }}); if (!row) return; const target=byOrder.get(key) || row; fields.forEach(function(field) {{ target[field]=row[field] || ''; }}); }});
    const merged = latest.concat(remakeRows.filter(function(r) {{ return !byOrder.has(remakeOrderKey(r)); }}));
    return fetch(REMAKE_SAVE_URL, {{method:'PUT', headers:{{'x-ms-blob-type':'BlockBlob','Content-Type':'application/json'}}, body:JSON.stringify(merged)}});
  }}).then(function(resp) {{ if (!resp.ok) throw new Error('HTTP ' + resp.status); remakeDirtyFields.clear(); setRemakeSaveStatus('Saved globally'); }}).catch(function(err) {{ console.warn('Remake save failed', err); setRemakeSaveStatus('Save failed — retrying on next edit'); }});
}}
function scheduleRemakeSave() {{
  clearTimeout(remakeSaveTimer);
  setRemakeSaveStatus('Unsaved changes…');
  remakeSaveTimer = setTimeout(saveRemakes, 700);
}}
function inferOriginalOrder(comment, remakeOrder) {{ const re=/(?:\b(?:reorder|remake|replacement)\b[^#\\n]{{0,80}}?|\b(?:repeat order|same issue as|from order|for order|of order)\b[^#\\n]{{0,40}}?|\border\s+)(?:#\s*)?(\d{{4,}})/i; const m=String(comment||'').match(re); return m && m[1] !== String(remakeOrder||'') ? m[1] : ''; }}
function mergeSavedRemakes(saved) {{
  if (!saved) return;
  const entries = Array.isArray(saved) ? saved : Object.keys(saved).map(function(k) {{ const v = saved[k] || {{}}; return Object.assign({{}}, typeof v === 'object' ? v : {{}}, {{ order: String(k).replace(/^#/, '').trim() }}); }});
  if (!entries.length) return;
  const byOrder = new Map(entries.map(function(r) {{ return [remakeOrderKey(r), r]; }}));
  remakeRows.forEach(function(r) {{
    const savedRow = byOrder.get(remakeOrderKey(r));
    if (savedRow) {{ r.original_order = savedRow.original_order || r.original_order || inferOriginalOrder(savedRow.comment || r.comment, remakeOrderKey(r)); r.culprit_subcategory = savedRow.culprit_subcategory || r.culprit_subcategory || ''; r.category = savedRow.category || r.category || ''; r.culprit = savedRow.culprit || r.culprit || ''; r.comment = savedRow.comment || r.comment || ''; }}
  }});
  remakeRows = remakeRows.concat(entries.filter(function(r) {{ return !remakeRows.some(function(x) {{ return remakeOrderKey(x) === remakeOrderKey(r); }}); }}));
}}

function forensicsRows(rows, fields) {{ return rows.filter(function(r) {{ return fields.every(function(f) {{ return !String(r[f] || '').trim(); }}); }}); }}
function renderForensics() {{
  const remUn = forensicsRows(remakeRows, ['category','culprit','comment']);
  const remCat = {{}}, remCul = {{}};
  remakeRows.forEach(function(r) {{ const c=String(r.category||'Uncategorized').trim()||'Uncategorized', u=String(r.culprit||'Unassigned').trim()||'Unassigned'; [ [remCat,c], [remCul,u] ].forEach(function(pair) {{ const g=pair[0][pair[1]]||(pair[0][pair[1]]={{orders:0,qty:0}}); g.orders++; g.qty += Number(r.qty)||0; }}); }});
  document.getElementById('remakeAnalysisKpis').innerHTML = [['Total remake orders',remakeRows.length],['Reviewed',remakeRows.length-remUn.length],['Not forensically reviewed',remUn.length]].map(function(x) {{ return '<div class="card metric"><div class="label">'+x[0]+'</div><div class="value">'+x[1].toLocaleString()+'</div></div>'; }}).join('');
  document.getElementById('remakeUnreviewedBody').innerHTML = remUn.map(function(r) {{ return '<tr><td>#'+esc(remakeOrderKey(r))+'</td><td>'+esc(r.customer||'(unknown)')+'</td><td class="right">'+(Number(r.qty)||0).toLocaleString()+'</td><td>'+esc(r.factory||'')+'</td><td>'+esc(r.admin||'')+'</td><td>'+esc(r.month||'')+'</td></tr>'; }}).join('') || '<tr><td colspan="6">All remake orders have forensic notes.</td></tr>';
  function groupHtml(g) {{ return Object.keys(g).sort().map(function(k) {{ return '<tr><td>'+esc(k)+'</td><td class="right">'+g[k].orders.toLocaleString()+'</td><td class="right">'+g[k].qty.toLocaleString()+'</td></tr>'; }}).join(''); }}
  document.getElementById('remakeCategoryBody').innerHTML = groupHtml(remCat); document.getElementById('remakeCulpritBody').innerHTML = groupHtml(remCul);
  const qcUn = forensicsRows(qcRejectionRows, ['error_type','avoidance_action','work_comment']); const et={{}}, pa={{}};
  qcRejectionRows.forEach(function(r) {{ const e=String(r.error_type||'Investigating').trim()||'Investigating', a=String(r.avoidance_action||'No prevention action recorded').trim()||'No prevention action recorded'; const ge=et[e]||(et[e]={{orders:0,qty:0}}); ge.orders++; ge.qty+=Number(r.defects_qty)||0; pa[a]=(pa[a]||0)+1; }});
  document.getElementById('qcAnalysisKpis').innerHTML = [['Total rejected orders',qcRejectionRows.length],['Reviewed',qcRejectionRows.length-qcUn.length],['Not forensically reviewed',qcUn.length],['Orders with reinspection',REINSPECTION_SUMMARY.orders],['Reinspection events',REINSPECTION_SUMMARY.events],['Approved reinspections',REINSPECTION_SUMMARY.approved_events],['Rejected reinspections',REINSPECTION_SUMMARY.rejected_events]].map(function(x) {{ return '<div class="card metric"><div class="label">'+x[0]+'</div><div class="value">'+x[1].toLocaleString()+'</div></div>'; }}).join('');
  document.getElementById('qcUnreviewedBody').innerHTML = qcUn.map(function(r) {{ return '<tr><td>#'+esc(qcRejectionKey(r))+'</td><td>'+esc(r.factory||'')+'</td><td class="right">'+(Number(r.total_qty)||0).toLocaleString()+'</td><td class="right">'+(Number(r.defects_qty)||0).toLocaleString()+'</td><td>'+esc(r.date||'')+'</td></tr>'; }}).join('') || '<tr><td colspan="5">All QC rejections have forensic notes.</td></tr>';
  document.getElementById('qcErrorTypeBody').innerHTML = groupHtml(et); document.getElementById('qcPreventionBody').innerHTML = Object.keys(pa).sort().map(function(k) {{ return '<tr><td>'+esc(k)+'</td><td class="right">'+pa[k].toLocaleString()+'</td></tr>'; }}).join('');
}}
// Init Remake Mgmt
(function() {{
  if (!document.getElementById('remakeMgmtBody')) return;
  const admins = [...new Set(remakeRows.map(function(r){{return r.admin || '(unknown)';}}))].sort();
  const months = [...new Set(remakeRows.map(function(r){{return r.month || '?';}}))].sort();
  var af = document.getElementById('remakeAdminFilter');
  admins.forEach(function(a){{var opt=document.createElement('option');opt.value=a;opt.textContent=a;af.appendChild(opt);}});
  var mf = document.getElementById('remakeMonthFilter');
  months.forEach(function(m){{var opt=document.createElement('option');opt.value=m;opt.textContent=m;mf.appendChild(opt);}});
  function rerender() {{ renderRemakeMgmt(af.value, mf.value); }}
  af.addEventListener('change', rerender);
  mf.addEventListener('change', rerender);
  document.getElementById('remakeMgmtBody').addEventListener('input', function(ev) {{
    const input = ev.target;
    if (!input.classList.contains('remake-edit')) return;
    const row = remakeRows.find(function(r) {{ return remakeOrderKey(r) === input.closest('tr').dataset.order; }});
    if (!row) return;
    if (input.classList.contains('remake-original-order')) {{ row.original_order = input.value; remakeDirtyFields.set(remakeOrderKey(row), new Set([...(remakeDirtyFields.get(remakeOrderKey(row)) || []), 'original_order'])); }}
    if (input.classList.contains('remake-category')) {{ row.category = input.value; remakeDirtyFields.set(remakeOrderKey(row), new Set([...(remakeDirtyFields.get(remakeOrderKey(row)) || []), 'category'])); }}
    if (input.classList.contains('remake-culprit')) {{ row.culprit = input.value; if (row.culprit !== 'Custimoo') row.culprit_subcategory = ''; remakeDirtyFields.set(remakeOrderKey(row), new Set([...(remakeDirtyFields.get(remakeOrderKey(row)) || []), 'culprit', 'culprit_subcategory'])); updateCustimooSubcategoryCell(row, remakeOrderKey(row), input); }}
    if (input.classList.contains('remake-culprit-subcategory')) {{ row.culprit_subcategory = input.value; remakeDirtyFields.set(remakeOrderKey(row), new Set([...(remakeDirtyFields.get(remakeOrderKey(row)) || []), 'culprit_subcategory'])); }}
    if (input.classList.contains('remake-comment')) {{ row.comment = input.value; remakeDirtyFields.set(remakeOrderKey(row), new Set([...(remakeDirtyFields.get(remakeOrderKey(row)) || []), 'comment'])); }}
    scheduleRemakeSave();
  }});
  document.querySelectorAll('.tab[data-target="remake-mgmt"]').forEach(function(btn){{
    btn.addEventListener('click', function(){{setTimeout(rerender,0);}});
  }});
  rerender();
  fetch(REMAKE_DATA_URL + '?v=' + Date.now())
    .then(function(resp) {{ if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); }})
    .then(function(saved) {{ mergeSavedRemakes(saved); rerender(); renderForensics(); setRemakeSaveStatus('Loaded saved annotations'); }})
    .catch(function() {{ setRemakeSaveStatus(REMAKE_SAVE_URL ? 'Using embedded annotations' : 'Local only — save endpoint unavailable'); }});
}})();

// ── QC Rejections work queue ──
var qcRejectionRows = QC_REJECTIONS.slice();
var qcRejectionSaveTimer = null;
var qcSaveInFlight = false;
var qcSavePending = false;
var qcDirtyFields = new Map();
function qcRejectionKey(r) {{ return String(r.order || '').replace(/^#/, '').trim(); }}
function qcRejectionHandled(r) {{ return Boolean(String(r.error_type || '').trim()); }}
function setQcRejectionSaveStatus(text) {{
  const el = document.getElementById('qcRejectionSaveStatus');
  if (el) el.textContent = text;
}}
function qcBackendOrderHtml(r) {{
  const id = String(r.backend_id || '').trim();
  if (!id) return '#' + esc(qcRejectionKey(r));
  return '<a href="' + QC_BACKEND_URL + '/order/' + escapeAttr(id) + '/detail" target="_blank" rel="noopener" style="color:#2563eb;text-decoration:underline">#' + esc(qcRejectionKey(r)) + '</a>';
}}
function qcTrackingHtml(r) {{
  const no = String(r.tracking_no || '').trim();
  const link = String(r.tracking_link || '').trim();
  const linkHtml = /^https?:\\/\\//i.test(link) ? '<a href="' + escapeAttr(link) + '" target="_blank" rel="noopener">Tracking link</a>' : (link ? esc(link) : '');
  if (no && linkHtml) return esc(no) + '<br>' + linkHtml;
  if (no) return esc(no);
  if (linkHtml) return linkHtml;
  return r.shipped ? 'No tracking' : '—';
}}
function renderQcRejections() {{
  const rows = qcRejectionRows.slice().sort(function(a,b) {{
    const handledCmp = qcRejectionHandled(a) - qcRejectionHandled(b);
    if (handledCmp) return handledCmp;
    return String(b.date || '').localeCompare(String(a.date || '')) || (parseInt(String(b.order || '').replace(/\D/g,''),10) || 0) - (parseInt(String(a.order || '').replace(/\D/g,''),10) || 0);
  }});
  const body = document.getElementById('qcRejectionBody');
  if (!body) return;
  body.innerHTML = rows.map(function(r) {{
    const order = qcRejectionKey(r);
    const selectedCustimoo = r.error_type === 'Custimoo error' ? ' selected' : '';
    const selectedFactory = r.error_type === 'Factory error' ? ' selected' : '';
    const selectedBoth = r.error_type === 'BOTH' ? ' selected' : '';
    return '<tr data-order="' + escapeAttr(order) + '">'
      + '<td data-label="Order" class="order-num">' + qcBackendOrderHtml(r) + '</td>'
      + '<td data-label="QC Date">' + esc(r.date || r.month || '') + '</td>'
      + '<td data-label="Factory">' + esc(r.factory || '') + '</td>'
      + '<td data-label="Items">' + esc(r.items || '') + '</td>'
      + '<td data-label="QC Comment" class="qc-comment">' + esc(r.qc_comment || '—') + '</td>'
      + '<td data-label="Order QTY" class="right">' + (r.total_qty || 0).toLocaleString() + '</td>'
      + '<td data-label="QTY Checked" class="right">' + (r.sample_qty || 0).toLocaleString() + '</td>'
      + '<td data-label="Defect QTY" class="right">' + (r.defects_qty || 0).toLocaleString() + '</td>'
      + '<td data-label="Severity">' + esc(r.severities || 'Not specified') + '</td>'
      + '<td data-label="Inspector">' + esc(r.inspectors || '') + '</td>'
      + '<td data-label="Final QC Approved">' + (r.final_approved ? 'Yes' + (r.final_approval_type ? ' · ' + esc(r.final_approval_type) : '') + (r.final_approval_date ? ' · ' + esc(r.final_approval_date) : '') : 'No') + '</td>'
      + '<td data-label="Reinspection">' + esc(r.reinspection_status || 'No') + '</td>'
      + '<td data-label="Reinspection Count" class="right">' + (r.reinspection_count || 0).toLocaleString() + '</td>'
      + '<td data-label="Latest Reinspection Date">' + esc(r.latest_reinspection_date || '—') + '</td>'
      + '<td data-label="Shipped">' + (r.shipped ? 'Yes' + (r.shipping_date ? ' · ' + esc(r.shipping_date) : '') : 'No') + '</td>'
      + '<td data-label="Tracking">' + qcTrackingHtml(r) + '</td>'
      + '<td data-label="Error Type"><select class="qc-edit qc-error-type" aria-label="Error type for order ' + escapeAttr(order) + '"><option value="">Investigating</option><option value="Custimoo error"' + selectedCustimoo + '>Custimoo error</option><option value="Factory error"' + selectedFactory + '>Factory error</option><option value="BOTH"' + selectedBoth + '>BOTH</option></select></td>'
      + '<td data-label="How to Avoid"><textarea class="qc-edit qc-avoidance" aria-label="How to avoid error for order ' + escapeAttr(order) + '" placeholder="Preventive action">' + esc(r.avoidance_action || '') + '</textarea></td>'
      + '<td data-label="Work Notes"><input class="qc-edit qc-work-comment" aria-label="Work notes for order ' + escapeAttr(order) + '" value="' + escapeAttr(r.work_comment || '') + '" placeholder="Investigation / follow-up"></td>'
      + '</tr>';
  }}).join('') || '<tr><td colspan="19">No eligible QC rejections in the report period.</td></tr>';
  document.getElementById('qcRejectionCount').textContent = rows.length + ' rejected orders';
}}
function saveQcRejections() {{
  if (!QC_REJECTIONS_SAVE_URL) {{ setQcRejectionSaveStatus('Local only — save endpoint unavailable'); return; }}
  if (qcSaveInFlight) {{ qcSavePending = true; return; }}
  qcSaveInFlight = true;
  qcSavePending = false;
  setQcRejectionSaveStatus('Saving…');
  const dirty = new Map(qcDirtyFields);
  fetch(QC_REJECTIONS_DATA_URL + '?merge=' + Date.now()).then(function(resp) {{ if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); }}).then(function(saved) {{
    const latest = Array.isArray(saved) ? saved : Object.keys(saved || {{}}).map(function(k) {{ return Object.assign({{}}, saved[k] || {{}}, {{order:String(k).replace(/^#/,'').trim()}}); }});
    const byOrder = new Map(latest.map(function(r) {{ return [qcRejectionKey(r), r]; }}));
    qcRejectionRows.forEach(function(r) {{ const s=byOrder.get(qcRejectionKey(r)); const dirtyFields=dirty.get(qcRejectionKey(r)) || new Set(); if (s) {{ if (!dirtyFields.has('error_type')) r.error_type=s.error_type||r.error_type||''; if (!dirtyFields.has('avoidance_action')) r.avoidance_action=s.avoidance_action||r.avoidance_action||''; if (!dirtyFields.has('work_comment')) r.work_comment=s.work_comment||r.work_comment||''; }} }});
    dirty.forEach(function(fields, key) {{ const row=qcRejectionRows.find(function(r) {{ return qcRejectionKey(r)===key; }}); if (!row) return; const target=byOrder.get(key) || row; fields.forEach(function(field) {{ target[field]=row[field] || ''; }}); }});
    const merged = latest.concat(qcRejectionRows.filter(function(r) {{ return !byOrder.has(qcRejectionKey(r)); }}));
    return fetch(QC_REJECTIONS_SAVE_URL, {{method:'PUT', headers:{{'x-ms-blob-type':'BlockBlob','Content-Type':'application/json'}}, body:JSON.stringify(merged)}});
  }}).then(function(resp) {{ if (!resp.ok) throw new Error('HTTP ' + resp.status); qcDirtyFields.clear(); qcSaveInFlight = false; setQcRejectionSaveStatus('Saved globally'); if (qcSavePending) setTimeout(saveQcRejections, 0); }}).catch(function(err) {{ qcSaveInFlight = false; console.warn('QC rejection save failed', err); setQcRejectionSaveStatus('Save failed — retrying on next edit'); if (qcSavePending) setTimeout(saveQcRejections, 0); }});
}}
function scheduleQcRejectionSave() {{
  clearTimeout(qcRejectionSaveTimer);
  setQcRejectionSaveStatus('Unsaved changes…');
  qcRejectionSaveTimer = setTimeout(saveQcRejections, 700);
}}
function mergeSavedQcRejections(saved) {{
  if (!saved) return;
  const entries = Array.isArray(saved) ? saved : Object.keys(saved).map(function(k) {{ const v = saved[k] || {{}}; return Object.assign({{}}, typeof v === 'object' ? v : {{}}, {{ order: String(k).replace(/^#/, '').trim() }}); }});
  if (!entries.length) return;
  const byOrder = new Map(entries.map(function(r) {{ return [qcRejectionKey(r), r]; }}));
  qcRejectionRows.forEach(function(r) {{
    const savedRow = byOrder.get(qcRejectionKey(r));
    if (savedRow) {{ r.error_type = savedRow.error_type || r.error_type || ''; r.avoidance_action = savedRow.avoidance_action || r.avoidance_action || ''; r.work_comment = savedRow.work_comment || r.work_comment || ''; }}
  }});
}}
(function() {{
  if (!document.getElementById('qcRejectionBody')) return;
  renderQcRejections();
  document.getElementById('qcRejectionBody').addEventListener('input', function(ev) {{
    const input = ev.target;
    if (!input.classList.contains('qc-edit')) return;
    const rowEl = input.closest('tr');
    const row = qcRejectionRows.find(function(r) {{ return qcRejectionKey(r) === rowEl.dataset.order; }});
    if (!row) return;
    if (input.classList.contains('qc-error-type')) {{ row.error_type = input.value; qcDirtyFields.set(qcRejectionKey(row), new Set([...(qcDirtyFields.get(qcRejectionKey(row)) || []), 'error_type'])); }}
    if (input.classList.contains('qc-avoidance')) {{ row.avoidance_action = input.value; qcDirtyFields.set(qcRejectionKey(row), new Set([...(qcDirtyFields.get(qcRejectionKey(row)) || []), 'avoidance_action'])); }}
    if (input.classList.contains('qc-work-comment')) {{ row.work_comment = input.value; qcDirtyFields.set(qcRejectionKey(row), new Set([...(qcDirtyFields.get(qcRejectionKey(row)) || []), 'work_comment'])); }}
    scheduleQcRejectionSave();
  }});
  document.querySelectorAll('.tab[data-target="qc-rejections"]').forEach(function(btn){{ btn.addEventListener('click', function(){{setTimeout(renderQcRejections,0);}}); }});
  fetch(QC_REJECTIONS_DATA_URL + '?v=' + Date.now())
    .then(function(resp) {{ if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); }})
    .then(function(saved) {{ mergeSavedQcRejections(saved); renderQcRejections(); renderForensics(); setQcRejectionSaveStatus('Loaded saved annotations'); }})
    .catch(function() {{ setQcRejectionSaveStatus(QC_REJECTIONS_SAVE_URL ? 'Using embedded QC rejection data' : 'Local only — save endpoint unavailable'); }});
}})();
renderForensics();

function factorySlug(name) {{ return String(name||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,''); }}
function showFactorySharePage(slug) {{
  const f = (DATA.factories || []).find(function(x) {{ return factorySlug(x.name) === slug; }});
  if (!f) return false;
  document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelectorAll('.tab').forEach(function(t) {{ t.classList.remove('active'); }});
  document.getElementById('factory-share-page').classList.add('active');
  document.getElementById('factoryShareTitle').textContent = f.name + ' — Factory Performance';
  const q=f.qarma||{{}};
  document.getElementById('factoryShareKpis').innerHTML = [['Total Orders',f.orders||0],['Total Order QTY',f.volume||0],['Remake Orders',f.remake_orders||0],['Remake QTY',f.remake_qty||0],['Qarma Orders Checked',q.orders_checked||0],['Qarma Error Orders',q.rejected_orders||0],['Orders with Reinspection',q.orders_with_reinspection||0]].map(function(x) {{ return '<div class="card metric"><div class="label">'+x[0]+'</div><div class="value">'+Number(x[1]||0).toLocaleString()+'</div></div>'; }}).join('');
  const params = new URLSearchParams(window.location.search);
  const periodKey = params.get('period') || 'all';
  const periodSelect = document.getElementById('factorySharePeriod'); periodSelect.value=periodKey; periodSelect.addEventListener('change', function() {{ params.set('period', this.value); window.location.search=params.toString(); }});
  const period = PERIODS[periodKey] || DATA;
  const periodFactory = (period.factories || []).find(function(x) {{ return x.name === f.name; }}) || f;
  const periodQ = periodFactory.qarma || {{}};
  document.getElementById('factoryShareKpis').innerHTML = [['Total Orders',periodFactory.orders||0,'all'],['Total Order QTY',periodFactory.volume||0,'all'],['Remake Orders',periodFactory.remake_orders||0,'remake'],['Remake QTY',periodFactory.remake_qty||0,'remake'],['Qarma Orders Checked',periodQ.orders_checked||0,'qarma_checked'],['Qarma Error Orders',periodQ.rejected_orders||0,'qarma_rejected'],['Orders with Reinspection',periodQ.orders_with_reinspection||0,'reinspection']].map(function(x) {{ return '<div class="card metric factory-filter-card" data-filter="'+x[2]+'" title="Click to show matching orders" style="cursor:pointer"><div class="label">'+x[0]+'</div><div class="value">'+Number(x[1]||0).toLocaleString()+'</div></div>'; }}).join('');
  const periodMonths = new Set(period.monthKeys || MONTH_KEYS);
  const orders = (FACTORY_SHARE_ORDERS[f.name] || []).filter(function(r) {{ return periodMonths.has(String(r.created_date || '').slice(0,7)); }});
  const qarmaPeriodMonths = new Set(period.monthKeys || MONTH_KEYS);
  orders.forEach(function(r) {{ const qm=new Set(r.qarma_months || []); r.qarma_checked_period = Array.from(qm).some(function(m) {{ return qarmaPeriodMonths.has(m); }}); r.qarma_rejected_period = r.qarma_checked_period && r.qarma_rejected; }});
  const reinspectionCard=document.querySelector('.factory-filter-card[data-filter="reinspection"]'); if (reinspectionCard) reinspectionCard.querySelector('.value').textContent=orders.filter(function(r) {{ return r.qarma_reinspected; }}).length.toLocaleString();
  function dateOnly(v) {{ return String(v||'').slice(0,10) || '—'; }}
  function renderFactoryOrderRows(list) {{ document.getElementById('factoryShareOrders').innerHTML = list.map(function(r) {{ const qlink=/^https?:\\/\\//i.test(r.qarma_report_link||'') ? '<a href="'+escapeAttr(r.qarma_report_link)+'" target="_blank" rel="noopener">Open report</a>' : '—'; return '<tr><td>#'+esc(r.order)+'</td><td>'+esc(r.customer_ref||'—')+'</td><td>'+dateOnly(r.actual_shipping_date)+'</td><td>'+dateOnly(r.completed_date)+'</td><td>'+dateOnly(r.backend_shipping_date)+'</td><td>'+esc(r.customer||'')+'</td><td class="right">'+Number(r.qty||0).toLocaleString()+'</td><td>'+ (r.qarma_checked_period?'Yes':'No') +'</td><td>'+ (r.qarma_rejected_period?'Yes':'No') +'</td><td>'+ (r.qarma_reinspected?'Yes':'No') +'</td><td>'+esc(r.qarma_comment||'—')+'</td><td>'+qlink+'</td><td>'+ (r.remake?'Yes':'No') +'</td></tr>'; }}).join('') || '<tr><td colspan="13">No matching completed orders in the selected period.</td></tr>'; }}
  function renderFactoryMonthly(list) {{
    const byMonth={{}};
    list.forEach(function(r) {{ const k=String(r.actual_shipping_date||r.completed_date||'').slice(0,7); if (!k) return; const x=byMonth[k]||(byMonth[k]={{orders:0,qty:0,remakes:0,remake_qty:0,qarma_checked:0,qarma_errors:0}}); x.orders++; x.qty+=Number(r.qty||0); if (r.remake) {{ x.remakes++; x.remake_qty+=Number(r.qty||0); }} if (r.qarma_checked_period) x.qarma_checked++; if (r.qarma_rejected_period) x.qarma_errors++; }});
    const keys=Object.keys(byMonth).sort().reverse();
    document.getElementById('factoryShareMonthly').innerHTML=keys.map(function(k) {{ const x=byMonth[k]; return '<tr><td>'+esc(MONTH_LABELS[k]||k)+'</td><td class="right">'+x.orders.toLocaleString()+'</td><td class="right">'+x.qty.toLocaleString()+'</td><td class="right">'+x.remakes.toLocaleString()+'</td><td class="right">'+x.remake_qty.toLocaleString()+'</td><td class="right">'+x.qarma_checked.toLocaleString()+'</td><td class="right">'+x.qarma_errors.toLocaleString()+'</td></tr>'; }}).join('') || '<tr><td colspan="7">No selected orders in the period.</td></tr>';
  }}
  const selectedCard=document.getElementById('factoryShareSelectedTitle').closest('.card'); const kpiGrid=document.getElementById('factoryShareKpis'); if (selectedCard && kpiGrid && kpiGrid.parentNode) kpiGrid.parentNode.insertBefore(selectedCard, kpiGrid.nextSibling);
  renderFactoryOrderRows(orders);
  renderFactoryMonthly(orders);
  function renderFactoryAnalysis() {{
    const qErrors=orders.filter(function(r) {{ return r.qarma_rejected_period; }});
    document.getElementById('factoryShareQarmaErrors').innerHTML=qErrors.map(function(r) {{ const link=/^https?:\\/\\//i.test(r.qarma_report_link||'') ? '<a href="'+escapeAttr(r.qarma_report_link)+'" target="_blank" rel="noopener">Open report</a>' : '—'; return '<tr><td>#'+esc(r.order)+'</td><td>'+dateOnly(r.actual_shipping_date)+'</td><td>Yes</td><td>'+ (r.qarma_reinspected?'Yes':'No') +'</td><td>'+esc(r.qarma_comment||'—')+'</td><td>'+link+'</td></tr>'; }}).join('') || '<tr><td colspan="6">No Qarma errors for this factory and period.</td></tr>';
    const matchFactory=function(r) {{ return factorySlug(r.factory||r.raw_factory||'')===factorySlug(f.name); }};
    const matchPeriod=function(r) {{ const d=String(r.month||r.date||r.fu_month||'').slice(0,7); return !d || periodMonths.has(d); }};
    const rem=(typeof REMAKES!=='undefined'?REMAKES:[]).filter(function(r) {{ return matchFactory(r) && matchPeriod(r); }});
    document.getElementById('factoryShareRemakeAnalysis').innerHTML=rem.map(function(r) {{ return '<tr><td>#'+esc(r.order||'')+'</td><td>'+Number(r.qty||r.total_qty||0).toLocaleString()+'</td><td>'+esc(r.category||'—')+'</td><td>'+esc(r.culprit||'—')+'</td><td>'+esc(r.culprit_subcategory||'—')+'</td><td>'+esc(r.source||'—')+'</td><td>'+esc(r.verification_status||'—')+'</td><td>'+esc(r.comment||'—')+'</td></tr>'; }}).join('') || '<tr><td colspan="8">No remake analysis rows for this factory and period.</td></tr>';
    const qc=(typeof QC_REJECTIONS!=='undefined'?QC_REJECTIONS:[]).filter(function(r) {{ return matchFactory(r) && matchPeriod(r); }});
    document.getElementById('factoryShareQcAnalysis').innerHTML=qc.map(function(r) {{ return '<tr><td>#'+esc(r.order||'')+'</td><td>'+esc(dateOnly(r.date||r.month))+'</td><td>'+esc(r.qc_comment||'—')+'</td><td>'+esc(r.error_type||'Investigating')+'</td><td>'+esc(r.reinspection_status||'No')+'</td><td>'+esc(r.avoidance_action||'—')+'</td><td>'+esc(r.work_comment||'—')+'</td></tr>'; }}).join('') || '<tr><td colspan="7">No QC analysis rows for this factory and period.</td></tr>';
    const pctDone=function(n,d) {{ return d ? Math.round(n/d*100)+'%' : '—'; }};
    const remCategory=rem.filter(function(r) {{ return r.category && r.category !== 'Uncategorized'; }}).length;
    const remCulprit=rem.filter(function(r) {{ return r.culprit && r.culprit !== 'Select culprit'; }}).length;
    const remVerified=rem.filter(function(r) {{ return r.verification_status && !/needs verification/i.test(r.verification_status); }}).length;
    const qcAttributed=qc.filter(function(r) {{ return r.error_type && r.error_type !== 'Investigating'; }}).length;
    const qcAvoidance=qc.filter(function(r) {{ return String(r.avoidance_action||'').trim(); }}).length;
    const qcNotes=qc.filter(function(r) {{ return String(r.work_comment||'').trim(); }}).length;
    document.getElementById('factoryShareProgress').innerHTML=[['Remakes — category set',remCategory,rem.length],['Remakes — culprit set',remCulprit,rem.length],['Remakes — verified',remVerified,rem.length],['QC — attribution set',qcAttributed,qc.length],['QC — How to Avoid',qcAvoidance,qc.length],['QC — Work Notes',qcNotes,qc.length]].map(function(x) {{ return '<div class="card metric"><div class="label">'+x[0]+'</div><div class="value">'+x[1]+' / '+x[2]+'</div><div class="sub">'+pctDone(x[1],x[2])+' complete</div></div>'; }}).join('');
  }}
  renderFactoryAnalysis();
  document.querySelectorAll('.factory-filter-card').forEach(function(card) {{ card.addEventListener('click', function() {{ const filter=card.dataset.filter; const subset=filter==='all' ? orders : filter==='remake' ? orders.filter(function(r) {{ return r.remake; }}) : filter==='qarma_checked' ? orders.filter(function(r) {{ return r.qarma_checked_period; }}) : filter==='qarma_rejected' ? orders.filter(function(r) {{ return r.qarma_rejected_period; }}) : orders.filter(function(r) {{ return r.qarma_reinspected; }}); const labels={{all:'All completed orders',remake:'Remake orders',qarma_checked:'Qarma checked orders',qarma_rejected:'Qarma rejected orders',reinspection:'Orders with reinspection'}}; document.getElementById('factoryShareSelectedTitle').textContent='Selected Orders — '+labels[filter]+' ('+subset.length+')'; renderFactoryOrderRows(subset); renderFactoryMonthly(subset); document.getElementById('factoryShareSelectedTitle').scrollIntoView({{behavior:'smooth',block:'start'}}); }}); }});
  return true;
}}
const factoryShareMatch = window.location.pathname.toLowerCase().match(/^\/factory\/([a-z0-9-]+)$/);
if (factoryShareMatch) showFactorySharePage(factoryShareMatch[1]);

function renderHummelAccountPage() {{
  const d = HUMMEL_DATA;
  const pct = function(n, den) {{ return den > 0 ? (n / den * 100).toFixed(2) + '%' : '—'; }};
  document.getElementById('hummelTotalOrders').textContent = (d.totalOrders || 0).toLocaleString();
  document.getElementById('hummelTotalQty').textContent = (d.totalVolume || 0).toLocaleString();
  document.getElementById('hummelRemakeOrders').textContent = (d.totalRemakeOrders || 0).toLocaleString();
  document.getElementById('hummelRemakeQty').textContent = (d.totalRemakeQty || 0).toLocaleString();
  document.getElementById('hummelOrderErrorRate').textContent = 'Remake Orders Error%: ' + pct(d.totalRemakeOrders || 0, d.totalOrders || 0);
  document.getElementById('hummelQtyErrorRate').textContent = 'Remake QTY Error%: ' + pct(d.totalRemakeQty || 0, d.totalVolume || 0);
  const remO = d.monthlyRemakeOrders || [], remQ = d.monthlyRemakeQty || [];
  document.getElementById('hummelMonthlyBody').innerHTML = (d.months || []).map(function(m, i) {{ const orders = ((d.monthlyOrders || [])[i] || 0), volume = ((d.monthlyVolume || [])[i] || 0), remakeOrders = remO[i] || 0, remakeQty = remQ[i] || 0; return '<tr><td><strong>' + esc(m) + '</strong></td><td class="right">' + orders.toLocaleString() + '</td><td class="right">' + volume.toLocaleString() + '</td><td class="right">' + remakeOrders.toLocaleString() + '</td><td class="right">' + pct(remakeOrders, orders) + '</td><td class="right">' + remakeQty.toLocaleString() + '</td><td class="right">' + pct(remakeQty, volume) + '</td></tr>'; }}).join('');
  document.getElementById('hummelFactoryBody').innerHTML = (d.factories || []).map(function(f) {{ const orders = f.orders || 0, volume = f.volume || 0, remakeOrders = f.remake_orders || 0, remakeQty = f.remake_qty || 0; return '<tr><td><strong>' + esc(f.name || '') + '</strong></td><td class="right">' + orders.toLocaleString() + '</td><td class="right">' + volume.toLocaleString() + '</td><td class="right">' + remakeOrders.toLocaleString() + '</td><td class="right">' + pct(remakeOrders, orders) + '</td><td class="right">' + remakeQty.toLocaleString() + '</td><td class="right">' + pct(remakeQty, volume) + '</td></tr>'; }}).join('');
}}
function showHummelAccountPage() {{
  document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelectorAll('.tab').forEach(function(t) {{ t.classList.remove('active'); }});
  const page = document.getElementById('hummel-pro-na');
  if (page) page.classList.add('active');
  renderHummelAccountPage();
}}
if (window.location.hash.toLowerCase() === '#hummel-pro-na' || window.location.pathname.toLowerCase() === '/hummel-pro-na') showHummelAccountPage();

</script>
</body>
</html>"""

out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report.html')
with open(out_path, 'w') as f:
    f.write(html)
print("Written:", out_path, "(%d bytes)" % len(html))
