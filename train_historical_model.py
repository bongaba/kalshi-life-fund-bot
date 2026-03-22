import pandas as pd
import json
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report
import joblib
from datetime import datetime

# Load historical markets data
with open('historical_markets.json', 'r') as f:
    historical_data = json.load(f)

# Convert to DataFrame
df = pd.DataFrame(historical_data)

# Filter for closed markets with results
df = df[df['status'] == 'determined']
df = df.dropna(subset=['result'])

print(f"Loaded {len(df)} historical markets with results")

# Feature engineering
def extract_features(row):
    # yes_price: use yes_bid at close (assuming it's the last price before close), convert cents to dollars
    yes_price = float(row.get('yes_bid', 0)) / 100.0

    # volume: convert to float
    volume = float(row.get('volume_fp', 0))

    # hours_to_close: calculate from close_time and expiration_time, ensure positive
    try:
        close_time = datetime.fromisoformat(row['close_time'].replace('Z', '+00:00'))
        exp_time = datetime.fromisoformat(row['expiration_time'].replace('Z', '+00:00'))
        hours_to_close = max(0, (exp_time - close_time).total_seconds() / 3600)
    except (KeyError, ValueError):
        hours_to_close = 24.0  # default fallback

    # title_keywords: extract sports type
    title = row.get('title', '').lower()
    sports_keywords = ['basketball', 'football', 'soccer', 'baseball', 'hockey', 'tennis', 'golf']
    market_category = 'other'
    for sport in sports_keywords:
        if sport in title:
            market_category = sport
            break

    # market_type: mostly binary
    market_type = row.get('market_type', 'binary')

    return {
        'yes_price': yes_price,
        'volume': volume,
        'hours_to_close': hours_to_close,
        'market_category': market_category,
        'market_type': market_type
    }

# Apply feature extraction
features_df = pd.DataFrame([extract_features(row) for _, row in df.iterrows()])

# Encode categorical features
features_df['market_category_encoded'] = features_df['market_category'].astype('category').cat.codes
features_df['market_type_encoded'] = features_df['market_type'].astype('category').cat.codes

# Select features for model
feature_cols = ['yes_price', 'volume', 'hours_to_close', 'market_category_encoded', 'market_type_encoded']
X = features_df[feature_cols].values  # Convert to numpy array to avoid feature name warnings

# Label: 1 for 'yes', 0 for 'no'
y = (df['result'] == 'yes').astype(int)

print(f"Features shape: {X.shape}, Labels shape: {y.shape}")
print(f"Label distribution: {y.value_counts()}")

# Split data
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# Train model
model = RandomForestClassifier(n_estimators=100, random_state=42)
model.fit(X_train, y_train)

# Evaluate
y_pred = model.predict(X_test)
accuracy = accuracy_score(y_test, y_pred)
print(f"Model Accuracy: {accuracy:.2f}")
print("Classification Report:")
print(classification_report(y_test, y_pred))

# Save model
joblib.dump(model, 'historical_model.pkl')
print("Model saved to historical_model.pkl")

# Feature importance
feature_importance = pd.DataFrame({
    'feature': feature_cols,
    'importance': model.feature_importances_
}).sort_values('importance', ascending=False)
print("Feature Importance:")
print(feature_importance)