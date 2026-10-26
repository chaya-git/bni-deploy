from flask import Flask, jsonify, render_template
import joblib, pandas as pd, numpy as np

app = Flask(__name__)

model = joblib.load("model.pkl")
df = pd.read_csv("dataset.csv")

LABEL_COL = " Label"

attack_map = {
    0:"Normal Traffic",
    1:"DDoS Attack",
    2:"Port Scan",
    3:"Brute Force Attack"
}

xai_map = {
    0:"Normal network behavior detected.",
    1:"High volume packet flooding detected.",
    2:"Multiple port scanning activity detected.",
    3:"Repeated login failures detected."
}

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/predict")
def predict():
    row = df.sample(1)
    X = row.drop(LABEL_COL, axis=1).values

    pred = int(model.predict(X)[0])
    prob = float(model.predict_proba(X).max())

    severity = "High" if prob>0.85 else "Medium" if prob>0.65 else "Low"

    timeline = list(np.random.randint(0,5,10))

    return jsonify({
        "attack": attack_map[pred],
        "confidence": round(prob,2),
        "severity": severity,
        "xai": xai_map[pred],
        "timeline": timeline
    })

app.run(debug=True)
