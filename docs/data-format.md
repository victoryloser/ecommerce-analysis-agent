# 数据格式说明

当前版本支持 Excel `.xlsx` 和 UTF-8 / UTF-8 BOM 编码的 CSV `.csv`。每张表的第一行是字段名；Excel 一张工作表对应一张业务表，CSV 一个文件对应一张业务表。

仓库不分发业务数据或历史样例。使用者自行提供有权使用的数据；导入文件、数据库、历史对话和导出结果保存在本机，不随源码上传。自动测试和文档截图使用代码临时构造的虚构记录，生成方式见 [开发文档](development.md#文档截图)。

完整九表模板需要下文列出的九张表和对应字段。源文件和列名可以不同，通过页面中的字段对应关系转换为标准字段。

## 订单和商品明细模板

| 来源 | 必需映射 | 可选映射及用途 |
|---|---|---|
| 订单 | `order_id`、`purchase_at`、`order_status` | 首次接入确认状态，例如将“已送达”映射为 `delivered` |
| 商品明细 | `order_id`、`order_item_id`，明细键支持带前导零的文本 | 行商品额映射到 `price`。单价另配 `quantity` 后相乘。可映射 `freight_value`、`category_id`、`category_name_zh`、`seller_id`、`seller_name`、`product_id` |
| 支付 | 添加此表时映射 `order_id`、`payment_sequence`、`payment_value` | 同单多笔支付按订单汇总 |
| 评价 | 添加此表时映射 `review_id`、`order_id`、`review_score` | 开启平均评分和低分率 |

轻量模板使用人民币元，商品成交额不含运费，换算结果须精确到分。金额定义待核实时可以选择“待确认”先统计订单。缺少整张支付表时支付指标标为未提供。已交付支付表、个别订单缺记录时支付额标为未知，真实零值显示为零。映射、可用能力和金额规则随快照保存。

团队上传页面可展开高级映射，在 `entity_aliases` 中记录经业务确认的别名，例如 `{"sellers":{"S001":["华东一店"]}}`。点名提问时只发送相关候选，重名时选择 ID。完整排名由数据库计算。字段名变更时，在团队导入界面选择原快照为“来源版本”，核对新映射后继续沿用该轻量数据源的交付目录。

## 退款流水

在团队页面单独上传 UTF-8 CSV，将一份完整交付绑定到已选销售快照。相同文件与相同声明可重复导入。更正金额、时区或交付期间时创建同源新版。交付区间采用左闭右开。只有表头的文件可用于确认该期间零退款。

| 列 | 填写方式 |
|---|---|
| `refund_id`、`order_id` | 唯一流水号和原订单号。一个订单的多次退款使用不同流水号。完全重复的流水归并，同号不同内容会指出冲突。 |
| `posted_at` | 实际入账时间。带偏移的 ISO 时间转换到交付声明的业务时区，本地时间按该时区解释。成功和撤销都需填写。 |
| `status`、`currency` | `success` 为成功，`reversal` 为撤销，`pending` 为待处理，`failed` 为失败。币种使用三位大写代码。 |
| `goods_amount`、`freight_amount`、`total_amount` | 填非负金额，最多两位小数。商品与运费均已提供时校核合计。仅有合计时可核对总退款，商品部分留待确认。已知某项为零时填 0。 |
| `original_refund_id` | 撤销需引用本次完整导出中已成功的原流水，订单、币种和金额一致。原流水可早于本次核账期间。首版按整笔冲回，同一原流水只冲回一次。 |

例如，一笔一月已送达的人民币商品销售 100 元，二月成功退商品 30 元和运费 2 元，二月这条流水填写 `goods_amount=30`、`freight_amount=2`、`total_amount=32`。在没有其他交易时，两个月的净商品额分别为 100 元和 −30 元。

规则 `refund-posted-v1` 按原订单下单期统计已送达商品额，按退款成功或撤销的入账期核算可归属商品退款。全店净商品额只在同币种相减。当前销售快照用人民币，其他币种分别列出退款。报表同时给出已确认部分、待核实流水数和完整净额。订单未匹配、商品金额存疑或退款无法拆分时，完整净额留空。明细授权成员可查看对应流水和订单。对外财务核账前，请确认交付期间、订单状态、含运费定义及企业采用的退款归属期。

## 值与约束

- `VARCHAR`：文本；标识和邮编建议在 Excel 中设为文本，保留前导零。
- `TIMESTAMP`：日期时间，建议使用 `YYYY-MM-DD HH:mm:ss`；Excel 日期时间单元格也可。
- `DECIMAL(18,2)`：金额数值，最多两位小数，不带货币符号和千位分隔符。
- `INTEGER` 为整数，`DOUBLE` 为数值。空单元格作为缺失值，不用数字 0 代替缺失值。
- 主键不可为空；多字段主键按组合识别记录。同键完全重复行归并，金额冲突保留候选并按绑定规则处理；非金额身份冲突拒绝导入。下文所列关联键必须有对应记录。
- 订单表至少需要一笔包含有效 `purchase_at` 的订单。其他表可以只有表头，但不能缺少被已有记录引用的客户、商品等关联数据。
- 默认经营指标按 `order_status=delivered` 统计，销售额不含运费。评分使用 1—5 分，低分定义为≤2分。

新上传使用正式整单金额准入规则，已有快照保持原规则。价格冲突、运费及支付缺失的具体处理见[指标口径](metrics.md#正式金额规则与历史兼容)。来源标签只描述数据来源，不决定计算方式。

## 业务表与字段

### `customers`

客户记录，customer_unique_id才是自然客户

主键：`customer_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `customer_id` | `VARCHAR` | 客户记录标识 |
| `customer_unique_id` | `VARCHAR` | 跨客户记录的自然客户标识 |
| `customer_zip_code` | `VARCHAR` | 客户邮编 |
| `customer_city` | `VARCHAR` | 客户城市 |
| `customer_state` | `VARCHAR` | 客户省份 |

### `geolocation`

邮编

主键：`zip_code`

| 字段 | 类型 | 含义 |
|---|---|---|
| `zip_code` | `VARCHAR` | 邮编 |
| `latitude` | `DOUBLE` | 纬度 |
| `longitude` | `DOUBLE` | 经度 |
| `city` | `VARCHAR` | 城市 |
| `state` | `VARCHAR` | 省份 |

### `orders`

订单

主键：`order_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `order_id` | `VARCHAR` | 订单唯一标识/关联键 |
| `customer_id` | `VARCHAR` | 客户记录标识 |
| `order_status` | `VARCHAR` | 订单状态；默认分析已送达 delivered，可指定其他状态 |
| `purchase_at` | `TIMESTAMP` | 下单时间，统计日期字段 |
| `approved_at` | `TIMESTAMP` | 支付审批时间 |
| `shipped_at` | `TIMESTAMP` | 发货时间 |
| `delivered_at` | `TIMESTAMP` | 实际送达时间 |
| `estimated_delivery_at` | `TIMESTAMP` | 预计送达时间 |

### `order_items`

订单商品明细

主键：`order_id`, `order_item_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `order_id` | `VARCHAR` | 订单唯一标识/关联键 |
| `order_item_id` | `INTEGER` | 订单内明细序号 |
| `product_id` | `VARCHAR` | 商品标识 |
| `seller_id` | `VARCHAR` | 商家标识 |
| `shipping_limit_at` | `TIMESTAMP` | 最晚发货时间 |
| `price` | `DECIMAL(18,2)` | 商品明细价款，不含运费 |
| `freight_value` | `DECIMAL(18,2)` | 运费，不计商品销售额 |

### `order_payments`

订单支付记录

主键：`order_id`, `payment_sequence`

| 字段 | 类型 | 含义 |
|---|---|---|
| `order_id` | `VARCHAR` | 订单唯一标识/关联键 |
| `payment_sequence` | `INTEGER` | 订单内支付序号 |
| `payment_type` | `VARCHAR` | 支付方式 |
| `payment_installments` | `INTEGER` | 分期期数 |
| `payment_value` | `DECIMAL(18,2)` | 单条支付金额 |

### `order_reviews`

评价记录

主键：`review_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `review_id` | `VARCHAR` | 评价标识 |
| `order_id` | `VARCHAR` | 订单唯一标识/关联键 |
| `review_score` | `INTEGER` | 评价分数，≤2为低分 |
| `review_title` | `VARCHAR` | 评价标题 |
| `review_message` | `VARCHAR` | 评价正文 |
| `review_created_at` | `TIMESTAMP` | 评价创建时间 |
| `seller_answered_at` | `TIMESTAMP` | 商家回复时间 |

### `products`

商品

主键：`product_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `product_id` | `VARCHAR` | 商品标识 |
| `category_id` | `VARCHAR` | 品类标识 |
| `product_name` | `VARCHAR` | 商品名称 |
| `product_name_length` | `INTEGER` | 商品名称长度 |
| `product_description_length` | `INTEGER` | 商品描述长度 |
| `product_photos_count` | `INTEGER` | 商品图片数量 |
| `product_weight_g` | `INTEGER` | 商品重量，克 |
| `product_length_cm` | `INTEGER` | 商品长度，厘米 |
| `product_height_cm` | `INTEGER` | 商品高度，厘米 |
| `product_width_cm` | `INTEGER` | 商品宽度，厘米 |
| `base_price` | `DECIMAL(18,2)` | 商品基础标价，不用于成交销售额 |

### `sellers`

商家

主键：`seller_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `seller_id` | `VARCHAR` | 商家标识 |
| `seller_name` | `VARCHAR` | 商家名称 |
| `seller_zip_code` | `VARCHAR` | 商家邮编 |
| `seller_city` | `VARCHAR` | 商家城市 |
| `seller_state` | `VARCHAR` | 商家省份 |

### `categories`

品类

主键：`category_id`

| 字段 | 类型 | 含义 |
|---|---|---|
| `category_id` | `VARCHAR` | 品类标识 |
| `category_name_en` | `VARCHAR` | 品类英文名称 |
| `category_name_zh` | `VARCHAR` | 品类中文名称 |

## 关联关系

| 关联字段 | 对应主表字段 |
|---|---|
| `orders.customer_id` | `customers.customer_id` |
| `order_items.order_id` | `orders.order_id` |
| `order_items.product_id` | `products.product_id` |
| `order_items.seller_id` | `sellers.seller_id` |
| `order_payments.order_id` | `orders.order_id` |
| `order_reviews.order_id` | `orders.order_id` |
| `products.category_id` | `categories.category_id` |

当前指标和关联依赖上述电商业务模型。字段对应关系用于适配表名和列名；导入后支持的指标见 [指标口径](metrics.md)。
