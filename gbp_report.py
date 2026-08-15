import os
from datetime import datetime, timezone, timedelta
from statistics import mean
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
ECB_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
BEIJING = timezone(timedelta(hours=8))


def parse_price(text):
    text = text.replace(",", "").strip()
    return float(text) / 100 if text else None


def get_boc_gbp_quote():
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(BOC_URL, headers=headers, timeout=20)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if cells and cells[0] == "英镑" and len(cells) >= 8:
            publish_date = cells[6].strip()
            publish_time = cells[7].strip()
            publish_stamp = (
                publish_date
                if " " in publish_date
                else f"{publish_date} {publish_time}".strip()
            )

            return {
                "spot_buy": parse_price(cells[1]),
                "cash_buy": parse_price(cells[2]),
                "spot_sell": parse_price(cells[3]),
                "cash_sell": parse_price(cells[4]),
                "conversion": parse_price(cells[5]),
                "publish_stamp": publish_stamp,
            }

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
    return [
        rate
        for date_text, rate in rows
        if datetime.fromisoformat(date_text).date() >= start_date
    ]


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


def parse_boc_date(publish_stamp):
    date_text = publish_stamp.split()[0].replace("/", "-")
    return datetime.strptime(date_text, "%Y-%m-%d").date()


def validate_quote(quote, latest_market, latest_market_date):
    errors = []
    notes = []
    buy = quote["spot_buy"]
    sell = quote["spot_sell"]
    conversion = quote["conversion"]

    if buy is None or sell is None:
        errors.append("中行现汇买入价或现汇卖出价为空")
        return errors, notes, None, None

    if not (5.0 <= buy <= 15.0 and 5.0 <= sell <= 15.0):
        errors.append("中行英镑价格超出合理解析范围，可能抓错了列")

    spread_pct = (sell / buy - 1) * 100 if buy else None
    if sell <= buy:
        errors.append("中行现汇卖出价不高于现汇买入价，数据关系异常")
    elif spread_pct > 3.0:
        errors.append(f"中行现汇买卖价差达到 {spread_pct:.2f}%，明显偏大")

    market_gap_pct = (sell / latest_market - 1) * 100
    today = datetime.now(BEIJING).date()
    market_age = (today - latest_market_date).days

    if market_age <= 5 and abs(market_gap_pct) > 3.0:
        errors.append(
            f"中行现汇卖出价与 ECB 最新市场参考价相差 {market_gap_pct:+.2f}%，超过 3% 校验线"
        )
    elif market_age > 5:
        notes.append(f"ECB 最新数据距今天 {market_age} 天，只作辅助参考")

    if conversion:
        conversion_gap = abs(sell / conversion - 1) * 100
        if conversion_gap > 3.0:
            errors.append(
                f"中行现汇卖出价与中行折算价相差 {conversion_gap:.2f}%，可能存在抓取异常"
            )

    publish_date = parse_boc_date(quote["publish_stamp"])
    quote_age = (today - publish_date).days
    if quote_age < 0:
        errors.append("中行牌价发布日期晚于当前日期，时间数据异常")
    elif quote_age > 0:
        notes.append(f"中行当前页面展示的是 {quote_age} 天前更新的牌价")
    if quote_age > 7:
        errors.append("中行牌价超过 7 天未更新，不适合作为当前购汇判断")

    return errors, notes, spread_pct, market_gap_pct


def report_slot():
    hour = datetime.now(BEIJING).hour
    return "上午参考" if hour < 14 else "下午参考"


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
    slot = report_slot()
    quote = get_boc_gbp_quote()
    history = get_ecb_gbp_cny_history()

    dates = [item[0] for item in history]
    market_rates = [item[1] for item in history]
    latest_market = market_rates[-1]
    latest_market_date = datetime.fromisoformat(dates[-1]).date()

    errors, notes, spread_pct, market_gap_pct = validate_quote(
        quote, latest_market, latest_market_date
    )

    buy = quote["spot_buy"]
    sell = quote["spot_sell"]
    conversion = quote["conversion"]
    note_text = "\n".join(f"- {note}" for note in notes) if notes else "- 无"

    if errors:
        error_text = "\n".join(f"- **{error}**" for error in errors)
        title = f"英镑购汇｜{slot}｜⚠️ 数据异常"
        desp = f"""## ⚠️ 今天先不要按自动结论购汇

自动校验发现数据异常，本次**不输出“划算 / 一般 / 偏贵”判断**。

### 抓到的原始数据

- 中行现汇买入价：{f"¥{buy:.4f}" if buy else "空"}
- 中行现汇卖出价：{f"¥{sell:.4f}" if sell else "空"}
- 中行折算价：{f"¥{conversion:.4f}" if conversion else "空"}
- 中行发布时间：{quote["publish_stamp"]}
- ECB 最新 GBP/CNY 参考：¥{latest_market:.4f}（{dates[-1]}）

### 异常项

{error_text}

### 其他提示

{note_text}

**建议：打开中国银行或你实际购汇银行 App，直接核对“英镑现汇卖出价”。确认后再决定是否购汇。**
"""
        return title, desp

    # 中行卖出价包含银行价差；ECB 是市场参考价。
    # 用当前中行/ECB比例把历史市场价折算到近似同一口径，仅比较近期高低。
    bank_factor = sell / latest_market
    adjusted_rows = [(date_text, rate * bank_factor) for date_text, rate in history]
    adjusted = [rate for _, rate in adjusted_rows]

    yesterday = adjusted[-2]
    avg7 = mean(rates_in_last_days(adjusted_rows, 7))
    avg30 = mean(rates_in_last_days(adjusted_rows, 30))
    avg90 = mean(adjusted)
    percentile = percentile_rank(adjusted, sell)
    verdict, suggestion = classify(percentile)
    trend, momentum = trend_text(adjusted)

    cost_1000 = sell * 1000
    diff_vs_30 = (sell / avg30 - 1) * 100

    title = f"英镑购汇｜{slot}｜{sell:.4f}｜{verdict}"
    desp = f"""## {slot}结论：**{verdict}**

- **中行英镑现汇卖出价：1 GBP ≈ ¥{sell:.4f}**
- 换 £1,000 约需：**¥{cost_1000:,.0f}**
- 中行发布时间：{quote["publish_stamp"]}

## 数据校验：✅ 正常

- 中行现汇买入价：¥{buy:.4f}
- 中行现汇卖出价：¥{sell:.4f}
- 买卖价差：{spread_pct:.2f}%
- 中行折算价：{f"¥{conversion:.4f}" if conversion else "无"}
- ECB 最新市场参考：¥{latest_market:.4f}（{dates[-1]}）
- 中行卖出价相对 ECB：{market_gap_pct:+.2f}%
{note_text if notes else ""}

## 近期对比

> 历史值用 ECB GBP/CNY 市场参考汇率，按当前中行价差比例折算，只用于比较高低位置，不代表当时银行真实成交价。

- 上一交易日参考：¥{yesterday:.4f}
- 近 7 天内交易日均值：¥{avg7:.4f}
- 近 30 天内交易日均值：¥{avg30:.4f}
- 近 90 天内交易日均值：¥{avg90:.4f}
- 当前比近30天均值：**{diff_vs_30:+.2f}%**
- 近90天位置：大约在 **第 {percentile} 百分位**（越低越适合买英镑）

## 大白话建议

**{suggestion}**

## 未来 1–2 周方向

{trend}

这是根据最近 5 个交易日和 20 个交易日的相对位置做的简单方向判断（动量约 {momentum:+.2f}%），**不是保证，也不是精确预测**。

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
                    f"英镑购汇｜{report_slot()}｜获取失败",
                    f"本次自动汇率简报没有生成成功。\n\n原因：`{type(exc).__name__}: {exc}`",
                )
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
