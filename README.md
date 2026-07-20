# Jenkins 自动打包工作流

这个工作流的目标是：用户只输入一段自然语言打包要求，脚本就自动解析产品、包型、版本、渠道号、混淆开关等参数，然后登录 Jenkins，自动选择对应任务并开始打包。

当前已按你给的规则预置：

- `纯A` -> 选择 `纯a打包`
- `A+B` 或 `纯B` -> 选择 `混淆 | 自动换分支`
- 产品名 -> 自动转全拼后填入 `BRANCH` 搜索框，例如 `称心如意` -> `chenxinruyi`；Jenkins 会自动过滤并选中完整分支（如 `g7/chenxinruyi`）
- `需加混淆` -> `enableProguard=true`
- `用A面icon` -> 默认把 `use_a_launch_theme` 置为 `true`

## 目录说明

- `pack_workflow.py`：主脚本
- `config.json`：Jenkins 地址、任务名、默认参数
- `requirements.txt`：依赖
- `web_app.py`：本地页面服务
- `web/index.html`：页面文件
- `start_web.command`：一键启动页面

## 安装

在 `jenkins_auto_pack` 目录下执行：

```bash
pip install -r requirements.txt --break-system-packages
python -m playwright install chromium
```

## 配置

先设置 Jenkins 登录账号密码：

```bash
export JENKINS_USERNAME="你的账号"
export JENKINS_PASSWORD="你的密码"
```

如果你不想每次登录，现在默认会复用 `jenkins_auto_pack/.playwright_profile` 里的本地登录态。
也就是说：

- 第一次如果检测到 Jenkins 登录页，可以勾选页面里的“可视化打开浏览器执行”
- 在弹出的浏览器里手动登录一次
- 登录态会保存在本地
- 后面再次打包时默认直接复用，不需要每次再输账号密码

如果你的 Jenkins 参数名和我按截图预置的不完全一致，改 `config.json` 即可。

重点可改项：

- `jobs.pure_a`
- `jobs.mixed_or_b`
- `parameter_defaults`
- `extra_parameter_mapping.filing_number`

说明：

- 我已经把你给的 Jenkins 地址写进 `config.json`
- `extra_parameter_mapping.filing_number` 目前默认写成了 `备案号`
- 如果你实际 Jenkins 中这个字段不是 `备案号`，改成真实参数名即可
- `BRANCH` 现在默认自动使用产品名称全拼，在 Jenkins 下方搜索框输入即可过滤出 `组号/拼音` 完整分支，不再需要维护产品分支映射表

## 页面模式

如果你更习惯点按钮操作，可以直接启动本地页面。

方式一：双击 `start_web.command`

方式二：命令行启动

```bash
python3 web_app.py
```

启动后浏览器打开：

```text
http://127.0.0.1:8899/
```

页面支持：

- 输入打包需求文案
- 先点“解析参数”预览识别结果
- 再点“开始打包”提交 Jenkins
- 可勾选“可视化打开浏览器执行”
- 默认复用本地持久化登录态，不再要求每次输入 Jenkins 账号密码

说明：

- 登录态默认保存在 `jenkins_auto_pack/.playwright_profile`
- 页面展示的任务名、产品分支、默认参数仍然来自 `config.json`

## 先做解析验证

先不要真的触发打包，建议先跑干跑模式：

```bash
python pack_workflow.py \
  --config config.json \
  --text "称心如意 小米辛苦出【纯 A 面无框架】包，版本：0.0.1，渠道号：qzcxryxiaomi，需加混淆，用A面icon，备案号为：鄂ICP备2026023347号-2A" \
  --dry-run
```

你应该会看到类似结果：

```json
{
  "product_name": "称心如意",
  "source_branch": "chenxinruyi",
  "job_name": "纯a打包",
  "package_mode": "pure_a",
  "version_name": "0.0.1",
  "version_code": "001",
  "channel": "qzcxryxiaomi",
  "enable_proguard": true
}
```

## 正式触发打包

```bash
python pack_workflow.py \
  --config config.json \
  --text "称心如意 小米辛苦出【纯 A 面无框架】包，版本：0.0.1，渠道号：qzcxryxiaomi，需加混淆，用A面icon，备案号为：鄂ICP备2026023347号-2A" \
  --headed
```

`--headed` 会打开可视化浏览器，便于你确认自动填写过程。

## 已实现逻辑

脚本会自动做这些事：

1. 解析自然语言中的产品名、包型、版本、渠道号、混淆、备案号
2. 将产品名称自动转换为全拼，并在 Jenkins `BRANCH` 下方搜索框输入，由 Jenkins 自动过滤出完整分支
3. 根据 `纯A / A+B / 纯B` 选择 Jenkins 任务
4. 登录 Jenkins
5. 打开参数化构建页
6. 按参数名查找输入框并自动填值
7. 提交构建

## 当前假设

因为我现在没有你的 Jenkins 账号，所以只验证到了登录页，没有实际提交构建。当前实现基于下面假设：

- Jenkins 任务名分别就是 `纯a打包` 和 `混淆 | 自动换分支`
- Jenkins 的 `BRANCH` 支持在下方搜索框输入产品名称全拼（如 `chenxinruyi`），自动过滤并选中完整分支（如 `g7/chenxinruyi`）
- 参数名和截图中的名称一致，比如 `BRANCH`、`CHANNELS`、`version_name`
- 备案号字段在页面里可按 `备案号` 这个名字找到

如果实际页面参数名稍有不同，只需要改 `config.json`，不需要改主脚本。
