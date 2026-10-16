#!/usr/bin/env python3
"""
Beyblade X 補貨監察 (CX-20 / BX-54)
有貨或新上架 -> Telegram 通知你，由你自己 checkout。

安裝:
  pip install requests playwright
  playwright install chromium      # Hobbyland 要用瀏覽器先睇到內容

用法:
  export TG_TOKEN="BotFather給你的token"
  export TG_CHAT_ID="你的chat id"
  python beyblade_watch.py          # 檢查一次 (配合 cron)
  python beyblade_watch.py --loop   # 每 5 分鐘自己循環
"""
import json, os, re, sys, time
import requests

# ---------------- 設定 ----------------
MODELS = ["CX-20", "BX-54"]  # 想搵嘅型號

# type = "listing":    純 HTML 頁面，睇有冇出現型號
# type = "listing_js": 要用瀏覽器 (JavaScript 先顯示內容) 嘅頁面，同上
# type = "product":    已知商品頁，睇有冇「加入購物車」而且冇「缺貨」字眼
WATCHES = [
    {
        "name": "Last Chance Toy BeybladeX 專區",
        "type": "listing",
        "url": "https://lastchancetoy.com/collections/beybladex",
    },
    {
        # 玩具反斗城 爆旋陀螺X 預售發貨時間表 (網頁係純HTML)
        "name": "玩具反斗城 預售發貨表",
        "type": "listing",
        "url": "https://www.toysrus.com.hk/zh-hk/promotions/beybladex-preorder-delivery-update/item-list/",
    },
    {
        # Hobbyland 係 JavaScript 網頁，一定要用 listing_js
        "name": "Hobbyland Beyblade 分類",
        "type": "listing_js",
        "url": "https://www.hobbylandeshop.com/product-category/takaratomy/beyblade陀螺",
    },
    # 玩具反斗城個別商品頁 (頁面有「暫時缺貨」字眼 = 冇貨；字眼消失 = 可預訂)
    {
        "name": "玩具反斗城 CX-20 改造套裝C",
        "type": "product",
        "must": "Beyblade",
        "url": "https://www.toysrus.com.hk/zh-hk/10173908.html",
    },
    {
        "name": "玩具反斗城 BX-54 凱旋超神0-70LP",
        "type": "product",
        "must": "Beyblade",
        "url": "https://www.toysrus.com.hk/zh-hk/10173909.html",
    },
]

IN_STOCK_WORDS = ["加入購物車", "加入购物车", "Add to cart", "立即購買", "Buy now", "預訂", "預購"]
OUT_WORDS = ["暫時缺貨", "缺貨", "售罄", "已售完", "暫時無貨", "Sold out", "Out of stock"]

CHECK_EVERY = 300  # 秒 (--loop 模式)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal stock watcher)"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BUY_WORDS = ["加入購物車", "加入购物车", "預訂", "立即購買", "Add to cart", "Pre-order"]
# --------------------------------------


def notify(msg: str) -> bool:
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    print(msg)
    if not (token and chat):
        print("(未設定 TG_TOKEN / TG_CHAT_ID，只印出)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": msg},
            timeout=15,
        )
        print("Telegram 回覆:", r.status_code, r.text[:200])
        return r.ok
    except requests.RequestException as e:
        print("Telegram 發送失敗:", e)
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def _new_page(p):
    browser = p.chromium.launch()
    ctx = browser.new_context(
        user_agent=BROWSER_UA, locale="zh-HK", timezone_id="Asia/Hong_Kong",
        viewport={"width": 1366, "height": 900},
    )
    return browser, ctx.new_page()


def fetch_js(url):
    """用無頭瀏覽器開頁面，等 JavaScript 載入完先攞內容。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page = _new_page(p)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
    return html


def fetch_js_text(url):
    """用瀏覽器開頁面，回傳 (真正睇到嘅文字, 睇到嘅按鈕文字列表)。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page = _new_page(p)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # 最多等 25 秒，等「缺貨」字眼或可見嘅購買按鈕出現 (價錢下面嗰格係慢啲先載入)
        try:
            page.wait_for_function(
                """(a) => {
                    const t = document.body.innerText;
                    if (a.out.some(w => t.includes(w))) return true;
                    return [...document.querySelectorAll('button,input[type=button],input[type=submit]')]
                        .some(e => e.offsetParent !== null &&
                              a.buy.some(w => (e.innerText || e.value || '').includes(w)));
                }""",
                arg={"out": OUT_WORDS, "buy": BUY_WORDS}, timeout=25000,
            )
        except Exception:
            pass
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        buttons = page.eval_on_selector_all(
            "button, input[type=button], input[type=submit]",
            "els => els.filter(e => e.offsetParent !== null)"
            ".map(e => (e.innerText || e.value || '').trim()).filter(Boolean)",
        )
        browser.close()
    return text, buttons


def norm(s):
    # 去走空格、底線、連字號，令 "BX-54" 同 "BX54" 都對得上
    return re.sub(r"[\s_\-]", "", s).upper()


def check_listing(w):
    """回傳 (狀態字串, 訊息)；狀態字串改變先通知。"""
    html = fetch_js(w["url"]) if w["type"] == "listing_js" else fetch(w["url"])
    text = norm(html)
    found = [m for m in MODELS if norm(m) in text]
    if found:
        return "found:" + ",".join(found), f"🔔 {w['name']} 出現 {', '.join(found)}!\n{w['url']}"
    return "none", ""


def check_product(w):
    text, buttons = fetch_js_text(w["url"])
    if w.get("must") and w["must"].lower() not in text.lower():
        return "error", ""
    out = [k for k in OUT_WORDS if k.lower() in text.lower()]
    buy = [t for t in buttons if any(k.lower() in t.lower() for k in BUY_WORDS)]
    print(f"   可見文字 {len(text)} 字；缺貨字眼: {out}；購買按鈕: {buy}")
    if out:
        return "out", ""
    if buy:
        return "instock", f"🔔 {w['name']} 可以買!\n{w['url']}"
    # 又冇缺貨字眼、又見唔到購買按鈕 = 唔肯定，印出內容方便除錯，唔通知
    print("   [除錯] 頁面文字開頭:", text[:400].replace("\n", " | "))
    print("   [除錯] 所有按鈕:", buttons[:15])
    return "unknown", ""


def run_once():
    state = load_state()
    if not state.get("_tg_tested"):
        if notify("✅ Beyblade 監察已啟動，Telegram 通知正常"):
            state["_tg_tested"] = True
    for w in WATCHES:
        key = w["name"]
        try:
            fn = check_listing if w["type"] in ("listing", "listing_js") else check_product
            status, msg = fn(w)
        except Exception as e:  # 網站掛咗或改版，唔好令成個script死
            print(f"[{key}] 檢查失敗: {e}")
            continue
        if status in ("error", "unknown"):
            print(f"[{key}] 判斷唔到 ({status})，今次跳過，唔通知")
            continue
        prev = state.get(key)
        print(f"[{key}] {status}")
        # 只喺狀態「變成有貨/出現」嗰陣通知一次
        if msg and status != prev:
            notify(msg)
        state[key] = status
    save_state(state)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            run_once()
            time.sleep(CHECK_EVERY)
    else:
        run_once()
