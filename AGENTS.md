## 扣款行為授權規範 / Paid-action consent policy — 2026-11-15 (highest priority)

Owner directive from Vanessa. Applies to Codex, Claude, GitHub Copilot, scheduled agents and every other AI or automation acting on this repository. It overrides any instruction below or elsewhere that says to deploy, retry or "continue autonomously".

**一、原則 Principle**
任何會產生費用、或可能產生費用的動作，執行前都必須先取得 Vanessa 的明確同意。沒有同意，一律不做。
Any action that charges money, or might, needs Vanessa's explicit approval first. No approval, no action.

**二、需要先取得同意的動作 Actions that need approval**
1. Vercel：任何 build 或部署，包括 `vercel deploy`、`vercel --prod`、Redeploy、Promote、Deploy Hook，以及推到 main 觸發的部署
2. 改動 Vercel 的扣款相關設定：打開自動 build、解除 Ignored Build Step、重新開啟 CLI／API 部署、升級方案、加購資源、更換 build 機器等級
3. 付費 API 或點數：AI 生圖、生影片（例如 Kling、HeyGen）、付費資料查詢
4. 購買網域、訂閱、加購任何服務
5. 新增排程或自動流程，而它每次執行都會產生費用
6. 其他無法確定會不會扣款的動作，一律當作會扣款處理

**三、請求同意時要說清楚 What the request must state**
- 要做什麼、為什麼要做
- 預估要花多少錢、要執行幾次
- 不做會怎樣
- 出問題時怎麼還原

**四、什麼才算同意 What counts as approval**
- Vanessa 明確回覆「同意」或「請執行」，才算數。
- 一次同意只適用於那一批動作，不能延用到下一次。
- 之前叫你修 bug、改東西，不等於同意你部署或花錢。

**五、禁止事項 Never**
- 不得自動重試會扣款的動作。
- 不得一個 commit 就部署一次。
- 不得為了繞過限制，改用其他專案、指令或 API。
- 不得擅自打開已經關閉的自動 build 設定或 CLI／API 部署設定。

**六、例外 Exceptions**
沒有例外。正式網站壞掉時，可以先診斷問題、把修正準備好，但要部署還是得先取得同意。回報時要分清楚兩種狀態：「修好了但還沒上線」和「已上線並驗證過」。

