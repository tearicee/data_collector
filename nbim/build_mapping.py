"""
Build mapping between NBIM Taiwan holdings and Taiwan stock codes.
Step 1: Load stock_info.xlsx (codes + English/Chinese names)
Step 2: Load unique TW companies from NBIM CSV
Step 3: Automated matching (fuzzy + keyword)
Step 4: Output unmatched for manual/web lookup
"""

import csv
import re
import openpyxl
from collections import defaultdict

# ── Load stock_info.xlsx ──
wb = openpyxl.load_workbook('/home/tearicee/data_collector/nbim/stock_info.xlsx', read_only=True)
ws = wb['t187ap03_L']

stocks = []
for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
    code = row[1]  # 公司代號
    full_name = row[2]  # 公司名稱
    short_name = row[3]  # 公司簡稱
    eng_abbrev = row[27]  # 英文簡稱
    if code is None:
        continue
    stocks.append({
        'code': str(code),
        'full_name': str(full_name or ''),
        'short_name': str(short_name or ''),
        'eng_abbrev': str(eng_abbrev or ''),
    })
wb.close()

print(f"Loaded {len(stocks)} stocks from stock_info.xlsx")

# ── Load NBIM TW companies ──
tw_companies = set()
with open('/home/tearicee/data_collector/nbim/nbim_all_holdings.csv', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row['country_code'] == 'TW':
            tw_companies.add(row['company_name'])

tw_companies = sorted(tw_companies)
print(f"Found {len(tw_companies)} unique TW companies in NBIM data")

# ── Normalize function ──
def normalize(s):
    """Normalize string for comparison"""
    s = s.lower().strip()
    # Remove common suffixes
    for suffix in [' co ltd', ' co. ltd', ' co., ltd', ' co.,ltd',
                   ' corp', ' corporation', ' inc', ' ltd', ' co',
                   ' holding', ' holdings', ' enterprise', ' enterprises',
                   ' international', ' group', '/taiwan', '/the', '/tw']:
        s = s.replace(suffix, '')
    s = re.sub(r'[^a-z0-9\s]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

# Build lookup indices
eng_norm_map = {}  # normalized eng_abbrev -> stock
for s in stocks:
    key = normalize(s['eng_abbrev'])
    if key and len(key) > 1:
        eng_norm_map[key] = s

# ── Matching ──
matched = {}  # nbim_name -> stock info
unmatched = []

for name in tw_companies:
    norm_name = normalize(name)

    # Skip government bonds
    if 'government of taiwan' in name.lower():
        matched[name] = {'code': 'GOV', 'short_name': '台灣政府公債', 'eng_abbrev': 'Government of Taiwan', 'match_method': 'manual'}
        continue

    best_match = None
    best_method = None

    # Method 1: Exact normalized match with eng_abbrev
    if norm_name in eng_norm_map:
        best_match = eng_norm_map[norm_name]
        best_method = 'exact_norm'

    # Method 2: Check if eng_abbrev is contained in NBIM name or vice versa
    if not best_match:
        for s in stocks:
            eng = normalize(s['eng_abbrev'])
            if not eng or len(eng) < 3:
                continue
            if eng == norm_name or norm_name == eng:
                best_match = s
                best_method = 'exact'
                break
            # NBIM name contains stock eng name
            if len(eng) >= 5 and eng in norm_name:
                best_match = s
                best_method = 'contains'
                break
            # Stock eng name contains NBIM name
            if len(norm_name) >= 5 and norm_name in eng:
                best_match = s
                best_method = 'reverse_contains'
                break

    # Method 3: Word-based matching
    if not best_match:
        name_words = set(norm_name.split())
        if len(name_words) >= 2:
            best_score = 0
            for s in stocks:
                eng = normalize(s['eng_abbrev'])
                if not eng:
                    continue
                eng_words = set(eng.split())
                if len(eng_words) < 2:
                    continue
                common = name_words & eng_words
                # Score based on overlap ratio
                score = len(common) / max(len(name_words), len(eng_words))
                if score > best_score and score >= 0.5 and len(common) >= 2:
                    best_score = score
                    best_match = s
                    best_method = f'word_match({score:.2f})'

    if best_match:
        matched[name] = {
            'code': best_match['code'],
            'short_name': best_match['short_name'],
            'eng_abbrev': best_match['eng_abbrev'],
            'full_name': best_match['full_name'],
            'match_method': best_method,
        }
    else:
        unmatched.append(name)

print(f"\nMatched: {len(matched)}")
print(f"Unmatched: {len(unmatched)}")

# Save unmatched for review
with open('/home/tearicee/data_collector/nbim/nbim_tw_unmatched.txt', 'w', encoding='utf-8') as f:
    for name in unmatched:
        f.write(name + '\n')

# Save matched for review
with open('/home/tearicee/data_collector/nbim/nbim_tw_matched_preliminary.csv', 'w', newline='', encoding='utf-8-sig') as f:
    writer = csv.writer(f)
    writer.writerow(['nbim_name', 'stock_code', 'chinese_short_name', 'eng_abbrev', 'match_method'])
    for name in sorted(matched.keys()):
        m = matched[name]
        writer.writerow([name, m['code'], m['short_name'], m['eng_abbrev'], m['match_method']])

print("\nUnmatched companies:")
for name in unmatched:
    print(f"  {name}")
