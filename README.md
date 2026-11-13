# 企微私聊分层

在 Mac 上读取指定的企微私聊，默认用本机 Ollama 提取事实和分层，也可选用模型 API，再把结构化结果同步到飞书测试表。也可以导入合成样本。见 [技术方案](docs/technical-design-wecom-segmentation.md)。

## 日常使用

双击项目中的「打开企微分层工作台.command」。首次会使用 Xcode Command Line Tools 编译本地 macOS 应用，完成后自动打开工作台。生成的 `.local/企微分层工作台.app` 也可直接打开，应用依赖当前项目路径和 `.local/venv`。在「系统设置 → 隐私与安全性 → 辅助功能」添加并启用此应用，授予企微窗口读取权限。如果工作台重新构建后已有授权开关仍打开却提示未授权，请移除旧条目、重新添加当前项目的应用，再退出并重新打开工作台。

项目位于桌面时，macOS 还可能单独询问是否允许访问桌面文件夹。若工作台连接本地工作区超时，到「系统设置 → 隐私与安全性 → 文件与文件夹」检查工作台的桌面文件夹权限，再点「重新连接」。

1. 在左侧点击「＋」添加企微会话，填写聊天顶部显示的完整名称。新会话默认选中「企微获客」，也可选「同事测试」。输入已添加的完整名称时，可点「打开已有会话」，继续使用原用途、采集进度和飞书绑定。若两位联系人显示同名，请先在企微中用备注区分。
2. 在企微打开该会话并滚到最新消息。回到工作台点击「采集并分层」，完成后查看阶段、有效线索判定、证据摘要和待核实项。
3. 检查分层预览，点击「同步飞书」并确认。同步结果会显示新增、更新或已是最新等状态。

「证据摘要」引用支持分层的客户短句，例如「“我现在上高二”」「“函数图像跟不上”」，每条单独一行。含常见联系方式或个人标识的片段会跳过。已有会话可点击「重新本地分层」生成新摘要，再同步飞书。

「选科组合」仅记录物理、化学、生物、政治、历史、地理中已确认的三门，信息不足时显示「待确认」。对旧分层可点击「重新本地分层」，用已有消息更新结果。工作台右上角的「查看飞书表格」会在默认浏览器打开已配置的表格。模型 API 尚未配置时，可直接点击会话操作里的「配置模型 API」。

咨询人默认标为「学生」，客户明确表明是家长时标为「家长」。「跟进状态」默认「推进中」。双方已确认「明晚 7 点」等相对时间，但无法核实日期时，标为「预约时间待确认」，试听场次留空，跨天后仍保留此状态。按北京时间，未约线索跨天会转为「暂缓」，工作台标记「待更新」，点击「同步飞书」后写入原行。明确暂缓或取消预约也标为「暂缓」，明确拒绝标为「拒绝」。企微窗口使用消息采集时间判断是否跨天。

分层预览会突出显示「初中生」和「家长」。初中生保留具体年级，仍判为「明确不符合」。家长身份独立标注，高中生家长的咨询仍按高中线索规则判断。同步时，已有的飞书测试表会补齐「人群标签」多选列。历史会话可用「重新本地分层」补上标签，再同步。

会话列表、数据库和配置保存在项目的 `.local/` 目录。持续使用同一个会话条目可保留采集进度和飞书记录。若采集成功、分层中途失败，点击「重试本地分层」可继续处理已采集的消息。飞书写入失败时可重跑同步。更新配置后，可用左侧底部的刷新按钮加载。首次使用时，按下文安装 Python 依赖、Ollama 和 `qwen3:4b`，并配置飞书测试表。使用单独下载的 Ollama 可执行文件时，可将其绝对路径写入 `.local/ollama-bin`。

终端菜单入口为「启动企微分层.command」。使用该入口采集时，在 macOS 辅助功能中授权运行脚本的终端。

## 可选模型 API

在工作台左侧点击「添加模型 API」或「配置模型 API」，填写 HTTPS 基础地址、模型名和密钥。保存到本机 `.local/model-api.env`，文件权限为 `600`。使用终端时也可直接填写以下三项。模型服务需支持 Chat Completions 和 JSON 输出，基础地址末尾省略 `/chat/completions`。

```dotenv
MODEL_API_BASE_URL=https://api.deepseek.com
MODEL_API_MODEL=服务商提供的模型名
MODEL_API_KEY=你的 API 密钥
```

在工作台选择会话，打开「模型 API」菜单。可采集并处理待分层的会话，也可重新处理已采集的消息。工作台会先显示发送地址和模型，并要求确认。每次请求包含一条客户聊天文字和最多六条近期上下文。请仅在允许把该会话内容发送给服务商时选择此方式。处理完成后可预览，通过「同步飞书」单独写入测试表。

命令行可使用 `classify --api`，加 `--reprocess` 可重新处理已有分层的会话。重新分层成功时递增同步版本，保留线索 ID。重新分层失败时可继续查看上次成功的结果。

## 在 Mac 上跑样本

安装 Python 3.13 和 [Ollama](https://docs.ollama.com/macos)。启动 Ollama 本地服务并下载 `qwen3:4b`，可通过 [本地模式设置](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features) 禁用云功能。程序固定连接 `127.0.0.1:11434`。

```sh
ollama pull qwen3:4b
python3.13 -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m wecom_segmentation import --file samples/conversations.jsonl
.local/venv/bin/python -m wecom_segmentation classify
.local/venv/bin/python -m wecom_segmentation sync --dry-run
.local/venv/bin/python -m wecom_segmentation status
```

`qwen3:4b` 下载约需 2.5 GB。如果 `pip` 报证书验证错误，可在安装命令里加上 `--cert /etc/ssl/cert.pem` 使用 macOS 系统证书。

默认数据库位于 `.local/segmentation.sqlite3`，可在子命令前用 `--db` 指定另一份数据库。JSONL 每行是一条消息，时间带时区。撤回事件使用 `kind: "recall"` 和 `target_message_id`。数据库保存原文件位置和校验值。移动或改写样本文件后，请保留原始消息并重新导入，供后续重放。

模型逐条提取客户消息中的事实，规则用原文核对预约、报名和付款等容易混淆的状态。预览输出为 JSON，日期字段使用毫秒时间戳。分层待处理或失败的行暂时隐藏，重新运行 `classify` 可重试。重复导入相同消息时保留原有线索 ID 和分层结果。

## 在 Mac 上读取指定企微测试私聊

在企微 5.0.13 上，从辅助功能控件读取前台聊天的文字气泡。先用已安装的 Xcode Command Line Tools 编译，并在「系统设置 → 隐私与安全性 → 辅助功能」授权运行命令的终端。打开与同事的测试私聊，滚到最新位置。执行带延时的采集命令后，在五秒内切回企微。首轮至少需要两条完整可见的文字消息。

```sh
mkdir -p .local
xcrun swiftc wecom_segmentation/read_window.swift wecom_segmentation/read_window_main.swift -o .local/wecom-window-reader
sleep 5 && .local/venv/bin/python -m wecom_segmentation --db .local/window-test.sqlite3 capture-window --title '聊天顶部的完整会话名'
.local/venv/bin/python -m wecom_segmentation --db .local/window-test.sqlite3 classify
.local/venv/bin/python -m wecom_segmentation --db .local/window-test.sqlite3 sync --dry-run
.local/venv/bin/python -m wecom_segmentation --db .local/window-test.sqlite3 sync --window-test
```

同一会话继续使用同一份数据库。程序用上次末尾三条消息定位。上次只有两条时，用这两条定位。只追加其后的气泡，两条同文消息会分配独立的本地消息 ID。找不到唯一重叠位置时，请把窗口放大、确保前后批次的消息都能显示，再重试。会话标题用于校验目标。测试时请选择标题可区分的私聊。

当前只读取完整可见的文字气泡，适合短批次按需运行。附件、撤回和完整历史补采需要另外验证。企微气泡没有提供逐条可用的发送时间，本地记录使用采集时间排序，飞书中的消息证据时间和互动时间留空。带相对日期的预约需人工复核。

测试私聊的原文保存在本机 SQLite。飞书同步需要显式指定 `--window-test`，只发送已分类的结构化字段，数据范围标记为「企微窗口测试」。可先用 `sync --dry-run` 核对字段。样本和测试私聊使用各自的数据库。

## 指定客户私聊试跑

每条客户私聊使用自己的本地数据库。打开指定会话并停在最新消息，将下例的数据库路径和会话名替换为该会话的值。第二条会话使用另一份数据库。增量采集沿用同一条会话首次采集的路径和标题。

```sh
sleep 5 && .local/venv/bin/python -m wecom_segmentation --db .local/customer-pilot/a.sqlite3 capture-window --customer-pilot --title '聊天顶部的完整会话名'
.local/venv/bin/python -m wecom_segmentation --db .local/customer-pilot/a.sqlite3 classify
.local/venv/bin/python -m wecom_segmentation --db .local/customer-pilot/a.sqlite3 sync --dry-run
.local/venv/bin/python -m wecom_segmentation --db .local/customer-pilot/a.sqlite3 sync --customer-pilot
```

飞书按每条会话独立生成的线索 ID 更新已有记录。数据范围显示「企微获客」，同步内容为结构化分层字段。已有会话的本地分层结果会自动使用新名称。会话名和聊天原文保存在本机数据库。请使用已配置的飞书测试表。

## 接入飞书测试表

1. 在[飞书开发者后台](https://open.feishu.cn/app)创建企业自建应用，启用机器人。在「权限管理」为应用身份开通「查看、评论、编辑和管理多维表格（`bitable:app`）」。若租户要求审批，在「版本管理与发布」发布版本并等待生效。[飞书权限说明](https://open.feishu.cn/document/ukTMukTMukTM/uQjN3QjL0YzN04CN2cDN)
2. 在同一组织的「云盘」创建空白多维表格，打开要测试的数据表。在右上角「… → 更多 → 添加文档应用」选择自建应用并授予可编辑权限。若目标表开启了高级权限，按该表的权限设置为应用授权。[飞书添加文档应用教程](https://open.feishu.cn/document/agile-project-cycle-management-based-on-bitable/run) · [飞书多维表格权限说明](https://open.feishu.cn/document/docs/bitable-v1/faq)
3. 将「凭证与基础信息」中的 App ID、App Secret 和测试表的浏览器地址填入 `.local/feishu.env`。地址应包含 `/base/` 和 `table=tbl…`。密钥放在本地配置文件中，`.local/` 已由 Git 忽略。创建文件时可先执行 `umask 077` 限制访问权限。

```dotenv
FEISHU_APP_ID=cli_你的应用ID
FEISHU_APP_SECRET=你的应用密钥
FEISHU_TABLE_URL=https://你的团队.feishu.cn/base/你的多维表格标识?table=tbl你的数据表标识
```

在与导入和分层相同的数据库上运行以下命令。使用前一阶段生成的数据库时，把 `--db .local/sample-preview.sqlite3` 放在 `sync` 前面。

```sh
.local/venv/bin/python -m wecom_segmentation sync --dry-run
.local/venv/bin/python -m wecom_segmentation sync --setup
.local/venv/bin/python -m wecom_segmentation sync
```

`--setup` 在选中的测试表补齐字段，再同步已有的合成线索。常规同步按「线索 ID」查询，更新版本较旧的行，并保留「人工复核备注」。重复同步同一版本时，结果应计入「已是最新」。临时错误按退避间隔重试。进程中断后可用同一数据库重跑，程序会对账并复用持久化的创建请求。若终端提示证书验证失败，可用 `REQUESTS_CA_BUNDLE=/etc/ssl/cert.pem` 指定 macOS 系统证书。`--config` 可指定其他本地配置文件。

分类列使用固定选项。来源、来源依据、有效线索判定、当前阶段、跟进状态、咨询人身份、年级、学习目标、主要卡点、到课待核实、付费待核实和数据范围为单选。科目和人群标签为多选。线索 ID、证据摘要和人工复核备注使用文本，时间列使用日期，同步版本使用数字。已有表格的文本分类列仍可接收同步，可在飞书中将其转换为单选。

工作台从客户明确提供的文字中识别微信号，例如「我的微信号是 wx_example」，也支持运营问微信号、客户紧接着单独回复账号。识别结果显示在分层预览，出现多个不同号码时留空。同步前会读取飞书中的「微信ID」，只补填当时为空的栏位。人工填写已有值后，建议等填写完成再同步。已有会话可点击「重新本地分层」补识别。

「用户基本情况」在分层预览和飞书中显示聊天里明确提到的省市、年级、近期成绩、薄弱科目和学习困难。没有相关信息时留空。新学情更新同一条飞书记录，已手动修改或清空的内容以飞书为准。已有会话可点击「重新本地分层」，再点击「同步飞书」补填。

## 测试

```sh
.local/venv/bin/python -m unittest discover -s tests -v
WECOM_TEST_OLLAMA=1 .local/venv/bin/python -m unittest discover -s tests -v
```

第一条运行分层和飞书同步测试。飞书测试经过官方 SDK，使用模拟 HTTP 响应覆盖新增、更新、字段清空、去重及失败后恢复。第二条还会使用已启动的 Ollama 处理仓库内的合成会话，并检查主要业务结果。

在 Ollama 已运行时，可用下列命令重跑 30 组合成企微私聊。测试使用独立 SQLite 数据库和模拟飞书响应，并检查各组预期的分层字段、增量采集和重复同步。

```sh
WECOM_TEST_30=1 WECOM_TEST_OLLAMA=1 .local/venv/bin/python -m unittest discover -s tests -v
```

可设置 `WECOM_TEST_30_REPORT` 为本地 JSON 文件路径，保存每组实际结果和预期。
