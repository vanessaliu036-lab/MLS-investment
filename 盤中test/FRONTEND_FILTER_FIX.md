# 篩選欄前端 render — 正確範例（杜絕「反相」bug）

## Bug 根因回顧

舊版 `passes_filters` 回傳 `{condition_name: bool}`。前端若這樣寫就會反相：

```javascript
// ❌ 錯誤：印出所有 key 名稱，不管 True/False
// aflow=-70 的股票照樣印出 "aflow_positive"，語意完全反了
const labels = Object.keys(row.filters)
  .filter(k => k !== 'all_pass')
  .join(', ');
```

問題：`Object.keys()` 拿到的是**條件名稱**，不是**通過與否**。前端漏了 `if value` 判斷，
就把「這檔被檢查的條件」誤印成「這檔通過的條件」。

## 正解：後端已給現成清單，前端無腦印

新版 `passes_filters` 回傳已經分好的清單，前端不再自己判斷：

```javascript
// row.filter_passed  : ["站上MA20", "象限真攻擊"]   已通過
// row.filter_failed  : ["主動差>0"]                 未通過
// row.filter_display : "✗主動差>0　✓站上MA20　✓象限真攻擊"
// row.pass_filters   : false                        是否全過

// ✅ 方式一：直接貼 display（最省事）
cell.textContent = row.filter_display;

// ✅ 方式二：分色印（推薦，盤中一眼分辨）
row.filter_passed.forEach(label => {
  const chip = document.createElement('span');
  chip.className = 'chip-pass';   // 綠
  chip.textContent = '✓' + label;
  cell.appendChild(chip);
});
row.filter_failed.forEach(label => {
  const chip = document.createElement('span');
  chip.className = 'chip-fail';   // 灰
  chip.textContent = '✗' + label;
  cell.appendChild(chip);
});
```

## 「符合條件」檢視模式

```javascript
// 訂閱池固定不動，只在前端過濾顯示（不 unsubscribe）
const rows = allRows.filter(r => viewMode === 'all' ? true : r.pass_filters);
```

## 驗收檢查（防止 bug 回歸）

任一檔 `aflow < 0`，其篩選欄**必須**看到 `✗主動差>0`（或該標籤出現在 `filter_failed`），
**絕不可**出現 `✓主動差>0`。台勝科(−70)、上銀(−76)即回歸測試案例，
對應 `test_negative_aflow_NOT_in_passed`、`test_upyin_negative`。

## CSS 建議（對齊淺色硬規則）

```css
.chip-pass{ color:#1e8449; border:1px solid #cfe6d8; background:#eef4f0; }
.chip-fail{ color:#6d5f3a; border:1px solid #ddd0b3; background:#efe9dc; }
/* 未過用淺褐灰而非純灰字，符合報告配色硬規則 */
```
