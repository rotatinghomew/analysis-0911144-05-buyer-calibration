# 交叠总体边际与极端权重下的采购意向校准

本项目保存业务资料的修订版本，提供资料校验、原始记录查询和现有口径的概览核算。操作人员通过本地 HTTP 接口导入资料，分析客户端按版本读取，避免后来的补录覆盖先前资料。

## 环境与运行

使用 Python 3.11，数值计算依赖固定在 requirements.txt。容器使用固定摘要的基础镜像。准备阶段可安装依赖，准备完成后测试与演示只访问本机；无需数据库服务或业务账号。

```sh
docker build -t buyer_calibration:local .
docker run --rm --network none buyer_calibration:local python -m unittest discover -s tests -v
docker run --rm --network none buyer_calibration:local python -m buyer_calibration demo
```

也可在 Python 3.11 虚拟环境中安装 requirements.txt 后运行相同的测试和模块命令。demo 在临时目录建库，启动真实本地 HTTP 服务，导入资料，再通过客户端读取结果，退出时关闭服务。运行过程没有固定等待或远程请求。

## 常驻资料接口

```sh
python -m buyer_calibration serve --database /tmp/buyer_calibration.sqlite3 --load fixtures/demo.json
```

默认监听 127.0.0.1:8080；--host 和 --port 可调整。服务用于可信本地环境，当前不提供公网认证与授权。数据保存在指定 SQLite 文件，重启后仍可按 revision 取回。API 格式见 docs/interfaces.md，业务数据语义见 docs/data-contract.md。

## 目录与现有能力

`buyer_calibration/domain.py` 负责资料规则和已有概览；`repository.py` 负责不可变资料版本、导入回执与已保存分析；`service.py` 是服务边界；`calibration.py` 是边际校准分析；`client.py` 是独立 HTTP 消费者。fixtures/demo.json 是便于逐项核对的小样本，fixtures/history.json 是同口径的本地合成历史资料。测试覆盖领域规则、版本稳定性、校准数学、结论分级、集团重抽样、分析记录持久化及真实 HTTP 往返。

## 校准分析

校准只在样本实际观察到的（地区、规模、行业）组合上分配总体企业数，边际之间允许交叠，区间约束同时生效；不创建无样本组合，也不能靠巨大权重假装观察过。点估计取离既有设计权重最近的可行解，同时给出约束决定的识别区间、总体企业数范围、企业级与集团级有效样本量、最大权重比。

结论分为四级：

* `calibrated`：在声明设置下通过有效信息量与区间检查；
* `unstable`：可行但估计不稳（有效信息量低、权重膨胀、区间过宽、总体未识别或重抽样常不可行）；
* `extrapolated`：依赖稀薄组合撑起较多总体，只有接受选择偏差假设才能外推；
* `conflict`：约束本身冲突，区分为无样本支撑（`support_gap`）与边际互相冲突（`margin_conflict`，附最小冲突集合）；不产生估计。

`tradeoffs` 记录收紧权重上限与逐项放宽边际的代价及最小可行上限，`choices` 保留每次选择依据，`assumption` 明示选择偏差假设。结论始终是校准与假设下的估计，不是对总体的观测。

```sh
# 对固定版本运行并可选保存为可复核记录
python -m buyer_calibration calibrate --database /tmp/data.sqlite3 --revision 1 --request-key review-1 --settings settings.json

# 通过 HTTP 运行，不保存
curl 'http://127.0.0.1:8080/calibration?revision=1&settings=%7B%22weight_cap%22%3A3%7D'
```

接口细节见 docs/interfaces.md，设置字段与分级口径见 docs/data-contract.md。

现有概览的解释边界跟随输出的 meaning 或 visibility 字段。原始资料和描述性结果不自动构成推断、优化或对外承诺；调用者应保留版本号和摘要。
