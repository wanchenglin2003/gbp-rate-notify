# BD 补货监控

此仓库的定时任务现检查“BD必第能量酵母蛋白粉 中式绿豆味 4磅”，目标店铺为“BANNY DEER官方旗舰店（必第BD）”。原汇率脚本 `gbp_report.py` 保留，工作流不再运行它。

## 定时与通知

工作流沿用原来的北京时间每天 10:40 和 16:30。发现任一平台目标 SKU 有货时，会向两个现有 ServerChan 设备发送平台、店铺、商品/SKU、价格和商品链接。两个平台都被明确核验为无货时保持静默。登录失效、访问被拦或页面数据不足以核验时会发送“无法核验”通知，不会把未知状态当成无货。

## GitHub Actions 配置

保留已有 Actions Secrets：

- `SERVERCHAN_SENDKEY`
- `SERVERCHAN_SENDKEY_2`

在仓库 Settings → Secrets and variables → Actions → Variables 中设置两个**目标商品详情页**链接：

- `DOUYIN_PRODUCT_URL`
- `PINDUODUO_PRODUCT_URL`

链接应来自目标官方店铺的商品详情页。不要填搜索页、店铺首页或第三方商品页。

如果页面必须登录才能读取，可将对应浏览器会话 Cookie 放入 Actions Secrets：

- `DOUYIN_COOKIE`
- `PINDUODUO_COOKIE`

Cookie 是登录凭据，登录态过期后需要在 GitHub Secrets 中更新。工作流只把它用于访问对应平台，不会写入日志或通知。

## 当前自动核验范围

监控器只接受商品详情页提供的 schema.org JSON-LD 结构化数据，并要求其中的商品名包含目标完整品名、报价店铺名与目标店铺完全一致、价格币种为 CNY，且库存状态明确为 InStock 或 OutOfStock。遇到登录页、安全验证、平台访问限制、店铺/SKU/价格/库存字段缺失或多个报价歧义时，会通知无法核验。

目前的公开搜索页未提供足以确认两个平台目标 SKU 和库存的页面数据。因此，填入商品链接和必要 Cookie 后，如果平台页面仍不提供上述结构化字段，还需要为相应平台补充经过实际页面验证的商品/SKU/库存解析器，或接入能读取这些字段的官方授权 API。当前监控器不会用第三方搜索结果或商品标题猜测库存。

可以通过 Actions 页面手动运行工作流；若配置尚不完整，会收到具体的无法核验原因。
