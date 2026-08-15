import os
from datetime import datetime, timezone, timedelta
from statistics import mean
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
ECB_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
BEIJING = timezone(timedelta(hours=8))


def get_boc_gbp_sell():
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(BOC_URL, headers=headers, timeout=20)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if cells and cells[0] == "英镑" and len(cells) >= 8:
            sell_price = float(cells[3]) / 100
            publish_date = cells[6]
            publish_time = cells[7]
            return sell_price, publish_date, publish_time

    raise RuntimeError("没有在中国银行外汇牌价页面找到英镑数据")


def get_ecb_gbp_cny_history():
    headers = {"User-Agent": "gbp-rate-notify/1.0"}
    response = requests.get(ECB_90D_URL, headers=headers, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    rows = []

    for cube in root.iter():
        date_text = cube.attrib.get("time")
        if not date_text:
            continue

        rates = {}
        for child in list(cube):
            currency = child.attrib.get("currency")
            rate = child.attrib.get("rate")
            if currency and rate:
                rates[currency] = float(rate)

        if "GBP" in rates and "CNY" in rates:
            gbp_cny = rates["CNY"] / rates["GBP"]
            rows.append((date_text, gbp_cny))

    rows.sort(key=lambda item: item[0])
    if len(rows) < 20:
        raise RuntimeError("ECB 最近90天数据不足")

    return rows


def rates_in_last_days(rows, days):
    latest_date = datetime.fromisoformat(rows[-1][0]).date()
    start_date = latest_date - timedelta(days=days - 1)
    return [rate for date_text, rate in rows if datetime.fromisoformat(date_text).date() >= start_date]


def percentile_rank(values, current):
    count = sum(value <= current for value in values)
    return round(count / len(values) * 100)


def classify(percentile):
    if percentile <= 25:
        return "比较划算", "近期需要英镑的话，可以考虑换计划金额的 30%–40%，剩下继续观察。"
    if percentile <= 70:
        return "一般", "如果近期要用，可以先换 20%–30%；不着急就继续分批。"
    return "偏贵", "如果不急，今天可以少换或先等等；必须用的话只换近期刚需部分。"


def trend_text(adjusted_rates):
    avg5 = mean(adjusted_rates[-5:])
    avg20 = mean(adjusted_rates[-20:])
    change = (avg5 / avg20 - 1) * 100

    if change <= -0.35:
        return "短期偏下，对人民币买英镑相对有利。", change
    if change >= 0.35:
        return "短期偏上，对人民币买英镑相对不利。", change
    return "短期更像震荡，没有很明确的单边方向。", change


def serverchan_send(sendkey, title, desp):
    if not sendkey:
        raise RuntimeError("缺少 SERVERCHAN_SENDKEY")

    if sendkey.startswith("sctp"):
        import re

        match = re.match(r"sctp(\d+)t", sendkey)
        if not match:
            raise RuntimeError("Server酱 SendKey 格式不正确")
        url = f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    response = requests.post(
        url,
        json={"title": title, "desp": desp},
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(f"Server酱 HTTP 状态异常：{response.status_code}")

    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Server酱返回失败，code={data.get('code')}")

    print("Server酱推送成功")


def build_report():
    boc_sell, publish_date, publish_time = get_boc_gbp_sell()
    history = get_ecb_gbp_cny_history()

    dates = [item[0] for item in history]
    market_rates = [item[1] for item in history]
    latest_market = market_rates[-1]

    # 今天的中行现汇卖出价包含银行价差；ECB数据是市场参考价。
    # 用今天的价差比例把历史市场价折算到近似同一口径，只用于比较近期高低。
    bank_factor = boc_sell / latest_market
    adjusted_rows = [(date_text, rate * bank_factor) for date_text, rate in history]
    adjusted = [rate for _, rate in adjusted_rows]

    yesterday = adjusted[-2]
    avg7 = mean(rates_in_last_days(adjusted_rows, 7))
    avg30 = mean(rates_in_last_days(adjusted_rows, 30))
    avg90 = mean(adjusted)
    percentile = percentile_rank(adjusted, boc_sell)
    verdict, suggestion = classify(percentile)
    trend, momentum = trend_text(adjusted)

    today = datetime.now(BEIJING)
    source_date = dates[-1]
    boc_date = publish_date[:10].replace("/", "-")
    is_stale = boc_date != today.strftime("%Y-%m-%d")
    stale_note = "（今天未显示新日期，可能是周末/非交易时段沿用上一牌价）" if is_stale else ""

    cost_1000 = boc_sell * 1000
    diff_vs_30 = (boc_sell / avg30 - 1) * 100

    title = f"英镑购汇日报｜{boc_sell:.4f}｜{verdict}"
    desp = f"""## 今天结论：**{verdict}**

- **中行英镑现汇卖出价：1 GBP ≈ ¥{boc_sell:.4f}**
- 中行发布时间：{publish_date} {publish_time} {stale_note}
- 换 £1,000 约需：**¥{cost_1000:,.0f}**

## 近期对比
> 历史值用 ECB GBP/CNY 市场参考汇率，按今天中行价差比例折算，只用于比较高低位置，不代表当时银行真实成交价。

- 上一交易日参考：¥{yesterday:.4f}
- 近 7 天内交易日均值：¥{avg7:.4f}
- 近 30 天内交易日均值：¥{avg30:.4f}
- 近 90 天内交易日均值：¥{avg90:.4f}
- 今天比近30天均值：**{diff_vs_30:+.2f}%**
- 近90天位置：大约在 **第 {percentile} 百分位**（越低越适合买英镑）

## 大白话建议
**{suggestion}**

## 未来 1–2 周方向
{trend}

这是根据最近 5 个交易日和 20 个交易日的相对位置做的简单趋势判断（动量约 {momentum:+.2f}%），**不是保证，也不是精确预测**。

数据来源：中国银行外汇牌价（实际购汇参考）+ 欧洲央行参考汇率（近期趋势比较，最新交易日 {source_date}）。

实际成交请以你下单银行当时显示的 **英镑现汇卖出价** 为准。
"""
    return title, desp


def main():
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()

    try:
        title, desp = build_report()
        print(title)
        serverchan_send(sendkey, title, desp)
    except Exception as exc:
        if sendkey:
            try:
                serverchan_send(
                    sendkey,
                    "英镑购汇日报获取失败",
                    f"今天的自动汇率简报没有生成成功。\n\n原因：`{type(exc).__name__}: {exc}`",
                )
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
