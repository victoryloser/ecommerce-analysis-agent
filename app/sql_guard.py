import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify

from app.source import SCHEMA

# 编译器需要的函数；没有文件、网络、动态 SQL、扩展或任意表达式入口。
FUNCTIONS = {"JSON_MERGE_PATCH", "LIST_COUNT", "ARRAY_DISTINCT", "TRY_CAST", "SUM", "COUNT", "AVG", "COALESCE", "NULLIF", "CAST", "CASE", "IF", "TIME_TO_STR", "RANK", "EXISTS", "AND", "OR", "J_S_O_N_OBJECT", "JSON_EXTRACT", "JSON_FORMAT", "CONCAT_WS"}
EXPLORATION_FUNCTIONS = FUNCTIONS | {"JSON_ARRAY_LENGTH",
    "MIN", "MAX", "ABS", "ROUND", "CEIL", "FLOOR", "POWER", "SQRT", "MOD",
    "ROW_NUMBER", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTILE",
    "DATE_TRUNC", "TIMESTAMP_TRUNC", "EXTRACT", "DATE_DIFF", "DATEDIFF", "TS_OR_DS_ADD", "TS_OR_DS_DIFF",
    "LOWER", "UPPER", "TRIM", "LENGTH", "SUBSTRING", "CONCAT", "CONCAT_WS",
    "STR_TO_TIME", "TRY_CAST", "MEDIAN", "STDDEV_SAMP", "STDDEV_POP", "VAR_SAMP", "VAR_POP",
}


def validate_sql(sql: str, *, schema: dict | None = None, exploration: bool = False) -> None:
    schema = SCHEMA if schema is None else schema
    statements = sqlglot.parse(sql, read="duckdb")
    allowed = (exp.Select, exp.SetOperation) if exploration else (exp.Select,)
    if len(statements) != 1 or not isinstance(statements[0], allowed):
        raise ValueError("只允许单条 SELECT")
    tree = statements[0]
    if any(node.args.get("recursive") for node in tree.find_all(exp.With)):
        raise ValueError("不允许递归查询")
    if any(tree.find_all(exp.DML, exp.DDL, exp.Command, exp.Copy, exp.Attach, exp.Pragma)):
        raise ValueError("查询及子查询中不能包含写入、DDL或管理命令")
    ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier) or table.db or table.catalog:
            raise ValueError("不允许外部表或表函数")
        if table.name not in set(schema) | ctes:
            raise ValueError(f"未声明的表：{table.name}")
    for function in tree.find_all(exp.Func):
        name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name()
        if name not in (EXPLORATION_FUNCTIONS if exploration else FUNCTIONS):
            raise ValueError(f"不允许的函数：{name}")
    if any(tree.find_all(exp.Into)):
        raise ValueError("不允许 SELECT INTO")
    try:
        qualify(tree.copy(), dialect="duckdb", schema=schema, validate_qualify_columns=True,
                quote_identifiers=False, identify=False)
    except OptimizeError as error:
        raise ValueError(f"列或关联作用域不合法：{error}") from error
