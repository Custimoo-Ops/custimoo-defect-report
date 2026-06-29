#!/usr/bin/env python3
"""
Custimoo Failure Report — generates error % trend from fu@custimoo.com vs backend.

Outputs three buckets: Exact (known from email) / Estimate (full-order proxy) / Skipped (non-defects).

Usage: python3 scripts/report.py
Requires: CUSTIMOO_GRAPH_* env vars, SSH tunnel to RDS on port 3307, pymysql
"""

import pymysql, os, json, urllib.request, urllib.parse, re
from collections import defaultdict

# ── Word-number dictionary ──
WORD_NUMS = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
    'thirty': 30, 'forty': 40, 'fifty': 50,
}
WORD_NUM_PATTERN = r'\b(' + '|'.join(WORD_NUMS.keys()) + r')\b'

# ── Manual overrides ──
# Exact counts (actual defective items as stated in or deduced from email bodies)
MANUAL_EXACT = {
    "22643": 30,    # "30 Ringette Pants"
    "24722": 143,   # "143 jerseys"
    "24516": 20,    # "all 20 pieces"
    "23613": 10,    # "10 pant shells"
    "22007": 2,     # "2 YM retail units"
    "24753": 1,     # Single goalie jersey remake
    "23939": 13,    # 13 pants of 26 total
    "23553": 44,    # All pants defective zippers
    "20153": 12,    # "12 Varsity Jackets"
    "22541": 3,     # "3 bags had bleeding from logos"
    "20126": 2,     # "2 polo shirts"
    "22341": 2,     # "Two jerseys printed with wrong numbers" (re: 507-item order!)
    "24728": 202,   # Socks remake — all 202 affected
    "23055": 165,   # Missing branding from ALL pieces on 165pc order
    "22699": 166,   # Blue buttons instead of white — all 166 jerseys
    "18913": 135,   # Bottom strap issue on all 135 ringette pants
    "21708": 521,   # Snap button issue — all 521 affected (QC rejection)
    "22788": 304,   # Missing pcs — all 304 missing (shipping error)
    "21163": 388,   # Wrong sizing on entire 388pc sizerun
    "19345": 264,   # Wrong size/labels in socks — all 264 pairs
    "22412": 269,   # Factory missed shipping PCs — all 269
    "22595": 150,   # Fabric issue — all 150 affected
    "21599": 162,   # Wrong patterns on all 162
    "24059": 134,   # Dark kit remake — all 134
    "21685": 200,   # Sizing issue on volleyball order — full order affected
    "23010": 3,     # 3 jerseys failed QC in size run
    "23375": 1,     # Admin (Samar Abbas) confirmed 1 affected — CSM approved dispatch
    "23427": 50,    # Printed backwards — full batch
    "23514": 31,    # Goalie jersey number sizing wrong
    "23690": 15,    # Admin (Haider Abbas) confirmed 15 affected
    "23719": 100,   # Gusset placement wrong — all affected
    "23761": 12,    # Admin (Haider Abbas) confirmed 12 affected
    "23762": 13,    # Missing brand logo — Lavoie order
    "24096": 50,    # Vancouver Rise badge color incorrect
    "24262": 50,    # Missing pcs — Pelle report
    "19810": 2,     # Glitch — end user error (2 items)
    "20234": 21,    # Wrong sizes
    "21545": 10,    # Multiple issues
    "19636": 2,     # "wrong color in 2 nameplates"
    "20416": 18,    # Fw: Discount request — 18
    "20371": 18,    # Fw: Discount request — 18
    "20985": 2,     # Hurricanes ringette pants
    "19711": 10,    # Ringette pant warranty
    "20055": 1,     # FW: Order Error
    "20123": 1,     # FW: Order
    "21247": 10,    # Your receipt from Custimoo
    "22972": 2,     # Missing jerseys
    "22983": 1,     # Bishop Ryan account
    "22007": 2,     # Union Omaha remake
    # Admin-confirmed exact quantities from order admin replies (Jun 2026)
    "19192": 63,    # Admin (Haider Abbas) confirmed 63/126 affected
    "21019": 1,     # Admin (Haider Abbas) confirmed 1/10 affected
    "21254": 1,     # Admin (Haider Abbas) confirmed 1/60 affected
    "21575": 33,    # Admin (Haider Abbas) confirmed 33/33 — all affected
    "20044": 4,     # Admin (Haseeb Iqbal) confirmed 4/4
    "20506": 25,    # Admin (Haseeb Iqbal) confirmed 25/25
    "20617": 24,    # Admin (Haseeb Iqbal) confirmed 24/24
    "20919": 5,     # Admin (Haseeb Iqbal) confirmed 5/92
    "21288": 30,    # Admin (Haseeb Iqbal) confirmed 30/30
    "21560": 12,    # Admin (Haseeb Iqbal) confirmed 12/24
    "21598": 12,    # Admin (Haseeb Iqbal) confirmed 12/12
    "21704": 6,     # Admin (Haseeb Iqbal) confirmed 6/6
    "22019": 10,    # Admin (Haseeb Iqbal) confirmed 10/10
    "22886": 16,    # Admin (Haseeb Iqbal) confirmed 16/32
    "20243": 3,     # Admin (Khizer Mir) confirmed 3/20
    "20694": 1,     # Admin (Khizer Mir) confirmed 1/21
    "20893": 50,    # Admin (Salman Faisal) confirmed 50/100
    # June 2026
    "25460": 3,   # 2 jerseys wrong number, 1 missing size
    # Updated affected quantities (Lars, Jun 16)
    "22412": 53,  # was 269
    "24728": 100, # was 202
    "24262": 1,   # was 50
    "23005": 21,  # missing — 21 shirts, Silver-Star
}

# Zero overrides (excluded — not production defects)
MANUAL_ZERO = {
    # Delay / process issues only
    "24040": 0, "23298": 0, "19937": 0, "20611": 0, "21274": 0,
    "21365": 0, "22220": 0, "22394": 0, "22874": 0, "23113": 0,
    "22906": 0, "19005": 0, "20988": 0, "22513": 0, "22558": 0,
    "22585": 0, "22608": 0, "23311": 0, "23955": 0, "21988": 0,
    "21545": 0,  # Process delays, not a defect
    # Customer/process non-defects
    "24976": 0,  # No OA assigned
    "23650": 0,  # Customer mistaken — resolved non-issue
    "24802": 0,  # Shipped hoping customer accepts
    "23834": 0,  # Stripes mismatch — no remake decided
    "17293": 0,  # Original order of a remake
    # Admin-confirmed zero affected (not actual defects)
    "22784": 0,  # Admin (Haider Abbas) — 0 affected
    "23309": 0,  # Admin (Haider Abbas) — 0 affected
    "20889": 0,  # Admin (Haseeb Iqbal) — 0 affected
    "21879": 0,  # Admin (Haseeb Iqbal) — 0 affected
    "22099": 0,  # Admin (Haseeb Iqbal) — 0 affected
    "22612": 0,  # Admin (Haseeb Iqbal) — 0 affected
    "22769": 0,  # Admin (Haseeb Iqbal) — 0 affected
    "21183": 0,  # Admin (Khizer Mir) — 0 affected
    "23646": 0,  # Admin (Haider Abbas) — 0 affected (was estimated 50)
    # Delays (not production defects)
    "22475": 0, "24555": 0,
}

MANUAL = {**MANUAL_EXACT, **MANUAL_ZERO}

# ── Product word pattern (must be broad enough to catch all variations) ──
PRODUCT_WORDS = (
    r'(jerseys?|pcs\\.?|pieces?|units?|items?|pairs?|pants?|shells?'
    r'|socks?|bags?|jackets?|shirts?|polos?|hoodies?|nameplates?|nameplate)'
)

# ── Non-product SKU IDs (name bars, fight straps, logos, accessories) ──
NON_PRODUCT_SKUS = {
    "Name patch", "BAUER S-Line Tackle Twill", "Skate soakers (1pair)",
    "Skatemat", "Name Plate", "Fight Strap",
}


def classify_product(order_no, db_conn):
    """Fetch and classify product type for an order. Returns (category, is_product)."""
    cur = db_conn.cursor()
    # Check all order_items for sku_id, sku_name, product_name, design_name
    cur.execute("""
        SELECT JSON_EXTRACT(oi.factory_products, '$[0].sku.sku_id') as sku_id,
               JSON_EXTRACT(oi.factory_products, '$[0].sku_name') as sku_name,
               JSON_EXTRACT(oi.factory_products, '$[0].product_name') as prod_name,
               JSON_EXTRACT(oi.factory_products, '$[0].design_nick_name') as design_name
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE o.order_no = %s
        LIMIT 1
    """, (order_no,))
    row = cur.fetchone()
    if not row:
        return "Unknown", True  # default to product if no data

    names = []
    for val in row:
        if val and val != "null" and val != b"null":
            try:
                parsed = json.loads(val) if isinstance(val, (str, bytes)) else val
            except (json.JSONDecodeError, TypeError):
                parsed = str(val)
            if parsed and str(parsed).strip():
                names.append(str(parsed).strip())

    # Check non-product SKUs
    for name in names:
        if name in NON_PRODUCT_SKUS:
            return "Name Plate" if "name" in name.lower() else "Accessory", False
        n_lower = name.lower()
        if "twill" in n_lower and "jersey" not in n_lower:
            return "Twill", False
        if ("name" in n_lower and ("plate" in n_lower or "patch" in n_lower or "bar" in n_lower)):
            return "Name Plate", False
        if "fight strap" in n_lower:
            return "Fight Strap", False
        if "skate" in n_lower or n_lower.startswith("mat") or " mat " in n_lower or "mat." in n_lower or "skatemat" in n_lower:
            return "Accessory", False
        if "neck tape" in n_lower or "care label" in n_lower:
            return "Trim/Accessory", False
        if ("logo" in n_lower and len(n_lower) < 30
                and "jersey" not in n_lower and "shirt" not in n_lower):
            return "Logo/Patch", False

    # Proper product classification
    category = classify_category(names[0] if names else "")
    return category, True


def classify_category(name):
    """Classify a product name into a broad category. Returns string."""
    n = name.lower()
    if "jersey" in n or "jersy" in n or "fullbutton" in n or "breakaway" in n or "powerplay" in n or "primetime" in n or "showtime" in n:
        return "Jersey"
    if "sock" in n:
        return "Socks"
    if "pant" in n or "knicker" in n or ("shell" in n and "pant" not in n):
        return "Pants/Knickers"
    if "hoodie" in n or "hoody" in n:
        return "Hoodie"
    if "jacket" in n:
        return "Jacket"
    if "polo" in n:
        return "Polo"
    if "shirt" in n or "tee" in n or "compression" in n or "racerback" in n or "razorback" in n or "tank" in n or "sleeveless" in n:
        return "Shirt"
    if "short" in n:
        return "Shorts"
    if "bag" in n or "backpack" in n or "duffel" in n or "duffle" in n or "puckbag" in n or "puck bag" in n:
        return "Bags"
    if "vest" in n or "goalie" in n:
        return "Vest/Goalie"
    if "towel" in n:
        return "Towel"
    if "sweater" in n:
        return "Sweater"
    if "gaiter" in n or "neck" in n:
        return "Neck Gaiter"
    if "cap" in n or "hat" in n or "beanie" in n or "toque" in n:
        return "Headwear"
    if "keeper" in n or "goalie" in n:
        return "Goalie Gear"
    if "warmup" in n or "track" in n:
        return "Warmup/Track"
    if "football" in n:
        return "Football"
    if "baseball" in n:
        return "Baseball"
    if "basketball" in n:
        return "Basketball"
    return "Other"


def extract_qty(mentions):
    """Extract affected quantity from all messages mentioning an order.
    Returns int (qty) or None."""
    all_text = "".join(m["subject"] + " " + m["body"] for m in mentions)[:5000]
    all_text = re.sub(r"https?://\S+", " ", all_text)

    # Pattern 1: digit + optional adjectives + product word
    pat1 = re.findall(r'(\d+)\s+(?:\w+\s+){0,3}' + PRODUCT_WORDS, all_text, re.IGNORECASE)
    if pat1:
        for num_str, _ in sorted(pat1, key=lambda x: int(x[0]), reverse=True):
            q = int(num_str)
            if 1 <= q <= 2000:
                return q

    # Pattern 2: word-number + optional adjectives + product word
    pat_w = re.findall(
        WORD_NUM_PATTERN + r'\s+(?:\w+\s+){0,3}' + PRODUCT_WORDS,
        all_text, re.IGNORECASE
    )
    if pat_w:
        for num_str, _ in sorted(pat_w, key=lambda x: WORD_NUMS[x[0].lower()], reverse=True):
            q = WORD_NUMS[num_str.lower()]
            if 1 <= q <= 200:
                return q

    # Pattern 3: "issue affecting X" / "error impacting X"
    for pat in [
        r'(?:issu(?:e|es)|error|problem|mistake)\s+(?:affecting|impacting|involving)\s+(\d+)',
        r'(?:all|the)\s+(\d+)\s+(?:of\s+the\s+)?' + PRODUCT_WORDS,
        r'(\d+)\s+(?:\w+\s+){0,3}' + PRODUCT_WORDS + r'\s+(?:from|of|in|on)\s+(?:this|the)\s+order',
        r'(?:identified|found|noticed)\s+(?:an?\s+)?(?:issue\s+)?(?:affecting\s+)?(\d+)',
        r'(\d+)\s+(?:\w+\s+){0,3}' + PRODUCT_WORDS + r'\s+(?:were|have|has|had|needs?)',
    ]:
        m = re.search(pat, all_text, re.IGNORECASE | re.MULTILINE)
        if m:
            q = int(m.group(1))
            if 1 <= q <= 2000:
                return q

    return None


def get_fu_messages():
    """Fetch all fu@custimoo.com messages since 2025-10-28."""
    tenant = os.environ["CUSTIMOO_GRAPH_TENANT_ID"]
    client_id = os.environ["CUSTIMOO_GRAPH_CLIENT_ID"]
    client_secret = os.environ["CUSTIMOO_GRAPH_CLIENT_SECRET"]
    body = (
        f"client_id={client_id}&client_secret={client_secret}"
        "&scope=https://graph.microsoft.com/.default"
        "&grant_type=client_credentials"
    )
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = json.loads(urllib.request.urlopen(req).read().decode())["access_token"]

    params = urllib.parse.urlencode({
        "$filter": "receivedDateTime ge 2025-10-28T00:00:00Z",
        "$top": 100,
        "$select": "subject,receivedDateTime,from,body",
        "$orderby": "receivedDateTime asc",
    })
    results = []
    url = f"https://graph.microsoft.com/v1.0/users/fu@custimoo.com/messages?{params}"
    while url:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Prefer": "outlook.body-content-type=text"},
        )
        resp = json.loads(urllib.request.urlopen(req).read().decode())
        results.extend(resp.get("value", []))
        url = resp.get("@odata.nextLink")
    return results


def query_db(order_nums):
    """Query Custimoo backend for pipeline totals and order data.
    Returns (pipeline, orders, conn) — caller must close conn."""
    conn = pymysql.connect(
        host="127.0.0.1", port=3307,
        database="custimoo_backend_prod",
        user="custimoo_backend_usr",
        password=os.environ.get("CUSTIMOO_DB_PASSWORD", ""),
    )
    cur = conn.cursor()

    cur.execute("""
        SELECT DATE_FORMAT(created_at, '%Y-%m') as month,
               SUM(CAST(JSON_EXTRACT(price_info, '$.total_quantity') AS SIGNED)),
               COUNT(*)
        FROM orders
        WHERE created_at >= '2025-09-01 00:00:00'
        GROUP BY DATE_FORMAT(created_at, '%Y-%m')
        ORDER BY month
    """)
    pipeline = {row[0]: {"qty": row[1] or 0, "count": row[2]} for row in cur.fetchall()}

    placeholders = ",".join(["%s"] * len(order_nums))
    cur.execute(
        f"""
        SELECT o.order_no, o.id, o.created_at,
               CAST(JSON_EXTRACT(o.price_info, '$.total_quantity') AS SIGNED) as total_qty
        FROM orders o WHERE o.order_no IN ({placeholders})
        """,
        list(order_nums),
    )
    orders = {}
    for row in cur.fetchall():
        orders[str(row[0])] = {
            "id": row[1],
            "created": str(row[2])[:7] if row[2] else "?",
            "total_qty": row[3] or 0,
        }
    return pipeline, orders, conn


def main():
    print("Fetching fu@custimoo.com messages...", end=" ", flush=True)
    all_msgs = get_fu_messages()
    print(f"{len(all_msgs)} messages")

    # Collect all order mentions across all messages (initial + replies/forwards)
    all_orders = defaultdict(list)
    for m in all_msgs:
        subj = m["subject"]
        content = m.get("body", {}).get("content", "")
        month = m["receivedDateTime"][:7]
        nums = re.findall(
            r"(?:order|Order|ORDER)?\s*#?\s*(\d{4,6})",
            subj + " " + content[:2000],
        )
        nums = set(n for n in nums if 10000 <= int(n) <= 99999)
        for n in nums:
            all_orders[n].append({"month": month, "body": content, "subject": subj})

    # Track first fu report month per order (initial emails only, not replies)
    order_fu_month = {}
    for m in all_msgs:
        subj = m["subject"]
        month = m["receivedDateTime"][:7]
        if subj.startswith(("Re:", "Fw:", "RE:", "FW:")):
            continue
        nums = re.findall(r"(?:order|Order|ORDER)?\s*#?\s*(\d{4,6})", subj)
        for n in set(n for n in nums if 10000 <= int(n) <= 99999):
            if n not in order_fu_month:
                order_fu_month[n] = month

    print(f"  {len(all_orders)} unique orders referenced")

    # Query DB
    print("Querying backend database...", end=" ", flush=True)
    pipeline, db_orders, db_conn = query_db(set(all_orders.keys()))
    print(f"{len(db_orders)} orders found in DB")

    # Classify all orders by product type
    product_types = {}
    non_product_orders = set()
    for ono in all_orders:
        cat, is_product = classify_product(ono, db_conn)
        product_types[ono] = cat
        if not is_product:
            non_product_orders.add(ono)

    # Determine affected qty per order
    order_info = {}
    for ono in all_orders:
        db = db_orders.get(ono, {"total_qty": 0, "created": "?"})
        full_qty = db["total_qty"]

        # Skip non-product orders entirely
        if ono in non_product_orders:
            continue

        if ono in MANUAL:
            affected = MANUAL[ono]
            source = "manual"
        else:
            extracted = extract_qty(all_orders[ono])
            if extracted and extracted > 0:
                affected = extracted
                source = "extracted"
            else:
                affected = full_qty
                source = "estimate"

        if affected == 0:
            continue  # skip zero-affected orders

        order_info[ono] = {
            "affected": affected,
            "full_qty": full_qty,
            "source": source,
            "created_month": db["created"],
            "product_type": product_types.get(ono, "Unknown"),
        }

    db_conn.close()

    # Aggregate by creation month — three buckets
    monthly_exact = defaultdict(int)
    monthly_estimate = defaultdict(int)
    monthly_skipped = defaultdict(int)
    monthly_counts = defaultdict(lambda: {"exact": 0, "estimate": 0, "skipped": 0})
    product_type_totals = defaultdict(int)
    product_type_order_count = defaultdict(int)

    for ono, info in order_info.items():
        cm = info["created_month"]
        if cm not in pipeline:
            continue
        af = info["affected"]
        src = info["source"]

        # Track product type
        pt = info.get("product_type", "Unknown")
        product_type_totals[pt] += af
        product_type_order_count[pt] += 1

        if src == "manual" and af > 0:
            monthly_exact[cm] += af
            monthly_counts[cm]["exact"] += 1
        elif src == "extracted":
            monthly_exact[cm] += af
            monthly_counts[cm]["exact"] += 1
        elif af == 0:
            monthly_skipped[cm] += 1
            monthly_counts[cm]["skipped"] += 1
        else:
            monthly_estimate[cm] += af
            monthly_counts[cm]["estimate"] += 1

    # ── Print report ──
    print()
    print("=" * 90)
    print("CUSTIMOO FAILURE REPORT — BY ORDER VINTAGE")
    print("=" * 90)
    header = (
        f"{'Month':<8} {'Pipeline':>10} {'Exact':>7} {'Estimate':>10}"
        f" {'Skip':>5} {'Orders':>11} {'Err%':>8}"
    )
    print(header)
    print("-" * 59)

    total_pipeline = total_exact = total_estimate = 0
    all_skipped = 0

    for month in sorted(pipeline.keys()):
        pipe = pipeline[month]["qty"]
        exact = monthly_exact.get(month, 0)
        estimate = monthly_estimate.get(month, 0)
        cc = monthly_counts.get(month, {"exact": 0, "estimate": 0, "skipped": 0})
        skipped = cc["skipped"]
        upper = exact + estimate
        rate = (upper / pipe * 100) if pipe > 0 else 0

        total_pipeline += pipe
        total_exact += exact
        total_estimate += estimate
        all_skipped += skipped

        bar = "█" * int(rate * 3)
        order_str = f"{cc['exact']}/{cc['estimate']}/{skipped}"
        print(
            f"{month:<8} {pipe:>10,} {exact:>7,} {estimate:>10,}"
            f" {skipped:>5} {order_str:>11} {rate:>7.2f}%  {bar}"
        )

    total_upper = total_exact + total_estimate
    total_rate = (total_upper / total_pipeline * 100) if total_pipeline > 0 else 0
    print("-" * 59)
    print(
        f"{'TOTAL':<8} {total_pipeline:>10,} {total_exact:>7,} {total_estimate:>10,}"
        f" {all_skipped:>5}"
        f"           {total_rate:>7.2f}%"
    )

    print()
    print("  Exact = items we KNOW were defective (from email body or manual override)")
    print("  Estimate = full order qty as proxy (email didn't state how many affected)")
    print("  Skip = orders excluded as non-defects (delays, process issues, etc.)")
    print(f"  Err% = (Exact + Estimate) / Pipeline — upper bound")
    print()
    total_manual = sum(1 for v in order_info.values() if v["source"] == "manual" and v["affected"] > 0)
    total_skipped = sum(1 for v in order_info.values() if v["affected"] == 0)
    total_extracted = sum(1 for v in order_info.values() if v["source"] == "extracted")
    total_estimated = sum(1 for v in order_info.values() if v["source"] == "estimate")
    print(f"  Orders with manual override (exact):   {total_manual}")
    print(f"  Orders with regex-extracted qty:        {total_extracted}")
    print(f"  Orders with full-order estimate:        {total_estimated}")
    print(f"  Orders excluded (non-defects):          {total_skipped}")
    print(f"  Total orders in report:                 {total_manual + total_extracted + total_estimated + total_skipped}")

    # Product type breakdown
    print()
    print("─" * 60)
    print("PRODUCT TYPE BREAKDOWN (proper products only — excludes name bars, fight straps, logos, accessories)")
    print("─" * 60)
    print(f"{'Product Type':<22s} {'Affected Items':>15s} {'Orders':>8s} {'% of Total':>10s}")
    print("-" * 60)
    for pt, total in sorted(product_type_totals.items(), key=lambda x: -x[1]):
        pct = total / sum(product_type_totals.values()) * 100
        bar = chr(9608) * max(1, int(pct / 2))
        print(f"  {pt:<22s} {total:>10,d}  {product_type_order_count[pt]:>4d}  {pct:>6.1f}%  {bar}")
    print("-" * 60)
    print(f"  {'TOTAL':<22s} {sum(product_type_totals.values()):>10,d}  {sum(product_type_order_count.values()):>4d}")

    # Non-product exclusion summary
    if non_product_orders:
        np_total = sum(
            MANUAL.get(ono, 0)
            for ono in non_product_orders
            if ono in MANUAL and MANUAL[ono] > 0
        )
        print()
        print(f"  Non-product orders excluded from report: {len(non_product_orders)}")
        for ono in sorted(non_product_orders):
            tag = product_types.get(ono, "?")
            a = MANUAL.get(ono, 0)
            print(f"    #{ono:<5s} {tag:<18s} ({a} items)" if a > 0 else f"    #{ono:<5s} {tag:<18s}")
        print(f"    Total affected items excluded: {np_total:,}")


if __name__ == "__main__":
    main()
