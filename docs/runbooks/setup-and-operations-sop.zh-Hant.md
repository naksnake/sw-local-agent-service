# SOP — SW Local Agent Service 安裝與營運

版本 1.3 · 2026-09-17 · 英文原文：`setup-and-operations-sop.md`（INV-13）。
1.2 新增第 7 節：第二輪安裝（ADR-0015）之後有哪些服務在執行——每個服務各自的 HTTP 介面、由模型管理器
啟動的 vLLM 實例、容器執行環境 socket 的選擇、沙箱映像，以及第一個程式撰寫任務。
依分支 `claude/vigilant-gauss-tsqua0` 撰寫。每一條「今日可用」都有測試涵蓋或已實際執行；每一條
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
| 在連網的 quickstart 主機上，從原始碼檢出以 `docker compose up` 啟動平台服務（`./install.sh --build`，ADR-0014） | 今日可用 | prod 用的簽署安裝包仍待發行主機 |
| 登入、人員、設定、Postgres 工單（`apps/api`，ADR-0005） | 今日可用 | — |
| 每個服務各自的 HTTP 介面；代理可從精靈啟動（ADR-0015，第二輪） | 今日可用，隨各服務的切片陸續到位 | 契約為 `docs/api-contract-round-2.md`；第 7 節說明可預期的狀態 |
| 由模型管理器透過容器執行環境 socket（Docker 或 Podman）啟動 vLLM 實例 | 今日可用（第二輪） | 釘選的 `vllm/vllm-openai` 映像由 `./install.sh --build` 拉取 |

連網的 quickstart 主機可從原始碼啟動整套平台（`./install.sh --build --fetch-models`，ADR-0014 與 ADR-0015）；第 7 節逐一說明安裝後有哪些服務在執行。在簽署安裝包出現之前，離線主機是開發與測試站代理程式主機；第 11 節說明如何如此使用。

## 3 · 角色

| 角色 | 職責 | 需要 |
|---|---|---|
| 建置主機操作員 | 下載權重、鎖定並簽署映像、建置安裝包 | 一台連網主機、發行金鑰 |
| 平台管理員 | 準備主機、安裝、管理人員、測試站、Git 主機 | 平台主機 root、`admin:*` |
| 驗證工程師 | 上傳測試套件、核准具破壞性的步驟、檢視發現 | `redfish`、`ssh`、`approve:destructive` |
| 線長 | 啟動工廠作業、投票模型意見不一時裁定 PASS 或 FAIL | `factory:verdict`、`factory:control` |
| 開發人員 | 執行程式撰寫任務、透過 Git 代理推送 | `files`、`git:push_branch` |

## 4 · 先決條件

平台主機：8 顆約 288 GB 的 GPU，NVLink 正常；Docker（或已啟用 socket 的 rootless Podman 與
`uidmap`）——預檢會說明是哪個引擎提供容器執行環境 socket、gVisor（`runsc`）是否已向該引擎註冊、
NVIDIA runtime 是否有回應；NVIDIA container toolkit；cgroups v2；`/AI/Agent` 至少 200 GiB，另備至少
1.5 TB 的獨立磁碟區供 `Models/` 使用；僅開放 443 埠。

需攜入的檔案：發行安裝包 `slas-bundle-<version>.tgz`、`config/cosign.pub`、第 5 節產生的
`models/` 目錄。平台主機不從網路下載任何東西（INV-1）。

## 5 · 程序 A — 在連網的建置主機

| # | 動作 | 完成判準 |
|---|---|---|
| A1 | `git clone` 儲存庫；`uv sync --frozen && uv run pytest` | 所有測試通過 |
| A2 | `config/model-sources.txt` 已填妥，每一行都釘選至一個提交；只有要改用其他版本時才修改該行 | 每一行都寫有儲存庫與提交 |
| A3 | 先執行 `scripts/fetch_models.py fetch --sources config/model-sources.txt --profile prod --dry-run --dest ./models`，再去掉 `--dry-run` 執行一次；僅在受限儲存庫時 `export HF_TOKEN=…` | 先顯示「Total: 7 models, …」與可用磁碟空間，之後每個模型一句話，並產生 `models/manifest.json` |
| A4 | 任何中斷後重新執行 A3；會續傳被切斷的檔案並保留已完成的檔案。先前已完成的模型會依 `manifest.json` 與校驗檔辨識，不再向 hub 列舉也不再重算雜湊 | 顯示「Done: N models」，或「already complete … nothing to download」 |
| A5 | 待決：`scripts/lock-images.sh --sign`，再 `scripts/build-bundle.sh --profile prod`；提交填妥的鎖定檔 | `compose/images.lock.*` 沒有 null 摘要 |
| A6 | 將 `models/`、安裝包與 `config/cosign.pub` 複製到攜帶磁碟 | 已記錄校驗和 |

## 6 · 程序 B — 在平台主機

| # | 動作 | 完成判準 |
|---|---|---|
| B1 | `./install.sh --preflight-only` | 沒有 ✗；每一行 ! 均已理解 |
| B2 | 掛載資料與模型磁碟區；`mkdir -p /AI/Agent/Models` | 預檢的 Disk space 為 ✓ |
| B3 | 將 `models/` 複製到 `install.sh` 旁，或放在任何位置並加上 `--models DIR`；`./install.sh --profile prod --models-only --dry-run` 會說明將複製的內容 | 顯示「Would copy 7 models (…) … into /AI/Agent/Models」 |
| B4 | `./install.sh --profile prod --models-only`：驗證校驗和、放置權重，並在 `/AI/Agent/Models/models.yaml` 不存在時依 `config/models.prod.yaml` 寫入，且永不覆寫既有檔案。之後在 Models 頁面調整角色。在安裝包存在之前即可執行 | 顯示「Placed 7 models under /AI/Agent/Models」與「Wrote /AI/Agent/Models/models.yaml from the prod template.」 |
| B5 | 待決：`tar xzf slas-bundle-<version>.tgz && cd slas-bundle-<version>`；`./install.sh --profile prod --dry-run` | 顯示「Dry run finished: every read-only step passed」 |
| B6 | 待決：`./install.sh --profile prod` | 印出登入網址與一次性管理員密碼 |
| B7 | 待決：登入、設定新密碼；Admin → People；Admin → Git hosts | 已新增第一位人員 |
| B8 | Admin → Stations → 新增測試站 → Issue code；在測試站的代理程式輸入代碼（`deploy/station-runner/`） | 測試站顯示「enrolled」 |

B300 的 GPU 配置（8 顆 GPU，每顆約 288 GB）：

| GPU | 實例 | 服務角色 |
|---|---|---|
| 0–3 | DeepSeek-V4 Pro，tensor parallel 4 | planner |
| 4 | DeepSeek-V4 Flash | triage；Pro 更換期間的代理 planner |
| 5 | Qwen3.8-27B FP8 ×2、BGE-M3、BGE reranker、Qwen3.8-27B BF16 | coder、投票模型、embed、rerank、評測基準 |
| 6 | DeepSeek-V4 Flash（第二個實例） | 投票模型 |
| 7 | MiniMax-M2.7 | 投票模型，第三個模型家族 |

`quant: fp4` 等待 ADR；在此之前 DeepSeek 條目以 `fp8` 通過驗證。模型管理器為每個角色與每個投票模型各啟動一個實例（CLAUDE.md §15，決議 14）。

## 7 · 第二輪 — `./install.sh --build --fetch-models` 之後有哪些服務在執行

連網的 quickstart 安裝（ADR-0014、ADR-0015；`docs/api-contract-round-2.md`）。預檢與唯讀檢查之後，
`./install.sh --build` 依序執行：

| # | 步驟 | 你會看到 |
|---|---|---|
| R1 | 依釘選的標籤拉取每個第三方映像——vLLM 映像 `vllm/vllm-openai:v0.29.0-x86_64-cu129` 依鎖定檔記錄的摘要拉取——並重新標記為 `local/…`；從 `images/<name>/Dockerfile` 建置每個第一方映像 | 每個映像一句「Pulled …」或「Built …」；填妥的鎖定檔位於 `/AI/Agent/images.lock.json` |
| R2 | 向沙箱管理器索取映像清單（`python -m slas_sandbox_manager.images list`），以儲存庫根目錄為上下文建置每個 `images/sandbox-<language>/Dockerfile`，將映像 ID 記錄於 `/AI/Agent/sandbox-images.lock.json`，並寫入 `/AI/Agent/Toolchains/manifest.json`（這些映像所含的語言與版本） | 「Built local/slas/sandbox-python:…」、「Wrote the toolchain manifest to …」 |
| R3 | 選擇容器執行環境 socket：Docker 的 `/var/run/docker.sock` 存在時用它（映像在 Docker 的映像庫裡），否則用 Podman 的 `/run/podman/podman.sock`，寫入 `.env` 的 `SLAS_RUNTIME_SOCKET` | 「Docker's socket /var/run/docker.sock serves the container runtime, so SLAS_RUNTIME_SOCKET=/var/run/docker.sock in .env points model-manager and sandbox-manager at it (the images install.sh builds and loads live in Docker's store); nothing else sees it (INV-4).」 |
| R4 | 寫入 `.env` 與密鑰檔；以你的使用者身分建立各服務綁定掛載的資料目錄：`Coding`、`Toolchains`、`.git-broker`、`Tickets`、`Skills/library`、`SOP`、`Validation`、`Factory/{Templates,mes/inbox,ca}`、`Models`、`Knowledge`、`Backups/stations`、`qdrant`、`tls` | 「Created N data directories under /AI/Agent as uid …」 |
| R5 | 放置權重、`docker compose up -d --pull never`、等待健康檢查、印出登入網址 | 「SW Local Agent Service is up.」 |

**啟動哪些代理程式（ADR-0017）。** 預設只啟動 **Coding Agent**：`.env` 中 `SLAS_AGENTS=coding`，不建立
`validation-executor`、`factory-executor`、`vector-db` 與 `local-search-api` 容器，WebUI 顯示 Coding、Runs、Models、Skills 與 Admin。
Validation、Factory 與知識庫（Qdrant、本地搜尋）已建置並測試但保持關閉；`./install.sh --build --agents coding,validation,factory,knowledge`
（或 `SLAS_AGENTS`）會建置它們的映像、啟動其 compose profile 並加入其頁面。以不同的清單再執行一次
即可更改選擇；安裝的其他部分不變。

**服務。** 每個服務都是一個容器，在平台內部以 8000 埠提供 HTTP：`api`（唯一位於邊緣後方者）、
`agent-core-orchestrator`（核心與三個代理）、`llm-gateway`、`model-manager`、`sandbox-manager`、
`git-broker`、`validation-executor`、`factory-executor`；`screen-worker` 與 `local-search-api` 在其輪次
到來前只回應健康檢查。服務之間透過 compose 設定的 `SLAS_*_URL` 變數（`http://<service>:8000`）在內部的
`slas-backend` 網路上互相尋找。`slas status` 與 `docker compose -p slas ps` 列出所有服務；
`docker compose -p slas logs <service>` 讀取其中一個的日誌。

**vLLM 實例出現的位置。** 它們不是 compose 服務。模型管理器讀取 `/AI/Agent/Models/models.yaml`，為每個
角色與每個投票模型透過容器執行環境 socket 以 `SLAS_VLLM_IMAGE` 建立一個容器：`vllm-coder`、`vllm-planner`、
`vllm-triage`、`vllm-embed`、`vllm-rerank`，以及每個投票模型一個 `vllm-voter-<model id>`，全部位於
`slas_slas-inference` 網路（內部網路，無對外連線），以唯讀方式掛載 `/AI/Agent/Models`，並配置放置演算法
指定的 GPU（`SLAS_GPU_VRAM_GIB`，預設每顆 GPU 270 GiB）。`docker ps --filter label=slas.kind=vllm`
列出它們；Models 頁面與模型管理器的 `GET /v1/status` 以一句話說明每個實例的狀態；容納不下的實例會被
回報，絕不啟動。Prometheus 以這些名稱抓取指標。

**Docker socket。** 在有 Docker 的主機上，安裝程式會寫入 `SLAS_RUNTIME_SOCKET=/var/run/docker.sock`
並說明：整個堆疊以 `docker compose` 執行，`./install.sh --build` 建置或 `docker load` 載入的映像都在 Docker
的映像庫裡，若在這種主機上向 Podman 的 socket 要求沙箱或 vLLM 容器，會以「image not known」失敗。rootless
Podman 的 socket 仍是 compose 的預設值，服務沒有 Docker 的主機。只有 `model-manager` 與 `sandbox-manager`
掛載該 socket，執行模型撰寫或技能撰寫步驱的服務絕不掛載（INV-4）。若要使用其他路徑（rootless Docker、
`/run/user/<uid>/podman/` 下的 rootless Podman socket），請在安裝前自行設定 `SLAS_RUNTIME_SOCKET`；
`.env` 中既有的值會被保留。預檢會說明是哪個引擎在該 socket 上回應：「Docker Engine 29.0.1 serves the
container-runtime socket /var/run/docker.sock …」或「Podman 4.9.3 serves …」。

**沙箱映像。** 每種語言一個映像（`local/slas/sandbox-<language>:<version>`），於 R2 由釘選的上游工具鏈
映像或套件建置，內含 `git` 與 `slas-check`，沒有憑證輔助程式也沒有網路（INV-14）。沙箱管理器透過容器
執行環境 socket 依標籤啟動它們；引擎已註冊 gVisor（`runsc`）時使用 gVisor，否則使用強化的 `runc`——
預檢與沙箱管理器的健康檢查都會說明是哪一種。`slas toolchain list` 顯示 R2 寫入的清單。

**第一個程式撰寫任務。**

| # | 動作 | 結果 |
|---|---|---|
| C1 | 登入；Home → New coding task；放入或貼上 `plan.md`；為任務命名 | 從計畫偵測語言（`/api/v1/coding/languages/detect`） |
| C2 | Setup：保留或修改語言；版本全部留空 | 句子會依 R2 的清單說明每種語言最新的隨附版本 |
| C3 | 檢視建議的步驟；**Start task** | 工單 `T-coding-n`；沙箱管理器準備 `/AI/Agent/Coding/<you>/Projects/<slug>`（以你的身分 `git init`），並以解析出的映像開啟沙箱 |
| C4 | 觀看檢查清單與動態 | 第一行動態寫出工具鏈；每個步驟在沙箱內執行 `slas-check …`；代理在自己的分支提交 |
| C5 | 閱讀結果 | 英文與中文的程式碼導覽、`Artifacts/` 下的 ZIP，以及 Git 面板：提交、歷史，並在 Settings → Git remotes 儲存遠端後可透過代理「Push to <remote>」 |

若任務停在「no instance serves the role coder yet」，表示模型管理器尚未啟動完成 `vllm-coder`：Models
頁面會顯示它正在啟動；若永遠容納不下，則顯示容納句子。

## 8 · 程序 C — 日常營運

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

## 9 · 程序 D — 模型

| 任務 | 動作 | 完成判準 |
|---|---|---|
| 載入前檢查是否容納 | `slas model fit <id>`（待決）或 Models 頁面的容納句子 | 顯示「…so it fits.」 |
| 更換角色的模型 | Models → 該角色 → Swap → 選擇候選模型 | 顯示「coder is now served by X. Y can be restored until <time>.」 |
| 回復 | Models → 該角色 → Restore，24 小時內 | 顯示「Rolled back: Y is serving coder again.」 |
| 更換 planner（Pro） | 將 planner 導向 Flash；停止 Pro；啟動新 Pro；煙霧測試；導回 | planner 由新 Pro 服務 |
| 手動編輯 | 修改 `/AI/Agent/Models/models.yaml` 的 `roles:`；每個角色的模型必須列出該角色 | 登錄檔以一句話通過驗證 |

量化：Blackwell 用 FP8 或 FP4，Ada 與 Ampere 用 AWQ 4-bit，僅保留一份 BF16 供評測回歸
（CLAUDE.md §7）。投票模型應來自三個模型家族；prod 隨附 DeepSeek、Qwen 與 MiniMax，quickstart 為兩個家族
（CLAUDE.md §15，決議 12）。

## 10 · 程序 E — 備份、還原、升級

| 任務 | 動作 | 完成判準 |
|---|---|---|
| 立即備份 | `slas backup now --type full` | 封存檢查通過 |
| 還原演練 | `slas backup drill --to <time>`；人工檢查；記錄 RTO | `docs/runbooks/restore-drill.md` 新增一列 |
| 實際還原 | `slas backup restore --to <time> --yes` | 服務健康；登入頁面有回應 |
| 升級 | 解開新安裝包；再次執行 `./install.sh --profile prod` | 具冪等性；僅重啟有變更的服務 |

quickstart 每夜自動備份並拍攝 Qdrant 快照；prod 另加 pgBackRest PITR 與物件鎖定
（`docs/runbooks/prod-profile.md`）。

## 11 · 今日的開發模式

```bash
git clone <repository> && cd sw-local-agent-service
uv sync --frozen && uv run pytest              # 全部以假件執行
cd apps/webui && pnpm install && pnpm dev      # WebUI 於 127.0.0.1:5173，使用 API 假件
uv run python -m slas_cli doctor               # 對本主機執行預檢
```

開發伺服器僅綁定 loopback；請透過 SSH 通道存取。測試站註冊（B8）今日即可對執行器的註冊伺服器運作。

## 12 · 疑難排解

| 徵狀 | 可能原因 | 處置 |
|---|---|---|
| 預檢 ✗ Container runtime | Docker 未啟動，或使用者不在 `docker` 群組 | `sudo systemctl enable --now docker`；加入群組；重新登入 |
| 預檢 ✗ GPU | 沒有 NVIDIA 驅動程式 | 安裝驅動程式、重新開機、再執行 |
| 瀏覽器打不開 `https://<ip>`（TLS 錯誤、連線重設） | edge 只回應 `SLAS_TLS_NAMES` 列出的名稱；2026-09-18 之前的安裝只列了主機名稱 | `git pull && ./install.sh`：登入網址改為主機在預設路由上的位址，憑證涵蓋主機的每個位址 |
| 主機的 DHCP 位址變了，舊網址不再回應 | `SLAS_PUBLIC_HOST` 與憑證記的是舊位址 | 再執行一次 `./install.sh`：它會更新位址（除非 `SLAS_PUBLIC_HOST_PINNED=yes`）並重建 edge；替主機設定固定的 DHCP 保留位址可免於重複 |
| 安裝印出「Nothing was changed on this host.」 | 某個唯讀步驟失敗：簽章、鎖定檔、清單 | 閱讀其上方的句子；修正；再執行 |
| 「not pinned … scripts/lock-images.sh」 | 映像鎖定檔出貨時未填妥 | 在建置主機執行 A5、提交、重建安裝包 |
| `verify` 列出某個檔案 | 攜帶磁碟複製錯誤 | 重新複製該檔案，再驗證 |
| 「The hub refused … (401)」 | 受限儲存庫，未提供 token | 接受授權條款、`export HF_TOKEN`、重新執行 |
| 「… is not turned on for the Factory Agent.」 | 該技能在本安裝的開關為關（ADR-0013） | Skills → 為 Factory 開啟；下一個作業即可使用 |
| 「… is waiting for your approval.」 | 計畫中有具破壞性的步驟 | Home 提示 → Open run → Approve，或移除該步驟 |
| 測試站顯示「not enrolled yet」 | 代碼未輸入，或已於 15 分鐘後過期 | 重新核發代碼；在代理程式輸入 |
| 預檢 ✗ Runtime socket：「No container-runtime socket was found at …」 | Podman 的 socket 與 Docker 的 socket 都不存在 | `systemctl --user enable --now podman.socket`，或安裝 Docker；其他路徑請設定 `SLAS_RUNTIME_SOCKET` |
| 預檢 ! gVisor runtime：「runsc is not registered …」 | 未安裝 gVisor，或未向引擎註冊 | `sudo runsc install && sudo systemctl restart docker`；quickstart 以強化的 runc 繼續 |
| 預檢 ✗ NVIDIA runtime | toolkit 已安裝但未為該引擎設定 | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| 「The sandbox image list could not be read …」 | 本檢出的沙箱管理器尚未提供 `images list` | 更新檢出；`uv sync --frozen`；再執行 `./install.sh --build` |
| `model-manager` 或 `sandbox-manager` 不健康：「runtime: down」 | `SLAS_RUNTIME_SOCKET` 指向的 socket 不是引擎提供的，或引擎已停止 | 檢查 `.env`、`ls -l` 該 socket、重啟引擎、`docker compose -p slas up -d` |
| 程式撰寫任務：「no instance serves the role coder yet」 | `vllm-coder` 仍在啟動，或容納不下 | Models 頁面：等待「healthy」，或為該角色選擇較小的版本 |

## 13 · 參考

`CLAUDE.md`（不變式、§3 部署、§7 模型、§9 UI）· `docs/api-contract-round-2.md`（第二輪服務契約）·
`docs/adr/0014-build-from-source-on-a-connected-host.md` ·
`docs/adr/0015-service-http-surfaces-and-runtime-socket-driver.md` ·
`docs/runbooks/deploy-hgx-b300.md`（主機細節與 GPU 配置）· `docs/runbooks/prod-profile.md` ·
`docs/runbooks/restore-drill.md` · `docs/runbooks/station-runner.md` ·
`docs/adr/0013-skill-enablement-record.md` · `config/model-sources.txt` ·
`config/models.quickstart.yaml` · `config/models.prod.yaml` · `scripts/fetch_models.py`。
