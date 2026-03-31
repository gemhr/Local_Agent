# Diagram Artifacts

该目录提供三种格式，便于在不同工具中使用：

1. `local_agent_flow_rich.svg`
   - 高保真图片流程图（可直接插入 PPT/WPS/Confluence）。
2. `local_agent_architecture.mm`
   - FreeMind `.mm` 思维导图文件（MindMaster 可导入）。
3. `excel_flow_nodes.csv` + `excel_flow_edges.csv`
   - Excel 可编辑的数据驱动流程图输入。

## Excel 生成流程图（数据驱动）

### Step 1: 导入 CSV
- 在 Excel 打开 `excel_flow_nodes.csv` 与 `excel_flow_edges.csv`（两个工作表）。

### Step 2: 在 `nodes` 表新增计算列
假设 `x,y,w,h` 在 D:E:F:G 列，新增：
- `center_x`: `=D2+F2/2`
- `center_y`: `=E2+G2/2`

### Step 3: 在 `edges` 表建立坐标映射
使用 `XLOOKUP`（或 `INDEX/MATCH`）把 from/to 的中心点映射出来：
- `from_x`: `=XLOOKUP(A2,nodes!A:A,nodes!H:H)`
- `from_y`: `=XLOOKUP(A2,nodes!A:A,nodes!I:I)`
- `to_x`: `=XLOOKUP(B2,nodes!A:A,nodes!H:H)`
- `to_y`: `=XLOOKUP(B2,nodes!A:A,nodes!I:I)`

### Step 4: 绘制
- 用 `插入 -> 形状` 画节点矩形（按 nodes 的 x,y,w,h）。
- 用 `插入 -> 连接符` 按 edges 表连接 from/to。
- 标签用 `label` 字段。

> 这种方式的好处：后续节点变更只改 CSV，团队可统一维护。

## MindMaster 导入说明
- 打开 MindMaster -> 导入 -> 选择 `local_agent_architecture.mm`。
- 可一键套主题、导出为 PNG/SVG/PDF。
