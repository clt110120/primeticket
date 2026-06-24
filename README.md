# Prime Ticket — Prime Lanka Tours

Upload airline itinerary PDFs → Groq AI (Llama 3.3) extracts flight data → Branded e-ticket PDF.

## Get Free Groq API Key (No card needed)

1. Go to console.groq.com
2. Sign up with Google or email
3. Click API Keys → Create API Key
4. Copy the key (starts with gsk_...)

## Deploy to Railway

1. Push this repo to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variable: GROQ_API_KEY=gsk_...
4. Done — live in 60 seconds

## Local Development

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_your_key_here
python app.py
# Open http://localhost:5000
```

## Free Limits (Groq)
- 14,400 requests/day
- 6,000 tokens/minute on Llama 3.3 70B
- No credit card required
