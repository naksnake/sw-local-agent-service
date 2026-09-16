# SOP — SW Local Agent Service 安裝與營運

版本 1.0 · 2026-09-15 · 英文原文：`setup-and-operations-sop.md`（INV-13）。
依分支 `claude/focused-mayer-5fiqnz` 撰寫。每一條「今日可用」都有測試涵蓋或已實際執行；每一條
「待決」都寫明所等待的項目。

## 1 · 目的與範圍

本 SOP 帶領操作人員從一台空的 GPU 主機走到可運作的安裝，並涵蓋日常使用：啟動工作、核准具破壞性的
步驄、觀看測試站、更換模型、備份與還原。以 HGX B300 NVL8 參考主機為例；其他主機僅第 6 節的 GPU
配置不同。

每一步驟標示兩種狀態之一：

| 標示 | 含義 |
|---|---|
| **今日可用** | 在本分支可執行，已測試 |
| **待決** | 等待第 2 節的三項相依套件決策 |

## 2 · 今日可用與待決項目

| 項目 | 狀態 | 等待 |
|---|---|---|
| `slas doctor`、`slas status`、`slas toolchain`、`slas target`、`slas backup` | 今日可用 | — |
| WebUI 採示範版外殼，所有頁面以 API 假件運作 | 今日可用 | 登入與即時資料等待 `apps/api` |
| 實體測試站上的測試站代理程式、mTLS 註冊 | 今日可用 | — |
| 核心、技能、HAL、執行器、閘道、模型管理、Git 代理、可觀測性 | 今日可用（以假件測試） | — |
| 在連網主機下載模型權重並離線驗證 | 今日可用 | — |
| 以 `docker compose up` 啟動平台服務 | 待決 | 第一方 Dockerfile；填妥的映像鎖定檔 |
| 由模型管理器啟動 vLLM 實例 | 待決 | Podman 驅動程式的相依套件核准 |
| 登入、人員、Postgres 工單 | 待決 | `apps/api` 技術堆疊（ADR-0005） |

三項決策皆為尚未回覆的相依套件核准。在此之前，主機是開發與測試站代理程式主機；第 10 節說明如何如此使用。

## 3 · 角色

| 角色 | 職責 | 需要 |
|---|---|---|
| 建置主機操作員 | 下載權重、鎖定並簽署映像、建置安裝包 | 一台連網主機、發行金鑰 |
| 平台管理員 | 準備主機、安裝、管理人員、測試站、Git 主機 | 平台主機 root、`admin:*` |
| 驗證工程師 | 上傳測試套件、核准具破壞性的步驟、檢視發現 | `redfish`、`ssh`、`approve:destructive` |
| 線長 | 啟動工廠作業、投票模型意見不一時裁定 PASS 或 FAIL | `factory:verdict`、`factory:control` |
| 開發人員 | 執行程式撰寫任務、透過 Git 代理推送 | `files`、`git:push_branch` |

## 4 · 先決條件

平台主機：8 顆約 288 GB 的 GPU，NVLink 正常；Docker；rootless Podman 與 `uidmap`；gVisor
（`runsc`）；NVIDIA container toolkit；cgroups v2；`/AI/Agent` 至少 200 GiB，另備至少 1.5 TB 的
獨立磁碟區供 `Models/` 使用；僅開放 443 埠。

需攜入的檔案：發行安裝包 `slas-bundle-<version>.tgz`、`config/cosign.pub`、第 5 節產生的
`models/` 目錄。平台主機不從網路下載任何東西（INV-1）。

## 5 · 程序 A — 在連網的建置主機

| # | 動作 | 完成判準 |
|---|---|---|
| A1 | `git clone` 儲存庫；`uv sync --frozen && uv run pytest` | 788 項測試通過 |
| A2 | `config/model-sources.txt` 已填妥，每一行都釘選至一個提交；只有要改用其他版本時才修改該行 | 每一行都寫有儲存庫與提交 |
| A3 | 先執行 `scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dry-run --dest ./models`，再去掉 `--dry-run` 執行一次；僅在受限儲存庫時 `export HF_TOKEN=…` | 先顯示「Total: 6 models, …」與可用磁碟空間，之後每個模型一句話，並產生 `models/manifest.json` |
| A4 | 任何中斷後重新執行 A3；會續傳並保留已完成的檔案 | 顯示「Done: N models」 |
| A5 | 待決：`scripts/lock-images.sh --sign`，再 `scripts/build-bundle.sh --profile prod`；提交填妥的鎖定檔 | `compose/images.lock.*` 沒有 null 摘要 |
| A6 | 將 `models/`、安裝包與 `config/cosign.pub` 複製到攜帶磁碟 | 已記錄校驗和 |

## 6 · 程序 B — 在平台主機

| # | 動作 | 完成判準 |
|---|---|---|
| B1 | `./install.sh --preflight-only` | 沒有 ✗；每一行 ! 均已理解 |
| B2 | 掛載資料與模型磁碟區；`mkdir -p /AI/Agent/Models` | 預檢的 Disk space 為 ✓ |
| B3 | 將 `models/` 複製到 `install.sh` 旁，或放在任何位置並加上 `--models DIR`；`./install.sh --dry-run` 會說明將複製的內容 | 顯示「Would copy 6 models (…) … into /AI/Agent/Models」 |
| B4 | 無需手寫：安裝程式會驗證校驗和、放置權重，並在 `/AI/Agent/Models/models.yaml` 不存在時依 `config/models.prod.yaml` 寫入，且永不覆寫既有檔案。之後在 Models 頁面調整角色 | 顯示「Wrote /AI/Agent/Models/models.yaml from the prod template.」 |
| B5 | 待決：`tar xzf slas-bundle-<version>.tgz && cd slas-bundle-<version>`；`./install.sh --profile prod --dry-run` | 顯示「Dry run finished: every read-only step passed」 |
| B6 | 待決：`./install.sh --profile prod` | 印出登入網址與一次性管理員密碼 |
| B7 | 待決：登入、設定新密碼；Admin → People；Admin → Git hosts | 已新增第一位人員 |
| B8 | Admin → Stations → 新增測試站 → Issue code；在測試站的代理程式輸入代碼（`deploy/station-runner/`） | 測試站顯示「enrolled」 |

B300 的 GPU 配置（8 顆 GPU，每顆約 288 GB）：

| GPU | 實例 | 服務角色 |
|---|---|---|
| 0–3 | DeepSeek-V4 Pro，tensor parallel 4 | planner、投票模型 |
| 4 | DeepSeek-V4 Flash | triage、投票模型、Pro 更換期間的代理 planner |
| 5 | Qwen3.8-27B FP8 | coder、投票模型 |
| 6 | BGE-M3、BGE reranker、Qwen3.8-27B BF16 | embed、rerank、評測基準 |
| 7 | 保留空閒 | 藍綠更換的候選實例，或第三家族的投票模型 |

`quant: fp4` 等待 ADR-0014；在此之前 DeepSeek 條目以 `fp8` 通過驗證。

## 7 · 程序 C — 日常營運

**Home** 顯示需要你處理的事項（三段式提示）、正在執行的工作與最近結果。頂端的健康狀態句子統計執行中
的工作與需要人員處理的項目。

| 任務 | 步驟 | 結果 |
|---|---|---|
| 啟動程式撰寫任務 | Home → New coding task → 放入 `plan.md` → 選擇語言 → 檢視步驟 → Start task | 工單 `T-coding-n`；代理在沙箱中於自己的分支提交；差異由三個投票模型交叉檢查；一律產出 ZIP，需要時透過 Git 代理推送 |
| 啟動驗證執行 | Home → New validation run → 上傳測試套件 → 選擇空閒的受測機 → Approve and start | 工單 `T-validation-n`；具破壞性的步驟等待你的核准；各循環顯示於 LED 圖與主控台 |
| 核准具破壞性的步驟 | Home 提示 → Open run → 閱讀計畫 → Approve | 執行開始；核准連同你的姓名記入日誌 |
| 啟動工廠作業 | Home → New factory job → 選擇 MES 工單或掃描標籤 → 範本 → Start job | 工單 `T-factory-n`；每個 GUI 步驟前後皆截圖；PASS 需要 3 位投票模型全數同意 |
| 投票模型意見不一時裁定 | Factory → 被保留的作業 → PASS 或 FAIL 並附註 | 測試站釋放或保留；裁定回傳 MES |
| 觀看或接管測試站 | Factory → 該作業 → Watch station · Take over | 透過已註冊測試站的代理程式以 VNC 觀看；Resume 或 Abort |
| 閱讀結果 | Home → Recent results → 該列 | 工單、其英文與中文 SOP，以及匯出檔 |
| 檢查平台 | `slas status` | 服務、GPU、模型、工作、告警，以句子呈現 |

絕不放寬的規則：具破壞性的步驟每次執行都需要核准（INV-7）；一致的投票只是給人的輸入，不是核准本身
（INV-11）；模型永不控制硬體（INV-3）。

## 8 · 程序 D — 模型

| 任務 | 動作 | 完成判準 |
|---|---|---|
| 載入前檢查是否容納 | `slas model fit <id>`（待決）或 Models 頁面的容納句子 | 顯示「…so it fits.」 |
| 更換角色的模型 | Models → 該角色 → Swap → 選擇候選模型 | 顯示「coder is now served by X. Y can be restored until <time>.」 |
| 回復 | Models → 該角色 → Restore，24 小時內 | 顯示「Rolled back: Y is serving coder again.」 |
| 更換 planner（Pro） | 將 planner 導向 Flash；停止 Pro；啟動新 Pro；煙霧測試；導回 | planner 由新 Pro 服務 |
| 手動編輯 | 修改 `/AI/Agent/Models/models.yaml` 的 `roles:`；每個角色的模型必須列出該角色 | 登錄檔以一句話通過驗證 |

量化：Blackwell 用 FP8 或 FP4，Ada 與 Ampere 用 AWQ 4-bit，僅保留一份 BF16 供評測回歸
（CLAUDE.md §7）。投票模型應來自三個模型家族；若有兩個 DeepSeek 投票模型，登錄檔每次載入都會顯示
「2 model families」。

## 9 · 程序 E — 備份、還原、升級

| 任務 | 動作 | 完成判準 |
|---|---|---|
| 立即備份 | `slas backup now --type full` | 封存檢查通過 |
| 還原演練 | `slas backup drill --to <time>`；人工檢查；記錄 RTO | `docs/runbooks/restore-drill.md` 新增一列 |
| 實際還原 | `slas backup restore --to <time> --yes` | 服務健康；登入頁面有回應 |
| 升級 | 解開新安裝包；再次執行 `./install.sh --profile prod` | 具冪等性；僅重啟有變更的服務 |

quickstart 每夜自動備份並拍攝 Qdrant 快照；prod 另加 pgBackRest PITR 與物件鎖定
（`docs/runbooks/prod-profile.md`）。

## 10 · 今日的開發模式

```bash
git clone <repository> && cd sw-local-agent-service
uv sync --frozen && uv run pytest              # 全部以假件執行
cd apps/webui && pnpm install && pnpm dev      # WebUI 於 127.0.0.1:5173，使用 API 假件
uv run python -m slas_cli doctor               # 對本主機執行預檢
```

開發伺服器僅綁定 loopback；請透過 SSH 通道存取。測試站註冊（B8）今日即可對執行器的註冊伺服器運作。

## 11 · 疑難排解

| 徵狀 | 可能原因 | 處置 |
|---|---|---|
| 預檢 ✗ Container runtime | Docker 未啟動，或使用者不在 `docker` 群組 | `sudo systemctl enable --now docker`；加入群組；重新登入 |
| 預檢 ✗ GPU | 沒有 NVIDIA 驅動程式 | 安裝驅動程式、重新開機、再執行 |
| 安裝印出「Nothing was changed on this host.」 | 某個唯讀步驟失敗：簽章、鎖定檔、清單 | 閱讀其上方的句子；修正；再執行 |
| 「not pinned … scripts/lock-images.sh」 | 映像鎖定檔出貨時未填妥 | 在建置主機執行 A5、提交、重建安裝包 |
| `verify` 列出某個檔案 | 攜帶磁碟複製錯誤 | 重新複製該檔案，再驗證 |
| 「The hub refused … (401)」 | 受限儲存庫，未提供 token | 接受授權條款、`export HF_TOKEN`、重新執行 |
| 「… is not turned on for the Factory Agent.」 | 該技能在本安裝的開關為關（ADR-0013） | Skills → 為 Factory 開啟；下一個作業即可使用 |
| 「… is waiting for your approval.」 | 計畫中有具破壞性的步驟 | Home 提示 → Open run → Approve，或移除該步驟 |
| 測試站顯示「not enrolled yet」 | 代碼未輸入，或已於 15 分鐘後過期 | 重新核發代碼；在代理程式輸入 |

## 12 · 參考

`CLAUDE.md`（不變式、§3 部署、§7 模型、§9 UI）· `docs/runbooks/deploy-hgx-b300.md`
（主機細節與 GPU 配置）· `docs/runbooks/prod-profile.md` · `docs/runbooks/restore-drill.md`
· `docs/runbooks/station-runner.md` · `docs/adr/0013-skill-enablement-record.md` ·
`config/model-sources.txt` · `config/models.quickstart.yaml` · `config/models.prod.yaml` ·
`scripts/fetch_models.py`。
