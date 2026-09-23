"""领域通用称谓。

状态文案与 ``fixtures/domain.json`` 的参考状态保持一致，业务记录只能
通过 :mod:`domain.registry` 的正式接口产生，不直接写字典字面量。
"""

# 性别
SEX_MALE = "MALE"
SEX_FEMALE = "FEMALE"
SEXES = (SEX_MALE, SEX_FEMALE)

# 标准年龄段（有序）；儿童年龄段单列，供发布拆分与最小样本判定
AGE_GROUPS = ("0-4", "5-14", "15-59", "60+")
CHILD_AGE_GROUPS = ("0-4", "5-14")

# 证据种类
KIND_INDICATOR = "indicator"            # 指标定义（如二手烟暴露口径）
KIND_SURVEY = "survey"                  # 人口调查（含抽样信息）
KIND_LITERATURE = "literature"          # 文献结果
KIND_GEO_MAPPING = "geo_mapping"        # 地区映射（来源地区名 -> ISO3）
KIND_RISK_PARAM = "risk_parameter"      # 风险参数（相对危险度 RR）
KIND_MORTALITY = "mortality_base"       # 死亡人数基数

# 证据生命周期状态（参考 fixtures/domain.json）
STATUS_PENDING = "待导入"
STATUS_PENDING_ADJUDICATION = "待裁定"
STATUS_COMPUTABLE = "可计算"
STATUS_PUBLISHED = "已发布"
STATUS_WITHDRAWN = "已失效"
STATUS_SUPERSEDED = "已替代"

# 计算单元状态
CELL_OBSERVED = "observed"              # 直接来自调查观测
CELL_ESTIMATED = "estimated"            # 明示方法的缺失年估算
CELL_VOID = "void"                      # 输入撤回且无替代，结论作废
CELL_CONFLICT = "conflict"              # 输入互为冲突且尚未裁定
CELL_NO_INPUT = "no_input"              # 缺少暴露输入，不产出数字
CELL_SUPPRESSED = "suppressed_small_sample"  # 最小样本披露规则：抑制
