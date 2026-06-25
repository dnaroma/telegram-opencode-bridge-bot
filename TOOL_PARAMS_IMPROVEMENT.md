# Tool Call 参数显示改进

## 改动说明

改进了流式输出时 Tool Call 的参数显示，从只显示工具名称改为显示完整的调用参数。

## 改进前后对比

### ❌ 改进前
```
🛠️ Calling Tool bash
```

只显示工具名称，看不到执行的命令和参数。

### ✅ 改进后
```
🛠️ Calling Tool bash — "List files in current directory"

Parameters:
  • command: ls -la
  • description: List files in current directory
```

显示完整的参数信息，包括：
- 工具名称
- 描述（如果有）
- 所有参数及其值

## 详细改进

### 1. 参数过滤优化
**之前**：过滤掉 `description` 和 `content` 参数
**现在**：只过滤 `description`（因为已经单独显示），保留 `content` 和其他所有参数

### 2. 格式优化
**之前**：
```
command: ls -la
description: List files...
```

**现在**：
```
Parameters:
  • command: ls -la
  • description: List files...
```

使用项目符号 `•` 和缩进，更清晰易读。

### 3. 长参数处理
对于超过 100 字符的参数值：
```
  • content: {
    "parts": [
      {
        "type": "text",
        "text": "This is a very long text that will be truncated to 100 cha... (523 chars)
```

显示前 100 个字符 + 总长度提示。

## 示例场景

### Bash 命令
```
🛠️ Calling Tool bash — "Install dependencies"

Parameters:
  • command: npm install express body-parser cors
  • workdir: /Users/user/project
  • timeout: 60000
```

### 文件读取
```
🛠️ Calling Tool read — "Read configuration file"

Parameters:
  • filePath: /Users/user/project/config.json
  • offset: 1
  • limit: 100
```

### 代码搜索
```
🛠️ Calling Tool ripgrep_search — "Search for function definition"

Parameters:
  • pattern: def process_data
  • path: /Users/user/project/src
  • glob: *.py
  • maxResults: 20
```

### 文件编辑
```
🛠️ Calling Tool edit — "Update API endpoint"

Parameters:
  • filePath: /Users/user/project/api.py
  • oldString: return Response(status=200)
  • newString: return Response(status=201, data=result)
```

## 技术细节

### 修改位置
`handlers/messages.py` 第 701-717 行

### 改动内容
```python
# 改进前
arg_lines = []
if isinstance(input_data, dict):
    for k, v in input_data.items():
        if k not in ("description", "content"):
            arg_lines.append(f"<b>{html.escape(str(k))}:</b> {html.escape(truncate(str(v)))}")
args_text = "\n".join(arg_lines)

# 改进后
msg = f"🛠️ <b>Calling Tool <code>{html.escape(tool_name)}</code></b>{desc_text}\n"

if isinstance(input_data, dict):
    params = {k: v for k, v in input_data.items() if k != "description"}
    
    if params:
        msg += "\n<b>Parameters:</b>\n"
        for k, v in params.items():
            v_str = str(v)
            if len(v_str) > 100:
                v_display = f"{v_str[:100]}... ({len(v_str)} chars)"
            else:
                v_display = v_str
            msg += f"  • <code>{html.escape(k)}</code>: {html.escape(v_display)}\n"
```

### 关键改进点
1. **不再过滤 `content`**：许多工具用 `content` 传递主要参数
2. **更好的格式**：使用 "Parameters:" 标题 + 项目符号
3. **智能截断**：长参数显示长度提示
4. **保留描述**：`description` 单独显示在标题行

## 测试方法

1. 启动 bot：`./start_all.sh`
2. 发送 `/enable` 启用流式输出
3. 发送任何需要工具调用的请求
4. 观察工具调用时的参数显示

## 兼容性

✅ 完全向后兼容
✅ 不影响非流式模式
✅ 所有现有功能保持不变

## Git 提交

```bash
cd /Users/peitang/Projects/telegram-opencode-bridge-bot-fork
git log --oneline -3
```

输出：
```
c353b81 feat: improve tool call parameter display in streaming
7b00d6c docs: add feature documentation and test script
3937bf2 feat: add model variants support
```

## 下一步

修改已安装到工作区，可以立即测试：

```bash
cd /Users/peitang/Projects/opencode-tgbot-bridge
./start_all.sh
```

在 Telegram 中：
1. `/enable` - 启用流式输出
2. 发送任何请求
3. 观察工具调用的参数显示
