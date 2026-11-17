# 数据格式说明

当前版本支持 Excel `.xlsx` 和 UTF-8 / UTF-8 BOM 编码的 CSV `.csv`。每张表的第一行是字段名；Excel 一张工作表对应一张业务表，CSV 一个文件对应一张业务表。

仓库不分发业务数据或历史样例。使用者自行提供有权使用的数据；导入文件、数据库、历史对话和导出结果保存在本机，不随源码上传。自动测试和文档截图使用代码临时构造的虚构记录，生成方式见 [开发文档](development.md#文档截图)。

导入时需要提供以下九张表及其全部字段。源文件和列名可以不同，通过页面中的字段对应关系转换为这些标准字段。多余列不参与导入。

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
