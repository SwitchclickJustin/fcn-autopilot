#!/bin/bash
# FCN Controller Launcher
# Sets up environment and starts the controller

# Ensure browser-use is in PATH
export PATH="$HOME/.browser-use-env/bin:$PATH"

# Copy Hermes OpenRouter key into env for the controller
if grep -q "OPENROUTER_API_KEY" ~/.hermes/.env 2>/dev/null; then
    # Source the actual key from Hermes env (it's set, just redacted in display)
    eval "$(grep 'OPENROUTER_API_KEY' ~/.hermes/.env | grep -v '^#')"
fi

echo "🚀 Starting FCN Controller..."
echo "   Open http://localhost:8765 in your browser"
echo "   Press Ctrl+C to stop"
echo ""

cd "$(dirname "$0")"
python3 fcn_controller.py