from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import urllib.request
import json
import pandas as pd

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_API = "https://exam.sanand.workers.dev/questionData?email=24f2007837%40ds.study.iitm.ac.in&quizSign=EkzQojscK%2BrSdQZP49M3tU%2FCnIFw5jjkleiaDzrX%2Bfe3Oiz%2FMjsO%2FdVZEWr1HBJishd%2Fe0RoKBTorEiA3dZNGCFZ1ZNxjklW%2B%2BA7d2Hi4vJmE6ypj5RhSUYyfDd%2BonDdCW870rPjJ%2BauDa979q5gh1JLgNJRGn0RQsugIwsKUW0TtquRhk6vmGuAsIYZslGf0j9juOzuciFdS%2Fg339jtIuHtunWb8lZI07XTuvKAEC%2FOYIZ5J5bVTCowAH%2BTpqjV5fwCaWDz22SUNbyaWDYbMb0W9faker%2BeRh1Ger6VJsqJrXPm51aywe6wEPeI1Lwl0fjMEwb9gJ5B5%2BfHttwKng%3D%3D&questionId=q-ledger-agent-server&path="

@app.post("/")
async def answer_question(request: Request):
    body = await request.json()
    question = body.get("question", "")
    
    # Example logic for: "How many different customers placed at least one paid order for an Air Fryer?"
    # 1. Fetch export data
    req = urllib.request.Request(BASE_API + "/export", headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    
    df = pd.DataFrame(data)
    
    # Deduplicate by order_id keeping latest updated_at
    if 'updated_at' in df.columns:
        df = df.sort_values('updated_at').drop_duplicates(subset=['order_id'], keep='last')
        
    # Filter for paid status and product
    paid_df = df[df['status'].str.lower() == 'paid']
    
    # Compute answer based on question type
    if "Air Fryer" in question:
        air_fryer_orders = paid_df[paid_df['product'].str.contains('Air Fryer', case=False, na=False)]
        unique_customers = int(air_fryer_orders['customer_id'].nunique())
        return {"answer": unique_customers}
        
    return {"answer": 0}

@app.get("/")
async def health_check():
    return {"status": "active"}
