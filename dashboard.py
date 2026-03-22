import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import sys
import os

# Add the current directory to path so we can import position_monitor
sys.path.append(os.path.dirname(__file__))
from position_monitor import get_current_positions, get_market_price

st.set_page_config(page_title="Life-Saving Fund", layout="wide")
st.title("💰 Kalshi Life-Saving Fund Dashboard")

conn = sqlite3.connect('trades.db')
df = pd.read_sql_query("SELECT * FROM trades", conn)

if df.empty:
    st.info("No trades yet. Bot is running in background.")
    st.stop()

df['timestamp'] = pd.to_datetime(df['timestamp'])
df = df.sort_values('timestamp', ascending=False)

# Statistics
total_trades = len(df)
won = len(df[df['status'] == 'WON'])
lost = len(df[df['status'] == 'LOST'])
pending = len(df[df['status'].isin(['PLACED', 'OPEN'])])
success_rate = (won / (won + lost) * 100) if (won + lost) > 0 else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Trades", total_trades)
col2.metric("Won Trades", won)
col3.metric("Lost Trades", lost)
col4.metric("Success Rate", f"{success_rate:.1f}%")

st.info(f"Pending/Open: {pending} trades")

# Chart
if 'pnl' in df.columns and df['pnl'].sum() != 0:
    cumulative = df['pnl'].cumsum()
    fig = px.line(x=df['timestamp'], y=cumulative, title="Cumulative P&L")
    st.plotly_chart(fig, use_container_width=True)

# Table
st.subheader("Trades")
st.dataframe(
    df[['timestamp', 'market_ticker', 'direction', 'size', 'price', 'pnl', 'status', 'reason']],
    use_container_width=True
)

# Current Positions Monitor
st.subheader("📊 Current Open Positions")

try:
    positions = get_current_positions()
    if positions:
        position_data = []
        total_unrealized_pnl = 0
        
        for position in positions:
            ticker = position.get('ticker')
            pos = position.get('position', 0)
            exposure = float(position.get('market_exposure_dollars', '0'))
            
            if pos == 0:
                continue
                
            # Determine direction from position sign
            direction = "YES" if pos > 0 else "NO"
            size = abs(exposure)
            
            # Get current market price
            current_price = get_market_price(ticker)
            
            # Get entry price from database
            conn_check = sqlite3.connect('trades.db')
            cursor_check = conn_check.cursor()
            cursor_check.execute("""
                SELECT price FROM trades 
                WHERE market_ticker = ? AND status = 'OPEN'
                ORDER BY timestamp DESC LIMIT 1
            """, (ticker,))
            entry_result = cursor_check.fetchone()
            conn_check.close()
            
            if entry_result:
                entry_price = entry_result[0]
                
                # Calculate unrealized P&L
                if direction == 'YES':
                    unrealized_pnl = size * (current_price - entry_price)
                else:
                    unrealized_pnl = size * (entry_price - current_price)
                
                total_unrealized_pnl += unrealized_pnl
                
                position_data.append({
                    'Market': ticker,
                    'Direction': direction,
                    'Size ($)': f"${size:.2f}",
                    'Entry Price': f"{entry_price:.3f}",
                    'Current Price': f"{current_price:.3f}",
                    'Unrealized P&L': f"${unrealized_pnl:.2f}"
                })
        
        if position_data:
            pos_df = pd.DataFrame(position_data)
            st.dataframe(pos_df, use_container_width=True)
            
            # Summary metrics
            col1, col2 = st.columns(2)
            col1.metric("Active Positions", len(position_data))
            col2.metric("Total Unrealized P&L", f"${total_unrealized_pnl:.2f}")
        else:
            st.info("No active positions found")
    else:
        st.info("Unable to fetch current positions")
        
except Exception as e:
    st.error(f"Error loading positions: {str(e)}")
    st.info("Make sure your API keys are configured correctly")