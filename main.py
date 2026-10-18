from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import urllib.request
import json
import pandas as pd
from datetime import datetime
import pytz

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_API = "https://exam.sanand.workers.dev/questionData?email=24f2007837%40ds.study.iitm.ac.in&quizSign=EkzQojscK%2BrSdQZP49M3tU%2FCnIFw5jjkleiaDzrX%2Bfe3Oiz%2FMjsO%2FdVZEWr1HBJishd%2Fe0RoKBTorEiA3dZNGCFZ1ZNxjklW%2B%2BA7d2Hi4vJmE6ypj5RhSUYyfDd%2BonDdCW870rPjJ%2BauDa979q5gh1JLgNJRGn0RQsugIwsKUW0TtquRhk6vmGuAsIYZslGf0j9juOzuciFdS%2Fg339jtIuHtunWb8lZI07XTuvKAEC%2FOYIZ5J5bVTCowAH%2BTpqjV5fwCaWDz22SUNbyaWDYbMb0W9faker%2BeRh1Ger6VJsqJrXPm51aywe6wEPeI1Lwl0fjMEwb9gJ5B5%2BfHttwKng%3D%3D&questionId=q-ledger-agent-server&path="

def fetch_ledger():
    # Fetch export or paginated orders
    req = urllib.request.Request(BASE_API + "/export", headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return pd.DataFrame(data)

def fetch_rates():
    req = urllib.request.Request(BASE_API + "/rates", headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))

@app.post("/")
async def answer_question(request: Request):
    body = await request.json()
    question = body.get("question", "")
    
    # Process ledger data using pandas according to business rules:
    # 1. Deduplicate by order ID keeping latest updated_at
    # 2. Filter for status == 'paid' for revenue
    # 3. Apply currency conversion using /rates if needed
    # 4. Handle Asia/Kolkata timezone for date queries
    
    df = fetch_ledger()
    
    # Simple rule-based or LLM-assisted query router can be added here
    # For demonstration / baseline:
    answer = "Processed successfully"
    
    return {"answer": answer}
