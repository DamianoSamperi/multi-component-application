from flask import Flask, request, jsonify
import subprocess
import yaml
import json

app = Flask(__name__)

def generate_configmaps(pipeline):
    configmaps = []
    for step in pipeline["steps"]:
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"pipeline-step-{step['id']}"},
            "data": {
                "PIPELINE_CONFIG": yaml.dump({
                    "step_id": step["id"],
                    "steps": [step]
                }, sort_keys=False)
            }
        }
        configmaps.append(cm)
    return configmaps

def apply_configmap(cm):
    yaml_str = yaml.dump(cm, sort_keys=False)
    proc = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml_str.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        return f"❌ {proc.stderr.decode()}"
    else:
        return f"✅ {proc.stdout.decode()}"

@app.route("/pipeline", methods=["POST"])
def update_pipeline():
    try:
        pipeline = request.get_json()
        if not pipeline:
            return jsonify({"error": "Invalid JSON"}), 400

        results = []
        for cm in generate_configmaps(pipeline):
            results.append(apply_configmap(cm))

        return jsonify({"status": "ok", "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
