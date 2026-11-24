"""Procurement Desk agent: deterministic implementation of Acme's playbook.

Usage: python agent.py <data_dir> <threads.csv> <out.csv>
<data_dir> must contain playbook/ (extracted playbook.zip).
"""
import re, sys, glob, os, math
from decimal import Decimal, ROUND_HALF_UP
import pandas as pd

# ---------------------------------------------------------------- playbook loading
def money(s):
    return float(s.replace('₹', '').replace(',', '').strip())

def load_playbook(root):
    pb = os.path.join(root, 'playbook')
    if os.path.isdir(os.path.join(pb, 'playbook')):
        pb = os.path.join(pb, 'playbook')
    cats = {}
    for f in glob.glob(os.path.join(pb, 'categories', '*.md')):
        txt = open(f, encoding='utf-8').read()
        g = lambda k: re.search(r'\*\*' + re.escape(k) + r'.*?:\*\*\s*(.+)', txt).group(1).strip()
        unit = g('Normalized unit')
        c = dict(
            name=re.search(r'# Category:\s*(.+)', txt).group(1).strip(),
            unit=unit,
            period='month' if 'per month' in unit else ('year' if 'per year' in unit else None),
            gst=float(g('GST rate').rstrip('%')) / 100,
            target=money(g('Target price')),
            walkaway=money(g('Walk-away price')),
            R=int(g('Negotiation rounds')),
            beta=float(g('Concession exponent')),
            limit=money(g('Approval limit')),
        )
        m = re.search(r'Pack conversion:\*\*\s*(.+)', txt)
        c['pack'] = m.group(1) if m else ''
        cats[os.path.basename(f)[:-3]] = c
    name2slug = {v['name'].lower(): k for k, v in cats.items()}
    name2slug['cleaning services'] = 'cleaning'
    name2slug['training workshops'] = 'training'
    name2slug['courier services'] = 'courier'

    amds = []
    for f in sorted(glob.glob(os.path.join(pb, 'amendments', '*.md'))):
        txt = open(f, encoding='utf-8').read()
        date = re.search(r'Effective from:\*\*\s*(\d{4}-\d{2}-\d{2})', txt).group(1)
        m = re.search(r'the (approval limit|target price|walk-away price) for (.+?) is now (₹[\d,]+)', txt)
        field = {'approval limit': 'limit', 'target price': 'target', 'walk-away price': 'walkaway'}[m.group(1)]
        amds.append((date, name2slug[m.group(2).strip().lower()], field, money(m.group(3))))
    amds.sort()

    sups = {}
    for f in glob.glob(os.path.join(pb, 'suppliers', '*.md')):
        txt = open(f, encoding='utf-8').read()
        name = re.search(r'# Supplier:\s*(.+)', txt).group(1).strip()
        st = re.search(r'Status:\*\*\s*(\w+)', txt).group(1).upper()
        s = dict(status=st, caps={}, fx={})
        for m in re.finditer(r'price cap for (.+?) of (₹[\d,]+)', txt):
            s['caps'][name2slug[m.group(1).strip().lower()]] = money(m.group(2))
        for m in re.finditer(r'invoices in ([A-Z]{3}) are converted at ₹([\d.]+)', txt):
            s['fx'][m.group(1)] = float(m.group(2))
        sups[name] = s
    return cats, amds, sups

FX = {'USD': 84.5, 'EUR': 91.0, 'GBP': 106.0}

# ---------------------------------------------------------------- number parsing
WORDS = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
    'fifteen sixteen seventeen eighteen nineteen'.split())}
for i, w in enumerate('twenty thirty forty fifty sixty seventy eighty ninety'.split()):
    WORDS[w] = 20 + 10 * i
SCALES = {'hundred': 100, 'thousand': 1000, 'lakh': 100000, 'lakhs': 100000, 'crore': 10**7}

def words_to_num(s):
    total, cur = 0, 0
    for tok in re.split(r'[\s-]+', s.lower().strip()):
        if tok in ('and', ''):
            continue
        if tok in WORDS:
            cur += WORDS[tok]
        elif tok == 'hundred':
            cur *= 100
        elif tok in SCALES:
            total += max(cur, 1) * SCALES[tok]; cur = 0
        else:
            return None
    return total + cur

NUMWORD = r'(?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakhs?|crore|and)[\s-]*)+'

CUR = r'(?P<cur>₹|Rs\.?\s?|INR\s?|\$|USD\s?|US\$\s?|€|EUR\s?|£|GBP\s?|[A-Z]{3}\s)'
NUM = r'(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<mult>k|K|lakh|lakhs|crore)?\b'

def cur_code(c):
    c = c.strip().rstrip('.')
    return {'₹': 'INR', 'Rs': 'INR', 'INR': 'INR', '$': 'USD', 'USD': 'USD', 'US$': 'USD',
            '€': 'EUR', 'EUR': 'EUR', '£': 'GBP', 'GBP': 'GBP'}.get(c, c)

def amount(m):
    v = float(m.group('num').replace(',', ''))
    mult = (m.group('mult') or '').lower()
    v *= {'k': 1e3, 'lakh': 1e5, 'lakhs': 1e5, 'crore': 1e7, '': 1}[mult]
    return v

PRICE_RE = re.compile(CUR + NUM)
WORDPRICE_RE = re.compile(r'(?P<w>' + NUMWORD + r')\s*rupees', re.I)

# ---------------------------------------------------------------- thread splitting
def split_thread(msg):
    """returns (latest_text, earlier_supplier_texts[list])"""
    blocks = re.split(r'\[(Earlier|Latest) message — ([^\]]*)\]', msg)
    if len(blocks) == 1:
        return msg, []
    earlier, latest = [], None
    for i in range(1, len(blocks), 3):
        kind, who, body = blocks[i], blocks[i + 1], blocks[i + 2]
        if kind == 'Latest':
            latest = body
        elif not who.startswith('Acme'):
            earlier.append(body)
    if latest is None:
        latest = blocks[-1]
    return latest, earlier

# ---------------------------------------------------------------- price extraction
class Missing(Exception):
    pass

def fx_rate(code, sup):
    if code == 'INR':
        return 1.0
    if code in sup['fx']:
        return sup['fx'][code]
    if code in FX:
        return FX[code]
    raise Missing(code)

def after(text, pos):
    return text[pos:pos + 160].split('\n')[0]

def unit_adjust(tail, cat, qty, raw):
    """Given the text after a price, convert to per-normalized-unit and period; returns price."""
    p = raw
    t = tail.lower()
    # pack / dozen / per N units / ream
    m = re.match(r'\s*per dozen\b', t)
    if m:
        p /= 12
    m = re.match(r'\s*per (\d+) (?!per)\w+', t)
    if m and not re.match(r'\s*per \d+\s*(?:%|days)', t):
        p /= int(m.group(1))
    m = re.match(r'\s*per pack of (\d+)', t)
    if m:
        p /= int(m.group(1))
    elif re.match(r'\s*per pack\b', t):
        pk = re.search(r'pack holds (\d+)', cat['pack'])
        if pk:
            p /= int(pk.group(1))
    if re.match(r'\s*per ream\b', t):
        rm = re.search(r'= (\d+) reams', cat['pack'])
        p *= int(rm.group(1)) if rm else 10
    # billing period
    seg = re.split(r'[.;,(]| for | with | plus | and ', t)[0] if t else ''
    seg = t[:60]
    per = None
    mm = re.search(r'per (?:\w+ )?(?:per )?(month|year|annum)|/\s*(month|year|mo|yr)|(monthly|yearly|annually)', seg)
    if mm:
        w = next(x for x in mm.groups() if x)
        per = 'month' if w in ('month', 'mo', 'monthly') else 'year'
    if cat['period']:
        if per is None:
            raise Missing('period')
        if per != cat['period']:
            p = p * 12 if per == 'month' else p / 12
    # GST
    if re.search(r'(incl\.?|including|inclusive of)\s*(of\s*)?gst|gst included|gst inclusive|incl gst', t[:80]):
        p /= (1 + cat['gst'])
    return p

def find_prices(text):
    out = []
    for m in PRICE_RE.finditer(text):
        out.append((m.start(), m.end(), cur_code(m.group('cur')), amount(m)))
    for m in WORDPRICE_RE.finditer(text):
        v = words_to_num(m.group('w'))
        if v:
            out.append((m.start(), m.end(), 'INR', float(v)))
    out.sort()
    return out

def offer_price(text, cat, sup, qty, prev_quote):
    """Normalized price of the offer in `text`. prev_quote: callable returning normalized prev price."""
    t = text
    # discount off previous quote
    m = re.search(r'(\d+(?:\.\d+)?)\s*% off (?:our|my) previous quote', t)
    if m:
        pq = prev_quote()
        if pq is None:
            raise Missing('prev')
        return pq * (1 - float(m.group(1)) / 100)
    # correction
    m = re.search(r'should read\s+(.+?), not', t)
    if m:
        t = m.group(1) + ' ' + t[m.end():]
        pr = find_prices(m.group(1))
        if pr:
            s, e, c, v = pr[0]
            return unit_adjust(m.group(1)[e:], cat, qty, v * fx_rate(c, sup))
    prices = find_prices(t)
    if not prices:
        raise Missing('noprice')
    # list-price discount
    m = re.search(r'(\d+(?:\.\d+)?)\s*% discount on our list price of', t)
    if m:
        s, e, c, v = next(x for x in prices if x[0] > m.end() - 1)
        p = unit_adjust(t[e:], cat, qty, v * fx_rate(c, sup))
        return p * (1 - float(m.group(1)) / 100)
    # totals
    m = re.search(r'in total for all (\d+) \w+', t)
    if m:
        s, e, c, v = [x for x in prices if x[0] < m.start()][-1]
        tail = t[m.end():]
        n = int(m.group(1))
        return unit_adjust(tail, cat, qty, v * fx_rate(c, sup) / n)
    # range: take upper end
    m = re.search(r'between\s+' + CUR + NUM + r'\s+and\s+', t)
    if m:
        s, e, c, v = [x for x in prices if x[0] >= m.end() - 1][0]
        return unit_adjust(t[e:], cat, qty, v * fx_rate(c, sup))
    # tiers
    m = re.search(r'for orders of up to (\d+) \w+, and\s+', t)
    if m:
        n = int(m.group(1))
        first = [x for x in prices if x[0] < m.start()][-1]
        second = [x for x in prices if x[0] >= m.end() - 1][0]
        s, e, c, v = first if qty <= n else second
        return unit_adjust(t[e:], cat, qty, v * fx_rate(c, sup))
    s, e, c, v = prices[0]
    p = unit_adjust(t[e:], cat, qty, v * fx_rate(c, sup))
    # volume discount
    m = re.search(r'further (\d+(?:\.\d+)?)% off for orders of (\d+) \w+ or more', t)
    if m and qty >= int(m.group(2)):
        p *= 1 - float(m.group(1)) / 100
    # free units
    m = re.search(r'plus (\d+) free \w+ for every (\d+) you pay for', t)
    if m:
        k, n = int(m.group(1)), int(m.group(2))
        p = p * n / (n + k)
    return p

# ---------------------------------------------------------------- checks
INJ_RE = re.compile(
    r'(language model|\bAI agent\b|\bAI\b(?![- ]powered)|\bbots?\b|\bLLM\b|chatbot|automated system|'
    r'reviewing assistant|\bassistant\s*:|admin override|\boverride\b|ignore (?:all |your )?(?:previous|prior) instructions|'
    r'pre-approved|approval already granted|already signed off|सहायक|निर्देश|system note|playbook checks|'
    r'output accept|set action|mark (?:this|it) )', re.I)
BENIGN = re.compile(r'AI-powered|system integration team|ERP system|assistant manager', re.I)

def is_injection(msg):
    for line in msg.split('\n'):
        l = BENIGN.sub('', line)
        if INJ_RE.search(l):
            return True
    return False

WORD_PCT = {'half': 50, 'one third': 33.3, 'a third': 33.3, 'two thirds': 66.7, 'a quarter': 25,
            'one quarter': 25, 'three quarters': 75, 'full': 100, 'entire': 100, 'all of': 100}

def banned(latest):
    t = latest.lower()
    if re.search(r'no automatic renewal|not auto-renew|does not (auto-)?renew|renewal only (if|by)', t):
        t2 = re.sub(r'there is no automatic renewal[^\n]*', '', t)
    else:
        t2 = t
    if re.search(r'auto-?renew|automatic(ally)? renew|renews? (on its own|automatically)|'
                 r'year to year unless|continues .* unless|unless cancelled', t2):
        return True
    if re.search(r'unlimited liability|liability [^\n.]*unlimited', t):
        return True
    t3 = re.sub(r'not asking for exclusivity|no exclusivity[^\n]*', '', t)
    if re.search(r'exclusiv|only from us|solely from us|sole supplier', t3):
        return True
    for line in t.split('\n'):
        if re.search(r'advance|upfront|up-front|up front|in advance|prepay', line):
            for m in re.finditer(r'(\d+(?:\.\d+)?)\s*%\s*(?:advance|upfront|up-front|of the order|payment|in advance)', line):
                if float(m.group(1)) > 30:
                    return True
            for w, v in WORD_PCT.items():
                if re.search(r'\b' + w + r'\b', line) and v > 30:
                    return True
    return False

# ---------------------------------------------------------------- decision
def rhalf(x):
    return int(Decimal(repr(x)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

def params(cats, amds, sups, slug, supplier, date):
    c = dict(cats[slug])
    for d, s, f, v in amds:
        if s == slug and d <= date:
            c[f] = v
    sup = sups.get(supplier, dict(status='APPROVED', caps={}, fx={}))
    if slug in sup['caps']:
        c['walkaway'] = sup['caps'][slug]
    return c, sup

def decide(row, cats, amds, sups, debug=False):
    slug = row['category']
    c, sup = params(cats, amds, sups, slug, row['supplier'], row['date'])
    qty = int(row['quantity'])
    r = int(row['round'])
    msg = row['supplier_message']
    latest, earlier = split_thread(msg)

    def prev_quote():
        for e in reversed(earlier):
            try:
                return offer_price(e, c, sup, qty, lambda: None)
            except Missing:
                continue
        return None

    # 1 missing info
    try:
        p = offer_price(latest, c, sup, qty, prev_quote)
    except Missing as ex:
        return 'ESCALATE:MISSING_INFO:', ('missing', str(ex))
    # 2 blacklist
    if sup['status'].startswith('BLACKLIST'):
        return 'REJECT:BLACKLISTED_SUPPLIER:', p
    # 3 injection
    if is_injection(msg):
        return 'ESCALATE:PROMPT_INJECTION:', p
    # 4 banned
    if banned(latest):
        return 'ESCALATE:BANNED_TERM:', p
    # 5 price
    R, beta = c['R'], c['beta']
    if sup['status'].startswith('PROBATION'):
        L = c['target']
    else:
        frac = min(max((r - 1) / (R - 1), 0), 1)
        L = c['target'] + (c['walkaway'] - c['target']) * frac ** beta
    if p > L * (1 + 1e-12) + 1e-9:
        if r >= R:
            return 'REJECT:ABOVE_WALKAWAY:', (p, L)
        return f'COUNTER:COUNTER_OFFER:{rhalf(L)}', (p, L)
    # 6 approval
    if p * qty > c["limit"] * (1 + 1e-12) + 1e-6:
        return 'ESCALATE:OVER_APPROVAL_LIMIT:', (p, L, p * qty, c['limit'])
    return 'ACCEPT:ACCEPTABLE_PRICE:', (p, L)

def main(data_dir, threads, out, debug_out=None):
    cats, amds, sups = load_playbook(data_dir)
    t = pd.read_csv(threads)
    labels, dbg = [], []
    for _, row in t.iterrows():
        lab, info = decide(row, cats, amds, sups)
        labels.append(lab); dbg.append(info)
    t['label'] = labels
    t[['thread_id', 'label']].to_csv(out, index=False)
    if debug_out:
        t['dbg'] = dbg
        t.to_csv(debug_out, index=False)
    return t

if __name__ == '__main__':
    main(*sys.argv[1:])
