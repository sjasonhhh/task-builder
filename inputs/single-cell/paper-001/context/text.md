# 单细胞表达计数：输入格式示例

本目录使用手工构造的数据展示输入格式，不对应真实论文或真实实验。

## 示例数据

`data/counts.csv` 包含 6 个细胞、4 个基因的非负整数表达计数。
`cell_id` 是细胞编号，其余列是基因。每行对应一个细胞。

## 示例分析

`code/analyze.py` 逐行计算基因表达计数之和，输出两列：
`cell_id` 和 `total_counts`。
`context/tables/summary.csv` 保存这份合成数据的汇总信息。

## 替换为真实材料

此文件放论文正文，`links.json` 放来源链接及上下文，
`figures/` 和 `tables/` 放论文图表，`data/` 放实验数据，`code/` 放论文代码。
