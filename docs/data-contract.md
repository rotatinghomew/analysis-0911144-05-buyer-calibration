# 业务资料口径

每个 respondents 行是一家独立企业，group 是共同经营集团，base_weight 为正的既有设计权重，intention 为已回答的意向金额。margins 的 where 是字段等值条件的合取；空条件表示全体，lower 和 upper 是该条件下总体企业数的允许范围。约束对象是企业权重总和而非金额总和。边际不是互斥分箱，不能直接求和生成联合总体。缺少抽样概率时只能在透明的校准与选择偏差假设下解释，不能宣称已消除自愿回复偏差。

小样本用于手工核对字段和边界；历史样本只扩展观测量，不保证任何推断、优化或方案一定成立。实际分析应允许信息不足或条件冲突。

## 校准分析的设置与结论分级

校准只在样本实际观察到的（地区、规模、行业）组合上分配总体企业数，不创建无样本组合。每项边际的区间同时约束，交叠边际不拆成互斥分箱，也不求和当联合总体。点估计取离既有设计权重最近的可行解；识别区间由约束本身决定，与求解方式无关。

settings 可选字段：margin_ids（使用的边际标识，缺省全部）、targets（[{id, where}] 业务关注人群，缺省为全体与各字段水平）、weight_cap（单元格校准总量不得超过既有设计权重的倍数，缺省不限）、min_cell_respondents（单元格最少回复数，默认 1）、bootstrap_replicates 与 random_seed（集团整群重抽样，默认 200）、以及 ess_threshold、group_ess_threshold、range_width_threshold、bootstrap_width_threshold、weight_inflation_threshold、thin_support_share_threshold、bootstrap_infeasible_threshold、max_corner_margins 等分级阈值。未知字段或越界值返回 400。

diagnosis.status 区分四类：
* conflict：约束本身不可行。codes 为 support_gap（某边际在观测不到的单元格上要求企业数）、margin_conflict（外围最小的冲突边际集合见 conflict_sets）或 no_support。不产生估计。
* unstable：存在可行解，但有效信息量不足、权重膨胀、识别区间或集团重抽样区间过宽、重抽样常不可行，或所选边际未锁定总体规模（unidentified_total）；有上限时总体区间仍会给范围。
* extrapolated：点估计依赖样本稀薄组合撑起较大部分总体，只有接受选择偏差假设才能外推。
* calibrated：在声明设置下通过上述检查。

每个 target 各有 status 与 codes，诊断内的 unstable_targets、extrapolated_targets、conflict_targets 汇总受影响人群。tradeoffs 保留每次放宽的代价与所用选择：cap_scenarios 列出各权重上限的可行性与均值、有效样本量（含最小可行上限）、margin_relaxations 列出逐项放宽边际后的均值变化、margin_endpoint_corners 把非退化边际固定到端点求解；choices 记录单元格、点估计、边际、权重上限、集团相关与选择偏差假设的处理方式。已保存的 POST /analyses 把当时的 revision、data_digest、settings_digest 与完整 result 一起固化。
