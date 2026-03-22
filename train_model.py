import json
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report
import joblib
import os

# Load data from demo_trades.json
DEMO_TRADES_FILE = 'demo_trades.json'
try:
    with open(DEMO_TRADES_FILE, 'r') as f:
        trades_data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    print("No demo_trades.json found or invalid JSON. Run demo_bot.py first to generate data.")
    exit()

# Create table (DataFrame) and insert data
df = pd.DataFrame(trades_data)
print(f"Loaded {len(df)} trades into DataFrame table.")

if df.empty or len(df) < 50:
    print("Not enough resolved trades for training. Need at least 50. Keep running demo_bot.py.")
    exit()

# Filter resolved trades (query-like operation)
df_resolved = df[df['status'].isin(['WON', 'LOST'])]
print(f"Training on {len(df_resolved)} resolved trades.")

# Feature engineering (add columns)
df_resolved = df_resolved.copy()
df_resolved['yes_price'] = df_resolved['price']
df_resolved['direction_encoded'] = df_resolved['direction'].map({'YES': 1, 'NO': 0})
df_resolved['market_category'] = df_resolved['title'].str.lower().str.contains('election|political').astype(int)

df_resolved['target'] = (df_resolved['pnl'] > 0).astype(int)

# Select features
features = ['yes_price', 'direction_encoded', 'market_category']  # Add 'volume', 'hours_to_close' if available
X = df_resolved[features]
y = df_resolved['target']

# Train model
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

model = RandomForestClassifier(n_estimators=100, random_state=42)
model.fit(X_train, y_train)

# Evaluate
y_pred = model.predict(X_test)
accuracy = accuracy_score(y_test, y_pred)
print(f"Model Accuracy: {accuracy:.2f}")
print(classification_report(y_test, y_pred))

# Save model
joblib.dump(model, 'trading_model.pkl')
print("Model saved as 'trading_model.pkl'. Batch training complete.")