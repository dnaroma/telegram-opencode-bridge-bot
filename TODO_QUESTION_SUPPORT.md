# 待修复：Question Tool 支持

## 问题描述

当 OpenCode 使用 `question` tool 询问用户问题时，bot 会卡住，不会将问题转发到 Telegram。

## 当前支持的交互

目前 bot 只支持以下交互类型：

1. **权限请求** (`permission.asked`)
   - 工具执行权限
   - 文件访问权限
   - 显示为按钮：✅ Yes, Allow / ❌ No, Deny

2. **工具执行状态** (`message.part.updated`)
   - 工具调用通知
   - 工具完成通知
   - 工具失败通知

## 缺失的功能

**Question Tool 支持**：
- 事件类型：待确认（可能是 `question.asked` 或类似）
- 需要转发到 Telegram 的内容：
  - 问题文本
  - 选项列表（如果有）
  - 用户选择后的回复

## 需要的信息

1. **事件类型**：OpenCode 发送的 question 事件名称
2. **事件结构**：事件的 payload 格式
3. **回复 API**：如何将用户的答案发回给 OpenCode

## 可能的实现方案

### 方案 A：内联按钮（推荐）

```python
elif event_type == "question.asked":  # 待确认事件名
    question_id = properties.get("id")
    question_text = properties.get("question")
    options = properties.get("options", [])
    
    # 创建按钮
    keyboard = []
    for option in options:
        keyboard.append([InlineKeyboardButton(
            option["label"], 
            callback_data=f"answer:{question_id}:{option['value']}"
        )])
    
    await update.message.reply_text(
        f"❓ <b>Question from OpenCode</b>\n\n{question_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
```

### 方案 B：文本回复

如果没有预定义选项，让用户直接回复文本。

## 测试场景

1. **选择题**：
   - OpenCode 问："使用哪个 package manager？"
   - 选项：npm, yarn, pnpm
   - 用户点击按钮选择

2. **自由文本**：
   - OpenCode 问："请描述你想要的功能"
   - 用户输入文本回复

## 相关代码位置

- **事件处理**：`handlers/messages.py` 第 599 行附近
- **回调处理**：`handlers/commands.py` 第 1760 行附近（参考 permission 处理）

## 下一步

1. 确认 OpenCode question 事件的确切格式
2. 实现事件处理和按钮显示
3. 实现回复 API 调用
4. 测试各种问题类型

## 参考

- Permission 处理：`handlers/messages.py:599-648`
- Callback 处理：`handlers/commands.py:1740-1791`
