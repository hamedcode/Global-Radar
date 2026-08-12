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
    # Search themes: global macro economy, crypto, big tech. NO general politics.
    'SEARCH_QUERIES': [
        '(Federal Reserve OR "interest rate" OR inflation OR recession) global economy',
        '(stock market OR "Wall Street" OR S&P500 OR Nasdaq) (crash OR rally OR record OR selloff)',
        '(oil price OR OPEC OR gold price) major move',
        'Bitcoin OR Ethereum (ETF OR regulation OR hack OR crash OR rally OR all-time high)',
        'crypto (SEC OR regulation OR exchange OR stablecoin) major',
        '(OpenAI OR "Google DeepMind" OR Anthropic OR Nvidia OR Microsoft OR Meta OR Apple) (AI OR launch OR breakthrough)',
        '(Nvidia OR Apple OR Microsoft OR Amazon OR Google OR Tesla) (earnings OR lawsuit OR antitrust OR layoffs)',
        'major tech company earnings report',
    ],
    'TARGET_SOURCES': [
        'bloomberg.com', 'reuters.com', 'cnbc.com', 'wsj.com', 'ft.com',
        'apnews.com', 'coindesk.com', 'theblock.co', 'techcrunch.com',
        'theverge.com', 'axios.com', 'businessinsider.com'
    ],
    'SOURCE_PRIORITY': {
        'bloomberg.com': 10, 'reuters.com': 10, 'wsj.com': 9, 'ft.com': 9,
        'cnbc.com': 8, 'apnews.com': 8, 'coindesk.com': 8, 'theblock.co': 8,
        'techcrunch.com': 6, 'theverge.com': 6, 'axios.com': 6,
        'businessinsider.com': 5,
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
    'MAX_CANDIDATES': 20,
    'MAX_TEXT_CHARS': 2500,
    'MIN_TEXT_LEN': 100,
    'MIN_AI_URGENCY_HINT': 4,
    'POLLINATIONS_KEY': os.environ.get('POLLINATIONS_API_KEY'),
    'CF_ACCOUNT_ID': os.environ.get('CF_ACCOUNT_ID'),
    'CF_API_TOKEN': os.environ.get('CF_API_TOKEN'),
    'CF_MODEL': os.environ.get('CF_MODEL', '@cf/openai/gpt-oss-120b'),
    'AI_RETRIES': 3,
    # Telegram now mirrors the site: same 4+ bar used to save a story at all.
    'MIN_TELEGRAM_URGENCY': 4,
    'MAX_DIGEST_ITEMS': 40,
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
    'اقتصاد': 'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?auto=format&fit=crop&w=1200&q=80',
    'کریپتو': 'https://images.unsplash.com/photo-1621761191319-c6fb62004040?auto=format&fit=crop&w=1200&q=80',
    'تکنولوژی': 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1200&q=80',
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
        t = (title or '').lower()
        score = 3
        high = [
            'crash', 'plunge', 'soar', 'record high', 'all-time high', 'surge', 'ban',
            'hack', 'collapse', 'bankrupt', 'rate cut', 'rate hike', 'fed', 'sec sues',
            'approve', 'antitrust', 'breakthrough',
        ]
        mid = ['earnings', 'inflation', 'ipo', 'merger', 'acquisition', 'launch', 'lawsuit']
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
        return CATEGORY_FALLBACK_IMAGE.get(category, CATEGORY_FALLBACK_IMAGE['اقتصاد'])

    def _pick_image(self, *candidates, category='اقتصاد'):
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

    # ───────────────────────── AI analysis ─────────────────────────

    def analyze_with_ai(self, headline, full_text, source_name):
        if not self.cf_account_id or not self.cf_api_token:
            logger.error("AI SKIPPED: CF_ACCOUNT_ID or CF_API_TOKEN is empty/not set in the environment.")
            return None

        system_prompt = (
            "تو یک تحلیل‌گر ارشد بازارهای مالی جهانی، کریپتو و تکنولوژی هستی که برای یک کانال خبری فارسی خلاصه می‌نویسی.\n\n"
            "🎯 **وظیفه اصلی: فیلتر سخت‌گیرانه اهمیت.**\n"
            "فقط اخباری که واقعاً 'خیلی مهم' هستن باید امتیاز urgency بالا بگیرن. اخبار روتین، تکراری یا کم‌اهمیت باید امتیاز پایین بگیرن.\n"
            "این خبر باید در یکی از این سه دسته باشد: اقتصاد کلان و بازارهای مالی جهانی (نرخ بهره، تورم، سهام، نفت، طلا)، کریپتو (بیت‌کوین، اتریوم، رگولاسیون، هک، ETF)، یا تکنولوژی (شرکت‌های بزرگ تک، هوش مصنوعی، محصولات جدید، دعاوی قضایی بزرگ).\n"
            "❌ **اخبار سیاسی صرف (انتخابات، درگیری‌های نظامی، دیپلماسی) که تاثیر مستقیم و فوری بر بازار/اقتصاد/تکنولوژی نداره را کنار بگذار و urgency پایین (1 تا 2) بده.**\n\n"
            "🔴 قوانین نگارش:\n"
            "۱. خیلی روان، ساده و مستقیم بنویس. از کلمات پیچیده و ترجمه تحت‌اللفظی خودداری کن.\n"
            "۰. فقط و فقط فارسی بنویس. هیچ کاراکتر چینی، ژاپنی یا هیچ زبان دیگری (جز اسامی خاص انگلیسی مثل نام شرکت‌ها) نباید در خروجی باشد. این قانون رو با دقت کامل رعایت کن.\n"
            "۰۰. برای اسم افراد یا مکان‌هایی که تلفظ فارسی رایج و شناخته‌شده دارن (مثل تنگه هرمز)، فقط همون املای درست و رایج رو بنویس. اگه از تلفظ فارسی یه اسم خاص (مخصوصاً اسم افراد) مطمئن نیستی، به‌جای حدس‌زدن یه املای اشتباه، همون اسم رو به انگلیسی/لاتین بنویس. هرگز املای اختراعی یا نامطمئن برای اسم خاص ننویس.\n"
            "۲. از عبارات کلیشه‌ای مثل 'به نظر می‌رسد'، 'لازم به ذکر است'، 'شایان ذکر است' استفاده نکن.\n"
            "۳. باید دو نسخه از خبر بنویسی، هر دو بر اساس متن کامل خبر (TEXT)، نه فقط تیتر:\n"
            "   الف) summary: نسخه‌ی خیلی کوتاه برای تلگرام (فضا محدوده)، دقیقاً ۲ تا ۳ خط، هر خط حداکثر ۲۵ کلمه:\n"
            "      - خط ۱: چه اتفاقی افتاد (با مهم‌ترین رقم یا جزئیات)\n"
            "      - خط ۲: چرا مهمه / چه تاثیری داره\n"
            "      - خط ۳ (اختیاری): یک جزئیات مهم دیگر\n"
            "   ب) body_fa: نسخه‌ی کامل‌تر برای سایت (اینجا محدودیت فضا نداریم)، یک متن روان و پیوسته فارسی در ۴ تا ۷ جمله که کل خبر رو با جزئیات، زمینه، اعداد، نقل‌قول‌های مهم (در صورت وجود) و پیامدهای احتمالی توضیح بده. این باید یک ترجمه‌ی روان و بازنویسی‌شده باشه، نه ترجمه‌ی کلمه‌به‌کلمه.\n"
            "   summary و body_fa هرگز نباید فقط تکرار تیتر باشن.\n"
            "۴. تیتر (title_fa) حداکثر ۱۰ کلمه، جذاب و بدون کلمات اضافه.\n\n"
            "قواعد امتیازبندی urgency (1 تا 10):\n"
            "- 9-10: تصمیم غافلگیرکننده فدرال‌رزرو، سقوط/رشد بزرگ بازار سهام (بالای ۳٪ در یک روز)، هک یا فروپاشی بزرگ کریپتو، تایید/رد ETF بیت‌کوین، جهش بیت‌کوین به رکورد جدید، اعلام مدل هوش‌مصنوعی انقلابی از OpenAI/Google/Anthropic.\n"
            "- 7-8: نتایج مالی غافلگیرکننده شرکت‌های بزرگ تک، تغییر نرخ بهره، رگولاسیون مهم کریپتو، ادغام/تملک بزرگ، حکم مهم آنتی‌تراست.\n"
            "- 4-6: گزارش‌های اقتصادی معمول (تورم، اشتغال) بدون شگفتی خاص، اخبار محصول متوسط.\n"
            "- 1-3: اخبار روتین، تکراری، حدس و گمان، یا سیاسی صرف بدون تاثیر اقتصادی.\n\n"
            "فرمت خروجی باید دقیقاً JSON زیر باشد و هیچ متن اضافه‌ای قبل یا بعدش نباشه:\n"
            "{\n"
            ' "title_fa": "تیتر کوتاه و روان",\n'
            ' "summary": ["خط ۱: چه اتفاقی افتاد", "خط ۲: چرا مهمه", "خط ۳: جزئیات مهم دیگر (اختیاری)"],\n'
            ' "body_fa": "یک پاراگراف ۴ تا ۷ جمله‌ای، روان و کامل، که کل خبر رو با جزئیات توضیح میده.",\n'
            ' "category": "اقتصاد یا کریپتو یا تکنولوژی",\n'
            ' "tag": "کلمه کلیدی کوتاه",\n'
            ' "urgency": عدد بین 1 تا 10\n'
            "}"
        )

        cf_url = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/ai/v1/chat/completions"

        current_text = full_text
        for attempt in range(CONFIG['AI_RETRIES']):
            try:
                if attempt > 0:
                    current_text = headline + " " + full_text[:800]
                resp = self.scraper.post(
                    cf_url,
                    headers={"Authorization": f"Bearer {self.cf_api_token}", "Content-Type": "application/json"},
                    json={
                        "model": self.cf_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"SOURCE: {source_name}\nHEADLINE: {headline}\nTEXT: {current_text}"},
                        ],
                        "temperature": 0.25,
                        "max_tokens": 8000,
                    },
                    timeout=CONFIG.get('AI_TIMEOUT', 45),
                )
                if resp.status_code == 200:
                    body = resp.json()
                    if not body.get('success', True) and body.get('errors'):
                        logger.error(f"CF AI error payload: {body['errors']}")
                        time.sleep(1)
                        continue
                    # OpenAI-compatible shape first, fall back to raw /ai/run shape
                    raw = ''
                    try:
                        raw = body['choices'][0]['message']['content']
                    except (KeyError, IndexError, TypeError):
                        raw = body.get('result', {}).get('response', '')
                    if not raw:
                        logger.error(f"AI empty response body: {str(body)[:400]}")
                        time.sleep(1)
                        continue
                    if isinstance(raw, dict):
                        data = raw
                    else:
                        clean = re.sub(r'<think>.*?</think>', '', str(raw), flags=re.DOTALL)
                        clean = re.sub(r'```json\s*|```', '', clean).strip()
                        # Some models wrap output with stray text; grab the outermost {...} block.
                        m = re.search(r'\{.*\}', clean, re.DOTALL)
                        if m:
                            clean = m.group(0)
                        data = json.loads(clean)
                    if isinstance(data, dict) and 'title_fa' in data and 'summary' in data:
                        return data
                    logger.error(f"AI response missing expected keys: {str(data)[:300]}")
                else:
                    logger.error(f"CF AI HTTP {resp.status_code}: {resp.text[:300]}")
                time.sleep(1)
            except Exception as e:
                logger.error(f"AI Attempt {attempt+1} failed: {e}")
                time.sleep(2)
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
        # Ensure a space between a digit run and an adjacent Persian word
        # (model sometimes glues them together, e.g. "389میلیون").
        text = re.sub(r'(\d)([\u0600-\u06FF])', r'\1 \2', text)
        text = re.sub(r'([\u0600-\u06FF])(\d)', r'\1 \2', text)
        return text

    def _fix_rtl_flow(self, text):
        """Persian is RTL, but runs of Latin letters/numbers/tickers embedded in
        it (company names, %, $ figures) confuse the Unicode bidi algorithm and
        visually scramble the sentence. Wrap each such run in RTL isolate marks
        (U+2067 ... U+2069) so it renders inline without reordering the rest of
        the sentence, and force the whole string to start RTL (U+200F)."""
        if not text:
            return text
        RLI, PDI, RLM = '\u2067', '\u2069', '\u200f'
        wrapped = re.sub(
            r'[A-Za-z0-9][A-Za-z0-9 .,%$&/\-]*[A-Za-z0-9%]|[A-Za-z0-9]',
            lambda m: f'{RLI}{m.group(0)}{PDI}',
            text
        )
        return RLM + wrapped

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
        logger.info(f"Processing (hint={hint}, score={self._domain_score(final_url, publisher)}): {publisher} | {raw_title[:50]}...")

        snippet = entry.get('description', raw_title)
        text, photo_url = self.scrape_article_data(final_url, snippet, raw_image=entry.get('image'))

        if hint < CONFIG.get('MIN_AI_URGENCY_HINT', 4) and len(text) < 200:
            return None

        ai = self.analyze_with_ai(raw_title, text, publisher)
        if not ai:
            return None

        try:
            urgency_val = int(ai.get('urgency', 3))
        except Exception:
            urgency_val = 3

        # Hard filter: drop anything the AI itself scored as unimportant, so
        # low-value items never even get stored.
        if urgency_val < 4:
            return None

        try:
            ts = parser.parse(entry.get('published date')).timestamp()
        except Exception:
            ts = time.time()

        category = ai.get('category', 'اقتصاد')
        if category not in CATEGORY_FALLBACK_IMAGE:
            category = 'اقتصاد'

        photo_url = self._pick_image(photo_url, entry.get('image'), category=category)
        news_id = self._generate_news_id(clean_final_url)

        def clean_fa(t):
            return self._fix_rtl_flow(self._fix_common_typos(self._strip_foreign_scripts(t)))

        return {
            "id": news_id,
            "title_fa": clean_fa(ai.get('title_fa', raw_title)),
            "title_en": raw_title,
            "summary": [clean_fa(s) for s in ai.get('summary', [snippet])[:3]],
            "body_fa": clean_fa(ai.get('body_fa', '')),
            "category": category,
            "tag": ai.get('tag', 'General'),
            "urgency": urgency_val,
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

        cat_emoji = {'اقتصاد': '📊', 'کریپتو': '🪙', 'تکنولوژی': '💻'}

        details_parts = []
        for i, it in enumerate(items_sorted, 1):
            title = esc(it.get('title_fa') or it.get('title_en'))
            source = esc(it.get('source', 'Unknown'))
            cat = it.get('category', 'اقتصاد')
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
            by_cat.setdefault(it.get('category', 'اقتصاد'), []).append(it)

        cat_emoji = {'اقتصاد': '📊', 'کریپتو': '🪙', 'تکنولوژی': '💻'}

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
                f"🪙 سکه تمام: {esc(market.get('coin_irt'))}\n"
                f"🥇 انس طلا: {esc(market.get('gold_oz_usd'))}\n"
                f"₮ تتر: {esc(market.get('usdt_irt'))}\n"
                f"₿ بیت‌کوین: {esc(market.get('btc'))}\n"
                f"Ξ اتریوم: {esc(market.get('eth'))}\n"
                f"✕ ریپل: {esc(market.get('xrp'))}"
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
                key=lambda x: self._domain_score(x.get('url'), x.get('publisher', {}).get('title', '')),
                reverse=True
            )
            candidates = candidates[:CONFIG.get('MAX_CANDIDATES', 20)]
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
