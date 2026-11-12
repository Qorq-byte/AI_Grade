# 前端 DOM 契约检查（批次 49）

> **它不是浏览器测试**：不执行 JS、不验证布局、不点按钮。
> 它检查的是"接线"——引用的元素在不在、事件的函数有没有定义、标记里有没有重复 id。

## 为什么做这个

从 B38 起，每个产品批次都写着「前端无浏览器级验证」。环境里没有 Playwright
（venv 未安装、`npx playwright` 也拉不到），硬写"浏览器测试"只会变成假的。
退一步做静态契约检查，能稳定抓住一类**不会报错、只会"点了没反应"**的问题。

## 检查项与当前数字

| 检查 | 数字 |
|---|---|
| 标记里的 id | 114（无重复） |
| JS 模板里定义的 id | 34 |
| `getElementById` / `querySelector('#…')` 引用 | 98（**0 个缺失**） |
| 内联事件处理器 | 71（**0 个未定义**） |
| 关键锚点 | `chatMessages`、`promptTextarea`、`aiModelSelect`、`wfLayout`、`wfFileZone`、`wfProgress` |
| 对比面板接线 | `buildComparisonPanel` 挂进步骤面板；`selectCandidate` / `showCandidateDiff` / `showAllDiffs` 都有入口 |

## 这次查出并清掉的东西

| 位置 | 问题 | 处理 |
|---|---|---|
| `renderWfFiles` / `handleWfFiles` / `removeWfFile` | 取 `#wfFileList`，但该元素早被按类型拆成 `#imgFileList/#pdfFileList/#docFileList`，函数永远 return；`handleWfFiles` 也没有任何入口调用 | 删除三个死函数 |
| `setLayoutMode` 里的 `#wfLayoutLabel` | 元素不存在，`if(lbl)` 永远假，标签从不更新 | 删除该行 |

## 怎么跑

```bash
NO_PROXY='*' .venv/Scripts/python.exe -m pytest tests/test_frontend_contract.py -q
```

变异验证：`python tests/_mutate_frontend_contract.py` —— 故意引用不存在的 id /
改掉事件函数名 / 制造重复 id，三处都必须让检查变红（当前 3/3）。

## 局限（诚实列出）

1. **不执行 JS**：运行时错误、事件绑定顺序、异步渲染都不覆盖。
2. **动态 id 不检查**：`getElementById('wfStep'+i)` 这类拼接出来的 id 抓不到；
   只校验字面量。
3. **模板字符串里的 id 只做宽松匹配**：JS 里 `id="..."` 会算作"已定义"，
   但它是否真的渲染出来，静态看不出来。
4. **不验证样式与布局**：元素存在不等于可见、不等于位置对。
