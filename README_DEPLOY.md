# Deploy the TradingView webhook app

## Render (recommended)
1. Create a new Render Web Service from this repository.
2. Set the build command to: `pip install -r requirements.txt`
3. Set the start command to: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add these environment variables:
   - `TRADINGVIEW_WEBHOOK_SECRET=your-secret`
   - `OPENAI_API_KEY=your-key` (optional)
5. Deploy.

Your webhook URL will be:
- `https://<your-render-app>.onrender.com/api/v1/integration/tradingview/webhook`

## Local test
Run:
- `c:/Users/joshb/OneDrive/Desktop/.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

Test:
- `http://127.0.0.1:8000/api/v1/integration/tradingview/webhook`
