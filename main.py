import os
import json
import time
import logging
import cloudscraper
import html
import re
import concurrent.futures
import feedparser
import hashlib
from urllib.parse import quote, unquote, urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from ddgs import DDGS
from gnews import GNews
from dateutil import parser
import trafilatura

# ───────────────────────── CONFIGURATION ─────────────────────────
CONFIG = {
    # Search themes: macro economy, energy/commodities, econ-relevant geopolitics,
    # tech, crypto, gaming. NO general politics for its own sake.
    'SEARCH_QUERIES': [
        # اقتصاد و بازار
        '(Federal Reserve OR ECB OR "interest rate" OR inflation OR recession) global economy',
        '(stock market OR "Wall Street" OR S&P500 OR Nasdaq) (crash OR rally OR record OR selloff)',
        'banking crisis OR sovereign debt OR credit downgrade',
        # انرژی و کالا
        '(oil price OR OPEC OR "natural gas") major move',
        'gold price OR "Treasury yields" major move',
        # ژئوپلیتیک و سیاست اقتصادی
        'sanctions OR tariffs global trade impact',
        '"Strait of Hormuz" OR "Suez Canal" shipping disruption',
        'US China trade OR export controls',
        'Russia Ukraine OR Middle East economic impact',
        # تکنولوژی
        '(OpenAI OR "Google DeepMind" OR Anthropic OR Nvidia OR Microsoft OR Meta OR Apple) (AI OR launch OR breakthrough)',
        '(Nvidia OR Apple OR Microsoft OR Amazon OR Google OR Tesla) (earnings OR lawsuit OR antitrust OR layoffs)',
        'semiconductor OR chip shortage OR chip export',
        'major cyberattack OR data breach company',
        # کریپتو و بلاکچین
        'Bitcoin OR Ethereum (ETF OR regulation OR hack OR crash OR rally OR all-time high)',
        'crypto (SEC OR regulation OR exchange OR stablecoin) major',
        '"exchange hack" OR "bridge exploit" OR "smart contract exploit" crypto',
        # گیم
        '(PlayStation OR Xbox OR Nintendo OR Steam) major announcement OR acquisition',
        'game studio (acquisition OR shutdown OR layoffs)',
    ],
    'TARGET_SOURCES': [
        'bloomberg.com', 'reuters.com', 'cnbc.com', 'wsj.com', 'ft.com',
        'apnews.com', 'coindesk.com', 'theblock.co', 'techcrunch.com',
        'theverge.com', 'axios.com', 'businessinsider.com',
        'oilprice.com', 'kitco.com', 'ign.com', 'gamesindustry.biz',
    ],
    'SOURCE_PRIORITY': {
        'bloomberg.com': 10, 'reuters.com': 10, 'wsj.com': 9, 'ft.com': 9,
        'cnbc.com': 8, 'apnews.com': 8, 'coindesk.com': 8, 'theblock.co': 8,
        'techcrunch.com': 6, 'theverge.com': 6, 'axios.com': 6,
        'businessinsider.com': 5, 'oilprice.com': 6, 'kitco.com': 6,
        'gamesindustry.biz': 6, 'ign.com': 5,
    },
    'FILES': {
        'NEWS': 'news.json',
        'MARKET': 'market.json',
        'SCHEDULE_STATE': 'schedule_state.json',
        'SITE_CONFIG': 'config.json',
    },
    'TELEGRAM': {
        'BOT_TOKEN': os.environ.get('TG_BOT_TOKEN'),
        'CHANNEL_ID': os.environ.get('TG_CHANNEL_ID'),
    },
    'TIMEOUT': 12,
    'AI_TIMEOUT': 90,
    'MAX_WORKERS': 3,
    # Candidates that actually reach the AI call, after the free keyword
    # pre-filter below. Kept moderate on purpose: with a 120B model on
    # Workers AI's free 10,000-neurons/day allocation, each extra AI call
    # eats into that budget across all 4 daily runs.
    'MAX_CANDIDATES': 30,
    'MAX_TEXT_CHARS': 2500,
    'MIN_TEXT_LEN': 100,
    # Pre-AI (free, keyword-based) cutoff — see _cheap_urgency_hint. Anything
    # below this AND without a substantial article body is dropped before
    # ever costing an AI call.
    'MIN_AI_URGENCY_HINT': 4,
    'POLLINATIONS_KEY': os.environ.get('POLLINATIONS_API_KEY'),
    'CF_ACCOUNT_ID': os.environ.get('CF_ACCOUNT_ID'),
    'CF_API_TOKEN': os.environ.get('CF_API_TOKEN'),
    'CF_ACCOUNT_ID_2': os.environ.get('CF_ACCOUNT_ID_2'),
    'CF_API_TOKEN_2': os.environ.get('CF_API_TOKEN_2'),
    'CF_MODEL': os.environ.get('CF_MODEL', '@cf/openai/gpt-oss-120b'),
    'GEMINI_API_KEY': os.environ.get('GEMINI_API_KEY'),
    'GEMINI_MODEL': os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash'),
    'AI_RETRIES': 3,
    # Storage bar (site/archive) — same bar is used for Telegram now, so
    # everything that's stored also gets sent (per user request).
    'MIN_TELEGRAM_URGENCY': 4,
    # Cap on how many items go out in a single digest message/run. If more
    # than this qualify in one run, the rest simply go out on the next run
    # (they stay "unsent" and get picked up again) — nothing is lost, it's
    # just spread out so one run doesn't blast a huge wall of text.
    'MAX_DIGEST_ITEMS': 20,
    'MAX_NEWS_AGE_HOURS': 20,
    # Total items kept in news.json / shown on the site. Newest-first; anything
    # beyond this count is dropped as new items come in.
    'HISTORY_SIZE': 300,
}

BAD_IMAGE_HOSTS = (
    'lh3.googleusercontent.com', 'lh4.googleusercontent.com', 'lh5.googleusercontent.com',
    'lh6.googleusercontent.com', 'encrypted-tbn0.gstatic.com', 'encrypted-tbn1.gstatic.com',
    'encrypted-tbn2.gstatic.com', 'encrypted-tbn3.gstatic.com', 'news.google.com',
    'www.google.com', 'google.com',
)

CATEGORY_FALLBACK_IMAGE = {
    'اقتصاد و بازار': 'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?auto=format&fit=crop&w=1200&q=80',
    'انرژی و کالا': 'https://images.unsplash.com/photo-1495107334309-fcf20504a5ab?auto=format&fit=crop&w=1200&q=80',
    'ژئوپلیتیک و سیاست اقتصادی': 'https://images.unsplash.com/photo-1526628953301-3e589a6a8b74?auto=format&fit=crop&w=1200&q=80',
    'تکنولوژی': 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1200&q=80',
    'کریپتو و بلاکچین': 'https://images.unsplash.com/photo-1621761191319-c6fb62004040?auto=format&fit=crop&w=1200&q=80',
    'گیم': 'https://images.unsplash.com/photo-1550745165-9bc0b252726f?auto=format&fit=crop&w=1200&q=80',
}

CATEGORY_EMOJI = {
    'اقتصاد و بازار': '📊',
    'انرژی و کالا': '🛢️',
    'ژئوپلیتیک و سیاست اقتصادی': '🌍',
    'تکنولوژی': '💻',
    'کریپتو و بلاکچین': '🪙',
    'گیم': '🎮',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


class GlobalRadar:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        self.scraper.headers.update({
            'Accept-Language': 'en-US,en;q=0.9,fa;q=0.8',
            'Cache-Control': 'no-cache',
        })
        self.cf_account_id = CONFIG['CF_ACCOUNT_ID']
        self.cf_api_token = CONFIG['CF_API_TOKEN']
        self.cf_model = CONFIG['CF_MODEL']
        # Up to 2 Cloudflare accounts, so writing calls can rotate/fail over
        # between them and roughly double the effective daily neuron budget.
        self.cf_accounts = [
            (aid, tok) for aid, tok in (
                (CONFIG['CF_ACCOUNT_ID'], CONFIG['CF_API_TOKEN']),
                (CONFIG['CF_ACCOUNT_ID_2'], CONFIG['CF_API_TOKEN_2']),
            ) if aid and tok
        ]
        if not self.cf_accounts:
            logger.warning("No Cloudflare account configured (CF_ACCOUNT_ID/CF_API_TOKEN missing).")
        self.gemini_api_key = CONFIG['GEMINI_API_KEY']
        self.gemini_model = CONFIG['GEMINI_MODEL']
        if not self.gemini_api_key and not self.cf_accounts:
            logger.error("NO AI PROVIDER CONFIGURED: neither Gemini nor Cloudflare credentials are set.")
        self.existing_news = self._load_existing_news()

        self.seen_urls = set()
        self.seen_titles = set()
        self.recent_title_hashes = set()
        self.failed_hosts = set()

        for item in self.existing_news:
            if item.get('url'):
                self.seen_urls.add(self._clean_url(item['url']))
            for key in ('title_en', 'title_fa'):
                if item.get(key):
                    self.seen_titles.add(self._normalize_text(item[key]))
                    self.recent_title_hashes.add(self._title_hash(item[key]))

        if len(self.recent_title_hashes) > 250:
            self.recent_title_hashes = set(list(self.recent_title_hashes)[-200:])

        self.gnews_en = GNews(language='en', country='US', period='6h', max_results=6)

    # ───────────────────────── helpers ─────────────────────────

    def _get_tehran_time(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("Asia/Tehran"))
        except ImportError:
            return datetime.now(timezone(timedelta(hours=3, minutes=30)))

    def _clean_url(self, url):
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean.rstrip('/')
        except Exception:
            return url

    def _normalize_text(self, text):
        if not text:
            return ""
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return re.sub(r'\s+', '', clean)

    def _title_hash(self, title):
        return hashlib.md5(self._normalize_text(title).encode('utf-8')).hexdigest()

    def _get_tokens(self, text):
        if not text:
            return set()
        stop_words = {
            'a', 'an', 'the', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
            'is', 'are', 'was', 'news', 'report', 'breaking',
        }
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return set(clean.split()) - stop_words

    def _is_duplicate_fuzzy(self, new_title, comparison_pool):
        norm_title = self._normalize_text(new_title)
        if norm_title in self.seen_titles:
            return True
        new_tokens = self._get_tokens(new_title)
        if len(new_tokens) < 3:
            return False
        pool = comparison_pool[:60] if len(comparison_pool) > 60 else comparison_pool
        for item in pool:
            existing_title = item.get('title_en') or item.get('title_fa') or item.get('title', '')
            existing_tokens = self._get_tokens(existing_title)
            if not existing_tokens:
                continue
            inter = new_tokens.intersection(existing_tokens)
            union = new_tokens.union(existing_tokens)
            if union and (len(inter) / len(union)) > 0.5:
                return True
        return False

    def _load_existing_news(self):
        if not os.path.exists(CONFIG['FILES']['NEWS']):
            return []
        try:
            with open(CONFIG['FILES']['NEWS'], 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _atomic_json_dump(self, path, data):
        tmp = f"{path}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _domain_score(self, url, publisher=""):
        try:
            host = urlparse(url or '').netloc.lower().replace('www.', '')
            for domain, score in CONFIG['SOURCE_PRIORITY'].items():
                if domain in host:
                    return score
        except Exception:
            pass
        pub = (publisher or '').lower()
        for domain, score in CONFIG['SOURCE_PRIORITY'].items():
            if domain.split('.')[0] in pub:
                return score
        return 3

    def _cheap_urgency_hint(self, title, publisher=""):
        """Free, keyword-based pre-filter (no AI call). This is the actual
        triage stage: it decides which candidates are even worth spending an
        AI call on. Kept intentionally simple/fast so it can run over a large
        raw candidate pool without cost."""
        t = (title or '').lower()
        score = 3
        high = [
            'crash', 'plunge', 'soar', 'record high', 'all-time high', 'surge', 'ban',
            'hack', 'collapse', 'bankrupt', 'rate cut', 'rate hike', 'fed', 'sec sues',
            'approve', 'antitrust', 'breakthrough', 'exploit', 'sanction', 'sanctions',
            'strait of hormuz', 'suez canal', 'export control', 'acquire', 'acquisition',
            'shutdown', 'shuts down', 'mass layoffs',
        ]
        mid = [
            'earnings', 'inflation', 'ipo', 'merger', 'lawsuit', 'launch',
            'tariff', 'opec', 'oil price', 'gold price', 'trade war', 'stablecoin',
            'regulation', 'etf', 'layoffs', 'console', 'studio',
        ]
        if any(w in t for w in high):
            score += 3
        if any(w in t for w in mid):
            score += 2
        if self._domain_score('', publisher) >= 8:
            score += 1
        return min(score, 9)

    def _generate_news_id(self, clean_url):
        return hashlib.md5((clean_url or str(time.time())).encode('utf-8')).hexdigest()[:10]

    def _is_valid_image_url(self, url):
        if not url or not isinstance(url, str):
            return False
        u = url.strip()
        if not u.startswith(('http://', 'https://')):
            return False
        if u.startswith('data:'):
            return False
        try:
            host = urlparse(u).netloc.lower().replace('www.', '')
            if any(bad in host for bad in BAD_IMAGE_HOSTS):
                return False
        except Exception:
            return False
        return True

    def _get_fallback_image(self, category):
        return CATEGORY_FALLBACK_IMAGE.get(category, CATEGORY_FALLBACK_IMAGE['اقتصاد و بازار'])

    def _pick_image(self, *candidates, category='اقتصاد و بازار'):
        for c in candidates:
            if self._is_valid_image_url(c):
                return c
        return self._get_fallback_image(category)

    # ───────────────────────── market snapshot ─────────────────────────

    def _scrape_alanchand_price(self, url, mode='irr'):
        """Try several strategies since alanchand pages don't all share the same markup.
        mode='irr' for Toman/Rial-denominated pages (coin, currencies),
        mode='usd' for pages priced directly in US dollars (gold ounce).
        Returns a raw numeric string (as shown on page) or None."""
        try:
            resp = self.scraper.get(url, timeout=12)
            if resp.status_code != 200:
                logger.warning(f"alanchand HTTP {resp.status_code} for {url}")
                return None
            html_text = resp.text
            soup = BeautifulSoup(html_text, 'lxml')

            data_curr = 'tmn' if mode == 'irr' else 'usd'
            # Strategy 1: hidden input used on currency pages
            el = soup.find('input', attrs={'data-curr': data_curr})
            if el:
                val = el.get('data-price') or el.get('value')
                if val:
                    return str(val).replace(',', '')
            # Some pages tag the USD input differently; try both if the first miss
            if mode == 'usd':
                el = soup.find('input', attrs={'data-curr': 'usd_xau'}) or soup.find('input', attrs={'data-curr': 'ounce'})
                if el:
                    val = el.get('data-price') or el.get('value')
                    if val:
                        return str(val).replace(',', '')

            # Strategy 2: schema.org JSON-LD price offers
            for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
                try:
                    payload = json.loads(script.string or '{}')
                    candidates = payload if isinstance(payload, list) else [payload]
                    for c in candidates:
                        offers = c.get('offers') if isinstance(c, dict) else None
                        if isinstance(offers, dict) and offers.get('price'):
                            return str(offers['price']).replace(',', '')
                except Exception:
                    continue

            # Strategy 3: raw-text regex
            if mode == 'irr':
                m = re.search(r'([\d][\d,]{5,})\s*IRR', html_text)
                if m:
                    return m.group(1).replace(',', '')
            else:
                m = re.search(r'\$\s?([\d][\d,]*\.?\d*)\s*(?:USD)?', html_text) or \
                    re.search(r'([\d][\d,]*\.?\d*)\s*USD', html_text)
                if m:
                    return m.group(1).replace(',', '')

            logger.warning(f"alanchand: no price pattern matched on {url}")
        except Exception as e:
            logger.warning(f"alanchand scrape failed for {url}: {e}")
        return None

    def fetch_market_rates(self):
        data = {"btc": "نامشخص", "eth": "نامشخص", "xrp": "نامشخص",
                "usdt_irt": "نامشخص", "coin_irt": "نامشخص", "gold_oz_usd": "نامشخص",
                "updated": "--:--"}
        try:
            resp = self.scraper.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,ripple&vs_currencies=usd&include_24hr_change=true",
                timeout=10
            )
            if resp.status_code == 200:
                j = resp.json()
                btc = j.get('bitcoin', {})
                eth = j.get('ethereum', {})
                xrp = j.get('ripple', {})
                if btc.get('usd'):
                    data['btc'] = f"${btc['usd']:,.0f} ({btc.get('usd_24h_change', 0):+.1f}%)"
                if eth.get('usd'):
                    data['eth'] = f"${eth['usd']:,.0f} ({eth.get('usd_24h_change', 0):+.1f}%)"
                if xrp.get('usd'):
                    data['xrp'] = f"${xrp['usd']:,.3f} ({xrp.get('usd_24h_change', 0):+.1f}%)"
        except Exception as e:
            logger.warning(f"CoinGecko fetch failed: {e}")

        # USDT/Toman, scraped from alanchand (Nobitex's API is unreachable from
        # GitHub Actions runners — persistent DNS failure, not worth retrying).
        usdt_raw = self._scrape_alanchand_price("https://alanchand.com/en/crypto-price/usdt", mode='irr')
        if usdt_raw:
            try:
                data['usdt_irt'] = f"{int(float(usdt_raw) / 10):,} تومان"
            except Exception:
                pass

        # Full gold coin (Emami), priced in Rial -> convert to Toman
        coin_raw = self._scrape_alanchand_price("https://alanchand.com/en/gold-price/sekkeh", mode='irr')
        if coin_raw:
            try:
                toman = float(coin_raw) / 10
                data["coin_irt"] = f"{toman / 1_000_000:.1f}M تومان"
            except Exception:
                pass

        # Gold ounce, priced directly in USD on alanchand
        oz_raw = self._scrape_alanchand_price("https://alanchand.com/en/gold-price/usd_xau", mode='usd')
        if oz_raw:
            try:
                data["gold_oz_usd"] = f"${float(oz_raw):,.2f}"
            except Exception:
                data["gold_oz_usd"] = f"${oz_raw}"

        data["updated"] = self._get_tehran_time().strftime("%H:%M")
        return data

    # ───────────────────────── news search ─────────────────────────

    def fetch_gnews(self, query):
        try:
            return self.gnews_en.get_news(query) or []
        except Exception as e:
            logger.error(f"GNews Error: {e}")
            return []

    def fetch_duckduckgo(self, query, max_results=8):
        results = []
        try:
            ddgs = DDGS()
            for r in ddgs.news(query=query, region='wt-wt', safesearch="off", timelimit="d", max_results=max_results):
                results.append({
                    'title': r.get('title'),
                    'url': r.get('url'),
                    'publisher': {'title': r.get('source')},
                    'published date': r.get('date'),
                    'description': r.get('body'),
                    'image': r.get('image'),
                })
        except Exception as e:
            logger.error(f"DDG Error ({query[:40]}): {e}")
        return results

    def fetch_bing_rss(self, query):
        results = []
        try:
            url = f"https://www.bing.com/news/search?q={quote(query)}&format=rss"
            feed = feedparser.parse(url)
            for entry in feed.entries:
                publisher = "Bing News"
                if hasattr(entry, 'news_source'):
                    publisher = entry.news_source
                elif hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    publisher = entry.source.title
                final_link = entry.link
                if "apiclick.aspx" in final_link:
                    m = re.search(r'[?&]url=([^&]+)', final_link)
                    if m:
                        final_link = unquote(m.group(1))
                results.append({
                    'title': entry.title,
                    'url': final_link,
                    'publisher': {'title': publisher},
                    'published date': entry.published if hasattr(entry, 'published') else None,
                    'description': entry.summary if hasattr(entry, 'summary') else entry.title,
                    'image': None,
                })
        except Exception as e:
            logger.error(f"Bing RSS Error: {e}")
        return results

    def fetch_manual_url(self, url):
        try:
            resp = self.scraper.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'lxml')
            title = soup.title.string if soup.title else "Unknown Title"
            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = og_title.get("content")
            publisher = "Manual Source"
            og_site = soup.find("meta", property="og:site_name")
            if og_site:
                publisher = og_site.get("content")
            image = None
            og_image = soup.find("meta", property="og:image")
            if og_image:
                image = og_image.get("content")
            return [{
                'title': title, 'url': url, 'publisher': {'title': publisher},
                'published date': datetime.now(timezone.utc).isoformat(),
                'description': "Manual Submission", 'image': image,
            }]
        except Exception as e:
            logger.error(f"Manual Fetch Error: {e}")
            return []

    def get_combined_news(self):
        all_entries = []
        futs = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for q in CONFIG['SEARCH_QUERIES']:
                futs.append(ex.submit(self.fetch_gnews, q))
                futs.append(ex.submit(self.fetch_duckduckgo, q, 6))
                futs.append(ex.submit(self.fetch_bing_rss, q))
            for fut in concurrent.futures.as_completed(futs):
                try:
                    all_entries.extend(fut.result() or [])
                except Exception as e:
                    logger.warning(f"Search worker failed: {e}")
        logger.info(f"Raw search hits: {len(all_entries)}")
        return all_entries

    # ───────────────────────── content grab ─────────────────────────

    def scrape_article_data(self, final_url, fallback_snippet, raw_image=None):
        if not final_url or final_url.lower().endswith('.pdf'):
            return fallback_snippet, None

        host = urlparse(final_url).netloc.lower()
        if host in self.failed_hosts:
            return fallback_snippet, raw_image if self._is_valid_image_url(raw_image) else None

        extracted_text = fallback_snippet
        extracted_image = raw_image if self._is_valid_image_url(raw_image) else None
        max_chars = CONFIG.get('MAX_TEXT_CHARS', 1800)

        try:
            downloaded = trafilatura.fetch_url(final_url)
            if downloaded:
                text = trafilatura.extract(downloaded, include_comments=False, include_tables=False, favor_precision=True)
                if text and len(text.strip()) > CONFIG.get('MIN_TEXT_LEN', 100):
                    extracted_text = re.sub(r'\s+', ' ', text).strip()[:max_chars]
                try:
                    meta = trafilatura.extract_metadata(downloaded)
                    if meta and getattr(meta, 'image', None) and self._is_valid_image_url(meta.image):
                        extracted_image = extracted_image or meta.image
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"trafilatura failed {final_url}: {e}")
            self.failed_hosts.add(host)

        need_soup = (not extracted_image or extracted_text == fallback_snippet or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100))
        if need_soup:
            try:
                resp = self.scraper.get(final_url, timeout=CONFIG['TIMEOUT'])
                soup = BeautifulSoup(resp.text, 'lxml')
                if extracted_text == fallback_snippet or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100):
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
                        tag.decompose()
                    paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
                    clean = ' '.join(paras[:12])
                    if len(clean) > CONFIG.get('MIN_TEXT_LEN', 100):
                        extracted_text = clean[:max_chars]
                if not extracted_image:
                    for prop in (('property', 'og:image'), ('name', 'twitter:image')):
                        tag = soup.find('meta', attrs={prop[0]: prop[1]})
                        if tag and tag.get('content') and self._is_valid_image_url(tag['content']):
                            extracted_image = tag['content'].strip()
                            break
            except Exception as e:
                logger.warning(f"Soup fallback failed {final_url}: {e}")
                self.failed_hosts.add(host)

        return extracted_text, extracted_image

    # ───────────────────────── AI analysis (2-stage, multi-provider) ─────────────────────────
    #
    # Stage A — SELECTION / PRIORITIZATION (headline + snippet only, cheap):
    #   Primary: Gemini Flash (much higher free daily request quota, so it can
    #   afford to run over every candidate that clears the free keyword filter).
    #   Fallback: Cloudflare, if Gemini isn't configured or fails.
    #   Decides: category, subcategory, iran_relevant, and a preliminary
    #   importance score used only to decide whether it's worth scraping the
    #   full article and spending a Stage B call on it.
    #
    # Stage B — WRITING / TRANSLATION (full article text, more expensive):
    #   Primary: Cloudflare (rotates across up to 2 accounts if a second one
    #   is configured). This is the model whose Persian output has already
    #   been tuned/debugged extensively for this project.
    #   Fallback: Gemini, if all Cloudflare accounts fail or are exhausted.
    #   Produces: title_fa, summary, body_fa, tag, and the FINAL authoritative
    #   importance/is_rumor/is_analysis (full-text judgment beats headline-only).
    #
    # Either provider can be entirely absent (no GEMINI_API_KEY, or no CF
    # credentials) and the pipeline still works on whichever one is present.

    _SELECTION_PROMPT = (
        "تو یک دستیار گزینش خبر برای یک کانال خبری فارسی هستی. فقط بر اساس تیتر و خلاصه‌ی کوتاه (نه متن کامل) باید "
        "تصمیم بگیری این خبر ارزش بررسی کامل داره یا نه.\n\n"
        "خبر باید دقیقاً در یکی از این ۶ دسته باشد:\n"
        "  ۱. اقتصاد و بازار (فدرال‌رزرو/ECB، نرخ بهره، تورم، رکود، بحران بانکی، بازار سهام)\n"
        "  ۲. انرژی و کالا (نفت، گاز، OPEC، طلا، دلار، Treasury)\n"
        "  ۳. ژئوپلیتیک و سیاست اقتصادی (تحریم، تعرفه، تجارت جهانی، تنگه هرمز، کانال سوئز، آمریکا-چین، درگیری‌های مؤثر بر اقتصاد جهانی)\n"
        "  ۴. تکنولوژی (AI، نیمه‌هادی، Big Tech، سایبرسکیوریتی، فضا، رباتیک، خودروی خودران، بیوتک)\n"
        "  ۵. کریپتو و بلاکچین (بیت‌کوین، اتریوم، ETF، رگولاسیون، هک صرافی/پروتکل)\n"
        "  ۶. گیم (کنسول، استودیوهای بزرگ، بازی‌های AAA، خرید/ادغام/تعطیلی استودیو، اخراج گسترده)\n"
        "اگر خبر در هیچ‌کدام از این ۶ دسته نمی‌گنجد (مثلاً سیاست داخلی صرف، ورزش، سرگرمی عمومی)، importance رو خیلی پایین (1) بده.\n\n"
        "importance (1 تا 10) فقط یک تخمین اولیه است — همینه که تعیین می‌کنه آیا ارزش بررسی کامل رو داره:\n"
        "9-10: احتمالاً یک خبر خیلی بزرگ (سقوط بازار، هک بزرگ، تصمیم غافلگیرکننده فدرال‌رزرو، تحریم بزرگ). "
        "4-8: احتمالاً قابل‌توجه ولی نه لزوماً خیلی بزرگ. 1-3: احتمالاً روتین/کم‌اهمیت/شایعه/تحلیل شخصی/بی‌ربط.\n\n"
        "فقط JSON زیر رو برگردون، بدون هیچ متن اضافه:\n"
        "{\n"
        ' "category": "دقیقاً یکی از ۶ رشته‌ی فارسی بالا",\n'
        ' "subcategory": "۲ تا ۴ کلمه فارسی",\n'
        ' "importance": عدد بین 1 تا 10,\n'
        ' "iran_relevant": true یا false\n'
        "}"
    )

    _WRITING_PROMPT = (
        "تو یک تحلیل‌گر ارشد بازارهای مالی جهانی، ژئوپلیتیک اقتصادی، تکنولوژی، کریپتو و صنعت گیم هستی که برای یک کانال خبری فارسی خلاصه می‌نویسی.\n\n"
        "این خبر از قبل در یکی از این ۶ دسته گزینش شده (در ورودی به‌عنوان CATEGORY_HINT داده میشه): "
        "اقتصاد و بازار / انرژی و کالا / ژئوپلیتیک و سیاست اقتصادی / تکنولوژی / کریپتو و بلاکچین / گیم. "
        "اگه با خوندن متن کامل فکر می‌کنی دسته‌ی درست‌تری هست، آزادی عوضش کنی، ولی فقط بین همین ۶ گزینه.\n\n"
        "🎯 **وظیفه اصلی: فیلتر سخت‌گیرانه اهمیت، این بار بر اساس متن کامل خبر.**\n"
        "فقط اخباری که واقعاً 'خیلی مهم' هستن باید importance بالا بگیرن. اخبار روتین، تکراری، شایعه یا تحلیل/نظر (نه رویداد واقعی) باید importance پایین بگیرن.\n"
        "❌ **سیاست داخلی معمولی، اظهارنظر سیاستمدار بدون اثر اقتصادی، یا دیپلماسی روتین را importance پایین (1 تا 2) بده.**\n\n"
        "🔴 قوانین نگارش:\n"
        "۱. خیلی روان، ساده و مستقیم بنویس. از کلمات پیچیده و ترجمه تحت‌اللفظی خودداری کن.\n"
        "۰. فقط و فقط فارسی بنویس. هیچ کاراکتر چینی، ژاپنی یا هیچ زبان دیگری (جز اسامی خاص انگلیسی مثل نام شرکت‌ها) نباید در خروجی باشد. این قانون رو با دقت کامل رعایت کن.\n"
        "۰۰. برای اسم افراد یا مکان‌هایی که تلفظ فارسی رایج و شناخته‌شده دارن (مثل تنگه هرمز)، فقط همون املای درست و رایج رو بنویس. اگه از تلفظ فارسی یه اسم خاص (مخصوصاً اسم افراد) مطمئن نیستی، به‌جای حدس‌زدن یه املای اشتباه، همون اسم رو به انگلیسی/لاتین بنویس. هرگز املای اختراعی یا نامطمئن برای اسم خاص ننویس.\n"
        "۰۰۰. هر عدد رو همیشه یک‌تکه و بدون فاصله بنویس (مثلاً 4400 یا 4,400 — هرگز 4 400 با فاصله‌ی وسط). فاصله‌ی داخل عدد در متن فارسی باعث به‌هم‌ریختن ترتیب نمایش عدد میشه.\n"
        "۲. از عبارات کلیشه‌ای مثل 'به نظر می‌رسد'، 'لازم به ذکر است'، 'شایان ذکر است' استفاده نکن.\n"
        "۳. باید دو نسخه از خبر بنویسی، هر دو بر اساس متن کامل خبر (TEXT)، نه فقط تیتر:\n"
        "   الف) summary: نسخه‌ی خیلی کوتاه برای تلگرام (فضا محدوده)، دقیقاً ۲ تا ۳ خط، هر خط حداکثر ۲۵ کلمه:\n"
        "      - خط ۱: چه اتفاقی افتاد (با مهم‌ترین رقم یا جزئیات)\n"
        "      - خط ۲: چرا مهمه / چه تاثیری داره\n"
        "      - خط ۳ (اختیاری): یک جزئیات مهم دیگر\n"
        "   ب) body_fa: نسخه‌ی کامل‌تر برای سایت (اینجا محدودیت فضا نداریم)، یک متن روان و پیوسته فارسی در ۴ تا ۷ جمله که کل خبر رو با جزئیات، زمینه، اعداد، نقل‌قول‌های مهم (در صورت وجود) و پیامدهای احتمالی توضیح بده. این باید یک ترجمه‌ی روان و بازنویسی‌شده باشه، نه ترجمه‌ی کلمه‌به‌کلمه.\n"
        "   summary و body_fa هرگز نباید فقط تکرار تیتر باشن.\n"
        "۴. تیتر (title_fa) حداکثر ۱۰ کلمه، جذاب و بدون کلمات اضافه.\n"
        "۵. هیچ‌وقت title_fa، هیچ‌کدوم از خط‌های summary، یا body_fa رو با یک کلمه یا حرف انگلیسی/لاتین شروع نکن. همیشه اولین کلمه‌ی جمله باید فارسی باشه، حتی اگه لازم باشه ترتیب جمله رو کمی عوض کنی.\n\n"
        "قواعد امتیازبندی importance (1 تا 10):\n"
        "- 9-10: تصمیم غافلگیرکننده فدرال‌رزرو، سقوط/رشد بزرگ بازار سهام (بالای ۳٪ در یک روز)، هک یا فروپاشی بزرگ کریپتو، تایید/رد ETF بیت‌کوین، جهش بیت‌کوین به رکورد جدید، اعلام مدل هوش‌مصنوعی انقلابی، تحریم/تعرفه بزرگ با اثر جهانی، اختلال بزرگ در تنگه هرمز یا کانال سوئز، خرید/تعطیلی بزرگ در صنعت گیم.\n"
        "- 7-8: نتایج مالی غافلگیرکننده شرکت‌های بزرگ، تغییر نرخ بهره، رگولاسیون مهم کریپتو، ادغام/تملک بزرگ، حکم مهم آنتی‌تراست، تغییر مهم سیاست تجاری بین دو کشور بزرگ.\n"
        "- 4-6: گزارش‌های اقتصادی معمول بدون شگفتی خاص، اخبار محصول متوسط، رویداد ژئوپلیتیک با اثر اقتصادی محدود.\n"
        "- 1-3: اخبار روتین، تکراری، شایعه، تحلیل/نظر بدون رویداد واقعی، محصول/آپدیت جزئی، یا سیاسی صرف بدون تاثیر اقتصادی.\n\n"
        "🔒 **قانون اصلی حذف:** اگر خبر اثر قابل‌توجهی بر جهان، اقتصاد، بازار، فناوری، کریپتو یا صنعت گیم نداره، حتی اگه از منبع معتبر باشه، importance رو پایین (زیر ۴) بده.\n\n"
        "فرمت خروجی باید دقیقاً JSON زیر باشد و هیچ متن اضافه‌ای قبل یا بعدش نباشه:\n"
        "{\n"
        ' "title_fa": "تیتر کوتاه و روان",\n'
        ' "summary": ["خط ۱: چه اتفاقی افتاد", "خط ۲: چرا مهمه", "خط ۳: جزئیات مهم دیگر (اختیاری)"],\n'
        ' "body_fa": "یک پاراگراف ۴ تا ۷ جمله‌ای، روان و کامل، که کل خبر رو با جزئیات توضیح میده.",\n'
        ' "category": "یکی از ۶ دسته، دقیقاً همون رشته فارسی",\n'
        ' "subcategory": "۲ تا ۴ کلمه فارسی",\n'
        ' "tag": "کلمه کلیدی کوتاه",\n'
        ' "importance": عدد بین 1 تا 10,\n'
        ' "is_rumor": true یا false,\n'
        ' "is_analysis": true یا false,\n'
        ' "iran_relevant": true یا false\n'
        "}"
    )

    def _parse_json_from_text(self, raw):
        if isinstance(raw, dict):
            return raw
        clean = re.sub(r'<think>.*?</think>', '', str(raw), flags=re.DOTALL)
        clean = re.sub(r'```json\s*|```', '', clean).strip()
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m:
            clean = m.group(0)
        return json.loads(clean)

    def _call_cloudflare(self, account, system_prompt, user_content, max_tokens=2000):
        """One attempt against a single Cloudflare account. Returns parsed
        dict or None. Caller handles retries/fallback across accounts."""
        account_id, api_token = account
        cf_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
        try:
            resp = self.scraper.post(
                cf_url,
                headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
                json={
                    "model": self.cf_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.25,
                    "max_tokens": max_tokens,
                },
                timeout=CONFIG.get('AI_TIMEOUT', 45),
            )
            if resp.status_code != 200:
                logger.warning(f"CF AI HTTP {resp.status_code} (account {account_id[:6]}...): {resp.text[:200]}")
                return None
            body = resp.json()
            if not body.get('success', True) and body.get('errors'):
                logger.warning(f"CF AI error payload: {body['errors']}")
                return None
            try:
                raw = body['choices'][0]['message']['content']
            except (KeyError, IndexError, TypeError):
                raw = body.get('result', {}).get('response', '')
            if not raw:
                logger.warning(f"CF AI empty response body: {str(body)[:300]}")
                return None
            return self._parse_json_from_text(raw)
        except Exception as e:
            logger.warning(f"CF AI call failed (account {account_id[:6]}...): {e}")
            return None

    def _call_cloudflare_with_failover(self, system_prompt, user_content, url_for_routing, max_tokens=2000):
        if not self.cf_accounts:
            return None
        start = hash(url_for_routing or '') % len(self.cf_accounts)
        order = self.cf_accounts[start:] + self.cf_accounts[:start]
        for account in order:
            for attempt in range(CONFIG['AI_RETRIES']):
                data = self._call_cloudflare(account, system_prompt, user_content, max_tokens=max_tokens)
                if data:
                    return data
                time.sleep(1)
        return None

    def _call_gemini(self, system_prompt, user_content):
        if not self.gemini_api_key:
            return None
        gemini_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent"
        )
        for attempt in range(CONFIG['AI_RETRIES']):
            try:
                resp = self.scraper.post(
                    gemini_url,
                    headers={"x-goog-api-key": self.gemini_api_key, "Content-Type": "application/json"},
                    json={
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
                        "generationConfig": {
                            "temperature": 0.25,
                            "responseMimeType": "application/json",
                        },
                    },
                    timeout=CONFIG.get('AI_TIMEOUT', 45),
                )
                if resp.status_code != 200:
                    logger.warning(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")
                    time.sleep(1)
                    continue
                body = resp.json()
                raw = body['candidates'][0]['content']['parts'][0]['text']
                if not raw:
                    time.sleep(1)
                    continue
                return self._parse_json_from_text(raw)
            except Exception as e:
                logger.warning(f"Gemini call failed: {e}")
                time.sleep(1)
        return None

    def select_with_ai(self, headline, snippet, source_name, url_for_routing):
        """Stage A: cheap, headline-only selection/prioritization. Gemini
        primary (higher free quota, runs over every keyword-filtered
        candidate), Cloudflare fallback."""
        user_content = f"SOURCE: {source_name}\nHEADLINE: {headline}\nSNIPPET: {snippet[:500]}"
        data = self._call_gemini(self._SELECTION_PROMPT, user_content)
        if data and 'category' in data:
            data['_selected_by'] = 'gemini'
            return data
        data = self._call_cloudflare_with_failover(self._SELECTION_PROMPT, user_content, url_for_routing, max_tokens=300)
        if data and 'category' in data:
            data['_selected_by'] = 'cloudflare'
            return data
        return None

    def write_with_ai(self, headline, full_text, source_name, category_hint, url_for_routing):
        """Stage B: full-article writing/translation. Cloudflare primary
        (rotates accounts, already-tuned Persian output), Gemini fallback."""
        user_content = (
            f"SOURCE: {source_name}\nCATEGORY_HINT: {category_hint}\n"
            f"HEADLINE: {headline}\nTEXT: {full_text}"
        )
        data = self._call_cloudflare_with_failover(self._WRITING_PROMPT, user_content, url_for_routing, max_tokens=8000)
        if data and 'title_fa' in data and 'summary' in data:
            return data
        # Fallback: trim text a bit for Gemini's single-shot call.
        user_content_short = (
            f"SOURCE: {source_name}\nCATEGORY_HINT: {category_hint}\n"
            f"HEADLINE: {headline}\nTEXT: {full_text[:2000]}"
        )
        data = self._call_gemini(self._WRITING_PROMPT, user_content_short)
        if data and 'title_fa' in data and 'summary' in data:
            return data
        return None

    # ───────────────────────── process item ─────────────────────────

    def _strip_foreign_scripts(self, text):
        """Safety net: strip CJK characters that GLM-family models occasionally
        leak into non-Chinese output, regardless of prompt instructions."""
        if not text:
            return text
        # CJK Unified Ideographs + common extensions/punctuation ranges
        cleaned = re.sub(
            r'[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]+',
            '', text
        )
        return re.sub(r'\s{2,}', ' ', cleaned).strip()

    # Common AI mistranscriptions of Persian proper nouns — extend as more show up.
    _KNOWN_TYPO_FIXES = {
        'هورموز': 'هرمز',
        'تنگه هورموز': 'تنگه هرمز',
    }

    def _fix_common_typos(self, text):
        if not text:
            return text
        for wrong, right in self._KNOWN_TYPO_FIXES.items():
            text = text.replace(wrong, right)
        # Ensure a space at Persian/Latin (letters or digits) boundaries when
        # the model glues them together without one, e.g. "389میلیون" or
        # "Wintermuteشرکت" — this is the main cause of words reading as jumbled.
        text = re.sub(r'([\u0600-\u06FF])([A-Za-z0-9])', r'\1 \2', text)
        text = re.sub(r'([A-Za-z0-9])([\u0600-\u06FF])', r'\1 \2', text)
        return text

    def process_item(self, entry):
        raw_title = (entry.get('title') or '').rsplit(' - ', 1)[0].strip()
        if not raw_title:
            return None
        publisher = entry.get('publisher', {}).get('title', 'Unknown')
        final_url = entry.get('url')
        if not final_url or "news.google.com" in final_url:
            return None

        clean_final_url = self._clean_url(final_url)

        if not os.environ.get('MANUAL_URL'):
            if clean_final_url in self.seen_urls:
                return None
            th = self._title_hash(raw_title)
            if th in self.recent_title_hashes or self._normalize_text(raw_title) in self.seen_titles:
                return None
            if self._is_duplicate_fuzzy(raw_title, self.existing_news):
                return None

        hint = self._cheap_urgency_hint(raw_title, publisher)
        snippet = entry.get('description', raw_title)

        # Free triage gate: cut obvious junk before spending any AI call at all.
        if hint < CONFIG.get('MIN_AI_URGENCY_HINT', 4) and len(snippet) < 200:
            return None

        # ── Stage A: selection/prioritization (headline + snippet only) ──
        selection = self.select_with_ai(raw_title, snippet, publisher, clean_final_url)
        if not selection:
            logger.warning(f"Selection stage failed (no AI provider available?): {raw_title[:60]}")
            return None
        try:
            prelim_importance = int(selection.get('importance', 3))
        except Exception:
            prelim_importance = 3
        if prelim_importance < CONFIG.get('MIN_AI_URGENCY_HINT', 4):
            return None

        category_hint = selection.get('category', 'اقتصاد و بازار')
        logger.info(
            f"Processing (hint={hint}, selected_by={selection.get('_selected_by', '?')}, "
            f"prelim={prelim_importance}, cat={category_hint}): {publisher} | {raw_title[:50]}..."
        )

        # Only scrape the full article for candidates that survived Stage A.
        text, photo_url = self.scrape_article_data(final_url, snippet, raw_image=entry.get('image'))

        # ── Stage B: writing/translation (full article text) ──
        ai = self.write_with_ai(raw_title, text, publisher, category_hint, clean_final_url)
        if not ai:
            return None
        # Fill in anything Stage B omitted with Stage A's judgment.
        ai.setdefault('subcategory', selection.get('subcategory', ''))
        ai.setdefault('iran_relevant', selection.get('iran_relevant', False))

        try:
            urgency_val = int(ai.get('importance', ai.get('urgency', 3)))
        except Exception:
            urgency_val = 3

        # Hard filter: drop anything the AI itself scored as unimportant, so
        # low-value items never even get stored.
        if urgency_val < 4:
            return None

        # Rumors get dropped outright regardless of how "important" they were
        # scored — unverified claims shouldn't reach the channel just because
        # the underlying story is a big one.
        if ai.get('is_rumor') is True:
            return None

        try:
            ts = parser.parse(entry.get('published date')).timestamp()
        except Exception:
            ts = time.time()

        category = ai.get('category', 'اقتصاد و بازار')
        if category not in CATEGORY_FALLBACK_IMAGE:
            category = 'اقتصاد و بازار'

        photo_url = self._pick_image(photo_url, entry.get('image'), category=category)
        news_id = self._generate_news_id(clean_final_url)

        def clean_fa(t):
            t = self._strip_foreign_scripts(t)
            t = self._fix_common_typos(t)
            if t:
                t = '\u200f' + t  # RLM: force RTL base direction even if the first word is Latin
            return t

        return {
            "id": news_id,
            "title_fa": clean_fa(ai.get('title_fa', raw_title)),
            "title_en": raw_title,
            "summary": [clean_fa(s) for s in ai.get('summary', [snippet])[:3]],
            "body_fa": clean_fa(ai.get('body_fa', '')),
            "category": category,
            "subcategory": ai.get('subcategory', ''),
            "tag": ai.get('tag', 'General'),
            "urgency": urgency_val,
            "iran_relevant": bool(ai.get('iran_relevant', False)),
            "source": publisher,
            "url": final_url,
            "clean_url": clean_final_url,
            "image": photo_url,
            "timestamp": ts,
            "sent_to_telegram": False,
        }

    # ───────────────────────── storage ─────────────────────────

    def save_news(self, new_items):
        combined = new_items + self.existing_news
        combined.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        combined = combined[:CONFIG.get('HISTORY_SIZE', 300)]
        self._atomic_json_dump(CONFIG['FILES']['NEWS'], combined)
        return combined

    # ───────────────────────── telegram digest ─────────────────────────

    def send_rich_digest_to_telegram(self, items, market=None):
        """Send digest via Telegram's Bot API 10.1 Rich Messages (sendRichMessage).
        Falls back to a plain-text sendMessage digest if the rich call fails
        (older Bot API server, or any transient issue)."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not items:
            logger.warning("TG credentials or items missing. Skipping dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        now_ir = self._get_tehran_time()
        time_str = now_ir.strftime("%H:%M")
        date_str = now_ir.strftime("%Y/%m/%d")
        base_site = os.environ.get('SITE_URL', '')

        items_sorted = sorted(items, key=lambda x: x.get('urgency', 3), reverse=True)

        market_html = ""
        if market:
            market_html = (
                "<table bordered striped>\n"
                "<tr><th>🪙 سکه تمام</th><th>🥇 انس طلا</th><th>₮ تتر</th></tr>\n"
                f"<tr>"
                f"<td align='center'>{esc(market.get('coin_irt'))}</td>"
                f"<td align='center'>{esc(market.get('gold_oz_usd'))}</td>"
                f"<td align='center'>{esc(market.get('usdt_irt'))}</td>"
                f"</tr>\n</table>\n"
                "<hr/>\n"
                "<table bordered striped>\n"
                "<tr><th>₿ بیت‌کوین</th><th>Ξ اتریوم</th><th>✕ ریپل</th></tr>\n"
                f"<tr>"
                f"<td align='center'>{esc(market.get('btc'))}</td>"
                f"<td align='center'>{esc(market.get('eth'))}</td>"
                f"<td align='center'>{esc(market.get('xrp'))}</td>"
                f"</tr>\n</table>\n"
            )

        cat_emoji = CATEGORY_EMOJI

        details_parts = []
        for i, it in enumerate(items_sorted, 1):
            title = esc(it.get('title_fa') or it.get('title_en'))
            source = esc(it.get('source', 'Unknown'))
            cat = it.get('category', 'اقتصاد و بازار')
            src_url = it.get('url') or '#'
            body_text = it.get('body_fa') or " ".join(it.get('summary', []))
            open_attr = " open" if i == 1 else ""
            details_parts.append(
                f"<details{open_attr}>\n"
                f"<summary><b>{cat_emoji.get(cat, '🔹')} {title}</b></summary>\n"
                f"<p>{esc(body_text)}</p>\n"
                f"<p>🔗 <a href=\"{esc(src_url)}\">منبع: {source}</a></p>\n"
                f"</details>\n<hr/>\n"
            )
        details_html = "".join(details_parts)

        footer_html = ""
        if base_site:
            footer_html = f"<footer><p>📊 <a href=\"{esc(base_site)}\">آرشیو کامل رصد جهانی</a></p></footer>\n"

        full_html = (
            f"<h1>🌐 خلاصه اخبار مهم جهان</h1>\n"
            f"<p>⏱ {esc(time_str)} — {esc(date_str)} (تهران)</p>\n"
            f"{market_html}"
            f"<hr/>\n"
            f"<h2>📋 جزئیات</h2>\n"
            f"{details_html}"
            f"{footer_html}"
        )
        if len(full_html) > 30000:
            full_html = full_html[:30000]

        api_url = f"https://api.telegram.org/bot{token}/sendRichMessage"
        payload = {
            "chat_id": chat_id,
            "rich_message": {"html": full_html, "is_rtl": True},
        }

        try:
            resp = self.scraper.post(api_url, json=payload, timeout=30)
            if resp.status_code == 200:
                logger.info(">>> Rich Message digest sent to Telegram.")
                return True
            logger.warning(f"sendRichMessage failed ({resp.status_code}): {resp.text[:300]} — falling back to plain text.")
        except Exception as e:
            logger.warning(f"sendRichMessage exception: {e} — falling back to plain text.")

        return self.send_digest_to_telegram(items, market=market)

    def send_digest_to_telegram(self, items, market=None):
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not items:
            logger.warning("TG credentials or items missing. Skipping dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        now_ir = self._get_tehran_time()
        time_str = now_ir.strftime("%H:%M")
        date_str = now_ir.strftime("%Y/%m/%d")
        base_site = os.environ.get('SITE_URL', '')

        by_cat = {}
        for it in items:
            by_cat.setdefault(it.get('category', 'اقتصاد و بازار'), []).append(it)

        cat_emoji = CATEGORY_EMOJI

        lines = [f"🌐 <b>خلاصه اخبار مهم جهان</b>\n⏱ {time_str} — {date_str} (تهران)\n"]
        for cat, cat_items in by_cat.items():
            lines.append(f"\n{cat_emoji.get(cat, '🔹')} <b>{esc(cat)}</b>")
            for it in cat_items:
                lines.append(f"\n<b>{esc(it['title_fa'])}</b>")
                body_text = it.get('body_fa') or " ".join(it.get('summary', []))
                lines.append(esc(body_text))
                lines.append(f"🔗 <a href=\"{esc(it['url'])}\">منبع: {esc(it['source'])}</a>")

        if market:
            lines.append(
                f"\n\n💰 <b>قیمت‌های لحظه‌ای</b>\n"
                f"🪙 سکه: {esc(market.get('coin_irt'))}  |  🥇 طلا: {esc(market.get('gold_oz_usd'))}\n"
                f"₮ تتر: {esc(market.get('usdt_irt'))}  |  ₿ بیت‌کوین: {esc(market.get('btc'))}\n"
                f"Ξ اتریوم: {esc(market.get('eth'))}  |  ✕ ریپل: {esc(market.get('xrp'))}"
            )

        if base_site:
            lines.append(f"\n\n📌 <a href=\"{esc(base_site)}\">مشاهده آرشیو کامل</a>")

        full_text = "\n".join(lines)

        # Telegram messages are capped at 4096 chars; split into chunks if needed.
        chunks = []
        current = ""
        for line in full_text.split("\n"):
            if len(current) + len(line) + 1 > 3900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)

        api = f"https://api.telegram.org/bot{token}/sendMessage"
        ok = True
        for chunk in chunks:
            try:
                resp = self.scraper.post(api, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, timeout=30)
                if resp.status_code != 200:
                    logger.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
                    ok = False
            except Exception as e:
                logger.error(f"Telegram send exception: {e}")
                ok = False
        return ok

    # ───────────────────────── main run ─────────────────────────

    def run(self):
        logger.info(">>> GlobalRadar started...")
        logger.info(f">>> Using AI model: {self.cf_model or '(not set!)'}")

        market_snapshot = self.fetch_market_rates()
        self._atomic_json_dump(CONFIG['FILES']['MARKET'], market_snapshot)

        raw_channel = (CONFIG['TELEGRAM']['CHANNEL_ID'] or '').strip()
        telegram_link = f"https://t.me/{raw_channel.lstrip('@')}" if raw_channel.startswith('@') else ''
        self._atomic_json_dump(CONFIG['FILES']['SITE_CONFIG'], {
            "telegram_channel": raw_channel,
            "telegram_link": telegram_link,
            "site_url": os.environ.get('SITE_URL', ''),
        })

        manual_url = os.environ.get('MANUAL_URL')
        if manual_url and manual_url.strip():
            logger.info(f"!!! MANUAL MODE: {manual_url} !!!")
            candidates = self.fetch_manual_url(manual_url)
        else:
            results = self.get_combined_news()
            candidates = []
            seen_batch_titles = set()
            cutoff_date = datetime.now(timezone.utc) - timedelta(hours=CONFIG['MAX_NEWS_AGE_HOURS'])

            for item in results:
                try:
                    p_date = item.get('published date')
                    if p_date:
                        dt = parser.parse(p_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff_date:
                            continue
                except Exception:
                    pass

                raw_url = item.get('url', '')
                clean_u = self._clean_url(raw_url)
                if clean_u in self.seen_urls or not raw_url or "news.google.com" in raw_url:
                    continue

                t = (item.get('title') or '').rsplit(' - ', 1)[0]
                norm_t = self._normalize_text(t)
                th = self._title_hash(t)
                if norm_t in self.seen_titles or norm_t in seen_batch_titles or th in self.recent_title_hashes:
                    continue
                if self._is_duplicate_fuzzy(t, self.existing_news):
                    continue

                seen_batch_titles.add(norm_t)
                candidates.append(item)

            candidates.sort(
                key=lambda x: (
                    self._cheap_urgency_hint(x.get('title', ''), x.get('publisher', {}).get('title', '')),
                    self._domain_score(x.get('url'), x.get('publisher', {}).get('title', '')),
                ),
                reverse=True
            )
            candidates = candidates[:CONFIG.get('MAX_CANDIDATES', 30)]
            logger.info(f"Total fetched: {len(results)} | Candidates: {len(candidates)}")

        new_items = []
        if candidates:
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as exc:
                futures = {exc.submit(self.process_item, i): i for i in candidates}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        res = fut.result()
                        if res:
                            new_items.append(res)
                            self.seen_urls.add(res['clean_url'])
                            self.recent_title_hashes.add(self._title_hash(res.get('title_en', '')))
                    except Exception as e:
                        logger.error(f"process_item worker error: {e}")

        if new_items:
            self.existing_news = self.save_news(new_items)
            logger.info(f">>> Saved {len(new_items)} new qualifying items.")
        else:
            logger.info(">>> No valid new items found this run.")

        # ── Dispatch digest whenever there are qualifying items to send.
        # No time-window gate needed: the workflow itself only runs 4x/day,
        # so every run that finds something already lands us at ~4 msgs/day,
        # without the fragility of matching GitHub's (often late) cron time
        # against a fixed Tehran-hour window.
        pending = [
            it for it in self.existing_news
            if it.get('urgency', 0) >= CONFIG['MIN_TELEGRAM_URGENCY'] and not it.get('sent_to_telegram')
        ]
        pending.sort(key=lambda x: x.get('urgency', 0), reverse=True)
        pending = pending[:CONFIG['MAX_DIGEST_ITEMS']]

        if pending:
            logger.info(f"Dispatching digest with {len(pending)} items.")
            # NOTE: sendRichMessage is disabled — Telegram's own Rich Message
            # renderer has a confirmed bug that reverses multi-digit numbers
            # inside RTL text (reproduced with both Western and Persian digits),
            # unrelated to our data. The plain formatted message below has
            # always rendered numbers correctly. Re-enable
            # send_rich_digest_to_telegram(...) if/when Telegram fixes this.
            # Rich Message kept per request. Note: Telegram's own Rich Message
            # renderer has a confirmed bug that can still occasionally reverse
            # multi-digit numbers in RTL text — that part is out of our control.
            sent_ok = self.send_rich_digest_to_telegram(pending, market=market_snapshot)
            if sent_ok:
                sent_ids = {it['id'] for it in pending}
                for it in self.existing_news:
                    if it['id'] in sent_ids:
                        it['sent_to_telegram'] = True
                self._atomic_json_dump(CONFIG['FILES']['NEWS'], self.existing_news)
        else:
            logger.info("No pending items above the Telegram urgency bar this run.")

        logger.info(f">>> Done. New={len(new_items)} | Failed hosts this run={len(self.failed_hosts)}")


if __name__ == "__main__":
    GlobalRadar().run()
