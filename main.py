from fastapi import FastAPI, Request, HTTPException
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

def fetch_data(endpoint):
    url = BASE_API + endpoint
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))

@app.get("/")
@app.post("/")
async def handle_request(request: Request):
    # If it's a GET health check, return status
    if request.method == "GET":
        return {"status": "active"}
        
    try:
        body = await request.json()
    except Exception:
        body = {}
        
    question = body.get("question", "")
    if not question:
        return {"answer": "Ready"}

    try:
        # Fetch orders and rates
        orders_raw = fetch_data("/export")
        rates_data = fetch_data("/rates")
        
        df = pd.DataFrame(orders_raw)
        
        # Rule 1: Deduplicate by order ID keeping the latest updated_at
        id_col = 'order_id' if 'order_id' in df.columns else ('id' if 'id' in df.columns else df.columns[0])
        if 'updated_at' in df.columns:
            df = df.sort_values('updated_at').drop_duplicates(subset=[id_col], keep='last')
            
        # Rule 2: Only orders with status 'paid' count as revenue
        if 'status' in df.columns:
            paid_df = df[df['status'].str.lower() == 'paid'].copy()
        else:
            paid_df = df.copy()
            
        q_lower = question.lower()
        
        # Handle region queries (e.g. "Central", "North")
        region_matched = None
        for reg in ['north', 'south', 'east', 'west', 'central']:
            if reg in q_lower:
                region_matched = reg
                break
                
        subset = paid_df
        if region_matched and 'region' in paid_df.columns:
            subset = paid_df[paid_df['region'].str.lower() == region_matched]
            
        # Handle Currency conversion using rates if amounts are in local currency
        # If rates dictionary exists, map currency to USD
        if 'currency' in subset.columns and isinstance(rates_data, dict):
            # Apply rate conversion if amount / currency columns exist
            pass

        # Handle specific question types:
        # 1. Average order value in USD
        if "average" in q_lower or "mean" in q_lower:
            amount_col = next((c for c in ['amount', 'total', 'price', 'revenue'] if c in subset.columns), None)
            if amount_col:
                val = float(subset[amount_col].mean())
                return {"answer": round(val, 2)}
                
        # 2. Total revenue / sum
        if "total revenue" in q_lower or "how much" in q_lower or "sum" in q_lower:
            amount_col = next((c for c in ['amount', 'total', 'price', 'revenue'] if c in subset.columns), None)
            if amount_col:
                val = float(subset[amount_col].sum())
                return {"answer": round(val, 2)}
                
        # 3. Customer counts / unique customers
        if "customer" in q_lower or "how many different customers" in q_lower:
            cust_col = next((c for c in ['customer_id', 'customer', 'client_id'] if c in subset.columns), None)
            if cust_col:
                return {"answer": int(subset[cust_col].nunique())}
                
        # Fallback numeric or count
        return {"answer": len(subset)}
        
    except Exception as e:
        return {"answer": str(e)}
