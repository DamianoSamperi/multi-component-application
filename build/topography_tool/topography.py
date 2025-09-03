from flask import Flask, request, jsonify
import subprocess
import yaml
import json
from typing import Union, List, Dict

app = Flask(__name__)

def flatten_steps(steps: List[Union[Dict, List]], start_id=0):
    """
    Trasforma lo schema JSON semplificato in una lista di step lineari
    con id assegnati e collegamenti next_step calcolati.
    """
    flat = []
    current_id = start_id

    for idx, step in enumerate(steps):
        if isinstance(step, dict):
            # Step singolo
            step_obj = {
                "id": current_id,
                "type": step["type"],
                "params": step.get("params", {}),
                "preferred_next": step.get("preferred_next")
            }

            # collegamento lineare di default
            if idx < len(steps) - 1:
                if isinstance(steps[idx + 1], list):
                    # biforcazione
                    next_ids = list(range(current_id + 1,
                                          current_id + 1 + len(steps[idx + 1])))
                else:
                    next_ids = [current_id + 1]
                step_obj["next_step"] = next_ids

            flat.append(step_obj)
            current_id += 1

        elif isinstance(step, list):
            # Biforcazione → ogni ramo diventa un nuovo step
            for sub in step:
                step_obj = {
                    "id": current_id,
                    "type": sub["type"],
                    "params": sub.get("params", {})
                }
                # next_step verrà gestito dal ramo successivo (se c’è)
                step_obj["next_step"] = []
                flat.append(step_obj)
                current_id += 1

    return flat

def generate_configmaps(pipeline_json: Dict, namespace="default"):
    steps = flatten_steps(pipeline_json["steps"])

    configmaps = []
    for step in steps:
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"pipeline-step-{step['id']}",
                "namespace": namespace
            },
            "data": {
                "PIPELINE_CONFIG": yaml.dump({
                    "step_id": step["id"],
                    "steps": [{
                        "id": step["id"],
                        "type": step["type"],
                        "params": step["params"],
                        **({"next_step": step["next_step"]} if "next_step" in step else {}),
                        **({"preferred_next": step["preferred_next"]} if step.get("preferred_next") else {})
                    }]
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
