from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join
import json
import re

# ── Dummy responses keyed by keyword ─────────────────────────────────────────
DUMMY_RESPONSES = {
    "plot": {
        "response": "Here's a matplotlib plot for your data:",
        "code": """\
import matplotlib.pyplot as plt
import numpy as np

# Replace with your actual data
x = np.linspace(0, 10, 100)
y = np.sin(x)

fig, ax = plt.subplots(figsize=(10, 4))
ax.plot(x, y, linewidth=2, color='steelblue', label='sin(x)')
ax.fill_between(x, y, alpha=0.1, color='steelblue')
ax.set_title('Data Plot', fontsize=14)
ax.set_xlabel('X')
ax.set_ylabel('Y')
ax.legend()
ax.grid(axis='y', linestyle='--', alpha=0.4)
plt.tight_layout()
plt.show()"""
    },
    "clean": {
        "response": "Here's how to clean null values in your DataFrame:",
        "code": """\
import pandas as pd

def clean_nulls(df: pd.DataFrame) -> pd.DataFrame:
    # Drop columns where more than 50% values are null
    thresh = int(len(df) * 0.5)
    df = df.dropna(thresh=thresh, axis=1)
    # Fill numeric nulls with median
    num_cols = df.select_dtypes(include='number').columns
    df[num_cols] = df[num_cols].fillna(df[num_cols].median())
    # Fill string nulls with mode
    str_cols = df.select_dtypes(include='object').columns
    for col in str_cols:
        df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else '')
    return df

# Usage
df = clean_nulls(df)
print(f"Shape after cleaning: {df.shape}")
df.head()"""
    },
    "train": {
        "response": "Here's a train/test split with scaling:",
        "code": """\
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

# Assuming df is your DataFrame and 'target' is the label column
X = df.drop('target', axis=1)
y = df['target']

X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y
)

scaler = StandardScaler()
X_train = scaler.fit_transform(X_train)
X_test  = scaler.transform(X_test)

print(f"Train size: {len(X_train):,}")
print(f"Test size:  {len(X_test):,}")"""
    },
    "explain": {
        "response": "This code defines a function and calls it. Here's a breakdown:",
        "code": None
    },
    "group": {
        "response": "Here's how to group and aggregate your DataFrame:",
        "code": """\
import pandas as pd

# Replace 'category' and 'value' with your actual column names
summary = df.groupby('category').agg(
    total   = ('value', 'sum'),
    average = ('value', 'mean'),
    count   = ('value', 'count'),
    max_val = ('value', 'max')
).reset_index()

print(summary)"""
    },
}

DEFAULT_RESPONSE = {
    "response": "Here's a general Python snippet to get you started:",
    "code": """\
import pandas as pd
import numpy as np

# Load your data
df = pd.read_csv('your_file.csv')

# Quick exploration
print(f"Shape: {df.shape}")
print(df.dtypes)
print(df.describe())
df.head()"""
}


def match_response(prompt: str) -> dict:
    prompt_lower = prompt.lower()
    for keyword, resp in DUMMY_RESPONSES.items():
        if keyword in prompt_lower:
            return resp
    return DEFAULT_RESPONSE


class LLMAskHandler(APIHandler):
    """
    POST /llm-assistant/llm/ask
    Body:  { "prompt": "..." }
    Returns: { "response": "...", "code": "..." or null }

    To connect your real LLM API replace the dummy block with:
        import requests
        resp = requests.post(
            YOUR_LLM_API_URL,
            headers={"Authorization": f"Bearer {YOUR_TOKEN}"},
            json={"prompt": data["prompt"]},
            timeout=60
        )
        result = resp.json()
        response_text = result.get("response", "")
        code = result.get("code", None)
    """

    def post(self):
        data = json.loads(self.request.body)
        prompt = data.get("prompt", "")

        # ── DUMMY: replace this block when connecting real API ────────────
        matched = match_response(prompt)
        response_text = matched["response"]
        code = matched.get("code")
        # ─────────────────────────────────────────────────────────────────

        self.finish(json.dumps({
            "response": response_text,
            "code": code,        # null if no code, string if there is code
            "prompt": prompt
        }))


def setup_handlers(web_app):
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    route_pattern = url_path_join(base_url, "llm-assistant", "llm/ask")
    handlers = [(route_pattern, LLMAskHandler)]
    web_app.add_handlers(host_pattern, handlers)
