import json
import os
import re
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


PRODUCT_NAME = "BD必第能量酵母蛋白粉 中式绿豆味 4磅"
STORE_NAME = "BANNY DEER官方旗舰店（必第BD）"
USER_AGENT = "Mozilla/5.0 (compatible; bd-stock-monitor/1.0)"
REDIRECT_CODES = {301, 302, 303, 307, 308}
PLATFORMS = (
    {
        "key": "douyin",
        "name": "抖音",
        "url_env": "DOUYIN_PRODUCT_URL",
        "cookie_env": "DOUYIN_COOKIE",
        "domains": ("douyin.com", "jinritemai.com"),
    },
    {
        "key": "pinduoduo",
        "name": "拼多多",
        "url_env": "PINDUODUO_PRODUCT_URL",
        "cookie_env": "PINDUODUO_COOKIE",
        "domains": ("pinduoduo.com", "yangkeduo.com"),
    },
)


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", value).casefold()


def host_allowed(host, domains):
    host = (host or "").casefold().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _walk_json(value):
    if isinstance(value, dict):
        item_type = value.get("@type", [])
        if isinstance(item_type, str):
            item_type = [item_type]
        if any(str(part).casefold() == "product" for part in item_type):
            yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _schema_name(value):
    if isinstance(value, dict):
        return str(value.get("name", "")).strip()
    return str(value or "").strip()


def _matching_offers(product):
    raw_offers = product.get("offers")
    offers = []
    for offer in _as_list(raw_offers):
        if not isinstance(offer, dict):
            continue
        offer_type = offer.get("@type", [])
        if isinstance(offer_type, str):
            offer_type = [offer_type]
        if any(str(part).casefold() == "aggregateoffer" for part in offer_type):
            continue

        seller = offer.get("seller") or product.get("seller")
        seller_name = _schema_name(seller)
        if normalize(seller_name) != normalize(STORE_NAME):
            continue

        price = offer.get("price")
        currency = offer.get("priceCurrency")
        if price is None:
            specification = offer.get("priceSpecification")
            if isinstance(specification, dict):
                price = specification.get("price")
                currency = specification.get("priceCurrency", currency)
        if price is None or str(currency or "").upper() != "CNY":
            continue

        availability = str(offer.get("availability", "")).casefold()
        if "outofstock" in availability:
            stock = "out"
        elif "instock" in availability:
            stock = "in"
        else:
            stock = "unknown"

        try:
            price_value = float(str(price).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if price_value <= 0:
            continue

        offers.append(
            {
                "stock": stock,
                "price": price_value,
                "seller": seller_name,
                "product": str(product.get("name", "")).strip(),
            }
        )
    return offers


def extract_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text()).strip()
        if not raw:
            continue
        try:
            products.extend(_walk_json(json.loads(raw)))
        except json.JSONDecodeError:
            continue
    target = [
        product
        for product in products
        if normalize(PRODUCT_NAME) in normalize(product.get("name", ""))
    ]
    if not target:
        return None, "页面没有提供可核实的目标商品名称、绿豆味和4磅规格"

    offers = []
    for product in target:
        offers.extend(_matching_offers(product))
    unique = {
        (item["stock"], item["price"], normalize(item["seller"]), normalize(item["product"])): item
        for item in offers
    }
    if not unique:
        return None, "页面未提供可核实的官方店铺、CNY价格和SKU库存字段"
    if len(unique) != 1:
        return None, "页面返回多个匹配报价，无法确认哪个报价对应目标SKU"

    listing = next(iter(unique.values()))
    if listing["stock"] == "unknown":
        return None, "页面没有明确给出目标SKU的有货或缺货状态"
    return listing, None


def _page_problem(response, domains):
    host = urlparse(response.url).hostname or ""
    if not host_allowed(host, domains):
        return "页面重定向到非目标平台域名，已停止核验"
    if response.status_code in (401, 403, 429):
        return f"平台访问受限（HTTP {response.status_code}），本轮无法核验"
    if response.status_code < 200 or response.status_code >= 300:
        return f"商品页面返回 HTTP {response.status_code}，本轮无法核验"

    path = urlparse(response.url).path.casefold()
    if any(part in path for part in ("/login", "/passport", "/auth")):
        return "页面跳转到登录入口，请刷新平台登录态后再继续"

    page_text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
    if any(marker in page_text for marker in ("请先登录", "登录后查看", "登录已过期", "请重新登录")):
        return "页面提示需要登录，请刷新平台登录态后再继续"
    if any(marker in page_text for marker in ("安全验证", "人机验证", "滑动验证", "验证码")):
        return "平台要求安全验证，自动巡检未尝试绕过，本轮无法核验"
    return None


def inspect_platform(config):
    platform = config["name"]
    url = os.environ.get(config["url_env"], "").strip()
    if not url:
        return {"platform": platform, "state": "unknown", "reason": f"未配置 {config['url_env']} 商品详情链接"}

    parsed = urlparse(url)
    if parsed.scheme != "https" or not host_allowed(parsed.hostname, config["domains"]):
        return {"platform": platform, "state": "unknown", "reason": "商品链接不是受支持的目标平台HTTPS链接"}

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    cookie = os.environ.get(config["cookie_env"], "").strip()
    if cookie:
        headers["Cookie"] = cookie

    current_url = url
    response = None
    try:
        for _ in range(6):
            current = urlparse(current_url)
            if current.scheme != "https" or not host_allowed(current.hostname, config["domains"]):
                return {"platform": platform, "state": "unknown", "reason": "页面跳转到非目标平台域名，已停止核验"}
            response = requests.get(
                current_url, headers=headers, timeout=20, allow_redirects=False
            )
            if response.status_code not in REDIRECT_CODES:
                break
            location = response.headers.get("Location")
            if not location:
                return {"platform": platform, "state": "unknown", "reason": "商品页面重定向信息不完整"}
            current_url = urljoin(current_url, location)
        else:
            return {"platform": platform, "state": "unknown", "reason": "商品页面重定向过多"}

        problem = _page_problem(response, config["domains"])
        if problem:
            return {"platform": platform, "state": "unknown", "reason": problem}

        listing, problem = extract_listing(response.text)
        if problem:
            return {"platform": platform, "state": "unknown", "reason": problem}
        return {
            "platform": platform,
            "state": listing["stock"],
            "price": listing["price"],
            "url": response.url,
        }
    except requests.RequestException:
        return {"platform": platform, "state": "unknown", "reason": "商品页面连接失败，本轮无法核验"}
    except (ValueError, TypeError):
        return {"platform": platform, "state": "unknown", "reason": "商品页面结构无法解析，本轮无法核验"}
    finally:
        if response is not None:
            response.close()


def serverchan_send(sendkey, title, description):
    if sendkey.startswith("sctp"):
        match = re.match(r"sctp(\d+)t", sendkey)
        if not match:
            raise RuntimeError("Server酱 SendKey 格式不正确")
        url = f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    response = requests.post(
        url, json={"title": title, "desp": description}, timeout=20
    )
    try:
        if response.status_code != 200 or response.json().get("code") != 0:
            raise RuntimeError("Server酱推送失败")
    finally:
        response.close()


def send_to_all(sendkeys, title, description):
    failures = []
    for index, sendkey in enumerate(sendkeys, start=1):
        try:
            serverchan_send(sendkey, title, description)
        except Exception:
            failures.append(f"Server酱账号 {index} 推送失败")
    if failures:
        raise RuntimeError("；".join(failures))


def build_notification(results):
    available = [item for item in results if item["state"] == "in"]
    uncertain = [item for item in results if item["state"] == "unknown"]
    title = (
        "BD绿豆味4磅监控｜发现有货"
        if available
        else "BD绿豆味4磅监控｜本次无法核验"
    )

    sections = [f"目标商品：{PRODUCT_NAME}", f"目标店铺：{STORE_NAME}"]
    for item in results:
        if item["state"] == "in":
            sections.extend(
                [
                    f"## {item['platform']}｜确认有货",
                    f"- 店铺：{STORE_NAME}",
                    f"- 商品/SKU：{PRODUCT_NAME}",
                    f"- 价格：¥{item['price']:.2f}",
                    f"- 商品链接：[打开商品]({item['url']})",
                ]
            )
        elif item["state"] == "out":
            sections.extend(
                [
                    f"## {item['platform']}｜确认无货",
                    f"- 店铺与目标SKU已由商品页结构化信息匹配。",
                ]
            )
        else:
            sections.extend(
                [
                    f"## {item['platform']}｜无法核验",
                    f"- 原因：{item['reason']}",
                ]
            )

    if available and uncertain:
        sections.append("另一平台本轮无法确认库存；不会据此判断有货或无货。")
    return title, "\n\n".join(sections)


def main():
    sendkeys = [
        value
        for value in (
            os.environ.get("SERVERCHAN_SENDKEY", "").strip(),
            os.environ.get("SERVERCHAN_SENDKEY_2", "").strip(),
        )
        if value
    ]
    if not sendkeys:
        raise RuntimeError("没有配置任何 Server酱 SendKey")

    results = [inspect_platform(config) for config in PLATFORMS]
    if all(item["state"] == "out" for item in results):
        print("两个平台均已核验为无货，本轮保持静默。")
        return

    title, description = build_notification(results)
    send_to_all(sendkeys, title, description)
    print("库存状态通知已发送。")


if __name__ == "__main__":
    main()
