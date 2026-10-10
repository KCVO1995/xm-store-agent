# 门店助手接入说明

1. 先完成 Octop 初始化，并由管理员配置可用的大模型。
2. 管理员创建「门店助手」Agent，开启共享，并在「聊天选项」中启用「门店」（内置门店助手默认勾选）。公司账号映射为普通 Octop 用户，能使用共享 Agent，但各自只看到自己的会话。不要直接共享管理员的私人 Agent 或工作区。
3. 用户在登录页使用公司账号和密码登录；管理员需要维护平台时可切换到「管理员本地登录」。公司登录调用 `/api/auth/xm-store/login`，使用 `XM_STORE_SUITE`；密码只经 Octop 后端转发给公司接口，不进入 Agent。
4. 启用「门店」的专家打开聊天页时，调用 `GET /api/xm-store/stores?agent_id=…`，输入框上方显示当前账号的门店选项。选择调用 `PUT /api/xm-store/selection`；选择保存在该会话中，并作为新会话的默认值。后端会重新校验门店权限。
5. 公司登录会为该用户自动创建私有的「我的门店」、BOH 和「遇学」连接器。「我的门店」的加密凭证供只读 MCP 工具 `list_my_stores` 使用。页面选项加载不调用 MCP；BOH 和遇学业务查询从服务端会话读取门店 ID，并在查询前验证权限。

门店列表接口普通故障会保留登录状态，界面提供重试；公司接口返回 `401/405/406` 会要求重新登录。由于公司登录接口把密码放在上游请求的查询参数中，部署时必须使用 HTTPS，并确认公司网关、反向代理和访问日志会隐藏查询参数；如上游提供 POST 请求体版本，应优先迁移。

## 企学宝课程选项

管理员可在创建或编辑专家时单独启用「企学宝课程」，无需启用门店。聊天窗按分页加载全部授权课程，按课程名称在前端即时筛选并支持多选，搜索时不会重新请求接口；「全部课程」对应空列表。新会话默认使用全部课程，不继承上一个会话的选择。

后端使用当前公司用户的加密 token 请求 `POST https://meetfun-talents.yujianxiaomian.com/meetfun-talents/talents/qiXueBaoCourse/queryPage`，请求头带 `systemtype: TALENTS`，固定传入 `isEnable: true`、`pageSize: 1000`，并按关键词与页码查询。页面通过 `GET /api/agents/{agent_id}/chat-context/courses` 读取列表，通过 `PUT /api/agents/{agent_id}/chat-context/courses/selection` 保存会话选择。后端在保存时检查当前用户、专家、会话和课程授权；课程业务工具调用 `authorized_courses_for_thread()` 获取可信课程范围，空列表表示全部已授权课程。

## 遇学连接器

公司登录时自动建立「遇学」连接器，服务启动时也会为已有公司账号补齐。连接器默认开启，仅保存公司连接器的引用；每次查询读取当前聊天用户的公司 token。连接器可启停或切换默认开启状态，凭证由公司登录管理，不能共享给其他账号。

使用专家时需启用「门店」和「企学宝课程」聊天选项。只读工具 `query_course_detail_page` 请求 `POST https://meetfun-talents.yujianxiaomian.com/meetfun-talents/talents/report/qiXueBao/getCourseListByPage`，携带 `token`、`systemtype: TALENTS` 请求头。后端固定设置 `chooseType: STORE`、`storeIdList: [当前会话门店]`、`courseIdList: [当前会话所选课程]`，查询前重新检查账号、会话和授权。模型不能填写这些范围参数。

默认 `courseStatus: PUBLISHED`（已发布/开放中）、`pageNum: 1`、`pageSize: 100`，每页最多 1000 条。支持课程名称、人员名称、人员 ID、学习状态、完成时间和加入时间筛选。工具保留分页信息及课程、人员原始明细；模型需继续翻页才能获取完整结果。未选择门店、所选课程权限被撤销或公司登录过期时，工具返回对应提示。
