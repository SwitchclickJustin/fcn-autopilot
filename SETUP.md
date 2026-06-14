# FCN Auto-Pilot — Setup Checklist

## What You Need to Provide

### 1. Railway Account & Project
- [ ] **Railway account** at [railway.app](https://railway.app)
- [ ] **Install Railway CLI**: `brew install railway` (macOS) or `npm i -g @railway/cli`
- [ ] **GitHub repo** to connect Railway to (or Railway CLI direct deploy)

### 2. Browser Use Cloud (✅ Already Have)
- Account: ✅ Done
- API Key: ✅ `bu_4u7sTWfzkVLTJD-0gydwY8OqWkoUxvE2r9F653J3DAc`
- Dashboard: [cloud.browser-use.com](https://cloud.browser-use.com)

### 3. LLM Provider (AI Brain — Need at least one)
Pick one or more:

| Provider | Sign Up | Cost | Notes |
|----------|---------|------|-------|
| **OpenRouter** | [openrouter.ai](https://openrouter.ai/keys) | Pay-as-you-go, ~$0.15/million tokens | Access to 200+ models. Best option. |
| **OpenAI** | [platform.openai.com](https://platform.openai.com/api-keys) | GPT-4o-mini ~$0.15/million | Simple, reliable |
| **Anthropic** | [console.anthropic.com](https://console.anthropic.com) | Claude Haiku ~$0.25/million | Good for supervisor role |

**Recommended:** OpenRouter API key (you already have one in Hermes — grab it from openrouter.ai/keys)

### 4. Decodo Proxy (Optional — for IP rotation)
- [ ] **Decodo account** at [decodo.com](https://decodo.com)
- [ ] **Proxy credentials** (IP:PORT or SOCKS5 URL)
- Need to know: format (HTTP/SOCKS5), how many IPs, regions available

### 5. Domain/Custom URL (Optional)
- Railway gives you `*.up.railway.app` for free
- Custom domain optional

---

## When You Have Those, I'll Need You To:

1. **Create a Railway project** at [railway.app](https://railway.app) → New Project
2. **Link your GitHub** or use Railway CLI
3. **Tell me the Railway project name** — I'll push the code with the right config
4. **Send me your OpenRouter (or other LLM) API key** — for the auto-pilot and supervisor

---

## What I'm Building Now

I'll start scaffolding the entire project. When you're ready with the checklist, we deploy together.