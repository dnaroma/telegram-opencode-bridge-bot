# Model Variants Support

## Overview
This feature adds support for selecting model variants (e.g., claude-opus-4 regular vs extended) in the Telegram bot.

## Changes Made

### 1. Modified `handlers/commands.py`

#### Model Button Display (lines ~1859-1872)
- Models are now checked for `variants` field
- If variants exist, button shows with `▶` indicator and uses `modelvariants:` callback
- If no variants, button directly uses `model:` callback (existing behavior)

#### New Callback Handler: `modelvariants:` (lines ~1805-1868)
- Triggered when user taps a model with variants
- Fetches model details from OpenCode API
- Displays a submenu with all available variants
- Each variant button uses `modelvariant:` callback
- Includes back navigation to provider models list

#### New Callback Handler: `modelvariant:` (lines ~1870-1886)
- Triggered when user selects a specific variant
- Parses the full path: `provider/model/variant`
- Sets the model with variant to user's session
- Shows confirmation message

## How It Works

### User Flow
1. User runs `/models` command
2. Selects a provider (e.g., Anthropic)
3. Sees list of models:
   - `🤖 Claude Opus 4 ▶` (has variants)
   - `🤖 Claude Sonnet 3.5` (no variants)
4. If tapping a model with variants:
   - Shows variants submenu with options like:
     - `✨ Regular`
     - `✨ Extended`
5. Selecting a variant sets model to `anthropic/claude-opus-4/extended`

### Expected API Data Structure
```json
{
  "all": [
    {
      "id": "anthropic",
      "name": "Anthropic",
      "models": {
        "claude-opus-4": {
          "name": "Claude Opus 4",
          "variants": [
            {"id": "regular", "name": "Regular"},
            {"id": "extended", "name": "Extended"}
          ]
        },
        "claude-sonnet-3.5": {
          "name": "Claude Sonnet 3.5"
        }
      }
    }
  ],
  "connected": ["anthropic"]
}
```

## Testing

To test this feature:

1. Ensure your OpenCode server supports the variants field in `/provider` endpoint
2. Run the bot: `uv run telegram-opencode-bot`
3. Send `/models` to the bot
4. Select a provider with models that have variants
5. Tap a model with the `▶` indicator
6. Select a variant from the submenu

## Backwards Compatibility

- ✅ Models without `variants` field work exactly as before
- ✅ Empty `variants` array is treated as no variants
- ✅ Existing model selection flow unchanged for models without variants

## Installation

### Development Setup
```bash
cd /Users/peitang/Projects/opencode-tgbot-bridge
uv sync
```

The workspace is configured to use the local fork at:
`/Users/peitang/Projects/telegram-opencode-bridge-bot-fork`

## Next Steps

### To push to your GitHub fork:
```bash
cd /Users/peitang/Projects/telegram-opencode-bridge-bot-fork

# Set your fork as remote (replace YOUR_USERNAME)
git remote set-url origin https://github.com/YOUR_USERNAME/telegram-opencode-bridge-bot.git

# Push the feature branch
git push -u origin feature/model-variants-support
```

### To create a Pull Request:
1. Visit your fork on GitHub
2. Click "Compare & pull request" for the `feature/model-variants-support` branch
3. Write a description explaining the feature
4. Submit to the original repository

## Future Enhancements

Possible improvements:
- Show variant descriptions if available in API
- Add icons/emojis to differentiate variant types
- Cache variants to reduce API calls
- Add keyboard shortcuts for common variants
